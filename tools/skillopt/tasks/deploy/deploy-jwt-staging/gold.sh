cd member-portal
graph-agents-cli deploy --env staging --dry-run --tag abc1234 || true
python3 - <<'PY'
from pathlib import Path

p = Path('deployment/helm/member-portal/values-staging.yaml')
t = p.read_text()
old = 'env:\n  APP_ENV: staging\n'
assert old in t
p.write_text(t.replace(old, old + '  AUTH_JWT_JWKS_URL: https://login.staging.acme.dev/.well-known/jwks.json\n  AUTH_JWT_ISSUER: https://login.staging.acme.dev/\n  AUTH_JWT_AUDIENCE: member-portal\n', 1))
PY
graph-agents-cli deploy --env staging --dry-run --tag abc1234
cat > "$GAC_FINAL" <<'MD'
Exit 3: the jwt policy had no JWKS URL, issuer or audience for staging. Set them in values-staging.yaml env; the staging dry run passes now.
MD
