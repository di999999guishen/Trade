"""Summarise a JSONL usage log.

Usage::

    python -m tradingagents.observability.usage_report <path/to/usage.jsonl>

Pure standard library so it also works where the agent dependencies are not
installed (the log is often inspected on another machine).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path


def summarize(path: Path) -> tuple[list[tuple[str, str, int, int, int]], int, int]:
    """Aggregate by ``(ticker, agent)``; returns rows, total prompt, bad lines."""
    buckets: dict[tuple[str, str], dict[str, int]] = defaultdict(
        lambda: {"calls": 0, "prompt": 0, "completion": 0}
    )
    bad_lines = 0
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                bad_lines += 1
                continue
            if not isinstance(record, dict):
                bad_lines += 1
                continue
            key = (str(record.get("ticker") or ""), str(record.get("agent") or ""))
            bucket = buckets[key]
            bucket["calls"] += 1
            bucket["prompt"] += int(record.get("prompt_tokens") or 0)
            bucket["completion"] += int(record.get("completion_tokens") or 0)

    total_prompt = sum(b["prompt"] for b in buckets.values())
    rows = [
        (ticker, agent, bucket["calls"], bucket["prompt"], bucket["completion"])
        for (ticker, agent), bucket in buckets.items()
    ]
    rows.sort(key=lambda row: (-row[3], row[0], row[1]))
    return rows, total_prompt, bad_lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", help="JSONL usage log produced by UsageRecorder")
    args = parser.parse_args(argv)

    path = Path(args.path).expanduser()
    if not path.exists():
        print(f"没有找到用量文件：{path}", file=sys.stderr)
        return 1
    if path.is_dir():
        print(f"这是一个目录，不是用量文件：{path}", file=sys.stderr)
        return 1

    try:
        rows, total_prompt, bad_lines = summarize(path)
    except OSError as exc:
        print(f"无法读取用量文件 {path}: {exc}", file=sys.stderr)
        return 1

    if not rows:
        print(f"用量文件里没有可统计的记录：{path}")
        return 0

    header = ("ticker", "agent", "calls", "prompt", "completion", "prompt_share")
    widths = (max(len(header[0]), *(len(r[0]) for r in rows)),
              max(len(header[1]), *(len(r[1]) for r in rows)),
              len(header[2]), len(header[3]), len(header[4]), len(header[5]))
    print("  ".join(h.ljust(w) for h, w in zip(header, widths)))
    print("  ".join("-" * w for w in widths))
    for ticker, agent, calls, prompt, completion in rows:
        share = prompt / total_prompt if total_prompt else 0.0
        cells = (ticker, agent, str(calls), str(prompt), str(completion),
                 f"{share:.1%}")
        print("  ".join(c.ljust(w) for c, w in zip(cells, widths)))
    print()
    print(f"total prompt tokens: {total_prompt}")
    if bad_lines:
        print(f"skipped malformed lines: {bad_lines}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
