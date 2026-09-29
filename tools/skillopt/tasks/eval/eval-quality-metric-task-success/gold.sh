cd review-agent
python3 - <<'PY'
import json
from pathlib import Path

cfg = Path("tests/eval/eval_config.yaml")
text = cfg.read_text()
line = "  response_quality: { threshold: 4, min_pass_rate: 0.9 }\n"
assert line in text
cfg.write_text(text.replace(line, line + "  task_success: { threshold: 4, min_pass_rate: 0.8 }\n"))
ds = Path("tests/eval/datasets/basic-dataset.json")
data = json.loads(ds.read_text())
for case in data["cases"]:
    if case["id"] in ("weather", "weather-follow-up"):
        case.setdefault("judge", {})["task_success"] = {"threshold": 4}
ds.write_text(json.dumps(data, indent=2) + "\n")
PY
graph-agents-cli eval run
echo "task_success is a quality metric (threshold 4, min_pass_rate 0.8) on weather and weather-follow-up; eval run exit 0 on the fake model (plumbing only)." > "$GAC_FINAL"
