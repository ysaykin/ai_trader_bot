"""Apply the shared color rule to numeric UI bindings. Safe to rerun.

Run from the repository root with Python 3.12. This edits component source only.
"""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPONENTS = ROOT / "reflex_dashboard/reflex_dashboard/components"
FIELDS = {"net_pnl_str", "gross_pnl_str", "net_ror_pct", "net_ror_str", "net_roc_pct",
          "fees_str", "avg_trade_str", "unrealized_pl", "quantity", "cost_basis",
          "delta", "theta", "last_price", "market_value", "win_rate"}
ATTRS = {"equity_str", "cash_str", "buying_power_str", "option_requirement_str",
         "total_net_pnl_str", "total_gross_pnl_str", "total_fees_str", "overall_win_rate"}


def normalize(path):
    source = path.read_text()
    lines = source.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    replacements = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call) or not node.args or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in {"text", "heading"}:
            continue
        first = node.args[0]
        already_formatted = isinstance(first, ast.Call) and isinstance(first.func, ast.Name) and first.func.id == "numeric_text"
        value = first.args[0] if already_formatted else first
        attr = value.attr if isinstance(value, ast.Attribute) else ""
        key = value.slice.value if isinstance(value, ast.Subscript) and isinstance(value.slice, ast.Constant) else ""
        if key not in FIELDS and attr not in ATTRS and not attr.startswith("benchmark_"):
            continue
        color = next((kw for kw in node.keywords if kw.arg == "color"), None)
        if not already_formatted:
            start = offsets[value.lineno-1] + len(lines[value.lineno-1].encode()[:value.col_offset].decode())
            end = offsets[value.end_lineno-1] + len(lines[value.end_lineno-1].encode()[:value.end_col_offset].decode())
            replacements.append((start, end, f"numeric_text({ast.get_source_segment(source, value)})"))
        if color and isinstance(color.value, ast.Call) and isinstance(color.value.func, ast.Name) and color.value.func.id == "numeric_color":
            continue
        expression = f"numeric_color({ast.get_source_segment(source, value)})"
        if color and ast.unparse(color.value) != expression:
            start = offsets[color.value.lineno-1] + len(lines[color.value.lineno-1].encode()[:color.value.col_offset].decode())
            end = offsets[color.value.end_lineno-1] + len(lines[color.value.end_lineno-1].encode()[:color.value.end_col_offset].decode())
            replacements.append((start, end, expression))
    for start, end, replacement in sorted(replacements, reverse=True):
        source = source[:start] + replacement + source[end:]
    if replacements:
        import_line = "from ..styles.numeric import numeric_color, numeric_text"
        if import_line not in source:
            if "from ..styles.numeric import numeric_color" in source:
                source = source.replace("from ..styles.numeric import numeric_color", import_line, 1)
            else:
                source = source.replace("import reflex as rx\n", f"import reflex as rx\n{import_line}\n", 1)
        path.write_text(source)
    return len(replacements)


if __name__ == "__main__":
    for path in sorted(COMPONENTS.glob("*.py")):
        count = normalize(path)
        if count:
            print(f"{path.relative_to(ROOT)}: {count} numeric bindings")
