"""Lossless JSON-friendly persistence for daily agent results."""
from datetime import date, datetime
from pathlib import Path
from uuid import uuid4
import json


def new_run_id(now: datetime) -> str:
    return f"{now:%Y%m%d_%H%M%S_%f}_{uuid4().hex}"


def json_default(value):
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"Unsupported daily result type: {type(value).__name__}")


def save_daily_result(directory, ticker, run_id, run_time, analysis_date, result, decision):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    stem = f"{ticker}_{run_id}"
    summary = directory / f"{stem}.json"
    report = directory / f"{stem}.md"
    payload = {"schema_version": 2, "run_id": run_id, "ticker": ticker,
               "run_time": run_time.isoformat(), "analysis_date": analysis_date,
               "decision": decision, "result": result}
    serialized = json.dumps(payload, ensure_ascii=False, indent=2, default=json_default)
    # Exclusive creation prevents accidental replacement of an earlier run.
    with summary.open("x", encoding="utf-8") as handle:
        handle.write(serialized)
    with report.open("x", encoding="utf-8") as handle:
        handle.write(f"# {ticker} 每日预测\n\n- 运行编号: {run_id}\n"
                     f"- 分析日期: {analysis_date}\n- 最终决策: **{decision}**\n\n"
                     f"完整结构化结果：[{summary.name}]({summary.name})\n\n"
                     f"```json\n{serialized}\n```\n")
    return str(report), str(summary)
