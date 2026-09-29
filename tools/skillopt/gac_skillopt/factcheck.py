# Copyright 2026 graph-agents-cli contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""The candidate gate (DESIGN section 6): machine-checkable facts a skill body must keep.

A candidate that fails is never rolled out: every item scores 0 with the violations as the
failure reason, so SkillOpt's gate rejects it and the analysts learn why. Checks:

1. the ``## Not covered`` and ``## Migration note`` sections exist; Google Cloud product names
   appear only under a heading containing "Migration" (the rules of tests/skills/test_bundle.py);
2. every `` `references/<file>.md` `` it names exists, and every reference file is still named;
3. every ``graph-agents-cli ...`` invocation in code resolves in the real command tree (the
   subcommand exists; every option is a visible option of it), and every
   ``GRAPH_AGENTS_CLI_*`` variable exists in the CLI;
4. its size stays within ``max_growth`` of the shipped body.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from functools import cache
from pathlib import Path

from .paths import TOOL_DIR, Bench
from .tasks import initial_body, skill_source

# Kept in step with tests/skills/test_bundle.py (FORBIDDEN_TERMS).
FORBIDDEN_TERMS = (
    "Google Cloud",
    "Vertex",
    "Cloud Run",
    "GKE",
    "Agent Runtime",
    "Agent Engine",
    "Cloud Trace",
    "Cloud Logging",
    "Cloud Build",
    "BigQuery",
    "Secret Manager",
    "Gemini Enterprise",
    "gcloud",
    "GCS",
    "ADK",
)
MAX_GROWTH = 1.25
STOP_TOKENS = {"|", "||", "&&", ";", ")", "(", ">", ">>", "<", "2>&1", "#", "\\"}


@cache
def command_tree(cli_python: str) -> dict:
    """The CLI's commands and variables, from the CLI build's own interpreter."""
    proc = subprocess.run(
        [cli_python, str(TOOL_DIR / "gac_skillopt" / "clitree.py")],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "GRAPH_AGENTS_CLI_NO_UPDATE_CHECK": "1"},
    )
    return json.loads(proc.stdout)


def _sections(text: str) -> list[tuple[str, str]]:
    sections: list[tuple[str, str]] = []
    heading, buf = "", []
    in_fence = False
    for line in text.splitlines():
        if line.startswith("```"):
            in_fence = not in_fence
        if line.startswith("#") and not in_fence:
            sections.append((heading, "\n".join(buf)))
            heading, buf = line, []
        else:
            buf.append(line)
    sections.append((heading, "\n".join(buf)))
    return sections


def code_snippets(text: str) -> list[str]:
    """Fenced blocks (line by line, continuation lines joined) and inline code spans."""
    snippets: list[str] = []
    fences = re.findall(r"^```[^\n]*\n(.*?)^```", text, re.M | re.S)
    for block in fences:
        joined = re.sub(r"\\\n\s*", " ", block)
        snippets.extend(joined.splitlines())
    outside = re.sub(r"^```[^\n]*\n.*?^```", "", text, flags=re.M | re.S)
    snippets.extend(re.findall(r"``?([^`\n]+)``?", outside))
    return snippets


def invocations(snippet: str) -> list[list[str]]:
    """Token lists following each ``graph-agents-cli`` in a snippet."""
    out = []
    for match in re.finditer(r"(?<![\w/-])graph-agents-cli(?![\w-])", snippet):
        rest = snippet[match.end() :]
        try:
            tokens = shlex.split(rest, comments=False, posix=True)
        except ValueError:
            tokens = rest.split()
        kept = []
        for tok in tokens:
            if tok in STOP_TOKENS or tok.startswith(("#", "`", "$(")) or tok.endswith(")"):
                break
            kept.append(tok)
        out.append(kept)
    return out


def check_invocation(tokens: list[str], tree: dict) -> str | None:
    commands = tree["commands"]
    path: list[str] = []
    i = 0
    while i < len(tokens) and " ".join([*path, tokens[i]]) in commands:
        path.append(tokens[i])
        i += 1
    cmd = " ".join(path)
    info = commands.get(cmd)
    if info is None:
        return None
    if info["group"] and i < len(tokens):
        nxt = tokens[i]
        if re.fullmatch(r"[a-z][a-z0-9-]*", nxt) and nxt not in ("...",):
            return f"`graph-agents-cli {' '.join([*path, nxt])}`: no such command"
    for tok in tokens[i:]:
        tok = tok.strip("[]{}(),.")
        for part in tok.split("|"):
            if not re.fullmatch(r"--?[A-Za-z][\w-]*(=.*)?", part):
                continue
            name = part.split("=", 1)[0]
            if name in ("--version",) and not path:
                continue
            if name in info["hidden"]:
                return f"`graph-agents-cli {cmd}` documents the hidden option {name}"
            if name not in info["options"]:
                return f"`graph-agents-cli {cmd}` has no option {name}"
    return None


def check_candidate(
    skill: str, body: str, *, bench: Bench | None = None, cli_python: str | None = None
) -> list[str]:
    """Violations of the rules above (empty: the candidate may be rolled out)."""
    problems: list[str] = []
    if not re.search(r"^## Not covered", body, re.M):
        problems.append("the '## Not covered by this skill' section is missing")
    if not re.search(r"^## Migration note", body, re.M):
        problems.append("the '## Migration note' section is missing")
    for heading, text in _sections(body):
        if "migration" in heading.lower():
            continue
        for term in FORBIDDEN_TERMS:
            if re.search(rf"\b{re.escape(term)}\b", text):
                problems.append(
                    f"{term!r} appears under {heading or 'the top'!r} (only migration notes may name it)"
                )
    mentioned = set(re.findall(r"`references/([A-Za-z0-9_.-]+\.md)`", body))
    present = {p.name for p in (skill_source(skill) / "references").glob("*.md")}
    for name in sorted(mentioned - present):
        problems.append(f"references/{name} does not exist")
    for name in sorted(present - mentioned):
        problems.append(f"references/{name} is no longer mentioned")
    python = cli_python or (str(bench.cli_python) if bench is not None else None)
    if python:
        tree = command_tree(python)
        seen: set[str] = set()
        for snippet in code_snippets(body):
            for tokens in invocations(snippet):
                problem = check_invocation(tokens, tree)
                if problem and problem not in seen:
                    seen.add(problem)
                    problems.append(problem)
        known = set(tree["env_vars"])
        for var in sorted(set(re.findall(r"GRAPH_AGENTS_CLI_[A-Z0-9_]*[A-Z0-9]", body)) - known):
            problems.append(f"{var} is not a variable the CLI reads")
    original = len(initial_body(skill))
    if len(body) > MAX_GROWTH * original:
        problems.append(
            f"the body grew to {len(body)} characters, over {MAX_GROWTH:.2f} x the shipped {original}"
        )
    return problems


def main() -> int:  # pragma: no cover - a manual check: the shipped skills must pass
    import argparse

    from .paths import SKILLS

    ap = argparse.ArgumentParser()
    ap.add_argument("--scratch")
    ap.add_argument("--skill", action="append")
    ap.add_argument("--body", type=Path, help="check this body file instead of the shipped one")
    args = ap.parse_args()
    bench = Bench.from_env(args.scratch)
    bad = 0
    for skill in args.skill or SKILLS:
        body = args.body.read_text() if args.body else initial_body(skill)
        problems = check_candidate(skill, body, bench=bench)
        print(f"{skill}: {'ok' if not problems else ''}")
        for p in problems:
            print(f"  - {p}")
        bad += bool(problems)
    return 1 if bad else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
