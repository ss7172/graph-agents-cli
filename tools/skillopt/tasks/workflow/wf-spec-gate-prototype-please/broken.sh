# A plausible mistake: "prototype" taken as licence to skip the spec.
graph-agents-cli create reply-drafter --prototype -y
cat > "$GAC_FINAL" <<'MD'
Created the prototype reply-drafter; edit app/agent.py's prompt next.
MD
