"""
新闻流报告生成 (Report Flow)
============================
把 macro / etf_constituents / institutional 三条流汇成：
  - 一份机器可读 JSON（reports/news_flow_<date>.json），供 daily_predict 注入上下文
  - 一份人读 HTML（reports/news_flow_<date>.html）

HTML 设计：先结论（刚发布的宏观 / 大机构大变动 / ETF 加权偏空告警），再分三节展开。
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any

_BASE = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(_BASE, "..", "reports")

# 成分股新闻时间窗（与 flows/etf_constituents_flow.py 共用同一配置，避免两处不一致）
try:
    from flow_config import NEWS_WINDOW_DAYS as _NWD  # noqa: E402
except Exception:
    _NWD = 14
NEWS_WINDOW_DAYS_LABEL = str(_NWD)


def _fmt_usd(v: float | None) -> str:
    if v is None:
        return "-"
    if abs(v) >= 1e9:
        return f"${v/1e9:.1f}B"
    if abs(v) >= 1e6:
        return f"${v/1e6:.0f}M"
    return f"${v:,.0f}"


def _h_alert(macro: dict, etfs: list, insts: list) -> str:
    alerts = []
    for m in macro.get("fresh_items", []):
        alerts.append(f"🔥 宏观「{m['label']}」于 {m['last_release']} 发布（数据月 {m['last_data_month']}），"
                      f"最新值 {m['current']}（前值 {m['previous']}）")
    for e in etfs:
        # 置信度「低」时不发偏空告警：样本不足（如仅 1 只成分股 2 条新闻）得出的负分
        # 不具代表性，报出来会误导决策。
        if (e.get("ok") and e.get("weighted_score") is not None
                and e.get("confidence", "高") != "低" and e["weighted_score"] < 0):
            alerts.append(f"⚠️ {e['name']} 成分股消息面偏空（分 {e['weighted_score']}，"
                          f"覆盖 {e.get('covered_constituents')} 只）")
        if e.get("ok") and e.get("sector_score") is not None and e["sector_score"] < 0:
            alerts.append(f"⚠️ {e['name']} 板块消息面偏空（分 {e['sector_score']}，"
                          f"{e.get('sector_news_count', 0)} 条）")
        # 大事件 + 高权重成分股的坏消息，单独提为告警
        for n in _rank_etf_news(e)[:3]:
            if n.get("is_major") or n.get("sentiment") == "bad":
                tag = "🔥大事件" if n.get("is_major") else "⚠️利空"
                alerts.append(f"{tag} {e['name']} 成分股 [{n.get('constituent_weight')}%] "
                              f"{n.get('constituent_name')}: {n.get('title','')[:45]}")
    for ins in insts:
        if not ins.get("ok"):
            continue
        for b in ins.get("big_changes", [])[:3]:
            tag = "增持" if b["delta"] > 0 else "减持"
            rel = (" → 关联 " + ",".join(b["relevance"])) if b.get("relevance") else ""
            alerts.append(f"🐋 {ins['name']} 大变动：{tag} {b['issuer']} {_fmt_usd(abs(b['delta']))}{rel}")
    if not alerts:
        return '<div class="empty">本期无高优先告警</div>'
    return "\n".join(f'<div class="alert-item">{a}</div>' for a in alerts)


def _h_macro(macro: dict) -> str:
    rows = []
    for m in macro.get("items", []):
        cur = m.get("current") or "-"
        prev = m.get("previous") or "-"
        fresh = " 🔥刚发布" if m.get("fresh") else ""
        nxt = m.get("next_release") or "-"
        rows.append(f"""<tr>
          <td>{m['label']}</td><td>{m.get('actual_date') or '-'}</td>
          <td><b>{cur}</b></td><td>{prev}</td>
          <td>{m.get('last_release') or '-'}{fresh}</td>
          <td>{nxt}</td></tr>""")
    return f"""<div class="section-title">📅 美国宏观数据发布追踪 <span class="badge" style="background:#3498db">{macro.get('fresh_count',0)} 刚发布</span></div>
    <table class="data-table"><thead><tr><th>指标</th><th>数据月</th><th>最新值</th><th>前值</th><th>发布日</th><th>下次发布</th></tr></thead>
    <tbody>{''.join(rows)}</tbody></table>"""


def _rank_etf_news(e: dict) -> list[dict]:
    """按「大事件 × 情绪 × 指数权重」给 ETF 成分股新闻排优先级，并去重。"""
    seen: set[str] = set()
    ranked = []
    for n in e.get("news", []):
        t = (n.get("title") or "").strip()
        if not t or t in seen:
            continue
        seen.add(t)
        w = n.get("constituent_weight") or 0
        prio = w
        if n.get("is_major"):
            prio += 100
        if n.get("sentiment") == "bad":
            prio += 50
        elif n.get("sentiment") == "good":
            prio += 10
        ranked.append((prio, n))
    ranked.sort(key=lambda x: -x[0])
    return [n for _, n in ranked]


def _h_etfs(etfs: list) -> str:
    blocks = []
    for e in etfs:
        if not e.get("ok"):
            blocks.append(f'<div class="etf-block"><h3>{e["name"]} ({e["ticker"]})</h3>'
                          f'<div class="empty">⚠️ {e.get("error","")}</div></div>')
            continue
        cons = ", ".join(f"{c['name']}({c['weight']}%)" for c in e.get("top_constituents", [])[:8])
        ranked = _rank_etf_news(e)
        news_html = ""
        for n in ranked[:14]:
            badge = "🔥" if n.get("is_major") else ""
            sent = n.get("sentiment", "")
            sc = "good" if sent == "good" else ("bad" if sent == "bad" else "neutral")
            news_html += f"""<div class="news-line {sc}">{badge}
              <span class="w">[{n.get('constituent_weight')}%]</span>
              <b>{n.get('constituent_name')}</b>: {n.get('title','')[:60]}
              <span class="meta">{n.get('source','')} {n.get('time','')}</span></div>"""
        if not news_html:
            news_html = '<div class="empty">成分股层面无相关新闻</div>'
        sector_html = ""
        for n in (e.get("sector_news") or [])[:6]:
            sent = n.get("sentiment", "")
            sc = "good" if sent == "good" else ("bad" if sent == "bad" else "neutral")
            sector_html += f"""<div class="news-line {sc}">
              <span class="w">[板块]</span>{n.get('title','')[:60]}
              <span class="meta">{n.get('source','')} {n.get('time','')}</span></div>"""
        if sector_html:
            sector_html = f'<div class="sub-h">板块/行业消息面（{e.get('sector_bias')}）</div>' + sector_html
        blocks.append(f"""<div class="etf-block">
          <h3>{e['name']} ({e['ticker']}) · 跟踪{e.get('index_name','')} · 个股加权分 <b>{e.get('weighted_score')}</b>（{e.get('bias')}，置信度 {e.get('confidence','-')}）· 板块分 {e.get('sector_score')}（{e.get('sector_bias','-')}）</h3>
          <div class="cons">权重前8：{cons} · 个股相关新闻 {e.get('news_count',0)} 条（覆盖 {e.get('covered_constituents',0)} 只）· 板块新闻 {e.get('sector_news_count',0)} 条 · 剔除 旧闻{e.get('dropped_stale',0)}/无关{e.get('dropped_irrelevant',0)}/行情复盘{e.get('dropped_market_action',0)} · 同事件合并 {e.get('dup_merged',0)} 条</div>
          {news_html}{sector_html}</div>""")
    return (f'<div class="section-title">🏭 ETF 成分股权重新闻（每日加权 · '
            f'仅统计近 {NEWS_WINDOW_DAYS_LABEL} 天）</div>' + "\n".join(blocks))


def _h_inst(insts: list) -> str:
    blocks = []
    for ins in insts:
        if not ins.get("ok"):
            blocks.append(f'<div class="etf-block"><h3>{ins["name"]}</h3>'
                          f'<div class="empty">⚠️ {ins.get("error","")}</div></div>')
            continue
        def _row(r):
            tag = "add" if r["delta"] > 0 else "cut"
            arrow = "▲增持" if r["delta"] > 0 else "▼减持"
            rel = (" <span class='rel'>→ " + ",".join(r["relevance"]) + "</span>") if r.get("relevance") else ""
            big = " big" if r.get("big") else ""
            return f"""<tr class="{tag}{big}"><td>{r['issuer']}{rel}</td>
              <td>{_fmt_usd(r['value_latest'])}</td><td class="{tag}">{arrow} {_fmt_usd(abs(r['delta']))}</td>
              <td>{r.get('pct_portfolio',0):.2f}%</td></tr>"""
        adds = "".join(_row(r) for r in ins.get("adds", [])[:6]) or '<tr><td colspan="4">无</td></tr>'
        cuts = "".join(_row(r) for r in ins.get("cuts", [])[:6]) or '<tr><td colspan="4">无</td></tr>'
        blocks.append(f"""<div class="etf-block">
          <h3>🐋 {ins['name']} · 报告期 {ins.get('report_date')} · 总持仓 {_fmt_usd(ins.get('total_value'))} · {ins.get('position_count')} 只</h3>
          <div class="two-col">
            <div><h4>增持榜</h4><table class="data-table"><thead><tr><th>标的</th><th>现市值</th><th>变动</th><th>占比</th></tr></thead><tbody>{adds}</tbody></table></div>
            <div><h4>减持榜</h4><table class="data-table"><thead><tr><th>标的</th><th>现市值</th><th>变动</th><th>占比</th></tr></thead><tbody>{cuts}</tbody></table></div>
          </div></div>""")
    return f'<div class="section-title">🐋 大型机构 13F 持仓变动</div>' + "\n".join(blocks)


def _h_cls(cls: dict) -> str:
    """财联社电报流 HTML 段。

    分「强关联 / 弱关联」两档展示，因为只有强关联进情绪分：
    强关联 = 成分股精确命中 / 指数点名 / 该 ETF 的直接行业主题；
    弱关联 = 宽基沾边、海外映射、大宗同向传导，只作背景参考。
    """
    etfs = cls.get("etfs", []) or []
    if not etfs and not cls.get("global_news"):
        return ('<div class="section-title">⚡ 财联社快讯（7×24 分钟级）</div>'
                '<div class="etf-block"><div class="empty">本次未取到快讯</div></div>')

    def _lv(n: dict) -> str:
        lv = n.get("level", "C")
        return f'<span class="lv lv-{lv}">{lv}</span>'

    blocks = []
    for e in etfs:
        if not e.get("strong_count") and not e.get("weak_count"):
            continue
        rows = ""
        for n in (e.get("strong_news") or [])[:12]:
            sent = n.get("sentiment", "")
            sc = "good" if sent == "good" else ("bad" if sent == "bad" else "neutral")
            mkt = ""
            if n.get("sentiment_src") == "market" and n.get("stock_changes"):
                mkt = f' <span class="w">市:{",".join(n["stock_changes"][:3])}</span>'
            sh = ' <span class="w">联动</span>' if n.get("shared") else ""
            rows += (f'<div class="news-line {sc}">{_lv(n)}'
                     f'<b>{n.get("via","")}</b>:{n.get("match_detail","")} · '
                     f'{n.get("title","")[:66]} '
                     f'<span class="meta">{n.get("time","")}{mkt}{sh}</span></div>')
        if not rows:
            rows = '<div class="empty">强关联无（仅弱关联背景，不进情绪分）</div>'
        weak = ""
        for n in (e.get("weak_news") or [])[:5]:
            weak += (f'<div class="news-line neutral">{_lv(n)}'
                     f'[弱]{n.get("match_detail","")} · {n.get("title","")[:60]} '
                     f'<span class="meta">{n.get("time","")}</span></div>')
        if weak:
            weak = '<div class="sub-h">弱关联背景（不进情绪分）</div>' + weak
        blocks.append(f"""<div class="etf-block">
          <h3>{e['name']} ({e['ticker']}) · 强 <b>{e.get('strong_count',0)}</b> / 弱 {e.get('weak_count',0)} ·
          加权分 <b>{e.get('weighted_score',0)}</b>（{e.get('bias','-')}，置信度 {e.get('confidence','-')}）</h3>
          {rows}{weak}</div>""")

    # 跨板块联动事件（共享事件只列一次）
    linked = cls.get("linked_events") or []
    if linked:
        lr = ""
        for ev in linked[:12]:
            tk = " ".join(ev.get("tickers", []))
            lr += (f'<div class="news-line neutral">{_lv(ev)}'
                   f'<b>联动</b> {ev.get("title","")[:60]} '
                   f'<span class="meta">{ev.get("time","")} → {tk}</span></div>')
        blocks.append(f'<div class="etf-block"><h3>🔗 跨板块联动事件'
                      f'（同一事件驱动多只ETF，加权已打折）</h3>{lr}</div>')

    # 未归属到任何 ETF 的 B 级以上快讯 = 当日市场环境
    g = ""
    for n in (cls.get("global_news") or [])[:12]:
        g += (f'<div class="news-line neutral">{_lv(n)}{n.get("title","")[:70]} '
              f'<span class="meta">{n.get("time","")} · {n.get("subject","")}</span></div>')
    if g:
        blocks.append(f'<div class="etf-block"><h3>🌐 全局快讯（未归属特定 ETF，B 级以上）</h3>{g}</div>')

    head = (f'<div class="section-title">⚡ 财联社快讯（7×24 分钟级 · 回溯 {cls.get("hours_back","-")} 小时）</div>'
            f'<div class="cls-meta">原始 {cls.get("raw_count",0)} 条 → 剔除行情复盘 {cls.get("noise_filtered",0)} 条'
            f' → 同事件去重后 {cls.get("after_dedup",0)} 条 → 命中持仓 ETF {cls.get("matched",0)} 条 / '
            f'未归属 {cls.get("unmatched",0)} 条 · 耗时 {cls.get("elapsed_sec","-")}s<br>'
            f'⚠️ 情绪分只由<b>强关联</b>驱动（成分股/指数点名/直接行业），弱关联仅作背景：'
            f'海外主体、大宗同向传导、宽基沾边一律降级为弱，避免「美油跌 2% 算煤炭利好」这类假信号。</div>')
    return head + "\n".join(blocks)


def build_report(macro: dict, etfs: dict, insts: dict, cls: dict | None = None) -> tuple[str, str]:
    now = datetime.now()
    date_str = now.strftime("%Y-%m-%d")
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    macro_items = macro.get("items", [])
    etf_list = etfs.get("etfs", [])
    inst_list = insts.get("institutions", [])
    cls = cls or {"etfs": [], "global_news": []}

    html = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>独立新闻流 - {date_str}</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box;}}
body{{font-family:-apple-system,"Microsoft YaHei",sans-serif;background:#f5f7fa;color:#222;line-height:1.6;}}
.header{{background:linear-gradient(135deg,#16213e,#0f3460);color:#fff;padding:32px 24px;}}
.header h1{{font-size:1.8em;}}
.container{{max-width:1180px;margin:0 auto;padding:20px 16px;}}
.section-title{{font-size:1.3em;font-weight:700;margin:28px 0 12px;padding-bottom:6px;border-bottom:3px solid #0f3460;}}
.alert-box{{background:#fff;border-left:5px solid #e74c3c;border-radius:8px;padding:16px;margin:16px 0;box-shadow:0 1px 4px rgba(0,0,0,.06);}}
.alert-item{{padding:6px 0;border-bottom:1px dashed #eee;font-size:.95em;}}
.data-table{{width:100%;border-collapse:collapse;background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,.06);margin:8px 0;}}
.data-table th,.data-table td{{padding:8px 10px;text-align:left;font-size:.9em;border-bottom:1px solid #eee;}}
.data-table th{{background:#16213e;color:#fff;}}
.data-table tr.add td.add{{color:#1e8449;font-weight:600;}}
.data-table tr.cut td.cut{{color:#c0392b;font-weight:600;}}
.data-table tr.big{{background:#fff5f5;}}
.etf-block{{background:#fff;border-radius:8px;padding:14px 16px;margin:10px 0;box-shadow:0 1px 4px rgba(0,0,0,.06);}}
.etf-block h3{{font-size:1.05em;margin-bottom:8px;}}
.etf-block h4{{font-size:.95em;margin:8px 0 4px;color:#555;}}
.cons{{font-size:.82em;color:#666;margin-bottom:8px;}}
.sub-h{{font-size:.82em;color:#0f3460;font-weight:600;margin:8px 0 2px;}}
.cls-meta{{font-size:.82em;color:#666;background:#fff;border-radius:8px;padding:12px 14px;margin:8px 0;line-height:1.7;box-shadow:0 1px 4px rgba(0,0,0,.06);}}
.lv{{display:inline-block;padding:0 5px;border-radius:3px;font-size:.78em;font-weight:700;color:#fff;margin-right:4px;}}
.lv-A{{background:#c0392b;}}.lv-B{{background:#e67e22;}}.lv-C{{background:#95a5a6;}}
.news-line{{padding:5px 0;border-bottom:1px dashed #eee;font-size:.88em;}}
.news-line .w{{display:inline-block;background:#eef;color:#2471a3;padding:0 6px;border-radius:4px;font-size:.8em;margin-right:4px;}}
.news-line.good{{border-left:3px solid #2ecc71;padding-left:6px;}}
.news-line.bad{{border-left:3px solid #e74c3c;padding-left:6px;}}
.news-line.neutral{{border-left:3px solid #ddd;padding-left:6px;}}
.news-line .meta{{color:#999;font-size:.8em;}}
.rel{{color:#c0392b;font-weight:600;}}
.two-col{{display:grid;grid-template-columns:1fr 1fr;gap:12px;}}
.empty{{color:#aaa;padding:12px;text-align:center;}}
.footer{{text-align:center;padding:30px;color:#aaa;font-size:.82em;}}
</style></head><body>
<div class="header"><h1>📡 独立新闻流 · 每日</h1>
<div class="subtitle">财联社快讯 · 宏观发布追踪 · ETF成分股权重新闻 · 大型机构13F持仓变动 | {now.strftime('%Y-%m-%d %H:%M')}</div></div>
<div class="container">
<div class="section-title">⚡ 优先告警</div>
<div class="alert-box">{_h_alert(macro, etf_list, inst_list)}</div>
{_h_cls(cls)}
{_h_macro(macro)}
{_h_etfs(etf_list)}
{_h_inst(inst_list)}
</div>
<div class="footer">数据：财联社电报 + BLS/Federal Reserve 日历 + akshare + SEC EDGAR 13F | 仅供参考，不构成投资建议</div>
</body></html>"""

    html_path = os.path.join(OUTPUT_DIR, f"news_flow_{date_str}.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)

    json_path = os.path.join(OUTPUT_DIR, f"news_flow_{date_str}.json")
    combined = {
        "date": date_str,
        "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "cls_telegraph": cls,
        "macro": macro,
        "etf_constituents": etfs,
        "institutions": insts,
    }
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(combined, f, ensure_ascii=False, indent=2, default=str)

    return html_path, json_path


def latest_json_path() -> str | None:
    """返回 reports/ 下最新的 news_flow_*.json 路径（供 daily_predict 注入）。"""
    if not os.path.isdir(OUTPUT_DIR):
        return None
    files = sorted([f for f in os.listdir(OUTPUT_DIR) if f.startswith("news_flow_") and f.endswith(".json")])
    return os.path.join(OUTPUT_DIR, files[-1]) if files else None


def _news_file_age_note(path: str, data: dict, stale_hours: float = 18.0) -> str:
    """返回「新闻流文件多旧」的提示行；足够新时返回空串。

    只做标注，不参与内容取舍、不过滤任何条目。
    """
    ts = None
    gen = str(data.get("generated_at") or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            ts = datetime.strptime(gen, fmt)
            break
        except (ValueError, TypeError):
            continue
    if ts is None:
        try:
            ts = datetime.fromtimestamp(os.path.getmtime(path))
        except OSError:
            return ""
    age_h = (datetime.now() - ts).total_seconds() / 3600.0
    if age_h < stale_hours:
        return f"- ⏱新闻流生成于 {ts:%Y-%m-%d %H:%M}（{age_h:.1f}h 前）"
    return (f"- ⚠️新闻流已过期：生成于 {ts:%Y-%m-%d %H:%M}，距今 {age_h:.1f}h（阈值 {stale_hours:.0f}h）。"
            f"以下快讯可能缺最近一个交易时段的盘中信息，不要当作最新盘面信号；"
            f"当晚新闻流很可能未成功运行，需人工确认。")


def load_latest_context(max_chars: int = 6000, source_path: str | None = None) -> str:
    """读取最新 news_flow JSON，压成一段 Markdown 上下文，供 daily_predict 注入分析。

    取舍原则（不是简单取前 N 条）：
      - 财联社快讯：放最前面。它是唯一「当天」的分钟级信号，且从尾部截断，
        排在后面就等于自废武功。只取强关联，弱关联最多列一句名字。
      - 宏观：刚发布的（fresh）优先，并标注「数据源滞后」避免被旧数字误导；
      - ETF 成分股：按「是否大事件 × 情绪正负 × 指数权重」算优先级，
        权重高的成分股出坏消息/大事件时必须浮到最前面；
      - 机构 13F：只取大变动，并带上「关联你的哪只 ETF」。
    """
    p = source_path or latest_json_path()
    if not p:
        return ""
    try:
        data = json.loads(open(p, encoding="utf-8").read())
    except Exception:
        return ""

    # Stale/future reports are unavailable evidence, not fresh facts plus a warning.
    from prediction_research.news_handoff import timestamp
    from datetime import timezone
    generated = timestamp(data.get("generated_at"))
    if generated is None or not 0 <= (datetime.now(timezone.utc) - generated).total_seconds() <= 18 * 3600:
        return "【独立新闻流不可用：生成时间缺失、在未来或超过 18 小时；不得据此判断消息中性。】"

    lines = ["【独立新闻流摘要】"]

    # ── 文件时效自检（只标注，不过滤）──
    # latest_json_path() 按文件名取最新、不做时效校验：若当晚新闻流跑失败，
    # 会静默注入上一份（可能缺整个交易日盘中）。把「这份有多旧」写进上下文，
    # 避免模型把过期窗口当当天信号。
    age_note = _news_file_age_note(p, data)
    if age_note:
        lines.append(age_note)

    # ── 财联社快讯：放最前面 ──
    # 它是唯一「当天」的分钟级信号，而 max_chars 是从尾部截断的，
    # 排在宏观/ETF/13F 之后就等于自废武功（那三条常常又长又旧）。
    cls = data.get("cls_telegraph") or {}
    cls_etfs = [e for e in (cls.get("etfs") or []) if e.get("strong_count")]
    if cls_etfs:
        lines.append(f"- ⚡财联社快讯（近 {cls.get('hours_back','-')}h：原始 {cls.get('raw_count',0)} 条"
                     f" → 去重 {cls.get('after_dedup',0)} → 命中持仓 {cls.get('matched',0)} 条。"
                     f"情绪分只由强关联驱动：成分股精确命中/指数点名/直接行业；海外与大宗传导已降级为弱、不进分）")
        for e in cls_etfs[:5]:
            lines.append(f"  · {e['name']}({e['ticker']}) 强{e['strong_count']}/弱{e.get('weak_count',0)} "
                         f"加权分 {e['weighted_score']:.1f} → {e['bias']}［{e.get('confidence','-')}］")
            for n in (e.get("strong_news") or [])[:3]:
                src = "市" if n.get("sentiment_src") == "market" else "词"
                sh = "·联动" if n.get("shared") else ""
                mkt = (" 市:" + ",".join(n["stock_changes"][:3])) if n.get("sentiment_src") == "market" and n.get("stock_changes") else ""
                lines.append(f"    - [{n.get('level','C')}/{src}{sh}] {n.get('time','')} "
                             f"{n.get('via','')}:{n.get('match_detail','')} {n.get('title','')[:50]}{mkt}")
    no_signal = [e for e in (cls.get("etfs") or []) if not e.get("strong_count") and e.get("weak_count")]
    if no_signal:
        lines.append("  · 仅弱关联、无强信号（不给方向）："
                     + "、".join(f"{e['name']}({e['ticker']})" for e in no_signal[:6]))
    linked = (cls.get("linked_events") or [])
    if linked:
        lines.append(f"  · 🔗 跨板块联动事件 {len(linked)} 条（同一事件驱动多只ETF，置信度已打折）：")
        for ev in linked[:6]:
            tk = "/".join(ev.get("tickers", []))
            lines.append(f"    - [{ev.get('level','C')}] {ev.get('time','')} "
                         f"{ev.get('title','')[:46]} → {tk}")
    g = (cls.get("global_news") or [])[:4]
    if g:
        lines.append("  · 全局（未归属特定 ETF，B 级以上）："
                     + "；".join(f"[{n.get('level','B')}] {n.get('title','')[:45]}" for n in g))

    # ── 宏观：刚发布的优先，滞后源显式标注 ──
    macro = data.get("macro", {})
    for m in macro.get("fresh_items", []):
        stale = "（⚠️数据源滞后，仅供参考）" if m.get("stale") else ""
        lines.append(f"- 宏观 {m['label']} 于 {m['last_release']} 发布："
                     f"最新 {m['current']}（前值 {m['previous']}）{stale}")
    # 下次发布按 (发布日, 数据月) 合并——CPI同比/环比/核心CPI 同日发布，
    # 非农就业/失业率 也同日发布，分开报是冗余。
    groups: dict[tuple[str, str], list[str]] = {}
    order: list[tuple[str, str]] = []
    for e in macro.get("items", []):
        nr = e.get("next_release")
        if not nr:
            continue
        key = (nr, str(e.get("next_data_month") or ""))
        if key not in groups:
            groups[key] = []
            order.append(key)
        lab = e["label"]
        if lab not in groups[key]:
            groups[key].append(lab)
    for key in order:
        lines.append(f"- {'/'.join(groups[key])} 下次发布：{key[0]}（数据月 {key[1]}）")

    # ── ETF 成分股：按优先级挑，而不是取前几条 ──
    for e in data.get("etf_constituents", {}).get("etfs", []):
        if not (e.get("ok") and e.get("weighted_score") is not None):
            continue
        lines.append(f"- {e['name']}({e['ticker']}) 成分股加权消息面：{e['bias']}"
                     f"（分 {e['weighted_score']}，覆盖 {e.get('covered_constituents')} 只成分股，"
                     f"置信度 {e.get('confidence','-')}）")
        if e.get("sector_score") is not None:
            lines.append(f"    · 板块消息面：{e.get('sector_bias')}"
                         f"（分 {e['sector_score']}，{e.get('sector_news_count',0)} 条）")
        # 置信度低时，个股直接新闻太少，并列板块新闻标题，避免「看起来没消息」
        if e.get("confidence") == "低":
            for n in (e.get("sector_news") or [])[:3]:
                lines.append(f"    · [板块] {n.get('title','')[:55]}")
        news = e.get("news", [])
        if not news:
            continue
        seen: set[str] = set()
        ranked = []
        for n in news:
            t = (n.get("title") or "").strip()
            if not t or t in seen:
                continue
            seen.add(t)
            w = n.get("constituent_weight") or 0
            prio = w
            if n.get("is_major"):
                prio += 100          # 大事件一票置顶
            if n.get("sentiment") == "bad":
                prio += 50           # 坏消息次优先
            elif n.get("sentiment") == "good":
                prio += 10
            ranked.append((prio, n))
        ranked.sort(key=lambda x: -x[0])
        for prio, n in ranked[:5]:
            tag = "🔥大事件" if n.get("is_major") else ("⚠️利空" if n.get("sentiment") == "bad" else "")
            title = n.get("title", "")[:55]
            # 带上发布日期：窗口为 14 天，若不标日期，模型会把十几天前的消息当当天消息用
            d = str(n.get("time", ""))[:10]
            lines.append(f"    · [{n.get('constituent_weight')}%] {d} "
                         f"{n.get('constituent_name')}: {title} {tag}")
            # 东财列表页的 title 常是多条标题拼接（如「实控人遭留置，富祥股份大跌…」），
            # 主语会错位。大事件额外带一句正文，避免分析师误读成别的公司出事。
            if n.get("is_major") and n.get("content"):
                excerpt = " ".join(str(n["content"]).split())[:110]
                lines.append(f"      └ {excerpt}")

    # ── 机构 13F 大变动 ──
    for ins in data.get("institutions", {}).get("institutions", []):
        if not ins.get("ok"):
            continue
        for b in ins.get("big_changes", [])[:5]:
            tag = "增持" if b["delta"] > 0 else "减持"
            rel = (" 关联 " + ",".join(b["relevance"])) if b.get("relevance") else ""
            lines.append(f"- 🐋 {ins['name']} 大变动：{tag} {b['issuer']} "
                         f"{_fmt_usd(abs(b['delta']))}{rel}")

    txt = "\n".join(lines)
    return txt[:max_chars]
