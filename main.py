"""Command-line entry point for a complete TradingAgents prediction run."""

import argparse
import sys
from datetime import date
from pathlib import Path

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import TradingAgentsGraph


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a trading prediction.")
    parser.add_argument("ticker", nargs="?", default="0700.HK")
    parser.add_argument(
        "--date",
        dest="trade_date",
        default=date.today().isoformat(),
        help="Analysis date in YYYY-MM-DD format (default: today).",
    )
    parser.add_argument(
        "--reports-dir",
        default="reports",
        help="Directory where the complete Markdown report tree is saved.",
    )
    parser.add_argument("--quiet", action="store_true", help="Disable agent trace output.")
    return parser.parse_args()


def main() -> int:
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    args = parse_args()
    try:
        date.fromisoformat(args.trade_date)
    except ValueError:
        print(f"Invalid --date value: {args.trade_date!r}; expected YYYY-MM-DD.", file=sys.stderr)
        return 2

    config = DEFAULT_CONFIG.copy()
    graph = TradingAgentsGraph(debug=not args.quiet, config=config)
    final_state, decision = graph.propagate(args.ticker, args.trade_date)

    report_root = Path(args.reports_dir) / f"{args.ticker}_{args.trade_date}"
    saved_path = graph.save_reports(final_state, args.ticker, save_path=report_root)
    print(f"\nFinal decision: {decision}")
    print(f"Reports saved to: {saved_path.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
