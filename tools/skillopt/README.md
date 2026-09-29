# gac-bench: a benchmark and SkillOpt environment for the graph-agents-cli skills

Contributor tooling for improving the six skills in [`skills/`](../../skills) with
[SkillOpt](https://github.com/microsoft/SkillOpt). Nothing here is part of the CLI, the wheel or a
generated project; an optimised skill replaces a shipped one only after a person reviews the diff.
[`DESIGN.md`](DESIGN.md) explains the decisions and the isolation evidence; this page is how to
run it.

- **gac-bench**: 101 realistic tasks (`tasks/<skill>/<id>/`) across all six skills, each with a
  fixture, a prompt, a deterministic verifier, a scripted gold solution the verifier accepts and a
  scripted broken solution it rejects. Frozen train/val/test splits per skill are in `splits/`.
- **`gac_skills`**: a SkillOpt environment (`gac_skillopt/adapter.py`) that installs a candidate
  `SKILL.md` as the real skill of Claude Code or Codex, runs the task in an isolated, sandboxed
  workspace, scores it with the verifier and writes the compact trajectory SkillOpt reflects on.

## Setup

Needs macOS (the sandbox behaviour was verified there), Python 3.12, `uv`, `helm`, and the harness
you want to run: Claude Code (logged in; rollouts use your plan) and/or Codex (billed to an OpenAI
key). Everything the runs write goes to a scratch directory outside the checkout.

```bash
S=/path/to/scratch                                  # outside this checkout
uv venv --python 3.12 $S/.venv
VIRTUAL_ENV=$S/.venv uv pip install -r tools/skillopt/requirements.txt
cd tools/skillopt
$S/.venv/bin/python -m gac_skillopt setup --scratch $S      # CLI build, current uv, warm caches
$S/.venv/bin/python -m gac_skillopt preflight --scratch $S  # one Claude session: isolation + sandbox
```

`setup` installs `graph-agents-cli` from this checkout into `$S/uv-tools` (behind a wrapper in
`$S/bin`), a current `uv` (older than 0.9.29 panics in the sandboxes) and warms the uv cache and
the chart-dependency cache.

**A stale CLI is refused.** Every other command refuses a scratch CLI that does not match the
checkout: its recorded commit is not HEAD or an ancestor, the build was dirty, or `src/`,
`pyproject.toml` or `hatch_build.py` changed since that commit. `setup` (or `setup
--rebuild-cli`) rebuilds it, and `GAC_SKILLOPT_ALLOW_STALE_CLI=1` runs it anyway.

**The preflight** runs one Claude session in the rollouts' permission mode (`--permission-mode`,
default `acceptEdits`). The session tries to write outside the workspace, read the key
directory, the checkout, other runs and the known copies of the skills outside the workspace,
reach a host other than PyPI, use `dangerouslyDisableSandbox` and edit its own settings. Every
check is judged from what happened, and `preflight: ok` means none of those succeeded.

Set `GAC_SKILLOPT_OPENAI_KEY_FILE` for Claude runs too (the path is enough): its directory is
then denied to every rollout. `GAC_SKILLOPT_DENY_READ` adds more paths.

## Checking the benchmark (no model calls)

```bash
$S/.venv/bin/python -m gac_skillopt validate                  # schemas, splits, frozen hashes
$S/.venv/bin/python -m gac_skillopt selfcheck --scratch $S    # every task: gold=1, broken=0, noop=0
uv run pytest tools/skillopt/tests -q                        # from the repository root
```

`selfcheck` runs, for every task, the scripted gold solution (must score `hard=1`, `soft=1.0` and
exit 0), the scripted broken solution (must score `hard=0` and fail exactly the mandatory checks
the task lists in `broken_fails`), and the untouched fixture (`noop`, must score `hard=0`). All 101
tasks pass it against a CLI built from v0.3 (commit dbf2ddd); a run takes about 15 minutes at 8
slots.

## Running agents

```bash
# Direct rollouts of the shipped skill (no SkillOpt): debugging a task or a harness.
$S/.venv/bin/python -m gac_skillopt rollout --scratch $S --harness claude --task eval-tool-case-lisbon --keep

# SkillOpt's evaluation path (scripts/eval_only.py): one split of one skill.
$S/.venv/bin/python -m gac_skillopt.run eval --config configs/claude.yaml --skill eval --split valid_seen --scratch $S

# Training (scripts/train.py, ReflACTTrainer): one skill per run.
$S/.venv/bin/python -m gac_skillopt.run train --config configs/claude.yaml --skill eval --scratch $S

# Repeated evaluation of all six skills on val and test (the baseline; --body-dir <dir> with
# <short name>.md files evaluates candidates instead), then the report.
$S/.venv/bin/python -m gac_skillopt.baseline run --scratch $S --harness claude --reps 3 --port-base 22100
$S/.venv/bin/python -m gac_skillopt.baseline report --out $S/runs/baseline --json baseline.json
```

`--port-base` (or `GAC_SKILLOPT_PORT_BASE`) moves a run's ports: slot `i` uses `base+i` and
`base+25+i`, so runs that share a machine need disjoint ranges. `baseline rescore` reruns the
transcript checks on recorded traces after a verifier change. Results of the shipped skills are in
[`results/baseline.md`](results/baseline.md) (round 1: val and test) and
[`results/val-baseline-r2.md`](results/val-baseline-r2.md) (round 2: train and val of the expanded
benchmark). The first training runs, 1-epoch smoke trains of the workflow and scaffold skills on
Claude Code, are in [`results/smoke-train-r2.md`](results/smoke-train-r2.md).

**Round 3a:**

- the permission-mode preflight and the harness prep: [`results/preflight-r3.md`](results/preflight-r3.md);
- the workflow and scaffold re-baseline: [`results/rebaseline-r3.md`](results/rebaseline-r3.md);
- the human review of the round-2 candidates, with the proposed final text:
  [`results/review-r2.md`](results/review-r2.md).

**Round 3b:**

- the full training of the workflow skill from the approved text (2 epochs, `env.val_reps=3`),
  and the head-to-head on val and test: [`results/train-r3-workflow.md`](results/train-r3-workflow.md);
  every rollout is in [`results/train-r3-workflow.json`](results/train-r3-workflow.json);
- the human review of its best body, with the proposed final text:
  [`results/review-r3-workflow.md`](results/review-r3-workflow.md);
- SkillOpt on the observability skill with Codex rollouts (2 epochs, `env.val_reps=3`), which
  found the salt procedure, and the head-to-heads on Codex and Claude:
  [`results/train-r3-observability.md`](results/train-r3-observability.md); every rollout is in
  [`results/train-r3-observability.json`](results/train-r3-observability.json);
- the human review of its best body, with the proposed final text:
  [`results/review-r3-observability.md`](results/review-r3-observability.md);
- the Codex transfer check of the approved workflow and scaffold texts:
  [`results/transfer-r3-codex.md`](results/transfer-r3-codex.md).

**Round 3c:** the final workflow text for the owner. It is A8's text plus a scope rule (a
concrete change to an existing project is not a new agent), the `asks` edit, and no
harness-derived wording. It is measured on Codex and Claude Code against the shipped and r2
texts, after two harness fixes: `uv run` in transcript checks, and no rollout reads of the known
skill copies outside the workspace. See
[`results/review-r3c-workflow.md`](results/review-r3c-workflow.md).

**Round 3d:** that text with generic examples in its scope rule, so that they no longer name
benchmark task types. It is re-checked on Claude Code (the same Claude Code and model as round
3c, pinned). See [`results/review-r3d-workflow.md`](results/review-r3d-workflow.md).

**v0.3 features (round 3d):** 15 tasks in five new families of three variants each (one in
train, one in val, one in test), for the agent-to-agent features of 0.3:

- langgraph-code:
  - `code-peer-wiring`: `peer add`;
  - `code-relay-gate`: relayed approval gates (`decide_with: relayed`, `relayers`);
  - `code-exchange-api`: `auth: exchange` APIs (audience, scope, resource);
  - `code-jsonrpc-policy`: `rpc_method` allows and denials on JSON-RPC APIs.
- deploy: `deploy-system-wiring`, `system apply` and `system check` over several projects.

No model has run them yet; `selfcheck` proves their verifiers. The structured final-answer mode
has no task until it is merged into this branch.

In training configs, `env.val_reps` (2 in `configs/claude.yaml`) runs each selection item k times,
and the gate sees the mean.

Codex runs additionally need `GAC_SKILLOPT_OPENAI_KEY_FILE` (a file with one `OPENAI_API_KEY=`
line; the key is passed to `codex login --with-api-key` on stdin and never appears on a command
line or in output) and, to record spend, `GAC_SKILLOPT_LEDGER` (a `spend.py` with `check` and
`add`). `configs/codex.yaml` sets `env.max_usd`; the runner refuses to start when the ledger has
no room for it, and stops before a rollout that could cross it.

Outputs go to `$S/runs/<mode>-<skill>-<time>/`: SkillOpt's files plus, per rollout,
`predictions/<task>/{conversation.json, result.json, trace.json, events.jsonl}`. `result.json`
has every check with its detail, the usage (Codex tokens and USD; Claude's API-equivalent
figure), whether the agent loaded the skill, what the session loaded (isolation) and what was
left running. A `train` run also writes `optimizer_calls.jsonl`: one line per optimizer session
(stage, wall time, prompt size, usage, error). SkillOpt's own 300 s cap on those sessions becomes
`GAC_SKILLOPT_OPTIMIZER_TIMEOUT` (default 1200 s).

## What a rollout is

1. **Fixture** (outside any sandbox): a workspace `/private/tmp/gac-x-skillopt/<run>/<slot>-<task>/`
   with the project `graph-agents-cli create`d, `.env` from `.env.example` (`MODEL_PROVIDER=fake`,
   `API_KEY=dev`), the task's overlay and setup commands, `install`, vendored chart dependencies
   and an APFS clone of the warm uv cache (so an agent's `install` needs no download).
2. **Skill**: the shipped frontmatter plus the candidate body, with the shipped `references/`, as
   `.claude/skills/<name>/` or `.agents/skills/<name>/`. No other skill is installed.
3. **Agent**: the task prompt after a fixed prefix (the fake provider is in use; work inside the
   current directory; the user is not available for questions). Claude Code `sonnet` or Codex
   `gpt-5.6-terra`, effort medium, 80 turns at most, the task's timeout (900 s by default).
4. **Verifier** (outside the sandbox, fake provider, the slot's verifier port, empty kubeconfig):
   the task's checks, then `hard` (every mandatory check passed) and `soft` (weighted fraction).
5. **Integrity and isolation**: a rollout that changed the skill or the sandbox settings scores 0;
   a Claude session that loaded anything beyond the skill under test (skills, plugins, MCP
   servers, hook events) stops the run. Leftover processes on the slot's ports or inside the
   workspace are stopped; the workspace is deleted.

**Isolation** (DESIGN section 3): environments are built from an allowlist.

- **Claude Code** runs with `--setting-sources project`, an empty strict MCP config, hooks off,
  and its bundled skills, workflows and connectors off. The Bash sandbox comes from the workspace
  settings:
  - writes only in the workspace;
  - reads of credentials, this checkout, other runs and the known copies of the skills outside
    the workspace denied: the scratch CLI's bundled skills (`graph_agents_cli/skills/data`), uv's
    cache and uv's tool directory (both harnesses, since round 3c; other benches' builds through
    `GAC_SKILLOPT_DENY_READ`);
  - network only to PyPI;
  - local binding for the project's server.

  The workspace's `.claude/` is denied to the file tools. Rollouts run in `--permission-mode
  acceptEdits` with `--permission-prompts none`. `bypassPermissions` failed its preflight
  ([`results/preflight-r3.md`](results/preflight-r3.md)): the Write and Edit tools wrote outside
  the workspace, and non-PyPI hosts were reachable.
- **Codex** runs with a scratch `CODEX_HOME` and `HOME` and a permission profile with the same
  limits. `DOCKER_HOST` points at a socket that does not exist and
`KUBECONFIG` at an empty file, so no rollout can build images or reach a cluster.

**Ports**: slot `i` uses `22050+i` for the agent's local server and `22075+i` for the
verifier's (at most 25 slots).

## Costs (measured)

Smoke runs through SkillOpt's evaluation path (`gac_skillopt.run eval`), shipped skills,
2026-09-27:

| Harness | Task | Agent time | Usage | Cost | Score |
|---|---|---|---|---|---|
| Claude Code, sonnet, medium | `eval-tool-case-tokyo` (edits the dataset, runs `eval run` in the sandbox) | 34 s, 10 turns | | $0.13 API-equivalent (plan) | 1 / 1.00 |
| Claude Code, sonnet, medium | `scaffold-create-prototype-gemini` (empty workspace, `create`) | 15 s, 5 turns | | $0.08 API-equivalent (plan) | 1 / 1.00 |
| Codex, gpt-5.6-terra, medium | `deploy-placeholder-url` (values fix, `deploy --dry-run` in the sandbox) | 12 s | 121k in (95k cached), 1.0k out | $0.084 | 1 / 1.00 |
| Codex, gpt-5.6-terra, medium | `deploy-secret-key` (manifest allowlist) | 18 s | 155k in (127k cached), 1.8k out | $0.104 | 1 / 1.00 |

The tasks where the agent runs `eval run` itself cost Codex $0.10 per rollout (38 s of agent
time) since the CLI fixes of round 2, against $0.21 before
([`results/refix-local-server.md`](results/refix-local-server.md)).

Add 3-5 s per rollout for the fixture and the verifier (up to 10 s when the verifier runs
`eval run`). Scripted solutions take 5-30 s. Harder tasks take more turns than these; budget
Codex at $0.10-0.60 per rollout until a calibration run on a whole split replaces the estimate.
A full training run of one skill at the current split sizes (6-7 train, 6-7 val, 1-3 test tasks)
with `configs/claude.yaml` is at most about 80 (2 epochs) to 120 (3 epochs) rollouts plus the
optimizer's sessions (DESIGN section 8 has the formula).

## Writing a task

`tasks/<skill short name>/<id>/task.json`:

```json
{
  "id": "eval-tool-case-lisbon", "skill": "graph-agents-cli-eval", "family": "eval-tool-case",
  "task_type": "eval/dataset",
  "fixture": {"kind": "project", "name": "weather-agent", "create_args": ["--prototype"],
              "env": {}, "setup": [], "install": true},
  "needs_local_server": true,
  "prompt": "In the project in weather-agent/, add an eval case ...",
  "checks": [{"id": "gate", "type": "eval", "expect_exit": [0], "statuses": {"lisbon-weather": "passed"}}],
  "broken_fails": ["gate"],
  "reference": "What a correct solution does (shown to SkillOpt's analyst, never to the agent)."
}
```

- `fixture.kind`: `project` (created with `create <name> <create_args>`) or `empty` (the agent
  creates it; `name` is where the checks look). `fixture/` files are copied in first (`dot_x`
  becomes `.x`), then `setup` commands run in the project, then `install`. An `empty` fixture's
  files and `setup` commands go to the workspace instead, so they can create several projects
  under `name` (the `deploy-system-*` tasks do); checks then name files below it
  (`front-desk/api-policy.yaml`) or set `cwd`.
- Check types (`gac_skillopt/verify.py`): `cmd`, `file`, `json`/`yaml`/`dotenv` (a Python
  expression over `data`), `unchanged` (against the fixture), `pyfile` (a script in `hidden/`,
  run with the project's interpreter; `hidden/_toolkit.py` calls a tool the way the runtime does,
  with a mock API transport), `eval` (`eval run` plus the results file), `transcript` (commands
  the agent ran, matched at the start of each simple command; its final answer). Every check
  may set `mandatory` (default true), `weight`, `cwd` and `why` (the rule the analyst reads when
  it fails).
- `gold.sh` and `broken.sh` are sourced by bash in the workspace; every command they run is
  recorded as the transcript and `$GAC_FINAL` is the final answer. The broken solution is a
  plausible mistake an agent makes without the skill (a wrong argument name, widening the API
  policy, hand-editing the manifest instead of `scaffold enhance`), not a random failure.
- `needs_local_server`: the agent must run `eval run` or `run` itself (informative; every harness
  runs these tasks since the CLI fixes for DESIGN section 9 issues 1 and 2, so a scratch CLI
  built before them needs `setup --rebuild-cli`).
- Add the task to `splits/<skill>.json` (train or val; test tasks stay frozen and unseen: a new
  variant of a test task's family must differ from it in fixture, prompt and expected specifics,
  DESIGN section 5.4), then freeze:
  `python -c "from gac_skillopt.tasks import freeze_hashes; freeze_hashes('<skill>')"`.
  `validate` fails when a task changes after its split was frozen.

The generated-project conventions the tasks rely on (the fake model's replies, the scaffolded
dataset) are the template's; a template change can break a gold solution, and `selfcheck`
shows it.
