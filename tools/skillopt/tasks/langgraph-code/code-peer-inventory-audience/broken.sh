# A plausible mistake: the default audience (the peer's name) instead of the one it accepts.
cd concierge
graph-agents-cli peer add inventory --path /a2a/stock --calls ask,status,cancel --description "Inventory agent: stock levels and reservations."
graph-agents-cli lint
cat > "$GAC_FINAL" <<'MD'
Added the inventory agent with `graph-agents-cli peer add inventory --path /a2a/stock`; lint passes.
MD
