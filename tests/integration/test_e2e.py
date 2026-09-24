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

"""End-to-end run of the CLI through subprocesses (opt-in: GRAPH_AGENTS_CLI_E2E=1).

Scaffolds real projects with `create`, installs one with `uv sync` (network),
and drives `lint`, `run`, `eval`, `playground`, `build`, `deploy --dry-run`,
`secrets --dry-run`, `infra check`, `info`, `login`, `scaffold enhance/upgrade`
and the API-policy check through the console script. Nothing talks to a
cluster: KUBECONFIG points at an empty file and every kubectl/helm-mutating
command runs under --dry-run.

Marked `slow`; skipped unless GRAPH_AGENTS_CLI_E2E=1 because it runs `uv sync`
(and `helm dependency build`, which fetches the bitnami subcharts).
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
import regen_fixtures as rf

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(
        os.environ.get("GRAPH_AGENTS_CLI_E2E") != "1",
        reason="set GRAPH_AGENTS_CLI_E2E=1 to run the end-to-end suite (uv sync, network)",
    ),
]

CLI = [sys.executable, "-m", "graph_agents_cli.main"]
REGISTRY = "ghcr.io/e2e"
API_KEY = "e2e"
# Parallel runs on one machine pick their own ports: the playground's here, the
# run/eval server's with GRAPH_AGENTS_CLI_RUN_PORT (read by the CLI itself).
PLAYGROUND_PORT = int(os.environ.get("GRAPH_AGENTS_CLI_E2E_PLAYGROUND_PORT", "18790"))


def _env(extra: dict[str, str] | None = None, *, kubeconfig: Path | None = None) -> dict[str, str]:
    env = {
        **os.environ,
        "GRAPH_AGENTS_CLI_NO_UPDATE_CHECK": "1",
        "GRAPH_AGENTS_CLI_DISABLE_OVERRIDES": "1",
        "NO_COLOR": "1",
        "COLUMNS": "200",
    }
    # `uv run` inside the CLI venv exports VIRTUAL_ENV; a rendered project's own
    # `uv sync`/`uv run` must not see it.
    env.pop("VIRTUAL_ENV", None)
    env.pop("MODEL_PROVIDER", None)
    if kubeconfig is not None:
        env["KUBECONFIG"] = str(kubeconfig)
    if extra:
        env.update(extra)
    return env


def cli(
    *args: str,
    cwd: Path,
    env: dict[str, str] | None = None,
    timeout: int = 600,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*CLI, *args],
        cwd=cwd,
        env=env or _env(),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _out(result: subprocess.CompletedProcess[str]) -> str:
    return result.stdout + result.stderr


def _ok(result: subprocess.CompletedProcess[str]) -> subprocess.CompletedProcess[str]:
    assert result.returncode == 0, _out(result)
    return result


def _create(workspace: Path, name: str, *args: str) -> Path:
    _ok(
        cli(
            "create",
            name,
            "-y",
            "--skip-checks",
            "--registry",
            REGISTRY,
            "-o",
            str(workspace),
            *args,
            cwd=workspace,
        )
    )
    project = workspace / name
    assert (project / rf.MANIFEST_FILENAME).is_file()
    return project


def _write_env(project: Path, **overrides: str) -> None:
    lines = (project / ".env.example").read_text(encoding="utf-8").splitlines()
    values = {
        "MODEL_PROVIDER": "fake",
        "CHECKPOINTER": "memory",
        "API_KEY": API_KEY,
        "TRACING_ENABLED": "false",
        "APP_ENV": "dev",
        **overrides,
    }
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        key = line.split("=", 1)[0].strip()
        if key in values and not line.startswith("#"):
            out.append(f"{key}={values[key]}")
            seen.add(key)
        else:
            out.append(line)
    for key, value in values.items():
        if key not in seen:
            out.append(f"{key}={value}")
    (project / ".env").write_text("\n".join(out) + "\n", encoding="utf-8")


def _latest(directory: Path, prefix: str) -> Path:
    files = sorted(directory.glob(f"{prefix}_*.json"), key=lambda p: p.stat().st_mtime)
    assert files, f"no {prefix}_*.json under {directory}"
    return files[-1]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def workspace(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("gacli-e2e")


@pytest.fixture(scope="module")
def kubeconfig(workspace: Path) -> Path:
    path = workspace / "empty-kubeconfig"
    path.write_text("", encoding="utf-8")
    return path


@pytest.fixture(scope="module")
def project2(workspace: Path) -> Path:
    """Combination 2 (fastapi / postgres / kubernetes / cd skip), installed."""
    project = _create(
        workspace,
        "p2-k8s",
        "--runtime",
        "fastapi",
        "--cd",
        "skip",
        "--checkpointer",
        "postgres",
        "-d",
        "kubernetes",
    )
    _write_env(project)
    _ok(cli("install", cwd=project, timeout=1200))
    yield project
    cli("run", "--stop-server", cwd=project)


@pytest.fixture(scope="module")
def project5(workspace: Path) -> Path:
    """Combination 5: API policy + process document (not installed)."""
    return _create(
        workspace,
        "p5-policy",
        "--runtime",
        "fastapi",
        "--cd",
        "skip",
        "-d",
        "kubernetes",
        "--api-policy",
        str(rf.SAMPLE_POLICY),
        "--process",
        "docs/process.md",
    )


# ---------------------------------------------------------------------------
# A. scaffold
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", list(rf.COMBINATIONS))
def test_scaffold_matrix_matches_fixtures(workspace: Path, name: str) -> None:
    out_dir = workspace / "matrix" / name
    out_dir.mkdir(parents=True)
    _ok(cli(*rf.create_args(name, out_dir), cwd=workspace))
    project = out_dir / rf.COMBINATIONS[name].project_name
    expected_files, expected_manifest = rf.read_fixture(name)
    assert rf.list_files(project) == expected_files
    assert rf.normalized_manifest(project) == expected_manifest


def test_invalid_combination_is_refused_with_the_reason(workspace: Path) -> None:
    result = cli(
        "create",
        "bad-combo",
        "-y",
        "--skip-checks",
        "--registry",
        REGISTRY,
        "-o",
        str(workspace),
        "--checkpointer",
        "memory",
        "-d",
        "kubernetes",
        cwd=workspace,
    )
    assert result.returncode == 2
    assert "Invalid combination" in _out(result)
    assert "multi-replica and restarts lose state" in _out(result)
    assert not (workspace / "bad-combo").exists()


def test_cd_requires_kubernetes(workspace: Path) -> None:
    result = cli(
        "create",
        "bad-cd",
        "-y",
        "--skip-checks",
        "-o",
        str(workspace),
        "-d",
        "none",
        "--cd",
        "argocd",
        cwd=workspace,
    )
    assert result.returncode == 2
    assert "requires --deployment-target kubernetes" in _out(result)


def test_prototype_forces_none_and_skip(workspace: Path) -> None:
    project = _create(workspace, "proto", "--prototype", "--cd", "argocd")
    manifest = (project / rf.MANIFEST_FILENAME).read_text(encoding="utf-8")
    assert "deployment_target: 'none'" in manifest
    assert "cd: 'skip'" in manifest
    assert not (project / "deployment").exists()


def test_project_name_is_normalised(workspace: Path) -> None:
    result = _ok(
        cli(
            "create",
            "My_Agent",
            "-y",
            "--skip-checks",
            "-o",
            str(workspace),
            "-d",
            "none",
            cwd=workspace,
        )
    )
    assert "my-agent" in _out(result)
    assert (workspace / "my-agent" / rf.MANIFEST_FILENAME).is_file()
    assert not (workspace / "My_Agent").exists()


def test_registry_defaults_to_git_origin_owner(workspace: Path) -> None:
    if shutil.which("git") is None:
        pytest.skip("git not on PATH")
    repo = workspace / "acme-repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/acme/x.git"], cwd=repo, check=True
    )
    _ok(cli("create", "x-agent", "-y", "--skip-checks", cwd=repo))
    manifest = (repo / "x-agent" / rf.MANIFEST_FILENAME).read_text(encoding="utf-8")
    assert "registry: 'ghcr.io/acme'" in manifest


# ---------------------------------------------------------------------------
# B. project 2: lint, run, eval, playground, build, deploy, secrets, infra, info
# ---------------------------------------------------------------------------


def test_lint_passes(project2: Path) -> None:
    result = _ok(cli("lint", cwd=project2))
    # The default project declares no API policy and its tools call no API.
    assert "API policy check: nothing to check" in _out(result)


def test_run_one_off_server(project2: Path) -> None:
    result = _ok(cli("run", "hi", cwd=project2))
    out = _out(result)
    assert "Hello! How can I help you today?" in out
    assert "Local server stopped." in out
    assert "Thread:" in out
    assert not (project2 / ".graph-agents-cli" / "run_server.json").exists()


def test_run_persistent_server_and_thread_resume(project2: Path) -> None:
    try:
        first = _ok(cli("run", "--start-server", "hi", cwd=project2))
        pid_file = project2 / ".graph-agents-cli" / "run_server.json"
        assert pid_file.is_file()
        info = json.loads(pid_file.read_text(encoding="utf-8"))
        assert {"pid", "port", "started_at", "last_activity", "runtime", "checkpointer"} <= set(
            info
        )
        thread_id = next(
            line.split("Thread:", 1)[1].strip()
            for line in _out(first).splitlines()
            if "Thread:" in line
        )
        second = _ok(cli("run", "and again", "--thread-id", thread_id, cwd=project2))
        assert "You said: and again" in _out(second)
        messages = httpx.get(
            f"http://127.0.0.1:{info['port']}/threads/{thread_id}/messages",
            headers={"Authorization": f"Bearer {API_KEY}"},
            timeout=10,
        ).json()
        assert [m["role"] for m in messages] == ["user", "assistant", "user", "assistant"]
    finally:
        stop = cli("run", "--stop-server", cwd=project2)
    assert stop.returncode == 0, _out(stop)
    assert not (project2 / ".graph-agents-cli" / "run_server.json").exists()


def test_eval_run_gate_met(project2: Path) -> None:
    result = _ok(cli("eval", "run", cwd=project2))
    assert "gate met" in _out(result)
    results = json.loads(
        _latest(project2 / "artifacts" / "grade_results", "results").read_text(encoding="utf-8")
    )
    assert {"dataset_hash", "graded_at", "judge", "capture", "summary", "quality", "cases"} <= set(
        results
    )
    assert results["summary"]["exit_code"] == 0
    assert results["summary"]["passed"] == len(results["cases"])
    assert {c["status"] for c in results["cases"]} == {"passed"}
    assert results["quality"]["response_quality"]["met"] is True
    assert results["judge"]["provider"] == "fake"
    traces = json.loads(
        _latest(project2 / "artifacts" / "traces", "traces").read_text(encoding="utf-8")
    )
    assert {"dataset_hash", "generated_at", "agent_version", "model", "traces"} <= set(traces)
    assert {t["status"] for t in traces["traces"]} == {"ok"}


def test_eval_run_deterministic_failure_exits_1(project2: Path) -> None:
    dataset = project2 / "tests" / "eval" / "datasets" / "basic-dataset.json"
    original = dataset.read_text(encoding="utf-8")
    data = json.loads(original)
    for case in data["cases"]:
        if case["id"] == "greeting":
            case["expect"]["contains"] = ["Goodbye"]
    dataset.write_text(json.dumps(data, indent=2), encoding="utf-8")
    try:
        result = cli("eval", "run", cwd=project2)
    finally:
        dataset.write_text(original, encoding="utf-8")
    assert result.returncode == 1, _out(result)
    assert "does not contain 'Goodbye'" in _out(result)


def test_eval_grade_missing_trace_exits_2(project2: Path) -> None:
    _ok(cli("eval", "generate", cwd=project2))
    traces_file = _latest(project2 / "artifacts" / "traces", "traces")
    doc = json.loads(traces_file.read_text(encoding="utf-8"))
    doc["traces"] = [t for t in doc["traces"] if t["case_id"] != "weather"]
    traces_file.write_text(json.dumps(doc), encoding="utf-8")
    result = cli("eval", "grade", "--traces", str(traces_file), cwd=project2)
    assert result.returncode == 2, _out(result)
    assert "incomplete run" in _out(result)
    results = json.loads(
        _latest(project2 / "artifacts" / "grade_results", "results").read_text(encoding="utf-8")
    )
    assert results["summary"]["missing"] == 1
    assert {c["id"]: c["status"] for c in results["cases"]}["weather"] == "missing"


def test_eval_compare_analyze_and_metric_list(project2: Path) -> None:
    results_dir = project2 / "artifacts" / "grade_results"
    files = sorted(results_dir.glob("results_*.json"), key=lambda p: p.stat().st_mtime)
    assert len(files) >= 2
    compare = _ok(cli("eval", "compare", str(files[0]), str(files[-1]), cwd=project2))
    assert "Case status changes" in _out(compare)
    analyze = _ok(cli("eval", "analyze", cwd=project2))
    assert "Analysis saved to" in _out(analyze)
    metrics = _ok(cli("eval", "metric", "list", cwd=project2))
    assert "response_quality" in _out(metrics)
    assert "tool_calls" in _out(metrics)


def test_playground_serves_html_and_health(project2: Path) -> None:
    proc = subprocess.Popen(
        [*CLI, "playground", "--no-open", "--port", str(PLAYGROUND_PORT)],
        cwd=project2,
        env=_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    base = f"http://127.0.0.1:{PLAYGROUND_PORT}"
    try:
        deadline = time.monotonic() + 90
        health = None
        while time.monotonic() < deadline and proc.poll() is None:
            try:
                health = httpx.get(f"{base}/health", timeout=2)
                break
            except httpx.TransportError:
                time.sleep(0.5)
        assert health is not None and health.status_code == 200, proc.stdout.read()
        assert health.json() == {"status": "ok", "runtime": "fastapi", "checkpointer": "memory"}
        page = httpx.get(f"{base}/playground", timeout=10)
        assert page.status_code == 200
        assert page.headers["content-type"].startswith("text/html")
        assert "<html" in page.text.lower()
    finally:
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=10)


def test_build_dry_run(project2: Path) -> None:
    result = _ok(cli("build", "--dry-run", cwd=project2))
    assert f"docker build -t {REGISTRY}/p2-k8s:latest -f Dockerfile ." in _out(result)


def test_deploy_dry_run_renders_chart(project2: Path, kubeconfig: Path) -> None:
    if shutil.which("helm") is None:
        pytest.skip("helm not on PATH")
    result = cli(
        "deploy", "--env", "dev", "--dry-run", cwd=project2, env=_env(kubeconfig=kubeconfig)
    )
    out = _out(result)
    assert result.returncode == 0, out
    assert f"[dry-run] docker build -t {REGISTRY}/p2-k8s:" in out
    assert "[dry-run] docker push" in out or "[dry-run] kind load" in out
    assert "[dry-run] kubectl create secret generic p2-k8s-app" in out
    assert "[dry-run] helm dependency build" in out
    assert "[dry-run] helm upgrade --install p2-k8s" in out
    assert out.index("helm dependency build") < out.index("helm upgrade --install")
    assert "kind: Deployment" in out
    assert "kind: ConfigMap" in out
    assert "Would deploy p2-k8s" in out
    # The printed `helm template` equivalent renders for dev and prod for real.
    chart = project2 / "deployment" / "helm" / "p2-k8s"
    for env_name in ("dev", "prod"):
        rendered = subprocess.run(
            [
                "helm",
                "template",
                "p2-k8s",
                str(chart),
                "-f",
                str(chart / "values.yaml"),
                "-f",
                str(chart / f"values-{env_name}.yaml"),
                "--set",
                f"image.repository={REGISTRY}/p2-k8s",
                "--set",
                "image.tag=abc1234",
                "--set",
                "existingSecret=p2-k8s-app",
                "--set",
                "gateway.parentRef.name=eg",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert rendered.returncode == 0, rendered.stderr
        assert "kind: Deployment" in rendered.stdout
    assert "kind: HTTPRoute" in rendered.stdout  # prod: gateway enabled


def test_secrets_dry_run_never_prints_a_key(project2: Path, kubeconfig: Path) -> None:
    env = _env(kubeconfig=kubeconfig)
    with_key = _ok(cli("secrets", "apply", "--env", "dev", "--dry-run", cwd=project2, env=env))
    assert "Generated API_KEY" not in _out(with_key)
    assert "kubectl create secret generic p2-k8s-app" in _out(with_key)
    assert "API_KEY: <redacted>" in _out(with_key)
    env_file = project2 / ".env"
    original = env_file.read_text(encoding="utf-8")
    env_file.write_text(
        "\n".join(line for line in original.splitlines() if not line.startswith("API_KEY=")) + "\n",
        encoding="utf-8",
    )
    try:
        without = _ok(cli("secrets", "apply", "--env", "dev", "--dry-run", cwd=project2, env=env))
    finally:
        env_file.write_text(original, encoding="utf-8")
    # The real run keeps the live Secret's key or generates one; --dry-run reads nothing and prints no key.
    assert "Generated API_KEY" not in _out(without)
    assert "API_KEY is not in the env file" in _out(without)
    assert "API_KEY: <redacted>" in _out(without)
    status = _ok(cli("secrets", "status", "--env", "dev", "--dry-run", cwd=project2, env=env))
    assert "kubectl get secret p2-k8s-app" in _out(status)


def test_deploy_refuses_digest_image_and_blank_gateway(project2: Path, kubeconfig: Path) -> None:
    env = _env(kubeconfig=kubeconfig)
    digest = cli(
        "deploy",
        "--env",
        "dev",
        "--image",
        f"{REGISTRY}/p2-k8s@sha256:{'0' * 64}",
        "--dry-run",
        cwd=project2,
        env=env,
    )
    assert digest.returncode == 3, _out(digest)
    assert "digest reference" in _out(digest)
    # The scaffolded prod values enable the gateway with a blank parentRef: exit 3 before any tool.
    prod = cli("deploy", "--env", "prod", "--tag", "abc1234", "--dry-run", cwd=project2, env=env)
    assert prod.returncode == 3, _out(prod)
    assert "gateway.parentRef.name is blank" in _out(prod)
    assert "[dry-run] docker build" not in _out(prod)


def test_infra_check_without_cluster(project2: Path, kubeconfig: Path) -> None:
    result = cli("infra", "check", "--env", "dev", cwd=project2, env=_env(kubeconfig=kubeconfig))
    assert result.returncode == 1
    out = _out(result)
    assert "cluster reachable" in out
    assert "Missing required prerequisites" in out
    as_json = cli(
        "infra", "check", "--env", "dev", "--json", cwd=project2, env=_env(kubeconfig=kubeconfig)
    )
    report = json.loads(as_json.stdout)
    assert report["ok"] is False
    assert any(c["name"] == "cluster reachable" and c["status"] != "ok" for c in report["checks"])


def test_infra_check_disconnected_profile(project2: Path, kubeconfig: Path) -> None:
    result = cli(
        "infra", "check", "--profile", "disconnected", cwd=project2, env=_env(kubeconfig=kubeconfig)
    )
    assert result.returncode == 1
    assert "disconnected: model provider" in _out(result)


def test_info_text_and_json(project2: Path) -> None:
    text = _ok(cli("info", cwd=project2))
    assert "Runtime:            fastapi" in text.stdout
    assert "Registry:           ghcr.io/e2e" in text.stdout
    as_json = json.loads(_ok(cli("info", "--json", cwd=project2)).stdout)
    project = as_json["project"]
    assert project["project_name"] == "p2-k8s"
    assert project["environments"]["dev"]["namespace"] == "p2-k8s-dev"
    assert "POSTGRES_DSN" in project["secret_keys"]


def test_extension_list(project2: Path) -> None:
    result = _ok(cli("extension", "list", cwd=project2))
    assert "No extensions installed" in _out(result)


def test_login_status_and_profiles(project2: Path) -> None:
    status = _ok(cli("login", "--status", cwd=project2))
    assert "fake" in _out(status)
    plain = _ok(cli("login", cwd=project2))  # fake provider: no key needed
    assert "test-only fake model" in _out(plain)
    compat = cli(
        "login",
        cwd=project2,
        env=_env({"MODEL_PROVIDER": "openai-compatible", "OPENAI_BASE_URL": ""}),
    )
    assert compat.returncode == 1
    assert "OPENAI_BASE_URL not set" in _out(compat)


def test_project_own_tests_pass(project2: Path) -> None:
    result = subprocess.run(
        ["uv", "run", "pytest", "tests/unit", "tests/integration", "-q", "-p", "no:cacheprovider"],
        cwd=project2,
        env=_env(),
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert " passed" in result.stdout


def test_enhance_to_argocd_then_upgrade_is_noop(workspace: Path) -> None:
    project = _create(
        workspace, "p2-enh", "--runtime", "fastapi", "--cd", "skip", "-d", "kubernetes"
    )
    # enhance backs the project up to ~/.graph-agents-cli/backups: a temporary home
    # keeps it out of the developer's (these commands install nothing, so no uv cache).
    home = workspace / "home-p2-enh"
    home.mkdir()
    env = _env({"HOME": str(home)})
    before = {p: (project / p).read_bytes() for p in ("app/agent.py", "app/fast_api_app.py")}
    result = _ok(cli("scaffold", "enhance", "--cd", "argocd", "-y", cwd=project, env=env))
    assert str(home / ".graph-agents-cli" / "backups") in _out(result)
    assert "Enhancement complete" in _out(result)
    for rel in (
        "deployment/argocd/application-dev.yaml",
        "deployment/argocd/application-prod.yaml",
        ".github/workflows/staging.yaml",
        ".github/workflows/promote-to-prod.yaml",
        ".github/CODEOWNERS",
    ):
        assert (project / rel).is_file(), rel
    manifest = (project / rf.MANIFEST_FILENAME).read_text(encoding="utf-8")
    assert "cd: argocd" in manifest or "cd: 'argocd'" in manifest
    for rel, content in before.items():
        assert (project / rel).read_bytes() == content, f"{rel} was modified"
    upgrade = _ok(cli("scaffold", "upgrade", "-y", cwd=project, env=env))
    assert "already at version" in _out(upgrade)


# ---------------------------------------------------------------------------
# D. A2A
# ---------------------------------------------------------------------------


def test_a2a_mode(project2: Path) -> None:
    import importlib.util

    result = cli("run", "--mode", "a2a", "hi", cwd=project2)
    if importlib.util.find_spec("a2a") is None:
        assert result.returncode == 1
        hint = [line for line in _out(result).splitlines() if "graph-agents-cli[a2a]" in line]
        assert len(hint) == 1, _out(result)
        assert "Starting" not in _out(result)  # no server was started for a missing extra
    else:
        assert result.returncode == 0, _out(result)
        assert "Hello! How can I help you today?" in _out(result)


# ---------------------------------------------------------------------------
# E. API policy
# ---------------------------------------------------------------------------


def test_policy_lint_passes_with_template_tools(project5: Path) -> None:
    result = _ok(cli("lint", "--policy-only", cwd=project5))
    assert "All declared API calls are allowed" in _out(result)
    assert "example_api.py" in _out(result)


def test_policy_lint_flags_a_denied_tool_and_a_leftover_product_calls(project5: Path) -> None:
    tool = project5 / "app" / "tools" / "mutate.py"
    tool.write_text(
        'API_CALLS = [{"api": "orders", "method": "DELETE", "operation_id": "deleteOrder",'
        ' "path": "/orders/{order_id}"}]\n'
        "TOOLS: list = []\n",
        encoding="utf-8",
    )
    try:
        result = cli("lint", "--policy-only", cwd=project5)
    finally:
        tool.unlink()
    assert result.returncode == 1
    out = _out(result)
    assert "mutate.py" in out and "DELETE" in out and "denied" in out
    assert "1 violation(s)" in out
    assert "graph-agents-cli api revoke orders deleteOrder --from denied" in out
    old = project5 / "app" / "tools" / "old.py"
    old.write_text('PRODUCT_CALLS = [{"method": "GET", "path": "/x"}]\n', encoding="utf-8")
    try:
        result = cli("lint", "--policy-only", cwd=project5)
    finally:
        old.unlink()
    assert result.returncode == 1
    assert "renamed to API_CALLS" in _out(result)


def test_lint_stops_on_the_retired_policy_file(project5: Path) -> None:
    legacy = project5 / "product-policy.yaml"
    legacy.write_text("product_api:\n  allowed_methods: [GET]\n", encoding="utf-8")
    try:
        result = cli("lint", "--policy-only", cwd=project5)
    finally:
        legacy.unlink()
    assert result.returncode == 3
    assert "Migrate it to api-policy.yaml" in _out(result)


def test_policy_lint_with_openapi_accepts_and_rejects(project5: Path) -> None:
    policy = project5 / "api-policy.yaml"
    original = policy.read_text(encoding="utf-8")
    (project5 / "docs").mkdir(exist_ok=True)
    (project5 / "docs" / "example-openapi.yaml").write_text(
        "openapi: 3.0.0\n"
        'info: {title: Example API, version: "1"}\n'
        "paths:\n"
        "  /items/{item_id}:\n"
        "    get:\n"
        "      operationId: getItem\n"
        '      responses: {"200": {description: ok}}\n'
        "  /items:\n"
        "    post:\n"
        "      operationId: createItem\n"
        '      responses: {"201": {description: created}}\n',
        encoding="utf-8",
    )
    tool = project5 / "app" / "tools" / "mutate.py"
    tool.write_text(
        'API_CALLS = [{"api": "orders", "method": "POST", "operation_id": "createItem",'
        ' "path": "/items"}]\n'
        "TOOLS: list = []\n",
        encoding="utf-8",
    )
    try:
        policy.write_text(
            "apis:\n"
            "  orders:\n"
            "    base_url_env: ORDERS_API_BASE_URL\n"
            "    auth: bearer\n"
            "    token_env: ORDERS_API_TOKEN\n"
            "    allowed_methods: [GET, POST]\n"
            "    allowed_operations:\n"
            "      - operationId: listOrders\n"
            "      - operationId: getItem\n"
            "      - operationId: createItem\n"
            "    openapi: docs/example-openapi.yaml\n",
            encoding="utf-8",
        )
        (project5 / "docs" / "example-openapi.yaml").write_text(
            (project5 / "docs" / "example-openapi.yaml").read_text(encoding="utf-8")
            + "  /orders:\n"
            "    get:\n"
            "      operationId: listOrders\n"
            '      responses: {"200": {description: ok}}\n',
            encoding="utf-8",
        )
        accepted = _ok(cli("lint", "--policy-only", cwd=project5))
        assert "spec: POST /items" in _out(accepted)
        # An operation the policy allows but the spec does not know is a violation.
        ghost = project5 / "app" / "tools" / "ghost.py"
        ghost.write_text(
            'API_CALLS = [{"api": "orders", "method": "GET", "operation_id": "getGhost"}]\n'
            "TOOLS: list = []\n",
            encoding="utf-8",
        )
        policy.write_text(
            policy.read_text(encoding="utf-8").replace(
                "      - operationId: createItem\n",
                "      - operationId: createItem\n      - operationId: getGhost\n",
            ),
            encoding="utf-8",
        )
        try:
            rejected = cli("lint", "--policy-only", cwd=project5)
        finally:
            ghost.unlink()
        assert rejected.returncode == 1
        assert "getGhost" in _out(rejected) and "unknown" in _out(rejected)
        # createItem dropped from allowed_operations -> denied.
        policy.write_text(
            "\n".join(
                line
                for line in policy.read_text(encoding="utf-8").splitlines()
                if "createItem" not in line and "getGhost" not in line
            )
            + "\n",
            encoding="utf-8",
        )
        denied = cli("lint", "--policy-only", cwd=project5)
        assert denied.returncode == 1
        assert "not in allowed_operations" in _out(denied)
    finally:
        tool.unlink(missing_ok=True)
        policy.write_text(original, encoding="utf-8")


RESTRICTIVE_POLICY = """\
apis:
  orders:
    base_url_env: ORDERS_API_BASE_URL
    auth: none
    allowed_methods: [GET, POST]
    allowed_operations:
      - {operationId: getOrderLine, path: "/orders/{order_id}/lines/{line_no}"}
      - {operationId: createOrder, path: /orders, methods: [POST]}
    denied_operations:
      - path: /orders/admin/lines/{line_no}
"""

# Dropped into the installed project: the rendered example tool, called for real
# through the policy-enforcing client against a mock transport.
EXAMPLE_TOOL_TEST = """\
import httpx
import pytest

from app.app_utils import api_client
from app.tools import example_api


async def test_the_example_tool_calls_its_declared_path(monkeypatch):
    monkeypatch.setenv("ORDERS_API_BASE_URL", "https://orders.test/v2")
    api_client.reset_policy_cache()
    sent = []

    def handler(request):
        sent.append(str(request.url))
        return httpx.Response(200, json={"line": 7})

    real = api_client.get_client
    monkeypatch.setattr(
        example_api,
        "get_client",
        lambda name, context=None: real(
            name, context=context, transport=httpx.MockTransport(handler)
        ),
    )
    call = example_api.TOOLS[0].coroutine
    assert await call(order_id="42", line_no="7", runtime=None) == '{"line": 7}'
    for order_id in ("admin", ".."):
        with pytest.raises(api_client.ApiPolicyError):
            await call(order_id=order_id, line_no="7", runtime=None)
    assert sent == ["https://orders.test/v2/orders/42/lines/7"]
"""


@pytest.fixture(scope="module")
def project7(workspace: Path) -> Path:
    """A restrictive seed policy (its first operation has path parameters), installed."""
    policy = workspace / "orders-policy.yaml"
    policy.write_text(RESTRICTIVE_POLICY, encoding="utf-8")
    project = _create(
        workspace,
        "p7-orders",
        "--runtime",
        "fastapi",
        "--cd",
        "skip",
        "-d",
        "kubernetes",
        "--api-policy",
        str(policy),
    )
    _write_env(project)
    _ok(cli("install", cwd=project, timeout=1200))
    return project


def test_a_restrictive_seed_policy_gives_a_project_that_lints_and_passes_its_tests(
    project7: Path,
) -> None:
    tool = (project7 / "app" / "tools" / "example_api.py").read_text(encoding="utf-8")
    assert '"operation_id": "getOrderLine"' in tool
    lint = _ok(cli("lint", cwd=project7, timeout=900))
    assert "All declared API calls are allowed" in _out(lint)
    extra = project7 / "tests" / "unit" / "test_example_tool_e2e.py"
    extra.write_text(EXAMPLE_TOOL_TEST, encoding="utf-8")
    try:
        result = subprocess.run(
            [
                "uv",
                "run",
                "pytest",
                "tests/unit",
                "tests/integration",
                "-q",
                "-p",
                "no:cacheprovider",
            ],
            cwd=project7,
            env=_env({"MODEL_PROVIDER": "fake"}),
            capture_output=True,
            text=True,
            timeout=900,
            check=False,
        )
    finally:
        extra.unlink()
    assert result.returncode == 0, result.stdout + result.stderr
    assert " passed" in result.stdout


# ---------------------------------------------------------------------------
# F. The policy's lifecycle: create without one, then `graph-agents-cli api`
# ---------------------------------------------------------------------------

ORDERS_READ_TOOL = """\
from __future__ import annotations

import json
from typing import Any

from langchain.tools import ToolRuntime
from langchain_core.tools import tool

from app.app_utils.api_client import get_client

API_CALLS: list[dict[str, str]] = [
    {"api": "orders", "method": "GET", "operation_id": "listOrders", "path": "/orders"},
    {"api": "orders", "method": "GET", "operation_id": "getOrder", "path": "/orders/{order_id}"},
]


@tool
async def list_orders(runtime: ToolRuntime[Any]) -> str:
    \"\"\"List the orders.\"\"\"
    client = get_client("orders", context=getattr(runtime, "context", None))
    data = await client.get("/orders", operation_id="listOrders")
    return data if isinstance(data, str) else json.dumps(data)


@tool
async def get_order(order_id: str, runtime: ToolRuntime[Any]) -> str:
    \"\"\"Return the order ORDER_ID.\"\"\"
    client = get_client("orders", context=getattr(runtime, "context", None))
    data = await client.get(
        "/orders/{order_id}", operation_id="getOrder", path_params={"order_id": order_id}
    )
    return data if isinstance(data, str) else json.dumps(data)


TOOLS = [list_orders, get_order]
"""

ORDERS_WRITE_TOOL = """\
from __future__ import annotations

import json
from typing import Any

from langchain.tools import ToolRuntime
from langchain_core.tools import tool

from app.app_utils.api_client import get_client

API_CALLS: list[dict[str, str]] = [
    {"api": "orders", "method": "POST", "operation_id": "createOrder", "path": "/orders"},
]


@tool
async def create_order(sku: str, quantity: int, runtime: ToolRuntime[Any]) -> str:
    \"\"\"Order QUANTITY of SKU.\"\"\"
    client = get_client("orders", context=getattr(runtime, "context", None))
    data = await client.post(
        "/orders", operation_id="createOrder", json_body={"sku": sku, "quantity": quantity}
    )
    return data if isinstance(data, str) else json.dumps(data)


TOOLS = [create_order]
"""

ORDERS_DELETE_TOOL = """\
API_CALLS: list[dict[str, str]] = [
    {"api": "orders", "method": "DELETE", "operation_id": "deleteOrder",
     "path": "/orders/{order_id}"},
]
TOOLS: list = []
"""

# Dropped into the installed project: the tools call a real HTTP server on
# 127.0.0.1 through the policy-enforcing client.
LIFECYCLE_RUNTIME_TEST = """\
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.app_utils import api_client
from app.tools import orders_read, orders_write


class Handler(BaseHTTPRequestHandler):
    def _answer(self):
        length = int(self.headers.get("content-length") or 0)
        body = json.loads(self.rfile.read(length)) if length else None
        data = json.dumps(
            {
                "method": self.command,
                "path": self.path,
                "body": body,
                "auth": self.headers.get("authorization"),
            }
        ).encode()
        self.send_response(201 if self.command == "POST" else 200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = do_DELETE = _answer

    def log_message(self, *args):
        return


@pytest.fixture
def server(monkeypatch):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("ORDERS_API_BASE_URL", f"http://127.0.0.1:{srv.server_address[1]}/api")
    monkeypatch.setenv("ORDERS_API_TOKEN", "t0k")
    api_client.reset_policy_cache()
    api_client.reset_limits()
    yield srv
    srv.shutdown()
    srv.server_close()
    thread.join(5)


async def test_the_tools_reach_the_api_through_the_policy(server):
    listed = json.loads(await orders_read.list_orders.coroutine(runtime=None))
    assert (listed["method"], listed["path"], listed["auth"]) == ("GET", "/api/orders", "Bearer t0k")
    got = json.loads(await orders_read.get_order.coroutine(order_id="7", runtime=None))
    assert got["path"] == "/api/orders/7"
    created = json.loads(
        await orders_write.create_order.coroutine(sku="a-1", quantity=2, runtime=None)
    )
    assert created["method"] == "POST"
    assert created["body"] == {"sku": "a-1", "quantity": 2}


async def test_a_denied_operation_is_refused_at_runtime(server):
    client = api_client.get_client("orders", run_id="deny")
    with pytest.raises(api_client.ApiPolicyError, match="denied"):
        await client.delete(
            "/orders/{order_id}", operation_id="deleteOrder", path_params={"order_id": "1"}
        )


async def test_limits_are_enforced_at_runtime(server):
    client = api_client.get_client("orders", run_id="limits")
    for _ in range(3):
        await client.get("/orders", operation_id="listOrders")
    with pytest.raises(api_client.ApiPolicyError, match="max_calls_per_run"):
        await client.get("/orders", operation_id="listOrders")
    other = api_client.get_client("orders", run_id="another-run")
    await other.get("/orders", operation_id="listOrders")
"""


@pytest.fixture(scope="module")
def project8(workspace: Path) -> Path:
    """A project created without a policy, installed; its policy grows with `api`."""
    project = _create(
        workspace, "p8-lifecycle", "--runtime", "fastapi", "--cd", "skip", "-d", "kubernetes"
    )
    _write_env(project)
    _ok(cli("install", cwd=project, timeout=1200))
    return project


def test_the_api_policy_lifecycle_on_a_fresh_project(project8: Path) -> None:
    project = project8
    policy = project / "api-policy.yaml"
    assert not policy.exists()
    add = [
        *("api", "add", "orders", "--base-url-env", "ORDERS_API_BASE_URL"),
        *("--auth", "bearer", "--token-env", "ORDERS_API_TOKEN", "--access", "read-write"),
        *("--max-calls-per-run", "3", "--rate-per-minute", "60"),
    ]
    dry = _ok(cli(*add, "--dry-run", cwd=project))
    assert "+++ b/api-policy.yaml" in _out(dry) and not policy.exists()
    _ok(cli(*add, cwd=project))
    # A comment the team adds survives every later change.
    policy.write_text(
        policy.read_text(encoding="utf-8").replace(
            "  orders:\n", "  orders:  # owned by the orders team\n"
        ),
        encoding="utf-8",
    )
    steps = [
        ["api", "allow", "orders", "listOrders", "--methods", "GET"],
        ["api", "allow", "orders", "createOrder", "--methods", "POST"],
        ["api", "allow", "orders", "--method", "GET", "--path", "/orders/{order_id}"],
        ["api", "allow", "orders", "cancelOrder", "--methods", "DELETE"],
        ["api", "deny", "orders", "--method", "DELETE", "--path", "/orders/{order_id}"],
        ["api", "revoke", "orders", "cancelOrder"],
        ["api", "access", "orders", "custom", "--methods", "GET,HEAD,POST,DELETE"],
        ["api", "limits", "orders", "--rate-per-minute", "none"],
        ["api", "limits", "orders", "--rate-per-minute", "120"],
    ]
    for step in steps:
        before = policy.read_text(encoding="utf-8")
        dry = _ok(cli(*step, "--dry-run", cwd=project))
        assert "Dry run: nothing was written." in _out(dry), step
        assert policy.read_text(encoding="utf-8") == before, step
        real = _ok(cli(*step, cwd=project))
        assert "--- a/api-policy.yaml" in _out(real), step
        assert "# owned by the orders team" in policy.read_text(encoding="utf-8"), step
    shown = json.loads(_ok(cli("api", "show", "orders", "--json", cwd=project)).stdout)
    orders = shown["apis"]["orders"]
    assert orders["allowed_methods"] == ["GET", "HEAD", "POST", "DELETE"]
    assert orders["allowed_operations"] == [
        {"operationId": "listOrders", "methods": ["GET"]},
        {"operationId": "createOrder", "methods": ["POST"]},
        {"path": "/orders/{order_id}", "methods": ["GET"]},
    ]
    assert orders["denied_operations"] == [{"path": "/orders/{order_id}", "methods": ["DELETE"]}]
    assert orders["limits"] == {"max_calls_per_run": 3, "rate_per_minute": 120}
    manifest = (project / rf.MANIFEST_FILENAME).read_text(encoding="utf-8")
    assert "ORDERS_API_TOKEN" in manifest and "policy_file: api-policy.yaml" in manifest

    tools = project / "app" / "tools"
    (tools / "orders_read.py").write_text(ORDERS_READ_TOOL, encoding="utf-8")
    (tools / "orders_write.py").write_text(ORDERS_WRITE_TOOL, encoding="utf-8")
    lint = _ok(cli("lint", cwd=project, timeout=900))  # ruff and the policy check
    assert "All declared API calls are allowed" in _out(lint)
    assert _ok(cli("api", "check", cwd=project)).returncode == 0

    # A denied operation is refused by lint (and by the runtime, below).
    denied_tool = tools / "orders_delete.py"
    denied_tool.write_text(ORDERS_DELETE_TOOL, encoding="utf-8")
    try:
        refused = cli("api", "check", cwd=project)
    finally:
        denied_tool.unlink()
    assert refused.returncode == 1
    assert "denied by denied_operations" in _out(refused)
    assert "graph-agents-cli api revoke orders --method DELETE --path /orders/{order_id}" in _out(
        refused
    )

    extra = project / "tests" / "unit" / "test_api_lifecycle_e2e.py"
    extra.write_text(LIFECYCLE_RUNTIME_TEST, encoding="utf-8")
    try:
        result = subprocess.run(
            [
                "uv",
                "run",
                "pytest",
                "tests/unit",
                "tests/integration",
                "-q",
                "-p",
                "no:cacheprovider",
            ],
            cwd=project,
            env=_env({"MODEL_PROVIDER": "fake"}),
            capture_output=True,
            text=True,
            timeout=900,
            check=False,
        )
    finally:
        extra.unlink()
    assert result.returncode == 0, result.stdout + result.stderr
    assert " passed" in result.stdout
