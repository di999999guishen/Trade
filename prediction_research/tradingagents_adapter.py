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


MIN_AGENT_HISTORY_BARS = 20


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


def build_plan(cfg: dict, top_n: int) -> dict:
    screen_path, screen = latest_screen(cfg)
    project = resolve_project_path(cfg, cfg["tradingagents"]["project_dir"])
    epochs = [int(row["quote_epoch"]) for row in screen["selected"] if row.get("quote_epoch")]
    china_time = timezone(timedelta(hours=8))
    trade_date = datetime.fromtimestamp(max(epochs), china_time).date().isoformat() if epochs else date.today().isoformat()

    # 默认**不**跳过无历史的标的：跳过会让"哪些标的被送进去"这件事不可见，
    # 而 insufficient_data 型 Hold 至少还是一条可审计的记录。
    # 真正修的是注入历史（见 injectable_history），不是把标的藏起来。
    # 想省 timeout 就在 config 的 workflow 里把 require_history_for_agents 设为 true。
    require_history = bool(cfg.get("workflow", {}).get("require_history_for_agents", False))
    candidates = []
    skipped_no_history = []
    for row in screen["selected"][:top_n]:
        history = injectable_history(cfg, row["symbol"], trade_date)
        if require_history and len(history) < MIN_AGENT_HISTORY_BARS:
            # 没历史的标的送进去只可能拿到 insufficient_data 型 Hold，
            # 白等一次 timeout_seconds（默认 1800s）还污染成功率统计。
            skipped_no_history.append({"symbol": row["symbol"], "name": row.get("name"),
                                       "history_bars": len(history)})
            continue
        candidates.append({"ticker": _ticker(row), "symbol": row["symbol"], "name": row["name"],
                           "screen_score": row["screen_score"], "screen_group": row["screen_group"],
                           "asset_class": row["asset_class"], "subtype": row["subtype"],
                           "order_divergence": row.get("order_divergence", {"status": "missing_data", "signal": "no_data"}),
                           "screen_evidence": {key: row.get(key) for key in (
                               "price", "quote_epoch", "change_pct", "amount", "market_cap", "main_net_inflow",
                               "main_net_inflow_pct", "super_large_net_inflow", "super_large_net_inflow_pct",
                               "large_net_inflow", "large_net_inflow_pct", "medium_net_inflow", "medium_net_inflow_pct",
                               "small_net_inflow", "small_net_inflow_pct", "screen_components", "screen_contributions", "relative_strength_context")},
                           "history_bars": history,
                           "history_bars_count": len(history),
                           "opportunity_observation": next((item for item in screen.get("opportunity_observation", {}).get("rows", [])
                                                            if item["symbol"] == row["symbol"]), None),
                           "evidence_handoff_status": "will_inject_via_screen_evidence_cli"})

    return {"screen_report": str(screen_path.resolve()), "source_snapshot": screen.get("source_snapshot"),
            "decision_at_utc": screen.get("decision_at_utc"), "rule_hash": screen.get("rule_hash"),
            "project_dir": str(project), "project_available": (project / "main.py").exists(), "trade_date": trade_date,
            "candidates": candidates, "skipped_no_history": skipped_no_history,
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


def _run_one(command: list[str], project: Path, timeout_seconds: int) -> tuple[int, str, str]:
    """跑一只候选并返回 (returncode, stdout, stderr)，超时按 rc=124 处理而非冒泡。

    旧实现让 ``subprocess.TimeoutExpired`` 直接冒泡到 ``run_tradingagents`` 外层，
    单只超时（默认 1800s）就会终止**整批**，后面还没跑的候选连一条
    ``skipped`` 记录都留不下。改成捕获后记为 failed，循环继续。
    """
    try:
        completed = subprocess.run(
            command, cwd=project, text=True, encoding="utf-8", errors="replace",
            capture_output=True, timeout=timeout_seconds, env=os.environ.copy(),
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
    # 限流退避：429 属可自愈的瞬时故障，先指数退避重试；重试仍失败才熔断整批。
    rate_limit_max_attempts = max(1, int(cfg["tradingagents"].get("rate_limit_max_attempts", 3)))
    rate_limit_backoff_seconds = max(0.0, float(cfg["tradingagents"].get("rate_limit_backoff_seconds", 30.0)))
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
                items.append(item)
                flush()
                continue

            command = [str(interpreter), "main.py", candidate["ticker"], "--date", plan["trade_date"], "--reports-dir", str(report_root), "--quiet"]
            target.mkdir(parents=True, exist_ok=True)
            evidence_path = target / "screen_evidence.json"
            evidence = {"schema_version": 1, "trade_date": plan["trade_date"],
                        "screen_report": plan["screen_report"], "source_snapshot": plan.get("source_snapshot"),
                        "decision_at_utc": plan.get("decision_at_utc"), "rule_hash": plan.get("rule_hash"), **candidate}
            evidence_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
            command.extend(["--screen-evidence", str(evidence_path.resolve())])
            attempt = 1
            while True:
                returncode, stdout, stderr = _run_one(command, project, timeout_seconds)
                stderr_tail = _safe_tail(stderr)
                error_class = _classify_failure(returncode, stderr_tail)
                if error_class != "rate_limited" or attempt >= rate_limit_max_attempts:
                    break
                delay = rate_limit_backoff_seconds * (2 ** (attempt - 1))
                print(f"[run-agents] {candidate['ticker']} rate limited "
                      f"(attempt {attempt}/{rate_limit_max_attempts}); retrying in {delay:.0f}s",
                      file=sys.stderr, flush=True)
                time.sleep(delay)
                attempt += 1
            item = {**candidate, "status": "ok" if returncode == 0 else "failed", "returncode": returncode,
                    "decision": _decision(stdout), "report_dir": str(target.resolve()), "error_class": error_class,
                    "attempts": attempt,
                    "stdout_tail": _safe_tail(stdout), "stderr_tail": stderr_tail,
                    "evidence_handoff_status": "passed_to_cli", "evidence_path": str(evidence_path.resolve())}
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
            "SELECT analysis_id,symbol,analysis_date,decision_text FROM agent_analyses WHERE status='ok' AND decision_text IS NOT NULL"
        ).fetchall()
        for analysis_id, symbol, analysis_date, decision_text in analyses:
            try:
                snapshot = load_cache(symbol, market_data_dir(cfg))
            except (FileNotFoundError, ValueError):
                continue
            semantics = decision_semantics(decision_text)
            direction = semantics["direction"]
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
                outcome_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO agent_outcomes (outcome_id, analysis_id, horizon, direction, "
                    "target_end_date, actual_return, actual_up, direction_correct, "
                    "hold_correct_vs_buy, peer_mean_return, excess_vs_peer, settled_at_utc) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (outcome_id, analysis_id, horizon, direction, sample.target_end_date.isoformat(), sample.target_return,
                     sample.target_up, correct, hold_score, None, None,
                     datetime.now(timezone.utc).isoformat()),
                )
                groups.setdefault((analysis_date, int(horizon)), []).append((outcome_id, float(sample.target_return)))
                settled.append({"analysis_id": analysis_id, "symbol": symbol, "horizon": horizon,
                                "decision_semantics": semantics, "direction": direction,
                                "actual_return": sample.target_return, "direction_correct": correct,
                                "hold_correct_vs_buy": hold_score})
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
    scored = sum(1 for item in settled if item["direction_correct"] is not None or item["hold_correct_vs_buy"] is not None)
    return {"successful_analyses": len(analyses), "settled": len(settled), "scored": scored, "items": settled}
