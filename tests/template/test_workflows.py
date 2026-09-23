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
        assert "CLI_VERSION_PIN" not in text, path.name
        # The file is only ever read by the loader step.
        readers = [s for s in _run_scripts(path) if "agent.env" in s and s != loader]
        for script in readers:
            assert "< " not in script and "cat " not in script and "grep " not in script, (
                f"{path.name} reads agent.env outside the loader:\n{script}"
            )


LOADER_CASES = [
    pytest.param(
        "GRAPH_AGENTS_CLI_SPEC=graph-agents-cli @ git+https://git.example.com/m/graph-agents-cli@v0.2.0\n",
        {
            "GRAPH_AGENTS_CLI_SPEC": "graph-agents-cli @ git+https://git.example.com/m/graph-agents-cli@v0.2.0"
        },
        id="pep508-spec-with-spaces",
    ),
    pytest.param(
        "S=graph-agents-cli[a2a] @ file:///opt/wheels/graph_agents_cli-0.2.0-py3-none-any.whl\n",
        {"S": "graph-agents-cli[a2a] @ file:///opt/wheels/graph_agents_cli-0.2.0-py3-none-any.whl"},
        id="extras-and-wheel",
    ),
    pytest.param(
        "S=$(touch pwned) `touch pwned2`; touch pwned3 && echo $HOME\n",
        {"S": "$(touch pwned) `touch pwned2`; touch pwned3 && echo $HOME"},
        id="shell-metacharacters-stay-text",
    ),
    pytest.param("A=1\r\nB=two words\r\n", {"A": "1", "B": "two words"}, id="crlf"),
    pytest.param("\ufeffA=1\nB=2\n", {"A": "1", "B": "2"}, id="utf8-bom"),
    pytest.param("A=1\nB=2", {"A": "1", "B": "2"}, id="no-final-newline"),
    pytest.param('A="x y"\nB=\'it "is"\'\n', {"A": "x y", "B": 'it "is"'}, id="quotes"),
    pytest.param('A=x y  # note\nB="q" # c\n', {"A": "x y", "B": "q"}, id="inline-comment"),
    pytest.param(
        "A=git+https://h/r.git#subdirectory=cli\n",
        {"A": "git+https://h/r.git#subdirectory=cli"},
        id="url-fragment-kept",
    ),
    pytest.param("  # c\n\t\n  A = 1  \nB=\n", {"A": "1", "B": ""}, id="blanks-and-spacing"),
    pytest.param("A=x<<EOF\nB=2\n", {"A": "x<<EOF", "B": "2"}, id="heredoc-marker-in-value"),
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
        pytest.param("A<<EOF\nx\nEOF\n", "expected NAME=VALUE", id="heredoc-line"),
        pytest.param("export A=1\n", "expected NAME=VALUE", id="shell-syntax"),
        pytest.param("words without equals\n", "expected NAME=VALUE", id="no-equals"),
        pytest.param("1A=x\n", "expected NAME=VALUE", id="bad-name"),
        pytest.param("GITHUB_PATH=/tmp\n", "cannot be set", id="github-var"),
        pytest.param("PATH=/tmp\n", "cannot be set", id="path"),
        pytest.param("BASH_ENV=/tmp/x\n", "cannot be set", id="bash-env"),
        pytest.param("LD_PRELOAD=/tmp/x.so\n", "cannot be set", id="ld-preload"),
        pytest.param('A="x\n', "unterminated", id="unterminated-quote"),
    ],
)
def test_agent_env_refuses_what_is_not_name_value(tmp_path: Path, text: str, message: str) -> None:
    project = tmp_path / "p"
    project.mkdir()
    result = load_agent_env(project, text)
    assert result.returncode != 0
    assert message in result.stderr and "::error file=.github/agent.env" in result.stderr


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

    # The squash-merged staging PR (and a merged prod promotion) change only a values file.
    assert skipped(["deployment/helm/weather-agent/values-staging.yaml"])
    assert skipped(["deployment/helm/weather-agent/values-prod.yaml"])
    # Code, chart or shared values changes still build and deploy.
    assert not skipped(["app/agent.py"])
    assert not skipped(["deployment/helm/weather-agent/values-staging.yaml", "app/agent.py"])
    assert not skipped(["deployment/helm/weather-agent/values.yaml"])
    assert not skipped(["deployment/helm/weather-agent/templates/deployment.yaml"])
    # A manual re-deploy is possible, from main only.
    assert "workflow_dispatch" in on
    assert _workflow(STAGING)["jobs"]["build"]["if"] == "github.ref == 'refs/heads/main'"
    land = _step(STAGING, "land_argocd", "Open the staging PR with auto-merge")["run"]
    assert land.index("git diff --quiet") < land.index("git commit")


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
    assert names[-3:] == [deploy_step, "Verify the rollout", "Remove kubeconfig"]
    deploy = _step(path, "deploy_helm_push", deploy_step)["run"]
    assert f'deploy --env {env} --image "$IMAGE" --yes' in deploy
    verify = _step(path, "deploy_helm_push", "Verify the rollout")["run"]
    assert 'rollout status "deployment/$RELEASE"' in verify
    assert 'port-forward "service/$RELEASE" :http' in verify
    assert "for path in /health /ready" in verify and "curl --fail" in verify


def test_the_argocd_prod_promotion_runs_without_a_prompt() -> None:
    step = _step(PROMOTE, "open_prod_pr", "Open the production PR (merge is the gate)")
    assert 'deploy --env prod --image "$IMAGE" --yes' in step["run"]


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
        "graph_agents_cli_spec": spec,
    }


@needs_bash
@pytest.mark.parametrize(
    ("change", "message"),
    [
        (
            {"manifest_edit": {"staging": {"namespace": "Bad_NS; rm -rf /"}}},
            "Invalid staging namespace",
        ),
        ({"agent_env": "RELEASE_NAME=x\n"}, "IMAGE_REPOSITORY is not set"),
        ({"agent_env_replace": ("CD=argocd", "CD=argo")}, "CD must be argocd or helm-push"),
        (
            {"agent_env_replace": ("RELEASE_NAME=weather-agent", "RELEASE_NAME=a b")},
            "Invalid RELEASE_NAME",
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


# --- pr_checks -----------------------------------------------------------------


def test_pr_checks_runs_the_gate_with_the_cli_s_own_commands() -> None:
    job = _workflow(PR_CHECKS)["jobs"]["checks"]
    assert job["env"]["MODEL_PROVIDER"] == "fake" and job["env"]["MODEL_NAME"] == "fake"
    # Provider keys reach only the eval gate step, not the tests.
    assert not any("secrets." in str(v) for v in job["env"].values())
    for name in ("graph-agents-cli lint (code and API-policy check)", "Eval gate"):
        assert _step(PR_CHECKS, "checks", name)["env"]["GRAPH_AGENTS_CLI_DISABLE_OVERRIDES"] == "1"
    tests = _step(PR_CHECKS, "checks", "Unit and integration tests")
    assert "env" not in tests


def _shims(root: Path) -> Path:
    """`yq -r '<path> // ""' <file>` and `uvx` stand-ins; uvx records what it was run with."""
    shims = root / "shims"
    shims.mkdir()
    yq = shims / "yq"
    yq.write_text(
        f"#!{sys.executable}\n"
        "import sys, yaml\n"
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


def test_codeowners_covers_everything_that_shapes_production_or_the_gate(
    rendered: dict[str, Path],
) -> None:
    for name in ("fastapi-argocd", "server-helm-push", "compat-custom"):
        codeowners = (rendered[name] / ".github" / "CODEOWNERS").read_text()
        for path in (
            "deployment/helm/weather-agent/values.yaml",
            "deployment/helm/weather-agent/values-prod.yaml",
            "deployment/helm/weather-agent/Chart.yaml",
            "deployment/helm/weather-agent/templates/deployment.yaml",
            "deployment/argocd/application-prod.yaml",
            ".github/workflows/pr_checks.yaml",
            ".github/workflows/promote-to-prod.yaml",
            ".github/agent.env",
            ".github/CODEOWNERS",
            "api-policy.yaml",
            "tests/eval/datasets/smoke.json",
            "tests/eval/eval_config.yaml",
            "graph-agents-cli-extensions.yaml",
            "extensions/team-lint/extension.yaml",
            ".graph-agents-cli/state.json",
            "graph-agents-cli-manifest.yaml",
        ):
            assert _owners(codeowners, path) == ["@CHANGE-ME/production-approvers"], (name, path)
        # Application code is reviewed as usual (no production approver needed).
        assert _owners(codeowners, "app/agent.py") == []
        assert _owners(codeowners, "tests/unit/test_agent.py") == []
