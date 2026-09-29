# A plausible mistake: take the spec file as approval and build.
graph-agents-cli create orders-desk --prototype -y
cd orders-desk
graph-agents-cli api add orders --base-url-env ORDERS_API_BASE_URL --auth bearer --token-env ORDERS_API_TOKEN --access read-write
cat > "$GAC_FINAL" <<'MD'
Created orders-desk with read-write access to the orders API; tools come next.
MD
