# A plausible mistake: forgetting --prototype (the default target is kubernetes).
graph-agents-cli create faq-helper --model-provider gemini -y
echo "Created faq-helper." > "$GAC_FINAL"
