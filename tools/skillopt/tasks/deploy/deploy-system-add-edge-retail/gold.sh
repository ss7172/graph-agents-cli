cd retail
graph-agents-cli system check
python3 - <<'PY'
import pathlib
p = pathlib.Path("graph-agents-system.yaml")
text = p.read_text()
text = text.replace(
    "    calls: [orders]\n",
    "    calls:\n      - orders\n      - {agent: loyalty, calls: [ask, status, cancel]}\n",
)
text = text.replace(
    "  orders:\n    project: orders-agent\n",
    "  orders:\n    project: orders-agent\n  loyalty:\n    project: loyalty-agent\n",
)
p.write_text(text)
PY
cat graph-agents-system.yaml
graph-agents-cli system apply --dry-run
graph-agents-cli system apply
graph-agents-cli system check || true
python3 - <<'PY'
import pathlib, re
p = pathlib.Path("loyalty-agent/deployment/helm/loyalty-agent/values-dev.yaml")
p.write_text(re.sub(r"^env:\n", "env:\n  AUTH_JWT_ISSUER: https://login.retail.example\n", p.read_text(), count=1, flags=re.M))
PY
graph-agents-cli system check
(cd storefront && graph-agents-cli api check)
cat > "$GAC_FINAL" <<'MD'
Added loyalty-agent to retail/graph-agents-system.yaml (the agent loyalty, and storefront's edge to
it with calls ask, status and cancel) and ran `graph-agents-cli system apply`: storefront has the
loyalty_agent peer at loyalty-agent's A2A mount /a2a/app (SendMessage, GetTask, CancelTask; a
token exchanged for the user's, audience loyalty), and loyalty-agent lists storefront's issuer
client id web-storefront in AUTH_ALLOWED_ACTORS, with its audience and appUrl for dev. I set
AUTH_JWT_ISSUER in loyalty-agent's values-dev.yaml (SC04); `system check` reports no error, and
the orders wiring is unchanged. Still to do: let web-storefront exchange for loyalty at the issuer
(`system delegations`) and review the relay line loyalty-agent's owners must run.
MD
