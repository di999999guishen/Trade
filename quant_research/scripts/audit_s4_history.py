"""Join downloaded SEC facts to full accession history without inventing availability."""
import json
import shutil
import sys
from pathlib import Path

import pandas as pd

from quant_research.artifacts import file_hash, publication, run_id, verify_files, write_json
from quant_research.sec_evidence import extract_facts


def audit(source):
    source = Path(source).resolve()
    verify_files(source, json.loads((source / "manifest.json").read_text()))
    identifier = run_id("s4_history_audit")
    report = {"source": str(source), "historical_universe_complete": False,
              "historical_returns_validated": False, "issuers": {}}
    with publication(source.parent, identifier) as stage:
        for label, cik in (("aapl", 320193), ("bbby_legacy", 886158), ("sears_legacy", 1310067)):
            metadata = json.loads((source / f"{label}_submissions.raw").read_bytes())
            facts = json.loads((source / f"{label}_facts.raw").read_bytes())
            filings = pd.read_parquet(source / f"{label}_filings.parquet")
            if filings.accessionNumber.duplicated().any():
                raise ValueError("duplicate accession")
            original = extract_facts(facts, metadata, cik)
            expanded = dict(metadata)
            expanded["filings"] = {"recent": filings.to_dict("list")}
            joined = extract_facts(facts, expanded, cik)
            if len(original) != len(joined) or not joined.available_at.isna().all():
                raise ValueError("fact count or historical availability changed")
            accepted = pd.to_datetime(joined.acceptance_timestamp_as_returned, utc=True, errors="coerce")
            matched = accepted.notna()
            # Preserve raw timestamps. Filing dates alone do not assert exact trading availability.
            joined.to_parquet(stage / f"{label}_fact_versions.parquet", index=False)
            form_evidence = filings[filings.form.isin(["25", "25-NSE", "15-12G", "15-15D"])].copy()
            form_evidence.to_parquet(stage / f"{label}_registration_forms.parquet", index=False)
            report["issuers"][label] = {
                "cik": cik, "name": metadata["name"], "facts": len(joined),
                "acceptance_matches_recent_only": int(pd.to_datetime(
                    original.acceptance_timestamp_as_returned, utc=True, errors="coerce").notna().sum()),
                "acceptance_matches_full_history": int(matched.sum()),
                "first_fact_period_end": str(joined.period_end.min()),
                "last_fact_period_end": str(joined.period_end.max()),
                "facts_filed_by_2016_09_16": int((joined.filed_date <= "2016-09-16").sum()),
                "registration_form_evidence": form_evidence[["accessionNumber", "form", "filingDate"]].to_dict("records"),
                "note": "Forms can concern specific securities or reporting obligations; not proof of common-stock delisting or recovery.",
            }
        shutil.copyfile(__file__, stage / "audit_s4_history.py")
        write_json(stage / "report.json", report)
        write_json(stage / "manifest.json", {"files": {p.name: file_hash(p) for p in stage.iterdir()},
                                               "source_manifest_hash": file_hash(source / "manifest.json")})
    print(source.parent / identifier)
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    audit(sys.argv[1])
