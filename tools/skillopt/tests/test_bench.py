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

"""The verifier, the trace parsers, the fact-check parser and the task data, on fake inputs."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from gac_skillopt import budget, factcheck, optlog, tasks, trace, verify
from gac_skillopt.workspace import Workspace, copy_overlay, file_hashes

# ── task data ──


def test_every_task_is_valid_and_in_one_split() -> None:
    all_tasks = tasks.all_tasks()
    assert len(all_tasks) >= 30
    assert {t.skill for t in all_tasks} == set(tasks.SKILLS)
    assert tasks.split_problems() == []
    for task in all_tasks:
        assert (task.dir / "gold.sh").is_file(), task.id
        assert (task.dir / "broken.sh").is_file(), task.id


def test_holdout_rule_allows_variants_within_a_test_family(tmp_path: Path) -> None:
    """Round 2 holds out variants within families: a train/val task may share a test task's
    family, never its project name or prompt; every test task is frozen."""

    def task(tid: str, family: str, name: str, prompt: str) -> tasks.Task:
        return tasks.Task(
            id=tid,
            skill="graph-agents-cli-workflow",
            family=family,
            task_type="x",
            prompt=prompt,
            fixture={"kind": "empty", "name": name},
            checks=[],
            reference="r",
            dir=tmp_path,
        )

    by_id = {
        "t-test": task("t-test", "fam", "incident-agent", "Build me an incident agent."),
        "t-variant": task("t-variant", "fam", "orders-desk", "Build me an orders agent."),
        "t-copy": task("t-copy", "fam", "incident-agent", "Build me an incident agent."),
        "t-other": task("t-other", "other", "incident-agent", "Something else."),
    }
    split = {
        "train": ["t-variant", "t-other"],
        "val": [],
        "test": ["t-test"],
        "hashes": {"t-test": "x"},
    }
    assert tasks.holdout_problems("s.json", split, by_id) == []
    split["val"] = ["t-copy"]
    problems = tasks.holdout_problems("s.json", split, by_id)
    assert len(problems) == 2 and all("t-copy" in p for p in problems)
    unfrozen = {"train": ["t-variant"], "val": [], "test": ["t-test"], "hashes": {}}
    assert tasks.holdout_problems("s.json", unfrozen, by_id) == [
        "s.json: test task t-test has no frozen hash"
    ]
    assert tasks.holdout_problems("s.json", {"train": ["t-variant"], "test": []}, by_id) == [
        "s.json: no test task"
    ]


def test_local_server_tasks_are_tagged_but_run_on_every_harness() -> None:
    """The tag is informative (the agent runs `eval run`/`run` itself); since the CLI fixes for
    DESIGN section 9 issues 1 and 2, no harness refuses these tasks."""
    by_id = {t.id: t for t in tasks.all_tasks()}
    assert by_id["eval-tool-case-lisbon"].needs_local_server
    assert not by_id["deploy-secret-key"].needs_local_server
    assert not hasattr(tasks, "codex_blockers")


def test_rendered_initial_skill_equals_the_shipped_file() -> None:
    for skill in tasks.SKILLS:
        shipped = (tasks.skill_source(skill) / "SKILL.md").read_text()
        assert tasks.render_skill(skill, tasks.initial_body(skill)) == shipped


def test_render_replaces_candidate_frontmatter() -> None:
    skill = "graph-agents-cli-eval"
    rendered = tasks.render_skill(skill, "---\nname: evil\n---\n\n# Body\n")
    assert rendered.startswith(
        tasks.split_skill((tasks.skill_source(skill) / "SKILL.md").read_text())[0]
    )
    assert "name: evil" not in rendered and rendered.endswith("# Body\n")


# ── verifier ──


def _ws(tmp_path: Path) -> Workspace:
    task = tasks.Task(
        id="t",
        skill="graph-agents-cli-eval",
        family="f",
        task_type="x",
        prompt="p",
        fixture={"kind": "project", "name": "proj"},
        checks=[],
        reference="r",
        dir=tmp_path / "taskdir",
    )
    (tmp_path / "proj").mkdir()
    return Workspace(task=task, root=tmp_path, slot=0)


def test_structured_checks_and_scoring(tmp_path: Path) -> None:
    ws = _ws(tmp_path)
    (ws.project / "d.json").write_text(json.dumps({"cases": [{"id": "a", "expect": {"x": 1}}]}))
    (ws.project / ".env").write_text("A=1\nexport B='two'\n# C=3\n")
    ws.snapshot = file_hashes(ws.project)
    (ws.project / "new.txt").write_text("hello world")
    checks = [
        {
            "id": "json",
            "type": "json",
            "file": "d.json",
            "expr": "any(get(c, 'expect.x') == 1 for c in data['cases'])",
        },
        {"id": "env", "type": "dotenv", "file": ".env", "expr": "data == {'A': '1', 'B': 'two'}"},
        {"id": "file", "type": "file", "path": "new.txt", "regex": "hello", "not_regex": "bye"},
        {"id": "absent", "type": "file", "path": "nope/*", "exists": False},
        {"id": "same", "type": "unchanged", "paths": ["d.json", ".env"]},
        {"id": "changed", "type": "unchanged", "paths": ["."], "mandatory": False},
        {
            "id": "cmd",
            "type": "cmd",
            "run": "echo out; exit 3",
            "expect_exit": [3],
            "output_regex": "^out$",
        },
    ]
    results = verify.Verifier(ws, {"PATH": "/usr/bin:/bin"}, trace.Trace("x")).run(checks)
    by_id = {r.id: r for r in results}
    assert all(by_id[i].passed for i in ("json", "env", "file", "absent", "same", "cmd")), results
    assert not by_id["changed"].passed and "new.txt (added)" in by_id["changed"].detail
    assert verify.score(results) == (1, round(6 / 7, 4))


def test_broken_check_fails_with_the_reason(tmp_path: Path) -> None:
    ws = _ws(tmp_path)
    (ws.project / "d.json").write_text("{}")
    checks = [
        {"id": "bad", "type": "json", "file": "d.json", "expr": "undefined_name", "why": "because"}
    ]
    [result] = verify.Verifier(ws, {}, trace.Trace("x")).run(checks)
    assert not result.passed and result.detail.startswith("because -- check error: NameError")
    assert verify.score([result]) == (0, 0.0)


def test_transcript_matches_simple_commands_only(tmp_path: Path) -> None:
    ws = _ws(tmp_path)
    t = trace.Trace("x", final="Exit code 0; ran on the fake model.")
    t.steps = [
        trace.Step(
            "command", "cd proj && GRAPH_AGENTS_CLI_RUN_PORT=1 timeout 60 graph-agents-cli eval run"
        ),
        trace.Step("command", 'echo "never kubectl create secret here" > notes.txt'),
    ]
    checks = [
        {"id": "ran", "type": "transcript", "must_run": [r"graph-agents-cli\s+eval\s+run"]},
        {"id": "not", "type": "transcript", "must_not_run": [r"kubectl\s+create"]},
        {
            "id": "final",
            "type": "transcript",
            "final_regex": ["fake", r"exit code 0"],
            "final_not_regex": ["ship it"],
        },
        {"id": "missing", "type": "transcript", "must_run": [r"graph-agents-cli\s+lint"]},
    ]
    results = {r.id: r.passed for r in verify.Verifier(ws, {}, t).run(checks)}
    assert results == {"ran": True, "not": True, "final": True, "missing": False}


def test_command_segments() -> None:
    assert verify.command_segments("a=1 b && (timeout 5 c --x) | d; e $(f g)") == [
        "b",
        "c --x)",
        "d",
        "e",
        "f g)",
    ]


def test_command_segments_env_options_and_program_paths() -> None:
    # Seen in a Codex rollout: the CLI by its absolute path, behind `env -u` and assignments.
    line = (
        'set -a; source .env; set +a; env -u ALL_PROXY -u no_proxy K="$V" '
        "/opt/tools/bin/graph-agents-cli eval run --url http://127.0.0.1:1"
    )
    assert verify.command_segments(line)[-1] == "graph-agents-cli eval run --url http://127.0.0.1:1"
    assert verify.command_segments("/usr/bin/env -i ./bin/graph-agents-cli deploy --env prod") == [
        "graph-agents-cli deploy --env prod"
    ]
    # Paths in arguments are left alone.
    assert verify.command_segments("cat /a/b/c.txt") == ["cat /a/b/c.txt"]


def test_command_segments_strip_uv_run() -> None:
    """Round 3b: `uv run graph-agents-cli eval run` runs the CLI (from PATH, in the project's
    environment), but the `proved` check (`must_run: graph-agents-cli\\s+eval\\s+run`) did not
    see it: 2 of the approved text's 6 Claude val failures were these false negatives."""
    want = "graph-agents-cli eval run"
    for line in (
        "uv run graph-agents-cli eval run",
        "uv run --quiet graph-agents-cli eval run",
        "uv run -q --no-sync graph-agents-cli eval run",
        "uv run --project weather-bot graph-agents-cli eval run",
        "uv run --env-file=.env --with httpx graph-agents-cli eval run",
        "uv run -- graph-agents-cli eval run",
        "cd weather-bot && MODEL_PROVIDER=fake uv run graph-agents-cli eval run 2>&1 | tail -80",
        "/opt/x/bin/uv run graph-agents-cli eval run",
    ):
        assert want in [s.split(" 2>&1")[0] for s in verify.command_segments(line)], line
    # A deploy behind `uv run` is still a deploy (must_not_run sees it too).
    assert verify.command_segments("uv run graph-agents-cli deploy --env prod") == [
        "graph-agents-cli deploy --env prod"
    ]
    # Other programs keep their name; `uvx` fetches a tool from PyPI and is left alone.
    assert verify.command_segments("uv run pytest -q") == ["pytest -q"]
    assert verify.command_segments("uvx graph-agents-cli lint") == ["uvx graph-agents-cli lint"]
    assert verify.command_segments("uv sync") == ["uv sync"]


def test_command_segments_respect_quotes_heredocs_and_keywords() -> None:
    # Round 3b, Codex: a quoted search pattern was scored as running `helm upgrade`.
    line = "rg -n -i 'langsmith|secrets apply|deploy.*restart|helm upgrade' trends-agent"
    assert verify.command_segments(line) == [line]
    assert len(verify.command_segments('grep -E "graph-agents-cli deploy|kubectl apply" f')) == 1
    # A command substitution inside double quotes still runs.
    assert verify.command_segments('echo "x=$(graph-agents-cli lint)"')[-1].startswith(
        "graph-agents-cli lint"
    )
    # Here-document bodies and comments are text; what follows them runs.
    heredoc = "cat > n.md <<'EOF'\nDon't run helm upgrade\nEOF\ngraph-agents-cli lint"
    assert verify.command_segments(heredoc) == ["cat > n.md <<'EOF'", "graph-agents-cli lint"]
    assert verify.command_segments("cat <<-END | wc -l\n\thelm upgrade\n\tEND\nls") == [
        "cat <<-END",
        "wc -l",
        "ls",
    ]
    assert verify.command_segments("# don't\ngraph-agents-cli lint") == ["graph-agents-cli lint"]
    # Unbalanced quotes fall back to the plain split (never hide a command).
    assert "helm upgrade x" in verify.command_segments("echo 'oops && helm upgrade x")
    # Compound-command keywords do not hide the program.
    assert verify.command_segments("if graph-agents-cli lint; then helm upgrade a; fi") == [
        "graph-agents-cli lint",
        "helm upgrade a",
        "fi",
    ]
    assert "helm upgrade $f" in verify.command_segments("for f in a b; do helm upgrade $f; done")


def test_overlay_renames_dot_parts(tmp_path: Path) -> None:
    src = tmp_path / "src"
    (src / "dot_github").mkdir(parents=True)
    (src / "dot_env.staging").write_text("A=1\n")
    (src / "dot_github" / "x.yaml").write_text("x: 1\n")
    written = copy_overlay(src, tmp_path / "dest")
    assert sorted(written) == [".env.staging", ".github/x.yaml"]


# ── traces ──

CLAUDE_STREAM = "\n".join(
    json.dumps(e)
    for e in [
        {
            "type": "system",
            "subtype": "init",
            "model": "m",
            "skills": ["graph-agents-cli-eval"],
            "plugins": [{"name": "agents-md"}],
            "mcp_servers": [],
        },
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "1",
                        "name": "Skill",
                        "input": {"skill": "graph-agents-cli-eval"},
                    }
                ]
            },
        },
        {
            "type": "user",
            "message": {
                "content": [{"type": "tool_result", "tool_use_id": "1", "content": "loaded"}]
            },
        },
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "Running."},
                    {
                        "type": "tool_use",
                        "id": "2",
                        "name": "Bash",
                        "input": {"command": "graph-agents-cli eval run"},
                    },
                ]
            },
        },
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "2",
                        "content": [{"type": "text", "text": "x" * 5000}],
                        "is_error": False,
                    }
                ]
            },
        },
        {
            "type": "result",
            "subtype": "success",
            "result": "Done: exit 0.",
            "num_turns": 3,
            "total_cost_usd": 0.1,
            "usage": {"output_tokens": 5},
        },
    ]
)


def test_parse_claude_and_isolation() -> None:
    t = trace.parse_claude(CLAUDE_STREAM, skill="graph-agents-cli-eval", prompt="do it")
    assert (
        t.skill_loaded
        and t.commands == ["graph-agents-cli eval run"]
        and t.final == "Done: exit 0."
    )
    assert t.num_turns == 3 and t.usage["api_equivalent_usd"] == 0.1
    assert trace.claude_isolation_problems(t, "graph-agents-cli-eval") == []
    t.init["skills"] = ["graph-agents-cli-eval", "other"]
    t.init["hook_events"] = 2
    problems = trace.claude_isolation_problems(t, "graph-agents-cli-eval")
    assert len(problems) == 2


def test_parse_claude_keeps_every_result_event() -> None:
    # A background task's notification after the result makes the session answer again.
    extra = {
        "type": "result",
        "subtype": "success",
        "result": "That was the leftover background search; nothing to do.",
        "num_turns": 1,
        "duration_ms": 2000,
        "total_cost_usd": 0.12,
        "usage": {"output_tokens": 1},
    }
    stream = CLAUDE_STREAM + "\n" + json.dumps(extra)
    t = trace.parse_claude(stream, skill="graph-agents-cli-eval", prompt="do it")
    assert t.final.startswith("Done: exit 0.") and t.final.endswith("nothing to do.")
    assert t.num_turns == 4 and t.usage["api_equivalent_usd"] == 0.12
    assert t.usage["result_events"] == 2


def test_conversation_is_compact_and_keeps_checks() -> None:
    t = trace.parse_claude(CLAUDE_STREAM, skill="graph-agents-cli-eval", prompt="do it")
    conv = trace.conversation(
        t,
        skill="graph-agents-cli-eval",
        checks=[
            {"id": "gate", "passed": False, "mandatory": True, "detail": "exit 1", "output": "tail"}
        ],
    )
    assert conv[0] == {"role": "user", "content": "do it"}
    calls = [r for r in conv if r.get("type") == "tool_call"]
    assert calls[0]["obs"] == "[loaded SKILL.md]"
    assert len(calls[1]["obs"]) < 2000 and "characters cut" in calls[1]["obs"]
    assert conv[-1]["role"] == "system" and "check gate (mandatory) FAIL" in conv[-1]["content"]
    many = trace.Trace(
        "x",
        prompt="p",
        final="f",
        steps=[trace.Step("command", f"c{i}", "o" * 700) for i in range(200)],
    )
    small = trace.conversation(many, skill="s", budget=10_000)
    assert sum(len(json.dumps(r)) for r in small) < 12_000
    assert any("omitted from the middle" in str(r.get("content")) for r in small)


def test_parse_codex() -> None:
    stream = "\n".join(
        json.dumps(e)
        for e in [
            {
                "type": "item.completed",
                "item": {
                    "type": "command_execution",
                    "command": "/bin/zsh -lc 'cat .agents/skills/graph-agents-cli-deploy/SKILL.md'",
                    "aggregated_output": "...",
                    "exit_code": 0,
                },
            },
            {
                "type": "item.completed",
                "item": {
                    "type": "command_execution",
                    "command": "/bin/zsh -lc 'graph-agents-cli deploy --env dev --dry-run'",
                    "exit_code": 1,
                },
            },
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 1000,
                    "cached_input_tokens": 800,
                    "cache_write_input_tokens": 190,
                    "output_tokens": 50,
                },
            },
            {"type": "item.completed", "item": {"type": "agent_message", "text": "done"}},
        ]
    )
    t = trace.parse_codex(stream, skill="graph-agents-cli-deploy", last_message="final answer")
    assert t.skill_loaded and t.commands[1] == "graph-agents-cli deploy --env dev --dry-run"
    assert t.steps[1].exit_code == 1 and t.final == "final answer"
    assert t.usage == {
        "input_tokens": 1000,
        "cached_input_tokens": 800,
        "cache_write_input_tokens": 190,
        "output_tokens": 50,
    }


# ── fact-check and budget ──

TREE = {
    "commands": {
        "": {"options": ["--help", "-h", "--version"], "hidden": [], "group": True},
        "eval": {"options": ["--help", "-h"], "hidden": [], "group": True},
        "eval run": {
            "options": ["--dataset", "--help", "-h"],
            "hidden": ["--session-token"],
            "group": False,
        },
    },
    "env_vars": ["GRAPH_AGENTS_CLI_RUN_PORT"],
}


@pytest.mark.parametrize(
    ("snippet", "problem"),
    [
        ("graph-agents-cli eval run --dataset x.json", None),
        ("graph-agents-cli eval run [--dataset F] | tee log", None),
        ("graph-agents-cli eval optimize", "no such command"),
        ("graph-agents-cli eval run --fail-fast", "has no option --fail-fast"),
        ("graph-agents-cli eval run --session-token t", "hidden option"),
    ],
)
def test_factcheck_invocations(snippet: str, problem: str | None) -> None:
    [tokens] = factcheck.invocations(snippet)
    found = factcheck.check_invocation(tokens, TREE)
    assert (found is None) if problem is None else (problem in found)


def test_factcheck_structure_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(factcheck, "command_tree", lambda _python: TREE)
    body = tasks.initial_body("graph-agents-cli-eval")
    assert factcheck.check_candidate("graph-agents-cli-eval", body) == []
    bad = (
        body.replace("## Migration note", "## Notes") + "\nUse GRAPH_AGENTS_CLI_NOPE with Vertex.\n"
    )
    problems = factcheck.check_candidate("graph-agents-cli-eval", bad, cli_python="python")
    assert any("Migration note" in p for p in problems)
    assert any("GRAPH_AGENTS_CLI_NOPE" in p for p in problems)
    assert any("Vertex" in p for p in problems)
    assert any("grew" in p for p in factcheck.check_candidate("graph-agents-cli-eval", body * 2))


def test_budget_prices_and_caps() -> None:
    usage = {"input_tokens": 1_000_000, "cached_input_tokens": 900_000, "output_tokens": 10_000}
    assert budget.usd("gpt-5.6-terra", usage) == pytest.approx(0.2 + 0.18 + 0.12)
    run = budget.RunBudget(
        model="gpt-5.6-terra", max_usd=1.0, ledger=budget.Ledger(None), estimate_per_rollout=0.4
    )
    run.before()
    run.before()
    with pytest.raises(budget.BudgetStop):
        run.before()  # two in flight at 0.40 each: a third could cross 1.00
    run.record(usage, note="t")
    assert run.inflight == 1 and run.spent == pytest.approx(0.5)
    # Cache writes (part of the uncached input) cost $2.50, not $2.00 (a real rollout's usage).
    real = {
        "input_tokens": 149514,
        "cached_input_tokens": 120978,
        "cache_write_input_tokens": 28518,
        "output_tokens": 1290,
    }
    assert budget.usd("gpt-5.6-terra", real) == pytest.approx(
        (18 * 2.00 + 28518 * 2.50 + 120978 * 0.20 + 1290 * 12.00) / 1e6
    )
    with pytest.raises(budget.BudgetStop):
        budget.usd("unknown-model", usage)


def test_optimizer_calls_get_a_timeout_and_a_log_line(tmp_path: Path) -> None:
    log = tmp_path / "optimizer_calls.jsonl"
    seen: list[int] = []

    def chat(**kw: object) -> tuple[str, dict]:
        seen.append(int(kw["timeout"]))  # type: ignore[arg-type]
        if kw["prompt"] == "boom":
            raise TimeoutError("took too long")
        return "patch", {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4}

    call = optlog.logged_call(chat, log, timeout_s=1200, stage=lambda: "analyst")
    assert call(system="s", prompt="p", model="opus", timeout=None)[0] == "patch"
    assert call(system="s", prompt="p", model="opus", timeout=60)[0] == "patch"
    with pytest.raises(TimeoutError):
        call(system="s", prompt="boom", model="opus", timeout=None)
    assert seen == [1200, 60, 1200]  # SkillOpt passes no timeout: the default replaces its 300 s
    rows = [json.loads(line) for line in log.read_text().splitlines()]
    assert [r["ok"] for r in rows] == [True, True, False]
    assert rows[0]["stage"] == "analyst" and rows[0]["usage"]["total_tokens"] == 4
    assert rows[2]["error"].startswith("TimeoutError")
