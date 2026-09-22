"""Trader: turns the Research Manager's investment plan into a concrete transaction proposal."""

from __future__ import annotations

import functools

from langchain_core.messages import AIMessage

from tradingagents.agents.schemas import TraderProposal, render_trader_proposal
from tradingagents.agents.utils.agent_utils import (
    get_instrument_context_from_state,
    get_language_instruction,
)
from tradingagents.agents.utils.structured import (
    NO_EXTERNAL_TOOLS,
    bind_structured,
    invoke_structured_or_freetext,
)


def create_trader(llm):
    structured_llm = bind_structured(llm, TraderProposal, "Trader")

    def trader_node(state, name):
        company_name = state["company_of_interest"]
        instrument_context = get_instrument_context_from_state(state)
        investment_plan = state["investment_plan"]
        # The research plan digests the debate but loses exact price structure;
        # give the Trader the technical market report so entry/stop levels are
        # grounded in real ATR / support-resistance / current price (#1167). The
        # report is empty when the user did not select the market analyst, so
        # only offer it (and the grounding instruction) when it has content.
        market_report = (state.get("market_report") or "").strip()

        if market_report:
            grounding = (
                "Ground concrete price levels (entry, stop-loss, position sizing) in the technical "
                "market report's price structure -- current price, support/resistance, ATR, and "
                "volatility -- and use the research plan for direction and strategy. "
                "You MUST also fill the price-structure block: current_price (copied from the "
                "report, never estimated), support_levels (2-4 prices below current price, "
                "nearest first), resistance_levels (2-4 prices above current price, nearest "
                "first), invalidation_price (nearest support minus a small ATR-scaled buffer), "
                "and scale_out_ladder (2-4 rungs, each an absolute trigger_price plus the "
                "close_pct of the original position to sell there, summing to 100 or less). "
                "Use the same quote currency as the report. If the report carries no usable "
                "price structure, leave those fields null instead of inventing numbers. "
            )
            report_section = f"Technical Market Report:\n{market_report}\n\n"
        else:
            grounding = (
                "No technical market report is available, so leave current_price, "
                "support_levels, resistance_levels, invalidation_price and scale_out_ladder "
                "null rather than inventing price levels. "
            )
            report_section = ""

        messages = [
            {
                "role": "system",
                "content": (
                    "You are a trading agent analyzing market data to make investment decisions. "
                    "Based on your analysis, provide one rating: Buy, Overweight, Hold, Underweight, or Sell. "
                    "Preserve the research plan's five-level scale unless specific evidence justifies a change. "
                    "Anchor your reasoning in the analysts' reports and the research plan. "
                    + grounding
                    + NO_EXTERNAL_TOOLS
                    + get_language_instruction()
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Here is the research team's investment plan for {company_name}. "
                    f"{instrument_context}\n\n"
                    f"{report_section}"
                    f"Proposed Investment Plan:\n{investment_plan}\n\n"
                    f"Make an informed, strategic trading decision, and return the full "
                    f"price-structure block (current price, support/resistance, entry, "
                    f"stop-loss, invalidation price, staged scale-out ladder)."
                ),
            },
        ]

        trader_plan = invoke_structured_or_freetext(
            structured_llm,
            llm,
            messages,
            render_trader_proposal,
            "Trader",
        )

        return {
            "messages": [AIMessage(content=trader_plan)],
            "trader_investment_plan": trader_plan,
            "sender": name,
        }

    return functools.partial(trader_node, name="Trader")
