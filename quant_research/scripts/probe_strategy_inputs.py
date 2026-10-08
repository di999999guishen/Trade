"""Bounded public-source checks; preserve responses, never overwrite snapshots."""
import json
import subprocess
import sys
from pathlib import Path

from curl_cffi import requests

from quant_research.artifacts import file_hash, publication, run_id, utc_now, write_json


def main():
    project = Path(__file__).resolve().parents[1]
    if len(sys.argv) > 1 and sys.argv[1] == "--s4-history":
        from acquire_s4_history import acquire
        acquire(project)
        return
    if len(sys.argv) > 1 and sys.argv[1] == "--yfinance":
        import yfinance as yf
        yf.set_tz_cache_location(str(project / "data/yfinance_probe_cache"))
        ticker = yf.Ticker("QQQ")
        try:
            frame = ticker.history(start="2005-09-01", end="2007-09-01", auto_adjust=False,
                                   timeout=12, raise_errors=True)
            frame.to_parquet(Path(sys.argv[2]) / "yfinance_qqq.parquet")
            print(json.dumps({"rows": len(frame), "version": yf.__version__}))
        except Exception as exc:  # noqa: BLE001
            print(json.dumps({"error": type(exc).__name__, "message": str(exc)}))
        return
    identifier = run_id("strategy_inputs")
    with publication(project / "outputs", identifier) as stage:
        reports = {}
        endpoints = [
            ("sec_aapl_facts", "https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json", {}),
            ("sec_aapl_submissions", "https://data.sec.gov/submissions/CIK0000320193.json", {}),
            ("eastmoney_aapl_financials", "https://datacenter.eastmoney.com/securities/api/data/v1/get",
             {"reportName": "RPT_USF10_FN_GMAININDICATOR", "columns": "ALL",
              "filter": '(SECUCODE="AAPL.O")', "pageNumber": "1", "pageSize": "200",
              "sortColumns": "REPORT_DATE", "sortTypes": "-1", "source": "SECURITIES", "client": "PC"}),
        ]
        for name, url, params in endpoints:
            try:
                response = requests.get(url, params=params, timeout=15,
                                        headers={"User-Agent": "TradingAgents-quant-research/0.1 public-data-check"})
                (stage / f"{name}.raw").write_bytes(response.content)
                reports[name] = {"status": response.status_code, "bytes": len(response.content),
                                 "url": url, "params": params, "fetched_at": utc_now()}
            except Exception as exc:  # noqa: BLE001
                reports[name] = {"error": type(exc).__name__, "message": str(exc), "url": url}
            print(name, reports[name], flush=True)
        try:
            result = subprocess.run([sys.executable, __file__, "--yfinance", str(stage)],
                                    capture_output=True, text=True, timeout=45, check=False)
            reports["yfinance_qqq"] = {"stdout": result.stdout, "stderr": result.stderr,
                                       "exit_code": result.returncode}
        except subprocess.TimeoutExpired:
            reports["yfinance_qqq"] = {"error": "TimeoutExpired", "seconds": 45}
        write_json(stage / "report.json", reports)
        write_json(stage / "manifest.json", {"files": {p.name: file_hash(p) for p in stage.iterdir()},
                                               "script_hash": file_hash(__file__)})
    print(project / "outputs" / identifier)


if __name__ == "__main__":
    main()
