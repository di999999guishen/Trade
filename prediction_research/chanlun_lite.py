"""缠论信号：高保真引擎（czsc）+ 轻量回退（lite），离线、零算力、面向国内 ETF 日线。

背景
----
根因分析（2026-10-21）指出：当前 top10 被多智体 LLM 判为「增持」的比例极低
（全量 Overweight 仅 5.6%、Buy 0 次），且框架从设计上回避方向性增持结论
（Overweight/Buy 的 direction 标成 unclassified，不计入方向命中率）。

本模块把「增持判定权」拿回自己手里，提供可审计、可度量的缠论买卖点信号，用于：
  - C（规则闸门）：LLM 判 Hold/Underweight 时，若缠论给买点 + 主力净流入为正 +
    非追高，则升级 Overweight(规则闸门)。
  - D（开源对照）：在 top10 历史上回测缠论买点的前向胜率，与 LLM 增持、规则闸门
    三方对照，给出「是否真的比你现在更准」的实证。

引擎选择（2026-10-21 升级为高保真）
-----------------------------------
优先使用 **czsc**（纯 Python 缠论引擎，完整实现 笔 / 线段 / 中枢 / 背驰 / 买卖点 /
多级别，pip 可装、CPU 运行、面向 A股/ETF）。PyPI 上的 `chanlun`（0xSchnappi）同名包
已改为 Rust 核心、仅提供 cp312/cp39 预编译 wheel，在 Python 3.13 上无法安装，故以
czsc 作为等效高保真后端。

若 czsc 不可用（未安装 / 运行异常），自动回退到内置的 **lite** 近似实现
（分型→笔→反转），保证流水线不中断。二者通过同一个 `signal()` 接口暴露，下游
（规则闸门、D harness）无需任何改动。

signal() 返回契约（保持稳定，下游依赖）：
    {"signal": "buy" | "sell" | "neutral", "reason", "last_pen_direction",
     "last_fractal_type", "bottom_fractal_high", "top_fractal_low", "bars",
     "engine": "czsc" | "lite", 以及诊断字段 bi_count / zs_count}
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable


@dataclass(frozen=True)
class Bar:
    trading_date: Any
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


# --------------------------------------------------------------------------- #
# 引擎探测
# --------------------------------------------------------------------------- #
try:
    import czsc as _czsc  # 高保真后端（纯 Python，含中枢/背驰/买卖点）
    _HAS_CZSC = True
except Exception:  # pragma: no cover - 依赖未装时走 lite 回退
    _czsc = None
    _HAS_CZSC = False

ENGINE = "czsc" if _HAS_CZSC else "lite"


# --------------------------------------------------------------------------- #
# 通用归一化
# --------------------------------------------------------------------------- #
def _as_bars(rows: Iterable[Any]) -> list[Bar]:
    """把 list[dict] 或 list[Bar-like] 归一化成内部 Bar 列表（按原顺序，不排序）。"""
    out: list[Bar] = []
    for row in rows:
        if hasattr(row, "high") and hasattr(row, "low") and hasattr(row, "close"):
            out.append(Bar(
                trading_date=getattr(row, "trading_date", None),
                open=float(row.open), high=float(row.high),
                low=float(row.low), close=float(row.close),
                volume=float(getattr(row, "volume", 0.0) or 0.0),
            ))
        else:
            out.append(Bar(
                trading_date=row.get("date"),
                open=float(row["open"]), high=float(row["high"]),
                low=float(row["low"]), close=float(row["close"]),
                volume=float(row.get("volume", 0.0) or 0.0),
            ))
    return out


def _iter_ohlcv(bars: Iterable[Any]):
    """产出 (dt_str, open, high, low, close, vol) 元组，兼容 dict 与 Bar-like。"""
    for row in bars:
        if hasattr(row, "high"):
            yield (getattr(row, "trading_date", None), row.open, row.high,
                   row.low, row.close, getattr(row, "volume", 0.0) or 0.0)
        else:
            yield (row.get("date"), float(row["open"]), float(row["high"]),
                   float(row["low"]), float(row["close"]),
                   float(row.get("volume", 0.0) or 0.0))


def _to_dt(value: Any):
    if value is None:
        return None
    try:
        import pandas as pd
        return pd.to_datetime(value)
    except Exception:  # pragma: no cover
        return None


# --------------------------------------------------------------------------- #
# 轻量回退实现（分型 → 笔 → 反转）
# --------------------------------------------------------------------------- #
def fractals(bars: list[Bar]) -> tuple[list[int], list[int]]:
    """返回顶分型 / 底分型的 K 线下标列表。"""
    n = len(bars)
    tops: list[int] = []
    bottoms: list[int] = []
    if n < 3:
        return tops, bottoms
    highs = [b.high for b in bars]
    lows = [b.low for b in bars]
    for i in range(1, n - 1):
        h, l = highs[i], lows[i]
        if h > highs[i - 1] and h > highs[i + 1] and l > lows[i - 1] and l > lows[i + 1]:
            tops.append(i)
        if l < lows[i - 1] and l < lows[i + 1] and h < highs[i - 1] and h < highs[i + 1]:
            bottoms.append(i)
    return tops, bottoms


def pens(bars: list[Bar]) -> list[tuple[int, int, str, float]]:
    """由分型贪心合并出「笔」。每笔 = (起点下标, 终点下标, 方向, 端点极值)。"""
    tops, bottoms = fractals(bars)
    frags = sorted([(i, "top") for i in tops] + [(i, "bottom") for i in bottoms])
    if not frags:
        return []
    highs = [b.high for b in bars]
    lows = [b.low for b in bars]
    result: list[tuple[int, int, str, float]] = []
    last_idx, last_type = frags[0]
    for i, typ in frags[1:]:
        if typ == last_type:
            if typ == "top" and highs[i] > highs[last_idx]:
                last_idx = i
            elif typ == "bottom" and lows[i] < lows[last_idx]:
                last_idx = i
            continue
        direction = "up" if typ == "top" else "down"
        extreme = highs[i] if typ == "top" else lows[i]
        result.append((last_idx, i, direction, extreme))
        last_idx, last_type = i, typ
    return result


def _neutral(bars: int, reason: str, engine: str = ENGINE, **extra) -> dict:
    return {"signal": "neutral", "reason": reason, "last_pen_direction": None,
            "last_fractal_type": None, "bottom_fractal_high": None,
            "top_fractal_low": None, "bars": bars, "engine": engine, **extra}


def _signal_lite(bars: Iterable[Any]) -> dict:
    """lite 回退：最后一条笔为下行笔且末端反转向上 → buy；上行笔末端反转向下 → sell。"""
    bl = _as_bars(bars)
    if len(bl) < 5:
        return _neutral(len(bl), "too_few_bars")
    highs = [b.high for b in bl]
    lows = [b.low for b in bl]
    closes = [b.close for b in bl]
    ps = pens(bl)
    if len(ps) < 2:
        return _neutral(len(bl), "too_few_pens", last_pen_direction=ps[-1][2] if ps else None)
    last = ps[-1]
    last_dir = last[2]
    last_fractal_idx = last[1]
    last_fractal_type = "top" if last_dir == "up" else "bottom"
    bottom_high = None
    top_low = None
    if last_dir == "down":
        bottom_high = highs[last[0]]
        bottom_fractal_high = highs[last_fractal_idx]
        reversal = closes[-1] > bottom_fractal_high
        if reversal:
            return {"signal": "buy", "reason": "down_pen_reversal_up",
                    "last_pen_direction": "down", "last_fractal_type": "bottom",
                    "bottom_fractal_high": round(bottom_fractal_high, 4),
                    "top_fractal_low": None, "bars": len(bl), "engine": "lite"}
    else:
        top_low = lows[last_fractal_idx]
        reversal = closes[-1] < top_low
        if reversal:
            return {"signal": "sell", "reason": "up_pen_reversal_down",
                    "last_pen_direction": "up", "last_fractal_type": "top",
                    "bottom_fractal_high": None,
                    "top_fractal_low": round(top_low, 4), "bars": len(bl), "engine": "lite"}
    return {"signal": "neutral", "reason": "no_reversal_confirmation",
            "last_pen_direction": last_dir, "last_fractal_type": last_fractal_type,
            "bottom_fractal_high": round(bottom_high, 4) if bottom_high is not None else None,
            "top_fractal_low": round(top_low, 4) if top_low is not None else None,
            "bars": len(bl), "engine": "lite"}


# --------------------------------------------------------------------------- #
# 高保真实现（czsc：笔 / 中枢 / 背驰 / 一类·三类买卖点）
# --------------------------------------------------------------------------- #
def _signal_czsc(bars: Iterable[Any]) -> dict:
    rows = [r for r in _iter_ohlcv(bars)]
    if len(rows) < 10:
        return _neutral(len(rows), "too_few_bars_czsc", engine="czsc")
    raw = []
    for i, (d, o, h, l, c, v) in enumerate(rows):
        dt = _to_dt(d)
        if dt is None:
            dt = _to_dt(f"2000-01-{i % 28 + 1:02d}")  # 不应发生；兜底保证 dt 非空
        raw.append(_czsc.RawBar(
            id=i, symbol="ETF", dt=dt, open=float(o), close=float(c),
            high=float(h), low=float(l), vol=float(v), amount=0.0,
            freq=_czsc.Freq.D,
        ))
    c = _czsc.CZSC(raw)
    bis = c.bi_list
    if len(bis) < 2:
        return _neutral(len(raw), "too_few_bi_czsc", engine="czsc")

    last = bis[-1]
    last_up = last.direction.value == "向上"
    last_dir = "up" if last_up else "down"

    # 同方向前一笔（背驰比较用）
    prev_same = None
    for bi in reversed(bis[:-1]):
        if (bi.direction.value == "向上") == last_up:
            prev_same = bi
            break

    sig: str = "neutral"
    reason: str = "no_signal"

    # 一类买卖点：末笔力度 < 同方向前一笔力度（背驰衰竭）
    if prev_same is not None and last.power_price < prev_same.power_price:
        sig = "buy" if last_dir == "down" else "sell"
        reason = "type1_buy_divergence" if last_dir == "down" else "type1_sell_divergence"

    # 三类买卖点：中枢突破（末笔方向突破中枢极值，且回踩未破坏中枢）
    zs = c.zs_list
    if zs:
        z = zs[-1]
        pullback = bis[-2] if len(bis) >= 2 else None
        if last_dir == "up" and pullback is not None:
            if last.high > z.gg and pullback.low >= z.zd:
                sig, reason = "buy", "type3_buy_breakout"
        elif last_dir == "down" and pullback is not None:
            if last.low < z.dd and pullback.high <= z.zg:
                sig, reason = "sell", "type3_sell_breakdown"

    bottom_high = round(last.high, 4) if last_dir == "down" else None
    top_low = round(last.low, 4) if last_dir == "up" else None
    return {"signal": sig, "reason": reason, "last_pen_direction": last_dir,
            "last_fractal_type": ("top" if last_dir == "up" else "bottom"),
            "bottom_fractal_high": bottom_high, "top_fractal_low": top_low,
            "bars": len(raw), "engine": "czsc",
            "bi_count": len(bis), "zs_count": len(zs)}


def signal(bars: Iterable[Any]) -> dict:
    """对截至最后一根 K 线的状态给出方向性信号。

    优先走 czsc 高保真引擎；不可用或异常时回退 lite。返回契约见模块 docstring。
    """
    bl = _as_bars(bars)
    if len(bl) < 5:
        return _neutral(len(bl), "too_few_bars")
    if _HAS_CZSC:
        try:
            return _signal_czsc(bars)
        except Exception:  # pragma: no cover - 任何 czsc 异常都回退，保证健壮
            pass
    return _signal_lite(bars)


if __name__ == "__main__":
    import sys

    def make(close_seq):
        out = []
        for k, c in enumerate(close_seq):
            o = close_seq[k - 1] if k else c
            h = max(o, c) * 1.01
            l = min(o, c) * 0.99
            out.append({"date": f"2026-01-{k + 1:02d}", "open": o, "high": h,
                        "low": l, "close": c, "volume": 1000})
        return out

    down_then_up = make([10, 9.5, 9.0, 8.6, 8.4, 8.5, 8.9, 9.3, 9.8, 10.2])
    up_then_down = make([10, 10.5, 11, 11.4, 11.5, 11.3, 10.9, 10.4, 9.9, 9.4])
    print(f"engine = {ENGINE}")
    print("down_then_up ->", signal(down_then_up))
    print("up_then_down ->", signal(up_then_down))
