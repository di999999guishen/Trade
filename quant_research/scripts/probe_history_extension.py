"""Freeze bounded public data probes for gaps; never modify accepted snapshots."""
import argparse
import json
from pathlib import Path

from curl_cffi import requests

from quant_research.artifacts import file_hash, publication, run_id, utc_now, write_json


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--round", type=int, choices=(1, 2, 3, 4, 5, 6, 7, 8, 9), default=1)
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[1]
    probes = [
        ("yahoo_ief", "https://query1.finance.yahoo.com/v8/finance/chart/IEF",
         {"period1": "1420070400", "period2": "1504224000", "interval": "1d", "events": "div,splits"}),
        ("tencent_ief", "https://web.ifzq.gtimg.cn/appstock/app/usfqkline/get",
         {"param": "usIEF,day,2015-01-01,2017-09-01,1000,qfq"}),
        ("eastmoney_ief", "https://push2his.eastmoney.com/api/qt/stock/kline/get",
         {"secid": "105.IEF", "fields1": "f1,f2,f3,f4,f5,f6", "fields2": "f51,f52,f53,f54,f55,f56,f57",
          "klt": "101", "fqt": "0", "beg": "20150101", "end": "20170901", "lmt": "1000"}),
        ("sina_ief_paged", "https://stock.finance.sina.com.cn/usstock/api/json_v2.php/US_MinKService.getDailyK",
         {"symbol": "ief", "scale": "240", "datalen": "10000", "end": "2016-01-01"}),
        ("nasdaq_ief", "https://api.nasdaq.com/api/quote/IEF/historical",
         {"assetclass": "etf", "fromdate": "2015-01-01", "todate": "2017-09-01", "limit": "1000"}),
    ]
    if args.round == 2:
        probes = [
            ("tencent_ief_oq", "https://web.ifzq.gtimg.cn/appstock/app/usfqkline/get",
             {"param": "usIEF.OQ,day,,2017-09-01,640,"}),
            ("tencent_tlt_oq", "https://web.ifzq.gtimg.cn/appstock/app/usfqkline/get",
             {"param": "usTLT.OQ,day,,2016-06-01,640,"}),
            ("tencent_xlb", "https://web.ifzq.gtimg.cn/appstock/app/usfqkline/get",
             {"param": "usXLB.P,day,,2018-12-01,30,"}),
            ("tencent_ief_end", "https://web.ifzq.gtimg.cn/appstock/app/usfqkline/get",
             {"param": "usIEF,day,,2017-09-01,640,"}),
            ("yahoo2_ief", "https://query2.finance.yahoo.com/v8/finance/chart/IEF",
             {"period1": "1420070400", "period2": "1504224000", "interval": "1d", "events": "div,splits"}),
        ]
    if args.round == 3:
        probes = [(name, "https://web.ifzq.gtimg.cn/appstock/app/usfqkline/get", {"param": param})
                  for name, param in [
                      ("tencent_ief_current", "usIEF,day,,,320,qfq"),
                      ("tencent_ief_old", "usIEF,day,2015-09-01,2017-09-01,640,qfq"),
                      ("tencent_ief_oq_old", "usIEF.OQ,day,2015-09-01,2017-09-01,640,qfq"),
                      ("tencent_xlb_old", "usXLB,day,2018-11-01,2018-12-01,30,qfq"),
                  ]]
    if args.round == 4:
        probes = [(name, "https://web.ifzq.gtimg.cn/appstock/app/usfqkline/get", {"param": param})
                  for name, param in [
                      ("tencent_ief_raw", "usIEF.OQ,day,2015-09-01,2017-09-01,640,"),
                      ("tencent_ief_bfq", "usIEF.OQ,day,2015-09-01,2017-09-01,640,bfq"),
                      ("tencent_xlb_raw", "usXLB.P,day,2018-11-01,2018-12-01,30,"),
                      ("tencent_tlt_raw", "usTLT.OQ,day,2015-09-01,2016-06-01,640,"),
                  ]]
    if args.round == 5:
        probes = [
            ("tencent_ief_uskline", "https://web.ifzq.gtimg.cn/appstock/app/uskline/get",
             {"param": "usIEF.OQ,day,2015-09-01,2017-09-01,640"}),
            ("tencent_ief_kline", "https://web.ifzq.gtimg.cn/appstock/app/kline/kline",
             {"param": "usIEF.OQ,day,2015-09-01,2017-09-01,640"}),
            ("tencent_ief_qfq_none", "https://web.ifzq.gtimg.cn/appstock/app/usfqkline/get",
             {"param": "usIEF.OQ,day,2015-09-01,2017-09-01,640,none"}),
        ]
    if args.round == 6:
        probes = [(name, "https://web.ifzq.gtimg.cn/appstock/app/kline/kline", {"param": param})
                  for name, param in [
                      ("IEF", "usIEF.OQ,day,2015-09-01,2017-09-01,640"),
                      ("TLT", "usTLT.OQ,day,2015-09-01,2016-06-01,640"),
                      ("XLB", "usXLB.P,day,2018-10-01,2018-12-15,640"),
                      ("XLE", "usXLE.P,day,2018-10-01,2018-12-15,640"),
                      ("XLRE", "usXLRE.P,day,2015-10-01,2017-09-01,640"),
                      ("XLC", "usXLC.P,day,2018-06-01,2018-08-01,640"),
                      ("SPY", "usSPY.P,day,2015-09-01,2015-10-01,640"),
                  ]]
    if args.round == 7:
        probes = [(name, "https://web.ifzq.gtimg.cn/appstock/app/kline/kline", {"param": param})
                  for name, param in [
                      ("XLB", "usXLB.AM,day,2018-10-01,2018-12-15,640"),
                      ("XLE", "usXLE.AM,day,2018-10-01,2018-12-15,640"),
                      ("XLRE", "usXLRE.AM,day,2015-10-01,2017-09-01,640"),
                      ("XLC", "usXLC.AM,day,2018-06-01,2018-08-01,640"),
                      ("SPY", "usSPY.AM,day,2015-09-01,2015-10-01,640"),
                  ]]
    if args.round == 8:
        probes = [(name, "https://web.ifzq.gtimg.cn/appstock/app/kline/kline", {"param": param})
                  for name, param in [
                      ("QQQ_2005", "usQQQ.OQ,day,2005-09-01,2007-09-01,640"),
                      ("IEF_2005", "usIEF.OQ,day,2005-09-01,2007-09-01,640"),
                      ("TLT_2005", "usTLT.OQ,day,2005-09-01,2007-09-01,640"),
                      ("SPY_2015", "usSPY.AM,day,2015-03-01,2015-04-30,640"),
                  ]]
    if args.round == 9:
        probes = [
            ("QQQQ_2005", "https://web.ifzq.gtimg.cn/appstock/app/kline/kline",
             {"param": "usQQQQ.OQ,day,2005-09-01,2007-09-01,640"}),
            ("sina_QQQQ", "https://stock.finance.sina.com.cn/usstock/api/json_v2.php/US_MinKService.getDailyK",
             {"symbol": "qqqq", "scale": "240", "datalen": "10000"}),
        ]
    identifier = "history_probe_" + run_id().rsplit("_", 1)[1]
    results = []
    with publication(project / "outputs", identifier) as stage:
        with requests.Session(impersonate="chrome") as client:
            for name, url, params in probes:
                record = {"name": name, "url": url, "params": params, "checked_at": utc_now()}
                try:
                    response = client.get(url, params=params, timeout=12)
                    target = stage / f"{name}.response"
                    target.write_bytes(response.content)
                    record.update(status=response.status_code, bytes=len(response.content), sha256=file_hash(target))
                    try:
                        payload = response.json()
                        record["json_type"] = type(payload).__name__
                        if isinstance(payload, list):
                            record.update(rows=len(payload), first=payload[0] if payload else None)
                        elif isinstance(payload, dict):
                            record["keys"] = list(payload)[:8]
                    except ValueError:
                        record["json_type"] = None
                except Exception as exc:  # noqa: BLE001 -- no arbitrary network error text
                    record["error"] = type(exc).__name__
                results.append(record)
                print(json.dumps(record), flush=True)
        write_json(stage / "manifest.json", {"status": "probe_only", "results": results,
                                             "files": {p.name: file_hash(p) for p in stage.iterdir()}})
    print(project / "outputs" / identifier)


if __name__ == "__main__":
    main()
