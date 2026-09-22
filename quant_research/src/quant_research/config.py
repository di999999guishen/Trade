"""Strict typed proposal contract. Unsupported modes fail before any I/O."""
import json
import math
from datetime import date, time
from importlib.resources import files
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, create_model, model_validator


class StrictSection(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, allow_inf_nan=False)


# Generate typed nested sections from the checked-in contract. Values not exposed
# as tunable below are Literals: accepting an ignored setting is an unsafe API.
TUNABLE = {
    "data.requested_start", "universe.symbols", "universe.min_adv_usd",
    "portfolio.initial_capital_usd", "portfolio.single_asset_weight_max",
    "portfolio.weight_change_trade_threshold", "portfolio.one_way_turnover_soft_cap",
    "portfolio.technology_proxy_weight_max", "portfolio.sector_etf_total_weight_max",
    "portfolio.execution_participation_of_lagged_daily_adv_max",
    "costs.base_one_way_bps", "costs.stress_one_way_bps", "validation.seed",
}


def _section(name: str, values: dict, prefix: str = "") -> type[BaseModel]:
    fields = {}
    for key, value in values.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            annotation = _section(key.title(), value, path)
        elif path == "data.provider":
            annotation = Literal["yfinance", "sina_us_daily", "sina_tencent_us_daily"]
        elif path in TUNABLE:
            annotation = list[type(value[0])] if isinstance(value, list) else type(value)
            if type(value) in (int, float) and path != "validation.seed":
                annotation = float
        elif isinstance(value, list):
            annotation = list[type(value[0])]
        elif value is None:
            annotation = type(None)
        else:
            annotation = Literal[value]
        fields[key] = (annotation, ...)
    return create_model(name, __base__=StrictSection, **fields)


DEFAULTS = json.loads(files("quant_research").joinpath("defaults.json").read_text(encoding="utf-8"))
Contract = _section("ResearchContract", DEFAULTS)


class ResearchConfig(Contract):
    @model_validator(mode="before")
    @classmethod
    def strict_booleans(cls, payload):
        def visit(actual, expected):
            if not isinstance(actual, dict):
                return
            for key, value in expected.items():
                if key not in actual:
                    continue
                if isinstance(value, dict):
                    visit(actual[key], value)
                elif isinstance(value, bool) and type(actual[key]) is not bool:
                    raise ValueError(f"{key} must be a JSON boolean")
        visit(payload, DEFAULTS)
        return payload

    @model_validator(mode="after")
    def check_semantics(self):
        values = self.model_dump()
        def check_lists(actual, expected, prefix=""):
            for key, value in expected.items():
                path = f"{prefix}.{key}" if prefix else key
                if isinstance(value, dict):
                    check_lists(actual[key], value, path)
                elif isinstance(value, list) and path not in TUNABLE and actual[key] != value:
                    raise ValueError(f"unsupported setting: {path}")
        check_lists(values, DEFAULTS)
        ZoneInfo(self.market.timezone)
        time.fromisoformat(self.market.decision_time)
        date.fromisoformat(self.data.requested_start)
        symbols = self.universe.symbols
        if not symbols or len(set(symbols)) != len(symbols):
            raise ValueError("universe must contain unique symbols")
        if not {"SPY", "QQQ"}.issubset(symbols):
            raise ValueError("both benchmarks must be in the snapshot universe")
        if any(not s.isascii() or not s.isalpha() or not s.isupper() for s in symbols):
            raise ValueError("symbols must be uppercase ASCII ETF tickers")
        p = self.portfolio
        if p.initial_capital_usd <= 0 or self.universe.min_adv_usd < 0:
            raise ValueError("invalid capital or ADV")
        for name in ("single_asset_weight_max", "weight_change_trade_threshold",
                     "one_way_turnover_soft_cap", "technology_proxy_weight_max",
                     "sector_etf_total_weight_max", "execution_participation_of_lagged_daily_adv_max"):
            value = getattr(p, name)
            if not math.isfinite(value) or not 0 <= value <= 1:
                raise ValueError(f"{name} must be in [0,1]")
        for cost in [self.costs.base_one_way_bps, *self.costs.stress_one_way_bps]:
            if not math.isfinite(cost) or not 0 <= cost < 10000:
                raise ValueError("cost must be finite and in [0,10000) bps")
        return self


def load_config(path: str | Path | None = None) -> ResearchConfig:
    payload: dict[str, Any] = DEFAULTS if path is None else json.loads(Path(path).read_text(encoding="utf-8"))
    return ResearchConfig.model_validate(payload)
