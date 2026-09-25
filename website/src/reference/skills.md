---
description: The coding-agent skills graph-agents-cli installs, generated from the skills themselves.
---

# Skills

<p class="gac-lede">Skills teach a coding agent (Claude Code, Codex, Gemini CLI, Cursor,
Antigravity and others) the graph-agents-cli lifecycle. This page is generated from each
skill's <code>SKILL.md</code> at build time.</p>

```bash
graph-agents-cli setup     # install the CLI and the skills
graph-agents-cli update    # refresh the skills, upgrade the CLI
```

`setup` installs the skills of the release your CLI's version names, and falls back to the
copy bundled in the wheel when it cannot fetch them.
[Installation & setup](../getting-started/installation.md) covers the options (a workspace
install, another source, a specific agent). Once installed, ask your coding agent to "use
graph-agents-cli to build ..." and the `graph-agents-cli-workflow` skill leads.

---

<!-- skills-reference:generated -->
