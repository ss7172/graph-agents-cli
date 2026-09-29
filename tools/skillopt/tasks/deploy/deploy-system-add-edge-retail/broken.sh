# A plausible mistake: peer add in the caller only; the file and loyalty-agent stay as they were.
cd retail/storefront
graph-agents-cli peer add loyalty --calls ask,status,cancel --description "Loyalty agent: points."
graph-agents-cli api check
cat > "$GAC_FINAL" <<'MD'
storefront now calls loyalty (`peer add loyalty --calls ask,status,cancel`).
MD
