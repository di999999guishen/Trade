"""Small, auditable connectivity probes; downloaded bodies are not market snapshots."""
import json
from pathlib import Path

from curl_cffi import requests

from quant_research.artifacts import file_hash, publication, run_id, utc_now, write_json


def main():
    root = Path(__file__).resolve().parents[1]
    identifier = run_id("domestic_probe")
    endpoints = [
        ("eastmoney_spy", "https://63.push2his.eastmoney.com/api/qt/stock/kline/get",
         {"secid": "107.SPY", "fields1": "f1,f2,f3,f4,f5,f6",
          "fields2": "f51,f52,f53,f54,f55,f56,f57", "klt": "101", "fqt": "0",
          "beg": "20260817", "end": "20260917", "lmt": "30"}),
        ("eastmoney_qqq", "https://63.push2his.eastmoney.com/api/qt/stock/kline/get",
         {"secid": "105.QQQ", "fields1": "f1,f2,f3,f4,f5,f6",
          "fields2": "f51,f52,f53,f54,f55,f56,f57", "klt": "101", "fqt": "0",
          "beg": "20260817", "end": "20260917", "lmt": "30"}),
        ("sina_spy", "https://finance.sina.com.cn/staticdata/us/SPY", {}),
        ("sina_qqq", "https://finance.sina.com.cn/staticdata/us/QQQ", {}),
        ("sina_json_spy", "https://stock.finance.sina.com.cn/usstock/api/json_v2.php/US_MinKService.getDailyK",
         {"symbol": "spy", "scale": "240", "datalen": "10000"}),
    ]
    results = []
    with publication(root / "outputs", identifier) as stage:
        for label, url, params in endpoints:
            result = {"name": label, "url": url, "params": params, "checked_at": utc_now()}
            try:
                response = requests.get(url, params=params, timeout=15, impersonate="chrome")
                body = stage / f"{label}.response"
                body.write_bytes(response.content)
                result.update(http_status=response.status_code, bytes=len(response.content),
                              file=body.name, sha256=file_hash(body))
                if label.startswith("eastmoney"):
                    data = response.json().get("data") or {}
                    result.update(code=data.get("code"), name_returned=data.get("name"),
                                  rows=len(data.get("klines") or []))
                else:
                    result["looks_encoded"] = response.text.lstrip().startswith("var ")
            except Exception as exc:  # noqa: BLE001 -- diagnostic, no exception URLs/credentials
                result["error_type"] = type(exc).__name__
            results.append(result)
            print(json.dumps(result, ensure_ascii=False), flush=True)
        write_json(stage / "manifest.json", {"status": "probe_only_not_validated_market_data",
                                             "results": results})
    print(str(root / "outputs" / identifier), flush=True)


if __name__ == "__main__":
    main()
