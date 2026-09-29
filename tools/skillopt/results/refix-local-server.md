# The local-server tasks after the CLI fixes (round 2)

Date: 2026-09-27. Branch `experiments/skillopt` at `d70fc47`, CLI build `0.2.0+gab96587`, which
has the four `fix:` commits of round 2:

| Commit | Fix |
|---|---|
| `03d3e0f` | Requests to the local server never go through the environment's proxies; SOCKS proxies work remotely (`httpx[socks]`); an unusable proxy is a one-line error, exit 3 |
| `2e14e97` | The local server is stopped when the sandbox denies process listing (process group, `Popen` handle); a teardown error never replaces the original one |
| `82f73d6` | `run --stop-server` exits 2, naming what is still running, when the OS refuses the signal, and keeps the record |
| `ab96587` | `info` skips `npx skills list` under `GRAPH_AGENTS_CLI_NO_UPDATE_CHECK=1` and in CI, and says so |

With them, the harness no longer refuses Codex runs over tasks tagged `needs_local_server`
(`d70fc47`). This page reruns the six val and test tasks with that tag and compares them with
the round-1 baseline ([`baseline.md`](baseline.md)), where every Codex rollout of these tasks hit
the proxy and process-listing failures and passed only through workarounds.

## How it was run

The same rollout function, models, effort and isolation as the baseline. The six `SKILL.md`
bodies are the ones round 1 measured (same hashes). The workflow skill's `references/commands.md`
gained the fixes' documentation.

| | Claude Code | Codex |
|---|---|---|
| Harness, model | Claude Code 2.1.283, `sonnet`, effort medium, permission mode `acceptEdits` (round 1's) | Codex 0.154.0, `gpt-5.6-terra`, effort medium |
| Tasks | `eval-tool-case-tokyo`, `eval-fix-missing-threshold`, `wf-end-to-end-tool` (val); `eval-multiturn-order`, `eval-honest-report`, `code-remove-checkpointer` (test) | same |
| Repetitions | 1 (6 rollouts), ports 22150-22155 / 22175-22180 | 3 (18 rollouts), ports 22160-22165 / 22185-22190 |
| Wall time per repetition | 102 s | 93 s, 56 s, 54 s |

```bash
python -m gac_skillopt setup      --scratch $S --port-base 22150 --rebuild-cli   # 0.2.0+gab96587
python -m gac_skillopt preflight  --scratch $S --port-base 22150                 # isolation: ok
python -m gac_skillopt.baseline run --scratch $S --harness claude --reps 1 --slots 6 \
    --port-base 22150 --task ... (the six) --out $S/runs/refix
GAC_SKILLOPT_OPENAI_KEY_FILE=... GAC_SKILLOPT_LEDGER=.../spend.py \
python -m gac_skillopt.baseline run --scratch $S --harness codex --model gpt-5.6-terra --reps 3 \
    --slots 6 --port-base 22160 --max-usd 6 --task ... (the six) --out $S/runs/refix
```

The owner's decision to move Claude rollouts to `bypassPermissions` (with the Bash sandbox kept)
is not applied here: it needs its sandbox preflight first, and this comparison needs round 1's
mode. `baseline run` now records the mode in its meta.

## Results

"Failure signature": the rollout's commands or output mention `socksio`, `PermissionError`,
"already listening", `registry.npmjs.org`, or unset the proxy variables (`env -u ALL_PROXY`) to
get around them. Round-1 figures are the three baseline repetitions of the same tasks.

### Codex (3 repetitions each)

| Task | hard / soft, round 1 → 2 | Cost per rollout | Agent time, mean (median) | Commands | Leftover processes | Rollouts with a failure signature |
|---|---|---|---|---|---|---|
| eval-tool-case-tokyo | 1.00 / 0.94 → 1.00 / 1.00 | $0.215 → $0.106 | 50 (57) → 40 (32) s | 17.7 → 6.3 | 5 → 0 | 3 → 0 |
| eval-fix-missing-threshold | 1.00 / 1.00 → 1.00 / 1.00 | $0.168 → $0.070 | 46 (48) → 35 (26) s | 13.7 → 4.3 | 4 → 0 | 3 → 0 |
| wf-end-to-end-tool | 1.00 / 1.00 → 1.00 / 1.00 | $0.289 → $0.176 | 59 (49) → 48 (41) s | 17.3 → 8.3 | 3 → 0 | 3 → 0 |
| eval-multiturn-order | 1.00 / 1.00 → 1.00 / 1.00 | $0.204 → $0.099 | 48 (52) → 40 (36) s | 15.0 → 6.0 | 4 → 0 | 3 → 0 |
| eval-honest-report | 1.00 / 1.00 → 1.00 / 1.00 | $0.179 → $0.060 | 111 (47) → 27 (22) s | 12.0 → 3.7 | 7 → 0 | 3 → 0 |
| code-remove-checkpointer | 1.00 / 1.00 → 1.00 / 1.00 | $0.228 → $0.102 | 46 (48) → 41 (39) s | 14.7 → 5.7 | 6 → 0 | 3 → 1 |
| **All six** | **1.00 / 0.99 → 1.00 / 1.00** | **$0.214 → $0.102** | **60 (48) → 38 (36) s** | **15.1 → 5.7** | **29 → 0** | **18 → 1** |

- **Cost per rollout halved** ($0.214 to $0.102) and is now at round 1's average for clean
  rollouts ($0.097). The round-1 figure for all 24 rollouts that hit the failures, across ten
  tasks, was $0.253 and 73 s.
- **Commands per rollout fell from 15 to 6**: no more retries, proxy workarounds or
  `--url` detours.
- **No leftover processes** (29 in round 1 on these tasks) and no "already listening".
- Failure signatures in round 1 (18 rollouts): `socksio` 18, `PermissionError` 18, proxy
  variables unset 18, "already listening" 7, npm registry 1. In round 2: `socksio` 1, and it is
  not the CLI. In `code-remove-checkpointer` repetition 1, the task's broken fixture builds a
  `ChatOpenAI` model directly, and under Codex's SOCKS `ALL_PROXY` the generated project's own
  model client failed at server startup (KI-132). The CLI reported it as "Local server process
  exited during startup (exit code 3)" with the log tail; the agent fixed the code the task was
  about, and `eval run` passed.
- Spend: $1.8405 computed, $1.851 in the ledger (per-rollout entries rounded up) for 2.94 M input
  tokens (2.46 M cached) and 32.8 k output tokens. Prices: `budget.PRICES`, read on 2026-09-27
  (gpt-5.6-terra $2.00 / $0.20 / $12.00 per 1M input / cached input / output tokens).

### Claude Code (1 repetition)

| Task | hard / soft, round 1 → 2 | API-equivalent cost (plan) | Agent time, mean (median) | Commands | Rollouts with a failure signature |
|---|---|---|---|---|---|
| eval-tool-case-tokyo | 1.00 / 1.00 → 1.00 / 1.00 | $0.124 → $0.185 | 40 (41) → 58 s | 3.0 → 4.0 | 0 → 0 |
| eval-fix-missing-threshold | 1.00 / 1.00 → 1.00 / 1.00 | $0.103 → $0.135 | 22 (22) → 49 s | 1.0 → 2.0 | 0 → 0 |
| wf-end-to-end-tool | 1.00 / 1.00 → 1.00 / 1.00 | $0.305 → $0.312 | 172 (217) → 69 s | 9.7 → 6.0 | 2 → 0 |
| eval-multiturn-order | 1.00 / 1.00 → 1.00 / 1.00 | $0.145 → $0.243 | 533 (32) → 59 s | 2.3 → 3.0 | 0 → 0 |
| eval-honest-report | 1.00 / 1.00 → 1.00 / 1.00 | $0.127 → $0.158 | 34 (32) → 54 s | 4.0 → 3.0 | 0 → 0 |
| code-remove-checkpointer | 1.00 / 1.00 → 1.00 / 1.00 | $0.191 → $0.255 | 56 (50) → 65 s | 5.7 → 7.0 | 0 → 0 |
| **All six** | **1.00 / 1.00 → 1.00 / 1.00** | **$0.166 → $0.215** | **143 (41) → 59 s** | **4.3 → 4.2** | **2 → 0** |

Claude Code's sandbox sets no SOCKS proxy and lets commands list processes, so fixes 1 and 2 do
not apply to it; one repetition cannot separate the cost and time differences from run-to-run
variation (the round-1 mean of `eval-multiturn-order` includes the 25 minutes the laptop slept).
What did change: in round 1, two `wf-end-to-end-tool` rollouts had `info` run `npx` into the
sandbox's network block; none did in round 2. That task also took less time (median 217 s to
69 s), but one repetition does not show how much of that is fix 4.

## Checks

- **The CLI build, end to end, before and after** (no model calls; `e2e.sh` in the run's
  scratch, a created project with the fake provider): under Codex's proxy variables
  (`ALL_PROXY=socks5h://...`, `HTTP(S)_PROXY=http://...`) and `sandbox-exec` denying
  `kern.proc.all`, `eval run` exits 0 on `0.2.0+gab96587`. The round-1 build `0.2.0+g9c93fd2`
  exits 2 with `PermissionError` (which hides the SOCKS `ImportError`); a one-off `run "hi"`
  under the proxies crashes there with the `ImportError` traceback. With signals to other
  processes denied, `run --stop-server` exits 2 and names both PIDs on the new build (the
  record is kept, and a later unsandboxed `run --stop-server` stops the server); the old build
  printed "Local server stopped.", dropped the record and left the server on its port. With
  `GRAPH_AGENTS_CLI_NO_UPDATE_CHECK=1` and an `npx` shim on `PATH`, `info` makes 0 calls ("not
  listed") on the new build and 1 on the old one.
- **Isolation**: the preflight passed (skill list, denied outside write, blocked `example.com`,
  PyPI allowed, checkout unreadable by the shell and by Read/Grep/Glob). Every rollout loaded the
  skill under test (18/18 Codex, 6/6 Claude).
- **Afterwards**: nothing listens on 22150-22199, `/private/tmp/gac-x-skillopt` is empty, no
  `auth.json` is left in the scratch `CODEX_HOME`s, and the key-leak scan over the scratch and
  this checkout finds 0 files.
- **Sessions**: 7 Claude Code sessions on the plan (1 preflight, 6 rollouts).
