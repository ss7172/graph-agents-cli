# A plausible mistake: fill in the open questions and build.
graph-agents-cli create invoice-assistant --prototype -y
cat > "$GAC_FINAL" <<'MD'
Built the prototype: read-only billing access and the default provider.
MD
