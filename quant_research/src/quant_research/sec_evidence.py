"""Preserve SEC fact versions as evidence, without assigning historical arrival times."""
import pandas as pd

TAGS = ("Assets", "GrossProfit", "NetIncomeLoss", "NetCashProvidedByUsedInOperatingActivities",
        "LongTermDebtCurrent", "LongTermDebtNoncurrent", "LongTermDebt", "ShortTermBorrowings", "CommercialPaper")


def extract_facts(companyfacts, submissions, expected_cik, tags=None):
    if int(companyfacts["cik"]) != expected_cik or int(submissions["cik"]) != expected_cik:
        raise ValueError("SEC identity mismatch")
    recent = submissions["filings"]["recent"]
    accession = recent["accessionNumber"]
    accepted = recent["acceptanceDateTime"]
    if len(accession) != len(accepted) or len(set(accession)) != len(accession):
        raise ValueError("invalid SEC accession mapping")
    acceptance = dict(zip(accession, accepted, strict=True))
    rows = []
    for tag in TAGS if tags is None else tags:
        for unit, facts in companyfacts.get("facts", {}).get("us-gaap", {}).get(tag, {}).get("units", {}).items():
            for fact in facts:
                if not {"accn", "end", "val", "filed", "form"}.issubset(fact):
                    raise ValueError("incomplete SEC fact provenance")
                rows.append({"security_id": f"CIK{expected_cik:010d}", "metric": tag, "unit": unit,
                             "period_start": fact.get("start"), "period_end": fact["end"], "value": fact["val"],
                             "revision_id": fact["accn"], "filed_date": fact["filed"], "form": fact["form"],
                             "acceptance_timestamp_as_returned": acceptance.get(fact["accn"]),
                             "available_at": None, "availability_status": "historical_arrival_not_verified"})
    # Same period across separate accessions remains separate, including revisions.
    return pd.DataFrame(rows)
