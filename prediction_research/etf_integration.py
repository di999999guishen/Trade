"""Sequential, auditable ETF integration stages; data waits do not masquerade as success."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from .config import resolve_project_path
from .etf_evidence import chain_report, etf_profiles, sync_etf_evidence, sync_existing_agents, sync_news_chains
from .evidence import etf_assets, evidence_status, import_evidence, now_utc
from .evidence_experiments import run_evidence_experiments
from .data import load_universe
from .store import connect, record_run


def _stage_markdown(row: dict) -> str:
    number = row["stage"]
    result = row.get("result", {})
    lines = [f"# E{number} {row['name']}", "", f"状态：`{row['status']}`", "",
             f"开始：{row['started_at_utc']}", "", f"完成：{row['finished_at_utc']}", "",
             f"详细证据：[E{number}.json](E{number}.json)", ""]
    if number == 1 and result:
        lines.extend([f"配置 ETF/基金数：{result['configured_etfs']}；已存标准证据：{result['records']}。",
                      "个股输入拒绝；时间必须带时区；来源、观察时间、可得时间、哈希、事实/推断标签必填。"])
    elif number == 2 and result:
        for name in ("flow", "existing_agents"):
            item = result[name]
            lines.append(f"- {name}：读取 {item['records']} 条，新增 {item['inserted']} 条，重复 {item['duplicates']} 条。")
        lines.extend(["", f"外部标准文件：{len(result['imports'])} 个。没有第三方真实证据时不产生第三方结论。"])
    elif number == 3 and result:
        lines.extend(["资金流为成交单大小估计；主体身份不可据此认定。", "",
                      "| ETF | 观察日数 | 净流入合计 | 不平衡阈值检查 | 身份 |",
                      "|---|---:|---:|---|---|"])
        for profile in result["profiles"]:
            net = profile["net_inflow_sum"]
            net_text = "无数据" if net is None else f"{net:.2f}"
            lines.append(f"| {profile['symbol']} | {profile['observed_days']} | {net_text} | {profile['flow_imbalance']} | {profile['actor_identity']} |")
    elif number == 4 and result:
        lines.extend([f"归档传播链：{result['storage']['records']} 条；当前时间与时效门槛内：{result['chain_count']} 条。",
                      f"映射版本：`{result['storage']['mapping_hash']}`。",
                      f"映射首次可得：{result['storage']['mapping_available_at_utc']}。", "",
                      "链条用于相关性解释，价格影响方向未验证；完整节点、来源和快照见 JSON。"])
    elif number == 5 and result:
        lines.extend(["| ETF 池 / 周期 | 基线样本 | 量价 v2 Brier | 历史概率 Brier | 实验状态 |",
                      "|---|---:|---:|---:|---|"])
        for item in result["items"]:
            metric = item["baseline_metrics"]
            lines.append(f"| {item.get('universe', '')} / {item['horizon']}日 | {metric['rows']} | {metric.get('brier', '无数据')} | {metric.get('historical_rate_brier', '无数据')} | {item['status']} |")
        lines.extend(["", "缺乏真实历史覆盖的模块等待积累；不能把测试用合成数据当作真实增益。", ""])
        for item in result["items"]:
            for source, readiness in item["readiness"].items():
                lines.append(f"- {item['horizon']}日 / {source}：{readiness['status']}；观察 {readiness['observed_days']} 日；可比样本 {readiness['comparable_test_rows']} 条。")
    elif number == 6 and result:
        lines.extend([f"标准证据总数：{result['records']}。", "",
                      "| 来源 | 条数 |", "|---|---:|"])
        lines.extend(f"| {source} | {count} |" for source, count in result["by_source"].items())
        lines.extend(["", "下次执行 `integrate-etfs` 或 `cycle` 自动继续所有就绪检查。个股阶段延期；不自动升级概率模型。"])
    if row.get("error_type"):
        lines.extend([f"错误类型：`{row['error_type']}`。", row["error"]])
    lines.extend(["", "本阶段结束后由编排器自动推进。waiting 表示缺数据/历史，尚未完成效果验证。"])
    return "\n".join(lines) + "\n"


def latest_integration(cfg: dict) -> dict | None:
    paths = sorted(resolve_project_path(cfg, cfg["runs_dir"]).glob("etf_integration_*/summary.json"), reverse=True)
    return json.loads(paths[0].read_text(encoding="utf-8")) if paths else None


def run_etf_integration(cfg: dict, universe: str | None = None) -> dict:
    universe = universe or cfg.get("evidence", {}).get("experiment_universe", "commodity")
    root = resolve_project_path(cfg, cfg["runs_dir"]) / f"etf_integration_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"
    root.mkdir(parents=True, exist_ok=False)
    report = {"run_type": "etf_integration", "scope": "etf_only", "started_at_utc": now_utc(),
              "stages": [], "summary_path": str((root / "summary.json").resolve())}

    def persist():
        (root / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    def stage(number: int, name: str, action, dependencies=()):
        prior = {row["stage"]: row["status"] for row in report["stages"]}
        row = {"stage": number, "name": name, "started_at_utc": now_utc()}
        if any(prior.get(dep) in {"failed", "blocked_dependency"} for dep in dependencies):
            row.update(status="blocked_dependency", dependencies=dependencies)
        else:
            try:
                value = action()
                status = value.get("stage_status", "complete")
                row.update(status=status, result=value)
            except Exception as exc:
                # No external stderr, credentials or raw model text in stage failure messages.
                row.update(status="failed", error_type=type(exc).__name__,
                           error="阶段失败；请独立运行对应 CLI 命令检查输入格式/数据路径")
        row["finished_at_utc"] = now_utc()
        detail = root / f"E{number}.json"
        detail.write_text(json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8")
        markdown = root / f"E{number}.md"
        markdown.write_text(_stage_markdown(row), encoding="utf-8")
        row["markdown_path"] = str(markdown.resolve())
        report["stages"].append(row)
        persist()

    stage(1, "ETF 范围与标准证据", lambda: {"configured_etfs": len(etf_assets(cfg)), **evidence_status(cfg)})

    def ingest():
        flow = sync_etf_evidence(cfg)
        agents = sync_existing_agents(cfg)
        imports = []
        inbox = resolve_project_path(cfg, cfg.get("evidence", {}).get("inbox_dir", "datasets/evidence_inbox"))
        for path in sorted(inbox.glob("*.json")):
            try:
                imports.append({"file": path.name, "status": "complete", **import_evidence(cfg, path)})
            except Exception as exc:
                imports.append({"file": path.name, "status": "failed", "error_type": type(exc).__name__})
        return {"flow": flow, "existing_agents": agents, "imports": imports,
                "stage_status": "partial_failure" if any(row["status"] == "failed" for row in imports) else "complete"}

    stage(2, "ETF 资金流、既有 Agent 与外部标准文件", ingest, (1,))
    stage(3, "ETF 资金行为与异常描述", lambda: etf_profiles(cfg), (2,))

    def news():
        result = sync_news_chains(cfg)
        return {"storage": result, **chain_report(cfg)}

    stage(4, "新闻→商品/板块→ETF", news, (1,))

    def experiments():
        items = []
        universes = [universe]
        if cfg.get("universes", {}).get("screened_current") and universe != "screened_current":
            universes.append("screened_current")
        for pool in universes:
            for horizon in cfg["horizons"]:
                _, snapshots = load_universe(cfg, pool)
                if len(snapshots) >= 2:
                    path, payload = run_evidence_experiments(cfg, pool, horizon)
                    items.append({"universe": pool, "horizon": horizon, "path": str(path.resolve()), "status": payload["status"],
                                  "baseline_metrics": payload["baseline_metrics"], "readiness": payload["readiness"]})
                else:
                    items.append({"universe": pool, "horizon": horizon, "status": "waiting_for_histories_or_valid_inputs",
                                  "baseline_metrics": {"rows": 0}, "readiness": {}})
        return {"items": items, "stage_status": "waiting_for_evidence_or_history"
                if any(item["status"] != "evaluated" for item in items) else "complete"}

    stage(5, "同时间切分的 ETF 外部证据增益实验", experiments, (2, 4))
    stage(6, "统一状态与报告", lambda: {**evidence_status(cfg), "next_run": "integrate-etfs 或 cycle",
                                        "probability_promotion": "disabled", "tradingagents_astock": "deferred_by_user"})
    statuses = {row["status"] for row in report["stages"]}
    report["outcome"] = ("partial_failure" if statuses & {"failed", "blocked_dependency", "partial_failure"}
                         else "complete_with_data_waits" if any(s.startswith("waiting") for s in statuses) else "complete")
    report["finished_at_utc"] = now_utc()
    lines = ["# ETF 分阶段整合运行", "", f"结果：`{report['outcome']}`", "",
             "| 阶段 | 内容 | 状态 | 记录 |", "|---|---|---|---|"]
    for row in report["stages"]:
        lines.append(f"| E{row['stage']} | {row['name']} | {row['status']} | [明细](E{row['stage']}.md) |")
    lines.extend(["", "A 股个股与 TradingAgents-Astock 按用户要求延期。",
                  "外部项目等待真实 ETF 证据；历史不足不宣称增益。每日 cycle 自动重做就绪检查，输入未变化复用相同实验。"])
    (root / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    persist()
    with connect(resolve_project_path(cfg, cfg["state_db"])) as connection:
        record_run(connection, "etf_integration", cfg, Path(report["summary_path"]))
    return report
