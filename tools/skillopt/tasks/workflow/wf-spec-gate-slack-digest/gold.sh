cat > "$GAC_FINAL" <<'MD'
I have not scaffolded anything: the design needs your decisions first.
1. Which GitHub organisation or repositories, and which read-only token may the agent use?
2. Which Slack channel, which bot token, and may the agent post without a person
   approving each message?
3. What should start the run every morning? The agent serves a chat API, so a scheduler
   (for example a Kubernetes CronJob) has to call it.
4. Which model provider may see the pull-request data?
5. Prototype first, or Kubernetes?
MD
