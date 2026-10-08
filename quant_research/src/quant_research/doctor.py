import sys
from importlib import import_module, metadata
from pathlib import Path

from .config import load_config


def doctor(project, config_path=None):
    config = load_config(config_path)
    dependencies = {}
    for distribution, module in (("pyqlib", "qlib"), ("pandas", "pandas"), ("pyarrow", "pyarrow"),
                                 ("duckdb", "duckdb"), ("exchange-calendars", "exchange_calendars"),
                                 ("yfinance", "yfinance"), ("lightgbm", "lightgbm")):
        try:
            import_module(module)
            dependencies[distribution] = {"status": "import_verified", "version": metadata.version(distribution)}
        except Exception as exc:  # noqa: BLE001 -- report any dependency import failure
            dependencies[distribution] = {"status": "unavailable", "error_type": type(exc).__name__}
    ready = all(item["status"] == "import_verified" for item in dependencies.values())
    return {"status": "environment_verified_research_not_ready" if ready else "environment_not_ready",
            "executable": sys.executable, "python": sys.version, "project": str(Path(project).resolve()),
            "dependencies": dependencies, "config_valid": True,
            "data_ready": False, "qlib_integration_check": "not_run_by_doctor_use_pytest", "lean_ready": False,
            "disabled_features": [key for key, value in config.features.model_dump().items() if value is False],
            "readiness_note": "Imports alone do not validate datasets, exchange accounting or trading readiness."}
