import argparse
import json
import sys
from pathlib import Path

from .artifacts import canonical


def main(argv=None):
    parser = argparse.ArgumentParser(prog="quant-research")
    parser.add_argument("--project", type=Path, default=Path(__file__).resolve().parents[2])
    commands = parser.add_subparsers(dest="command", required=True)
    doc = commands.add_parser("doctor")
    doc.add_argument("--offline", action="store_true", required=True)
    doc.add_argument("--config", type=Path)
    data = commands.add_parser("data")
    actions = data.add_subparsers(dest="action", required=True)
    fetch = actions.add_parser("fetch")
    fetch.add_argument("--config", type=Path)
    fetch.add_argument("--start")
    fetch.add_argument("--end", help="exclusive end date")
    fetch.add_argument("--parent", type=Path, help="complete same-source snapshot for incremental fetch")
    domestic = actions.add_parser("fetch-domestic", help="freeze unverified Sina US data separately")
    domestic.add_argument("--config", type=Path)
    domestic.add_argument("--start")
    domestic.add_argument("--end", help="exclusive end date")
    prepare = actions.add_parser("prepare-sina", help="build explicitly exploratory, sample-checked US snapshot")
    prepare.add_argument("--candidate", type=Path, required=True)
    prepare.add_argument("--supplement", type=Path, required=True)
    prepare.add_argument("--decoded", type=Path, required=True)
    prepare.add_argument("--config", type=Path, required=True)
    prepare.add_argument("--evidence", type=Path, required=True)
    validate = actions.add_parser("validate")
    validate.add_argument("--snapshot", type=Path, required=True)
    validate.add_argument("--config", type=Path)
    fixture = commands.add_parser("fixture")
    fixture.add_argument("--output", type=Path)
    backtest = commands.add_parser("backtest")
    backtest.add_argument("--snapshot", type=Path, required=True)
    backtest.add_argument("--config", type=Path)
    replay = commands.add_parser("replay")
    replay.add_argument("--manifest", type=Path, required=True)
    replay.add_argument("--offline", action="store_true", required=True)
    comparison = commands.add_parser("compare")
    comparison.add_argument("--snapshot", type=Path, required=True)
    comparison.add_argument("--config", type=Path)
    comparison.add_argument("--model-test-start", help="predeclared earliest quarterly model test boundary")
    comparison.add_argument("--portfolio-start", help="predeclared full-window first decision date")
    comparison_replay = commands.add_parser("replay-comparison")
    comparison_replay.add_argument("--manifest", type=Path, required=True)
    comparison_replay.add_argument("--offline", action="store_true", required=True)
    commands.add_parser("status")
    daily = commands.add_parser("daily")
    daily.add_argument("--snapshot", type=Path, required=True)
    daily.add_argument("--config", type=Path)
    factors = commands.add_parser("factors")
    factors.add_argument("--snapshot", type=Path, required=True)
    factors.add_argument("--config", type=Path)
    factors.add_argument("--horizon", type=int, choices=(5, 20), default=5)
    args = parser.parse_args(argv)
    try:
        if args.command == "compare":
            from .comparison import compare
            from .config import load_config
            print(canonical(compare(args.snapshot, load_config(args.config), args.project,
                                    args.model_test_start, args.portfolio_start)))
            return 0
        if args.command == "replay-comparison":
            from .comparison import replay_comparison
            print(canonical(replay_comparison(args.manifest, args.project)))
            return 0
        if args.command == "daily":
            from .config import load_config
            from .orchestration import daily_research
            print(canonical(daily_research(args.snapshot, load_config(args.config), args.project)))
            return 0
        if args.command == "status":
            from .readiness import readiness
            print(canonical({"status": "research_only", "stages": readiness()}))
            return 0
        if args.command == "factors":
            from .config import load_config
            from .factor_run import factor_run
            print(canonical(factor_run(args.snapshot, load_config(args.config), args.project, args.horizon)))
            return 0
        if args.command == "doctor":
            from .doctor import doctor
            result = doctor(args.project, args.config)
            print(canonical(result))
            return 0 if result["status"].startswith("environment_verified") else 2
        if args.command == "data":
            from .config import load_config
            from .data import YahooProvider, normalize, request_for_config, snapshot
            config = load_config(args.config)
            if args.action == "prepare-sina":
                from .sina_prepare import SinaFrozenProvider
                evidence = json.loads(args.evidence.read_text(encoding="utf-8"))
                if config.data.provider != "sina_us_daily":
                    raise ValueError("prepare-sina requires separate sina_us_daily configuration")
                request = request_for_config(config, evidence["window_start"], evidence["end_exclusive"])
                provider = SinaFrozenProvider(args.candidate, args.supplement, args.decoded, args.evidence)
                path = snapshot(request, provider, args.project / "data" / "sina_research", attempts=1, pause=0)
                result = json.loads((path / "manifest.json").read_text())
                print(canonical({"snapshot": str(path), **result}))
                return 0 if result["status"] == "complete" else 2
            if args.action == "fetch-domestic":
                from .domestic import collect
                result = collect(request_for_config(config, args.start, args.end), args.project / "data" / "domestic")
                print(canonical(result))
                return 0 if result["download_status"] == "complete" else 2
            if args.action == "fetch":
                path = snapshot(request_for_config(config, args.start, args.end),
                                YahooProvider(args.project / "state" / "yfinance"), args.project / "data" / "raw", parent=args.parent)
                result = json.loads((path / "manifest.json").read_text())
                print(canonical({"snapshot": str(path), **result}))
                return 0 if result["status"] == "complete" else 2
            _, quality = normalize(args.snapshot, config)
            print(canonical(quality))
            return 0 if quality["backtest_ready"] else 2
        if args.command == "fixture":
            from .fixtures import create_fixture
            print(canonical(create_fixture(args.output or args.project / "data" / "fixtures")))
            return 0
        if args.command == "backtest":
            from .config import load_config
            from .research import research
            print(canonical(research(args.snapshot, load_config(args.config), args.project)))
            return 0
        if args.command == "replay":
            from .research import replay
            print(canonical(replay(args.manifest, args.project)))
            return 0
    except (ValueError, OSError, RuntimeError) as exc:
        print(canonical({"status": "failed", "type": type(exc).__name__, "reason": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
