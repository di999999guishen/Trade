"""Unified entry point. Preview: python run_pipeline.py --date YYYY-MM-DD --dry-run."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
# Reuse the existing WMI/proxy/dotenv bootstrap, without invoking its CLI.
import run_research  # noqa: F401,E402
from prediction_research.master_pipeline import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
