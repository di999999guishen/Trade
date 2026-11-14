from __future__ import annotations

import json
import os
import subprocess
import re
import sys
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .config import resolve_project_path
from .config import market_data_dir
from .data import load_cache
from .features import build_samples
from .screening import latest_screen
from .store import connect, record_agent_analysis
from .chanlun_lite import signal as chanlun_signal_fn


def _ticker(row: dict) -> str:
    return row["symbol"] + (".SS" if row["exchange"] == "SSE" else ".SZ")


def _safe_tail(value: str) -> str:
    value = re.sub(r"(?i)(api[ _-]?key[^\n:]*[:=]\s*)[^\s,'\"}]+", r"\1[REDACTED]", value)
    return value[-2000:]


# 一次失败是否会让**整批**候选同样失败。凭证失效与限流都属这一类：继续把剩下的候选
# 跑完只会把一次故障放大成 N 次失败。旧实现正是如此——429 被判成 "runtime"，
# 既不重试也不熔断，剩余候选逐个重跑、逐个失败（记忆 R7 挂账未修项）。
_BATCH_FATAL_CLASSES = frozenset({"authentication", "rate_limited"})

_AUTH_MARKERS = ("authentication", "unauthorized", "invalid api key", "invalid_api_key",
                 "insufficient balance", "account is not active")
_RATE_LIMIT_MARKERS = ("rate limit", "rate_limit", "ratelimit", "too many requests",
                       "当前访问量过大", "访问量过大", "请求过于频繁", "限流", "quota exceeded")


def _http_status_present(text: str, status: int) -> bool:
    """匹配独立出现的 HTTP 状态码，避免把 4290ms / 1401 这类数字误判成 429/401。"""
    return re.search(rf"(?<!\d){status}(?!\d)", text) is not None


def _classify_failure(returncode: int, stderr_tail: str) -> str | None:
    """把一次 LLM 子进程失败归类，供批次熔断与退避重试决策使用。

    - ``None``：成功。
    - ``authentication``：凭证问题，后续候选必然同败 ⇒ 熔断。
    - ``rate_limited``：限流（HTTP 429 / 供应商限速文案），可退避重试；重试耗尽后熔断。
    - ``timeout`` / ``runtime``：单只问题，不牵连整批。
    """
    if not returncode:
        return None
    lowered = stderr_tail.lower()
    if any(marker in lowered for marker in _AUTH_MARKERS) or _http_status_present(stderr_tail, 401):
        return "authentication"
    if _http_status_present(stderr_tail, 429) or any(marker in lowered for marker in _RATE_LIMIT_MARKERS):
        return "rate_limited"
    if returncode == 124 or "TIMEOUT after" in stderr_tail:
        return "timeout"
    return "runtime"


def _decision(stdout: str) -> str | None:
    matches = re.findall(r"^[ \t]*Final decision:[ \t]*(.*)$", stdout, re.IGNORECASE | re.MULTILINE)
    if not matches:
        return None
    if len(matches) == 1:
        return matches[0].strip() or None
    # Keep conflicting final fields visible to the parser instead of choosing one.
    return "\n".join(f"Final decision: {value.strip()}" for value in matches)


_RATING_ALIASES = {
    "buy": "Buy", "买入": "Buy",
    "overweight": "Overweight", "增持": "Overweight",
    "hold": "Hold", "持有": "Hold", "观望": "Hold",
    "underweight": "Underweight", "减持": "Underweight",
    "sell": "Sell", "卖出": "Sell",
}
_RATING_INTENTS = {
    "Buy": ("enter_or_add", "up"),
    "Overweight": ("increase_exposure", "unclassified"),
    "Hold": ("maintain_or_wait", "neutral"),
    "Underweight": ("reduce_exposure", "unclassified"),
    "Sell": ("exit_or_avoid", "down"),
}
_DECISION_FIELD = re.compile(
    r"^\s*(?:#{1,6}\s*)?(?:\*\*|__)?\s*"
    r"(?:final decision|rating|recommendation|action|最终决策|最终评级|评级|建议|操作)"
    r"\s*(?:\*\*|__)?\s*[:：]\s*(?:\*\*|__)?\s*(.*?)\s*$",
    re.IGNORECASE,
)


def _rating_token(value: str) -> str | None:
    token = value.strip().rstrip("。.!！").strip(" *_`\t")
    if token.startswith("建议"):
        token = token[2:].strip(" *_`\t")
    return _RATING_ALIASES.get(token.casefold())


def decision_semantics(value: str | None) -> dict:
    """Read explicit ratings; never infer an action from words inside a narrative.

    Overweight/Underweight express exposure changes, not predictions of positive
    or negative absolute returns. Only explicit Buy/Sell retain the legacy
    up/down direction used by outcome evaluation. Unknown text stays unknown.
    """
    result = {"rating": None, "action_intent": "unknown", "direction": "unclassified",
              "parse_status": "missing" if not value or not value.strip() else "unrecognized"}
    if result["parse_status"] == "missing":
        return result
    rating = _rating_token(value)
    if rating is None:
        fields = [match.group(1) for line in value.splitlines()
                  if (match := _DECISION_FIELD.fullmatch(line))]
        ratings = [_rating_token(field) for field in fields]
        if not ratings or any(item is None for item in ratings):
            return result
        if len(set(ratings)) != 1:
            return {**result, "parse_status": "ambiguous"}
        rating = ratings[0]
    intent, direction = _RATING_INTENTS[rating]
    return {"rating": rating, "action_intent": intent, "direction": direction, "parse_status": "parsed"}


def decision_direction(value: str | None) -> str:
    return decision_semantics(value)["direction"]


MIN_AGENT_HISTORY_BARS = 60


def injectable_history(cfg: dict, symbol: str, trade_date: str, limit: int = 120) -> list[dict]:
    """决策时刻之前的历史日线，注入给 LLM 作为趋势判断依据。

    为什么必须有这个
    ----------------
    2026-09-15 那批是唯一成功的一批，5 条分析**全部**输出 `Hold`，且报告里明写
    `history_bars=0`、`flow_observation_days=1`、`point_in_time_verified=false`。

    根因不是"多智体辩论天然保守"，而是 screen_evidence 里只有当日快照、
    没有任何历史价格序列。证据不足 → LLM 按提示语要求给出 insufficient_data 型
    Hold → direction=neutral → settle 时 direction_correct 恒为 None →
    agent_outcomes 的有效样本永远为 0。

    只取 trade_date 当天及之前的 K 线。注入未来数据是前视偏差，会让
    agent 的"预测能力"被系统性高估——这正是我们要避免的。
    """
    try:
        snapshot = load_cache(symbol, market_data_dir(cfg))
    except (FileNotFoundError, ValueError, OSError):
        return []
    try:
        cutoff = date.fromisoformat(trade_date)
    except (TypeError, ValueError):
        return []
    bars = [bar for bar in snapshot.bars if bar.trading_date <= cutoff]
    return [{"date": bar.trading_date.isoformat(), "open": round(bar.open, 4),
             "high": round(bar.high, 4), "low": round(bar.low, 4),
             "close": round(bar.close, 4), "volume": round(bar.volume, 2)}
            for bar in bars[-limit:]]


def rule_gate_rating(candidate: dict, llm_rating: str | None, overheat_threshold: float = 7.0,
                     allow_divergence: bool = False, min_amount: float = 50_000_000.0,
                     chanlun_weight: float = 0.0, min_confirm: float = 1.0) -> dict:
    """规则闸门（C）：在 LLM 评级之外，把增持判定权拿回自己手里。

    2026-11-09 用户定版（两项修订）：
      1. **缠论默认不参与判定**（chanlun_weight=0）。旧版把 `chanlun_signal=="buy"`
         当硬门槛，实测会把 0.08~0.15 亿薄成交额候选直接抬成 Overweight。
         现在资金确认是唯一的基础票；缠论只在 chanlun_weight>0 时作为**加分票**，
         永远不能单独触发升级 —— 这就是用户说的「比重降低」口径。
         想回到旧口径：chanlun_weight=0.5 且 min_confirm=1.5（资金+缠论缺一不可）。
      2. **流动性硬否决**。`amount` 缺失/<=0（新浪回退时 K 线无成交额）或
         低于 `min_amount`（默认 5000 万 = 0.5 亿，粗筛 min_amount 500 万的 10 倍）
         一律不升级。`flow_to_cap` 会把薄成交额候选的净流入占比放大到 50%+，
         不设这道门槛就会把「小盘口偶发大单」误读成「持续吸筹」。

    判定顺序：LLM 为 Buy/Sell 或非 Hold/Underweight/None ⇒ 原样保留；
    → 流动性硬否决 → 非追高硬否决 → 确认分 >= min_confirm 才升 Overweight。
    返回含 rule_gated_rating / rule_override / reason，原始 llm_rating 不丢失、可审计。
    """
    def _num(v):
        return float(v) if isinstance(v, (int, float)) else None

    def _keep(reason: str) -> dict:
        return {"rule_gated_rating": llm_rating or "Hold", "rule_override": False, "reason": reason}

    if llm_rating in ("Buy", "Sell"):
        return {"rule_gated_rating": llm_rating, "rule_override": False, "reason": "llm_directional_kept"}
    if llm_rating not in ("Hold", "Underweight", None):
        return _keep("no_rule_trigger")

    ev = candidate.get("screen_evidence") or {}
    cl = (candidate.get("chanlun_signal") or {}).get("signal")
    od = candidate.get("order_divergence") or {}
    flow_pct = _num(ev.get("main_net_inflow_pct"))
    change = _num(ev.get("change_pct"))
    amount = _num(ev.get("amount"))

    # ① 流动性硬门槛：数据不可用时保守否决，绝不把 0 当成「流动性差」以外的任何含义。
    if amount is None or amount <= 0:
        return _keep("liquidity_unavailable")
    if amount < min_amount:
        return _keep("liquidity_veto")
    # ② 非追高硬门槛
    if change is not None and change >= overheat_threshold:
        return _keep("chasing")

    # ③ 确认分：资金确认 1.0 为基础票；缠论/背离都是可选加分，不能单独触发。
    confirm = 0.0
    if flow_pct is not None and flow_pct > 0:
        confirm += 1.0
    chanlun_buy = cl == "buy"
    if chanlun_weight > 0 and chanlun_buy:
        confirm += chanlun_weight
    bullish_div = od.get("signal") == "bullish_divergence"
    if allow_divergence and bullish_div:
        confirm += 1.0
    if confirm < min_confirm:
        return _keep("no_confirmation")

    reason = "cash_confirm+liquidity_ok+not_chasing"
    if chanlun_weight > 0 and chanlun_buy:
        reason += "+chanlun_weighted"
    if allow_divergence and bullish_div:
        reason += "+order_divergence_bullish"
    return {"rule_gated_rating": "Overweight", "rule_override": True, "reason": reason}


def _attach_rule_gate(item: dict, candidate: dict, overheat_threshold: float, allow_divergence: bool = False,
                      min_amount: float = 50_000_000.0, chanlun_weight: float = 0.0,
                      min_confirm: float = 1.0) -> dict:
    """给单只候选的结论挂上规则闸门结果（C），并算好可审计字段。"""
    sem = decision_semantics(item.get("decision") if item.get("status") == "ok" else None)
    item["decision_semantics"] = sem
    gate = rule_gate_rating(candidate, sem["rating"], overheat_threshold, allow_divergence,
                            min_amount, chanlun_weight, min_confirm)
    item["rule_gated_rating"] = gate["rule_gated_rating"]
    item["rule_override"] = gate["rule_override"]
    item["rule_override_reason"] = gate["reason"]
    return item


def build_plan(cfg: dict, top_n: int) -> dict:
    from .news_handoff import configured_candidate_news, validate_screen_date
    from .screen_shadow import candidate_factor_evidence
    from .universe_assessment import candidate_universe_evidence
    frozen = cfg.get("workflow", {}).get("frozen_screen_report")
    if frozen:
        screen_path = Path(frozen)
        screen = json.loads(screen_path.read_text(encoding="utf-8"))
    else:
        screen_path, screen = latest_screen(cfg)
    validate_screen_date(screen, cfg)
    project = resolve_project_path(cfg, cfg["tradingagents"]["project_dir"])
    epochs = [int(row["quote_epoch"]) for row in screen["selected"] if row.get("quote_epoch")]
    china_time = timezone(timedelta(hours=8))
    trade_date = datetime.fromtimestamp(max(epochs), china_time).date().isoformat() if epochs else date.today().isoformat()

    # RC1 修复（A）：默认开启历史门限，且根数从 20 提到 60（可经 config.workflow
    # .min_agent_history_bars 调）。送 LLM 前强制要求足够历史，否则该标的只可能拿到
    # insufficient_data 型 Hold（机械 Hold，非真没机会）。被剔除的标的记入
    # deferred_for_backfill，由 backfill_klines.py 回补 K 线后再评（"回补后再评"）。
    require_history = bool(cfg.get("workflow", {}).get("require_history_for_agents", True))
    min_bars = int(cfg.get("workflow", {}).get("min_agent_history_bars", MIN_AGENT_HISTORY_BARS))
    candidates = []
    skipped_no_history = []
    deferred_for_backfill = []
    for row in screen["selected"][:top_n]:
        history = injectable_history(cfg, row["symbol"], trade_date)
        if require_history and len(history) < min_bars:
            skipped_no_history.append({"symbol": row["symbol"], "name": row.get("name"),
                                       "history_bars": len(history)})
            deferred_for_backfill.append({"symbol": row["symbol"], "name": row.get("name"),
                                          "history_bars": len(history)})
            continue
        cl = chanlun_signal_fn(history)
        candidates.append({"ticker": _ticker(row), "symbol": row["symbol"], "name": row["name"],
                           "screen_score": row["screen_score"], "screen_group": row["screen_group"],
                           "asset_class": row["asset_class"], "subtype": row["subtype"],
                           "order_divergence": row.get("order_divergence", {"status": "missing_data", "signal": "no_data"}),
                           "screen_evidence": {key: row.get(key) for key in (
                               "price", "quote_epoch", "change_pct", "amount", "market_cap", "main_net_inflow",
                               "main_net_inflow_pct", "super_large_net_inflow", "super_large_net_inflow_pct",
                               "large_net_inflow", "large_net_inflow_pct", "medium_net_inflow", "medium_net_inflow_pct",
                               "small_net_inflow", "small_net_inflow_pct", "screen_components", "screen_contributions", "relative_strength_context",
                               # 可选人工注入的额外证据（如 Polymarket 预测市场上下文）。tradingagents/evidence_context.py
                               # 的 render_screen_evidence 会把整份 evidence JSON 塞进 prompt 的 <screen_evidence> 块，
                               # 因此这里放进去即可被 LLM 读到，无需给 main.py 加参数。冻结候选行未提供该字段时为 null，
                               # 对其余标的无影响。
                               "polymarket_context")},
                           "history_bars": history,
                           "history_bars_count": len(history),
                           "factor_evidence": candidate_factor_evidence(cfg, row["symbol"], trade_date),
                           "category_assessment": candidate_universe_evidence(cfg, row["symbol"], screen),
                           "news_evidence": configured_candidate_news(
                               cfg, _ticker(row), screen["decision_at_utc"], row.get("name")),
                           "chanlun_signal": cl,
                           "opportunity_observation": next((item for item in screen.get("opportunity_observation", {}).get("rows", [])
                                                            if item["symbol"] == row["symbol"]), None),
                           "evidence_handoff_status": "will_inject_via_screen_evidence_cli"})

    return {"screen_report": str(screen_path.resolve()), "source_snapshot": screen.get("source_snapshot"),
            "decision_at_utc": screen.get("decision_at_utc"), "rule_hash": screen.get("rule_hash"),
            "project_dir": str(project), "project_available": (project / "main.py").exists(), "trade_date": trade_date,
            "candidates": candidates, "skipped_no_history": skipped_no_history,
            "deferred_for_backfill": deferred_for_backfill, "min_agent_history_bars": min_bars,
            "history_injection_enabled": True}


def _existing_analyses(db_path: Path, trade_date: str) -> dict[str, dict]:
    """决策日当天已产出的 TradingAgents 结论（ticker → 记录），供断点续跑复用。

    为什么需要
    ----------
    2026-10-09 实测：cycle 跑到第 15 步（内置 run_tradingagents）处理第 6 只时
    进程被杀，前 5 只各自的 ``complete_report.md`` 都已落盘，但因旧实现把结果
    攒在内存里等循环结束才入库，这 5 只**既没进 DB 也没进批次 JSON**。重跑时
    只能整批重来，10 只 × 单只 6-7 分钟的 LLM 调用等于白烧一遍。

    为什么匹配键是「决策日 + 标的」而不是 screen_report
    ------------------------------------------------
    ``screen_etfs`` 每次执行都写一个带时间戳的**新文件名**（即使当日同 rule_hash
    的冻结 payload 被复用）。若把 screen_report 算进匹配键，重跑 cycle 后
    续跑永远命中不了，等于没做续跑。同一交易日、同一标的的多智能体结论没必要
    重算第二遍。

    判据取「DB 里 status=ok 且 decision_text 非空」**且**「报告目录下
    complete_report.md 仍在」——只看 DB 会在报告目录被清理/迁移后误判为已完成
    而跳过；只看报告目录则会把上一批次的同名目录算进来。两者都满足才复用。
    """
    try:
        with connect(db_path) as connection:
            rows = connection.execute(
                # ORDER BY created_at_utc：同一标的同决策日可能有多条（早期重跑遗留的
                # 重复行），升序扫描让字典逐条覆盖，最终保留**最新**一条的 analysis_id。
                "SELECT ticker, symbol, decision_text, report_dir, analysis_id FROM agent_analyses "
                "WHERE analysis_date=? AND agent_system='TradingAgents' AND status='ok' "
                "AND decision_text IS NOT NULL AND TRIM(decision_text) <> '' "
                "ORDER BY created_at_utc",
                (trade_date,),
            ).fetchall()
    except Exception:
        # 断点信息取不到不应该让整轮分析失败：退化为「全部重跑」。
        return {}
    done: dict[str, dict] = {}
    for ticker, symbol, decision, report_dir, analysis_id in rows:
        if report_dir and (Path(report_dir) / "complete_report.md").exists():
            done[ticker] = {"analysis_id": analysis_id, "ticker": ticker, "symbol": symbol,
                            "decision": decision, "report_dir": report_dir}
    return done


def _atomic_write_json(path: Path, payload: dict) -> None:
    """先写临时文件再改名覆盖，避免被中断时留下半截 JSON。

    增量落盘必须原子：外部监视/恢复逻辑会读这个文件判断「跑到哪了」，
    读到写坏的一半 JSON 比读到旧版本更危险。
    """
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


# Key2（InternAI 网关）专属的进程级注入项。降级到官方 flash 时必须从子进程
# 环境里清掉：否则官方 API key 会被打到 InternAI 网关（401），reasoning 专属的
# max_tokens 与串行锁也会残留。.env 是官方通道的唯一权威来源。
_KEY2_ONLY_ENV = (
    "TRADINGAGENTS_LLM_BACKEND_URL",      # .env 无此项；清掉后走 api.deepseek.com
    "TRADINGAGENTS_MAX_TOKENS",           # 8192 是 reasoning 网关专属上限
    "TRADINGAGENTS_LLM_SERIALIZE",        # 串行锁是 key2 429 的临时缓解
    "TRADINGAGENTS_RATE_LIMIT_FALLBACK",  # 防止降级子进程里再嵌套触发降级
    "TRADINGAGENTS_RATE_LIMIT_MAX_ATTEMPTS",    # 编排层参数，main.py 不读
    "TRADINGAGENTS_RATE_LIMIT_BACKOFF_SECONDS",
    "TRADINGAGENTS_LLM_FAST_FAILOVER",    # 编排层开关；官方子进程无需再判
)
_KEY2_HOST = "discovery-api.intern-ai.org.cn"
_OFFICIAL_ENV_KEYS = (
    "DEEPSEEK_API_KEY", "TRADINGAGENTS_LLM_PROVIDER",
    "TRADINGAGENTS_DEEP_THINK_LLM", "TRADINGAGENTS_QUICK_THINK_LLM",
    "TRADINGAGENTS_LLM_MAX_RETRIES", "TRADINGAGENTS_OUTPUT_LANGUAGE",
)


def _official_flash_env(base_env: dict, project_root: Path) -> dict:
    """构造「官方 deepseek-v4-flash」的子进程环境，仅供**单标的**降级使用。

    官方配置以项目根 ``.env`` 为唯一来源（与 00:00 自动化口径一致），key2 的
    进程级注入项全部清除。父进程 ``os.environ`` **不被修改** —— 子进程结束即
    消失，下一只候选自动回到 key2，无需（也不应该）显式「切回来」。
    """
    from dotenv import dotenv_values  # 延迟导入，避免顶层依赖

    official = dotenv_values(project_root / ".env")
    env = dict(base_env)
    for name in _KEY2_ONLY_ENV:
        env.pop(name, None)
    for name in _OFFICIAL_ENV_KEYS:
        value = official.get(name)
        if value not in (None, ""):
            env[name] = value
        else:
            env.pop(name, None)
    # 2026-11-13: fast failover means NO waiting on the official channel either.
    # .env sets TRADINGAGENTS_LLM_MAX_RETRIES=10 (with SDK backoff); override it
    # so an official failure surfaces immediately and the batch can halt cleanly.
    if base_env.get("TRADINGAGENTS_LLM_FAST_FAILOVER", "").strip() == "1":
        env["TRADINGAGENTS_LLM_MAX_RETRIES"] = "0"
    # 官方通道不需要把 InternAI 网关加进 NO_PROXY
    for name in ("NO_PROXY", "no_proxy"):
        cur = env.get(name)
        if cur and _KEY2_HOST in cur:
            env[name] = ",".join(p for p in cur.split(",")
                                 if p.strip() and p.strip() != _KEY2_HOST)
    if not env.get("DEEPSEEK_API_KEY"):
        raise RuntimeError("official flash fallback: .env has no DEEPSEEK_API_KEY")
    env["TRADINGAGENTS_LLM_CHANNEL"] = "official_flash_fallback"
    return env


def _run_one(command: list[str], project: Path, timeout_seconds: int,
             env: dict | None = None) -> tuple[int, str, str]:
    """跑一只候选并返回 (returncode, stdout, stderr)，超时按 rc=124 处理而非冒泡。

    旧实现让 ``subprocess.TimeoutExpired`` 直接冒泡到 ``run_tradingagents`` 外层，
    单只超时（默认 1800s）就会终止**整批**，后面还没跑的候选连一条
    ``skipped`` 记录都留不下。改成捕获后记为 failed，循环继续。

    ``env`` 只对这一次子进程生效（默认继承父进程环境）。单标的模型降级靠它
    实现：主流程环境全程不变，因此降级天然只覆盖当前这一只候选。
    """
    try:
        completed = subprocess.run(
            command, cwd=project, text=True, encoding="utf-8", errors="replace",
            capture_output=True, timeout=timeout_seconds,
            env=os.environ.copy() if env is None else dict(env),
        )
        return completed.returncode, completed.stdout or "", completed.stderr or ""
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else (exc.stdout or b"").decode("utf-8", "replace")
        stderr = exc.stderr if isinstance(exc.stderr, str) else (exc.stderr or b"").decode("utf-8", "replace")
        return 124, stdout, f"TIMEOUT after {timeout_seconds}s\n{stderr}"


def run_tradingagents(cfg: dict, top_n: int, resume: bool = True) -> tuple[Path, dict]:
    plan = build_plan(cfg, top_n)
    if not plan["project_available"]:
        raise FileNotFoundError(plan["project_dir"])
    project = Path(plan["project_dir"])
    interpreter = project / ".venv" / "Scripts" / "python.exe"
    if not interpreter.exists():
        raise FileNotFoundError(f"TradingAgents interpreter not found: {interpreter}")
    report_root = resolve_project_path(cfg, cfg["tradingagents"]["reports_dir"])
    db_path = resolve_project_path(cfg, cfg["state_db"])

    # 断点续跑：本决策日已成功的标的直接复用结论。这同时消除了 cycle 第 15 步
    # 内置 run_tradingagents 与随后独立 run-agents 对**同一批 top_n** 的重复 LLM
    # 调用（旧行为下这会把整条链的 LLM 耗时翻倍，是"老中断"的放大器）。
    already_done = _existing_analyses(db_path, plan["trade_date"]) if resume else {}

    output = resolve_project_path(cfg, cfg["runs_dir"]) / f"tradingagents_batch_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.json"
    items: list[dict] = []
    payload = {**plan, "run_type": "tradingagents_after_etf_screen",
               "created_at_utc": datetime.now(timezone.utc).isoformat(),
               "resume_enabled": bool(resume), "resumed_reused": sorted(already_done),
               "items": items}

    def flush() -> None:
        # 增量落盘：每完成一只就刷新一次，使进程被中断时已完成结果不丢。
        # 旧实现只在候选循环全部结束后一次性写盘，中断 ⇒ 整批结果全部丢弃。
        _atomic_write_json(output, payload)

    flush()  # 先把骨架落盘，外部监视可据此判断"已启动、正在跑第几只"

    timeout_seconds = int(cfg["tradingagents"]["timeout_seconds"])
    # 单标的级通道降级（2026-11-12 用户定）：只有显式设了
    # TRADINGAGENTS_RATE_LIMIT_FALLBACK=official_flash 的进程（即 key2 launcher）
    # 才启用。官方 00:00 自动化不设此变量 ⇒ 下面三段行为与改动前完全一致。
    # 启用后：key2 连续 5 次限流即**立即**换官方 flash 跑完这一只，不排队冷却；
    # 跑完该子进程结束，父进程环境没被动过，下一只自动回到 key2。
    fallback_enabled = os.environ.get("TRADINGAGENTS_RATE_LIMIT_FALLBACK", "").strip().lower() in {
        "official_flash", "official", "env_default"}
    # 2026-11-13 用户定：不等待、失败立即重调一次、仍失败立即用官方。
    # 只由 .workbuddy/tmp/run_pipeline_key2.py 注入；00:00 官方自动化不设，
    # 因此下面的旧分支（3 次 / 30s 退避、仅限流降级）行为完全不变。
    fast_failover = os.environ.get("TRADINGAGENTS_LLM_FAST_FAILOVER", "").strip() == "1"
    # 未启用时维持旧的 3 次 / 30s 退避；启用时 5 次、退避 0（不做冷却队列）；
    # 快速降级时 2 次（首次 + 立即重调 1 次）、退避 0。
    default_max_attempts = 2 if fast_failover else (5 if fallback_enabled else 3)
    default_backoff = 0.0 if (fallback_enabled or fast_failover) else 30.0
    rate_limit_max_attempts = max(1, int(os.environ.get(
        "TRADINGAGENTS_RATE_LIMIT_MAX_ATTEMPTS",
        cfg["tradingagents"].get("rate_limit_max_attempts", default_max_attempts))))
    rate_limit_backoff_seconds = max(0.0, float(os.environ.get(
        "TRADINGAGENTS_RATE_LIMIT_BACKOFF_SECONDS",
        cfg["tradingagents"].get("rate_limit_backoff_seconds", default_backoff))))
    # 规则闸门（C）用的"非追高"阈值：当日涨跌幅超过它视为过热，不触发增持升级。
    overheat_threshold = float(cfg.get("workflow", {}).get("overheat_threshold_pct",
                              cfg.get("etf_market", {}).get("screen", {}).get("overheat_threshold_pct", 7.0)))
    # 规则闸门是否允许"订单流看多背离"也升级为增持。默认 False，
    # 因背离分支过松会触发绝大多数标的，易把保守偏置翻成宽松偏置。
    allow_divergence = bool(cfg.get("workflow", {}).get("rule_gate_allow_divergence", False))
    # 2026-11-09 用户定版：流动性硬门槛 + 缠论降权（默认 0 = 不参与）。
    wf = cfg.get("workflow", {})
    gate_min_amount = float(wf.get("rule_gate_min_amount", 50_000_000.0))
    gate_chanlun_weight = float(wf.get("rule_gate_chanlun_weight", 0.0))
    gate_min_confirm = float(wf.get("rule_gate_min_confirm", 1.0))
    stop_reason = None
    with connect(db_path) as connection:
        for candidate_index, candidate in enumerate(plan["candidates"]):
            target = report_root / f"{candidate['ticker']}_{plan['trade_date']}"

            def persist(item: dict) -> None:
                item["decision_semantics"] = decision_semantics(item.get("decision") if item["status"] == "ok" else None)
                item["analysis_id"] = record_agent_analysis(connection, plan["screen_report"], item, plan["trade_date"])
                items.append(item)
                flush()

            if stop_reason:
                persist({**candidate, "status": "skipped_after_batch_failure", "returncode": None,
                         "decision": None, "report_dir": str(target.resolve()), "error_class": stop_reason})
                continue
            prior = already_done.get(candidate["ticker"])
            if prior and cfg.get("workflow", {}).get("frozen_screen_report"):
                # Same ticker/day is insufficient after inputs change. Only a
                # matching frozen rule/snapshot/news bundle can be resumed.
                try:
                    old_evidence = json.loads((Path(prior["report_dir"]) / "screen_evidence.json").read_text(encoding="utf-8"))
                    if (old_evidence.get("rule_hash") != plan.get("rule_hash")
                        or old_evidence.get("source_snapshot", {}).get("sha256") != plan.get("source_snapshot", {}).get("sha256")
                        or old_evidence.get("news_evidence") != candidate.get("news_evidence")
                        or old_evidence.get("factor_evidence") != candidate.get("factor_evidence")
                        or old_evidence.get("category_assessment") != candidate.get("category_assessment")):
                        prior = None
                except (OSError, ValueError):
                    prior = None
            if prior:
                # 复用本决策日已有结论：不重复调用 LLM，也**不再重复入库**。
                # 旧实现会以本批次（新）screen_report 为键再插一行——因唯一约束是
                # (screen_report,ticker,analysis_date,agent_system)，新 screen 不与之冲突，
                # 于是同标的同决策日累积出多行重复：agent_outcomes 被重复结算、
                # 报告 TA 章节同一标的出现两次（2026-10-10 重跑 cycle 实测 12→22 行）。
                # 结论本身已在库，直接沿用其 analysis_id 即可。
                item = {**candidate, "status": "ok", "returncode": 0, "decision": prior["decision"],
                        "report_dir": prior["report_dir"], "error_class": None, "reused": True,
                        "analysis_id": prior.get("analysis_id"),
                        "decision_semantics": decision_semantics(prior["decision"]),
                        "note": "resume: 复用本决策日已有产出，未重复调用 LLM"}
                item = _attach_rule_gate(item, candidate, overheat_threshold, allow_divergence,
                                     gate_min_amount, gate_chanlun_weight, gate_min_confirm)
                items.append(item)
                flush()
                continue

            command = [str(interpreter), "main.py", candidate["ticker"], "--date", plan["trade_date"], "--reports-dir", str(report_root), "--quiet"]
            target.mkdir(parents=True, exist_ok=True)
            evidence_path = target / "screen_evidence.json"
            evidence = {"schema_version": 1, "trade_date": plan["trade_date"],
                        "rule_hash": plan.get("rule_hash"),
                        "screen_report": plan["screen_report"], "source_snapshot": plan.get("source_snapshot"),
                        "decision_at_utc": plan.get("decision_at_utc"), "rule_hash": plan.get("rule_hash"), **candidate}
            evidence_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
            command.extend(["--screen-evidence", str(evidence_path.resolve())])
            attempt = 1
            while True:
                returncode, stdout, stderr = _run_one(command, project, timeout_seconds)
                stderr_tail = _safe_tail(stderr)
                error_class = _classify_failure(returncode, stderr_tail)
                # fast failover: ANY failure gets one immediate zero-wait re-call.
                # Legacy path: only rate_limited is retried, with backoff.
                retryable = fast_failover or error_class == "rate_limited"
                if error_class is None or not retryable or attempt >= rate_limit_max_attempts:
                    break
                delay = 0.0 if fast_failover else rate_limit_backoff_seconds * (2 ** (attempt - 1))
                wait_note = f"retrying in {delay:.0f}s" if delay > 0 else "retrying immediately"
                print(f"[run-agents] {candidate['ticker']} {error_class or 'failed'} "
                      f"(attempt {attempt}/{rate_limit_max_attempts}); {wait_note}",
                      file=sys.stderr, flush=True)
                if delay > 0:
                    time.sleep(delay)
                attempt += 1

            # ── 单标的临时通道降级（2026-11-12 定，2026-11-13 改为快速降级）──
            # 旧规则：只对 rate_limited 降级，且要连败 5 次。
            # 新规则（fast failover）：任何失败、只立即重调 1 次、零等待，随
            # 即切官方 flash。官方也失败仍熔断整批（下方 _BATCH_FATAL_CLASSES）。
            # 作用域只有这一次子进程；父进程 os.environ 从未修改 ⇒ 子进程一结束
            # 就自动回到 key2，下一只候选继续用 key2，无需显式「切回来」。
            fallback_applied = False
            fallback_error = None
            fallback_reason = error_class
            fallback_due = (returncode != 0 and attempt >= rate_limit_max_attempts
                            and (fast_failover or error_class == "rate_limited"))
            if fallback_enabled and fallback_due:
                try:
                    official_env = _official_flash_env(os.environ.copy(), project)
                except (OSError, RuntimeError, ImportError) as exc:
                    fallback_error = f"official env unavailable: {exc}"
                    print(f"[run-agents] {candidate['ticker']}: fallback skipped ({fallback_error})",
                          file=sys.stderr, flush=True)
                else:
                    print(f"[run-agents] {candidate['ticker']}: key2 failed ({fallback_reason}) after "
                          f"{attempt} attempt(s); immediately switching to official "
                          f"{official_env.get('TRADINGAGENTS_QUICK_THINK_LLM', 'flash')} "
                          f"for THIS ticker only (zero wait)",
                          file=sys.stderr, flush=True)
                    fb_rc, fb_out, fb_err = _run_one(command, project, timeout_seconds, env=official_env)
                    fb_tail = _safe_tail(fb_err)
                    fb_class = _classify_failure(fb_rc, fb_tail)
                    fallback_applied = True
                    returncode, stdout, stderr = fb_rc, fb_out, fb_err
                    stderr_tail, error_class = fb_tail, fb_class
                    if fb_rc == 0:
                        # 降级成功：清掉限流标记，否则 workflow 层会把整批误判成
                        # blocked_rate_limit / batch halted。
                        error_class = None
                    print(f"[run-agents] {candidate['ticker']}: fallback "
                          f"{'succeeded' if fb_rc == 0 else 'failed: ' + str(fb_class)}; "
                          f"parent env untouched (key2 stays the default for any subsequent ticker)",
                          file=sys.stderr, flush=True)

            item = {**candidate, "status": "ok" if returncode == 0 else "failed", "returncode": returncode,
                    "decision": _decision(stdout), "report_dir": str(target.resolve()), "error_class": error_class,
                    "attempts": attempt,
                    "stdout_tail": _safe_tail(stdout), "stderr_tail": stderr_tail,
                    "evidence_handoff_status": "passed_to_cli", "evidence_path": str(evidence_path.resolve())}
            if fallback_applied:
                item.update({
                    "llm_channel": "official_flash_fallback" if returncode == 0 else "official_flash_fallback_failed",
                    "fallback_from": "key2",
                    "fallback_after_attempts": attempt,
                    "fallback_reason": fallback_reason,
                    "fallback_attempts": 1,
                    "fallback_scope": "single_ticker_subprocess_only",
                })
            if fallback_error:
                item["fallback_error"] = fallback_error
            item = _attach_rule_gate(item, candidate, overheat_threshold, allow_divergence,
                                     gate_min_amount, gate_chanlun_weight, gate_min_confirm)
            persist(item)  # ← 单只结束立即入库 + 落盘
            if error_class in _BATCH_FATAL_CLASSES:
                stop_reason = error_class
                remaining = len(plan["candidates"]) - candidate_index - 1
                print(f"[run-agents] {candidate['ticker']}: {error_class} after {attempt} attempt(s); "
                      f"skipping the remaining {remaining} candidate(s) in this batch",
                      file=sys.stderr, flush=True)
    flush()
    return output, payload


def settle_agent_analyses(cfg: dict) -> dict:
    db_path = resolve_project_path(cfg, cfg["state_db"])
    settled = []
    groups: dict[tuple[str, int], list[tuple[str, float]]] = {}
    with connect(db_path) as connection:
        analyses = connection.execute(
            "SELECT analysis_id,symbol,analysis_date,decision_text,rule_override FROM agent_analyses WHERE status='ok' AND decision_text IS NOT NULL"
        ).fetchall()
        for analysis_id, symbol, analysis_date, decision_text, rule_override in analyses:
            try:
                snapshot = load_cache(symbol, market_data_dir(cfg))
            except (FileNotFoundError, ValueError):
                continue
            semantics = decision_semantics(decision_text)
            direction = semantics["direction"]
            # 规则闸门（C）：LLM 的 Hold/Underweight 被升级为 Overweight 时给方向 "up"，
            # 用现有 agent_outcomes 框架单独度量其前向准确率，不污染 LLM 原生方向统计。
            rule_dir = "up" if (rule_override and "Overweight" in str(rule_override)) else None
            for horizon in cfg["horizons"]:
                exists = connection.execute("SELECT 1 FROM agent_outcomes WHERE analysis_id=? AND horizon=?", (analysis_id, horizon)).fetchone()
                if exists:
                    continue
                sample = next((row for row in build_samples(snapshot, int(horizon)) if row.feature_date.isoformat() == analysis_date), None)
                if not sample or sample.target_return is None:
                    continue
                correct = None if direction in {"neutral", "unclassified"} else int((direction == "up") == bool(sample.target_up))
                # Hold 没有方向，但相对"买入"这个默认动作仍然可以计分：
                # 上涨 = 踏空(0)，下跌 = 避损(1)。这是机会成本口径，不是方向判断能力，
                # 所以单独存一列，不与 direction_correct 混在一起。
                hold_score = None
                if direction in {"neutral", "unclassified"}:
                    hold_score = 0 if sample.target_up else 1
                rule_correct = None if rule_dir is None else int((rule_dir == "up") == bool(sample.target_up))
                outcome_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO agent_outcomes (outcome_id, analysis_id, horizon, direction, "
                    "target_end_date, actual_return, actual_up, direction_correct, "
                    "hold_correct_vs_buy, rule_direction_correct, peer_mean_return, excess_vs_peer, settled_at_utc) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (outcome_id, analysis_id, horizon, direction, sample.target_end_date.isoformat(), sample.target_return,
                     sample.target_up, correct, hold_score, rule_correct, None, None,
                     datetime.now(timezone.utc).isoformat()),
                )
                groups.setdefault((analysis_date, int(horizon)), []).append((outcome_id, float(sample.target_return)))
                settled.append({"analysis_id": analysis_id, "symbol": symbol, "horizon": horizon,
                                "decision_semantics": semantics, "direction": direction,
                                "actual_return": sample.target_return, "direction_correct": correct,
                                "hold_correct_vs_buy": hold_score, "rule_direction_correct": rule_correct})
        # 横截面相对表现：同一天被送进去的标的是同一批候选，拿它们的等权平均做基准，
        # 才能看出 LLM 是挑出了相对更好的，还是只是被动跟着池子走。
        for (_, _), rows in groups.items():
            if len(rows) < 2:
                continue
            mean = sum(return_ for _, return_ in rows) / len(rows)
            for outcome_id, actual in rows:
                connection.execute(
                    "UPDATE agent_outcomes SET peer_mean_return=?, excess_vs_peer=? WHERE outcome_id=?",
                    (mean, actual - mean, outcome_id))
        connection.commit()
    scored = sum(1 for item in settled if item["direction_correct"] is not None or item["hold_correct_vs_buy"] is not None
                 or item.get("rule_direction_correct") is not None)
    return {"successful_analyses": len(analyses), "settled": len(settled), "scored": scored, "items": settled}
