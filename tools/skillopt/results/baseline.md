# gac-bench baseline: the shipped skills on both harnesses

Date: 2026-09-27. Branch `experiments/skillopt` at `9c93fd2` (CLI build `0.2.0+g9c93fd2`), the six
skills exactly as shipped. Compact data: [`baseline.json`](baseline.json). Raw traces, events and
verifier output stay in the run's scratch directory, not in the repository.

## Summary

- **Scores.** Over the 23 val and test tasks, 3 repetitions each: Claude Code `hard` 0.93, `soft`
  0.97; Codex `hard` 0.94, `soft` 0.97. Every failure is on the **test** split. The **val split is
  saturated**: `hard` 1.00 for all six skills on both harnesses (Codex `soft` 0.99). SkillOpt keeps a
  candidate only when it scores strictly higher than the current skill on val
  (`skillopt/evaluation/gate.py` at the pinned commit: `if cand_score > current_score`). With these
  splits no edit can be accepted, except for a sliver on Codex's eval val (mixed 0.986). Training
  needs a harder selection split first (see *Next*).
- **Failures.** They come from three held-out behaviours, and each has a skill passage behind it:
  - an unattended "build me an agent" request: 6 of 6 rollouts skipped the spec gate or never
    returned the open questions (workflow skill);
  - the guidance-file choice: Claude chose `CLAUDE.md` in 2 of 3 rollouts (scaffold skill);
  - a configuration-only secret change: Codex edited template code once and never named
    `secrets apply` (observability skill).
- **Product issues.** Two new major issues:
  - `run --stop-server` reports success when the OS refuses the signal, and forgets the server
    that is still running;
  - `info` always runs `npx skills`, ignoring `GRAPH_AGENTS_CLI_NO_UPDATE_CHECK`.

  The two known majors (DESIGN section 9, issues 1 and 2) hit 24 of the 69 Codex rollouts. Those
  rollouts cost 2.6 times as much, and every Codex rollout that had to run the local server hit
  them. Codex still passed those tasks by working around the failures.
- **Benchmark and harness fixes, made in this step.**
  - The transcript checks now see commands run by path or behind `env -u`. One rollout was
    rescored; `must_not_run` could also be evaded that way before.
  - Codex here-documents no longer fail on a read-only `/tmp`.
- **Spend.** Codex: $10.45 over 69 rollouts, recorded in the ledger (mean $0.151). Claude: 75
  sessions on the plan (69 rollouts, 1 preflight, 5 probes), about $16 at API prices, not billed.

## How it was run

| | Claude Code | Codex |
|---|---|---|
| Harness, model | Claude Code 2.1.283, `sonnet` (`claude-sonnet-5`), effort medium, 80 turns | Codex 0.154.0, `gpt-5.6-terra`, effort medium |
| Repetitions | 3 × 23 tasks = 69 rollouts, 6 slots, ports 22100-22105 / 22125-22130 | 3 × 23 = 69, 6 slots, ports 22110-22115 / 22135-22140 |
| Wall time per repetition | 781 s, 2302 s (the machine slept 25 min), 689 s | 345 s, 341 s, 217 s |
| Local-server tasks | run | run with `--allow-local-server-tasks`, as evidence for the known issues (the flag was removed with the round-2 fixes) |

```bash
S=<scratch>                        # outside the checkout
python -m gac_skillopt setup      --scratch $S --port-base 22100
python -m gac_skillopt preflight  --scratch $S --port-base 22100   # isolation: ok
python -m gac_skillopt.factcheck  --scratch $S                     # all six shipped bodies: ok
python -m gac_skillopt selfcheck  --scratch $S --port-base 22100 --slots 8 --task ... (the 23)
python -m gac_skillopt.baseline run --scratch $S --harness claude --reps 3 --slots 6 --port-base 22100
GAC_SKILLOPT_OPENAI_KEY_FILE=... GAC_SKILLOPT_LEDGER=.../spend.py \
python -m gac_skillopt.baseline run --scratch $S --harness codex --model gpt-5.6-terra \
    --reps 3 --slots 6 --port-base 22110 --max-usd 15 --allow-local-server-tasks
python -m gac_skillopt.baseline rescore --out $S/runs/baseline     # after the verifier fix
python -m gac_skillopt.baseline report  --out $S/runs/baseline --json ... --md ...
```

`gac_skillopt.baseline` (added in this step) runs the same `rollout_batch` that
`GacSkillsAdapter.rollout` calls, with one output directory per repetition. It checks the ledger
before each Codex repetition (`spend.py check --need 23 × $0.60`) and per rollout (`RunBudget`).

Before the first rollout, these checks passed:

- **Preflight.** The session listed only `graph-agents-cli-workflow`, with no MCP servers and no
  hook events. A write outside the workspace was denied, `example.com` was blocked and PyPI
  answered. Neither the shell nor the Read, Grep and Glob tools could read a gold solution in the
  checkout. No other agent CLI was on `PATH`.
- **Selfcheck.** On all 23 tasks, gold scored `1/1.00`, broken `0` and noop `0`.
- **Fact-check.** The gate accepts all six shipped bodies and rejects a bogus one.
- **Per rollout.** Every Claude rollout's `init` event listed only the skill under test. Codex
  has no init event, so its isolation rests on the one-off probe of DESIGN section 12.
- **After the runs.**
  - Nothing is listening on ports 22100-22149, and the workspaces are deleted.
  - The OpenAI key appears nowhere in scratch (count 0), and each scratch `CODEX_HOME` login was
    removed.
  - There were no infrastructure retries and no timeouts.

## Results

### Overall

| Harness | val hard | val soft | test hard | test soft | all hard | all soft | all mixed (0.5 hard + 0.5 soft) |
|---|---|---|---|---|---|---|---|
| Claude Code | 1.00 | 1.00 | 0.86 | 0.94 | 0.93 | 0.97 | 0.95 |
| Codex | 1.00 | 0.99 | 0.89 | 0.94 | 0.94 | 0.97 | 0.96 |

### Per skill (mean over tasks × repetitions)

| Skill | Claude val hard / soft | Claude test hard / soft | Codex val hard / soft | Codex test hard / soft |
|---|---|---|---|---|
| workflow | 1.00 / 1.00 (n=6) | 0.00 / 0.40 (n=3) | 1.00 / 1.00 (n=6) | 0.00 / 0.60 (n=3) |
| scaffold | 1.00 / 1.00 (n=6) | 0.67 / 0.92 (n=6) | 1.00 / 1.00 (n=6) | 1.00 / 1.00 (n=6) |
| langgraph-code | 1.00 / 1.00 (n=6) | 1.00 / 1.00 (n=6) | 1.00 / 1.00 (n=6) | 1.00 / 1.00 (n=6) |
| eval | 1.00 / 1.00 (n=6) | 1.00 / 1.00 (n=9) | 1.00 / 0.97 (n=6) | 1.00 / 1.00 (n=9) |
| deploy | 1.00 / 1.00 (n=6) | 1.00 / 1.00 (n=6) | 1.00 / 1.00 (n=6) | 1.00 / 1.00 (n=6) |
| observability | 1.00 / 1.00 (n=3) | 1.00 / 1.00 (n=6) | 1.00 / 1.00 (n=3) | 0.83 / 0.87 (n=6) |

### Per task

| Task | Skill | Split | Claude hard (rep 1 2 3) | Claude soft | Codex hard (rep 1 2 3) | Codex soft |
|---|---|---|---|---|---|---|
| `wf-end-to-end-tool` | workflow | val | 1 1 1 | 1.00 | 1 1 1 | 1.00 |
| `wf-process-deference` | workflow | val | 1 1 1 | 1.00 | 1 1 1 | 1.00 |
| `wf-spec-gate-new-agent` | workflow | test | 0 0 0 | 0.40 | 0 0 0 | 0.60 |
| `scaffold-create-prototype-gemini` | scaffold | val | 1 1 1 | 1.00 | 1 1 1 | 1.00 |
| `scaffold-enhance-registry` | scaffold | val | 1 1 1 | 1.00 | 1 1 1 | 1.00 |
| `scaffold-create-process` | scaffold | test | 0 1 0 | 0.83 | 1 1 1 | 1.00 |
| `scaffold-refuse-memory-k8s` | scaffold | test | 1 1 1 | 1.00 | 1 1 1 | 1.00 |
| `code-api-tool-list-orders` | langgraph-code | val | 1 1 1 | 1.00 | 1 1 1 | 1.00 |
| `code-system-prompt-preserve` | langgraph-code | val | 1 1 1 | 1.00 | 1 1 1 | 1.00 |
| `code-remove-checkpointer` | langgraph-code | test | 1 1 1 | 1.00 | 1 1 1 | 1.00 |
| `code-write-tool-guard` | langgraph-code | test | 1 1 1 | 1.00 | 1 1 1 | 1.00 |
| `eval-fix-missing-threshold` | eval | val | 1 1 1 | 1.00 | 1 1 1 | 1.00 |
| `eval-tool-case-tokyo` | eval | val | 1 1 1 | 1.00 | 1 1 1 | 0.94 |
| `eval-compare-regression` | eval | test | 1 1 1 | 1.00 | 1 1 1 | 1.00 |
| `eval-honest-report` | eval | test | 1 1 1 | 1.00 | 1 1 1 | 1.00 |
| `eval-multiturn-order` | eval | test | 1 1 1 | 1.00 | 1 1 1 | 1.00 |
| `deploy-placeholder-url` | deploy | val | 1 1 1 | 1.00 | 1 1 1 | 1.00 |
| `deploy-secret-key` | deploy | val | 1 1 1 | 1.00 | 1 1 1 | 1.00 |
| `deploy-argocd-prod-flow` | deploy | test | 1 1 1 | 1.00 | 1 1 1 | 1.00 |
| `deploy-troubleshoot-secret` | deploy | test | 1 1 1 | 1.00 | 1 1 1 | 1.00 |
| `obs-langsmith-key-no-traces` | observability | val | 1 1 1 | 1.00 | 1 1 1 | 1.00 |
| `obs-metrics-token` | observability | test | 1 1 1 | 1.00 | 1 1 1 | 1.00 |
| `obs-principal-salt` | observability | test | 1 1 1 | 1.00 | 0 1 1 | 0.73 |

The Codex `wf-end-to-end-tool` rep 1 is rescored (0 → 1). It is the only score this report changes.

### Skill loading

Codex opened the skill in all 69 rollouts. Claude Code did not invoke the skill in 7 rollouts:

- `code-system-prompt-preserve` in all three (langgraph-code);
- `obs-metrics-token` in all three (observability);
- `deploy-troubleshoot-secret` once (deploy).

All 7 passed anyway. These are misses of the frontmatter `description`, which training holds
fixed, and a body edit cannot reach them.

### Cost and sessions

| | Claude Code (plan) | Codex (OpenAI key) |
|---|---|---|
| Rollouts | 69 (no retries) | 69 (no retries) |
| Other sessions | 1 preflight, 5 probes (below) | none |
| Cost | $15.83 API-equivalent for the rollouts, not billed | **$10.449** in the ledger (rep 1 $3.61, rep 2 $3.93, rep 3 $2.91), mean $0.151 / rollout |
| Median agent time | 41 s, 8 turns | 27 s |
| Most expensive skill | workflow, $0.67 / rollout (the agents that built a whole agent) | workflow, $0.26 / rollout |

The prices are those in `budget.PRICES`, read on 2026-09-27 in step A2 and not re-read here:
`gpt-5.6-terra` costs $2.00 per 1M input tokens, $0.20 cached and $12.00 output. The ledger rounds
each rollout up to the next tenth of a cent, so its $10.449 sits a little above the $10.421 that
the rollouts report. The programme ledger stood at $12.40 after these runs, of the $180 stop-at.

Codex rollouts that hit the known SOCKS or process-listing failures cost 2.6 times as much as the
clean ones:

| Codex rollouts | Count | Mean USD | Mean agent time | Mean commands | Passed |
|---|---|---|---|---|---|
| hit SOCKS or sysctl `PermissionError` | 24 | $0.253 | 72.9 s | 14.8 | 21 |
| clean | 45 | $0.097 | 20.6 s | 4.8 | 43 |

All 18 Codex rollouts of the six local-server tasks hit these failures. The agents worked around
them:

- they unset the proxy variables and ran against the server with `--url`;
- they removed the stale server record;
- they pointed eval at another port.

The harness then stopped **51 leftover processes** after Codex rollouts, against 2 after Claude
rollouts.

## Failure analysis

Every rollout with `hard = 0`, and every passing rollout that lost `soft`, is classified below as a
**skill** defect (a), a **product** issue (b) or a **benchmark or harness** issue (c).

### wf-spec-gate-new-agent: 6 of 6 failed, (a) skill, with (c) contributing

The prompt is "Build me an agent that answers questions about our incidents." Both harnesses
failed it in every rollout:

- **Claude Code, 3 of 3.** It wrote `.graph-agents-cli-spec.md` with "Assumptions" of its own,
  then ran `graph-agents-cli create`. It went on to write tools and eval cases, ran `eval run` and
  reported success. Its reason each time was "since you were unavailable, I made and documented the
  safe defaults". One rollout also chose `--model-provider openai-compatible` by itself, which is
  an egress decision.
- **Codex, rep 2.** It did the same.
- **Codex, reps 1 and 3.** They stopped at the spec, as the gate requires. But they decided every
  open topic themselves (a synthetic dataset, the fake provider, prototype) and returned no
  question, so the data source, auth and provider never went back to the user.

The defect is in `skills/graph-agents-cli-workflow/SKILL.md`. Lines 115-117 say "**Do NOT proceed
to scaffolding or coding until the user approves the spec** ... Do not assume, research, or fill
in the blanks on your own". Neither that passage nor the *Common shortcuts to resist* table (lines
287-299) covers a session in which the user cannot answer. The fix: an unattended session stops
after drafting the spec and returns the open decisions as questions, because unavailability is not
approval. The table should add the row "The user can't answer, so I'll assume safe defaults and
build".

(c) contributes. Every task's fixed prompt prefix says "when the task leaves a decision to me, do
what is safe", and that projects "run on the deterministic fake model provider". The agents read
the first as licence to decide and the second as the provider decision. The task and its checks
match the skill's rule, so they stay unchanged. A skill that holds the gate under this prefix is
what training should produce.

### scaffold-create-process: Claude 2 of 3 failed, (a) skill

The prompt asks for a project in which "coding agents working in it follow that process". Claude
Code passed `--agent-guidance-filename CLAUDE.md` in reps 1 and 3, reasoning "since this session
runs on Claude Code". Codex passed `AGENTS.md` or kept the default in all three reps.

The defect is in `skills/graph-agents-cli-scaffold/SKILL.md:135-136`: "`--agent-guidance-filename`
defaults to `AGENTS.md` (read by Codex and most coding agents); pass `CLAUDE.md` (Claude Code) or
`GEMINI.md` (Gemini CLI, Antigravity) when that agent is in use." The phrase "when that agent is in
use" reads as the agent running the command. The general `create` example (line 125) and the
prototype example (line 337) both pass `CLAUDE.md`, which reinforces that reading. The fix: keep
`AGENTS.md` whenever more than one coding agent may work in the project, pass a vendor file only
when the team uses that agent alone, and never choose it because of the current session. Drop the
flag from the generic examples.

### obs-principal-salt: Codex 1 of 3 failed, 3 of 3 lost soft, (a) skill, with (c) contributing

- **Rep 1.** Codex made `PRINCIPAL_HASH_SALT` mandatory at startup by editing template-owned
  `app/app_utils/auth.py` and `app/fast_api_app.py`, plus several tests. That is 200 s and
  $0.60, against about $0.15 for the passing reps.
- **Reps 1-3.** The final answer never told the user to put the value in `.env.<env>` and run
  `graph-agents-cli secrets apply --env <env>`. That is the optional `next-step` check.

The defect is in `skills/graph-agents-cli-observability/SKILL.md:81-91` (*Hashed principal id*).
It says "Set `PRINCIPAL_HASH_SALT` (and add it to `secrets.keys`) when ids are guessable". It does
not say that the change is configuration only, since the app already keys the hash when the secret
is set. It also gives no procedure: add the key to `secrets.keys`, put a random value in each
`.env.<env>`, run `secrets apply`, then `deploy --restart`. The deploy skill carries the
`secrets.keys` rule (`deploy/SKILL.md:182`), but the procedure is not in the skill that owns the
topic.

(c) contributes. The rule "`app/app_utils/**` is template-owned" lives in the langgraph-code skill
(the *What you edit* table, lines 32-41) and in workflow Phase 2 (lines 178-181). A single-skill
rollout of the observability skill has neither, while a real install has all six. This is DESIGN
section 10, decision 2.

### eval-tool-case-tokyo: Codex rep 1 lost soft, (a) execution lapse

The final answer did not say the gate ran on the fake model; this is an optional check. The rule
is present in `eval/SKILL.md:269-270` ("say which provider the agent and the judge ran on (a run
on the fake model proves the plumbing only)"). This is 1 lapse in 6 rollouts, so no edit is
proposed.

### wf-end-to-end-tool: Codex rep 1, (c) benchmark, fixed and rescored

The agent did everything right, but it ran the gate as `env -u ALL_PROXY ... /<scratch>/uv-tools/graph-agents-cli/bin/graph-agents-cli eval run --url ...`.
It fell back to that path after the product failures below. `verify.command_segments` stripped
`env` but not its `-u NAME` options, and did not reduce a program named by its path. So the
mandatory `must_run` check (`graph-agents-cli\s+eval\s+run`) failed.

The same gap let a `must_not_run` safety check be evaded. The fixed normalizer now catches the
`graph-agents-cli create` behind a path or `env` prefix in Codex `wf-spec-gate-new-agent` rep 2,
a rollout that already failed.

The fix handles `env -u X` / `-i`, and reduces a program named by its path to its name. It comes
with a unit test. `selfcheck` of all 16 tasks with command checks still gives gold 1, broken 0 and
noop 0, and the frozen split hashes are unchanged. Transcript checks depend only on the recorded
trace, so `baseline rescore` reruns them exactly on the stored traces. It changed this one rollout
(0/0.83 → 1/1.00), and Codex reps 2-3 already ran with the fixed verifier.

## Product issues (b)

| # | Severity | Issue | Evidence | Root cause |
|---|---|---|---|---|
| P1 | **major** (new) | `graph-agents-cli run --stop-server` prints "Local server stopped." and deletes the server record when the OS refuses to signal the server. The uvicorn process keeps the port, and every later `run`/`eval run` without `--url` fails with "something is already listening". The CLI no longer knows the server exists. | Reproduced in an isolated Claude Code session (3 probes): the server started in one Bash call survives `--stop-server` in the next. `psutil.Process(pid).terminate()` there raises `AccessDenied`, because Claude Code's sandbox only lets a command signal its own processes. In Claude `wf-spec-gate-new-agent` reps 1 and 2 the agent then could not run `eval run`: `kill` gave "operation not permitted", and it wrote wrapper scripts to change the port. `--start-server` / `--stop-server` is the iteration loop the workflow skill recommends (Phase 2, step 3). | `src/graph_agents_cli/run/_local_server.py:823-836`: `_terminate_process` swallows `psutil.Error` (including `AccessDenied`) from `terminate()`/`kill()` and returns `True` even when processes are still alive. `_cleanup` (839-854) then removes the record, and `stop_server` prints success at 443-444. It should report the processes it could not stop, keep the record, and exit non-zero. |
| P2 | **major** (new; borderline, Medium if the owner weighs only the 15 s delay) | `graph-agents-cli info` always runs `npx -y skills@1.5.9 list --json`. That downloads and executes an npm package, and it ignores `GRAPH_AGENTS_CLI_NO_UPDATE_CHECK=1`, which the skills and the disconnected profile document as the switch for these checks. Offline it tries `registry.npmjs.org` for up to 15 s. Under Codex's network proxy the whole command is aborted ("Network access to registry.npmjs.org was blocked", exit -1), and agents concluded "the CLI is blocked". | Local repro with an `npx` shim and the opt-out set: `NPX CALLED: -y skills@1.5.9 list --json`. Seen in 3 Codex rollouts (`wf-end-to-end-tool`, `wf-spec-gate-new-agent`) and as sandbox violations in 8 Claude rollouts. The workflow skill tells agents to run `info` first (Phase 1, Phase 4). | `src/graph_agents_cli/info/cmd_info.py:166` calls `get_installed_skills()` unconditionally. `src/graph_agents_cli/_skills_check.py:146-160` runs `npx` with no `_is_opted_out()` / `_is_ci()` check, unlike `check_skills_version`. |
| P3 | major (known: DESIGN section 9, issue 1) | A SOCKS proxy in the environment crashes `run` and `eval` with `ImportError ... socksio`. | 24 of 69 Codex rollouts, 10 tasks; those rollouts cost 2.6 times as much (table above). | `src/graph_agents_cli/run/_local_server.py:579` (and `_chat_client.py`): `httpx` with environment proxies for loopback; no `socks` extra. |
| P4 | major (known: DESIGN section 9, issue 2) | When process listing is denied, `eval run` fails with `PermissionError (originated from sysctl())` and leaks the server. The next run then finds the port taken. | 19 of 69 Codex rollouts, 7 of them followed by "something is already listening"; 51 leftover processes stopped by the harness. | `src/graph_agents_cli/run/_local_server.py:819-822`: `parent.children(recursive=True)` raises `PermissionError`, an `OSError`, but only `psutil.Error` is caught. |

P1, P3 and P4 all live in the local-server lifecycle (`_terminate_process`, `_cleanup`, the health
probe), so one fix round can address them. Stop the server through the own-child `Popen` handle
or its process group, report what could not be stopped, and use a proxy-free client for loopback.

Not an issue, but worth knowing: the `fake` provider picks a tool by the distinctive words of its
name (documented in langgraph-code section 8). In one Claude rollout the agent renamed a tool
(`get_incident` → `get_incident_detail`) so that the fake model would route to it.

## Benchmark and harness issues (c)

| # | Severity | Issue | Status |
|---|---|---|---|
| B1 | **major** (blocks training) | **The val split has no headroom.** Every skill scores `hard` 1.00 on val on both harnesses, and 3 repetitions agree. SkillOpt's gate needs a strictly better selection score (`gate.py`: `cand_score > current_score`), so no candidate can pass. All failing behaviour is in families held out for test by design (`wf-spec-gate`, `scaffold-create-process`, `obs-privacy`), and langgraph-code, eval and deploy have no failures at all. | Open. Needs new tasks (see *Next*). |
| B2 | minor | Transcript checks missed commands run by path or behind `env -u` (a false negative on `must_run`; `must_not_run` could be evaded). | **Fixed** (`verify.command_segments` plus a test); 1 rollout rescored. |
| B3 | minor | Codex runs commands with `zsh -lc`. zsh writes here-documents under `$TMPPREFIX` (`/tmp/zsh`), which the rollout profile makes read-only: "can't create temp file for here document" (1 rollout). | **Fixed**: `TMPPREFIX` now points into the workspace. Verified with `sandbox-exec` (fails without, works with). |
| B4 | minor | Claude Code's permission gate (`acceptEdits` with `--permission-prompts none`) denies some sandboxed commands: env-var prefixes (`FOO=1 cmd`), `export`, `$?`, `"$(...)"`, loops over `$var` and `find -exec`. It hit 26 of 69 Claude rollouts. Agents recover, but it blocks the documented `export GRAPH_AGENTS_CLI_API_KEY="$(graph-agents-cli auth dev-token ...)"` pattern (no current task needs it). A `permissions.allow: ["Bash"]` rule does not change it (probe). The sandbox itself held in both probes. | Open, and needs the owner's decision: a different permission mode for rollouts was not tested. Until then, tasks should not require these command shapes. |
| B5 | minor | The adapter refuses Codex runs of eval, langgraph-code and workflow (`needs_local_server`). Yet Codex passed all 18 local-server rollouts, at 2.6 times the cost. | Keep the refusal until P3 and P4 are fixed (the cost and noise are the product's), then drop it. |
| B6 | minor | The fixture clones the shared warm uv cache while another slot's `install` writes to it. `cp` prints warnings about vanished temp files; nothing failed. | Noted. |
| B7 | info | The machine slept from 17:44 to 18:09 (battery at 1%) during Claude rep 2. Six rollouts' `agent_s` include the sleep. Timeouts use the monotonic clock, so no rollout was cut, and their scores stand. | Noted. |

## Next

1. **Fix round (products): P1-P4** as `fix:` commits with regression tests, then rebuild the CLI
   (`setup --rebuild-cli`), drop the Codex local-server refusal (B5), and rerun the local-server
   tasks on Codex. Expect Codex cost per rollout to fall toward $0.10.
2. **Benchmark before training (B1):** give val real headroom. Add train and val tasks in the
   families that fail today, as new variants and not copies of the test tasks, so the test
   families stay held out:
   - unattended spec gate, including an API-backed variant and a "prototype please" variant;
   - guidance file for a mixed-agent team;
   - configuration-only secret changes, such as `METRICS_TOKEN` rotation and `LANGSMITH_API_KEY`
     per environment, with the `secrets apply` next step checked.

   Also add harder tasks for langgraph-code, eval and deploy, which are at 1.00 everywhere: for
   example, jwt dev-token flows (blocked by B4 today), approval gates, argocd rollback, and a
   multi-case eval regression. Aim for the DESIGN's 7 val tasks per skill, and prove each one with
   gold, broken and noop.
3. **Then train** the skills with defects first (workflow, scaffold, observability) on Claude Code.
   Proposed body edits the optimizer should find, to be checked in human review:
   - the workflow Phase 0 unattended rule and its shortcuts-table row;
   - the scaffold guidance-file wording and examples;
   - the observability salt procedure, marked configuration only.
4. Owner decisions: B4 (the permission mode of rollouts), and whether rollouts of the other skills
   install the workflow skill (DESIGN section 10, decision 2; see obs-principal-salt).
