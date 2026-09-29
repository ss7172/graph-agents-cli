graph-agents-cli create netops-agent --model-provider openai-compatible --model qwen2.5:14b --runtime fastapi --deployment-target kubernetes --registry harbor.corp.internal/agents --cd skip -y
python3 - <<'PY'
from pathlib import Path

p = Path('netops-agent/deployment/helm/netops-agent/values.yaml')
t = p.read_text()
assert 'OPENAI_BASE_URL: http://CHANGE-ME:11434/v1' in t
p.write_text(t.replace('OPENAI_BASE_URL: http://CHANGE-ME:11434/v1', 'OPENAI_BASE_URL: http://vllm.ml.svc:8000/v1'))
PY
cat > "$GAC_FINAL" <<'MD'
Created netops-agent (openai-compatible qwen2.5:14b, fastapi, kubernetes, harbor.corp.internal/agents, no CD) and pointed the chart's OPENAI_BASE_URL at http://vllm.ml.svc:8000/v1.
MD
