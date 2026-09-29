cd claims-desk
cat docs/process.md
mkdir -p docs/stories
cat > docs/stories/utc-time-tool.md <<'MD'
# Story: tell users the current UTC time

Status: proposed

## Goal

Users can ask the agent what time it is and get the current time in UTC.

## Acceptance criteria

- A tool `get_utc_time` returns the current UTC time in ISO 8601; it calls no external API
  (`API_CALLS = []`).
- An eval case asks "What time is it in UTC?" and expects a call to `get_utc_time`.
- `graph-agents-cli lint` and `graph-agents-cli eval run` pass.
MD
echo "This project follows docs/process.md: I wrote docs/stories/utc-time-tool.md (Status: proposed) and wrote no code. Once the product owner sets it to approved, I will implement it." > "$GAC_FINAL"
