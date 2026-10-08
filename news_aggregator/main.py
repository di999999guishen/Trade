"""
个股大事件新闻流聚合器 - 主入口
一键运行：python main.py
定时运行：配置 cron / Windows Task Scheduler / GitHub Actions
"""
import sys
import os
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import STOCKS
from fetcher import fetch_all_news, classify_news
from reporter import generate_report


def main():
    print("=" * 60)
    print("   📊 个股大事件新闻流聚合器")
    print("   覆盖 A股 · 港股 · 美股")
    print(f"   运行时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    # 展示配置
    for market, stocks in STOCKS.items():
        print(f"  {market}: {len(stocks)} 只股票")

    # 1. 获取新闻
    print("\n[1/3] 开始获取新闻数据...")
    all_news = fetch_all_news()

    # 2. 分类（大事件 vs 普通）
    print("\n[2/3] 智能分类：识别重大事件...")
    major_events, normal_news = classify_news(all_news)

    total = sum(len(v) for v in all_news.values())
    print(f"\n  📈 统计:")
    print(f"     总新闻数: {total}")
    print(f"     🔥 重大事件: {len(major_events)}")
    for market in ["A股", "港股", "美股"]:
        print(f"     {market}: {len(all_news.get(market, []))} 条")

    # 3. 生成报告
    print("\n[3/3] 生成 HTML 报告...")
    report_path = generate_report(all_news, major_events, normal_news)
    print(f"\n  ✅ 报告已生成: {report_path}")

    # 4. 打印大事件摘要
    if major_events:
        print(f"\n{'='*60}")
        print("  🔥 今日重大事件摘要")
        print(f"{'='*60}")
        for i, event in enumerate(major_events[:10], 1):
            print(f"  {i}. [{event['market']}] {event['stock_name']}({event['stock_code']})")
            print(f"     {event['title'][:80]}")
            print(f"     📅 {event['time']} | 📢 {event['source']}")
            print()

    print(f"\n{'='*60}")
    print("  🎉 完成！打开 reports/ 目录查看报告")
    print(f"{'='*60}")

    return report_path


if __name__ == "__main__":
    main()
