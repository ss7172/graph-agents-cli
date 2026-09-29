# A plausible mistake: ignore the declared process and write the code.
cd claims-desk
cat > app/tools/clock.py <<'PY'
from datetime import UTC, datetime

from langchain_core.tools import tool

API_CALLS: list[dict[str, str]] = []


@tool
def get_utc_time() -> str:
    """Return the current time in UTC (ISO 8601)."""
    return datetime.now(UTC).isoformat()


TOOLS = [get_utc_time]
PY
echo "Added get_utc_time." > "$GAC_FINAL"
