# SkillOpt for the graph-agents-cli skills: design

Status: design and spike (branch `experiments/skillopt`), 2026-09-27; built as `gac_skillopt`
with the gac-bench tasks the same day (how to run it: [`README.md`](README.md); where the build
differs from this design: section 12). Contributor tooling only:
nothing under `tools/skillopt/` is a dependency of the CLI or of a generated project, and an
optimised skill replaces a shipped one only after human review.

[SkillOpt](https://github.com/microsoft/SkillOpt) (MIT) treats one skill document as the
trainable state of a frozen agent: a target agent runs tasks with the current document, an
optimizer model reads the scored trajectories and proposes bounded edits, and a candidate is
kept only when it scores strictly better on a held-out selection split. This document records
what the spike established about SkillOpt and about isolating the two agent harnesses, and the
design for training the six graph-agents-cli skills with it. The spike scripts are in
[`spike/`](spike/); the appendix shows how to rerun them.

## 1. Decisions at a glance

| Question | Decision |
|---|---|
| SkillOpt version | Git commit `79124b37e9a6371e13b753f8bcd7adb1e493ade1` (main, 2026-09-06), installed in a scratch venv with the `claude` extra. Not the PyPI 0.2.0 wheel: it ships no prompt files, so the reflect stage cannot load `analyst_error` (section 9). |
| Which parts of SkillOpt | Its trainer (`ReflACTTrainer`), data loader base, reflection, aggregation, selection, patching, gate, slow update and meta skill. **Not** its exec harness (`run_target_exec`): our adapter runs the agents itself (section 2.4). |
| Unit of optimisation | One run per skill: the trainable document is that skill's `SKILL.md` body (frontmatter held fixed); its `references/` ship with it into every rollout, unchanged. |
| Target harnesses | Claude Code 2.1.283 (`claude -p`, owner's plan) and Codex 0.154.0 (`codex exec`, OpenAI key). |
| Target models | Claude Code: `sonnet` (resolves to `claude-sonnet-5`), effort medium. Codex: `gpt-5.6-terra`, effort medium (in Codex 0.154.0's model catalog; `gpt-6-sol` is not and runs on fallback metadata). |
| Optimizer | Claude Code as a chat model (`claude_code_exec` optimizer backend, `opus`, effort high) behind an isolating wrapper, for both harnesses: the OpenAI key then pays only for Codex rollouts. |
| Isolation | Proven for both harnesses (section 3): the session sees exactly the skill under test; no user skills, plugins, hooks, MCP servers, connectors, memory or ancestor `CLAUDE.md`/`AGENTS.md`; shell sandboxed to the workspace with network only to PyPI and loopback. |
| Scoring | Deterministic verifiers run by the harness after the agent exits (lint, API-policy check, tests, `eval run` and `deploy --dry-run` with `MODEL_PROVIDER=fake`, file and manifest assertions, transcript assertions). No LLM judge. |
| Guard rails on candidates | A static fact-check gate (frontmatter, required sections, references, every `graph-agents-cli` command and flag against the real Click tree, size) runs before any rollout of a candidate; a failing candidate scores 0 with the violation as the failure reason. |
| Dataset | 30 tasks per skill in parametrised families, split 15 train / 7 selection / 8 test, stratified by family. Hold-out (round 2): test holds out *variants within families*, not whole families; test tasks stay frozen and unseen (section 5.4). As built: 86 tasks, at least 6 train and 6 val per skill (section 12). |
| Budget | Claude: full size for all six skills (tiered epochs). Codex: one full optimisation (eval skill) plus a cross-harness transfer evaluation of every Claude-optimised skill, under a Track A cap of $90 enforced by the runner (section 8). |

## 2. SkillOpt, as it works at the pinned commit

### 2.1 Install

```bash
uv venv --python 3.12 <scratch>/.venv
VIRTUAL_ENV=<scratch>/.venv uv pip install \
  "skillopt[claude] @ git+https://github.com/microsoft/SkillOpt@79124b37e9a6371e13b753f8bcd7adb1e493ade1"
```

The package also installs a top-level `scripts` package (`scripts.train`, `scripts.eval_only`);
keep it in its own venv. Registration of a new environment has no plugin hook: `scripts/train.py`
keeps a module-level `_ENV_REGISTRY` (line 43) that `get_adapter()` fills with built-ins and then
looks up, passing only the config keys the adapter's `__init__` accepts (lines 105-127). Our
runner imports `scripts.train`, adds `_ENV_REGISTRY["gac_skills"] = GacSkillsAdapter`, and calls
`scripts.train.main()`.

### 2.2 The training loop and what triggers a rollout

`skillopt/engine/trainer.py` (`ReflACTTrainer.train`): `steps_per_epoch = ceil(train_size /
(batch_size * accumulation))` (line 892). Every call to `adapter.rollout()` is one agent run per
item, and those are the whole target-side cost:

| Phase | Where | Rollouts |
|---|---|---|
| Baseline on the selection split (`valid_seen` = `val/`) | 1097-1104 | V |
| Each step: roll out a train batch | 1228 | B (x accumulation) |
| Each step: evaluate the candidate on the selection split (skipped on a cache hit or when no patch survives) | 1533-1540 | V |
| Slow update, every epoch from the second: previous and current skill on the same train sample | 1807-1832 | 2 x `slow_update_samples` |
| Slow update gate (only with `slow_update_gate_with_selection`) | 1929-1938 | V |
| Meta skill (reuses the slow-update pairs when slow update ran) | 2080-2101 | 0 (else 2 x samples) |
| `longitudinal_pair_policy: changed` top-up | 260-338 | up to 2 per scanned train item |
| Final selection eval when the last skill differs from the best | 2201-2207 | V |
| Test (`valid_unseen` = `test/`): initial skill, best skill, final skill if different | 2249-2356 | 2T to 3T |

Optimizer calls per step: one analyst call per minibatch of failures and per minibatch of
successes (`gradient/reflect.py`), hierarchical merges (`merge_batch_size`), one ranking call,
plus a rewrite call in the `rewrite_*` update modes; one call each for slow update and meta skill
per epoch from the second.

### 2.3 The API we implement

- `SplitDataLoader.load_split_items(split_path) -> list[dict]` (`datasets/base.py`): items need a
  unique `id`; everything else is ours. `split_mode: split_dir` reads `train/`, `val/`, `test/`.
- `EnvAdapter` (`envs/base.py`): `build_train_env`, `build_eval_env`, `rollout`,
  `get_task_types` are abstract; `get_dataloader`, `setup`, `build_env_from_batch` and the
  prompt getters are overridable; `reflect()` is inherited.
- `rollout(env_manager, skill_content, out_dir) -> list[dict]`: each result needs `id`, `hard`
  (0/1) and `soft` (0-1); extra fields (`task_description`, `task_type`, `fail_reason`,
  `reference_text`, `n_turns`, ...) reach reflection. Scoring lives here: there is no
  `evaluate()` on the adapter.
- For reflection the rollout must write `out_dir/predictions/<id>/conversation.json`; results
  without it are silently dropped from reflection (`fmt_minibatch_trajectories`, line 141).
- The adapter's env name is derived from its module path (`skillopt.envs.<name>.adapter`); an
  adapter outside the package gets the generic prompts unless it overrides
  `get_error_minibatch_prompt()` / `get_success_minibatch_prompt()`, which we do (section 4.5).

### 2.4 How SkillOpt's exec harness gives the agent the skill, and why we do not use it

`model/codex_harness.py`: `prepare_workspace()` (96-185) writes the skill to
`.agents/skills/skillopt-target/SKILL.md`, wrapped by `render_skill_md()` (48-74) in its own
frontmatter (`name: "skillopt-target"`), a `# ReflACT Target Skill` heading and a `## Dynamic
Guidance` section, plus the task in `task.md`. The prompt (`_exec_prompt`, 701-715; the Claude
SDK path appends the same text to the `claude_code` system-prompt preset, 816-831) tells the
agent to read both files directly and **not** to call a Skill tool. Then:

- Claude Code, CLI path (884-954): `claude -p --output-format text --permission-mode
  bypassPermissions --tools Read,Bash` by default, no setting-source, MCP, hook or plugin
  controls, and text output: no usage and no tool trace. SDK path (801-881): structured output,
  usage and messages are returned, but again with no isolation controls.
- Codex, CLI path (1340-1420): `codex exec --sandbox ...` inheriting the caller's `CODEX_HOME`
  and `HOME`; the token tracker records `("rollout", 0, 0)` (1402), so rollout tokens are never
  counted.
- Both retry an empty answer once (`EXEC_EMPTY_RESPONSE_RETRIES`, default 1), which silently
  doubles the cost of a failing rollout.

For our skills that measures the wrong thing (a renamed document read on instruction, instead
of the real skill found and loaded the way a user's agent would), leaks the developer's
configuration, and loses usage. Our adapter therefore installs the candidate as the real skill
(`.claude/skills/<name>/` for Claude Code, `.agents/skills/<name>/` for Codex, with its
`references/`), runs the harness with the isolation of section 3 and JSON event output, and
records usage per rollout. The config still names `target_backend: claude_code_exec` /
`codex_exec` so SkillOpt's bookkeeping is right; nothing calls `run_target_exec`.

### 2.5 How trajectories reach reflection

`gradient/reflect.py`: failures and successes are shuffled and split into minibatches of
`minibatch_size`; each minibatch is one optimizer call whose user message is the current skill,
the edit budget, the step buffer (previous patterns and rejected edits), the meta-skill memory
and the formatted trajectories. `fmt_trajectory()` renders `{"type": "tool_call", "cmd", "obs"}`
records as `[action]`/`[obs]` lines, `{"role": "system"}` records as `[verification]` lines, and
anything else by role. Each trajectory gets a header with the task, task type, `fail_reason`,
the hidden `reference_text`, and the target system/user prompts. **Nothing is truncated**
(`_clip_text`, line 54, ignores its limit), so the adapter must write compact trajectories
(section 4.4) or one minibatch of agentic traces can exceed 200k tokens.

### 2.6 Usage reporting

Chat backends record per-stage tokens (`analyst`, `merge`, `ranking`, `rewrite`, `slow_update`,
`meta_skill`, ...) in per-backend `TokenTracker`s; the trainer writes per-step deltas to each
`steps/step_NNNN/step_record.json` (`tokens`) and totals to the run summary. The Claude Code
chat backend reports `input_tokens`/`output_tokens` from the CLI's result event (no cache
fields, no cost). Exec rollouts report nothing (2.4). Our adapter records, per rollout: Codex
`turn.completed` usage (`input_tokens`, `cached_input_tokens`, `cache_write_input_tokens`,
`output_tokens`, summed over turns) and the computed USD, with cache writes at their own price
(`budget.CACHE_WRITE_PRICES`; until round 3b they were priced as plain input, about 12 % low); Claude Code's result event (`usage` with cache fields,
`total_cost_usd` as an API-equivalent figure, `num_turns`, `duration_ms`). The runner appends
Codex spend to the programme ledger during the run (section 8.3).

### 2.7 Knobs that drive cost

| Knob | Effect |
|---|---|
| `train.num_epochs`, `train.batch_size`, `train.accumulation`, train split size | Steps = epochs x ceil(train / (batch x accumulation)); each step costs B train rollouts and (usually) V selection rollouts. |
| `evaluation.sel_env_num` (V) | Paid at baseline, at almost every step, at the slow-update gate and at the final check: the largest single multiplier after the step count. |
| `evaluation.test_env_num` (T), `eval_test` | 2-3 x T at the end. |
| `optimizer.use_slow_update`, `slow_update_samples`, `slow_update_gate_with_selection` | 2 x samples (+ V if gated) per epoch from the second. |
| `optimizer.use_meta_skill` | Free when slow update ran; else 2 x samples per epoch. |
| `optimizer.longitudinal_pair_policy: changed` | Top-up rollouts; keep `mixed`. |
| `gradient.minibatch_size`, `merge_batch_size`, `failure_only` | Number of optimizer calls; `failure_only` halves analyst calls. |
| `gradient.analyst_workers` | Parallelism only. |
| `optimizer.skill_update_mode` | `patch` (default) is cheapest; `rewrite_*` adds a rewrite call with `model.rewrite_max_completion_tokens` (64000 by default); `full_rewrite_minibatch` makes every analyst call a full rewrite. |
| `model.reasoning_effort`, `rewrite_reasoning_effort`, `claude_code_exec_effort` | Optimizer thinking tokens. |
| `optimizer.use_skill_aware_reflection`, `skill_aware_consolidate_threshold` | Extra consolidation calls. |
| `lr_control_mode: autonomous` | One extra call per step. |
| `env.exec_timeout`, `EXEC_EMPTY_RESPONSE_RETRIES` | Only for SkillOpt's own harness; ours has its own timeout and no automatic retry. |

## 3. Harness isolation (verified)

Every claim here was checked with a real session; the evidence (the harness's own event
streams, JSON) is kept in the run's scratch directory, not in the repository.

### 3.1 Claude Code 2.1.283

**What does not work.** A scratch `CLAUDE_CONFIG_DIR` answers `Not logged in · Please run
/login` (the plan's OAuth credentials belong to the user configuration), and `--bare` accepts
only an API key. Claude Code also loads `AGENTS.md` from ancestor directories (built-in
`agents-md` plugin), so a workspace under the home directory would pick up a home-level
`AGENTS.md`.

**Method.** Keep the user configuration for authentication and switch everything else off:

- flags: `--setting-sources project`, `--strict-mcp-config --mcp-config '{"mcpServers":{}}'`,
  `--settings '{"disableAllHooks":true}'`, `--no-session-persistence`, an explicit `--tools`
  list, `--output-format stream-json --verbose --include-hook-events`;
- environment (built from an allowlist, never inherited): `ENABLE_CLAUDEAI_MCP_SERVERS=false`,
  `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`, `CLAUDE_CODE_DISABLE_BUNDLED_SKILLS=1`,
  `CLAUDE_CODE_DISABLE_CLAUDE_API_SKILL=1`, `CLAUDE_CODE_DISABLE_POLICY_SKILLS=1`,
  `CLAUDE_CODE_DISABLE_WORKFLOWS=1`, `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`,
  `DISABLE_DOCTOR_COMMAND=1`, `DISABLE_BUILTIN_AGENTS=1`;
- a workspace outside the home tree; the skill under test as a project skill,
  `.claude/skills/<name>/SKILL.md`.

**Proof.** The session's `init` event lists what it loaded; the probe also asks the model.

| Session | Skills | Plugins | MCP servers | Hook events | Model's own answer |
|---|---|---|---|---|---|
| No isolation (control) | 146 (user skills incl. the google-agents-cli set, plugin and synced skills, 18 bundled) | 6 | 24 (16 plugin, 8 claude.ai connectors) | 17 (user SessionStart hooks ran) | lists them |
| Flags only | probe + 18 bundled Claude Code skills | `agents-md`, `telemetry` (built-in) | 0 | 0 | - |
| Flags + bundled/policy/workflow switches | probe + `doctor` | `agents-md` | 0 | 0 | "SKILLS: gac-probe-skill" |
| **Final** (flags + all switches above) | **probe only** | `agents-md` (built-in; reads `AGENTS.md` in the workspace chain, of which there is none) | **0** | **0** | "SKILLS: gac-probe-skill ... MCP: NONE ... INSTRUCTIONS: NONE", probe word returned via the Skill tool |

A real rollout with the final settings and the `graph-agents-cli-workflow` skill listed exactly
`["graph-agents-cli-workflow"]`, invoked it through the Skill tool, answered correctly, and its
shell PATH contained only the scratch bin and system directories (the developer's
`~/.local/bin`, which holds `google-agents-cli` and `agents-cli`, did not reach it).

**Shell sandbox.** Claude Code's Bash sandbox (seatbelt on macOS) confines what the agent's
commands can do. Findings:

- It takes effect only from a settings **file**: `sandbox` in `--settings` together with
  `--permission-mode bypassPermissions` left commands unsandboxed (example.com reachable, a
  write outside the workspace succeeded). From the workspace's `.claude/settings.json`, with
  `--permission-mode acceptEdits` and `--permission-prompts none` (anything that would prompt is
  denied), it works: `pypi.org` 200, `example.com` refused by the proxy (403), a write outside
  the workspace "operation not permitted", reads of denied credential directories refused.
- It always allows writes under Claude Code's own temp root (`/private/tmp/claude-<uid>`),
  which here contains the whole session scratchpad (ledger, other workspaces). Moving
  `CLAUDE_CODE_TMPDIR` does not change that; denying the temp root breaks Claude Code's own
  shell bookkeeping; denying the scratchpad while the workspace is inside it also denies the
  workspace (deny wins over allow). **Working layout**: workspaces under
  `/private/tmp/gac-x-skillopt/<run>/<rollout>/`, `denyWrite` on the scratchpad. Verified:
  workspace writable, scratchpad and other paths not, PyPI yes, other hosts no.
- `allowLocalBinding: true` is required: `eval run` and `run` start the project's server on
  `127.0.0.1:<GRAPH_AGENTS_CLI_RUN_PORT>`. With it, `graph-agents-cli lint` and `eval run`
  (fake provider) passed inside the sandbox and the server was stopped.

The settings file (`spike/harness_env.py`, `claude_project_settings`) is:
`disableAllHooks`, `sandbox.enabled`, `autoAllowBashIfSandboxed`, `allowUnsandboxedCommands:
false`, `network.allowedDomains: [pypi.org, files.pythonhosted.org]`, `network.allowLocalBinding:
true`, `filesystem.denyRead: [credential dirs]`, `filesystem.denyWrite: [scratchpad]`.

**Residual.** The Read/Edit tools are governed by permissions, not the seatbelt: `acceptEdits`
lets the agent edit files in the workspace. Since round 3a the workspace's `.claude/` (the
sandbox settings and the skill under test) is denied to Edit and Write. Claude Code reloads a
changed settings file, so an edit could otherwise loosen the sandbox mid-session. The harness
also records the settings file's hash and invalidates a rollout that changed it. Reads outside
the workspace are possible except for the denied directories. The shell can read another run's
workspace under the workspace root, which is not in the deny lists
([`results/preflight-r3.md`](results/preflight-r3.md), finding 2). The session also carries the logged-in
account's email address (Claude Code puts it in the context; no setting found to remove it): one
round-2 rollout used it as a `dev-token --sub`. It is not a credential and stays in the stored
traces; no candidate or patch has contained it.

**Permission mode (round 3a).** Rollouts run in `acceptEdits` with `--permission-prompts none`:
anything that would prompt is denied. The mode is `env.harness_permission_mode`, or
`--permission-mode` on `preflight`, `rollout` and `baseline run`. Every result records it, with the
session's own `init.permissionMode`.

The owner's choice of `bypassPermissions` with the sandbox kept failed its preflight on 2.1.283
([`results/preflight-r3.md`](results/preflight-r3.md)), so the mode stayed. The shell stayed
confined, but two things escaped:

- **File tools.** The Write and Edit tools wrote outside the workspace. Bypass allows every tool
  call a deny rule does not match, and no setting confines file-tool writes to the working
  directories.
- **Network.** example.com answered 200. A non-allowlisted host makes the sandbox ask for network
  access (`SandboxNetworkAccess`), and bypass approves the request. `sandbox.network.strictAllowlist:
  true` denies instead; Claude Code honours it only from user, managed or `--settings` settings.

`dangerouslyDisableSandbox` never reached the filesystem in either mode, because
`allowUnsandboxedCommands: false` is set.

### 3.2 Codex 0.154.0

**Method.** A scratch `CODEX_HOME` with an API-key login (`codex login --with-api-key`, key on
stdin from the key file; never on a command line or in output) and a generated `config.toml`;
a scratch `HOME`; the skill under test in `.agents/skills/<name>/` (a native Codex repo-skill
location); `codex exec --json --ephemeral --ignore-rules --skip-git-repo-check`, stdin closed.

**Proof** (the model lists its skills; Codex has no init event):

| Session | Skills listed |
|---|---|
| Scratch `CODEX_HOME`, real `HOME` | 5 Codex system skills (installed into `CODEX_HOME/skills/.system` on first run) + the probe + the google-agents-cli skills from `~/.agents/skills` |
| **Final**: scratch `HOME`, system skills disabled with `[[skills.config]] name=... enabled=false`, features `apps`, `browser_use*`, `computer_use`, `image_generation`, `in_app_browser`, `multi_agent`, `plugins`, `remote_plugin`, `hooks`, `memories` off, `web_search = "disabled"` | **probe only**; "MCP: NONE", "INSTRUCTIONS: NONE" |

**Sandbox.** The legacy `workspace-write` mode with `network_access = false` blocks all
network, but also blocks binding 127.0.0.1 (so `eval run` cannot start its server), and by
default leaves `/tmp` and `$TMPDIR` writable (fixed with `exclude_slash_tmp` /
`exclude_tmpdir_env_var`). The design uses a **permission profile** instead
(`default_permissions = "rollout"`, `features.network_proxy = true`, `extends = ":workspace"`,
`network.enabled = true`, `allow_local_binding = true`, domains `pypi.org` and
`files.pythonhosted.org` allowed, `":slash_tmp" = "read"`, credential directories and the scratch
`CODEX_HOME` (its `auth.json` holds the key) `deny`), run without `--sandbox` (the flag selects
the legacy settings). Round 3a added `~/.codex` itself to the denied directories; the standalone
Codex install lives in `~/.codex/packages`, and `codex exec` re-executes that binary inside the
sandbox, so round 3b adds a more specific `read` rule for `~/.codex/packages` only
(`isolation.codex_readable_install`; without it every Codex session fails at start). Verified: PyPI 200, `example.com` "blocked: domain is not on the
allowlist", scratchpad write denied, key-file read denied, workspace writable, `graph-agents-cli
lint` passes. `":slash_tmp" = "deny"` is too strong: it also stops the rollout executing the
scratch `graph-agents-cli` and `uv`.

Round 3c denies, to both harnesses, the copies of the skills outside the workspace that the
harness knows of (`isolation.skill_copy_dirs`):

- the shipped skills bundled in this bench's scratch CLI build
  (`site-packages/graph_agents_cli/skills/data`). Only `setup` reads them, and a round-3b Codex
  rollout read the bundled workflow `SKILL.md` while a candidate was under test;
- uv's cache (`~/.cache/uv`), which unpacks every graph-agents-cli wheel ever installed;
- uv's tool directory (`~/.local/share/uv/tools`), which holds other agent CLIs.

Another bench's scratch CLI build is denied only when `GAC_SKILLOPT_DENY_READ` names it. Verified
with `codex sandbox` under the generated profile and with the Claude preflight's
`skill_copies_unreadable` check ([`results/review-r3c-workflow.md`](results/review-r3c-workflow.md)).

**Blocked at first, fixed since.** Inside this sandbox `graph-agents-cli eval run` failed for
two product reasons (section 9, issues 1 and 2): the proxy puts a SOCKS URL in the environment
and the CLI's httpx client raised `ImportError` (no `socksio`), and the sandbox denies listing
processes, so the server teardown raised `PermissionError` and left the server running. Both are
fixed on this branch (`fix:` commits 03d3e0f and 2e14e97, see section 9), and Codex rollouts run
`eval`/`run` themselves.

**Model.** `gpt-6-sol` runs, but Codex 0.154.0 warns "Model metadata for `gpt-6-sol` not found.
Defaulting to fallback metadata" (its catalog, `codex debug models`, lists gpt-6-astra,
gpt-5.6-sol/-terra/-luna, gpt-5.5, gpt-5.4(-mini), gpt-5.2). `gpt-5.6-terra` runs without the
warning at a similar price.

### 3.3 Common rollout environment

- Environment from an allowlist (`PATH`, `HOME`, `USER`, `LOGNAME`, `SHELL`, `TMPDIR`, `LANG`,
  `LC_*`, `TERM`, `SSL_CERT_FILE`), then: `PATH` = scratch bin + system and Homebrew
  directories; `KUBECONFIG` = an empty scratch file; `MODEL_PROVIDER=fake`;
  `GRAPH_AGENTS_CLI_NO_UPDATE_CHECK=1`; `GRAPH_AGENTS_CLI_RUN_PORT` from the slot's port;
  `TMPDIR` and `UV_CACHE_DIR` inside the workspace. A rollout started from inside an agent
  session must not inherit `CLAUDECODE`, `CLAUDE_CODE_SESSION_ID` or the messaging socket and
  token variables; the allowlist drops them.
- Scratch bin: `graph-agents-cli` built from this branch (`uv tool install --from <checkout>`
  into a scratch `UV_TOOL_DIR`), and a current `uv`. **uv older than 0.9.29 panics in both
  sandboxes** ("Tokio executor failed", SCDynamicStore blocked; fixed upstream in 0.9.29), and
  this machine's uv is 0.9.2; the spike used 0.12.19.
- Ports: 22000-22049 belong to this track. Slot `i` gets `22010 + i` for the agent's server and
  `22030 + i` for the verifier's; at most 20 slots. After every rollout the harness kills
  anything still listening on the slot's ports whose command line is inside the workspace, and
  the workspace's process group. `playground` defaults to port 8000 (another project's server
  listens there); the CLI refuses a busy port, and tasks never ask for `playground`.
- Workspaces are deleted after the verifier has copied what reflection and the report need.

### 3.4 Per-run preflight

Isolation depends on the harness versions and on the developer's configuration, both of which
change. The runner re-proves it before every training run: one probe session per harness
(`spike/claude_isolation_probe.py` / `codex_isolation_probe.py` logic), and refuses to start
unless the listed skills are exactly the probe, no MCP server or hook event appears, the plugin
list is at most `agents-md`, and the rollout PATH has no `google-agents-cli`/`agents-cli`. It
also checks `uv --version` in the scratch bin and that no `~/.claude/CLAUDE.md` exists (user
memory could not be tested for leakage because none exists on this machine).

**The Claude preflight as built (round 3a, `gac_skillopt/preflight.py`).** It runs one session in
a real rollout workspace, in the permission mode under test. The session tries every way out:

- writes outside the workspace, with the shell and with the Write and Edit tools: a
  write-denied scratch path, `$HOME`, and another run's workspace;
- reads of the key directory, a gold solution in the checkout and a marker in `<scratch>/runs`,
  with the shell and with Read, Grep and Glob. The key directory is probed for names and sizes
  only;
- example.com against PyPI;
- two commands with `dangerouslyDisableSandbox`;
- an Edit and a shell append to its own `.claude/settings.json`, then one more
  `dangerouslyDisableSandbox` write.

The checks are judged from the files on disk and the tool calls' real inputs and results, never
from the model's report. A step the model did not attempt fails its check.

### 3.5 Measured trivial rollouts

| Harness | Model, effort | Rollout | Wall | Tokens | Cost |
|---|---|---|---|---|---|
| Claude Code | sonnet, medium | real workflow skill, answer + env check (7 turns) | 14.6 s | 24.5k cache write, 81.8k cache read, 0.8k out | $0.12 API-equivalent (plan, not billed) |
| Claude Code | sonnet, default | sandboxed: `lint` + `eval run` on a created project (11 turns) | 29.9 s | - | $0.08 API-equivalent |
| Codex | gpt-6-sol, medium | real workflow skill, answer + env check | 14.8 s | 37.8k in (25.3k cached), 1.2k out | $0.042 |
| Codex | gpt-5.6-terra, medium | same | 15.9 s | 86.1k in (68.8k cached), 1.7k out | $0.068 |
| Codex | gpt-6-sol, low | sandboxed: `lint` + `eval run` | 13.5-21 s | 35-53k in (26-44k cached), 0.7-1.1k out | $0.03-0.04 |

Project setup outside the agent (`create` + `install` from a warm uv cache) takes 1-3.5 s.
Verifier building blocks: `lint` 0.7 s, `eval run` with the fake provider 1.5 s, `deploy --env
dev --dry-run` 0.5 s (works with an empty kubeconfig), the full generated test suite 85 s (too
slow per rollout; tasks use targeted tests).

## 4. Integration design

### 4.1 Layout (`tools/skillopt/`, to be built after this design)

```
tools/skillopt/
  DESIGN.md                  this document
  README.md                  how to run, where results go, review and write-back procedure
  requirements.txt           skillopt pinned by commit (never in the CLI's pyproject)
  gac_skillopt/
    adapter.py               GacSkillsAdapter(EnvAdapter) + prompt overrides
    dataloader.py            GacTaskLoader(SplitDataLoader) over tasks/<skill>/
    harness/claude.py        isolated claude -p runner (from spike/harness_env.py)
    harness/codex.py         isolated codex exec runner
    workspace.py             fixtures, slots and ports, integrity hashes, cleanup
    verify.py                check types, scoring
    trace.py                 event stream -> compact conversation.json
    factcheck.py             candidate gate (section 6)
    budget.py                per-rollout usage, live projection, ledger appends, run cap
    writeback.py             best_skill.md -> skills/ and the bundled copy (after review)
    run.py                   preflight, registration, scripts.train.main()
  bin/claude-isolated        wrapper used as model.claude_code_exec_path for optimizer calls
  tasks/<skill>/<task-id>/   task.json, fixture/, hidden/ (verifier-only files)
  splits/<skill>.json        frozen train/val/test assignment + content hashes
  configs/{claude,codex}/<skill>.yaml
  spike/                     this spike
```

Results (SkillOpt `out_root`, traces, verifier logs, proposed skill diffs) live in scratch; the
branch gets only the tooling, the tasks and a summary of each run.

### 4.2 From six skills to SkillOpt's single document

- **Per-skill runs, not one joint document.** Each skill is its own document with its own
  trigger description, and the bundle test requires the six to stay separate; a joint document
  could not be written back without re-splitting it by hand, and one skill's regressions would
  hide behind another's gains.
- **Trainable text**: the `SKILL.md` body after the frontmatter. The frontmatter (`name`,
  `description`, `metadata.version`, `metadata.license`, `requires`) is re-attached verbatim at
  render time: the `description` decides triggering across harnesses and is a separate
  experiment if at all.
- **References** ship unchanged in every rollout at `<skill>/references/`, so the body can keep
  pointing to them and the agent can read them. `workflow/references/commands.md` and
  `scaffold/references/flags.md` must equal the real `--help` output and are never trained. A
  later stage may train one prose reference at a time as its own document with the tuned
  `SKILL.md` frozen; not in the first round.
- **Other skills**: the programme requires rollouts to see only the skill under test, so the
  other five are absent. This matters most for `graph-agents-cli-workflow`, which routes to the
  others; installing the five frozen siblings for that skill's run is an open decision
  (section 10).
- **Rendering**: frontmatter + body; SkillOpt's protected regions (`<!-- SLOW_UPDATE_START -->`
  ... and `<!-- APPENDIX_START -->` ...) stay in the rendered file during training (the agent sees
  the slow-update guidance) and are converted to a normal section at write-back.

### 4.3 One rollout

1. **Fixture** (harness, outside any sandbox): empty workspace, or `graph-agents-cli create`
   with the task's arguments, `install` from the warm uv cache, `.env` from `.env.example` with
   `API_KEY=dev`, then the task's fixture overlay (files that set up the scenario, e.g. a broken
   dataset). The workspace is the project root, as a user would have it.
2. **Skill**: render the candidate and copy it with its references into `.claude/skills/<name>/`
   or `.agents/skills/<name>/`; write the Claude sandbox settings; record hashes of the skill
   files and the settings file.
3. **Agent**: the task's prompt, prefixed with one fixed sentence for every candidate
   ("graph-agents-cli and its agent skills are installed; the project uses the fake model
   provider"), no mention of how to use the skill. Timeout per task (default 900 s). No automatic
   retry; an infrastructure failure (rate limit, 5xx, harness crash before the first turn) is
   rerun once and marked.
4. **Verifier** (harness, outside the sandbox): copy `hidden/` in, run the checks (section 5.3)
   with `MODEL_PROVIDER=fake`, `API_KEY=dev`, the slot's verifier port, empty kubeconfig.
5. **Integrity**: skill files and settings unchanged; nothing left listening; a hash mismatch
   invalidates the rollout (score 0, `fail_reason: integrity`).
6. **Result**: `{id, hard, soft, task_type, task_description, fail_reason, reference_text,
   n_turns, usage, usd, wall_s, skill_loaded}`; `conversation.json` per 4.4.

`skill_loaded` (the Skill tool was called, or the skill's `SKILL.md` was read) is reported per
run: a body change cannot help a rollout that never loads the skill.

### 4.4 Trajectories for reflection

`trace.py` turns the stream (Claude `stream-json`, Codex `--json` items) into
`conversation.json`: the user prompt; one `{"type": "tool_call", "cmd", "obs"}` per tool call
with the observation cut to its first 800 and last 800 characters; reads of the skill itself
replaced by `[loaded SKILL.md]` / `[read references/<file>]` (the analyst already has the
skill); the final message; then one `{"role": "system"}` entry per verifier check with its
command, exit code and the tail of its output. Budget: 24k characters per trajectory, cut from
the middle of the tool calls, never from the verifier entries. The task's hidden reference goes
in `reference_text`.

### 4.5 Analyst prompts

The adapter returns SkillOpt's generic analyst prompts plus a fixed addendum: the document is a
coding-agent skill for graph-agents-cli with a frontmatter-free body; keep `## Not covered by
this skill` and `## Migration note`; do not invent commands, flags, environment variables or
exit codes (a fact-check rejects them); prefer `insert_after`/`replace` in the section that owns
the topic over `append` (which lands after `## Migration note`); keep edits general, never
task-specific values; do not grow the document by more than the edit needs.

### 4.6 Write-back

`writeback.py --skill <name> --from <run>/best_skill.md` (run by a person after reviewing the
diff): re-attach the frontmatter, convert protected regions to prose, write `skills/<name>/
SKILL.md`, sync the bundle exactly as CONTRIBUTING describes (`rm -rf
src/graph_agents_cli/skills/data/graph-agents-cli-*`, `cp -R skills/graph-agents-cli-* ...`, the
README copy), then `diff -r -x .DS_Store skills src/graph_agents_cli/skills/data`, `uv run pytest
tests/skills -q`, `uv run ruff format --check .`, and the fact-check gate once more. The change
lands as its own reviewed commit with a CHANGELOG entry; the run summary (scores, cost, the
accepted edits) goes into the pull request description.

## 5. Benchmark

### 5.1 Task schema

```json
{
  "id": "eval-add-check-02",
  "skill": "graph-agents-cli-eval",
  "family": "eval-add-check",
  "task_type": "eval/dataset",
  "fixture": {
    "kind": "project",
    "create_args": ["--deployment-target", "none", "--prototype"],
    "overlay": "fixture/",
    "install": true
  },
  "prompt": "Add an eval case that asks for the weather in Lisbon and checks that the agent calls the weather tool with city Lisbon. Show me the eval result.",
  "timeout_s": 900,
  "checks": [
    {"id": "dataset-valid", "type": "cmd", "run": "graph-agents-cli eval run", "expect_exit": [0, 1], "mandatory": true},
    {"id": "case-added", "type": "json", "file": "tests/eval/datasets/basic-dataset.json",
     "select": "cases[?contains(input, 'Lisbon')]", "expect": {"count": 1}, "mandatory": true},
    {"id": "tool-assert", "type": "json", "file": "tests/eval/datasets/basic-dataset.json",
     "select": "cases[?contains(input, 'Lisbon')].expect.tool_calls[0]",
     "expect": {"name": "get_weather", "args_subset": {"city": "Lisbon"}}, "weight": 2},
    {"id": "lint", "type": "cmd", "run": "graph-agents-cli lint", "expect_exit": [0]},
    {"id": "untouched", "type": "unchanged", "paths": ["app/", "pyproject.toml"]},
    {"id": "ran-eval", "type": "transcript", "must_run": ["graph-agents-cli eval run"]}
  ],
  "reference": "A correct solution adds one case with a tool-call expectation using args_subset {city: Lisbon}, keeps the dataset schema, runs eval run and reports the per-status counts and the exit code, noting that the fake model proves plumbing only."
}
```

(The JSON paths above are illustrative; the check vocabulary is the contract.)

### 5.2 Task families per skill

Each skill gets 8-10 families with 3-4 parametrised variants each (30 tasks). Families come from
the skill's own claims (procedures, "Common gotchas"/"Common mistakes", "Troubleshooting",
"Proving your work"), the command reference, the get-started paths of the documentation site,
the end-to-end test flows, and fixed issues in the changelog and known-issues list.

| Skill | Example families (all verified deterministically) |
|---|---|
| scaffold | create with a given runtime x checkpointer x target x CD x auth combination (manifest keys, files present/absent, lint); refuse an invalid combination; prototype semantics; `--api-policy` seed; `scaffold enhance` (add CD, set the registry); `scaffold upgrade --dry-run` report |
| langgraph-code | add a tool with its `API_CALLS` declaration and policy entry (`api check`, lint, hidden unit test with the fake provider); add a `StateGraph` node; add a human-approval gate on an API; stream events; provider switch via settings; keep tool results untrusted (hidden prompt-injection test) |
| eval | add a case with tool-call assertions; configure a quality metric (`threshold` + `min_pass_rate`); fix an incomplete run (exit 2) or a config error (exit 3); multi-turn `scope: all_turns`; `eval compare` a regression; report results honestly on the fake model |
| deploy | set registry and environment values, then `deploy --env dev --dry-run` succeeds and renders the expected resources; secrets allowlist; argocd files for `--cd argocd`; refuse direct staging deploys in helm-push mode; troubleshooting from given pod events |
| observability | enable tracing for an environment (values and `.env` keys); capture policy; hashed principal id; never-captured fields stay off; run records |
| workflow | end-to-end mini projects (create, add a tool, prove with lint + tests + eval); code preservation (no unrelated edits); systematic debugging of a seeded failure; stopping at the right phase |

### 5.3 Verifier

Check types: `cmd` (command and accepted exit codes, optional output regex), `pytest` (hidden
test files copied in after the agent exits; targeted, not the full suite), `eval` (`eval run`
exit code and the results file's gate and per-status counts), `api_check`, `deploy_dry_run`
(exit code plus assertions on the rendered manifests), `manifest` (key in
`graph-agents-cli-manifest.yaml`), `json`/`yaml` (path assertions), `file` (exists, absent,
regex), `unchanged` (paths identical to the fixture), `transcript` (commands the agent ran or
must not run, e.g. `playground`, `google-agents-cli`, a real provider). A transcript pattern
matches at the start of each simple command, after variable assignments and wrappers (`env`,
`timeout`, a program's directory and, since round 3c, `uv run` with its options) are dropped;
before that, `uv run graph-agents-cli eval run` failed `must_run: graph-agents-cli eval run`.

Scoring: `hard = 1` when every mandatory check passes; `soft` = weighted fraction of passed
checks; the gate uses `mixed` (0.5 hard + 0.5 soft), which SkillOpt documents for small
selection splits with partial credit. Verifiers never call a model and never touch a cluster;
every check has a timeout.

### 5.4 Splits

30 tasks per skill: 15 train, 7 selection (`val/`, SkillOpt's `valid_seen`), 8 test (`test/`,
`valid_unseen`). The assignment is frozen in `splits/<skill>.json` with content hashes before the
first run, and test tasks are never used for development of the harness.

**Hold-out rule (decided for round 2, 2026-09-27).** The first design held out two whole families
for test. The round-1 baseline showed the cost: every failing behaviour sat in a test-only family
(the unattended spec gate, the guidance-file choice, the configuration-only salt), val scored
`hard` 1.00 for every skill, and SkillOpt's strict gate (`cand > current` on val) could accept no
edit. The rule is now:

- test holds out **variants within families**, not whole families;
- test tasks stay **frozen and unseen**: their ids, files and content hashes do not change, they
  are in no other split, and nothing is authored or tuned against them;
- a train or val task may belong to a test task's family (the same failure family), but it must
  differ in fixture, prompt and expected specifics, so that no test answer leaks: another
  project and domain, another environment, identity type, policy or CD mode, another prompt.
  `validate` enforces the mechanical part (`tasks.holdout_problems`: at least one test task per
  skill, every test task frozen, and a variant in a test family never reuses its project name
  or prompt); the rest is review.

Round-2 acceptance for SkillOpt follows from it: training on these splits should rediscover the
three targeted edits (the workflow skill's rule for unattended sessions, the scaffold skill's
guidance-file wording, the observability skill's salt procedure marked configuration-only) from
train and val variants alone; the skills are not hand-edited to add them.

## 6. Fact-check gate

`factcheck.py` runs on every candidate document before any rollout (inside the adapter's
`rollout()`: a failing candidate returns `hard = soft = 0` for every item with the violation as
`fail_reason`, so the gate rejects it). The reason reaches the run log and the candidate's
selection results, not the analysts: SkillOpt's step buffer tells the next step only which edits
were rejected and at what score (found in the round-2 smoke train). It checks:

1. Required sections present: `## Not covered by this skill`, `## Migration note`; Google Cloud
   product names only under a heading containing "Migration" (the rules of
   `tests/skills/test_bundle.py`, imported rather than copied).
2. Every `references/<file>.md` it mentions exists; every reference file is still mentioned.
3. Every `graph-agents-cli ...` invocation in code spans and fences resolves in the real Click
   tree (`graph_agents_cli.main`, loaded from the branch's build): the subcommand path exists,
   every option is a real, non-hidden option of that command; every `GRAPH_AGENTS_CLI_*`
   variable exists in the CLI source or in `commands.md`.
4. Python blocks pass `ruff format --check` (CI formats Markdown Python blocks).
5. Size: body at most 1.25 x the original (configurable).

Claims that are not machine-checkable (behaviour described in prose) are for the human review
of the final diff.

## 7. Configurations

Claude Code, full size (tier 1 skills: eval, scaffold, langgraph-code):

```yaml
# The installed package ships no configs/: _base.yaml is a copy of SkillOpt's
# configs/_base_/default.yaml at the pinned commit.
_base_: ../_base.yaml
model:
  optimizer: opus
  target: sonnet
  optimizer_backend: claude_code_exec
  target_backend: claude_code_exec
  claude_code_exec_path: tools/skillopt/bin/claude-isolated
  claude_code_exec_use_sdk: cli        # the SDK path would bypass the wrapper
  claude_code_exec_effort: high        # optimizer calls only; rollouts use harness_effort
train: {num_epochs: 3, batch_size: 5, accumulation: 1, seed: 42}
gradient: {minibatch_size: 3, merge_batch_size: 4, analyst_workers: 4, failure_only: false}
optimizer:
  learning_rate: 3
  min_learning_rate: 1
  lr_scheduler: cosine
  lr_control_mode: fixed
  skill_update_mode: patch
  use_slow_update: true
  slow_update_samples: 5
  slow_update_gate_with_selection: true
  longitudinal_pair_policy: mixed
  use_meta_skill: true
  use_skill_aware_reflection: false
evaluation: {use_gate: true, gate_metric: mixed, gate_mixed_weight: 0.5, sel_env_num: 7, test_env_num: 8, eval_test: true}
env:
  name: gac_skills
  skill: graph-agents-cli-eval
  harness: claude
  harness_model: sonnet
  harness_effort: medium
  harness_permission_mode: acceptEdits   # round 3a: bypassPermissions failed its preflight
  val_reps: 2                            # round 3a: each selection item k times, mean per item
  split_mode: split_dir
  split_dir: tools/skillopt/splits/graph-agents-cli-eval
  slots: 4
  port_base: 22010
  task_timeout_s: 900
```

Tier 2 (deploy, workflow, observability): the same with `num_epochs: 2`.

Codex (eval skill): as tier 2, with `target_backend: codex_exec`, `harness: codex`,
`harness_model: gpt-5.6-terra`, `harness_effort: medium`, `slots: 6`, and `max_usd: 60` (the
runner's hard stop for this run). The optimizer stays on Claude Code.

## 8. Cost and time

### 8.1 Rollouts per run

R = V + S x E x (B + V) + (E - 1) x (2n + V) + V + 3T at most (S steps per epoch, E epochs, B
batch, V selection, T test, n slow-update samples):

| Config | S x E | Rollouts (upper bound) | Optimizer calls |
|---|---|---|---|
| Claude tier 1 (E=3) | 3 x 3 | 7 + 108 + 34 + 7 + 24 = **180** | about 50 |
| Claude tier 2 / Codex (E=2) | 3 x 2 | 7 + 72 + 17 + 7 + 24 = **127** | about 34 |
| Transfer check (baseline and optimised skill on test) | - | 16 | 0 |

### 8.2 Estimates (to be recalibrated on the first run)

Real tasks are 10-40 turns, not the 1-11 of the spike. Assumed per rollout: Codex
gpt-5.6-terra 0.4-1.0M input tokens (85 % or more cached) and 8-20k output, **$0.25-0.60,
central $0.40**, 3-8 min plus 0.5-1.5 min of verification; Claude Code sonnet 2-6 min plus
verification (API-equivalent $0.5-1.5, not billed).

| Item | Rollouts | OpenAI cost (central, range) | Claude sessions | Wall time |
|---|---|---|---|---|
| Claude, 3 tier-1 skills | 3 x 180 | $0 | about 690 | about 5 h each at 4 slots |
| Claude, 3 tier-2 skills | 3 x 127 | $0 | about 485 | about 3.5 h each |
| Codex, eval skill optimisation | 127 | $51 ($32-76) | about 34 (optimizer) | about 3 h at 6 slots |
| Codex, transfer of the other five optimised skills | 80 | $32 ($20-48) | 0 | about 1.5 h |
| **Track A total** | 1,128 | **$83 ($52-124)**, capped at **$90** | about 1,210 | about 30 h of runs |

If the optimizer ran on OpenAI (`gpt-5.6-terra`, about 40k in / 4k out per call) it would add
$5-7 per run. A cheaper Codex target (`gpt-5.6-luna`, a tenth of the price) would allow full
Codex optimisation of all six skills for about $35, at the cost of tuning against a much
weaker model.

### 8.3 Budget controls

- Before a Codex run: `spend.py check --need <projected run cost>`; refuse on exit 1.
- During the run the adapter sums actual usage per rollout at the recorded prices, appends to
  the ledger every 10 rollouts or 5 minutes, and after the baseline selection (7 rollouts)
  projects the whole run from the measured mean; it stops launching rollouts (SkillOpt resumes
  from `runtime_state.json` later) when the projection or the ledger total would cross the
  run's `max_usd`, the Track A cap, or the programme's stop-at.
- Claude runs back off on rate limits (exponential, then pause the run) and report session
  counts per run.
- The spike's own spend and session counts are in the report accompanying this commit.

## 9. Issues found during the spike

| # | Severity | Category | Finding | Root cause |
|---|---|---|---|---|
| 1 | major (fixed, 03d3e0f) | CLI, robustness | `run`, `eval run` and `eval generate` crash with an unhandled `ImportError: Using SOCKS proxy, but the 'socksio' package is not installed` whenever a SOCKS proxy is in the environment (`ALL_PROXY=socks5h://...`), even with `NO_PROXY=localhost,127.0.0.1` and even though the server is on loopback. Codex's network proxy sets such a variable, so no Codex agent with network can run eval; proxy tools on developer machines do too. | `src/graph_agents_cli/run/_local_server.py:579` builds `httpx.get(...)` with the environment's proxies for a loopback URL and catches only `httpx.HTTPError` (580); the same pattern in `src/graph_agents_cli/_chat_client.py` (265, 354, 377, 391, 414, 431, 451); `pyproject.toml:26` depends on `httpx` without the `socks` extra. |
| 2 | major (fixed, 2e14e97) | CLI, cleanup | When the OS denies listing processes (Codex's macOS sandbox; reproduced with `sandbox-exec` denying `sysctl kern.proc.all`), `eval run` passes every case, then fails with `PermissionError ... (originated from sysctl() malloc 1/3)` and leaves the uvicorn server running, reparented to PID 1, holding the port; on a failed start the same error replaces the original one and also leaks the server. | `src/graph_agents_cli/run/_local_server.py:819-822`: `parent.children(recursive=True)` calls `psutil.pids()`, which raises `PermissionError` (an `OSError`, not a `psutil.Error`), and only `psutil.Error` is caught; `_terminate_process` is reached from the start-failure cleanup (384) and from normal teardown. The own-child `Popen` handle could stop the server without psutil. |
| 3 | minor | environment, docs | uv older than 0.9.29 panics inside the macOS agent sandboxes (Claude Code's, Codex's), so `install`, `lint` (via `uv run ruff`) and `eval` fail with "Tokio executor failed"; CI and CONTRIBUTING pin uv 0.9.2. | Upstream: uv's `system-configuration` use blocked by the sandbox, fixed in uv 0.9.29 (astral-sh/uv#17829). A note in the skills' troubleshooting would help agent users. |
| 4 | minor | upstream | SkillOpt 0.2.0 on PyPI ships no `skillopt/prompts/*.md`: `load_prompt('analyst_error')` raises `FileNotFoundError`, so training cannot reflect. | Packaging of the 0.2.0 wheel; the git install at the pinned commit has them. |
| 5 | minor | integration | SkillOpt's exec harness is unsuitable for our skills (section 2.4): no isolation, no usage, a renamed skill read on instruction. | Design of `codex_harness.py`; we run the agents ourselves. |
| 6 | minor | docs vs CLI | The skills and `commands.md` say every `*.py` under `app/tools/` declares a literal `API_CALLS` and that `lint` checks it, but `lint` accepts a tools module without `API_CALLS` (it "declares no calls"); only the runtime registry warns. A tool that calls an API without declaring it passes `lint` (the runtime policy still refuses what the policy does not allow). Found while authoring `code-temperature-tool`. | `src/graph_agents_cli/dev/policy_check.py` `read_api_calls` ("A tool without the name declares no calls"); the skills' wording in `graph-agents-cli-workflow/SKILL.md` Phase 2 step 5 and `langgraph-code/SKILL.md` section 2. |

## 10. Open decisions for the owner

1. **Workspace root outside the scratchpad** (`/private/tmp/gac-x-skillopt/...`): required for
   the Claude Code sandbox to protect the scratchpad (3.1). Workspaces are deleted after each
   rollout.
2. **Only the skill under test** in rollouts (the programme's rule), or the five frozen
   siblings for the `workflow` skill, which routes to them.
3. **Codex target model**: `gpt-5.6-terra` (recommended), `gpt-6-sol` (fallback metadata in
   Codex 0.154.0), or `gpt-5.6-luna` (cheap enough to optimise all six on Codex).
4. **Codex scope** within the shared $200: one Codex optimisation plus transfer checks under a
   $90 Track A cap, or a second optimisation if Track B spends less.
5. **Optimizer on Claude Code** (plan) for both harnesses, or OpenAI for the Codex runs.
6. **Fix issues 1 and 2 first** (separate `fix:` commits on this branch). Done in round 2:
   03d3e0f (proxies) and 2e14e97 (process listing), with 82f73d6 (`run --stop-server` false
   success) and ab96587 (`info` and the update-check opt-out) found by the baseline.
7. **Plan load**: about 1,200 Claude sessions for full size on all six skills; the alternative
   is tier 2 for more skills or fewer skills.

## 11. Next steps

1. Fix issues 1 and 2 with regression tests (`fix:` commits). Done: 03d3e0f, 2e14e97.
2. Build `gac_skillopt` from the spike: harness runners, workspace/slots, verifier, trace
   compaction, fact-check, budget, runner with preflight; unit tests for the verifier and the
   fact-check (fake traces, no model calls).
3. Author the eval skill's 30 tasks and hidden checks; dry-run every task's verifier against a
   hand-written reference solution (must score 1) and against the untouched fixture (must score
   below 1).
4. Smoke run on Claude Code with `num_epochs: 1`, `batch_size: 3`, 3 selection and 3 test
   tasks; check traces, gate decisions, usage records and cleanup.
5. Codex calibration: the baseline selection of the eval skill (7 rollouts, about $3) to replace
   the estimates of section 8.2, then decide the Codex scope.
6. Full runs in priority order (eval, scaffold, langgraph-code, deploy, workflow,
   observability), each followed by the transfer check and a human review of the diff.

## 12. As built (step 2)

`tools/skillopt/gac_skillopt/` implements sections 2-7 with these differences:

- **Layout.** One package instead of the planned `harness/` subpackage: `isolation.py` (the
  spike's settings), `workspace.py`, `harness.py`, `trace.py`, `verify.py`, `factcheck.py` (+
  `clitree.py`, run with the CLI build's interpreter), `budget.py`, `rollout.py`, `tasks.py`,
  `adapter.py`, `run.py`, and a command line (`python -m gac_skillopt`: `setup`, `preflight`,
  `validate`, `list`, `selfcheck`, `rollout`). `writeback.py` is not built yet; the fact-check
  does not yet run `ruff format` on Python blocks. `configs/` holds `claude.yaml`, `codex.yaml`
  and `_base.yaml` (SkillOpt's, MIT, copied with attribution).
- **Workspace.** The agent's working directory is the workspace, with the project in a
  subdirectory (as in the spike's project probe) and a private `.bench/` beside it (temp files,
  the cloned uv cache, the CLI's home, an empty kubeconfig, helm's directories, and, after the
  agent exits, the hidden verifier files). The CLI in the scratch bin is a wrapper that points
  `HOME` into the workspace (`scaffold enhance` backs up to `~/.graph-agents-cli`, which neither
  sandbox may write). `UV_PYTHON` names the CLI build's interpreter and downloads are off;
  `DOCKER_HOST` names a socket that does not exist.
- **Ports.** This track's range is 22050-22099: slot `i` uses `22050+i` (agent) and `22075+i`
  (verifier).
- **Prompt.** Every task prompt follows one fixed prefix (`tasks.PROMPT_PREFIX`): the CLI and
  its skills are installed, projects run on the fake provider, work only in the current
  directory, the user is not available for questions (decide safely and explain).
- **Isolation, per rollout.** Besides the preflight (`python -m gac_skillopt preflight`: one
  Claude session proving the skill list, a denied write outside the workspace, a blocked
  non-PyPI host, a clean PATH, and that neither the shell nor the Read, Grep and Glob tools can
  read a gold solution in the checkout), every Claude rollout's `init` event is checked: anything
  loaded beyond the skill under test stops the run. A one-off Codex probe (three sessions,
  $0.10) found the same: `cat`/`ls` of the checkout, reading another run's log and writing the
  scratch were denied ("Operation not permitted"), and no file content reached any output. Codex
  emitted no command events for the four denied commands (only its final message reports them),
  so a Codex transcript lists the commands that ran, not every attempt. The Claude settings also deny the Read,
  Edit and Write tools what the sandbox denies the shell (the checkout, other runs, credentials),
  and a scratch under a Claude Code session's temp root write-denies that project's whole temp
  directory. Only the skill directory and `.claude/settings.json` are integrity-hashed.
- **Benchmark size.** Round 1: 38 tasks (2-3 train, 1-2 val, 1-3 test per skill, at least one
  family held out for test). Round 2 (section 5.4's hold-out rule): 86 tasks, at least 6 train and 6 val
  per skill (scaffold 7 and 7), the 12 test tasks unchanged (workflow 13, scaffold 16,
  langgraph-code 14, eval 15, deploy 14, observability 14: 86), instead of 30 per skill. The 48
  new train and val tasks add variants of the failing families (unattended spec gate: 6;
  guidance-file choice: 7, two of them added after the first baseline pass;
  configuration-only observability changes: 9) and harder variants for langgraph-code (API_CALLS
  with the policy: new operations, new APIs, limits, approval gates, JSON bodies), eval
  (per-metric thresholds and rates, dataset fixes) and deploy (helm-push and argocd differences,
  per-environment values). The splits are frozen with content hashes.
- **Checks.** `cmd`, `file`, `json`/`yaml`/`dotenv` (a Python expression over the parsed file),
  `unchanged`, `pyfile` (hidden scripts run with the project's interpreter), `eval` and
  `transcript`; the planned `api_check`, `deploy_dry_run` and `manifest` types are expressed with
  `cmd` and `yaml`. Transcript patterns match at the start of each simple command, so text in an
  `echo` or a here-document is never a command.
- **Verifier proof.** Every task has a scripted gold solution and a scripted broken one (a
  plausible mistake); `selfcheck` requires gold `hard=1`/`soft=1.0`, broken `hard=0` failing
  exactly the mandatory checks the task lists in `broken_fails`, and the untouched fixture
  `hard=0`.
- **Codex.** Tasks where the agent must run `eval run`/`run` itself are tagged
  `needs_local_server` (11 of round 1's 38; 20 of the 86). Until issues 1 and 2 were fixed the adapter refused a Codex run over a
  split that held one; with the fixes (round 2) the refusal is gone and the tag is informative.
- **Training smoke run (round 2).** `run train` ran end to end on Claude Code for the workflow
  and scaffold skills (1 epoch, batch 3, fact-check on, optimizer `opus` through
  `bin/claude-isolated`; [`results/smoke-train-r2.md`](results/smoke-train-r2.md)). The optimizer
  wrapper was probed first (its `init` event: no tools, skills or MCP servers, only the built-in
  `agents-md` plugin; the model reported no instructions). SkillOpt's Claude Code chat backend
  cuts every optimizer call at 300 s and retries it up to five times, and its token tracker reads
  only `input_tokens` (cache reads and writes are dropped, so prompts show 2 tokens);
  `gac_skillopt/optlog.py` gives the calls a 1200 s default and logs every session to
  `optimizer_calls.jsonl`. Slow update, the meta skill and the test evaluation do not run in a
  1-epoch run.
- **Round 3a harness prep** ([`results/preflight-r3.md`](results/preflight-r3.md)):
  - **`env.val_reps`** (2 in `configs/claude.yaml`). Each selection item runs k times in parallel
    slots (`rep<k>/predictions/`, `val_reps.json`), and the gate sees the per-item mean. A
    repetition zeroed by the infrastructure is left out of the mean. Train and test items run
    once.
  - **A stale-CLI guard.** `ensure_bin` refuses, and `setup` rebuilds, a scratch CLI whose
    recorded commit is not HEAD or an ancestor, was dirty, or predates a change under `src/`,
    `pyproject.toml` or `hatch_build.py`. The install refreshes the package, because uv's
    git-commit cache key does not change inside a git worktree.
  - **Leftover processes** are recorded with their command line.
  - **Permission-gate denials** (`permission_denials` in the result event) are counted per
    rollout and totalled by the baseline report.
  - **More of the home directory is denied.** The sandboxed shell can read the home directory
    except the denied paths, and a rollout listed this CLI's `~/.graph-agents-cli/backups`
    (copies of projects' `.env` files). `HOME_SECRET_DIRS` now also denies `~/.codex`,
    `~/.graph-agents-cli`, `~/.gnupg`, `~/.config/gcloud` and `~/.azure`, and the preflight
    checks the first two.
  - **`GAC_SKILLOPT_DENY_READ`** adds deny-read paths. Set `GAC_SKILLOPT_OPENAI_KEY_FILE` (the
    path is enough) for Claude runs too: without it the key directory is not denied.
- **Smoke runs** (shipped skills, through `scripts/eval_only.py`): Claude Code passed
  `eval-tool-case-tokyo` (its own `eval run` in the sandbox, 34 s) and
  `scaffold-create-prototype-gemini` (15 s); Codex `gpt-5.6-terra` passed `deploy-placeholder-url`
  and `deploy-secret-key` for $0.19 together (about 120-155k input tokens, 80 % cached, per
  rollout). Every session listed only the skill under test and loaded it; nothing was left
  running.

## Appendix: rerunning the spike

All scripts take `--scratch <dir>` (evidence JSON is written there); Codex scripts need
`GAC_SKILLOPT_OPENAI_KEY_FILE` pointing to a file with one `OPENAI_API_KEY=` line, and every
Codex run's spend goes into the programme ledger.

```bash
S=<scratch>/iso
python tools/skillopt/spike/claude_isolation_probe.py --scratch $S --variant isolated   # and: default, config_dir
python tools/skillopt/spike/codex_isolation_probe.py  --scratch $S --variant scratch_home  # and: real_home
python tools/skillopt/spike/trivial_rollout.py --harness claude --scratch $S
python tools/skillopt/spike/trivial_rollout.py --harness codex  --scratch $S --model gpt-5.6-terra
python tools/skillopt/spike/sandbox_probe.py --harness claude --scratch $S --sandbox project \
  --work-root /private/tmp/gac-x-skillopt-probe --outside $S/sibling.txt --deny-write <scratchpad> --no-allow-work
python tools/skillopt/spike/sandbox_probe.py --harness codex --scratch $S --network proxy \
  --work-root /private/tmp/gac-x-skillopt-probe --outside $S/sibling.txt
python tools/skillopt/spike/project_task_probe.py --harness claude --scratch $S --port 22002 --scratchpad <scratchpad>
```

`spike/harness_env.py` holds the isolation settings the probes verified; the adapter's harness
modules start from it.
