"""Audit frozen full Sina responses for 10/20-year US ETF research feasibility."""
import json
from pathlib import Path

import pandas as pd

from quant_research.artifacts import file_hash, publication, run_id, verify_files, write_json
from quant_research.calendar import calendar


def main():
    project = Path(__file__).resolve().parents[1]
    candidate = project / "data/domestic/sina_candidate_20260918T035536607818Z_e935d5dea9934db1bf8e64d2a5a8cf3f"
    manifest = json.loads((candidate / "manifest.json").read_text(encoding="utf-8"))
    verify_files(candidate, manifest)
    evidence = json.loads((project / "configs/sina_pilot_evidence.json").read_text(encoding="utf-8"))
    cal = calendar()
    end = pd.Timestamp("2026-09-17")
    rows = []
    for symbol in manifest["request"]["symbols"]:
        data = pd.DataFrame(json.loads((candidate / f"{symbol}.response.json").read_text(encoding="utf-8")))
        data.index = pd.to_datetime(data.d)
        numeric = data[["o", "h", "l", "c", "v"]].apply(pd.to_numeric)
        bad = ((numeric.h < numeric[["o", "c", "l"]].max(axis=1))
               | (numeric.l > numeric[["o", "c", "h"]].min(axis=1))
               | (numeric[["o", "h", "l", "c"]] <= 0).any(axis=1)
               | numeric.isna().any(axis=1) | (numeric.v < 0))
        record = {"symbol": symbol, "vendor_first": str(data.index.min().date()),
                  "vendor_last": str(data.index.max().date()), "rows": len(data),
                  "inception": evidence["products"][symbol]["inception"],
                  "duplicate_dates": int(data.index.duplicated().sum()), "windows": {}}
        for years in (10, 20):
            test_start = cal.date_to_session(end - pd.DateOffset(years=years), direction="next")
            warmup_start = cal.session_offset(test_start, -252)
            # Issuer inception is an approximate listing lower bound, not a proof
            # of the first tradable session. Preserve this distinction in output.
            expected_start = max(warmup_start, cal.date_to_session(record["inception"], direction="next")
                                 if pd.Timestamp(record["inception"]) >= cal.first_session else warmup_start)
            expected = cal.sessions_in_range(expected_start, end)
            missing = expected.difference(data.index)
            invalid = data.index[bad & data.index.isin(expected)]
            record["windows"][str(years)] = {
                "test_start": str(test_start.date()), "test_end": str(end.date()),
                "warmup_start": str(warmup_start.date()), "expected_from_inception_proxy": str(expected_start.date()),
                "not_yet_incepted_at_test_start": pd.Timestamp(record["inception"]) > test_start,
                "missing_sessions": len(missing), "first_missing": str(missing[0].date()) if len(missing) else None,
                "last_missing": str(missing[-1].date()) if len(missing) else None,
                "invalid_ohlcv_dates": [str(d.date()) for d in invalid],
                "basic_coverage_pass": not len(missing) and not len(invalid) and not data.index.has_duplicates,
                "total_return_basis_independently_verified": False}
        rows.append(record)
    identifier = "history_" + run_id().rsplit("_", 1)[1]
    with publication(project / "outputs", identifier) as stage:
        write_json(stage / "coverage.json", {"status": "feasibility_only_not_backtest_ready",
                                             "source_manifest_hash": file_hash(candidate / "manifest.json"),
                                             "source": str(candidate), "assets": rows})
        report = ["# 10/20 年美股 ETF 历史覆盖审计", "",
                  "检查完整原始响应，未沿用旧采集请求的 2010 年筛选；不修补、不拼接、不生成策略收益。",
                  "缺失统计从预热起点或发行人成立日开始；成立日仅作上市下界代理。新 ETF 按成立后预热入池，不回填。",
                  "价格覆盖通过不代表复权、公司行动或历史可得时点已经核验。", "",
                  "| 资产 | 已下载起点 | 10年：缺日 / 坏行 | 20年：缺日 / 坏行 |",
                  "|---|---|---:|---:|"]
        for row in rows:
            counts = [f"{row['windows'][str(n)]['missing_sessions']} / {len(row['windows'][str(n)]['invalid_ohlcv_dates'])}" for n in (10, 20)]
            report.append(f"| {row['symbol']} | {row['vendor_first']} | {counts[0]} | {counts[1]} |")
        report += ["", "S3 若要求净测试期达到 10/20 年，另需向前约 6 年训练验证和约 1 年特征预热，不能把输入历史长度当成模型测试长度。",
                   "S4/S5/S6/S7 的 PIT 财务、预期、借券和期权链缺口不由延长日线解决。"]
        (stage / "report.md").write_text("\n".join(report) + "\n", encoding="utf-8")
        (stage / "audit_source.py").write_bytes(Path(__file__).read_bytes())
        write_json(stage / "manifest.json", {"status": "feasibility_only", "files": {
            p.name: file_hash(p) for p in stage.iterdir() if p.is_file()}})
    print(project / "outputs" / identifier)
    for row in rows:
        print(row["symbol"], row["vendor_first"], {n: (w["missing_sessions"], len(w["invalid_ohlcv_dates"]))
                                                  for n, w in row["windows"].items()})


if __name__ == "__main__":
    main()
