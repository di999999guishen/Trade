"""Pydantic schemas used by agents that produce structured output.

The framework's primary artifact is still prose: each agent's natural-language
reasoning is what users read in the saved markdown reports and what the
downstream agents read as context.  Structured output is layered onto the
three decision-making agents (Research Manager, Trader, Portfolio Manager)
so that:

- Their outputs follow consistent section headers across runs and providers
- Each provider's native structured-output mode is used (json_schema for
  OpenAI/xAI, response_schema for Gemini, tool-use for Anthropic)
- Schema field descriptions become the model's output instructions, freeing
  the prompt body to focus on context and the rating-scale guidance
- A render helper turns the parsed Pydantic instance back into the same
  markdown shape the rest of the system already consumes, so display,
  memory log, and saved reports keep working unchanged
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, field_validator

# LLMs sometimes write a placeholder string ("None", "N/A", ...) into an optional
# numeric field instead of omitting it. Coerce those to None so the structured
# call validates instead of erroring (#1058). Pydantic still parses real numeric
# strings ("189.5") to float.
_NULLISH_FLOAT = {"", "none", "n/a", "na", "null", "nil", "-", "tbd", "unknown"}


def _coerce_optional_float(value):
    if isinstance(value, str) and value.strip().lower() in _NULLISH_FLOAT:
        return None
    return value


# ---------------------------------------------------------------------------
# Shared rating types
# ---------------------------------------------------------------------------


class PortfolioRating(str, Enum):
    """5-tier rating used by the Research Manager and Portfolio Manager."""

    BUY = "Buy"
    OVERWEIGHT = "Overweight"
    HOLD = "Hold"
    UNDERWEIGHT = "Underweight"
    SELL = "Sell"


class TraderAction(str, Enum):
    """Five-tier action scale shared with research and portfolio management."""

    BUY = "Buy"
    OVERWEIGHT = "Overweight"
    HOLD = "Hold"
    UNDERWEIGHT = "Underweight"
    SELL = "Sell"


# ---------------------------------------------------------------------------
# Price-level / exit-plan building blocks
# ---------------------------------------------------------------------------


class ScaleOutStep(BaseModel):
    """One rung of the staged profit-taking / position-clearing ladder.

    The ladder answers "at what price do I sell how much?" so a plan is
    executable without a second decision round: each rung names the trigger
    price and the share of the ORIGINAL position to close there.
    """

    trigger_price: float = Field(
        description=(
            "Price level in the instrument's quote currency that triggers this "
            "rung. Must be an absolute price, not a percentage or a range."
        ),
    )
    close_pct: float = Field(
        ge=0.0,
        le=100.0,
        description=(
            "Percentage of the ORIGINAL position to close at this rung (0-100). "
            "All rungs together must not exceed 100."
        ),
    )
    condition: str | None = Field(
        default=None,
        description=(
            "Optional execution condition for this rung, e.g. 'only if volume "
            "expands' or 'needs a daily close above the level'. Leave null when "
            "the rung is an unconditional limit order."
        ),
    )

    @field_validator("trigger_price", "close_pct", mode="before")
    @classmethod
    def _nullish_float_to_none(cls, v):
        return _coerce_optional_float(v)


def _format_levels(levels: list[float] | None) -> str | None:
    """Render a level list as ``a / b / c`` (drop placeholders, keep order)."""
    if not levels:
        return None
    cleaned = [f"{level:g}" for level in levels if isinstance(level, (int, float))]
    return " / ".join(cleaned) if cleaned else None


def _render_ladder(ladder: list[ScaleOutStep] | None) -> list[str]:
    """Render the staged exit ladder as a markdown table block."""
    if not ladder:
        return []
    rows = ["**Scale-out Ladder** (close % is of the original position):", "",
            "| Rung | Trigger Price | Close % | Condition |",
            "| --- | --- | --- | --- |"]
    for index, step in enumerate(ladder, start=1):
        condition = (step.condition or "-").replace("|", "/").strip() or "-"
        rows.append(f"| {index} | {step.trigger_price:g} | {step.close_pct:g} | {condition} |")
    return rows


# ---------------------------------------------------------------------------
# Research Manager
# ---------------------------------------------------------------------------


class ResearchPlan(BaseModel):
    """Structured investment plan produced by the Research Manager.

    Hand-off to the Trader: the recommendation pins the directional view,
    the rationale captures which side of the bull/bear debate carried the
    argument, and the strategic actions translate that into concrete
    instructions the trader can execute against.
    """

    recommendation: PortfolioRating = Field(
        description=(
            "The investment recommendation. Exactly one of Buy / Overweight / "
            "Hold / Underweight / Sell. Choose Hold when the evidence is "
            "balanced, materially conflicting, ambiguous, or insufficient to "
            "justify changing exposure; otherwise commit to the side with the "
            "clearly stronger arguments. Do not pick a direction merely to be "
            "decisive."
        ),
    )
    rationale: str = Field(
        description=(
            "Conversational summary of the key points from both sides of the "
            "debate, ending with which arguments led to the recommendation. "
            "Speak naturally, as if to a teammate."
        ),
    )
    strategic_actions: str = Field(
        description=(
            "Concrete steps for the trader to implement the recommendation, "
            "including position sizing guidance consistent with the rating."
        ),
    )


def render_research_plan(plan: ResearchPlan) -> str:
    """Render a ResearchPlan to markdown for storage and the trader's prompt context."""
    return "\n".join([
        f"**Recommendation**: {plan.recommendation.value}",
        "",
        f"**Rationale**: {plan.rationale}",
        "",
        f"**Strategic Actions**: {plan.strategic_actions}",
    ])


# ---------------------------------------------------------------------------
# Trader
# ---------------------------------------------------------------------------


class TraderProposal(BaseModel):
    """Structured transaction proposal produced by the Trader.

    The trader reads the Research Manager's investment plan and the analyst
    reports, then turns them into a concrete transaction: what action to
    take, the reasoning that justifies it, and the practical levels for
    entry, stop-loss, and sizing.
    """

    action: TraderAction = Field(
        description="The action. Exactly one of Buy / Overweight / Hold / Underweight / Sell.",
    )
    reasoning: str = Field(
        description=(
            "The case for this action, anchored in the analysts' reports and "
            "the research plan. Two to four sentences."
        ),
    )
    entry_price: float | None = Field(
        default=None,
        description="Optional entry price target in the instrument's quote currency.",
    )
    stop_loss: float | None = Field(
        default=None,
        description="Optional stop-loss price in the instrument's quote currency.",
    )
    position_sizing: str | None = Field(
        default=None,
        description="Optional sizing guidance, e.g. '5% of portfolio'.",
    )
    current_price: float | None = Field(
        default=None,
        description=(
            "Latest traded price used to anchor every other level. Copy it from "
            "the technical market report / verified snapshot; never estimate it."
        ),
    )
    support_levels: list[float] | None = Field(
        default=None,
        description=(
            "Two to four support prices BELOW the current price, nearest first. "
            "Derive from the report's price structure (moving averages, prior "
            "swing lows, Bollinger bands, round numbers) and state them as "
            "absolute prices."
        ),
    )
    resistance_levels: list[float] | None = Field(
        default=None,
        description=(
            "Two to four resistance prices ABOVE the current price, nearest "
            "first, derived the same way as support_levels."
        ),
    )
    invalidation_price: float | None = Field(
        default=None,
        description=(
            "The price at which the trade thesis is considered dead (usually the "
            "nearest support minus a small buffer). Either invalidation_price or "
            "stop_loss must be filled whenever a directional action is proposed."
        ),
    )
    scale_out_ladder: list[ScaleOutStep] | None = Field(
        default=None,
        description=(
            "Two to four staged profit-taking rungs for a NEW position, ordered "
            "from nearest to furthest target. close_pct across all rungs must sum "
            "to 100 or less. Omit for a Hold with no position."
        ),
    )

    @field_validator("entry_price", "stop_loss", "current_price", "invalidation_price",
                     mode="before")
    @classmethod
    def _nullish_float_to_none(cls, v):
        return _coerce_optional_float(v)


def render_trader_proposal(proposal: TraderProposal) -> str:
    """Render a TraderProposal to markdown.

    The trailing ``FINAL TRANSACTION PROPOSAL: **BUY/HOLD/SELL**`` line is
    preserved for backward compatibility with the analyst stop-signal text
    and any external code that greps for it.
    """
    parts = [
        f"**Action**: {proposal.action.value}",
        "",
        f"**Reasoning**: {proposal.reasoning}",
    ]
    if proposal.entry_price is not None:
        parts.extend(["", f"**Entry Price**: {proposal.entry_price}"])
    if proposal.current_price is not None:
        parts.extend(["", f"**Current Price**: {proposal.current_price}"])
    if proposal.stop_loss is not None:
        parts.extend(["", f"**Stop Loss**: {proposal.stop_loss}"])
    if proposal.invalidation_price is not None:
        parts.extend(["", f"**Invalidation Price**: {proposal.invalidation_price}"])
    support = _format_levels(proposal.support_levels)
    if support:
        parts.extend(["", f"**Support Levels** (nearest first): {support}"])
    resistance = _format_levels(proposal.resistance_levels)
    if resistance:
        parts.extend(["", f"**Resistance Levels** (nearest first): {resistance}"])
    if proposal.position_sizing:
        parts.extend(["", f"**Position Sizing**: {proposal.position_sizing}"])
    ladder = _render_ladder(proposal.scale_out_ladder)
    if ladder:
        parts.extend(["", *ladder])
    parts.extend([
        "",
        f"FINAL TRANSACTION PROPOSAL: **{proposal.action.value.upper()}**",
    ])
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Portfolio Manager
# ---------------------------------------------------------------------------


class PortfolioDecision(BaseModel):
    """Structured output produced by the Portfolio Manager.

    The model fills every field as part of its primary LLM call; no separate
    extraction pass is required. Field descriptions double as the model's
    output instructions, so the prompt body only needs to convey context and
    the rating-scale guidance.
    """

    rating: PortfolioRating = Field(
        description=(
            "The final position rating. Exactly one of Buy / Overweight / Hold / "
            "Underweight / Sell, picked based on the analysts' debate. Choose "
            "Hold when the case is balanced, materially conflicting, ambiguous, "
            "or insufficient to justify changing exposure, rather than forcing a "
            "direction to appear decisive."
        ),
    )
    executive_summary: str = Field(
        description=(
            "A concise action plan covering entry strategy, position sizing, "
            "key risk levels, and time horizon. Two to four sentences."
        ),
    )
    investment_thesis: str = Field(
        description=(
            "Detailed reasoning anchored in specific evidence from the analysts' "
            "debate. If prior lessons are referenced in the prompt context, "
            "incorporate them; otherwise rely solely on the current analysis."
        ),
    )
    price_target: float | None = Field(
        default=None,
        description="Optional target price in the instrument's quote currency.",
    )
    time_horizon: str | None = Field(
        default=None,
        description="Optional recommended holding period, e.g. '3-6 months'.",
    )
    current_price: float | None = Field(
        default=None,
        description=(
            "Latest traded price that every other level below is anchored to. "
            "Copy it from the analyst evidence; never estimate it."
        ),
    )
    support_levels: list[float] | None = Field(
        default=None,
        description=(
            "Two to four support prices BELOW current_price, nearest first, in "
            "absolute quote currency. Carry them over from the trader plan unless "
            "the risk debate justifies moving them, and say so in the thesis."
        ),
    )
    resistance_levels: list[float] | None = Field(
        default=None,
        description=(
            "Two to four resistance prices ABOVE current_price, nearest first, in "
            "absolute quote currency."
        ),
    )
    entry_zone: str | None = Field(
        default=None,
        description=(
            "Where a holder should BUY or ADD, and under what confirmation, e.g. "
            "'1.240-1.255 on a daily close back above 10EMA; skip if it gaps "
            "straight to 1.30'. Leave null when the rating forbids adding."
        ),
    )
    stop_loss: float | None = Field(
        default=None,
        description=(
            "The single hard stop price for the position. Must sit below "
            "current_price for a long. Fill it for every actionable rating "
            "(Buy / Overweight / Hold-with-position / Underweight); use null only "
            "when no position is held."
        ),
    )
    scale_out_ladder: list[ScaleOutStep] | None = Field(
        default=None,
        description=(
            "Staged profit-taking / position-clearing ladder for the EXISTING or "
            "intended position: two to four rungs ordered nearest-first, each with "
            "an absolute trigger price and the percent of the original position to "
            "close. close_pct must sum to 100 or less. For a full exit rating, make "
            "a single rung (or two) that clears 100%."
        ),
    )
    reduce_trigger: str | None = Field(
        default=None,
        description=(
            "The observable condition that forces a reduction before the stop is "
            "hit, e.g. 'two consecutive closes below 1.255' or 'main outflow "
            "exceeds 5% of turnover for two sessions'."
        ),
    )

    @field_validator("price_target", "current_price", "stop_loss", mode="before")
    @classmethod
    def _nullish_float_to_none(cls, v):
        return _coerce_optional_float(v)


def render_pm_decision(decision: PortfolioDecision) -> str:
    """Render a PortfolioDecision back to the markdown shape the rest of the system expects.

    Memory log, CLI display, and saved report files all read this markdown,
    so the rendered output preserves the exact section headers (``**Rating**``,
    ``**Executive Summary**``, ``**Investment Thesis**``) that downstream
    parsers and the report writers already handle.
    """
    parts = [
        f"**Rating**: {decision.rating.value}",
        "",
        f"**Executive Summary**: {decision.executive_summary}",
        "",
        f"**Investment Thesis**: {decision.investment_thesis}",
    ]
    if decision.price_target is not None:
        parts.extend(["", f"**Price Target**: {decision.price_target}"])
    if decision.time_horizon:
        parts.extend(["", f"**Time Horizon**: {decision.time_horizon}"])
    if decision.current_price is not None:
        parts.extend(["", f"**Current Price**: {decision.current_price}"])
    support = _format_levels(decision.support_levels)
    if support:
        parts.extend(["", f"**Support Levels** (nearest first): {support}"])
    resistance = _format_levels(decision.resistance_levels)
    if resistance:
        parts.extend(["", f"**Resistance Levels** (nearest first): {resistance}"])
    if decision.entry_zone:
        parts.extend(["", f"**Entry / Add Zone**: {decision.entry_zone}"])
    if decision.stop_loss is not None:
        parts.extend(["", f"**Stop Loss**: {decision.stop_loss}"])
    if decision.reduce_trigger:
        parts.extend(["", f"**Reduce Trigger**: {decision.reduce_trigger}"])
    ladder = _render_ladder(decision.scale_out_ladder)
    if ladder:
        parts.extend(["", *ladder])
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Sentiment Analyst
# ---------------------------------------------------------------------------


class SentimentBand(str, Enum):
    """Discrete sentiment direction produced by the Sentiment Analyst.

    Six tiers keep the signal granular enough to be actionable while remaining
    small enough for every provider to map reliably from its JSON output.
    """

    BULLISH = "Bullish"
    MILDLY_BULLISH = "Mildly Bullish"
    NEUTRAL = "Neutral"
    MIXED = "Mixed"
    MILDLY_BEARISH = "Mildly Bearish"
    BEARISH = "Bearish"


class SentimentReport(BaseModel):
    """Structured sentiment report produced by the Sentiment Analyst.

    Replaces the previous free-form prose output so downstream consumers
    (dashboards, audit logs, PDF renderers, other agents) can read
    ``overall_band`` and ``overall_score`` without maintaining fragile regex
    fallbacks that drift with every model release. ``narrative`` preserves the
    rich source-by-source analysis; ``render_sentiment_report`` prepends a
    deterministic header so the saved report stays human-readable.
    """

    overall_band: SentimentBand = Field(
        description=(
            "Overall sentiment direction. Exactly one of: "
            "Bullish / Mildly Bullish / Neutral / Mixed / Mildly Bearish / Bearish. "
            "Use Mixed when sources point in clearly different directions. "
            "Use Neutral only when all sources are genuinely silent or non-committal."
        ),
    )
    overall_score: float = Field(
        ge=0.0,
        le=10.0,
        description=(
            "Numeric sentiment intensity on a 0–10 scale. "
            "0 = maximally bearish, 5 = neutral, 10 = maximally bullish. "
            "Guideline for consistency with overall_band: "
            "Bullish ~6.5–10, Mildly Bullish ~5.5–6.4, Neutral/Mixed ~4.5–5.5, "
            "Mildly Bearish ~3.5–4.4, Bearish ~0–3.4. "
            "Only the 0–10 bounds are enforced."
        ),
    )
    confidence: Literal["low", "medium", "high"] = Field(
        description=(
            "Confidence in the assessment based on data quality and sample size. "
            "Use 'low' when one or more sources returned a placeholder or fewer "
            "than 5 data points; 'medium' when data is present but sparse; "
            "'high' when all three sources returned substantive data."
        ),
    )
    narrative: str = Field(
        description=(
            "Full sentiment report covering, in order: "
            "(1) source-by-source breakdown with specific evidence (cite message "
            "counts, ratios, notable posts); "
            "(2) cross-source divergences and alignments; "
            "(3) dominant narrative themes; "
            "(4) catalysts and risks surfaced by the data; "
            "(5) a markdown table summarising key sentiment signals, their "
            "direction, source, and supporting evidence. "
            "Keep it informative and substantive: develop each section thoroughly "
            "with concrete evidence so every point adds new signal for the trader."
        ),
    )


def render_sentiment_report(report: SentimentReport) -> str:
    """Render a SentimentReport to the markdown shape the rest of the system expects.

    The structured header (band + score + confidence) is prepended to the
    narrative so the saved report is both human-readable and machine-parseable
    without regex.
    """
    return "\n".join([
        f"**Overall Sentiment:** **{report.overall_band.value}** "
        f"(Score: {report.overall_score:.1f}/10)",
        f"**Confidence:** {report.confidence.capitalize()}",
        "",
        report.narrative,
    ])
