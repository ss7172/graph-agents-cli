cd util-agent
cat > app/tools/temperature.py <<'PY'
"""Temperature conversion (no external API)."""

from langchain_core.tools import tool

API_CALLS: list[dict[str, str]] = []


@tool
def convert_temperature(value: float, unit: str) -> str:
    """Convert VALUE between Celsius and Fahrenheit: unit "C" converts Celsius to Fahrenheit,
    unit "F" converts Fahrenheit to Celsius."""
    if unit.strip().upper() == "C":
        return f"{value:g} C = {value * 9 / 5 + 32:g} F"
    if unit.strip().upper() == "F":
        return f"{value:g} F = {(value - 32) * 5 / 9:g} C"
    return f"Unknown unit {unit!r}: use C or F."


TOOLS = [convert_temperature]
PY
graph-agents-cli lint
echo "Added app/tools/temperature.py (convert_temperature, API_CALLS = [], TOOLS); lint passes." > "$GAC_FINAL"
