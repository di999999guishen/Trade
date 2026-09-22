"""Freeze independent Sina static history and missing action responses."""
import json
import time
from pathlib import Path

from curl_cffi import requests

from quant_research.artifacts import file_hash, publication, run_id, utc_now, write_json
from quant_research.config import load_config
from quant_research.domestic import FACTORS


def main():
    root = Path(__file__).resolve().parents[1] / "data" / "domestic"
    identifier = run_id("sina_supplement")
    result = {"status": "unverified_supplement", "created_at": utc_now(), "responses": {}, "errors": {}}
    with publication(root, identifier) as stage:
        with requests.Session(impersonate="chrome") as client:
            for symbol in load_config().universe.symbols:
                entries = [("static", f"https://finance.sina.com.cn/staticdata/us/{symbol}")]
                if symbol in {"SPY", "XLB", "XLE", "XLRE"}:
                    entries.append(("adjustment", FACTORS.format(symbol)))
                for kind, url in entries:
                    key = f"{symbol}.{kind}.response"
                    try:
                        response = client.get(url, timeout=20)
                        (stage / key).write_bytes(response.content)
                        response.raise_for_status()
                        result["responses"][key] = {"url": url, "bytes": len(response.content)}
                    except Exception as exc:  # noqa: BLE001 -- frozen failure record
                        result["errors"][key] = type(exc).__name__
                    print(json.dumps({"file": key, "error": result["errors"].get(key)}), flush=True)
                    time.sleep(.25)
        result["files"] = {p.name: file_hash(p) for p in stage.iterdir()}
        write_json(stage / "manifest.json", result)
    print(root / identifier)


if __name__ == "__main__":
    main()
