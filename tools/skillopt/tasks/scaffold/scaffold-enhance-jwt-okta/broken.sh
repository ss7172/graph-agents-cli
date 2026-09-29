# A plausible mistake: flip the manifest by hand.
cd tickets-agent
python3 - <<'PY'
from pathlib import Path

p = Path('graph-agents-cli-manifest.yaml')
t = p.read_text()
assert "auth_policy: 'shared-bearer'" in t
p.write_text(t.replace("auth_policy: 'shared-bearer'", "auth_policy: 'jwt'"))
PY
cat > "$GAC_FINAL" <<'MD'
Set auth_policy to jwt.
MD
