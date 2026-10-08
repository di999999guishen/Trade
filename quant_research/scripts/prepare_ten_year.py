"""Register the ten-year extension before computing any strategy returns."""
import json
from pathlib import Path

from quant_research.artifacts import file_hash, publication, run_id, write_json
from quant_research.config import ResearchConfig
from quant_research.data import normalize, request_for_config, snapshot
from quant_research.history_extension import ExtendedFrozenProvider


def main():
    project = Path(__file__).resolve().parents[1]
    identifier = "tenyear_inputs_" + run_id().rsplit("_", 1)[1]
    with publication(project / "outputs", identifier) as stage:
        config = json.loads((project / "configs/sina_pilot.json").read_text(encoding="utf-8"))
        config["data"].update(provider="sina_tencent_us_daily", requested_start="2015-09-17")
        write_json(stage / "config.json", config)
        evidence = json.loads((project / "configs/sina_pilot_evidence.json").read_text(encoding="utf-8"))
        evidence.update(window_start="2015-09-17", end_exclusive="2026-09-18",
                        window_selection="Ten-year target ending frozen 2026-09-17; first decision 2016-09-19; 252 prior sessions plus one prior-close bar for the SPY 2015-09-18 ex-date. No performance-based asset exclusions.",
                        model_test_start="2023-01-01",
                        model_window_reason="First complete calendar test year after 5+1 years fit history; 15 quarterly folds, 30 fits, no parameter search.")
        evidence["sources"] = {k: str(project / v) for k, v in {
            "candidate": "data/domestic/sina_candidate_20260918T035536607818Z_e935d5dea9934db1bf8e64d2a5a8cf3f",
            "supplement": "data/domestic/sina_supplement_20260918T041046647159Z_d802685bef6145799224d5e7cc39a8ec"}.items()}
        evidence["sources"].update({k: str(project / v) for k, v in {
            "decoded": "data/domestic/sina_decoded_20260918_1211",
            "tencent_bonds": "outputs/history_probe_db7023541776452b820e53579faab78f",
            "tencent_equity": "outputs/history_probe_f9054faf6aca4e37a504bc2f0d1dfffc"}.items()})
        evidence["tencent_symbols"] = {s: ["tencent_bonds", "OQ"] for s in ("IEF", "TLT")}
        evidence["tencent_symbols"].update({s: ["tencent_equity", "AM"] for s in ("XLB", "XLE", "XLRE", "XLC", "SPY")})
        evidence["listing_sources"] = {
            "XLRE": "https://nasdaqtrader.com/content/newsalerts/2015/infocircular/SPDRS10082015.pdf",
            "XLC": "https://www.sec.gov/Archives/edgar/data/895421/000183988221011200/ms2213_fwp-06956.htm"}
        evidence["special_distribution_proxy"] = {
            "date": "2016-09-19", "vendor_cash_equivalent": 4.44356,
            "actual_type": "XLRE shares distributed to XLF holders; not ordinary cash dividend",
            "treatment": "vendor estimated cash-equivalent ex-date reinvestment proxy only; no actual XLRE stock/cash ledger",
            "event_source": "https://www.miaxglobal.com/sites/default/files/alert-files/XLF_Distribution_39575.pdf",
            "amount_status": "vendor_value_not_final_independently_verified_distribution_valuation"}
        write_json(stage / "evidence.json", evidence)
        write_json(stage / "registration.json", {"status": "registered_before_backtest", "universe": config["universe"]["symbols"],
                    "horizon": "ten_year_rule_portfolios_partial_four_year_model_test", "costs_bps": [5, 10, 25],
                    "model_test_start": "2023-01-01", "max_model_fits": 30,
                    "allowed_repairs": {"XLB": ["2018-11-15"], "XLE": ["2018-11-15"], "XLRE": ["2017-07-11"]},
                    "missing_history_fill": "IEF/TLT only, original raw bars retained elsewhere unchanged",
                    "missing_XLRE_7_early_sessions": "leave missing; before 253-observation eligibility; never fabricate trades",
                    "volume_basis": "provisional share unit only; provider count discrepancies disclosed",
                    "promotion": "NO-GO until independent full-history basis and untouched holdout"})
        (stage / "preparation_source.py").write_bytes(Path(__file__).read_bytes())
    root = project / "outputs" / identifier
    config = ResearchConfig.model_validate(config)
    provider = ExtendedFrozenProvider(root / "evidence.json")
    request = request_for_config(config, evidence["window_start"], evidence["end_exclusive"])
    # Validate every asset first so errors carry their actual reason.
    try:
        for symbol in request.symbols:
            frame, metadata = provider.fetch(symbol, request)
            review = metadata["research_basis_evidence"]["row_review"]
            print(symbol, len(frame), "patches", len(review["patches"]), "early_gaps", len(review["unfilled_pre_warmup_gaps"]), flush=True)
        path = snapshot(request, provider, project / "data/ten_year", attempts=1, pause=0)
        _, quality = normalize(path, config)
    except Exception as exc:  # preserve rejected input preparations before reraising
        write_json(root / "failure.json", {"error": type(exc).__name__, "reason": str(exc)})
        write_json(root / "manifest.json", {"status": "failed", "files": {
            p.name: file_hash(p) for p in root.iterdir() if p.is_file()}})
        raise
    write_json(root / "snapshot.json", {"snapshot": str(path), "quality": quality})
    write_json(root / "manifest.json", {"status": "exploratory_inputs_ready", "files": {
        p.name: file_hash(p) for p in root.iterdir() if p.is_file()}})
    print(json.dumps({"registration": str(root), "snapshot": str(path)}), flush=True)


if __name__ == "__main__":
    main()
