"""Entry point: python -m prediction_research.validation <subcommand> [args]"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from . import SUBCOMMANDS  # noqa: E402


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] not in SUBCOMMANDS:
        print("usage: python -m prediction_research.validation <subcommand> [args]")
        print("subcommands: " + ", ".join(SUBCOMMANDS))
        raise SystemExit(1)
    name = sys.argv[1]
    # strip the subcommand token so each script's own argparse only sees its args
    sys.argv = [sys.argv[0], *sys.argv[2:]]
    module = __import__(f"prediction_research.validation.{SUBCOMMANDS[name]}", fromlist=["main"])
    module.main()


if __name__ == "__main__":
    main()
