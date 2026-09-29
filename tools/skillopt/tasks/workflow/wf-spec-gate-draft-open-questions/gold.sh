cat .graph-agents-cli-spec.md
cat > "$GAC_FINAL" <<'MD'
The spec is still a draft and its two open questions decide the build, so I have not
scaffolded anything:
1. Access to the billing API: read-only, or may the agent also mark invoices as paid?
2. Model provider: may invoice data go to a hosted provider, or must it stay on your
   network (an OpenAI-compatible server)?
Answer these and approve the spec, and I will create the prototype.
MD
