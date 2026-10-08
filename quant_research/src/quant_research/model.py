"""A fixed, low-capacity LightGBM fold; no automatic search or test-set fitting."""

import lightgbm as lgb
import numpy as np
import pandas as pd

from .artifacts import digest, file_hash, publication, run_id, utc_now, write_json

PARAMETERS = {"objective": "regression", "num_leaves": 7, "max_depth": 3, "learning_rate": .03,
              "min_data_in_leaf": 200, "n_estimators": 500, "reg_lambda": 1.0,
              "verbosity": -1, "deterministic": True, "force_col_wise": True, "n_jobs": 1}


def train_fold(features, samples, split, output, seed=42, estimator="lightgbm"):
    if estimator not in {"lightgbm", "ridge"}:
        raise ValueError("unregistered estimator")
    if split.get("status") != "ready":
        raise ValueError("insufficient_data: a ready purged split is required")
    if features.index.has_duplicates or samples.sample_id.duplicated().any():
        raise ValueError("duplicate sample identifiers")
    if not 10 <= len(features.columns) <= 20 or any("label" in str(c).lower() or "return_value" == c for c in features):
        raise ValueError("expected 10-20 registered feature columns, excluding labels")
    data = samples.set_index("sample_id")
    selections = {part: data.loc[split[part]] for part in ("train", "validation", "test")}
    train, valid, test = (selections[k] for k in ("train", "validation", "test"))
    if any(part.empty for part in selections.values()):
        raise ValueError("empty training/validation/test partition")
    if not all(np.isfinite(part.return_value).all() for part in (train, valid)):
        raise ValueError("training and validation require mature finite labels")
    groups = [set(part.session) for part in selections.values()]
    if groups[0] & groups[1] or groups[1] & groups[2] or groups[0] & groups[2]:
        raise ValueError("same decision date in multiple partitions")
    if train.label_available_at.max() >= valid.decision_at.min() or valid.label_available_at.max() >= test.decision_at.min():
        raise ValueError("unmatured labels cross fitting boundary")
    if train.session.max() >= valid.session.min() or valid.session.max() >= test.session.min():
        raise ValueError("nonchronological split")
    # Actual fit occurs now for a historical reconstruction. Logical fit cutoff
    # describes permissible historical knowledge, never a forged generation time.
    fit_cutoff = test.decision_at.min()
    raw_train = features.loc[train.index].replace([np.inf, -np.inf], np.nan)
    medians = raw_train.median()
    if medians.isna().any():
        raise ValueError("training feature has no finite observations")
    x = {part: features.loc[rows.index].replace([np.inf, -np.inf], np.nan).fillna(medians)
         for part, rows in selections.items()}
    # Nested atomic publication must stay below legacy Windows path limits.
    identifier = "mdl_" + run_id("model").rsplit("_", 1)[1]
    with publication(output, identifier) as stage:
        contract = {"estimator": estimator,
                    "parameters": {**PARAMETERS, "random_state": seed} if estimator == "lightgbm" else {"alpha": 1.0}, "feature_columns": list(features.columns),
                    "split": split, "preprocessor": {"fit_partition": "train", "medians": medians.to_dict()},
                    "fit_cutoff": fit_cutoff.isoformat(), "max_training_label_available_at": train.label_available_at.max().isoformat(),
                    "max_validation_label_available_at": valid.label_available_at.max().isoformat(),
                    "generated_at": utc_now(), "generation_mode": "historical_reconstruction",
                    "trial_budget": 1, "trial_count": 1, "quarterly_refit": split.get("refit_policy", "single_fold")}
        write_json(stage / "contract.json", contract)
        if estimator == "lightgbm":
            model = lgb.LGBMRegressor(**PARAMETERS, random_state=seed)
            model.fit(x["train"], train.return_value, eval_set=[(x["validation"], valid.return_value)],
                      callbacks=[lgb.early_stopping(30, verbose=False)])
            scores = model.predict(x["test"])
            model.booster_.save_model(str(stage / "model.txt"))
            model_path = stage / "model.txt"
            best_iteration = model.best_iteration_
        else:
            from sklearn.linear_model import Ridge
            from sklearn.preprocessing import StandardScaler
            scaler = StandardScaler().fit(x["train"])
            model = Ridge(alpha=1.0).fit(scaler.transform(x["train"]), train.return_value)
            scores = model.predict(scaler.transform(x["test"]))
            model_path = stage / "ridge.json"
            write_json(model_path, {"mean": scaler.mean_.tolist(), "scale": scaler.scale_.tolist(),
                                   "coef": model.coef_.tolist(), "intercept": float(model.intercept_)})
            best_iteration = None
        predictions = pd.DataFrame({"sample_id": test.index, "score": scores,
                                    "decision_at": test.decision_at.to_numpy()})
        predictions.to_parquet(stage / "predictions.parquet", index=False)
        result = {"status": "research_only_fold_not_strategy_validation", "model_id": identifier,
                  "contract_hash": digest(contract), "best_iteration": best_iteration,
                  "test_rows": len(predictions), "model_hash": file_hash(model_path)}
        write_json(stage / "result.json", result)
    return result, predictions
