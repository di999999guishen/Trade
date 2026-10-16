"""
宏观数据流 (Macro Flow)
=======================
解决痛点：美国 CPI/PPI/非农/FOMC 发布了，我却在新闻流里看不到。

做法：
  1. 用 `flow_config.MACRO_RELEASES` 里的 2026 真实发布日历，判定某指标「是否已发布 / 距今天数 / 下次发布日」。
  2. 用 akshare（国内源，符合禁 FRED/yfinance 约定）拉取各指标最新实际值。
     akshare 宏观序列「最后一行是未来发布的占位行（现值 NaN）」，故取「最后一个非空值行」。
  3. 输出结构化结果，供 HTML 报告与 daily_predict 注入。

注意：akshare 部分月率序列存在约一年数据滞后，其数值仅作参考，
发布日一律以 MACRO_RELEASES 日历为准（cpi_yoy 当前可用，已实测 9月CPI=3.7% 于 10-14 发布）。
"""

from __future__ import annotations

import sys
from datetime import datetime, date
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from flow_config import MACRO_RELEASES, MACRO_AKSHARE_FUNCS, FRESH_WINDOW_DAYS  # noqa: E402


def _parse_date(s: str) -> date | None:
    try:
        return datetime.strptime(str(s).strip()[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def _fetch_akshare_latest(func_name: str) -> dict | None:
    """抓取 akshare 宏观函数「最后一个非空值行」。返回 {date, current, previous, release} 或 None。"""
    try:
        import akshare as ak
    except Exception as e:  # pragma: no cover
        print(f"  [WARN] akshare 不可用: {e}")
        return None
    try:
        fn = getattr(ak, func_name, None)
        if fn is None:
            return None
        df = fn()
        if df is None or len(df) == 0:
            return None
        cols = [str(c) for c in df.columns]
        # 取值列：现值 / 今值
        val_col = next((c for c in cols if c in ("现值", "今值")), None)
        prev_col = "前值" if "前值" in cols else None
        date_col = next((c for c in cols if c in ("时间", "日期")), cols[0])
        release_col = "发布日期" if "发布日期" in cols else None
        if val_col is None:
            return None
        clean = df[df[val_col].notna()]
        if len(clean) == 0:
            return None
        row = clean.iloc[-1]
        return {
            "date": str(row[date_col])[:10],
            "current": str(row[val_col]),
            "previous": str(row[prev_col]) if prev_col else "",
            "release": str(row[release_col])[:10] if release_col else None,
        }
    except Exception as e:
        print(f"  [WARN] 抓 {func_name} 失败: {str(e)[:80]}")
        return None


def _release_status(cal_key: str, today: date) -> dict:
    rows = [r for r in MACRO_RELEASES if r["indicator"] == cal_key]
    rows.sort(key=lambda r: r["release_date"])
    last = None
    nxt = None
    for r in rows:
        d = _parse_date(r["release_date"])
        if d is None:
            continue
        if d <= today:
            last = {**r, "release_date_parsed": d}
        elif nxt is None:
            nxt = {**r, "release_date_parsed": d}
    return {"last": last, "next": nxt}


def fetch_macro_flow(today: date | None = None) -> dict[str, Any]:
    today = today or date.today()
    items = []
    print("\n[宏观流] 拉取美国宏观实际值 + 发布日历...")

    for label, spec in MACRO_AKSHARE_FUNCS.items():
        cal_key = spec["cal_key"]
        actual = _fetch_akshare_latest(spec["func"])
        status = _release_status(cal_key, today)

        last = status.get("last")
        nxt = status.get("next")
        days_since = (today - last["release_date_parsed"]).days if last else None
        fresh = (days_since is not None and 0 <= days_since <= FRESH_WINDOW_DAYS)

        # 数据滞后判定：akshare 实际数据月份 与 日历数据月 不一致 → 源滞后
        # （cpi_yoy 的「时间」=2026-09 与日历数据月 2026-09 一致；月率序列常滞后到 2025 → 判滞后）
        stale = False
        if actual and last:
            ad = _parse_date(actual.get("date", ""))
            if ad:
                data_month = f"{ad.year}-{ad.month:02d}"
                if data_month != last["data_month"]:
                    stale = True

        items.append({
            "label": label,
            "actual_date": actual.get("date") if actual else None,
            "current": actual.get("current") if actual else None,
            "previous": actual.get("previous") if actual else None,
            "source_release": actual.get("release") if actual else None,
            "last_release": last["release_date"] if last else None,
            "last_data_month": last["data_month"] if last else None,
            "next_release": nxt["release_date"] if nxt else None,
            "next_data_month": nxt["data_month"] if nxt else None,
            "days_since_release": days_since,
            "fresh": fresh,
            "stale": stale,
            "fetched": actual is not None,
        })
        flag = "🔥刚发布" if fresh else ("已发布" if days_since is not None else "未发布")
        cur = items[-1]["current"]
        stale_tag = " (源滞后)" if stale else ""
        print(f"  {label:8s} 最新={cur}{stale_tag}  发布日={items[-1]['last_release']}  {flag}")

    fresh_items = [i for i in items if i["fresh"]]
    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "today": today.strftime("%Y-%m-%d"),
        "items": items,
        "fresh_count": len(fresh_items),
        "fresh_items": fresh_items,
    }


if __name__ == "__main__":
    import json
    print(json.dumps(fetch_macro_flow(), ensure_ascii=False, indent=2, default=str))
