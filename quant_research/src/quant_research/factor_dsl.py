"""S8 restricted AST interpreter and durable trial budget. No eval/exec or I/O DSL."""
import ast
import sqlite3
from pathlib import Path
from uuid import uuid4

import numpy as np
import pandas as pd

from .artifacts import canonical, digest, utc_now
from .factors import SPEC

WINDOWS = {5, 10, 21, 63, 126, 252}
ARITY = {"rank": 1, "zscore": 1, "lag": 2, "rolling_mean": 2, "std": 2,
         "min": 2, "max": 2, "corr": 3, "safe_div": 2}


def parse(expression):
    if not isinstance(expression, str) or len(expression) > 2048:
        raise ValueError("expression too long")
    tree = ast.parse(expression, mode="eval")
    if sum(1 for _ in ast.walk(tree)) > 64:
        raise ValueError("expression too large")
    def inspect(node, depth=0):
        if depth > 4:
            raise ValueError("expression exceeds depth four")
        if isinstance(node, ast.Name) and node.id in SPEC:
            return
        if isinstance(node, ast.Constant) and type(node.value) in (float, int):
            if not np.isfinite(node.value) or abs(node.value) > 1e6:
                raise ValueError("invalid numeric constant")
            return
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult)):
            inspect(node.left, depth+1)
            inspect(node.right, depth+1)
            return
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            inspect(node.operand, depth+1)
            return
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ARITY:
            name = node.func.id
            if node.keywords or len(node.args) != ARITY[name]:
                raise ValueError("invalid function arguments")
            if name in {"lag", "rolling_mean", "std", "min", "max", "corr"}:
                window = node.args[-1]
                if not isinstance(window, ast.Constant) or type(window.value) is not int:
                    raise ValueError("literal integer window required")
                if name == "lag":
                    if not 0 <= window.value <= 252:
                        raise ValueError("lag must be between zero and 252")
                elif window.value not in WINDOWS:
                    raise ValueError("unregistered rolling window")
            for argument in node.args:
                inspect(argument, depth+1)
            return
        raise ValueError("forbidden syntax or unregistered feature")
    inspect(tree.body)
    return tree


def evaluate(expression, features):
    tree = parse(expression)
    required = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and n.id in SPEC}
    if not required or not required.issubset(features):
        raise ValueError("missing registered inputs")
    template = features[min(required)]
    if not isinstance(template.index, pd.DatetimeIndex) or template.index.has_duplicates or not template.index.is_monotonic_increasing:
        raise ValueError("ordered unique session index required")
    if template.columns.has_duplicates:
        raise ValueError("duplicate securities")
    for key in required:
        frame = features[key]
        if not frame.index.equals(template.index) or not frame.columns.equals(template.columns):
            raise ValueError("unaligned feature panels")
        if not all(np.issubdtype(t, np.number) for t in frame.dtypes):
            raise ValueError("numeric feature panels required")
    def visit(node):
        if isinstance(node, ast.Name):
            return features[node.id].copy()
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.UnaryOp):
            value = visit(node.operand)
            return -value if isinstance(node.op, ast.USub) else value
        if isinstance(node, ast.BinOp):
            left, right = visit(node.left), visit(node.right)
            if isinstance(node.op, ast.Add):
                return left+right
            return left-right if isinstance(node.op, ast.Sub) else left*right
        name, args = node.func.id, [visit(a) for a in node.args]
        first = args[0]
        if not isinstance(first, pd.DataFrame):
            raise TypeError("first function argument must be a feature panel")
        if name == "rank":
            return first.rank(axis=1, pct=True)
        if name == "zscore":
            return first.sub(first.mean(axis=1), axis=0).div(first.std(axis=1, ddof=0).replace(0, np.nan), axis=0)
        if name == "lag":
            return first.shift(args[1])
        if name == "safe_div":
            denominator = args[1]
            if isinstance(denominator, pd.DataFrame):
                denominator = denominator.where(denominator.abs() > 1e-12)
            elif abs(denominator) <= 1e-12:
                denominator = np.nan
            return first/denominator
        if name == "corr":
            if not isinstance(args[1], pd.DataFrame):
                raise ValueError("corr requires two panels")
            return first.rolling(args[2], min_periods=args[2]).corr(args[1])
        rolling = first.rolling(args[1], min_periods=args[1])
        if name == "rolling_mean":
            return rolling.mean()
        if name == "std":
            return rolling.std(ddof=1)
        return rolling.min() if name == "min" else rolling.max()
    result = visit(tree.body)
    if not isinstance(result, pd.DataFrame):
        raise TypeError("expression must produce a panel")
    return result.replace([np.inf, -np.inf], np.nan)


class TrialLedger:
    """One durable 30-attempt campaign; failed and duplicate attempts also count.

    A new file is a new campaign, never a claimed continuation of the old budget.
    Caller archives this file with experiment results. Not an OS sandbox.
    """
    def __init__(self, path, contract):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS campaign (id INTEGER PRIMARY KEY CHECK(id=1), contract TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS trials (id TEXT PRIMARY KEY, expression TEXT, expression_hash TEXT, "
                       "status TEXT, detail TEXT, created_at TEXT)")
            frozen = canonical(contract)
            db.execute("INSERT OR IGNORE INTO campaign VALUES (1, ?)", (frozen,))
            if db.execute("SELECT contract FROM campaign").fetchone()[0] != frozen:
                raise ValueError("campaign contract changed; existing budget cannot be reset")

    def attempt(self, expression, features):
        trial_id = "factor_"+uuid4().hex
        try:
            expression_hash = digest(ast.dump(parse(expression), include_attributes=False))
        except (ValueError, SyntaxError, TypeError):
            expression_hash = digest(str(expression))
        with sqlite3.connect(self.path, timeout=30) as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT COUNT(*) FROM trials").fetchone()[0] >= 30:
                raise ValueError("30-attempt budget exhausted")
            duplicate = db.execute("SELECT id FROM trials WHERE expression_hash=?", (expression_hash,)).fetchone()
            db.execute("INSERT INTO trials VALUES (?, ?, ?, ?, ?, ?)",
                       (trial_id, str(expression), expression_hash, "duplicate" if duplicate else "running",
                        duplicate[0] if duplicate else "", utc_now()))
        if duplicate:
            return {"trial_id": trial_id, "status": "duplicate", "duplicate_of": duplicate[0]}, None
        try:
            values = evaluate(expression, features)
            result = {"trial_id": trial_id, "status": "evaluated", "finite_values": int(values.notna().sum().sum())}
        except (ValueError, SyntaxError, TypeError, OverflowError) as exc:
            values = None
            result = {"trial_id": trial_id, "status": "rejected", "reason": str(exc)}
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE trials SET status=?, detail=? WHERE id=?", (result["status"], canonical(result), trial_id))
        return result, values
