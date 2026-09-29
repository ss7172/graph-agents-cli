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

"""Round-3 harness changes on fake inputs: repeated selection rollouts (``env.val_reps``), the
stale-CLI guard, the permission mode and its denials, leftover notes and the preflight judge."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from gac_skillopt import isolation, preflight, rollout, trace, workspace
from gac_skillopt.paths import Bench
from gac_skillopt.tasks import Task

# ── env.val_reps ──


def _task(tid: str, tmp_path: Path) -> Task:
    return Task(
        id=tid,
        skill="graph-agents-cli-workflow",
        family="f",
        task_type="t",
        prompt="p",
        fixture={"kind": "empty", "name": "x"},
        checks=[],
        reference="r",
        dir=tmp_path,
    )


def _result(tid: str, hard: int, soft: float, reason: str = "", infra: str = "") -> dict:
    return {
        "id": tid,
        "hard": hard,
        "soft": soft,
        "fail_reason": reason,
        "infra_error": infra,
        "task_type": "t",
    }


def test_only_selection_eval_batches_are_repeated() -> None:
    items = [{"id": "a"}, {"id": "b"}]
    for split in ("valid_seen", "val", "selection"):
        tagged = rollout.tag_selection_items(items, phase="eval", split=split, reps=2)
        assert rollout.item_reps(tagged) == 2
    assert items == [{"id": "a"}, {"id": "b"}]  # the loader's items are not changed
    for phase, split in (("train", "train"), ("eval", "valid_unseen"), ("eval", "test")):
        once = rollout.tag_selection_items(items, phase=phase, split=split, reps=2)
        assert rollout.item_reps(once) == 1
    assert (
        rollout.item_reps(rollout.tag_selection_items(items, phase="eval", split="val", reps=1))
        == 1
    )
    assert rollout.item_reps([]) == 1


def test_mean_over_reps_averages_each_item() -> None:
    rep1 = [_result("a", 1, 1.0), _result("b", 0, 0.5, "asks failed")]
    rep2 = [_result("a", 0, 0.75, "no-create failed"), _result("b", 0, 0.25, "asks failed")]
    merged = rollout.mean_over_reps([rep1, rep2])
    assert [m["id"] for m in merged] == ["a", "b"]
    assert merged[0]["hard"] == 0.5 and merged[0]["soft"] == 0.875
    assert merged[1]["hard"] == 0 and merged[1]["soft"] == 0.375
    assert merged[0]["fail_reason"] == "rep2: no-create failed"
    assert merged[1]["fail_reason"] == "rep1: asks failed | rep2: asks failed"
    assert [r["hard"] for r in merged[0]["reps"]] == [1, 0]
    assert merged[0]["reps_scored"] == 2


def test_mean_over_reps_leaves_out_infrastructure_zeros() -> None:
    ok = _result("a", 1, 1.0)
    limited = _result("a", 0, 0.0, "infrastructure: rate limit", infra="rate limit")
    merged = rollout.mean_over_reps([[ok], [limited]])
    assert merged[0]["hard"] == 1.0 and merged[0]["soft"] == 1.0
    assert merged[0]["reps_scored"] == 1 and merged[0]["reps"][1]["infra"] is True
    both = rollout.mean_over_reps([[limited], [dict(limited)]])
    assert both[0]["hard"] == 0 and both[0]["soft"] == 0.0 and both[0]["reps_scored"] == 0
    assert both[0]["fail_reason"].startswith("rep1: infrastructure")


def test_rollout_repeated_runs_k_times_and_returns_the_mean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fake traces: the rollout function scores task `a` 1 then 0 and task `b` 0.5 every time;
    every repetition lands in its own predictions directory and workspace tag."""
    seen: list[tuple[str, str, str]] = []
    scores = {"a": iter([(1, 1.0), (0, 0.5)]), "b": iter([(0, 0.5), (0, 0.5)])}

    def fake_rollout_one(bench, task, body, cfg, slot, out_dir, tag=""):
        seen.append((task.id, out_dir.name, tag))
        hard, soft = next(scores[task.id])
        return _result(task.id, hard, soft, "" if hard else "check failed")

    monkeypatch.setattr(rollout, "rollout_one", fake_rollout_one)
    cfg = rollout.RolloutConfig(harness="gold", slots=1)
    tasks = [_task("a", tmp_path), _task("b", tmp_path)]
    merged = rollout.rollout_repeated(None, tasks, None, cfg, tmp_path / "sel", reps=2)  # type: ignore[arg-type]
    assert sorted(seen) == [
        ("a", "rep1", "rep1"),
        ("a", "rep2", "rep2"),
        ("b", "rep1", "rep1"),
        ("b", "rep2", "rep2"),
    ]
    assert [(m["id"], m["hard"], m["soft"]) for m in merged] == [("a", 0.5, 0.75), ("b", 0, 0.5)]
    saved = json.loads((tmp_path / "sel" / "val_reps.json").read_text())
    assert saved["reps"] == 2 and [i["hard"] for i in saved["items"]] == [0.5, 0]
    # One repetition is the plain batch: no rep directories, no val_reps.json.
    seen.clear()
    scores.update({"a": iter([(1, 1.0)]), "b": iter([(1, 1.0)])})
    once = rollout.rollout_repeated(None, tasks, None, cfg, tmp_path / "once", reps=1)  # type: ignore[arg-type]
    assert [m["hard"] for m in once] == [1, 1] and {s[2] for s in seen} == {""}
    assert not (tmp_path / "once" / "val_reps.json").exists()


# ── stale CLI ──


@pytest.mark.parametrize(
    ("kw", "want"),
    [
        ({"commit": "a" * 40, "is_ancestor": True, "sources_unchanged": True}, ""),
        ({"commit": None, "is_ancestor": True, "sources_unchanged": True}, "no source commit"),
        ({"commit": "a" * 40, "dirty": True}, "uncommitted"),
        ({"commit": "a" * 40, "is_ancestor": False, "sources_unchanged": True}, "not HEAD"),
        ({"commit": "a" * 40, "is_ancestor": True, "sources_unchanged": False}, "changed since"),
        ({"commit": "a" * 40, "is_ancestor": None, "sources_unchanged": True}, "could not"),
        ({"commit": "a" * 40, "head": None}, "HEAD is unknown"),
    ],
)
def test_stale_reason(kw: dict[str, Any], want: str) -> None:
    args = {
        "commit": "a" * 40,
        "dirty": False,
        "head": "b" * 40,
        "is_ancestor": True,
        "sources_unchanged": True,
    }
    args.update(kw)
    commit, dirty, head = args.pop("commit"), args.pop("dirty"), args.pop("head")
    reason = isolation.stale_reason(commit, dirty, head, **args)
    assert (reason == "") if not want else (want in reason)


def test_ensure_bin_refuses_a_stale_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bench = Bench(tmp_path / "scratch")
    bench.cli_real.parent.mkdir(parents=True)
    bench.cli_real.write_text("#!/bin/sh\n")
    monkeypatch.setattr(isolation, "cli_staleness", lambda b: "the CLI sources changed")
    monkeypatch.delenv("GAC_SKILLOPT_ALLOW_STALE_CLI", raising=False)
    installs: list[int] = []
    monkeypatch.setattr(isolation, "_install_cli", lambda b: installs.append(1))
    with pytest.raises(SystemExit, match="stale: the CLI sources changed"):
        isolation.ensure_bin(bench)
    assert installs == []
    # setup rebuilds once, then refuses when the rebuilt CLI is still stale.
    with pytest.raises(SystemExit, match="stale"):
        isolation.ensure_bin(bench, on_stale="rebuild")
    assert installs == [1]


# ── permission mode, denials, settings ──


def test_claude_cmd_permission_mode() -> None:
    cmd = isolation.claude_cmd("p", model="sonnet", effort=None, max_turns=None)
    assert cmd[cmd.index("--permission-mode") + 1] == isolation.CLAUDE_PERMISSION_MODE
    cmd = isolation.claude_cmd(
        "p", model="sonnet", effort=None, max_turns=None, permission_mode="bypassPermissions"
    )
    assert cmd[cmd.index("--permission-mode") + 1] == "bypassPermissions"
    assert cmd[cmd.index("--permission-prompts") + 1] == "none"
    with pytest.raises(ValueError):
        isolation.claude_cmd("p", model="s", effort=None, max_turns=None, permission_mode="auto")


def test_settings_deny_the_workspace_sandbox_settings(tmp_path: Path) -> None:
    bench = Bench(tmp_path / "scratch")
    work = Path("/private/tmp/gac-x-skillopt/run/00-task")
    deny = isolation.claude_settings(bench, work)["permissions"]["deny"]
    assert f"Edit(/{work}/.claude/**)" in deny and f"Write(/{work}/.claude/**)" in deny
    assert f"Read(/{bench.runs}/**)" in deny
    settings = isolation.claude_settings(bench, work)["sandbox"]
    assert settings["allowUnsandboxedCommands"] is False and settings["enabled"] is True


def test_key_directory_and_extra_paths_are_denied(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GAC_SKILLOPT_OPENAI_KEY_FILE", "/tmp/keys-x/provider.env")
    monkeypatch.setenv("GAC_SKILLOPT_DENY_READ", "/tmp/secret-a:/tmp/secret-b")
    dirs = [str(d) for d in isolation.secret_dirs()]
    assert str(Path("/tmp/keys-x").resolve()) == dirs[0]
    assert str(Path("/tmp/secret-a").resolve()) in dirs
    assert str(Path("/tmp/secret-b").resolve()) in dirs


DENIAL_STREAM = "\n".join(
    json.dumps(e)
    for e in [
        {
            "type": "system",
            "subtype": "init",
            "skills": ["graph-agents-cli-workflow"],
            "permissionMode": "acceptEdits",
        },
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "Bash",
                        "input": {
                            "command": "MODEL_PROVIDER=fake graph-agents-cli create x",
                            "dangerouslyDisableSandbox": True,
                        },
                    }
                ]
            },
        },
        {
            "type": "system",
            "subtype": "permission_denied",
            "tool_name": "Bash",
            "tool_use_id": "t1",
            "decision_reason": "no approval surface in this session",
            "message": "Permission for this tool use was denied.",
        },
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t1",
                        "content": "Permission for this tool use was denied.",
                        "is_error": True,
                    }
                ]
            },
        },
        {
            "type": "result",
            "subtype": "success",
            "result": "Stopped.",
            "num_turns": 2,
            "permission_denials": [
                {
                    "tool_name": "Bash",
                    "tool_use_id": "t1",
                    "tool_input": {"command": "MODEL_PROVIDER=fake graph-agents-cli create x"},
                }
            ],
        },
    ]
)


def test_parse_claude_records_mode_and_denials() -> None:
    t = trace.parse_claude(DENIAL_STREAM, skill="graph-agents-cli-workflow")
    assert t.init["permissionMode"] == "acceptEdits"
    assert t.permission_denials == [
        {"tool": "Bash", "input": "MODEL_PROVIDER=fake graph-agents-cli create x"}
    ]
    uses = trace.claude_tool_uses(DENIAL_STREAM)
    assert len(uses) == 1 and uses[0]["input"]["dangerouslyDisableSandbox"] is True
    assert uses[0]["is_error"] and uses[0]["denied_by"].startswith("no approval surface")
    assert uses[0]["result"] == "Permission for this tool use was denied."


def test_leftover_note_names_the_command_line() -> None:
    ws = Path("/private/tmp/gac-x-skillopt/run/03-task")
    note = workspace.leftover_note(
        4242, f"{ws}/proj/.venv/bin/python -m uvicorn app.server:app --port 22403\n", ws
    )
    assert note == (
        "stopped leftover pid 4242: W/proj/.venv/bin/python -m uvicorn app.server:app --port 22403"
    )
    assert workspace.leftover_note(7, "", ws) == "stopped leftover pid 7: (exited)"
    assert len(workspace.leftover_note(1, "x" * 1000)) < 400


# ── preflight judge ──


def _use(name: str, result: str, **inp: Any) -> dict[str, Any]:
    return {"name": name, "input": inp, "result": result, "is_error": False}


def _probe(tmp_path: Path) -> preflight.Probe:
    return preflight.Probe(
        pid="p1",
        home=tmp_path / "home",
        denied_dir=tmp_path / "denied",
        decoy_dir=tmp_path / "ws" / "other" / "00-decoy",
        runs_marker=tmp_path / "runs" / "m" / "gac-runs-marker-p1.txt",
        gold=tmp_path / "checkout" / "tasks" / "eval" / "lisbon" / "gold.sh",
        key_file=tmp_path / "keys" / "provider.env",
    )


def _all_steps(p: preflight.Probe, *, write_ok: bool, net_ok: bool) -> list[dict[str, Any]]:
    """One tool call per preflight step; `write_ok`/`net_ok` make the file tools and the
    network succeed where they must not."""
    denied = "Operation not permitted"
    tool_res = "File created successfully" if write_ok else "denied by your permission settings"
    gold_dir, runs, kdir = (
        str(p.gold.parent.parent),
        str(p.runs_marker.parent.parent),
        str(p.key_file.parent),
    )
    ex = "200" if net_ok else "000\ndeny network-outbound example.com:443"
    return [
        _use("Bash", denied, command=f"touch {p.path('sh_denied')}"),
        _use("Bash", denied, command=f"touch {p.path('sh_home')}"),
        _use("Bash", denied, command=f"echo tampered >> {p.path('decoy_file')}"),
        _use("Bash", ex, command="curl -w %{http_code} https://example.com/"),
        _use("Bash", "200", command="curl -w %{http_code} https://pypi.org/simple/"),
        _use("Bash", "", command="echo inside > inside.txt"),
        _use("Bash", "/s/bin/graph-agents-cli", command="command -v graph-agents-cli uv"),
        _use("Bash", denied, command=f"cat {p.gold}"),
        _use("Bash", denied, command=f"cat {p.runs_marker}"),
        _use("Bash", "DECOY-p1 original", command=f"cat {p.path('decoy_file')}"),
        _use("Bash", f"ls: {kdir}: {denied}", command=f"ls -la {kdir}"),
        _use("Bash", f"wc: {p.key_file}: open: {denied}", command=f"wc -c {p.key_file}"),
        _use("Bash", denied, command=f"touch {p.path('dds_home')}", dangerouslyDisableSandbox=True),
        _use("Bash", ex, command="curl https://example.com/", dangerouslyDisableSandbox=True),
        _use("Write", tool_res, file_path=str(p.path("tool_home"))),
        _use("Write", "denied by your permission settings", file_path=str(p.path("tool_denied"))),
        _use("Write", tool_res, file_path=str(p.path("decoy_new"))),
        _use("Edit", tool_res, file_path=str(p.path("decoy_file"))),
        _use("Write", "File created successfully", file_path="inside-tool.txt"),
        _use("Read", "denied by your permission settings", file_path=str(p.gold)),
        _use("Grep", f"Permission to read {gold_dir} has been denied.", path=gold_dir),
        _use("Glob", f"Permission to read {gold_dir} has been denied.", path=gold_dir),
        _use("Read", "denied by your permission settings", file_path=str(p.runs_marker)),
        _use("Glob", f"Permission to read {runs} has been denied.", path=runs),
        _use("Read", "denied by your permission settings", file_path=f"{kdir}/absent.txt"),
        _use("Glob", f"Permission to read {kdir} has been denied.", path=kdir),
        _use("Grep", f"Permission to read {kdir} has been denied.", path=kdir),
        _use("Read", "requires approval", file_path=str(p.path("decoy_file"))),
        _use("Edit", "denied by your permission settings", file_path=".claude/settings.json"),
        _use("Bash", denied, command="echo '#' >> .claude/settings.json"),
        _use(
            "Bash", denied, command=f"touch {p.path('after_home')}", dangerouslyDisableSandbox=True
        ),
    ]


def _exists(**over: bool) -> dict[str, bool]:
    names = ("sh_denied", "sh_home", "dds_home", "tool_home", "tool_denied", "after_home")
    state = {n: False for n in (*names, "decoy_new")}
    state.update({"inside": True, "inside_tool": True})
    state.update(over)
    return state


def test_preflight_judge_passes_a_confined_session(tmp_path: Path) -> None:
    p = _probe(tmp_path)
    checks = preflight.judge(
        p,
        _all_steps(p, write_ok=False, net_ok=False),
        exists=_exists(),
        decoy_after=p.decoy_text,
        settings_unchanged=True,
    )
    gating = {k: c["ok"] for k, c in checks.items() if c["gating"]}
    assert all(gating.values()), gating
    assert checks["unsandboxed_retry_refused"]["evidence"]["flag_sent"] == [True, True]
    # The decoy of another run was readable by the shell: reported, not gating.
    assert checks["other_run_workspace_unreadable"]["ok"] is False
    assert checks["other_run_workspace_unreadable"]["gating"] is False


def test_preflight_judge_fails_file_tool_writes_and_open_network(tmp_path: Path) -> None:
    """What bypassPermissions did on Claude Code 2.1.283: the Write and Edit tools wrote
    outside the workspace and example.com answered 200; shell writes stayed denied."""
    p = _probe(tmp_path)
    checks = preflight.judge(
        p,
        _all_steps(p, write_ok=True, net_ok=True),
        exists=_exists(tool_home=True, decoy_new=True),
        decoy_after=f"EDITED-{p.pid} original\n",
        settings_unchanged=True,
    )
    assert checks["shell_write_outside_denied"]["ok"] is True
    assert checks["tool_write_outside_denied"]["ok"] is False
    assert checks["network_pypi_only"]["ok"] is False
    assert checks["unsandboxed_retry_refused"]["ok"] is False
    assert checks["settings_protected"]["ok"] is True


def test_preflight_judge_fails_steps_not_attempted(tmp_path: Path) -> None:
    """A session that refuses the steps proves nothing: every check fails as not attempted."""
    p = _probe(tmp_path)
    checks = preflight.judge(
        p,
        [],
        exists=_exists(inside=False, inside_tool=False),
        decoy_after=p.decoy_text,
        settings_unchanged=True,
    )
    assert not any(c["ok"] for c in checks.values())
    assert not checks["key_dir_unreadable"]["attempted"]


def test_preflight_prompt_skips_the_prefix_and_names_every_target(tmp_path: Path) -> None:
    p = _probe(tmp_path)
    text = preflight.prompt(p)
    for role in ("sh_denied", "sh_home", "dds_home", "tool_home", "tool_denied", "after_home"):
        assert str(p.path(role)) in text
    assert str(p.key_file) in text and "dangerouslyDisableSandbox" in text
    task = preflight.PreflightTask(
        id="x",
        skill="s",
        family="f",
        task_type="t",
        prompt=text,
        fixture={"kind": "empty", "name": "x"},
        checks=[],
        reference="-",
        dir=tmp_path,
    )
    assert task.full_prompt == text


def test_home_secret_dirs_are_denied_and_probed(tmp_path: Path) -> None:
    """The Codex login and the CLI's own backups (copies of projects' .env files) are denied to
    every rollout; the preflight fails when the shell can list them."""
    home = Path.home()
    dirs = {str(d) for d in isolation.secret_dirs()}
    assert str(home / ".codex") in dirs and str(home / ".graph-agents-cli") in dirs
    p = _probe(tmp_path)
    p.home_secret_dirs = (tmp_path / "home" / ".codex", tmp_path / "home" / ".graph-agents-cli")
    assert all(f"`ls -la {d}`" in preflight.prompt(p) for d in p.home_secret_dirs)
    base = _all_steps(p, write_ok=False, net_ok=False)
    denied = [
        _use("Bash", f"ls: {d}: Operation not permitted", command=f"ls -la {d}")
        for d in p.home_secret_dirs
    ]
    listed = [
        _use("Bash", "total 8\ndrwxr-xr-x  3 u  staff  96 backups", command=f"ls -la {d}")
        for d in p.home_secret_dirs
    ]
    kw = {"exists": _exists(), "decoy_after": p.decoy_text, "settings_unchanged": True}
    assert preflight.judge(p, base + denied, **kw)["home_secrets_unreadable"]["ok"] is True
    assert preflight.judge(p, base + listed, **kw)["home_secrets_unreadable"]["ok"] is False
    assert preflight.judge(p, base, **kw)["home_secrets_unreadable"]["attempted"] is False


def test_baseline_run_names_differ_within_one_second() -> None:
    # Two baseline runs started together (two candidate bodies) shared one workspace directory
    # per task and deleted each other's workspaces (round 3b).
    from gac_skillopt.baseline import baseline_run_name

    names = {baseline_run_name("claude", 1) for _ in range(20)}
    assert len(names) == 20
    assert all(n.startswith("base-claude-r1-") for n in names)
    # The same holds for the Codex login of each invocation (deleted when the invocation ends).
    from gac_skillopt.baseline import codex_home_name

    assert len({codex_home_name() for _ in range(20)}) == 20


# ── Codex spend: the track cap and an exhausted account (round 3b) ──


def test_ledger_check_names_the_track(tmp_path: Path) -> None:
    """The in-run ledger check passes the run's track, so the track's cap is enforced as well
    as the programme's stop-at (it used to check only the stop-at)."""
    from gac_skillopt import budget

    script = tmp_path / "spend.py"
    script.write_text(
        "import sys\nprint(' '.join(sys.argv[1:]))\n"
        "sys.exit(0 if sys.argv[-2:] == ['--track', 'skillopt'] else 1)\n"
    )
    ok, message = budget.Ledger(script).check(0.6)
    assert ok and message == "check --need 0.6000 --track skillopt"
    ok, message = budget.Ledger(script, track="").check(0.6)
    assert not ok and "--track" not in message


def _codex_run(error: str = "", stderr: str = "", exit_code: int = 1) -> Any:
    from gac_skillopt import harness

    return harness.AgentRun(
        harness="codex",
        exit_code=exit_code,
        timed_out=False,
        wall_s=1.0,
        trace=trace.Trace(harness="codex", error=error),
        stderr_tail=stderr,
    )


def test_quota_error_is_told_apart_from_a_transient_infra_error() -> None:
    no_credit = (
        "stream disconnected before completion: You have no credits remaining. Add credits to "
        "continue using the API at https://platform.openai.com/settings/organization"
    )
    run = _codex_run(error=no_credit)
    assert run.quota_error and run.infra_error  # an infra error, and a fatal one
    assert _codex_run(stderr="Error: 429 insufficient_quota").quota_error
    rate = _codex_run(error="stream disconnected before completion: rate limit reached")
    assert rate.infra_error and not rate.quota_error
    ok = _codex_run(stderr="the docs mention insufficient_quota", exit_code=0)
    assert not ok.quota_error


def test_an_exhausted_account_stops_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 429 insufficient_quota is not retried or scored: the rollout raises BudgetStop, which
    stops the batch (and a training run) instead of zeroing every later rollout at $0."""
    from gac_skillopt import budget

    ws = workspace.Workspace(task=_task("a", tmp_path), root=tmp_path / "ws", slot=0)
    (tmp_path / "ws").mkdir()
    calls: list[str] = []
    monkeypatch.setattr(workspace, "build", lambda *a, **k: ws)
    monkeypatch.setattr(workspace, "install_skill", lambda *a, **k: tmp_path / "SKILL.md")
    monkeypatch.setattr(workspace, "cleanup_processes", lambda *a, **k: [])
    monkeypatch.setattr(workspace, "remove", lambda w: calls.append("removed"))

    def fake_agent(bench, w, cfg):
        calls.append("agent")
        return _codex_run(error="stream disconnected: You have no credits remaining.")

    monkeypatch.setattr(rollout, "_run_agent", fake_agent)
    run_budget = budget.RunBudget(model="gpt-5.6-terra", max_usd=5.0, ledger=budget.Ledger(None))
    cfg = rollout.RolloutConfig(
        harness="codex", model="gpt-5.6-terra", codex_home=tmp_path / "ch", budget=run_budget
    )
    with pytest.raises(budget.BudgetStop, match="no quota left"):
        rollout.rollout_one(None, ws.task, None, cfg, 0, tmp_path / "out")  # type: ignore[arg-type]
    assert calls == ["agent", "removed"]  # no retry; the workspace is still removed
    saved = json.loads((tmp_path / "out" / "predictions" / "a" / "result.json").read_text())
    assert saved["fail_reason"].startswith("infrastructure: quota:")
    assert run_budget.inflight == 0 and run_budget.rollouts == 1


def test_codex_can_exec_its_own_install_inside_a_denied_codex_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Denying all of ~/.codex (eadd7ee) also denied the standalone Codex binary under
    ~/.codex/packages, so `codex exec` could not start its sandboxed helper ("sandbox-exec:
    execvp() of .../codex failed: Operation not permitted") and every Codex rollout failed
    before the model was called. The install directory is readable again; the login is not."""
    codex_dir = tmp_path / "home" / ".codex"
    binary = codex_dir / "packages" / "standalone" / "releases" / "0.154.0" / "bin" / "codex"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    (codex_dir / "auth.json").write_text("{}")
    monkeypatch.setenv("GAC_SKILLOPT_DENY_READ", str(codex_dir))
    monkeypatch.delenv("GAC_SKILLOPT_OPENAI_KEY_FILE", raising=False)
    monkeypatch.setattr(isolation, "CODEX_BIN", str(binary))
    bench = Bench(tmp_path / "scratch")
    text = isolation.codex_config_toml(bench, tmp_path / "ch", model="m", effort="medium")
    assert f'"{codex_dir.resolve()}" = "deny"' in text
    assert text.rstrip().endswith(f'"{(codex_dir / "packages").resolve()}" = "read"')
    # A binary outside every denied directory opens nothing.
    outside = tmp_path / "bin" / "codex"
    outside.parent.mkdir()
    outside.write_text("#!/bin/sh\n")
    monkeypatch.setattr(isolation, "CODEX_BIN", str(outside))
    text = isolation.codex_config_toml(bench, tmp_path / "ch", model="m", effort="medium")
    assert '= "read"\n' not in text.split('":slash_tmp" = "read"', 1)[1]


# ── copies of the skills outside the workspace (round 3c) ──


def _fake_cli_build(bench: Bench) -> Path:
    """The layout `uv tool install` gives the scratch CLI, with one bundled skill."""
    data = (
        bench.uv_tools
        / "graph-agents-cli"
        / "lib"
        / "python3.12"
        / "site-packages"
        / "graph_agents_cli"
        / "skills"
        / "data"
    )
    (data / "graph-agents-cli-workflow").mkdir(parents=True)
    (data / "graph-agents-cli-workflow" / "SKILL.md").write_text(
        "---\nname: graph-agents-cli-workflow\n---\n"
    )
    return data.resolve()


def test_bundled_skills_of_the_scratch_cli_are_denied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A round-3b Codex rollout read the shipped workflow SKILL.md bundled in the scratch CLI
    (`.../site-packages/graph_agents_cli/skills/data/`) while a candidate body was under test.
    That directory is denied to both harnesses; the rest of the package (the CLI) is not."""
    monkeypatch.delenv("GAC_SKILLOPT_DENY_READ", raising=False)
    monkeypatch.delenv("GAC_SKILLOPT_OPENAI_KEY_FILE", raising=False)
    bench = Bench(tmp_path / "scratch")
    data = _fake_cli_build(bench)
    assert isolation.bundled_skill_dirs(bench) == (data,)
    assert data in isolation.no_read(bench)
    assert data.parent not in isolation.no_read(bench)  # graph_agents_cli/skills/ stays readable
    work = Path("/private/tmp/gac-x-skillopt/run/00-task")
    settings = isolation.claude_settings(bench, work)
    assert str(data) in settings["sandbox"]["filesystem"]["denyRead"]
    assert f"Read(/{data}/**)" in settings["permissions"]["deny"]
    toml = isolation.codex_config_toml(bench, tmp_path / "ch", model="m", effort="medium")
    assert f'"{data}" = "deny"' in toml


def test_home_copies_of_skills_are_denied(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """uv's cache (every graph-agents-cli wheel ever unpacked, bundled skills included) and uv's
    tool directory (other agent CLIs and their skills) are denied when they exist."""
    home = tmp_path / "home"
    (home / ".cache" / "uv").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("GAC_SKILLOPT_DENY_READ", raising=False)
    monkeypatch.delenv("GAC_SKILLOPT_OPENAI_KEY_FILE", raising=False)
    bench = Bench(tmp_path / "scratch")
    denied = isolation.no_read(bench)
    assert home / ".cache" / "uv" in denied
    assert home / ".local" / "share" / "uv" / "tools" not in denied  # absent: not listed


def test_preflight_probes_the_bundled_skills(tmp_path: Path) -> None:
    p = _probe(tmp_path)
    p.bundled_skill = tmp_path / "data" / "graph-agents-cli-workflow" / "SKILL.md"
    p.skill_copy_dirs = (tmp_path / "home" / ".cache" / "uv",)
    text = preflight.prompt(p)
    assert f"`head -3 {p.bundled_skill}`" in text and f"Read tool on `{p.bundled_skill}`" in text
    assert f"`ls -la {p.skill_copy_dirs[0]}`" in text
    bdir = str(p.bundled_skill.parent.parent)
    base = _all_steps(p, write_ok=False, net_ok=False)
    kw = {"exists": _exists(), "decoy_after": p.decoy_text, "settings_unchanged": True}

    def steps(content: str, glob: str, listing: str) -> list[dict[str, Any]]:
        return [
            _use("Bash", content, command=f"head -3 {p.bundled_skill}"),
            _use("Read", content, file_path=str(p.bundled_skill)),
            _use("Glob", glob, path=bdir, pattern="**/SKILL.md"),
            _use("Bash", listing, command=f"ls -la {p.skill_copy_dirs[0]}"),
        ]

    refused = steps(
        "head: Operation not permitted",
        f"Permission to read {bdir} has been denied.",
        "ls: Operation not permitted",
    )
    readable = steps(
        "---\nname: graph-agents-cli-workflow",
        f"{bdir}/graph-agents-cli-workflow/SKILL.md",
        "total 8\ndrwxr-xr-x  3 u  staff  96 archive-v0",
    )
    assert preflight.judge(p, base + refused, **kw)["skill_copies_unreadable"]["ok"] is True
    assert preflight.judge(p, base + readable, **kw)["skill_copies_unreadable"]["ok"] is False
    assert preflight.judge(p, base, **kw)["skill_copies_unreadable"]["attempted"] is False
