import sys, time, requests
from pathlib import Path
PROJECT = Path(r"E:/Trade/TradingAgents")
sys.path.insert(0, str(PROJECT))
try:
    import wmi_guard  # noqa: F401
except Exception:
    pass
import akshare as ak

S = requests.Session(); S.trust_env = False

# 1) NAV via akshare fund_etf_fund_daily_em
t0 = time.time()
try:
    df = ak.fund_etf_fund_daily_em()
    cols = [c for c in df.columns if "单位净值" in c]
    for c in ["159792", "159740", "159158"]:
        sub = df[df["基金代码"].astype(str) == c]
        if len(sub):
            r = sub.iloc[0]
            got = None
            for col in sorted(cols, reverse=True):
                v = str(r[col]).strip()
                if v and v not in ("---", "nan", "None"):
                    got = (col.replace("-单位净值", ""), v); break
            print(f"NAV {c} -> {got}", flush=True)
        else:
            print(f"NAV {c} -> not found", flush=True)
except Exception as exc:
    print(f"NAV err: {type(exc).__name__}: {str(exc)[:120]}", flush=True)
print(f"[nav] {time.time()-t0:.1f}s", flush=True)

# 2) HK index proxy daily via eastmoney push2his
for secid in ["105.HSTECH", "1.930456", "105.3032"]:
    try:
        r = S.get("https://push2his.eastmoney.com/api/qt/stock/kline/get",
                  params={"secid": secid, "klt": 101, "fqt": 1, "beg": "20260901",
                          "end": "20500101", "fields1": "f1,f2,f3",
                          "fields2": "f51,f53"}, timeout=20)
        data = (r.json().get("data") or {})
        klines = data.get("klines") or []
        print(f"KLINE {secid} name={data.get('name')} rows={len(klines)} last={klines[-1] if klines else ''}", flush=True)
    except Exception as exc:
        print(f"KLINE {secid} err: {type(exc).__name__}: {str(exc)[:100]}", flush=True)
