"""Offline decoder using the existing AKShare decoder and installed MiniRacer.

Run with the repository root interpreter. Network text is passed as data, never
evaluated as JavaScript. AKShare's installed decoder is recorded by SHA-256.
"""
import argparse
import ast
import hashlib
import importlib.util
import json
import re
from pathlib import Path

import py_mini_racer


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    spec = importlib.util.find_spec("akshare")
    constants = Path(spec.origin).parent / "stock" / "cons.py"
    decoder = None
    for node in ast.parse(constants.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "zh_js_decode"
                                               for t in node.targets):
            decoder = ast.literal_eval(node.value)
    if not isinstance(decoder, str):
        raise TypeError("installed decoder not found")
    args.output.mkdir(parents=True, exist_ok=False)
    records = {}
    with py_mini_racer.MiniRacer() as context:
        context.eval(decoder)
        for path in sorted(args.source.glob("*.static.response")):
            match = re.fullmatch(r'\s*var\s+\w+\s*=\s*"([^"]+)"\s*;?\s*', path.read_text())
            if match is None:
                raise ValueError(f"unexpected encoded wrapper: {path.name}")
            rows = context.call("d", match.group(1))
            symbol = path.name.split(".")[0]
            target = args.output / f"{symbol}.decoded.json"
            target.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
            records[symbol] = {"rows": len(rows), "source_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                               "decoded_sha256": hashlib.sha256(target.read_bytes()).hexdigest()}
            print(symbol, len(rows), rows[0], rows[-1], flush=True)
    (args.output / "manifest.json").write_text(json.dumps({"decoder_file": str(constants),
        "decoder_sha256": hashlib.sha256(decoder.encode()).hexdigest(), "assets": records}), encoding="utf-8")


if __name__ == "__main__":
    main()
