"""Dependency gates for work that requires unavailable external evidence."""

REQUIREMENTS = {
    "lightgbm_oos": ["verified_real_prices", "factor_diagnostics", "purged_splits", "registered_trials", "untouched_holdout"],
    "lean_raw_audit": ["lean_engine_version", "raw_price_volume_verified", "corporate_actions", "dividend_pay_dates", "frozen_targets"],
    "forward_paper": ["lean_raw_audit_passed", "verified_real_prices", "frozen_strategy", "no_backdated_execution"],
    "shorting": ["historical_borrow_availability", "borrow_fees", "recalls", "financing", "margin", "dividend_compensation"],
    "options": ["historical_contract_master", "historical_bid_ask_chain", "timestamped_volume_oi", "adjustments", "assignment", "dividends"],
    "automatic_factors": ["verified_dataset", "registered_factors", "isolated_generator", "untouched_holdout", "trial_budget"],
}


def readiness(evidence=None):
    evidence = evidence or {}
    return {stage: {"status": "inputs_present_not_validated" if all(evidence.get(k) for k in needed) else "blocked_missing_evidence",
                    "missing": [k for k in needed if not evidence.get(k)], "enabled": False}
            for stage, needed in REQUIREMENTS.items()}
