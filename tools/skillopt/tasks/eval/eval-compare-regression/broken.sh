cd faq-agent
graph-agents-cli eval compare reports/before.json reports/after.json
printf 'weather\n' > regressions.txt
echo "One case regressed (weather). Not safe to merge." > "$GAC_FINAL"
