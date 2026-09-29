graph-agents-cli create cache-agent --deployment-target kubernetes --checkpointer memory -y || true
cat > "$GAC_FINAL" <<'MD'
I did not create it: `create` refuses kubernetes with the in-memory checkpointer (exit 2), because
restarts and multiple replicas would lose every conversation. The valid options are Postgres on
Kubernetes (the dev values bundle a Postgres subchart; staging and prod need an external one), or a
prototype with the in-memory checkpointer (`--prototype`) until Postgres exists. Tell me which.
MD
