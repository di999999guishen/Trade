"""
个股大事件新闻流聚合器 - HTML报告生成
"""
import os
from datetime import datetime
from typing import Any


def generate_report(all_news: dict, major_events: list, normal_news: list,
                    output_dir: str = "reports") -> str:
    """生成HTML报告并保存"""
    os.makedirs(output_dir, exist_ok=True)

    now = datetime.now()
    report_date = now.strftime("%Y-%m-%d")
    report_time = now.strftime("%Y-%m-%d %H:%M:%S")

    # 统计
    total = sum(len(v) for v in all_news.values())
    a_count = len(all_news.get("A股", []))
    hk_count = len(all_news.get("港股", []))
    us_count = len(all_news.get("美股", []))

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>个股大事件新闻流 - {report_date}</title>
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", sans-serif; background: #f5f7fa; color: #333; line-height:1.6; }}
.header {{ background: linear-gradient(135deg, #1a1a2e 0%, #16213e 50%, #0f3460 100%); color: white; padding: 40px 24px; text-align: center; }}
.header h1 {{ font-size: 2em; margin-bottom: 8px; }}
.header .subtitle {{ opacity: 0.85; font-size: 0.95em; }}
.container {{ max-width: 1200px; margin: 0 auto; padding: 24px 16px; }}
.stats {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 16px; margin-bottom: 32px; }}
.stat-card {{ background: white; border-radius: 12px; padding: 20px; text-align: center; box-shadow: 0 2px 8px rgba(0,0,0,0.06); }}
.stat-card .number {{ font-size: 2em; font-weight: 700; }}
.stat-card .label {{ color: #888; font-size: 0.85em; margin-top: 4px; }}
.stat-card.major .number {{ color: #e74c3c; }}
.stat-card.ashare .number {{ color: #e74c3c; }}
.stat-card.hk .number {{ color: #2ecc71; }}
.stat-card.us .number {{ color: #3498db; }}

.section-title {{ font-size: 1.4em; font-weight: 700; margin: 32px 0 16px; padding-bottom: 8px; border-bottom: 3px solid #e74c3c; display: flex; align-items: center; gap: 8px; }}
.section-title .badge {{ background: #e74c3c; color: white; font-size: 0.65em; padding: 3px 10px; border-radius: 12px; }}

.news-card {{ background: white; border-radius: 12px; padding: 20px 24px; margin-bottom: 12px; box-shadow: 0 1px 4px rgba(0,0,0,0.06); border-left: 4px solid #e0e0e0; transition: transform 0.2s; }}
.news-card:hover {{ transform: translateX(4px); box-shadow: 0 4px 12px rgba(0,0,0,0.1); }}
.news-card.major {{ border-left-color: #e74c3c; background: #fff5f5; }}
.news-card .stock-tag {{ display: inline-block; padding: 2px 10px; border-radius: 4px; font-size: 0.8em; font-weight: 600; margin-right: 8px; }}
.news-card .stock-tag.ashare {{ background: #fde8e8; color: #c0392b; }}
.news-card .stock-tag.hk {{ background: #e8f8ef; color: #1e8449; }}
.news-card .stock-tag.us {{ background: #e8f0fe; color: #2471a3; }}
.news-card .event-badge {{ display: inline-block; background: #e74c3c; color: white; font-size: 0.75em; padding: 2px 8px; border-radius: 4px; }}
.news-card h3 {{ font-size: 1.05em; margin: 8px 0; line-height: 1.5; }}
.news-card .meta {{ color: #999; font-size: 0.85em; display: flex; gap: 16px; flex-wrap: wrap; }}
.news-card .meta span {{ display: flex; align-items: center; gap: 4px; }}
.news-card a {{ color: #3498db; text-decoration: none; font-size: 0.9em; }}
.news-card a:hover {{ text-decoration: underline; }}

.filters {{ display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 20px; }}
.filter-btn {{ padding: 8px 16px; border: 1px solid #ddd; border-radius: 20px; background: white; cursor: pointer; font-size: 0.9em; transition: all 0.2s; }}
.filter-btn:hover {{ background: #f0f0f0; }}
.filter-btn.active {{ background: #1a1a2e; color: white; border-color: #1a1a2e; }}

.footer {{ text-align: center; padding: 40px; color: #aaa; font-size: 0.85em; }}
.empty {{ text-align: center; color: #aaa; padding: 40px; }}

@media (max-width: 768px) {{
    .header h1 {{ font-size: 1.4em; }}
    .stats {{ grid-template-columns: repeat(2, 1fr); }}
}}
</style>
</head>
<body>
<div class="header">
    <h1>📊 个股大事件新闻流</h1>
    <div class="subtitle">覆盖 A股 &middot; 港股 &middot; 美股 | 更新时间: {report_time}</div>
</div>

<div class="container">
    <div class="stats">
        <div class="stat-card major">
            <div class="number">{len(major_events)}</div>
            <div class="label">🔥 重大事件</div>
        </div>
        <div class="stat-card ashare">
            <div class="number">{a_count}</div>
            <div class="label">🇨🇳 A股新闻</div>
        </div>
        <div class="stat-card hk">
            <div class="number">{hk_count}</div>
            <div class="label">🇭🇰 港股新闻</div>
        </div>
        <div class="stat-card us">
            <div class="number">{us_count}</div>
            <div class="label">🇺🇸 美股新闻</div>
        </div>
        <div class="stat-card">
            <div class="number">{total}</div>
            <div class="label">📰 新闻总数</div>
        </div>
    </div>

    <div class="filters">
        <button class="filter-btn active" onclick="filterNews('all')">全部</button>
        <button class="filter-btn" onclick="filterNews('major')">🔥 仅大事件</button>
        <button class="filter-btn" onclick="filterNews('ashare')">🇨🇳 A股</button>
        <button class="filter-btn" onclick="filterNews('hk')">🇭🇰 港股</button>
        <button class="filter-btn" onclick="filterNews('us')">🇺🇸 美股</button>
    </div>

    <!-- 重大事件 -->
    <div class="section-title">
        🔥 重大事件 <span class="badge">{len(major_events)}</span>
    </div>
    {_render_news_cards(major_events, "major")}

    <!-- 全部新闻（按市场分） -->
    <div class="section-title">📰 A股新闻 <span class="badge" style="background:#e74c3c">{a_count}</span></div>
    {_render_news_cards([n for n in normal_news if n["market"]=="A股"], "ashare")}

    <div class="section-title">📰 港股新闻 <span class="badge" style="background:#2ecc71">{hk_count}</span></div>
    {_render_news_cards([n for n in normal_news if n["market"]=="港股"], "hk")}

    <div class="section-title">📰 美股新闻 <span class="badge" style="background:#3498db">{us_count}</span></div>
    {_render_news_cards([n for n in normal_news if n["market"]=="美股"], "us")}

</div>

<div class="footer">
    <p>数据来源: 东方财富 / Yahoo Finance | 个股大事件新闻流聚合器</p>
    <p>⚠️ 本报告仅供参考，不构成投资建议。投资有风险，决策需谨慎。</p>
</div>

<script>
function filterNews(type) {{
    document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
    event.target.classList.add('active');

    document.querySelectorAll('.news-card').forEach(card => {{
        if (type === 'all') {{
            card.style.display = 'block';
        }} else if (type === 'major') {{
            card.style.display = card.classList.contains('major') ? 'block' : 'none';
        }} else if (type === 'ashare') {{
            card.style.display = card.dataset.market === 'A股' ? 'block' : 'none';
        }} else if (type === 'hk') {{
            card.style.display = card.dataset.market === '港股' ? 'block' : 'none';
        }} else if (type === 'us') {{
            card.style.display = card.dataset.market === '美股' ? 'block' : 'none';
        }}
    }});
}}
</script>
</body>
</html>"""

    filename = f"stock_news_{report_date}.html"
    filepath = os.path.join(output_dir, filename)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(html)

    return filepath


def _render_news_cards(news_list: list[dict[str, Any]], category: str) -> str:
    """渲染新闻卡片HTML"""
    if not news_list:
        return '<div class="empty">暂无数据</div>'

    cards = []
    for item in news_list:
        market_class = {
            "A股": "ashare",
            "港股": "hk",
            "美股": "us",
        }.get(item.get("market", ""), "")

        is_major = item.get("is_major", False)
        major_class = "major" if is_major else ""
        event_badge = '<span class="event-badge">🔥 大事件</span> ' if is_major else ""

        title = item.get("title", "无标题")
        source = item.get("source", "未知")
        time_str = item.get("time", "")
        url = item.get("url", "#")
        stock_name = item.get("stock_name", "")
        stock_code = item.get("stock_code", "")

        cards.append(f"""
    <div class="news-card {major_class}" data-market="{item.get('market', '')}">
        <span class="stock-tag {market_class}">{item.get('market', '')} | {stock_name}({stock_code})</span>
        {event_badge}
        <h3>{title}</h3>
        <div class="meta">
            <span>📅 {time_str}</span>
            <span>📢 {source}</span>
            {"<a href='" + url + "' target='_blank'>查看原文 →</a>" if url and url != "#" and url != "nan" else ""}
        </div>
    </div>""")

    return "\n".join(cards)
