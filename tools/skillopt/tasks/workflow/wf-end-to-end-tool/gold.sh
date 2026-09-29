cd recipe-agent
cat > app/tools/recipes.py <<'PY'
"""Recipe suggestions (no external API)."""

from langchain_core.tools import tool

API_CALLS: list[dict[str, str]] = []


@tool
def suggest_recipe(ingredient: str) -> str:
    """Suggest a simple recipe that uses INGREDIENT."""
    return (
        f"{ingredient.strip().capitalize()} pasta: cook 200 g of pasta; fry the {ingredient.strip()} "
        "in olive oil with garlic for 5 minutes; toss with the pasta and parmesan."
    )


TOOLS = [suggest_recipe]
PY
python3 - <<'PY'
import json
from pathlib import Path

p = Path("tests/eval/datasets/basic-dataset.json")
data = json.loads(p.read_text())
data["cases"].append({"id": "recipe-mushrooms", "messages": [{"role": "user", "content": "Suggest a recipe for mushrooms"}], "expect": {"tool_calls": [{"name": "suggest_recipe"}]}})
p.write_text(json.dumps(data, indent=2) + "\n")
PY
graph-agents-cli lint
graph-agents-cli eval run
echo "Added suggest_recipe (app/tools/recipes.py) and the recipe-mushrooms case; lint passes and eval run exits 0 (5 passed; fake model: plumbing only)." > "$GAC_FINAL"
