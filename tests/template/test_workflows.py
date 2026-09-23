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

"""The generated GitHub workflows: structure, and their shell steps run for real.

The steps that carry logic (loading .github/agent.env, choosing the eval
gate's model) are extracted from the workflow files and executed with bash
the way the runner does (``bash --noprofile --norc -eo pipefail``), with small
stand-ins for ``yq`` and ``uvx``; GITHUB_ENV is then read with a port of the
runner's parser.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from tests.template.render import SCAFFOLD

KUBE_WORKFLOWS = SCAFFOLD / "deployment_targets" / "kubernetes" / "python" / ".github" / "workflows"
PR_CHECKS = SCAFFOLD / "base_templates" / "python" / ".github" / "workflows" / "pr_checks.yaml"
STAGING = KUBE_WORKFLOWS / "staging.yaml"
PROMOTE = KUBE_WORKFLOWS / "promote-to-prod.yaml"
ALL_WORKFLOWS = (PR_CHECKS, STAGING, PROMOTE)
BASH = shutil.which("bash")
needs_bash = pytest.mark.skipif(BASH is None, reason="bash is not on PATH")

# Where each workflow loads .github/agent.env.
LOADERS = {
    PR_CHECKS: ("checks", "Load project settings"),
    STAGING: ("build", "Load project settings"),
    PROMOTE: ("settings", "Load project settings"),
}


def _workflow(path: Path) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    # PyYAML reads the bare `on` key as the boolean True.
    data["on"] = data.pop(True, data.get("on"))
    return data


def _step(path: Path, job: str, name: str) -> dict:
    steps = _workflow(path)["jobs"][job]["steps"]
    matches = [s for s in steps if s.get("name") == name]
    assert len(matches) == 1, f"{path.name}: {job} has {len(matches)} steps named {name!r}"
    return matches[0]


def _run_scripts(path: Path) -> list[str]:
    return [
        step["run"]
        for job in _workflow(path)["jobs"].values()
        for step in job.get("steps", [])
        if "run" in step
    ]


def parse_github_env_file(text: str) -> dict[str, str]:
    """Port of the runner's GITHUB_ENV / GITHUB_OUTPUT parser (actions/runner, FileCommandManager).

    Each non-empty line is ``NAME=VALUE`` or opens a ``NAME<<DELIMITER`` heredoc;
    anything else fails the step with ``Invalid format '<line>'``.
    """
    lines = text.replace("\r\n", "\n").split("\n")
    entries: dict[str, str] = {}
    index = 0
    while index < len(lines):
        line = lines[index]
        index += 1
        if line == "":
            continue
        equals, heredoc = line.find("="), line.find("<<")
        if equals >= 0 and (heredoc < 0 or equals < heredoc):
            name, value = line.split("=", 1)
            if not name:
                raise ValueError(f"Invalid format '{line}'. Name must not be empty")
            entries[name] = value
        elif heredoc >= 0 and (equals < 0 or heredoc < equals):
            name, delimiter = line.split("<<", 1)
            body: list[str] = []
            while index < len(lines) and lines[index] != delimiter:
                body.append(lines[index])
                index += 1
            if index >= len(lines):
                raise ValueError(f"Invalid value. Matching delimiter not found '{delimiter}'")
            index += 1
            entries[name] = "\n".join(body)
        else:
            raise ValueError(f"Invalid format '{line}'")
    return entries


def run_step(
    script: str, cwd: Path, env: dict[str, str], shims: Path | None = None
) -> subprocess.CompletedProcess[str]:
    """Run a `run:` script as the runner does (bash -eo pipefail, a file per step)."""
    step_file = cwd.parent / f"{cwd.name}-step.sh"
    step_file.write_text(script, encoding="utf-8")
    path = os.environ.get("PATH", "")
    if shims is not None:
        path = f"{shims}{os.pathsep}{path}"
    return subprocess.run(
        [BASH or "bash", "--noprofile", "--norc", "-eo", "pipefail", str(step_file)],
        cwd=cwd,
        env={"PATH": path, "HOME": str(cwd), **env},
        capture_output=True,
        text=True,
        check=False,
    )


def load_agent_env(project: Path, text: str | None = None) -> subprocess.CompletedProcess[str]:
    """Run the workflows' loader in ``project``; GITHUB_ENV ends up in project/github_env."""
    if text is not None:
        (project / ".github").mkdir(parents=True, exist_ok=True)
        (project / ".github" / "agent.env").write_bytes(text.encode("utf-8"))
    github_env = project / "github_env"
    github_env.write_text("", encoding="utf-8")
    script = _step(PR_CHECKS, *LOADERS[PR_CHECKS])["run"]
    return run_step(script, project, {"GITHUB_ENV": str(github_env)})


# --- .github/agent.env is data -------------------------------------------------


def test_every_workflow_loads_agent_env_with_the_same_loader() -> None:
    scripts = {path.name: _step(path, *where)["run"] for path, where in LOADERS.items()}
    assert len(set(scripts.values())) == 1, "the three agent.env loaders differ"
    loader = scripts["pr_checks.yaml"]
    assert 'done < "$file"' in loader and '>> "$GITHUB_ENV"' in loader
    for path in ALL_WORKFLOWS:
        text = path.read_text(encoding="utf-8")
        # Never shell-evaluated: no `source`, no `.`, no `set -a` around the file.
        assert not re.search(r"(^|[;&|]\s*|\s)(\.|source)\s+\S*agent\.env", text), path.name
        assert "set -a" not in text, path.name
        # The old pin is only named in the loader's rename hint, never read.
        assert "$CLI_VERSION_PIN" not in text and "{CLI_VERSION_PIN" not in text, path.name
        # The file is only ever read by the loader step.
        readers = [s for s in _run_scripts(path) if "agent.env" in s and s != loader]
        for script in readers:
            assert "< " not in script and "cat " not in script and "grep " not in script, (
                f"{path.name} reads agent.env outside the loader:\n{script}"
            )


# Every setting the loader accepts (any other name is refused).
KNOWN_SETTINGS = {
    "IMAGE_REPOSITORY",
    "RELEASE_NAME",
    "CHART_PATH",
    "RUNTIME",
    "CD",
    "GRAPH_AGENTS_CLI_SPEC",
}
SPEC = "GRAPH_AGENTS_CLI_SPEC"

LOADER_CASES = [
    pytest.param(
        f"{SPEC}=graph-agents-cli @ git+https://git.example.com/m/graph-agents-cli@v0.2.0\n",
        {SPEC: "graph-agents-cli @ git+https://git.example.com/m/graph-agents-cli@v0.2.0"},
        id="pep508-spec-with-spaces",
    ),
    pytest.param(
        f"{SPEC}=graph-agents-cli[a2a] @ file:///opt/wheels/graph_agents_cli-0.2.0-py3-none-any.whl\n",
        {
            SPEC: "graph-agents-cli[a2a] @ file:///opt/wheels/graph_agents_cli-0.2.0-py3-none-any.whl"
        },
        id="extras-and-wheel",
    ),
    pytest.param(
        f"{SPEC}=$(touch pwned) `touch pwned2`; touch pwned3 && echo $HOME\n",
        {SPEC: "$(touch pwned) `touch pwned2`; touch pwned3 && echo $HOME"},
        id="shell-metacharacters-stay-text",
    ),
    pytest.param(
        "CD=argocd\r\nRUNTIME=two words\r\n", {"CD": "argocd", "RUNTIME": "two words"}, id="crlf"
    ),
    pytest.param("\ufeffCD=1\nRUNTIME=2\n", {"CD": "1", "RUNTIME": "2"}, id="utf8-bom"),
    pytest.param("CD=1\nRUNTIME=2", {"CD": "1", "RUNTIME": "2"}, id="no-final-newline"),
    pytest.param(
        'CD="x y"\nRUNTIME=\'it "is"\'\n', {"CD": "x y", "RUNTIME": 'it "is"'}, id="quotes"
    ),
    pytest.param(
        'CD=x y  # note\nRUNTIME="q" # c\n', {"CD": "x y", "RUNTIME": "q"}, id="inline-comment"
    ),
    pytest.param(
        f"{SPEC}=git+https://h/r.git#subdirectory=cli\n",
        {SPEC: "git+https://h/r.git#subdirectory=cli"},
        id="url-fragment-kept",
    ),
    pytest.param(
        "  # c\n\t\n  CD = 1  \nRUNTIME=\n", {"CD": "1", "RUNTIME": ""}, id="blanks-and-spacing"
    ),
    pytest.param(
        "CD=x<<EOF\nRUNTIME=2\n", {"CD": "x<<EOF", "RUNTIME": "2"}, id="heredoc-marker-in-value"
    ),
]


@needs_bash
@pytest.mark.parametrize(("text", "expected"), LOADER_CASES)
def test_agent_env_values_are_data(tmp_path: Path, text: str, expected: dict[str, str]) -> None:
    project = tmp_path / "p"
    project.mkdir()
    result = load_agent_env(project, text)
    assert result.returncode == 0, result.stderr
    assert parse_github_env_file((project / "github_env").read_text()) == expected
    assert not list(project.glob("pwned*")), "a value was executed"


@needs_bash
@pytest.mark.parametrize(
    ("text", "message"),
    [
        pytest.param("CD<<EOF\nx\nEOF\n", "expected NAME=VALUE", id="heredoc-line"),
        pytest.param("export CD=1\n", "expected NAME=VALUE", id="shell-syntax"),
        pytest.param("words without equals\n", "expected NAME=VALUE", id="no-equals"),
        pytest.param("1A=x\n", "expected NAME=VALUE", id="bad-name"),
        pytest.param("BASH_FUNC_x%%=() { id; }\n", "expected NAME=VALUE", id="bash-function"),
        pytest.param('CD="x\n', "unterminated", id="unterminated-quote"),
        pytest.param("CD=argocd\nCD=helm-push\n", "CD is set twice", id="duplicate"),
        pytest.param("CD=argo\rcd\n", "control characters", id="carriage-return-inside"),
        pytest.param("CD=argo\x1b[2Jcd\n", "control characters", id="escape-sequence"),
        pytest.param(
            "CLI_VERSION_PIN=0.1.0\n", "replaced by GRAPH_AGENTS_CLI_SPEC", id="legacy-pin"
        ),
        # Not a setting the workflows read: refused whatever it is, so nothing in
        # agent.env can change how a later step runs.
        *(
            pytest.param(f"{name}={value}\n", f"{name} is not a setting", id=name.lower())
            for name, value in (
                ("SHELLOPTS", "xtrace"),
                ("PS4", "$(touch pwned)"),
                ("BASH_ENV", "/tmp/x"),
                ("ENV", "/tmp/x"),
                ("IFS", "/"),
                ("GLOBIGNORE", "*"),
                ("PROMPT_COMMAND", "touch pwned"),
                ("PATH", "/tmp"),
                ("path", "/tmp"),
                ("HOME", "/tmp"),
                ("GITHUB_PATH", "/tmp"),
                ("LD_PRELOAD", "/tmp/x.so"),
                ("NODE_OPTIONS", "--require /tmp/x.js"),
                ("PYTHONPATH", "/tmp"),
                ("PYTHONSTARTUP", "/tmp/x.py"),
                ("UV_INDEX_URL", "https://evil.example/simple"),
                ("KUBECONFIG", "/tmp/kubeconfig"),
                ("GRAPH_AGENTS_CLI_DISABLE_OVERRIDES", "0"),
                ("MODEL_PROVIDER", "openai"),
                ("cd", "argocd"),
            )
        ),
    ],
)
def test_agent_env_refuses_what_is_not_a_known_setting(
    tmp_path: Path, text: str, message: str
) -> None:
    project = tmp_path / "p"
    project.mkdir()
    result = load_agent_env(project, text)
    assert result.returncode != 0
    assert message in result.stderr and "::error file=.github/agent.env" in result.stderr
    # Nothing reaches GITHUB_ENV from a file that is not valid as a whole.
    assert (project / "github_env").read_text() == ""


@needs_bash
def test_agent_env_cannot_run_code_in_later_steps(tmp_path: Path) -> None:
    """SHELLOPTS=xtrace + PS4=$(...) (or BASH_ENV) would run code in every later bash step."""
    project = tmp_path / "p"
    project.mkdir()
    hostile = (
        "CD=argocd\n"  # valid lines first: they must not reach GITHUB_ENV either
        "SHELLOPTS=xtrace\n"
        "PS4=$(touch pwned-ps4)\n"
        f"BASH_ENV={tmp_path / 'evil.sh'}\n"
    )
    (tmp_path / "evil.sh").write_text("touch pwned-bash-env\n")
    result = load_agent_env(project, hostile)
    assert result.returncode != 0 and "SHELLOPTS is not a setting" in result.stderr
    github_env = (project / "github_env").read_text()
    assert github_env == ""
    # The next step, run as the runner runs it with that GITHUB_ENV.
    later = run_step("echo later", project, parse_github_env_file(github_env))
    assert later.returncode == 0, later.stderr
    assert not list(project.glob("pwned*")) and not list(tmp_path.glob("pwned*"))


def test_the_loader_knows_exactly_the_settings_the_templates_write() -> None:
    loader = _step(PR_CHECKS, *LOADERS[PR_CHECKS])["run"]
    known = re.search(r'^known="([^"]*)"$', loader, re.MULTILINE)
    assert known and set(known.group(1).split()) == KNOWN_SETTINGS
    written: set[str] = set()
    for agent_env in (
        SCAFFOLD / "base_templates" / "python" / ".github" / "agent.env",
        SCAFFOLD / "deployment_targets" / "kubernetes" / "python" / ".github" / "agent.env",
    ):
        written |= {
            line.split("=", 1)[0]
            for line in agent_env.read_text().splitlines()
            if line and not line.startswith("#")
        }
    assert written == KNOWN_SETTINGS
    # The settings the CD steps require are all ones the loader accepts.
    required = set()
    for path in (STAGING, PROMOTE):
        for script in _run_scripts(path):
            for names in re.findall(r"^\s*for var in ([A-Z_ ]+); do$", script, re.MULTILINE):
                required |= set(names.split())
    assert required and required <= KNOWN_SETTINGS


@needs_bash
def test_a_missing_agent_env_fails_the_step(tmp_path: Path) -> None:
    result = load_agent_env(tmp_path)
    assert result.returncode != 0 and "is missing" in result.stderr


@needs_bash
def test_every_rendered_agent_env_loads(rendered: dict[str, Path], tmp_path: Path) -> None:
    for name, project in rendered.items():
        work = tmp_path / name
        shutil.copytree(project / ".github", work / ".github")
        result = load_agent_env(work)
        assert result.returncode == 0, f"{name}: {result.stderr}"
        entries = parse_github_env_file((work / "github_env").read_text())
        assert (
            entries["GRAPH_AGENTS_CLI_SPEC"] == "git+https://example.test/graph-agents-cli@v0.1.0"
        )
        if name != "none":
            assert entries["IMAGE_REPOSITORY"] == "ghcr.io/acme/weather-agent", name
            assert entries["CHART_PATH"] == "deployment/helm/weather-agent", name
            assert entries["RELEASE_NAME"] == "weather-agent", name


# --- staging / promote-to-prod -------------------------------------------------


def _github_glob(pattern: str) -> re.Pattern[str]:
    """GitHub's path filter glob: `*` stays within a path segment, `**` crosses them."""
    out = ""
    i = 0
    while i < len(pattern):
        if pattern.startswith("**", i):
            out += ".*"
            i += 2
        elif pattern[i] == "*":
            out += "[^/]*"
            i += 1
        else:
            out += re.escape(pattern[i])
            i += 1
    return re.compile(f"^{out}$")


def test_staging_does_not_retrigger_itself() -> None:
    on = _workflow(STAGING)["on"]
    ignored = on["push"]["paths-ignore"]

    def skipped(changed: list[str]) -> bool:
        return all(any(_github_glob(p).match(path) for p in ignored) for path in changed)

    # The squash-merged staging PR (and a merged prod or dev promotion) change only a
    # values file.
    assert skipped(["deployment/helm/weather-agent/values-staging.yaml"])
    assert skipped(["deployment/helm/weather-agent/values-prod.yaml"])
    assert skipped(["deployment/helm/weather-agent/values-dev.yaml"])
    # Code, chart or shared values changes still build and deploy.
    assert not skipped(["app/agent.py"])
    assert not skipped(["deployment/helm/weather-agent/values-staging.yaml", "app/agent.py"])
    assert not skipped(["deployment/helm/weather-agent/values.yaml"])
    assert not skipped(["deployment/helm/weather-agent/templates/deployment.yaml"])
    # A manual re-deploy is possible, from main only.
    assert "workflow_dispatch" in on
    assert _workflow(STAGING)["jobs"]["build"]["if"] == "github.ref == 'refs/heads/main'"


@pytest.mark.parametrize(
    ("path", "job", "environment"),
    [(STAGING, "deploy_helm_push", "staging"), (PROMOTE, "deploy_helm_push", "production")],
)
def test_helm_push_credentials_are_environment_scoped_and_removed(
    path: Path, job: str, environment: str
) -> None:
    spec = _workflow(path)["jobs"][job]
    assert spec["environment"] == environment
    steps = spec["steps"]
    configure = next(s for s in steps if s.get("name") == "Configure kubeconfig")
    assert configure["env"] == {"DEPLOY_KUBECONFIG": "${{ secrets.DEPLOY_KUBECONFIG }}"}
    script = configure["run"]
    assert "umask 077" in script and '"$RUNNER_TEMP/kubeconfig"' in script
    assert 'echo "KUBECONFIG=$RUNNER_TEMP/kubeconfig" >> "$GITHUB_ENV"' in script
    cleanup = steps[-1]
    assert cleanup["if"] == "always()" and 'rm -f "$RUNNER_TEMP/kubeconfig"' in cleanup["run"]
    text = path.read_text(encoding="utf-8")
    assert "$HOME/.kube" not in text and "secrets.KUBECONFIG" not in text


@needs_bash
@pytest.mark.parametrize(("path", "environment"), [(STAGING, "staging"), (PROMOTE, "production")])
def test_kubeconfig_is_private_to_the_job_and_removed(
    tmp_path: Path, path: Path, environment: str
) -> None:
    steps = _workflow(path)["jobs"]["deploy_helm_push"]["steps"]
    configure = next(s for s in steps if s.get("name") == "Configure kubeconfig")["run"]
    remove = steps[-1]["run"]
    runner_temp = tmp_path / "runner-temp"
    runner_temp.mkdir()
    github_env = tmp_path / "github_env"
    github_env.write_text("")
    env = {"RUNNER_TEMP": str(runner_temp), "GITHUB_ENV": str(github_env)}
    missing = run_step(configure, tmp_path, {**env, "DEPLOY_KUBECONFIG": ""})
    assert missing.returncode != 0
    assert f"DEPLOY_KUBECONFIG secret of the {environment} environment" in missing.stderr
    written = run_step(
        configure, tmp_path, {**env, "DEPLOY_KUBECONFIG": "apiVersion: v1\nkind: Config\n"}
    )
    assert written.returncode == 0, written.stderr
    kubeconfig = runner_temp / "kubeconfig"
    assert kubeconfig.read_text() == "apiVersion: v1\nkind: Config\n\n"
    assert kubeconfig.stat().st_mode & 0o077 == 0
    assert parse_github_env_file(github_env.read_text()) == {"KUBECONFIG": str(kubeconfig)}
    assert run_step(remove, tmp_path, env).returncode == 0
    assert not kubeconfig.exists()


def test_no_secret_or_untrusted_value_is_expanded_inside_a_script() -> None:
    for path in ALL_WORKFLOWS:
        for script in _run_scripts(path):
            for forbidden in ("${{ secrets.", "${{ inputs.", "${{ github.event.", "${{ needs."):
                assert forbidden not in script, f"{path.name}: {forbidden} in:\n{script}"


@pytest.mark.parametrize(
    ("path", "env", "deploy_step"),
    [(STAGING, "staging", "Deploy to staging"), (PROMOTE, "prod", "Deploy to production")],
)
def test_helm_push_verifies_the_rollout(path: Path, env: str, deploy_step: str) -> None:
    names = [s.get("name") for s in _workflow(path)["jobs"]["deploy_helm_push"]["steps"]]
    assert names[-5:] == [
        "Configure kubeconfig",
        "Resolve the kube context",
        deploy_step,
        "Verify the rollout",
        "Remove kubeconfig",
    ]
    # One context for the deploy and every check after it.
    deploy = _step(path, "deploy_helm_push", deploy_step)["run"]
    assert f'deploy --env {env} --image "$IMAGE" --context "$KUBE_CONTEXT" --yes' in deploy
    verify = _step(path, "deploy_helm_push", "Verify the rollout")["run"]
    kubectl_calls = [line for line in verify.splitlines() if re.search(r"^\s*kubectl ", line)]
    assert len(kubectl_calls) == 2
    assert all('kubectl --context "$KUBE_CONTEXT" ' in line for line in kubectl_calls)
    assert 'rollout status "deployment/$RELEASE"' in verify
    assert 'port-forward "service/$RELEASE" :http' in verify
    assert "for path in /health /ready" in verify and "curl --fail" in verify


def test_every_step_that_runs_the_cli_ignores_extensions() -> None:
    """LIFECYCLE-4: a committed or runner-wide extension cannot replace the gate or deploy."""
    seen = 0
    for path in ALL_WORKFLOWS:
        for name, job in _workflow(path)["jobs"].items():
            for step in job.get("steps", []):
                if "graph-agents-cli " not in step.get("run", ""):
                    continue
                seen += 1
                env = {**job.get("env", {}), **step.get("env", {})}
                assert env.get("GRAPH_AGENTS_CLI_DISABLE_OVERRIDES") == "1", (
                    path.name,
                    name,
                    step.get("name"),
                )
    # lint + eval (pr_checks), deploy (staging), open the prod PR + deploy (promote).
    assert seen == 5


def test_the_argocd_prod_promotion_runs_without_a_prompt() -> None:
    step = _step(PROMOTE, "open_prod_pr", "Open the production PR (merge is the gate)")
    assert 'deploy --env prod --image "$IMAGE" --yes' in step["run"]


@needs_bash
@pytest.mark.skipif(shutil.which("curl") is None, reason="curl is not on PATH")
@pytest.mark.parametrize("path", [STAGING, PROMOTE])
@pytest.mark.parametrize("ready_status", [200, 503])
def test_verify_step_checks_health_and_ready_through_a_port_forward(
    tmp_path: Path, path: Path, ready_status: int
) -> None:
    """The real step against a kubectl stand-in whose port-forward points at a local server."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    seen: list[str] = []

    class Agent(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            seen.append(self.path)
            self.send_response(200 if self.path == "/health" else ready_status)
            self.end_headers()
            self.wfile.write(b'{"status": "ok"}')

        def log_message(self, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Agent)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    shims = tmp_path / "shims"
    shims.mkdir()
    calls = tmp_path / "kubectl.calls"
    kubectl = shims / "kubectl"
    kubectl.write_text(
        f"#!{sys.executable}\n"
        "import sys, time\n"
        f"open({str(calls)!r}, 'a').write(' '.join(sys.argv[1:]) + '\\n')\n"
        "if 'port-forward' in sys.argv:\n"
        f"    print('Forwarding from 127.0.0.1:{server.server_address[1]} -> 8000', flush=True)\n"
        "    print('Forwarding from [::1]:1 -> 8000', flush=True)\n"
        "    time.sleep(60)\n"
    )
    sleep = shims / "sleep"
    sleep.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(0.05)\n")
    for shim in (kubectl, sleep):
        shim.chmod(0o755)
    work = tmp_path / "work"
    work.mkdir()
    script = _step(path, "deploy_helm_push", "Verify the rollout")["run"]
    try:
        result = run_step(
            script,
            work,
            {
                "NAMESPACE": "weather-agent-staging",
                "RELEASE": "weather-agent",
                "IMAGE": "ghcr.io/acme/weather-agent:0123abc",
                "KUBE_CONTEXT": "arn:aws:eks:eu-west-1:1:cluster/stg",
                "RUNNER_TEMP": str(tmp_path),
            },
            shims=shims,
        )
    finally:
        server.shutdown()
    recorded = calls.read_text().splitlines()
    context = "--context arn:aws:eks:eu-west-1:1:cluster/stg"
    assert recorded[0] == (
        f"{context} -n weather-agent-staging rollout status deployment/weather-agent --timeout=300s"
    )
    assert (
        recorded[1]
        == f"{context} -n weather-agent-staging port-forward service/weather-agent :http"
    )
    if ready_status == 200:
        assert result.returncode == 0, result.stderr
        assert seen == ["/health", "/ready"]
        assert "answers /health and /ready" in result.stdout
    else:
        assert result.returncode != 0
        assert "/ready did not answer 2xx" in result.stderr
        assert seen.count("/ready") == 10


@needs_bash
def test_verify_step_refuses_missing_targets(tmp_path: Path) -> None:
    script = _step(STAGING, "deploy_helm_push", "Verify the rollout")["run"]
    result = run_step(script, tmp_path, {"NAMESPACE": "", "RELEASE": "weather-agent"})
    assert result.returncode != 0 and "resolved no namespace" in result.stderr
    result = run_step(
        script, tmp_path, {"NAMESPACE": "ns", "RELEASE": "weather-agent", "KUBE_CONTEXT": ""}
    )
    assert result.returncode != 0 and "no kube context was resolved" in result.stderr


def _kubectl_config_shim(root: Path, current: str, contexts: list[str]) -> Path:
    """`kubectl config current-context` / `get-contexts NAME` over a fixed kubeconfig."""
    shims = root / "kube-shims"
    shims.mkdir(exist_ok=True)
    kubectl = shims / "kubectl"
    kubectl.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        f"current, contexts = {current!r}, {contexts!r}\n"
        "args = sys.argv[1:]\n"
        "if args == ['config', 'current-context']:\n"
        "    if not current:\n"
        "        sys.exit('error: current-context is not set')\n"
        "    print(current)\n"
        "elif args[:2] == ['config', 'get-contexts'] and len(args) == 3:\n"
        "    sys.exit(0 if args[2] in contexts else 'error: context not found')\n"
        "else:\n"
        "    sys.exit(f'unexpected kubectl call: {args}')\n",
        encoding="utf-8",
    )
    kubectl.chmod(0o755)
    return shims


@needs_bash
@pytest.mark.parametrize("path", [STAGING, PROMOTE])
@pytest.mark.parametrize(
    ("manifest_context", "current", "expected", "error"),
    [
        # The manifest's environments.<env>.context wins, as it does for `deploy`.
        ("prod-admin@cluster", "kind-dev", "prod-admin@cluster", None),
        # None recorded: the kubeconfig's current context, pinned for every step.
        ("", "arn:aws:eks:eu-west-1:1:cluster/stg", "arn:aws:eks:eu-west-1:1:cluster/stg", None),
        ("", "", None, "No kube context"),
        ("missing-context", "kind-dev", None, "is not in the DEPLOY_KUBECONFIG"),
        ("", "-n kube-system", None, "Invalid kube context name"),
    ],
)
def test_deploy_and_verify_resolve_one_kube_context(
    tmp_path: Path,
    path: Path,
    manifest_context: str,
    current: str,
    expected: str | None,
    error: str | None,
) -> None:
    script = _step(path, "deploy_helm_push", "Resolve the kube context")["run"]
    known = ["prod-admin@cluster", "kind-dev", "arn:aws:eks:eu-west-1:1:cluster/stg"]
    shims = _kubectl_config_shim(tmp_path, current, known)
    github_env = tmp_path / "github_env"
    github_env.write_text("")
    result = run_step(
        script,
        tmp_path,
        {"MANIFEST_CONTEXT": manifest_context, "GITHUB_ENV": str(github_env)},
        shims=shims,
    )
    entries = parse_github_env_file(github_env.read_text())
    if error is not None:
        assert result.returncode != 0 and error in result.stderr, result.stderr
        assert entries == {}
        return
    assert result.returncode == 0, result.stderr
    assert entries == {"KUBE_CONTEXT": expected}
    job = _workflow(path)["jobs"]["deploy_helm_push"]
    needs = job["needs"]
    assert job["env"]["MANIFEST_CONTEXT"] == f"${{{{ needs.{needs}.outputs.kube_context }}}}"


SHA = "0123abc4567def89012345678901234567890abc"


def _resolve(
    tmp_path: Path,
    project: Path,
    path: Path,
    *,
    agent_env: str | None = None,
    manifest_edit: dict | None = None,
    staging_tag: str | None = None,
    env: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], dict[str, str]]:
    """Run a workflow's settings job (load agent.env, then the step with id `settings`)."""
    work = tmp_path / "work"
    shutil.copytree(project / ".github", work / ".github")
    shutil.copytree(project / "deployment" / "helm", work / "deployment" / "helm")
    manifest = yaml.safe_load((project / "graph-agents-cli-manifest.yaml").read_text())
    if manifest_edit:
        manifest["environments"].update(manifest_edit)
    (work / "graph-agents-cli-manifest.yaml").write_text(yaml.safe_dump(manifest))
    if agent_env is not None:
        (work / ".github" / "agent.env").write_text(agent_env)
    if staging_tag is not None:
        values = work / "deployment" / "helm" / "weather-agent" / "values-staging.yaml"
        values.write_text(values.read_text().replace('tag: ""', f'tag: "{staging_tag}"'))
    loaded = load_agent_env(work)
    assert loaded.returncode == 0, loaded.stderr
    job = "build" if path == STAGING else "settings"
    step = next(s for s in _workflow(path)["jobs"][job]["steps"] if s.get("id") == "settings")
    outputs = tmp_path / "github_output"
    outputs.write_text("")
    step_env = {
        **parse_github_env_file((work / "github_env").read_text()),
        "GITHUB_SHA": SHA,
        "GITHUB_OUTPUT": str(outputs),
        "GITHUB_ENV": str(tmp_path / "github_env_2"),
        **{k: "" for k in step.get("env", {})},
        **(env or {}),
    }
    result = run_step(step["run"], work, step_env, shims=_shims(tmp_path))
    return result, parse_github_env_file(outputs.read_text()) if result.returncode == 0 else {}


@needs_bash
def test_staging_resolves_what_the_deploy_jobs_need(
    rendered: dict[str, Path], tmp_path: Path
) -> None:
    spec = "graph-agents-cli @ git+https://git.example.com/m/graph-agents-cli@v0.2.0"
    project = rendered["server-helm-push"]
    agent_env = (
        (project / ".github" / "agent.env")
        .read_text()
        .replace("git+https://example.test/graph-agents-cli@v0.1.0", spec)
    )
    result, outputs = _resolve(tmp_path, project, STAGING, agent_env=agent_env)
    assert result.returncode == 0, result.stderr
    assert outputs == {
        "cd": "helm-push",
        "tag": "0123abc",
        "image": "ghcr.io/acme/weather-agent:0123abc",
        "chart_path": "deployment/helm/weather-agent",
        "release": "weather-agent",
        "namespace": "weather-agent-staging",
        "kube_context": "",
        "graph_agents_cli_spec": spec,
    }


@needs_bash
@pytest.mark.parametrize(("path", "env"), [(STAGING, "staging"), (PROMOTE, "prod")])
def test_the_recorded_kube_context_reaches_the_deploy_job(
    rendered: dict[str, Path], tmp_path: Path, path: Path, env: str
) -> None:
    project = rendered["server-helm-push"]
    edit = {env: {"namespace": f"weather-agent-{env}", "context": "admin@prod-cluster"}}
    result, outputs = _resolve(
        tmp_path, project, path, manifest_edit=edit, staging_tag="abc1234", env={"IMAGE_TAG": ""}
    )
    assert result.returncode == 0, result.stderr
    assert outputs["kube_context"] == "admin@prod-cluster"
    assert (
        _workflow(path)["jobs"]["build" if path == STAGING else "settings"]["outputs"][
            "kube_context"
        ]
        == "${{ steps.settings.outputs.kube_context }}"
    )


@needs_bash
@pytest.mark.parametrize(
    ("change", "message"),
    [
        (
            {"manifest_edit": {"staging": {"namespace": "Bad_NS; rm -rf /"}}},
            "Invalid staging namespace",
        ),
        (
            {"manifest_edit": {"staging": {"context": "ctx\nnamespace=evil"}}},
            "Invalid staging kube context",
        ),
        (
            {"manifest_edit": {"staging": {"context": "--kubeconfig=/tmp/x"}}},
            "Invalid staging kube context",
        ),
        ({"agent_env": "RELEASE_NAME=x\n"}, "IMAGE_REPOSITORY is not set"),
        ({"agent_env_replace": ("CD=argocd", "CD=argo")}, "CD must be argocd or helm-push"),
        (
            {"agent_env_replace": ("RELEASE_NAME=weather-agent", "RELEASE_NAME=a b")},
            "Invalid RELEASE_NAME",
        ),
        # The scaffold's registry placeholder, and a reference docker would refuse.
        (
            {"agent_env_replace": ("ghcr.io/acme/", "ghcr.io/CHANGE-ME/")},
            "IMAGE_REPOSITORY must be",
        ),
        (
            {"agent_env_replace": ("ghcr.io/acme/weather-agent", "ghcr.io/Acme/Weather")},
            "IMAGE_REPOSITORY must be",
        ),
    ],
)
def test_staging_refuses_settings_it_cannot_use(
    rendered: dict[str, Path], tmp_path: Path, change: dict, message: str
) -> None:
    project = rendered["fastapi-argocd"]
    kwargs = dict(change)
    if "agent_env_replace" in kwargs:
        old, new = kwargs.pop("agent_env_replace")
        kwargs["agent_env"] = (project / ".github" / "agent.env").read_text().replace(old, new)
    result, _ = _resolve(tmp_path, project, STAGING, **kwargs)
    assert result.returncode != 0 and message in result.stderr, result.stderr


@needs_bash
@pytest.mark.parametrize(
    ("image_tag", "staging_tag", "expected"),
    [
        ("", "abc1234", "abc1234"),
        ("fedcba9", None, "fedcba9"),
        ("", None, "No promotable tag"),  # values-staging.yaml still has no tag
        ("latest", "abc1234", "must be a short commit SHA"),
        ("abc1234; rm -rf /", "abc1234", "must be a short commit SHA"),
    ],
)
def test_promote_resolves_the_tag_to_promote(
    rendered: dict[str, Path],
    tmp_path: Path,
    image_tag: str,
    staging_tag: str | None,
    expected: str,
) -> None:
    result, outputs = _resolve(
        tmp_path,
        rendered["fastapi-argocd"],
        PROMOTE,
        staging_tag=staging_tag,
        env={"IMAGE_TAG": image_tag},
    )
    if result.returncode != 0:
        assert expected in result.stderr, result.stderr
        return
    assert outputs["tag"] == expected
    assert outputs["image"] == f"ghcr.io/acme/weather-agent:{expected}"
    assert outputs["namespace"] == "weather-agent-prod" and outputs["cd"] == "argocd"


# --- argocd staging: the desired-state PR -----------------------------------------

GIT = shutil.which("git")
needs_git = pytest.mark.skipif(GIT is None or BASH is None, reason="git and bash are needed")
LAND_STEP = "Open the staging PR with auto-merge"
_GIT_ENV = {
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_AUTHOR_NAME": "dev",
    "GIT_AUTHOR_EMAIL": "dev@example.com",
    "GIT_COMMITTER_NAME": "dev",
    "GIT_COMMITTER_EMAIL": "dev@example.com",
}


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        [GIT or "git", *args],
        cwd=cwd,
        env={"PATH": os.environ.get("PATH", ""), "HOME": str(cwd), **_GIT_ENV},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"git {' '.join(args)}: {result.stderr}"
    return result.stdout.strip()


def _gh_shim(shims: Path) -> None:
    """`gh pr list/create/close/merge/view` over a JSON file of pull requests (GH_STATE)."""
    gh = shims / "gh"
    gh.write_text(
        f"#!{sys.executable}\n"
        "import json, os, subprocess, sys\n"
        "state_file = os.environ['GH_STATE']\n"
        "state = json.load(open(state_file))\n"
        "args = sys.argv[1:]\n"
        "def opt(name):\n"
        "    return args[args.index(name) + 1] if name in args else None\n"
        "def save():\n"
        "    json.dump(state, open(state_file, 'w'))\n"
        "state['calls'].append(args)\n"
        "save()\n"
        "prs = state['prs']\n"
        "if args[:2] == ['pr', 'list']:\n"
        "    open_prs = [p for p in prs if p['state'] == 'open']\n"
        "    if opt('--head'):\n"
        "        print('\\n'.join(str(p['number']) for p in open_prs if p['head'] == opt('--head')))\n"
        "    else:\n"
        "        print('\\n'.join(f\"{p['number']} {p['head']}\" for p in open_prs))\n"
        "elif args[:2] == ['pr', 'create']:\n"
        "    prs.append({'number': len(prs) + 1, 'head': opt('--head'), 'state': 'open',\n"
        "                'auto': False})\n"
        "    save()\n"
        "elif args[:2] == ['pr', 'close']:\n"
        "    pr = next(p for p in prs if p['number'] == int(args[2]))\n"
        "    pr['state'] = 'closed'\n"
        "    save()\n"
        "    if '--delete-branch' in args:\n"
        "        subprocess.run(['git', '--git-dir', os.environ['GH_ORIGIN'], 'branch', '-D',\n"
        "                        pr['head']], check=True, capture_output=True)\n"
        "elif args[:2] == ['pr', 'merge']:\n"
        "    for p in prs:\n"
        "        if p['head'] == args[-1] and p['state'] == 'open':\n"
        "            p['auto'] = True\n"
        "    save()\n"
        "elif args[:2] == ['pr', 'view']:\n"
        "    print('')\n"
        "else:\n"
        "    sys.exit(f'unexpected gh call: {args}')\n",
        encoding="utf-8",
    )
    gh.chmod(0o755)


class StagingRepo:
    """A GitHub repository in miniature: a bare origin, pushes to main, the land step's runs."""

    def __init__(self, root: Path, project: Path) -> None:
        self.root = root
        self.origin = root / "origin.git"
        self.values = "deployment/helm/weather-agent/values-staging.yaml"
        self.state = root / "gh.json"
        self.state.write_text(json.dumps({"prs": [], "calls": []}))
        self.shims = _shims(root)
        _gh_shim(self.shims)
        self.dev = root / "dev"
        _git(root, "init", "--quiet", "--bare", "-b", "main", str(self.origin))
        _git(root, "clone", "--quiet", str(self.origin), str(self.dev))
        (self.dev / self.values).parent.mkdir(parents=True)
        shutil.copy(project / self.values, self.dev / self.values)
        _git(self.dev, "checkout", "--quiet", "-b", "main")
        self.push("initial")
        self.runs = 0

    def push(self, message: str) -> str:
        """A push to main (a code change): returns its sha."""
        (self.dev / "app.txt").write_text(message)
        _git(self.dev, "add", "-A")
        _git(self.dev, "commit", "--quiet", "-m", message)
        _git(self.dev, "push", "--quiet", "origin", "HEAD:main")
        return _git(self.dev, "rev-parse", "HEAD")

    def land(self, sha: str) -> subprocess.CompletedProcess[str]:
        """The land step as a staging run for `sha` runs it (full clone, detached at sha)."""
        self.runs += 1
        work = self.root / f"run{self.runs}"
        _git(self.root, "clone", "--quiet", str(self.origin), str(work))
        _git(work, "checkout", "--quiet", "--detach", sha)
        tag = sha[:7]
        return run_step(
            _step(STAGING, "land_argocd", LAND_STEP)["run"],
            work,
            {
                **_GIT_ENV,
                "GITHUB_SHA": sha,
                "TAG": tag,
                "IMAGE": f"ghcr.io/acme/weather-agent:{tag}",
                "CHART_PATH": "deployment/helm/weather-agent",
                "GH_STATE": str(self.state),
                "GH_ORIGIN": str(self.origin),
            },
            shims=self.shims,
        )

    def prs(self) -> list[dict]:
        return json.loads(self.state.read_text())["prs"]

    def merge(self, number: int) -> bool:
        """Squash-merge a PR into main, as auto-merge would; False on a conflict."""
        pr = next(p for p in self.prs() if p["number"] == number)
        assert pr["state"] == "open", pr
        _git(self.dev, "fetch", "--quiet", "origin")
        _git(self.dev, "reset", "--quiet", "--hard", "origin/main")
        merged = subprocess.run(
            [GIT or "git", "merge", "--squash", f"origin/{pr['head']}"],
            cwd=self.dev,
            env={"PATH": os.environ.get("PATH", ""), "HOME": str(self.dev), **_GIT_ENV},
            capture_output=True,
            text=True,
            check=False,
        )
        if merged.returncode != 0:
            _git(self.dev, "reset", "--quiet", "--hard", "origin/main")
            return False
        _git(self.dev, "commit", "--quiet", "-m", f"Merge #{number}")
        _git(self.dev, "push", "--quiet", "origin", "HEAD:main")
        state = json.loads(self.state.read_text())
        next(p for p in state["prs"] if p["number"] == number)["state"] = "merged"
        self.state.write_text(json.dumps(state))
        return True

    def main_tag(self) -> str:
        _git(self.dev, "fetch", "--quiet", "origin")
        text = _git(self.dev, "show", f"origin/main:{self.values}")
        return str(yaml.safe_load(text)["image"]["tag"])

    def branches(self) -> set[str]:
        out = _git(self.root, "--git-dir", str(self.origin), "branch", "--format=%(refname:short)")
        return set(out.split())


@pytest.fixture
def staging_repo(rendered: dict[str, Path], tmp_path: Path) -> StagingRepo:
    return StagingRepo(tmp_path, rendered["fastapi-argocd"])


@needs_git
def test_two_quick_pushes_land_the_newest_build(staging_repo: StagingRepo) -> None:
    """Run B starts before run A's PR merged: A is superseded, B merges cleanly."""
    repo = staging_repo
    a, b = repo.push("change a"), repo.push("change b")
    first = repo.land(a)
    assert first.returncode == 0, first.stderr
    second = repo.land(b)
    assert second.returncode == 0, second.stderr
    prs = {p["head"]: p for p in repo.prs()}
    assert prs[f"deploy/staging/{a[:7]}"]["state"] == "closed"
    assert prs[f"deploy/staging/{b[:7]}"]["state"] == "open"
    assert prs[f"deploy/staging/{b[:7]}"]["auto"] is True
    assert f"deploy/staging/{a[:7]}" not in repo.branches()
    assert repo.merge(prs[f"deploy/staging/{b[:7]}"]["number"])
    assert repo.main_tag() == b[:7]
    # The tag is written as a quoted string.
    text = _git(repo.dev, "show", f"origin/main:{repo.values}")
    assert f'tag: "{b[:7]}"' in text


@needs_git
def test_a_build_after_a_merged_one_is_based_on_the_latest_main(
    staging_repo: StagingRepo,
) -> None:
    repo = staging_repo
    a, b = repo.push("change a"), repo.push("change b")
    assert repo.land(a).returncode == 0
    assert repo.merge(1)  # A lands while B's run is still building
    assert repo.main_tag() == a[:7]
    result = repo.land(b)
    assert result.returncode == 0, result.stderr
    pr_b = next(p for p in repo.prs() if p["head"] == f"deploy/staging/{b[:7]}")
    assert repo.merge(pr_b["number"]), "the second staging PR conflicts with main"
    assert repo.main_tag() == b[:7]


@needs_git
def test_a_re_run_of_an_older_build_never_moves_staging_back(staging_repo: StagingRepo) -> None:
    repo = staging_repo
    a, b = repo.push("change a"), repo.push("change b")
    assert repo.land(b).returncode == 0
    # B's PR is still open: an older run has nothing to do and leaves it alone.
    rerun = repo.land(a)
    assert rerun.returncode == 0, rerun.stderr
    assert "A newer staging build is pending" in rerun.stdout
    assert [p["head"] for p in repo.prs()] == [f"deploy/staging/{b[:7]}"]
    assert repo.prs()[0]["state"] == "open"
    # Once B merged, an older run (or B's own re-run) changes nothing either.
    assert repo.merge(1)
    again = repo.land(a)
    assert again.returncode == 0 and "main already deploys a newer build" in again.stdout
    same = repo.land(b)
    assert same.returncode == 0 and "already deploys" in same.stdout
    assert len(repo.prs()) == 1 and repo.main_tag() == b[:7]


@needs_git
def test_a_re_run_of_the_same_build_updates_its_own_pr(staging_repo: StagingRepo) -> None:
    repo = staging_repo
    a = repo.push("change a")
    assert repo.land(a).returncode == 0
    rerun = repo.land(a)
    assert rerun.returncode == 0, rerun.stderr
    assert [(p["head"], p["state"]) for p in repo.prs()] == [(f"deploy/staging/{a[:7]}", "open")]
    assert repo.merge(1) and repo.main_tag() == a[:7]


@needs_git
def test_a_staging_pr_it_cannot_order_is_left_alone(staging_repo: StagingRepo) -> None:
    """A workstation `deploy --env staging` from a dirty tree: its tag is not a commit."""
    repo = staging_repo
    state = json.loads(repo.state.read_text())
    state["prs"].append(
        {
            "number": 1,
            "head": "deploy/staging/abc1234-dirty-20260923",
            "state": "open",
            "auto": False,
        }
    )
    repo.state.write_text(json.dumps(state))
    a = repo.push("change a")
    assert repo.land(a).returncode == 0
    assert [p["state"] for p in repo.prs()] == ["open", "open"]


def test_pr_checks_runs_the_gate_with_the_cli_s_own_commands() -> None:
    job = _workflow(PR_CHECKS)["jobs"]["checks"]
    assert job["env"]["MODEL_PROVIDER"] == "fake" and job["env"]["MODEL_NAME"] == "fake"
    # Provider keys reach only the eval gate step, not the tests.
    assert not any("secrets." in str(v) for v in job["env"].values())
    # The whole job runs without extension overrides; no step turns them back on.
    assert job["env"]["GRAPH_AGENTS_CLI_DISABLE_OVERRIDES"] == "1"
    for step in job["steps"]:
        assert "GRAPH_AGENTS_CLI_DISABLE_OVERRIDES" not in step.get("env", {}), step.get("name")
    tests = _step(PR_CHECKS, "checks", "Unit and integration tests")
    assert "env" not in tests


def _shims(root: Path) -> Path:
    """`yq` and `uvx` stand-ins; uvx records what it was run with.

    yq: `-r '<path> // ""' <file>` reads a value; `-i '.image.tag = strenv(TAG) ...'
    <file>` writes image.tag, double-quoted, and leaves every other line alone.
    """
    shims = root / "shims"
    shims.mkdir(exist_ok=True)
    yq = shims / "yq"
    yq.write_text(
        f"#!{sys.executable}\n"
        "import os, re, sys, yaml\n"
        "if '-i' in sys.argv:\n"
        "    expr, path = [a for a in sys.argv[1:] if a != '-i']\n"
        "    assert expr.startswith('.image.tag = strenv(TAG)'), expr\n"
        "    lines = open(path).read().split('\\n')\n"
        "    image = False\n"
        "    for i, line in enumerate(lines):\n"
        "        if re.match(r'^\\S', line):\n"
        "            image = line.startswith('image:')\n"
        "        elif image and re.match(r'^\\s+tag:', line):\n"
        "            indent = line[: len(line) - len(line.lstrip())]\n"
        "            lines[i] = indent + 'tag: \"' + os.environ['TAG'] + '\"'\n"
        "            break\n"
        "    else:\n"
        "        sys.exit('no image.tag')\n"
        "    open(path, 'w').write('\\n'.join(lines))\n"
        "    sys.exit(0)\n"
        "args = [a for a in sys.argv[1:] if a != '-r']\n"
        "expr, path = args[0], args[1]\n"
        "keys = expr.split(' // ')[0].strip().lstrip('.').split('.')\n"
        "try:\n"
        "    node = yaml.safe_load(open(path))\n"
        "except FileNotFoundError:\n"
        "    sys.exit(1)\n"
        "for key in keys:\n"
        "    node = node.get(key) if isinstance(node, dict) else None\n"
        "print('' if node is None else node)\n",
        encoding="utf-8",
    )
    uvx = shims / "uvx"
    uvx.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "keep = ('MODEL_PROVIDER', 'MODEL_NAME', 'JUDGE_MODEL_PROVIDER', "
        "'GRAPH_AGENTS_CLI_DISABLE_OVERRIDES')\n"
        "record = {'argv': sys.argv[1:], 'env': {k: os.environ.get(k) for k in keep}}\n"
        "open(os.environ['UVX_RECORD'], 'w').write(json.dumps(record))\n",
        encoding="utf-8",
    )
    for shim in (yq, uvx):
        shim.chmod(0o755)
    return shims


def _eval_project(root: Path, *, dataset: bool = True, judge: str | None = None) -> Path:
    project = root / "project"
    (project / "tests" / "eval" / "datasets").mkdir(parents=True)
    if dataset:
        (project / "tests" / "eval" / "datasets" / "smoke.json").write_text("[]")
    (project / "tests" / "eval" / "eval_config.yaml").write_text(
        yaml.safe_dump({"judge": {"provider": judge, "model": None}})
    )
    (project / "graph-agents-cli-manifest.yaml").write_text(
        yaml.safe_dump(
            {"name": "p", "create_params": {"model_provider": "openai", "model": "gpt-5-mini"}}
        )
    )
    return project


def _run_eval_gate(
    root: Path, project: Path, env: dict[str, str]
) -> tuple[subprocess.CompletedProcess[str], dict | None, str]:
    step = _step(PR_CHECKS, "checks", "Eval gate")
    record = root / "uvx.json"
    summary = root / "summary.md"
    base = {
        # The job env plus what the step's env maps from (empty) vars and secrets.
        "MODEL_PROVIDER": "fake",
        "MODEL_NAME": "fake",
        "GRAPH_AGENTS_CLI_SPEC": "git+https://example.test/graph-agents-cli@v0.2.0",
        "GRAPH_AGENTS_CLI_DISABLE_OVERRIDES": "1",
        "GITHUB_STEP_SUMMARY": str(summary),
        "UVX_RECORD": str(record),
        **{k: "" for k in step["env"] if k != "GRAPH_AGENTS_CLI_DISABLE_OVERRIDES"},
    }
    result = run_step(step["run"], project, {**base, **env}, shims=_shims(root))
    recorded = json.loads(record.read_text()) if record.exists() else None
    return result, recorded, summary.read_text() if summary.exists() else ""


@needs_bash
def test_eval_gate_warns_when_it_runs_on_the_fake_model(tmp_path: Path) -> None:
    result, recorded, summary = _run_eval_gate(tmp_path, _eval_project(tmp_path), {})
    assert result.returncode == 0, result.stderr
    assert recorded["env"]["MODEL_PROVIDER"] == "fake"
    assert recorded["argv"][:3] == [
        "--from",
        "git+https://example.test/graph-agents-cli@v0.2.0",
        "graph-agents-cli",
    ]
    assert recorded["argv"][3:] == ["eval", "run"]
    assert "::warning title=Eval gate is not a quality signal::" in result.stdout
    assert "OPENAI_API_KEY" in result.stdout and "not a quality signal" in summary


@needs_bash
def test_eval_gate_uses_the_project_provider_when_its_key_exists(tmp_path: Path) -> None:
    result, recorded, summary = _run_eval_gate(
        tmp_path, _eval_project(tmp_path), {"OPENAI_API_KEY": "sk-test"}
    )
    assert result.returncode == 0, result.stderr
    assert recorded["env"]["MODEL_PROVIDER"] == "openai"
    assert recorded["env"]["MODEL_NAME"] == "gpt-5-mini"
    assert recorded["env"]["GRAPH_AGENTS_CLI_DISABLE_OVERRIDES"] == "1"
    assert "::warning" not in result.stdout and summary == ""


@needs_bash
def test_eval_gate_warns_when_the_judge_is_fake(tmp_path: Path) -> None:
    project = _eval_project(tmp_path, judge="fake")
    result, recorded, _ = _run_eval_gate(tmp_path, project, {"OPENAI_API_KEY": "sk-test"})
    assert result.returncode == 0, result.stderr
    assert recorded["env"]["MODEL_PROVIDER"] == "openai"
    assert "judge: fake" in result.stdout and "::warning" in result.stdout


@needs_bash
@pytest.mark.parametrize(
    ("variables", "expected"),
    [
        # Named by the repository variables: used as is, even without a key secret.
        (
            {"REPO_MODEL_PROVIDER": "anthropic", "REPO_MODEL_NAME": "claude-test"},
            ("anthropic", "claude-test"),
        ),
        # The project's provider named explicitly keeps the project's model.
        ({"REPO_MODEL_PROVIDER": "openai"}, ("openai", "gpt-5-mini")),
        # Another provider than the project's needs its model named: never the project's model.
        ({"REPO_MODEL_PROVIDER": "anthropic"}, None),
    ],
)
def test_eval_gate_follows_the_repository_variables(
    tmp_path: Path, variables: dict[str, str], expected: tuple[str, str] | None
) -> None:
    result, recorded, _ = _run_eval_gate(tmp_path, _eval_project(tmp_path), variables)
    if expected is None:
        assert result.returncode != 0 and recorded is None
        assert "Set the MODEL_NAME repository variable" in result.stderr
        return
    assert result.returncode == 0, result.stderr
    assert (recorded["env"]["MODEL_PROVIDER"], recorded["env"]["MODEL_NAME"]) == expected
    assert "::warning" not in result.stdout


@needs_bash
def test_eval_gate_skips_without_a_dataset(tmp_path: Path) -> None:
    result, recorded, _ = _run_eval_gate(tmp_path, _eval_project(tmp_path, dataset=False), {})
    assert result.returncode == 0 and recorded is None
    assert "skipping the eval gate" in result.stdout


# --- CODEOWNERS ----------------------------------------------------------------


def _owners(codeowners: str, path: str) -> list[str]:
    """Owners of `path` by GitHub's rules: the last matching pattern wins."""
    owners: list[str] = []
    for line in codeowners.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        pattern, *who = line.split()
        anchored = pattern.startswith("/")
        body = pattern.lstrip("/")
        if body.endswith("/"):
            regex = re.escape(body) + ".*"
        else:
            regex = _github_glob(body).pattern[1:-1] + "(/.*)?"
        prefix = "^" if anchored else "^(.*/)?"
        if re.match(prefix + regex + "$", path):
            owners = who
    return owners


GATE_INPUTS = (
    ".github/workflows/pr_checks.yaml",
    ".github/agent.env",
    "api-policy.yaml",
    "tests/eval/datasets/smoke.json",
    "tests/eval/eval_config.yaml",
    "graph-agents-cli-extensions.yaml",
    "extensions/team-lint/extension.yaml",
    ".graph-agents-cli/state.json",
    "graph-agents-cli-manifest.yaml",
)
DEPLOYMENT_INPUTS = (
    "deployment/helm/weather-agent/values.yaml",
    "deployment/helm/weather-agent/values-prod.yaml",
    "deployment/helm/weather-agent/Chart.yaml",
    "deployment/helm/weather-agent/templates/deployment.yaml",
)
APPROVERS = ["@CHANGE-ME/production-approvers"]


def test_codeowners_covers_everything_that_shapes_production_or_the_gate(
    rendered: dict[str, Path],
) -> None:
    for name in ("fastapi-argocd", "server-helm-push", "compat-custom"):
        assert not (rendered[name] / "CODEOWNERS").exists(), name
        codeowners = (rendered[name] / ".github" / "CODEOWNERS").read_text()
        for path in (
            *GATE_INPUTS,
            *DEPLOYMENT_INPUTS,
            "deployment/argocd/application-prod.yaml",
            ".github/workflows/promote-to-prod.yaml",
            ".github/CODEOWNERS",
        ):
            assert _owners(codeowners, path) == APPROVERS, (name, path)
        # Application code is reviewed as usual (no production approver needed), and so
        # are the dev and staging values: the staging image-tag PR auto-merges.
        assert _owners(codeowners, "app/agent.py") == []
        assert _owners(codeowners, "tests/unit/test_agent.py") == []
        assert _owners(codeowners, "deployment/helm/weather-agent/values-staging.yaml") == []
        assert _owners(codeowners, "deployment/helm/weather-agent/values-dev.yaml") == []


def test_projects_without_a_cd_mode_own_the_gate_inputs_too(rendered: dict[str, Path]) -> None:
    """No CD workflows (cd skip, or -d none): the same rules, at the repository root.

    The engine keeps .github/CODEOWNERS only next to the CD workflows; GitHub reads
    the root CODEOWNERS when .github/ has none.
    """
    cd_rules = [
        line
        for line in (rendered["fastapi-argocd"] / ".github" / "CODEOWNERS").read_text().splitlines()
        if line and not line.startswith("#")
    ]
    for name in ("none", "fastapi-skip", "jwt", "custom-dir"):
        project = rendered[name]
        assert not (project / ".github" / "CODEOWNERS").exists(), name
        codeowners = (project / "CODEOWNERS").read_text()
        assert "cookiecutter" not in codeowners and "{%" not in codeowners
        for path in (*GATE_INPUTS, "CODEOWNERS", ".github/workflows/staging.yaml"):
            assert _owners(codeowners, path) == APPROVERS, (name, path)
        assert _owners(codeowners, "app/agent.py") == []
        rules = [line for line in codeowners.splitlines() if line and not line.startswith("#")]
        if name == "none":
            assert not any(rule.startswith("/deployment/") for rule in rules)
        else:
            for path in DEPLOYMENT_INPUTS:
                assert _owners(codeowners, path) == APPROVERS, (name, path)
            # The kubernetes rules are the CD projects' ones, plus the file itself.
            assert rules == [*cd_rules[:4], f"/CODEOWNERS {APPROVERS[0]}", *cd_rules[4:]], rules
