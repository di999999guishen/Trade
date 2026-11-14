"""One auditable run across existing news, backfill, holdings and cycle flows.

No brokerage API is called. Order plans are text artifacts. The cycle owns the
screen -> forecast -> TradingAgents -> settlement -> research-report chain.

Execution order (2026-11-13, user instruction): news -> backfill ->
``daily_predict`` (the user's four self-selected holdings/tracking tickers) ->
``cycle`` (full-universe screen + the screened backup candidates' run-agents)
-> order_plans + consolidated briefing. The user's own four tickers run FIRST:
cycle routinely dies on gateway 429 / the per-stage timeout, and putting
holdings after it meant a failed cycle produced zero conclusions for the four
tickers that actually matter. ``daily_predict`` needs only the news artifact,
not any cycle/screen output, so moving it earlier is dependency-safe.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path

from .config import load_config, resolve_project_path
from .news_handoff import timestamp


def now():
    return datetime.now(timezone.utc).isoformat()


def write_json(path, payload):
    path = Path(path)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    tmp.replace(path)


@contextmanager
def pipeline_lock(root):
    """OS releases the lock if a process dies; a leftover file is harmless."""
    root.mkdir(parents=True, exist_ok=True)
    with (root / "pipeline.lock").open("a+b") as handle:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def execute(command, project, log, timeout):
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"}
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    with log.open("w", encoding="utf-8") as stream:
        proc = subprocess.Popen(command, cwd=project, stdout=stream, stderr=subprocess.STDOUT,
                                env=env, creationflags=flags)
        try:
            code = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                               capture_output=True, creationflags=flags, check=False)
            else:
                proc.kill()
            proc.wait()
            raise RuntimeError(f"stage timed out after {timeout}s; see {log}")
    if code:
        raise RuntimeError(f"stage exited {code}; see {log}")


def inspect_cycle(payload, expected_date):
    artifacts = payload.get("artifacts", {})
    if payload.get("outcome") not in {"complete", "complete_with_data_waits"}:
        raise ValueError(f"cycle not complete: {payload.get('outcome')}")
    batch_path = Path(artifacts["tradingagents_batch"])
    batch = json.loads(batch_path.read_text(encoding="utf-8"))
    if batch.get("trade_date") != expected_date:
        raise ValueError("agent batch date differs from the pipeline date")
    if Path(batch.get("screen_report", "")).resolve() != Path(artifacts["screen"]).resolve():
        raise ValueError("agent batch does not belong to this cycle's frozen screen")
    items = batch.get("items", [])
    if not items or len(items) != len(batch.get("candidates", [])) or any(row.get("status") != "ok" for row in items):
        raise ValueError("agent batch is empty or incomplete")
    return batch


def _holdings_names():
    """Code -> verified Chinese name for the four self-selected tickers; never guess."""
    try:
        path = Path(__file__).resolve().parent.parent / "holdings_config.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        return {str(row.get("ticker")): row.get("name")
                for row in data.get("holdings", []) if row.get("ticker")}
    except Exception:
        return {}


def _label(ticker, name=None, names=None):
    """A ticker is never shown alone: append its verified Chinese name."""
    label = (name or "").strip() or ((names or {}).get(str(ticker)) or "").strip()
    return f"{ticker} {label}" if label else f"{ticker} 名称未核实"


def _pm_or_trader(report_dir):
    """价位优先取 PM 最终层（与 decision 同层），没有才回退 Trader 草案并标注。"""
    pm = Path(report_dir) / "5_portfolio/decision.md"
    trader = Path(report_dir) / "3_trading/trader.md"
    if pm.is_file():
        return pm, "PM 最终层"
    if trader.is_file():
        return trader, "Trader 草案"
    return None, "无价格块"


def _indicators(fields, source):
    """简报 / briefing.json 共用的买卖指标；缺值保留 None，绝不补造。"""
    return {
        "price_source_layer": source,
        "price_block_format": fields.get("price_block_format"),
        "direction": fields.get("direction"),
        "current_price": fields.get("current_price"),
        "entry_zone": fields.get("entry_zone"),
        "entry_price": fields.get("entry_price"),
        "stop_loss": fields.get("stop_loss"),
        "invalidation_price": fields.get("invalidation_price"),
        "reduce_trigger": fields.get("reduce_trigger"),
        "support_levels": fields.get("support"),
        "resistance_levels": fields.get("resist"),
        "scale_out_ladder": fields.get("ladder"),
        "ladder_sum": fields.get("ladder_sum"),
        "ladder_ok": fields.get("ladder_ok"),
        "price_target": fields.get("price_target"),
    }


_DIRECTION_CN = {"buy": "买入/加仓", "reduce": "减配/卖出",
                 "hold": "观望/持有", "unknown": "未识别"}


def _indicator_rows(rows, names):
    """两节共用的买卖指标表；每个代码同处带中文名称。"""
    from daily_order_plan import _cell, _shown
    lines = ["| 标的 | 方向 | 现价 | 买入/加仓触发 | 止损 | 减仓触发 | 支撑（近→远） | 阻力（近→远） | 分批减仓阶梯 |",
             "|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        v = r.get("indicators") or {}
        ladder = " / ".join(f"{x['price']}·{x['close_pct']}%"
                           for x in (v.get("scale_out_ladder") or [])) or "无数据"
        lines.append(
            f"| {_label(r.get('ticker'), r.get('name'), names)} | "
            f"{_DIRECTION_CN.get(v.get('direction'), '未识别')} | "
            f"{_shown(v.get('current_price'))} | {_cell(v.get('entry_zone'), 56)} | "
            f"{_shown(v.get('stop_loss'))} | {_cell(v.get('reduce_trigger'), 56)} | "
            f"{_shown(v.get('support_levels'))} | {_shown(v.get('resistance_levels'))} | {ladder} |")
    return lines


def render_delivery(folder, manifest, cycle, batch):
    """Read exact current-run paths; never glob global latest daily results."""
    from daily_order_plan import extract, render
    names = _holdings_names()
    # Per-ticker channel marks written by the four-ticker launcher (optional).
    channels = {}
    try:
        rows = json.loads((folder / "daily_channel_log.json").read_text(encoding="utf-8"))
        channels = {str(row.get("ticker")): row.get("llm_channel") or "未标注"
                    for row in rows if isinstance(row, dict)}
    except Exception:
        pass
    output = folder / "order_plans"
    output.mkdir(exist_ok=True)
    plans, missing = [], []
    for item in batch.get("items", []):
        src, layer = _pm_or_trader(item["report_dir"])
        if src is None:
            missing.append(item["ticker"])
            item["indicators"] = _indicators(extract(""), layer)
            continue
        fields = extract(src.read_text(encoding="utf-8"))
        item["indicators"] = _indicators(fields, layer)
        path = output / f"{item['ticker']}.md"
        path.write_text(render(item["ticker"], src, fields, item.get("decision"),
                               label=_label(item.get("ticker"), item.get("name"), names),
                               price_source=layer), encoding="utf-8")
        plans.append(str(path))
    daily = []
    for path in sorted((folder / "daily").glob("*.json")):
        if path.name.startswith("error_"):
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("analysis_date") != manifest["analysis_date"]:
            raise ValueError("daily artifact has the wrong analysis date")
        result = payload.get("result") or {}
        pm = result.get("final_trade_decision") or ""
        draft = result.get("trader_investment_plan") or ""
        plan = pm or draft
        layer = "PM 最终层" if pm else ("Trader 草案" if draft else "无价格块")
        fields = extract(plan)
        row = {"ticker": payload.get("ticker"), "name": names.get(str(payload.get("ticker"))),
               "llm_channel": channels.get(str(payload.get("ticker")), "未标注"),
               "decision": payload.get("decision"), "path": str(path),
               "indicators": _indicators(fields, layer)}
        daily.append(row)
        if plan:
            dest = output / f"holding_{payload['ticker']}.md"
            dest.write_text(render(payload["ticker"], path, fields, str(payload.get("decision")),
                                   label=_label(payload.get("ticker"), row.get("name"), names),
                                   price_source=layer), encoding="utf-8")
            plans.append(str(dest))
    summary = {"run_id": manifest["run_id"], "analysis_date": manifest["analysis_date"],
               "cycle_report": cycle["artifacts"].get("report"),
               "universe_assessment": cycle["artifacts"].get("universe_assessment_report"),
               "universe_coverage": cycle.get("universe_coverage"),
               "factor_shadow": cycle["artifacts"].get("screen_factor_shadow"),
               "factor_settlement": cycle["artifacts"].get("factor_shadow_settlement"),
               "holdings": daily,
               "candidate_decisions": [dict({k: r.get(k) for k in
                                            ("ticker", "name", "decision", "reused", "rule_gated_rating",
                                             "rule_override", "llm_channel")},
                                           indicators=r.get("indicators")) for r in batch["items"]],
               "order_plans": plans, "missing_trader_reports": missing,
               "note": "Research and text order plans only. Missing prices remain missing; no orders submitted."}
    write_json(folder / "briefing.json", summary)
    # 2026-11-13 用户指令：简报展示顺序与执行顺序一致 —— 自选四只在先、粗筛备选在后。
    # 每个证券代码必须在同一单元格内带中文名称，查不到写「名称未核实」。
    lines = [f"# ETF 综合简报 · {manifest['analysis_date']}", f"运行编号：{manifest['run_id']}",
             "", f"研究报告：{summary['cycle_report']}", "",
             "## 一、自选四只（先跑 · 决策优先）", "",
             "| 标的 | LLM 决策 | 模型通道 | 报告 |", "|---|---|---|---|"]
    lines += [f"| {_label(r['ticker'], r.get('name'), names)} | {r['decision']} | "
              f"{r.get('llm_channel') or '未标注'} | {r['path']} |" for r in daily]
    # 2026-11-13 用户指令：10 只备选与 4 只自选跑完后都要给出买入 / 卖出指标。
    lines += ["", "### 买卖指标 · 自选四只", ""] + _indicator_rows(daily, names)
    lines += ["", "## 二、粗筛备选（后跑 · 仅作观察，非持仓）", "",
              "| 标的 | LLM 决策 | 复用 | 模型通道 |", "|---|---|---|---|"]
    lines += [f"| {_label(r.get('ticker'), r.get('name'), names)} | {r.get('decision')} | "
              f"{bool(r.get('reused'))} | {r.get('llm_channel') or '未降级'} |" for r in batch["items"]]
    lines += ["", "### 买卖指标 · 粗筛备选", ""] + _indicator_rows(batch["items"], names)
    lines += ["", "---",
              "> 价位唯一来源 = 模型文本（**PM 最终层优先、与评级同层**），缺项写「无数据」，不推测、不补价。",
              "> 质量门 ICIR < 0.30 未过 ⇒ 选股禁用；以上仅为纪律价位 / 条件单参考，**不构成买入建议**。",
              "> 减配 / 卖出方向不生成建仓单；完整触发条件见 `order_plans/` 下逐只清单。",
              "挂单清单仅为待核对文本；缺价不补价。", f"缺少交易员报告：{missing}"]
    if summary['factor_shadow']:
        lines += ["", f"新增维度对照（未改变生产排名）：{summary['factor_shadow']}",
                  f"冻结候选后续结算：{summary['factor_settlement']}"]
    if summary['universe_assessment']:
        lines += ["", f"全量 ETF 分类评价：{summary['universe_assessment']}",
                  f"覆盖：{summary['universe_coverage']['assessed_records']} / {summary['universe_coverage']['input_records']}；各类使用独立标准，缺项保留。"]
    (folder / "briefing.md").write_text("\n".join(lines), encoding="utf-8")
    return {"briefing": str(folder / "briefing.md"), "summary": str(folder / "briefing.json"), "missing": missing}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--date", required=True, help="已收盘的境内交易日 YYYY-MM-DD")
    ap.add_argument("--config")
    ap.add_argument("--top", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true", help="打印流程，不联网、不调用 LLM、不写文件")
    ap.add_argument("--resume", type=Path, help="续跑既有 pipeline run 目录")
    ap.add_argument("--holdings", action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument("--timeout", type=int, default=10800, help="单阶段秒数上限")
    args = ap.parse_args(argv)
    date.fromisoformat(args.date)
    if (args.top is not None and args.top < 1) or args.timeout < 1:
        ap.error("top and timeout must be positive")
    cfg = load_config(args.config)
    if cfg.get("workflow", {}).get("skip_tradingagents"):
        ap.error("Unified pipeline requires TradingAgents; skip_tradingagents must be false")
    project = resolve_project_path(cfg, cfg["tradingagents"]["project_dir"])
    plan = ["news_flow", "backfill"]
    if args.holdings:
        # 2026-11-13 用户指令：先跑自选四只，再跑粗筛备选。
        plan.append("daily_predict (holdings + mandatory proxies; user-selected tickers FIRST)")
    plan.append("cycle (full_universe_assessment/screen/factor_shadow/evidence/backtest/predict/agents/settle/report)")
    plan.append("order_plans + consolidated_briefing")
    if args.dry_run:
        print(json.dumps({"date": args.date, "project": str(project), "steps": plan,
                          "agent_top_n": cfg["workflow"].get("agent_top_n"),
                          "prediction_top_n": args.top or cfg["workflow"].get("prediction_top_n"),
                          "QDII": "separate optional market workflow; not silently invoked",
                          "submits_orders": False}, ensure_ascii=False, indent=2))
        return 0
    runs = resolve_project_path(cfg, cfg["runs_dir"])
    root = runs / "pipelines"
    contract = {"config": cfg, "date": args.date, "top": args.top, "holdings": args.holdings}
    digest = hashlib.sha256(json.dumps(contract, sort_keys=True).encode()).hexdigest()
    try:
        with pipeline_lock(root):
            folder = args.resume.resolve() if args.resume else root / f"{args.date}_{uuid.uuid4().hex[:12]}"
            if args.resume:
                manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
                if manifest["contract_hash"] != digest:
                    raise ValueError("resume config/date/options changed; start a new run")
            else:
                folder.mkdir(parents=True)
                manifest = {"run_id": folder.name, "analysis_date": args.date, "contract_hash": digest,
                            "started_at": now(), "steps": {}, "outcome": "running"}
            def persist():
                write_json(folder / "manifest.json", manifest)
            def stage(name, action):
                old = manifest["steps"].get(name, {})
                if old.get("status") == "ok":
                    # Successful steps are reused only while all recorded artifacts survive.
                    if all(Path(p).is_file() and hashlib.sha256(Path(p).read_bytes()).hexdigest() == sha
                           for p, sha in old.get("artifact_hashes", {}).items()):
                        return old["result"]
                    raise ValueError(f"resume artifact changed or missing: {name}; start a new run")
                manifest["steps"][name] = {"status": "running", "started_at": now()}
                persist()
                print(f"[{name}] started", flush=True)
                value = action()
                paths = value.pop("_artifacts", [])
                manifest["steps"][name].update(status="ok", finished_at=now(), result=value,
                    artifact_hashes={str(p): hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in paths})
                persist()
                return value
            effective = copy.deepcopy(cfg)
            effective.setdefault("workflow", {}).update(expected_feature_date=args.date)
            config_path = folder / "effective_config.json"
            def command(name, cmd):
                execute([sys.executable, "-B", *cmd], project, folder / f"{name}.log", args.timeout)
            try:
                manifest["outcome"] = "running"
                manifest.pop("error", None)
                persist()
                def news():
                    started = datetime.now(timezone.utc)
                    command("news", ["news_aggregator/run_flow.py"])
                    paths = sorted((project / "reports").glob("news_flow_*.json"))
                    if not paths:
                        raise ValueError("news runner produced no JSON")
                    source = paths[-1]
                    payload = json.loads(source.read_text(encoding="utf-8"))
                    generated = timestamp(payload.get("generated_at"))
                    if generated is None or generated.timestamp() < started.timestamp() - 2:
                        raise ValueError("news runner left stale output")
                    target = folder / "news.json"
                    shutil.copy2(source, target)
                    return {"path": str(target), "_artifacts": [target]}
                news_result = stage("news", news)
                effective["workflow"]["news_context_path"] = news_result["path"]
                write_json(config_path, effective)
                def backfill():
                    before = set(runs.glob("backfill_*.json"))
                    command("backfill", ["run_research.py", "--config", str(config_path), "backfill"])
                    paths = set(runs.glob("backfill_*.json")) - before
                    if len(paths) != 1:
                        raise ValueError("backfill artifact is missing or ambiguous; concurrent legacy runner?")
                    path = paths.pop()
                    value = json.loads(path.read_text(encoding="utf-8"))
                    if value.get("outcome") != "complete":
                        raise ValueError("backfill did not complete")
                    return {"path": str(path), "_artifacts": [path]}
                stage("backfill", backfill)
                # 2026-11-13 用户指令：四只自选持仓/跟踪池优先于粗筛备选。
                # daily_predict 只依赖 news 产物（--news-file），不读 cycle 的
                # screen / backtest / agents 产物，所以可以安全前置。cycle 常因
                # 429 或单阶段超时整批失败；放后面会导致四只完全没产出。
                if args.holdings:
                    def holdings():
                        command("holdings", ["daily_predict.py", "--analysis-date", args.date,
                                              "--results-dir", str(folder / "daily"),
                                              "--news-file", news_result["path"]])
                        files = list((folder / "daily").glob("*.json"))
                        if not files or any(p.name.startswith("error_") for p in files):
                            raise ValueError("holdings outputs missing or include errors")
                        return {"directory": str(folder / "daily"), "_artifacts": files}
                    stage("holdings", holdings)
                def cycle():
                    before = set(runs.glob("cycle_*.json"))
                    cmd = ["run_research.py", "--config", str(config_path), "cycle"]
                    if args.top:
                        cmd += ["--top", str(args.top)]
                    command("cycle", cmd)
                    paths = set(runs.glob("cycle_*.json")) - before
                    if len(paths) != 1:
                        raise ValueError("cycle artifact is missing or ambiguous; concurrent legacy runner?")
                    path = paths.pop()
                    value = json.loads(path.read_text(encoding="utf-8"))
                    batch = inspect_cycle(value, args.date)
                    tracked = [path, value["artifacts"]["screen"], value["artifacts"]["tradingagents_batch"]]
                    tracked += [value["artifacts"][key] for key in ("screen_factor_shadow", "factor_shadow_settlement", "universe_assessment", "universe_assessment_report") if value["artifacts"].get(key)]
                    return {"path": str(path), "_artifacts": tracked}
                result = stage("cycle", cycle)
                cycle_payload = json.loads(Path(result["path"]).read_text(encoding="utf-8"))
                batch = inspect_cycle(cycle_payload, args.date)
                delivered = render_delivery(folder, manifest, cycle_payload, batch)
                manifest.update(outcome="complete_with_data_waits" if cycle_payload["outcome"] == "complete_with_data_waits" or delivered["missing"] else "complete",
                                delivery=delivered, finished_at=now())
                persist()
                print(json.dumps({"outcome": manifest["outcome"], "manifest": str(folder / "manifest.json"), **delivered}, ensure_ascii=False, indent=2))
                return 0
            except BaseException as exc:
                manifest.update(outcome="failed", finished_at=now(), error=f"{type(exc).__name__}: {exc}")
                for value in manifest["steps"].values():
                    if value["status"] == "running":
                        value.update(status="failed", finished_at=now())
                persist()
                raise
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"pipeline failed: {exc}", file=sys.stderr)
        return 2
