---
description: Known medium- and low-priority issues in graph-agents-cli, each with its impact and workaround.
toc_depth: 2
---

<!--
  This page includes sections of KNOWN_ISSUES.md from the repository root, unchanged
  (pymdownx.snippets section markers: intro, summary, entries). Edit KNOWN_ISSUES.md, never
  the included text here. hooks/repo_links.py turns the file's repository-relative links
  into links to site pages or to GitHub; toc_depth keeps the entries out of the sidebar.
-->

# Known issues

--8<-- "KNOWN_ISSUES.md:intro"

**Medium** marks security-relevant, data-integrity or production-operations edge cases;
**Low** marks developer experience, docs, output polish and cosmetic issues. "Found in" names
what first reported an issue: a pre-release review round of 0.2.0, one of the experiments run
since, or a phase of the 0.3 release's work. How an entry is triaged
and graduates into a release is in
[KNOWN_ISSUES.md](https://github.com/ss7172/graph-agents-cli/blob/main/KNOWN_ISSUES.md#triage-and-how-an-issue-graduates)
on GitHub; a fixed entry leaves this page and its fix is recorded in the
[changelog](changelog.md) with its `KI-` id.

--8<-- "KNOWN_ISSUES.md:summary"

--8<-- "KNOWN_ISSUES.md:entries"
