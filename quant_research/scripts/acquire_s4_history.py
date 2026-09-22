"""Download actual historical inputs; failures remain evidence, not fake coverage."""
import io
import json
import os
import time
from zipfile import ZipFile

import pandas as pd
from curl_cffi import requests

from quant_research.artifacts import file_hash, publication, run_id, utc_now, write_json


def acquire(project):
    identifier = run_id("s4_history")
    with publication(project / "outputs", identifier) as stage:
        reports = {}
        headers = {"User-Agent": "TradingAgents-quant-research/0.1 public-data-check"}
        def fetch(name, url, params=None):
            time.sleep(.25)
            try:
                response = requests.get(url, params=params, headers=headers, timeout=60)
                (stage / f"{name}.raw").write_bytes(response.content)
                reports[name] = {"http_status": response.status_code, "bytes": len(response.content),
                                 "url_without_credentials": url, "fetched_at": utc_now()}
                print(name, response.status_code, len(response.content), flush=True)
                return response.content if response.status_code == 200 else None
            except Exception as exc:  # noqa: BLE001 -- preserve bounded acquisition failures
                reports[name] = {"error": type(exc).__name__}
                print(name, type(exc).__name__, flush=True)
                return None
        # A historical filing cohort is evidence, NOT a listed-stock universe.
        body = fetch("sec_2016q3", "https://www.sec.gov/files/dera/data/financial-statement-data-sets/2016q3.zip")
        if body and body.startswith(b"PK"):
            with ZipFile(io.BytesIO(body)) as archive:
                submissions = pd.read_csv(archive.open("sub.txt"), sep="\t", low_memory=False)
                submissions.to_parquet(stage / "2016q3_submissions.parquet", index=False)
                reports["sec_2016q3"].update({"submissions": len(submissions), "unique_ciks": int(submissions.cik.nunique()),
                                             "columns": list(submissions.columns)})
        for label, cik in (("aapl", "0000320193"), ("bbby_legacy", "0000886158"), ("sears_legacy", "0001310067")):
            body = fetch(f"{label}_submissions", f"https://data.sec.gov/submissions/CIK{cik}.json")
            if not body:
                continue
            try:
                metadata = json.loads(body)
                if int(metadata["cik"]) != int(cik):
                    raise ValueError("CIK mismatch")
            except (ValueError, KeyError):
                reports[f"{label}_submissions"]["schema_invalid"] = True
                continue
            tables = [pd.DataFrame(metadata["filings"]["recent"])]
            for item in metadata["filings"].get("files", [])[:10]:
                filename = item["name"]
                if not filename.startswith(f"CIK{cik}-submissions-") or "/" in filename:
                    raise ValueError("unexpected SEC history path")
                older = fetch(f"{label}_{filename[:-5]}", f"https://data.sec.gov/submissions/{filename}")
                if older:
                    tables.append(pd.DataFrame(json.loads(older)))
            all_filings = pd.concat(tables, ignore_index=True).drop_duplicates("accessionNumber")
            all_filings.to_parquet(stage / f"{label}_filings.parquet", index=False)
            facts = fetch(f"{label}_facts", f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json")
            reports[f"{label}_coverage"] = {"issuer_name": metadata["name"], "cik": cik,
                "filings": len(all_filings), "first_filed": str(all_filings.filingDate.min()),
                "last_filed": str(all_filings.filingDate.max()),
                "delisting_forms": int(all_filings.form.isin(["25", "25-NSE", "15-12G", "15-15D"]).sum()),
                "facts_downloaded": facts is not None,
                "historical_price_and_final_recovery_present": False}
        key = os.environ.get("ALPHA_VANTAGE_API_KEY") or "demo"
        for state in ("active", "delisted"):
            body = fetch(f"listing_20160916_{state}", "https://www.alphavantage.co/query",
                         {"function": "LISTING_STATUS", "date": "2016-09-16", "state": state, "apikey": key})
            valid = bool(body and body.lstrip().startswith(b"symbol,name,exchange"))
            reports[f"listing_20160916_{state}"]["valid_listing_csv"] = valid
            reports[f"listing_20160916_{state}"]["credential_mode"] = "configured" if key != "demo" else "public_demo"
            if valid:
                frame = pd.read_csv(io.BytesIO(body))
                frame.to_parquet(stage / f"listing_20160916_{state}.parquet", index=False)
                reports[f"listing_20160916_{state}"]["rows"] = len(frame)
        write_json(stage / "report.json", reports)
        write_json(stage / "manifest.json", {"files": {p.name: file_hash(p) for p in stage.iterdir()},
                                               "script_hash": file_hash(__file__)})
    print(project / "outputs" / identifier)
