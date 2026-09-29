# A plausible mistake: the tool is defined but neither declared nor registered.
cd util-agent
cat > app/tools/temperature.py <<'PY'
"""Temperature conversion (no external API)."""

from langchain_core.tools import tool


@tool
def convert_temperature(value: float, unit: str) -> str:
    """Convert VALUE between Celsius and Fahrenheit: unit "C" converts Celsius to Fahrenheit,
    unit "F" converts Fahrenheit to Celsius."""
    if unit.strip().upper() == "C":
        return f"{value:g} C = {value * 9 / 5 + 32:g} F"
    if unit.strip().upper() == "F":
        return f"{value:g} F = {(value - 32) * 5 / 9:g} C"
    return f"Unknown unit {unit!r}: use C or F."
PY
graph-agents-cli lint || true
echo "Added convert_temperature." > "$GAC_FINAL"
