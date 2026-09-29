cd faq-agent
graph-agents-cli eval compare reports/before.json reports/after.json
printf 'weather\nweather-follow-up\n' > regressions.txt
echo "weather and weather-follow-up regressed (passed -> failed; exit code 0 -> 1). Not safe to merge: the gate fails." > "$GAC_FINAL"
