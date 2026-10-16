"""Shared helpers for invoking an agent with structured output and a graceful fallback.

The Portfolio Manager, Trader, and Research Manager all follow the same
canonical pattern:

1. At agent creation, wrap the LLM with ``with_structured_output(Schema)``
   so the model returns a typed Pydantic instance. If the provider does
   not support structured output (rare; mostly older Ollama models), the
   wrap is skipped and the agent uses free-text generation instead.
2. At invocation, run the structured call and render the result back to
   markdown. The call carries ``STRUCTURED_OUTPUT_INSTRUCTION``, because a
   provider that cannot force ``tool_choice`` will otherwise sometimes answer
   the prompt's prose instructions instead of calling the tool. If the
   structured call still fails (transient provider issue), fall back to a
   plain ``llm.invoke`` so the pipeline never blocks.

Centralising the pattern here keeps the agent factories small and ensures
all three agents log the same warnings when fallback fires.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, TypeVar

from pydantic import BaseModel

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

# Schema-only structured output binds exactly one tool (the schema itself), so a
# model that reaches for a search tool emits an unknown tool call and the whole
# structured attempt is discarded for a free-text retry. Agents on this path
# state the constraint explicitly rather than relying on the binding alone
# (#1130).
NO_EXTERNAL_TOOLS = (
    "Use only the evidence provided in this prompt. Do not call external tools "
    "or search the web; if something is missing, say so explicitly."
)

# DeepSeek's thinking models reject ``tool_choice`` (see
# ``llm_clients/capabilities.py``), so the schema tool is bound but never
# forced: the model is free to honour the prompt's own "write these sections,
# starting with the recommendation on its own line" instructions in prose
# instead of calling the tool, and the parser then returns nothing (#1051).
# Measured on deepseek-v4-flash with a prose-shaped Research Manager prompt:
# 4/10 calls produced a tool call and 6/10 produced prose — which is exactly
# the intermittent "structured-output invocation failed" seen in the
# 2026-10-10 batch (Research Manager 3/5, Portfolio Manager 1/5, Trader 1/5).
# Appending this line took the same prompt to 10/10 tool calls. Nothing is lost
# by demanding the tool call: the markdown the agent emits is rendered from the
# tool's fields, not from the model's prose.
STRUCTURED_OUTPUT_INSTRUCTION = (
    "Return your answer by calling the provided tool, with every required field "
    "filled in: the tool call is the answer. Do not write the answer out as "
    "prose, and do not call any other tool."
)


def _with_structured_instruction(prompt: Any) -> Any:
    """Append ``STRUCTURED_OUTPUT_INSTRUCTION`` to a str or message-list prompt.

    The call sites pass either a rendered string (Portfolio Manager, Research
    Manager, Sentiment Analyst) or a list of message dicts (Trader), so both
    shapes are handled. Anything else is left alone rather than guessed at.
    """
    if isinstance(prompt, str):
        return f"{prompt}\n\n{STRUCTURED_OUTPUT_INSTRUCTION}"
    if isinstance(prompt, list):
        return [*prompt, {"role": "user", "content": STRUCTURED_OUTPUT_INSTRUCTION}]
    return prompt


def bind_structured(llm: Any, schema: type[T], agent_name: str) -> Any | None:
    """Return ``llm.with_structured_output(schema)`` or ``None`` if unsupported.

    Logs a warning when the binding fails so the user understands the agent
    will use free-text generation for every call instead of one-shot fallback.
    """
    try:
        return llm.with_structured_output(schema)
    except (NotImplementedError, AttributeError) as exc:
        logger.warning(
            "%s: provider does not support with_structured_output (%s); "
            "falling back to free-text generation",
            agent_name, exc,
        )
        return None


def invoke_structured(structured_llm: Any | None, prompt: Any, agent_name: str) -> T | None:
    """Run the structured call; ``None`` when there is none or it fails.

    ``prompt`` is whatever the underlying LLM accepts (a string for chat
    invocations, a list of message dicts for chat models that take that
    shape), so a caller can forward the same value to its free-text fallback.

    Every attempt carries ``STRUCTURED_OUTPUT_INSTRUCTION``, which is the fix
    for the prose answers a provider that cannot force ``tool_choice``
    otherwise produces. A miss is retried once before returning ``None``: at
    0/10 misses with the instruction in place the retry is insurance against a
    transient provider hiccup, not the mechanism doing the work. Returning
    ``None`` is the caller's cue to fall back to free text.
    """
    if structured_llm is None:
        return None
    attempt_prompt = _with_structured_instruction(prompt)
    for attempt in (1, 2):
        try:
            result = structured_llm.invoke(attempt_prompt)
            if result is not None:
                return result
            reason = "the model answered without calling the tool"
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"
        logger.warning(
            "%s: structured-output attempt %d/2 failed (%s)", agent_name, attempt, reason,
        )
    logger.warning(
        "%s: no structured result after 2 attempts; falling back to free text", agent_name,
    )
    return None


def invoke_structured_or_freetext(
    structured_llm: Any | None,
    plain_llm: Any,
    prompt: Any,
    render: Callable[[T], str],
    agent_name: str,
) -> str:
    """Run the structured call and render to markdown; fall back to free-text on any failure."""
    result = invoke_structured(structured_llm, prompt, agent_name)
    if result is not None:
        return render(result)
    return plain_llm.invoke(prompt).content
