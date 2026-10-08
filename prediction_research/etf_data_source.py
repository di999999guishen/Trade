"""ETF 日线统一数据源 —— 带自动降级的多源获取。

背景
----
本机到 ``*.push2*.eastmoney.com`` 全系列主机的 HTTPS 连接在 TLS 重协商后
被 RST（curl error 56 / requests RemoteDisconnected）。已逐一验证：

    主机                          结果
    push2his.eastmoney.com        RST
    82.push2.eastmoney.com        RST
    push2.eastmoney.com           RST
    push2delay.eastmoney.com      RST
    48.push2 / 1.push2            RST

而在**同一个**代理出口下，腾讯 web.ifzq.gtimg.cn 与新浪 money.finance.sina.com.cn
均返回 200 且数据完整。据此判定：

  * 不是代理配置问题（同一代理，其他站点通畅）；
  * 不是 TLS 指纹问题（curl_cffi 的 chrome / edge / safari 指纹同样被 RST）；
  * 是东财侧对本机网络出口的限制，属于服务端单方面行为，本地无法修复。

结论：不要再去"修"东财这条链路，改为多源自动降级。东财源保留在链路最前面，
一旦网络环境变化（换网 / 换出口 IP）它能自动恢复，无需改代码。

用法
----
    from prediction_research.etf_data_source import fetch_etf_daily, refresh_pool

    df = fetch_etf_daily("518880")            # 自动选源
    df = fetch_etf_daily("518880", source="sina")
    report = refresh_pool(["518880", "510300"], cache_dir="datasets/market")

输出统一为 DataFrame，列名与 datasets/market/cache_*.csv 一致：
    date, open, high, low, close, volume, amount
"""

from __future__ import annotations

import io
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

import pandas as pd

__all__ = [
    "SourceResult",
    "SourceUnavailable",
    "fetch_etf_daily",
    "refresh_pool",
    "probe_sources",
    "to_prefixed_symbol",
    "SOURCE_CHAIN",
]

# 降级链顺序：东财在最前，网络恢复后可自动切回。
SOURCE_CHAIN: tuple[str, ...] = ("eastmoney", "sina", "tencent")

_STANDARD_COLUMNS = ["date", "open", "high", "low", "close", "volume", "amount"]

_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "zh-CN,zh;q=0.9",
}


class SourceUnavailable(RuntimeError):
    """某个数据源不可用（网络被拒、返回空、解析失败）。"""


@dataclass
class SourceResult:
    """一次取数的结果，含来源与耗时，便于事后审计数据出自哪里。"""

    source: str
    rows: int
    elapsed_sec: float
    first_date: str | None = None
    last_date: str | None = None
    df: pd.DataFrame | None = field(default=None, repr=False)

    def as_dict(self) -> dict:
        return {
            "source": self.source,
            "rows": self.rows,
            "elapsed_sec": round(self.elapsed_sec, 2),
            "first_date": self.first_date,
            "last_date": self.last_date,
        }


# --------------------------------------------------------------------------
# 代码 → 带市场前缀
# --------------------------------------------------------------------------

def to_prefixed_symbol(code: str) -> str:
    """518880 -> sh518880, 159985 -> sz159985。

    上交所：50/51/56/58/60 开头（含 501018 这类 LOF）
    深交所：12/15/16/159 开头（含 162411 这类 LOF）
    """
    code = str(code).strip()
    if code[:2].lower() in ("sh", "sz"):
        return code.lower()
    if code[:2] in ("50", "51", "56", "58", "60", "68"):
        return "sh" + code
    if code[:2] in ("12", "15", "16"):
        return "sz" + code
    # 兜底：6 开头沪市，其余深市
    return ("sh" if code.startswith("6") else "sz") + code


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    """裁剪为标准列、排序、去重、转类型。"""
    if df is None or df.empty:
        raise SourceUnavailable("empty frame")

    d = df.copy()
    d.columns = [str(c).strip().lower() for c in d.columns]

    missing = [c for c in _STANDARD_COLUMNS if c not in d.columns]
    if missing:
        raise SourceUnavailable(f"missing columns: {missing}")

    d = d[_STANDARD_COLUMNS].copy()
    d["date"] = pd.to_datetime(d["date"], errors="coerce").dt.strftime("%Y-%m-%d")
    d = d.dropna(subset=["date"])
    for c in _STANDARD_COLUMNS[1:]:
        d[c] = pd.to_numeric(d[c], errors="coerce")
    d = d.dropna(subset=["close"])
    d = d.sort_values("date").drop_duplicates("date", keep="last").reset_index(drop=True)
    if d.empty:
        raise SourceUnavailable("no valid rows after normalize")
    return d


# --------------------------------------------------------------------------
# 三个源的具体实现
# --------------------------------------------------------------------------

def _fetch_eastmoney(code: str) -> pd.DataFrame:
    """东财（AKShare fund_etf_hist_em）。本机当前被 RST，保留以待网络恢复。"""
    import akshare as ak

    df = ak.fund_etf_hist_em(
        symbol=code, period="daily", start_date="19700101",
        end_date="20991231", adjust="qfq",
    )
    rename = {"日期": "date", "开盘": "open", "最高": "high", "最低": "low",
              "收盘": "close", "成交量": "volume", "成交额": "amount"}
    return df.rename(columns=rename)


def _fetch_sina(code: str) -> pd.DataFrame:
    """新浪（AKShare fund_etf_hist_sina）。当前实测可用，返回全历史约 3200 行。"""
    import akshare as ak

    return ak.fund_etf_hist_sina(symbol=to_prefixed_symbol(code))


def _fetch_tencent(code: str) -> pd.DataFrame:
    """腾讯直连接口。

    注意：该接口返回的是"截至 end 的最近 N 条"而非全区间，单次上限约
    640-1150 行。因此分段必须**向前推进 end**（每轮把 end 挪到已取到的
    最早一天的前一天），直到不再返回新数据或触底。
    """
    import datetime as _dt

    import requests

    sym = to_prefixed_symbol(code)
    rows: dict[str, list] = {}
    end = _dt.date.today()

    for _ in range(12):  # 每轮最多约 1000 行，足够覆盖十年日线
        url = (
            "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
            f"?param={sym},day,1990-01-01,{end.isoformat()},1200,qfq"
        )
        r = requests.get(url, headers=_DEFAULT_HEADERS, timeout=20)
        r.raise_for_status()
        payload = (r.json() or {}).get("data") or {}
        node = payload.get(sym) or {}
        arr = node.get("qfqday") or node.get("day") or []
        if not arr:
            break

        for row in arr:
            rows[str(row[0])] = row

        days = sorted(rows)
        earliest = days[0]
        # 新数据少于 2 条，或已触达请求上界，说明到底了
        if len(arr) < 2:
            break
        try:
            next_end = _dt.date.fromisoformat(earliest) - _dt.timedelta(days=1)
        except ValueError:
            break
        if next_end >= end or next_end.year < 1990:
            break
        end = next_end

    if not rows:
        raise SourceUnavailable("tencent returned no rows")

    # 腾讯的字段顺序是 open, close, high, low —— close 在第 3 位，
    # 这与常见的 OHLC 顺序不同，按 OHLC 解析会把 close 读成 low（实测偏差 0.07）。
    _TENCENT_COLUMNS = ["date", "open", "close", "high", "low", "volume"]
    ordered = [rows[d] for d in sorted(rows)]
    df = pd.DataFrame(ordered, columns=_TENCENT_COLUMNS)
    df = df[["date", "open", "high", "low", "close", "volume"]]
    df["amount"] = 0
    return df


_FETCHERS = {
    "eastmoney": _fetch_eastmoney,
    "sina": _fetch_sina,
    "tencent": _fetch_tencent,
}


# --------------------------------------------------------------------------
# 对外 API
# --------------------------------------------------------------------------

def fetch_etf_daily(
    code: str,
    source: str | None = None,
    sources: Sequence[str] | None = None,
) -> SourceResult:
    """取一只 ETF 的全历史日线，自动按 SOURCE_CHAIN 降级。

    source 为 None 时自动降级；显式指定则只用该源，失败即抛错。
    """
    chain = [source] if source else list(sources or SOURCE_CHAIN)
    errors: list[str] = []

    for name in chain:
        fn = _FETCHERS.get(name)
        if fn is None:
            errors.append(f"{name}: unknown source")
            continue
        t0 = time.time()
        try:
            df = _normalize(fn(code))
        except Exception as exc:  # noqa: BLE001 - 降级链需要吞掉一切再试下一个源
            errors.append(f"{name}: {type(exc).__name__}: {str(exc)[:120]}")
            continue
        elapsed = time.time() - t0
        return SourceResult(
            source=name,
            rows=len(df),
            elapsed_sec=elapsed,
            first_date=str(df["date"].iloc[0]),
            last_date=str(df["date"].iloc[-1]),
            df=df,
        )

    raise SourceUnavailable(f"all sources failed for {code}: " + " | ".join(errors))


def probe_sources(code: str = "518880") -> list[dict]:
    """依次探测每个源是否可用，用于诊断网络环境是否恢复。"""
    out: list[dict] = []
    for name in SOURCE_CHAIN:
        t0 = time.time()
        try:
            res = fetch_etf_daily(code, source=name)
            out.append({"source": name, "ok": True, **res.as_dict()})
        except Exception as exc:  # noqa: BLE001
            out.append({
                "source": name, "ok": False,
                "elapsed_sec": round(time.time() - t0, 2),
                "error": f"{type(exc).__name__}: {str(exc)[:100]}",
            })
    return out


def refresh_pool(
    codes: Iterable[str],
    cache_dir: str | Path = "datasets/market",
    min_rows: int = 20,
    sleep_sec: float = 0.3,
) -> dict:
    """批量刷新并写回 cache_<code>.csv，保持与现有文件格式完全一致。

    返回每个代码的结果，失败的会带上 error 字段。
    """
    cache = Path(cache_dir)
    cache.mkdir(parents=True, exist_ok=True)
    report: dict[str, dict] = {}

    for code in codes:
        try:
            res = fetch_etf_daily(code)
        except Exception as exc:  # noqa: BLE001
            report[code] = {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:150]}"}
            continue

        df = res.df
        if len(df) < min_rows:
            report[code] = {"ok": False, "error": f"too few rows: {len(df)}"}
            continue

        dest = cache / f"cache_{code}.csv"
        df.to_csv(dest, index=False, encoding="utf-8-sig")
        report[code] = {"ok": True, "file": str(dest), **res.as_dict()}
        time.sleep(sleep_sec)

    return report


if __name__ == "__main__":
    import argparse
    import json

    ap = argparse.ArgumentParser(description="ETF 多源数据获取 / 源探测")
    ap.add_argument("--probe", action="store_true", help="只探测各源可用性")
    ap.add_argument("--code", action="append", help="ETF 代码，可重复")
    ap.add_argument("--pool", action="store_true", help="刷新<｜hy_place▁holder▁no▁813｜>默认池")
    ap.add_argument("--cache-dir", default="datasets/market")
    args = ap.parse_args()

    # 默认（无参数）只做只读探测，绝不写盘 —— 写盘必须显式 --pool / --code + --cache-dir
    if args.probe or (not args.code and not args.pool):
        print(json.dumps(probe_sources(), ensure_ascii=False, indent=2))

    if args.pool:
        import json as _json

        cfg_path = Path(__file__).with_name("config") / "research.json"
        universes = _json.loads(cfg_path.read_text(encoding="utf-8")).get("universes", {})
        codes = sorted({
            str(item.get("symbol"))
            for lst in universes.values()
            for item in lst
            if item.get("symbol")
        })
    else:
        codes = args.code or []

    if codes:
        if not args.cache_dir or args.cache_dir == "datasets/market":
            print(
                "\n[警告] 未显式指定 --cache-dir，将写入默认 datasets/market 并覆盖现有缓存。\n"
                "       如需保留旧数据请先备份，或指定 --cache-dir <其他目录>。",
                flush=True,
            )
        print(json.dumps(refresh_pool(codes, args.cache_dir), ensure_ascii=False, indent=2))
