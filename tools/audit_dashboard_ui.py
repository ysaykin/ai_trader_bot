"""List dashboard review candidates without importing the app or contacting a broker.

Run with: python3 tools/audit_dashboard_ui.py
This source audit identifies review candidates, not verified runtime defects.
Browser checks must confirm displayed values, styles and interactions.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "reflex_dashboard/reflex_dashboard"
SIGNED_FIELDS = {
    "unrealized_pl", "gross_pnl_str", "net_pnl_str", "net_ror_pct",
    "net_ror_str", "net_roc_pct", "avg_trade_str", "fees_str", "delta",
    "theta", "quantity", "cost_basis",
}
NUMBER = re.compile(r"^[+-]?(?:\$)?\d[\d,.]*(?:%|\s|$)")


def field_name(value: ast.expr) -> str | None:
    if isinstance(value, ast.Subscript) and isinstance(value.slice, ast.Constant):
        return str(value.slice.value)
    if isinstance(value, ast.Attribute):
        return value.attr
    return None


def location(path: Path, node: ast.AST, **details: object) -> dict[str, object]:
    return {"file": str(path.relative_to(ROOT)), "line": node.lineno, **details}


def audit() -> dict[str, object]:
    colors = []
    constants = []
    signals = []
    for path in sorted((APP / "components").glob("*.py")):
        source = path.read_text()
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            if not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in {"text", "heading"}:
                continue
            value = node.args[0]
            color = next((kw.value for kw in node.keywords if kw.arg == "color"), None)
            literal = value.value if isinstance(value, ast.Constant) else None
            if isinstance(literal, str) and NUMBER.match(literal):
                constants.append(location(path, node, value=literal))
            numeric = field_name(value) in SIGNED_FIELDS
            negative = isinstance(literal, str) and bool(re.match(r"^-\$?\d", literal))
            if numeric or negative:
                colors.append(location(
                    path, node,
                    value=ast.unparse(value),
                    color=ast.unparse(color) if color is not None else "inherited",
                    reason="Check numeric sign; status/category colors do not prove sign handling.",
                ))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if any(text in node.value for text in (
                "All 14 completed", "Broker confirms 0", "100.0% Available",
                "$5,000", "30.0% Max Loss",
            )):
                signals.append(location(path, node, reason="Fixed status/risk text", value=node.value))

    adapter = APP / "data/adapters.py"
    source = adapter.read_text()
    tree = ast.parse(source, filename=str(adapter))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            is_mark = any(isinstance(t, ast.Name) and t.id == "mark" for t in node.targets)
            if is_mark and "cost_basis" in ast.unparse(node.value):
                signals.append(location(adapter, node, reason="Missing mark falls back to entry cost"))

    state = APP / "state.py"
    tree = ast.parse(state.read_text(), filename=str(state))
    example_fields = {"equity", "cash", "buying_power", "ab_card_a", "ab_card_b", "market_regime"}
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id in example_fields and node.value is not None:
                signals.append(location(state, node, reason="Review populated startup default", field=node.target.id))

    return {
        "scope": "Static review of Reflex numeric displays and selected data-state risks",
        "limitations": "Candidates require human/browser review. This does not validate broker values, CSS or task completion.",
        "sign_format_candidates": sorted(colors, key=lambda row: (row["file"], row["line"])),
        "literal_metric_candidates": sorted(constants, key=lambda row: (row["file"], row["line"])),
        "data_state_candidates": sorted(signals, key=lambda row: (row["file"], row["line"])),
    }


if __name__ == "__main__":
    print(json.dumps(audit(), indent=2))
