---
description: gac-bench, the contributor benchmark that measures and improves the graph-agents-cli skills with SkillOpt, Claude Code and Codex.
---

# Skills benchmark

<p class="gac-lede">For contributors who change a skill. gac-bench runs coding agents on
realistic graph-agents-cli tasks with the skill under test, scores each run with a
deterministic verifier, and lets <a href="https://github.com/microsoft/SkillOpt">SkillOpt</a>
propose edits to a skill's text. It lives in the repository under
<code>tools/skillopt/</code>.</p>

!!! note "Contributor tooling, never shipped"
    Nothing under `tools/` is a dependency of the CLI or of a generated project, and none of
    it is in the wheel or the sdist: the wheel packages `src/graph_agents_cli` only, and the
    sdist includes a fixed list of paths without `tools/`. An optimised skill replaces a
    shipped one only after a person reviews the diff.

## What it holds

| Part | What it is |
|---|---|
| Tasks | `tools/skillopt/tasks/<skill>/<id>/`: a fixture (a project `create`d with given flags, plus files and setup commands), a prompt, deterministic checks, and two scripted solutions: `gold.sh`, which the checks must accept, and `broken.sh`, a plausible mistake they must reject |
| Splits | `tools/skillopt/splits/<skill>.json`: frozen train, val and test tasks per skill, with a content hash per task |
| `gac_skillopt` | The SkillOpt environment: it installs a candidate `SKILL.md` as the only skill of Claude Code or Codex, runs a task in an isolated, sandboxed workspace, scores it and writes the trajectory SkillOpt reflects on; plus the runner, baseline and preflight commands |
| Results | `tools/skillopt/results/`: every measurement and human review so far |

The [README](https://github.com/ss7172/graph-agents-cli/blob/main/tools/skillopt/README.md)
is how to run it; [DESIGN.md](https://github.com/ss7172/graph-agents-cli/blob/main/tools/skillopt/DESIGN.md)
records the decisions, the isolation evidence for both harnesses and the budget rules.

## Check the benchmark (no model calls)

The unit tests need no network, model or SkillOpt install, and CI runs them on every pull
request. They also check every task's schema, its split and its frozen hash:

```bash
uv run pytest tools/skillopt/tests -q        # from the repository root
```

A change to the template or the CLI can break a task's scripted solution. `selfcheck` runs,
for every task, the gold solution (it must score `hard=1`), the broken one (it must fail
exactly the checks the task lists in `broken_fails`) and the untouched fixture (it must score
`hard=0`). It needs macOS (the sandbox behaviour was verified there), Python 3.12, `uv` and
`helm`, and builds the CLI from your checkout into a scratch directory outside it:

```bash
S=/path/to/scratch                                   # outside the checkout
uv venv --python 3.12 $S/.venv
VIRTUAL_ENV=$S/.venv uv pip install -r tools/skillopt/requirements.txt
cd tools/skillopt
$S/.venv/bin/python -m gac_skillopt setup --scratch $S       # CLI build, current uv, warm caches
$S/.venv/bin/python -m gac_skillopt validate                 # schemas, splits, frozen hashes
$S/.venv/bin/python -m gac_skillopt selfcheck --scratch $S --slots 8
```

Every other command refuses a scratch CLI that no longer matches the checkout; `setup
--rebuild-cli` rebuilds it.

## Measure and improve a skill

Rollouts run Claude Code (on your plan) or Codex (billed to an OpenAI key); each sees only
the skill under test. Before a run, `preflight` proves that a session cannot write outside its
workspace, read your credentials or the checkout, reach hosts other than PyPI, or leave the
sandbox.

```bash
# The shipped skill on one task, for debugging a task or a harness.
$S/.venv/bin/python -m gac_skillopt rollout --scratch $S --harness claude --task <task id> --keep
# One split of one skill, then training (SkillOpt's trainer; one skill per run).
$S/.venv/bin/python -m gac_skillopt.run eval --config configs/claude.yaml --skill <skill> --split valid_seen --scratch $S
$S/.venv/bin/python -m gac_skillopt.run train --config configs/claude.yaml --skill <skill> --scratch $S
```

SkillOpt keeps a candidate only when it scores strictly better on val. The best candidate
then goes through a human review: wording learned from the benchmark is generalised, claims
are checked against the code, and the text is measured again. Once adopted, it is written into
both skill copies, byte-identical ([CONTRIBUTING](https://github.com/ss7172/graph-agents-cli/blob/main/CONTRIBUTING.md#skills)),
with a CHANGELOG entry.

What the reviewed texts scored against the 0.2.0 texts, on the benchmark's val tasks unless
noted (each review in `tools/skillopt/results/` has the runs, the repetitions and the
statistics):

| Skill | Harness | 0.2.0 text | Reviewed text |
|---|---|---|---|
| workflow | Claude Code | 10 of 18 | 17 of 18 |
| workflow | Codex | 6 of 12 | 12 of 12 |
| scaffold | Claude Code | 0.71 | 1.00 |
| observability | Codex | 1 of 9 (the salt task) | 6 of 6 |

## Add a task

A task is a directory with `task.json`, `gold.sh`, `broken.sh` and, optionally, `fixture/`
and `hidden/` (files only the verifier sees). The check types are `cmd`, `file`,
`json`/`yaml`/`dotenv` (an expression over the parsed file), `unchanged`, `pyfile`, `eval` and
`transcript` (the commands the agent ran, and its final answer); the
[README](https://github.com/ss7172/graph-agents-cli/blob/main/tools/skillopt/README.md#writing-a-task)
has the schema.

- **Make the broken solution a real mistake:** one an agent makes without the skill (a
  widened API policy, a hand-edited manifest, the wrong flag), not a random failure.
- **Hold out variants, not families.** Test tasks stay frozen and unseen. A new train or val
  task may belong to a test task's family, but it must differ in its fixture, its prompt and
  its expected specifics, so that no test answer leaks. `validate` enforces the project name
  and the prompt.
- **Freeze, then prove.** Add the task to its skill's split, freeze the hashes, and run
  `validate` and `selfcheck` on it.
