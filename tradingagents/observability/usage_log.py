"""Append-only JSONL log of LLM token usage.

The logger defaults ON because nightly runs have no other way to notice a
provider silently changing its token accounting. Set ``TRADINGAGENTS_USAGE_LOG``
to ``off``/``0``/``false`` (case-insensitive) to remove the callback entirely;
without it, or with it pointing at a path, the recorder is attached.

Every failure inside ``on_llm_end`` is swallowed on purpose: observability must
never be able to turn a real prediction into a failed one.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler

logger = logging.getLogger(__name__)

_DISABLED_VALUES = {"off", "0", "false"}
_WRITE_LOCK = threading.Lock()
_RECORDER: "UsageRecorder | None" = None


def usage_log_enabled() -> bool:
    """False only for the explicit off values; unset and empty mean enabled."""
    raw = os.environ.get("TRADINGAGENTS_USAGE_LOG")
    if raw is None:
        return True
    return raw.strip().lower() not in _DISABLED_VALUES


def usage_log_path(now: datetime | None = None) -> Path:
    """Explicit env path when given, otherwise ``<repo>/.workbuddy/usage/YYYYMMDD.jsonl``."""
    raw = os.environ.get("TRADINGAGENTS_USAGE_LOG", "").strip()
    if raw:
        return Path(raw).expanduser()
    repo_root = Path(__file__).resolve().parents[2]
    stamp = (now or datetime.now()).strftime("%Y%m%d")
    return repo_root / ".workbuddy" / "usage" / f"{stamp}.jsonl"


def get_usage_recorder() -> "UsageRecorder | None":
    """One shared recorder per process, or None while logging is disabled."""
    global _RECORDER
    try:
        if not usage_log_enabled():
            return None
        if _RECORDER is None:
            _RECORDER = UsageRecorder()
        return _RECORDER
    except Exception as exc:  # pragma: no cover - defensive, never break a run
        logger.debug("usage recorder unavailable: %s", exc, exc_info=True)
        return None


def _as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _usage_from_mapping(usage: Any) -> tuple[int, int, int] | None:
    """Normalise OpenAI-style and LangChain-style token keys to three ints."""
    if not isinstance(usage, dict):
        return None
    prompt = _as_int(usage.get("prompt_tokens", usage.get("input_tokens")))
    completion = _as_int(
        usage.get("completion_tokens", usage.get("output_tokens"))
    )
    total = _as_int(usage.get("total_tokens"))
    if prompt is None and completion is None and total is None:
        return None
    prompt = prompt or 0
    completion = completion or 0
    if total is None:
        total = prompt + completion
    return prompt, completion, total


def _agent_from_run_context() -> str:
    """Best-effort graph-node (agent) name for the current LLM call.

    LangGraph records the executing node under ``config["metadata"]
    ["langgraph_node"]``. The value is reachable from the runnable-config
    contextvar while an LLM call runs inside a node, but it is not passed to
    ``on_llm_end`` via the ``metadata`` kwarg in this LangGraph version, so
    we read it from the active context instead. Returns ``""`` when no node
    is on the stack (e.g. auxiliary LLM calls outside the graph).
    """
    try:
        from langchain_core.runnables.config import var_child_runnable_config

        rc = var_child_runnable_config.get()
        if isinstance(rc, dict):
            return str((rc.get("metadata") or {}).get("langgraph_node") or "")
    except Exception:  # pragma: no cover - observability must never break a run
        pass
    return ""


def _extract_usage(response: Any) -> tuple[tuple[int, int, int] | None, str]:
    """Return ``((prompt, completion, total), model)`` using the documented priority."""
    model = ""
    llm_output = getattr(response, "llm_output", None)
    if isinstance(llm_output, dict):
        model = str(llm_output.get("model_name") or "")
        usage = _usage_from_mapping(llm_output.get("token_usage"))
        if usage is not None:
            return usage, model

    message = None
    try:
        message = response.generations[0][0].message
    except (AttributeError, IndexError, KeyError, TypeError):
        message = None
    usage_metadata = getattr(message, "usage_metadata", None)
    usage = _usage_from_mapping(usage_metadata)
    if usage is not None:
        return usage, model
    return None, model


class UsageRecorder(BaseCallbackHandler):
    """LangChain callback that appends one JSON object per LLM call."""

    def on_llm_end(self, response: Any, *, run_id: Any = None,
                   parent_run_id: Any = None, tags: Any = None,
                   **kwargs: Any) -> None:
        try:
            if not usage_log_enabled():
                return
            usage, model = _extract_usage(response)
            if usage is None:
                return
            prompt_tokens, completion_tokens, total_tokens = usage

            # Agent (graph node) attribution. LangGraph stamps the node name
            # into the runnable-config metadata as ``langgraph_node``; it is
            # NOT surfaced through the callback ``on_llm_end`` metadata kwarg
            # in this LangGraph version, so read it from the active run
            # context instead. Falls back to the callback metadata/tags for
            # any future wiring that does forward it.
            agent = _agent_from_run_context()
            if not agent:
                metadata = kwargs.get("metadata")
                if isinstance(metadata, dict):
                    agent = str(metadata.get("langgraph_node") or "")
            if not agent:
                for tag in (kwargs.get("tags") or []):
                    if isinstance(tag, str) and tag.startswith("langgraph_node:"):
                        agent = tag.split(":", 1)[1]
                        break

            record = {
                "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
                "run_id": (os.environ.get("TRADINGAGENTS_RUN_ID")
                           or datetime.now().strftime("%Y-%m-%d")),
                "ticker": os.environ.get("TRADINGAGENTS_TICKER", "") or "",
                "agent": agent,
                "model": model,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": total_tokens,
            }
            path = usage_log_path()
            line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
            with _WRITE_LOCK:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
        except Exception as exc:
            logger.debug("usage log write failed: %s", exc, exc_info=True)
