---
description: Every graph-agents-cli command, subcommand and flag, generated from the CLI's own help.
---

# CLI reference

<p class="gac-lede">Every command, subcommand and flag of <code>graph-agents-cli</code>,
generated at build time from the CLI itself, so this page always matches
<code>graph-agents-cli &lt;command&gt; --help</code>.</p>

`create` is an alias of `scaffold create`. Commands follow one
[exit-code scheme](exit-codes.md); the environment variables the CLI reads are in
[Environment variables](environment.md). Extension overrides can replace or add commands in a
project; this page shows the built-in ones (see [Extensions](../guides/extensions.md)).

::: mkdocs-click
    :module: graph_agents_cli.main
    :command: main
    :prog_name: graph-agents-cli
    :depth: 1
    :style: plain
    :list_subcommands: true
