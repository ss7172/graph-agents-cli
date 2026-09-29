cat > .graph-agents-cli-spec.md <<'MD'
# Reply drafter: draft spec (NOT approved)

A prototype agent that drafts (never sends) replies to customer support emails.
Open: email source, model provider.
MD
cat > "$GAC_FINAL" <<'MD'
Short spec drafted in .graph-agents-cli-spec.md; I stopped there because it needs
your approval. Two quick questions:
1. Where do the emails come from: pasted into the chat, or read from a mailbox or
   help-desk API (which one, read-only)?
2. Which model provider may see your customers' emails?
Once you approve, I will scaffold it with --prototype.
MD
