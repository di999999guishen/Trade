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
    _NWD = 7
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
        if e.get("ok") and e.get("weighted_score") is not None and e["weighted_score"] < 0:
            alerts.append(f"⚠️ {e['name']} 加权消息面偏空（分 {e['weighted_score']}）")
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
            news_html = '<div class="empty">成分股层面无新新闻</div>'
        blocks.append(f"""<div class="etf-block">
          <h3>{e['name']} ({e['ticker']}) · 跟踪{e.get('index_name','')} · 加权分 <b>{e.get('weighted_score')}</b>（{e.get('bias')}）</h3>
          <div class="cons">权重前8：{cons} · 近{NEWS_WINDOW_DAYS_LABEL}日新闻 {e.get('news_count',0)} 条（过滤旧闻 {e.get('dropped_stale',0)} 条）</div>
          {news_html}</div>""")
    return f'<div class="section-title">🏭 ETF 成分股权重新闻（每日加权）</div>' + "\n".join(blocks)


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


def build_report(macro: dict, etfs: dict, insts: dict) -> tuple[str, str]:
    now = datetime.now()
    date_str = now.strftime("%Y-%m-%d")
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    macro_items = macro.get("items", [])
    etf_list = etfs.get("etfs", [])
    inst_list = insts.get("institutions", [])

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
<div class="subtitle">宏观发布追踪 · ETF成分股权重新闻 · 大型机构13F持仓变动 | {now.strftime('%Y-%m-%d %H:%M')}</div></div>
<div class="container">
<div class="section-title">⚡ 优先告警</div>
<div class="alert-box">{_h_alert(macro, etf_list, inst_list)}</div>
{_h_macro(macro)}
{_h_etfs(etf_list)}
{_h_inst(inst_list)}
</div>
<div class="footer">数据：BLS/Federal Reserve 日历 + akshare + SEC EDGAR 13F | 仅供参考，不构成投资建议</div>
</body></html>"""

    html_path = os.path.join(OUTPUT_DIR, f"news_flow_{date_str}.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)

    json_path = os.path.join(OUTPUT_DIR, f"news_flow_{date_str}.json")
    combined = {
        "date": date_str,
        "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
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


def load_latest_context(max_chars: int = 4000) -> str:
    """读取最新 news_flow JSON，压成一段 Markdown 上下文，供 daily_predict 注入分析。

    取舍原则（不是简单取前 N 条）：
      - 宏观：刚发布的（fresh）优先，并标注「数据源滞后」避免被旧数字误导；
      - ETF 成分股：按「是否大事件 × 情绪正负 × 指数权重」算优先级，
        权重高的成分股出坏消息/大事件时必须浮到最前面；
      - 机构 13F：只取大变动，并带上「关联你的哪只 ETF」。
    """
    p = latest_json_path()
    if not p:
        return ""
    try:
        data = json.loads(open(p, encoding="utf-8").read())
    except Exception:
        return ""

    lines = ["【独立新闻流摘要】"]

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
                     f"（分 {e['weighted_score']}，覆盖 {e.get('covered_constituents')} 只成分股）")
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
            lines.append(f"    · [{n.get('constituent_weight')}%] "
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
