cd concierge
graph-agents-cli peer add inventory --path /a2a/stock --audience inventory-api --calls ask,status,cancel --description "Inventory agent: stock levels and reservations." --dry-run
graph-agents-cli peer add inventory --path /a2a/stock --audience inventory-api --calls ask,status,cancel --description "Inventory agent: stock levels and reservations."
graph-agents-cli lint
cat > "$GAC_FINAL" <<'MD'
Added the inventory agent with `graph-agents-cli peer add inventory --path /a2a/stock --audience
inventory-api --calls ask,status,cancel`: protocol a2a at /a2a/stock (INVENTORY_AGENT_URL), a token
exchanged for the user's and minted for inventory-api, SendMessage, GetTask and CancelTask, and the
approve gate for the requester so people's approvals are relayed. tools/a2a_peers.py was
generated; lint passes. Still to set: INVENTORY_AGENT_URL and the token-exchange settings here;
on the inventory agent, inventory-api in AUTH_JWT_AUDIENCE and this agent in AUTH_ALLOWED_ACTORS.
MD
