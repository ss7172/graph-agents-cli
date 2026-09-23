# Copyright 2026 Google LLC
# Modifications Copyright 2026 graph-agents-cli contributors
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

"""graph-agents-cli login — preflight for provider keys, tracing, and kubeconfig.

The CLI stores no credentials, so ``login`` is a preflight check. The
command reads the project manifest when run inside a project, inspects the
process environment and the project's ``.env``, and reports whether:

* the model provider key variable (``_defaults.PROVIDER_KEY_VARS``) is set, or,
  for ``openai-compatible``, whether ``OPENAI_BASE_URL`` is set and answers
  ``GET /models`` (3 s timeout; reachability is advisory);
* ``LANGSMITH_API_KEY`` (or an OTLP endpoint) is present when
  ``TRACING_ENABLED=true``;
* ``kubectl config current-context`` resolves (and, with ``--cluster``,
  ``kubectl cluster-info`` succeeds);
* under the ``shared-bearer`` auth policy, ``API_KEY`` is set (the local
  server answers 503 to every request without it);
* under ``--profile disconnected``, nothing hosted is configured: no
  hosted model provider, no LangSmith, no GitHub-hosted CI, ``fastapi`` runtime.

``--write-env`` prompts for the missing keys and writes them to ``.env``
without echoing the values (a blank ``KEY=`` line copied from ``.env.example``
is filled in place, anything else is appended; the file is kept at mode 0600);
inside a ``shared-bearer`` project it also generates a missing ``API_KEY`` (as
``secrets apply`` does). The CLI itself stores nothing.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import click
import yaml

from graph_agents_cli._defaults import (
    DEFAULT_AUTH_POLICY,
    DEFAULT_MODEL_PROVIDER,
    DEFAULT_RUNTIME,
    FAKE_PROVIDER,
    MODEL_PROVIDERS,
    PROVIDER_KEY_VARS,
    normalize_auth_policy,
)
from graph_agents_cli._output import Console
from graph_agents_cli._project import ManifestError, find_project_root
from graph_agents_cli._runner import run_resolved
from graph_agents_cli._skills_check import NO_UPDATE_CHECK_ENV
from graph_agents_cli._tools import ToolNotFoundError, install_hint

MANIFEST_FILENAME = "graph-agents-cli-manifest.yaml"
PROFILES = ("default", "disconnected")
HOSTED_PROVIDERS = frozenset(p for p in MODEL_PROVIDERS if p != "openai-compatible")

# Reachability probe for openai-compatible servers (self-hosted models).
MODELS_PROBE_TIMEOUT_S = 3.0
KUBECTL_TIMEOUT_S = 5
CLUSTER_INFO_TIMEOUT_S = 15

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
# GitHub-hosted runner labels (disconnected: only an on-network GHES keeps CD in profile).
_HOSTED_RUNNER_RE = re.compile(
    r"runs-on:\s*\[?\s*['\"]?(ubuntu|windows|macos)-(latest|\d+(\.\d+)?)", re.IGNORECASE
)

# Status values, ordered by severity for the summary.
OK, WARN, FAIL, SKIP = "ok", "warn", "fail", "skip"


@dataclass
class Check:
    """One preflight result. ``detail`` never contains a secret value."""

    name: str
    status: str
    detail: str
    hint: str = ""


@dataclass
class ProjectInfo:
    """What `login` learned from the manifest (all fields optional)."""

    root: str | None = None
    name: str | None = None
    provider: str = DEFAULT_MODEL_PROVIDER
    provider_source: str = "default"
    runtime: str = DEFAULT_RUNTIME
    deployment_target: str = "kubernetes"
    cd: str = "skip"
    auth_policy: str = DEFAULT_AUTH_POLICY
    environments: dict[str, dict[str, str]] = field(default_factory=dict)


@dataclass
class EnvView:
    """Merged view of the process environment and the project's .env.

    The process environment wins over the file. ``sources`` records where each
    present key came from so the report can say "set (environment)" without
    printing the value.
    """

    values: dict[str, str]
    sources: dict[str, str]
    env_file: Path

    def get(self, key: str) -> str:
        return self.values.get(key, "")

    def has(self, key: str) -> bool:
        return bool(self.values.get(key, "").strip())

    def where(self, key: str) -> str:
        return self.sources.get(key, "unset")

    def truthy(self, key: str) -> bool:
        return self.get(key).strip().lower() in _TRUE_VALUES


# ── Inputs ──────────────────────────────────────────────────────────────────


def _read_manifest(root: Path) -> dict[str, Any]:
    path = root / MANIFEST_FILENAME
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as e:
        raise ManifestError(f"Could not read {path}: {e}") from e
    if not isinstance(data, dict):
        raise ManifestError(f"malformed {MANIFEST_FILENAME}")
    return data


def load_project_info(cwd: Path | None = None) -> ProjectInfo:
    """Read the manifest of the enclosing project, if any."""
    root = find_project_root(cwd)
    if root is None:
        return ProjectInfo()
    data = _read_manifest(root)
    params = data.get("create_params") or {}
    if not isinstance(params, dict):
        params = {}
    info = ProjectInfo(root=str(root), name=data.get("name"))
    provider = params.get("model_provider")
    if provider:
        info.provider = str(provider)
        info.provider_source = "manifest"
    info.runtime = str(params.get("runtime") or DEFAULT_RUNTIME)
    info.deployment_target = str(params.get("deployment_target") or "kubernetes")
    info.cd = str(params.get("cd") or "skip")
    info.auth_policy = normalize_auth_policy(params.get("auth_policy"), warn=False)
    envs = data.get("environments") or {}
    if isinstance(envs, dict):
        info.environments = {
            str(k): {str(a): str(b or "") for a, b in (v or {}).items()}
            for k, v in envs.items()
            if isinstance(v, dict)
        }
    return info


def load_env(env_file: Path) -> EnvView:
    """Merge ``env_file`` (when present) under the process environment."""
    from dotenv import dotenv_values

    values: dict[str, str] = {}
    sources: dict[str, str] = {}
    if env_file.is_file():
        try:
            for key, value in dotenv_values(env_file).items():
                if value is None:
                    continue
                values[key] = value
                sources[key] = env_file.name
        except Exception as e:  # dotenv raises plain exceptions on bad files
            raise click.ClickException(f"Could not parse {env_file}: {e}") from e
    for key, value in os.environ.items():
        if value:
            values[key] = value
            sources[key] = "environment"
    return EnvView(values=values, sources=sources, env_file=env_file)


def resolve_provider(info: ProjectInfo, env: EnvView) -> tuple[str, str]:
    """Provider precedence: MODEL_PROVIDER (environment or .env), then the manifest, then the default.

    The manifest records the provider chosen at scaffold time; the app itself
    reads ``MODEL_PROVIDER`` at runtime, so a value in the
    environment or ``.env`` (for example ``fake`` for a local check) is what the
    preflight must judge.
    """
    if env.has("MODEL_PROVIDER"):
        return env.get("MODEL_PROVIDER").strip(), env.where("MODEL_PROVIDER")
    if info.provider_source == "manifest":
        return info.provider, "manifest"
    return DEFAULT_MODEL_PROVIDER, "default"


# ── Checks ──────────────────────────────────────────────────────────────────


def _models_url(base_url: str) -> str:
    return base_url.rstrip("/") + "/models"


def probe_openai_compatible(base_url: str, api_key: str = "") -> Check:
    """GET ``<OPENAI_BASE_URL>/models`` with a short timeout. Advisory only."""
    import httpx

    url = _models_url(base_url)
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        response = httpx.get(url, headers=headers, timeout=MODELS_PROBE_TIMEOUT_S)
    except httpx.HTTPError as e:
        return Check(
            "openai_base_url",
            WARN,
            f"OPENAI_BASE_URL set but {url} is unreachable ({type(e).__name__})",
            "Start the on-network model server or fix OPENAI_BASE_URL; the check is advisory.",
        )
    if response.status_code in (401, 403):
        return Check(
            "openai_base_url",
            WARN,
            f"{url} reachable but rejected the credentials (HTTP {response.status_code})",
            "Set MODEL_API_KEY to a key the server accepts.",
        )
    if response.status_code >= 400:
        return Check(
            "openai_base_url",
            WARN,
            f"{url} answered HTTP {response.status_code}",
            "Check that OPENAI_BASE_URL points at the API root (ending in /v1 for most servers).",
        )
    count = ""
    try:
        data = response.json().get("data")
        if isinstance(data, list):
            count = f", {len(data)} model(s) listed"
    except (ValueError, AttributeError):
        pass
    return Check("openai_base_url", OK, f"{url} reachable (HTTP {response.status_code}{count})")


def check_provider(provider: str, source: str, env: EnvView, *, profile: str) -> list[Check]:
    checks: list[Check] = []
    if provider == FAKE_PROVIDER:
        # Test-only deterministic model: no key, no network,
        # allowed under every profile. Never offered by `create`.
        checks.append(
            Check(
                "provider",
                WARN,
                f"model provider {FAKE_PROVIDER} ({source}) is the test-only fake model",
                "Set MODEL_PROVIDER to a real provider before deploying.",
            )
        )
        checks.append(Check("provider_key", SKIP, "no key needed for the fake provider"))
        return checks
    if provider not in MODEL_PROVIDERS:
        checks.append(
            Check(
                "provider",
                FAIL,
                f"unknown model provider {provider!r} ({source})",
                f"Use one of: {', '.join(MODEL_PROVIDERS)}.",
            )
        )
        return checks

    if profile == "disconnected" and provider in HOSTED_PROVIDERS:
        checks.append(
            Check(
                "provider",
                FAIL,
                f"model provider {provider} ({source}) is hosted; the disconnected profile "
                "requires openai-compatible",
                "Set MODEL_PROVIDER=openai-compatible and OPENAI_BASE_URL to an on-network server.",
            )
        )
    else:
        checks.append(Check("provider", OK, f"model provider {provider} ({source})"))

    key_var = PROVIDER_KEY_VARS[provider]
    if env.has(key_var):
        checks.append(Check("provider_key", OK, f"{key_var} set ({env.where(key_var)})"))
    elif provider == "openai-compatible":
        checks.append(
            Check(
                "provider_key",
                WARN,
                f"{key_var} not set (optional for servers that do not require a key)",
                f"Set {key_var} in .env or run 'graph-agents-cli login --write-env'.",
            )
        )
    else:
        checks.append(
            Check(
                "provider_key",
                FAIL,
                f"{key_var} not set",
                f"Export {key_var}, add it to .env, or run 'graph-agents-cli login --write-env'.",
            )
        )

    if provider == "openai-compatible":
        if env.has("OPENAI_BASE_URL"):
            checks.append(probe_openai_compatible(env.get("OPENAI_BASE_URL"), env.get(key_var)))
        else:
            checks.append(
                Check(
                    "openai_base_url",
                    FAIL,
                    "OPENAI_BASE_URL not set (required for openai-compatible)",
                    "Set OPENAI_BASE_URL to the server's API root, e.g. http://ollama:11434/v1.",
                )
            )
    return checks


def check_judge(env: EnvView, *, profile: str) -> Check:
    judge_provider = env.get("JUDGE_MODEL_PROVIDER").strip()
    if not judge_provider:
        return Check("judge", SKIP, "judge defaults to the agent's provider and key")
    if judge_provider == FAKE_PROVIDER:
        return Check("judge", OK, f"judge provider {FAKE_PROVIDER} (test-only; no key needed)")
    if judge_provider not in MODEL_PROVIDERS:
        return Check(
            "judge",
            FAIL,
            f"unknown JUDGE_MODEL_PROVIDER {judge_provider!r}",
            f"Use one of: {', '.join(MODEL_PROVIDERS)}.",
        )
    if profile == "disconnected" and judge_provider in HOSTED_PROVIDERS:
        return Check(
            "judge",
            FAIL,
            f"JUDGE_MODEL_PROVIDER={judge_provider} is hosted",
            "Point JUDGE_MODEL_PROVIDER=openai-compatible and JUDGE_BASE_URL at an on-network server.",
        )
    key_var = PROVIDER_KEY_VARS[judge_provider]
    if env.has("JUDGE_API_KEY") or env.has(key_var):
        return Check("judge", OK, f"judge provider {judge_provider}, key present")
    if judge_provider == "openai-compatible":
        return Check("judge", OK, f"judge provider {judge_provider} (no key; optional)")
    return Check(
        "judge",
        WARN,
        f"judge provider {judge_provider} but neither JUDGE_API_KEY nor {key_var} is set",
        "Set JUDGE_API_KEY (defaults to the provider key when the providers match).",
    )


def check_tracing(env: EnvView, *, profile: str) -> Check:
    enabled = env.truthy("TRACING_ENABLED")
    has_langsmith = env.has("LANGSMITH_API_KEY")
    has_otlp = env.has("OTEL_EXPORTER_OTLP_ENDPOINT")

    if profile == "disconnected" and has_langsmith:
        return Check(
            "tracing",
            FAIL,
            f"LANGSMITH_API_KEY set ({env.where('LANGSMITH_API_KEY')}); LangSmith is a hosted dependency",
            "Unset LANGSMITH_API_KEY and export traces over OTLP to an in-cluster collector, or disable tracing.",
        )
    if not enabled:
        return Check("tracing", SKIP, "TRACING_ENABLED is not true; tracing off")
    if profile == "disconnected":
        if has_otlp:
            return Check("tracing", OK, "tracing enabled with OTEL_EXPORTER_OTLP_ENDPOINT")
        return Check(
            "tracing",
            FAIL,
            "TRACING_ENABLED=true without OTEL_EXPORTER_OTLP_ENDPOINT",
            "Set OTEL_EXPORTER_OTLP_ENDPOINT to an on-network collector or set TRACING_ENABLED=false.",
        )
    if has_langsmith:
        return Check(
            "tracing",
            OK,
            f"tracing enabled; LANGSMITH_API_KEY set ({env.where('LANGSMITH_API_KEY')})",
        )
    if has_otlp:
        return Check(
            "tracing", OK, "tracing enabled with OTEL_EXPORTER_OTLP_ENDPOINT (no LangSmith)"
        )
    return Check(
        "tracing",
        FAIL,
        "TRACING_ENABLED=true but neither LANGSMITH_API_KEY nor OTEL_EXPORTER_OTLP_ENDPOINT is set",
        "Set LANGSMITH_API_KEY (or an OTLP endpoint), or set TRACING_ENABLED=false.",
    )


def _kubectl(args: list[str], timeout: int) -> subprocess.CompletedProcess:
    return run_resolved(
        ["kubectl", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def check_kubeconfig(info: ProjectInfo, *, cluster: bool) -> list[Check]:
    if info.root is not None and info.deployment_target == "none":
        return [Check("kubeconfig", SKIP, "deployment target is none")]

    severity = FAIL if cluster else WARN
    try:
        result = _kubectl(["config", "current-context"], KUBECTL_TIMEOUT_S)
    except ToolNotFoundError:
        return [Check("kubeconfig", severity, "kubectl not found on PATH", install_hint("kubectl"))]
    except (OSError, subprocess.SubprocessError) as e:
        return [Check("kubeconfig", severity, f"kubectl failed: {e}")]

    if result.returncode != 0:
        return [
            Check(
                "kubeconfig",
                severity,
                "no current kube context (kubectl config current-context failed)",
                "Set a context with 'kubectl config use-context <name>' or point KUBECONFIG at a file.",
            )
        ]
    context = (result.stdout or "").strip()
    detail = f"current context: {context}"
    expected = {
        env_name: cfg.get("context", "")
        for env_name, cfg in info.environments.items()
        if cfg.get("context")
    }
    matches = [env_name for env_name, ctx in expected.items() if ctx == context]
    if matches:
        detail += f" (manifest environment: {', '.join(matches)})"
    checks = [Check("kubeconfig", OK, detail)]

    if cluster:
        try:
            result = _kubectl(["cluster-info"], CLUSTER_INFO_TIMEOUT_S)
        except (OSError, subprocess.SubprocessError) as e:
            checks.append(Check("cluster", FAIL, f"kubectl cluster-info failed: {e}"))
            return checks
        if result.returncode == 0:
            checks.append(Check("cluster", OK, f"cluster reachable via context {context}"))
        else:
            err = (result.stderr or result.stdout or "").strip().splitlines()
            checks.append(
                Check(
                    "cluster",
                    FAIL,
                    f"cluster unreachable via context {context}: {err[0] if err else 'unknown error'}",
                    "Check VPN / kubeconfig credentials; 'kubectl cluster-info' must succeed before deploy.",
                )
            )
    return checks


def _hosted_ci_indicators(root: Path | None, env: EnvView) -> list[str]:
    """Return what makes this project depend on GitHub-hosted CI."""
    indicators: list[str] = []
    if env.truthy("GITHUB_ACTIONS"):
        indicators.append("running under GITHUB_ACTIONS")
    if root is None:
        return indicators
    workflows = root / ".github" / "workflows"
    if not workflows.is_dir():
        return indicators
    for path in sorted(workflows.iterdir()):
        if path.suffix not in (".yml", ".yaml"):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if _HOSTED_RUNNER_RE.search(text):
            indicators.append(f"{path.relative_to(root)} uses a GitHub-hosted runner")
    return indicators


def check_disconnected_profile(info: ProjectInfo, env: EnvView) -> list[Check]:
    """Extra checks that only apply under ``--profile disconnected``."""
    checks: list[Check] = []

    if info.root is not None:
        if info.runtime == "fastapi":
            checks.append(Check("profile.runtime", OK, "runtime fastapi"))
        else:
            checks.append(
                Check(
                    "profile.runtime",
                    FAIL,
                    f"runtime {info.runtime} needs a LangSmith license check at startup",
                    "Scaffold with --runtime fastapi for the disconnected profile.",
                )
            )

    indicators = _hosted_ci_indicators(Path(info.root) if info.root else None, env)
    if indicators:
        checks.append(
            Check(
                "profile.ci",
                FAIL,
                "GitHub-hosted CI detected: " + "; ".join(indicators),
                "CI/CD is outside the disconnected profile unless an on-network GitHub Enterprise "
                "Server hosts Actions; use 'cd: skip' with direct-mode deploy.",
            )
        )
    elif info.root is not None and info.cd != "skip":
        checks.append(
            Check(
                "profile.ci",
                WARN,
                f"cd: {info.cd} needs an on-network GitHub Enterprise Server",
                "Confirm the Actions host is on-network, or switch to 'cd: skip'.",
            )
        )
    else:
        checks.append(Check("profile.ci", OK, "no GitHub-hosted CI dependency"))

    if os.environ.get(NO_UPDATE_CHECK_ENV) == "1":
        checks.append(Check("profile.update_check", OK, f"{NO_UPDATE_CHECK_ENV}=1"))
    else:
        checks.append(
            Check(
                "profile.update_check",
                WARN,
                f"{NO_UPDATE_CHECK_ENV} is not 1; the CLI will try the GitHub release check "
                "and npx on each run",
                f"Export {NO_UPDATE_CHECK_ENV}=1.",
            )
        )
    return checks


def needs_api_key(info: ProjectInfo, env: EnvView) -> bool:
    """True inside a project whose effective auth policy is shared-bearer and API_KEY is unset.

    ``AUTH_POLICY`` from the environment or ``.env`` (what the app reads at
    runtime) wins over the manifest's ``create_params.auth_policy``.
    """
    if info.root is None or env.has("API_KEY"):
        return False
    policy = env.get("AUTH_POLICY").strip() or info.auth_policy
    return policy == "shared-bearer"


def check_api_key(info: ProjectInfo, env: EnvView) -> Check | None:
    if info.root is None:
        return None
    if needs_api_key(info, env):
        return Check(
            "api_key",
            WARN,
            "API_KEY unset; under AUTH_POLICY=shared-bearer the local server answers 503 "
            "to every request (run, eval, playground)",
            "Run 'graph-agents-cli login --write-env' to generate one into .env.",
        )
    if env.has("API_KEY"):
        return Check("api_key", OK, f"API_KEY set ({env.where('API_KEY')})")
    return None


def run_preflight(info: ProjectInfo, env: EnvView, *, profile: str, cluster: bool) -> list[Check]:
    provider, source = resolve_provider(info, env)
    checks = check_provider(provider, source, env, profile=profile)
    api_key = check_api_key(info, env)
    if api_key is not None:
        checks.append(api_key)
    checks.append(check_judge(env, profile=profile))
    checks.append(check_tracing(env, profile=profile))
    checks.extend(check_kubeconfig(info, cluster=cluster))
    if profile == "disconnected":
        checks.extend(check_disconnected_profile(info, env))
    return checks


# ── --write-env ─────────────────────────────────────────────────────────────


def missing_env_keys(info: ProjectInfo, env: EnvView, *, profile: str) -> list[tuple[str, bool]]:
    """Keys ``--write-env`` should ask for: ``(name, is_secret)`` in prompt order."""
    provider, _ = resolve_provider(info, env)
    wanted: list[tuple[str, bool]] = []
    if provider in PROVIDER_KEY_VARS:
        if provider == "openai-compatible":
            wanted.append(("OPENAI_BASE_URL", False))
        wanted.append((PROVIDER_KEY_VARS[provider], True))
    if env.truthy("TRACING_ENABLED") and profile != "disconnected":
        if not env.has("OTEL_EXPORTER_OTLP_ENDPOINT"):
            wanted.append(("LANGSMITH_API_KEY", True))
    return [(name, secret) for name, secret in wanted if not env.has(name)]


_ENV_ASSIGNMENT = re.compile(r"^(?P<lead>\s*(?:export\s+)?)(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*=")
_BLANK_VALUES = frozenset({"", '""', "''"})
ENV_FILE_MODE = 0o600


def write_env(env_file: Path, entries: dict[str, str]) -> None:
    """Set ``KEY=value`` in ``env_file``: fill blank ``KEY=`` lines in place, append the rest.

    ``cp .env.example .env`` leaves ``API_KEY=`` (and a blank provider key)
    in the file; appending a second line would leave two assignments. Every
    blank assignment of a key gets the value (a later blank one would
    otherwise win), and a key the file does not assign is appended. The file
    holds credentials: it is written atomically with mode 0600 (a symlinked
    ``.env`` is written through to its target).
    """
    if not entries:
        return
    target = env_file.resolve() if env_file.is_symlink() else env_file
    target.parent.mkdir(parents=True, exist_ok=True)
    existing = ""
    if target.is_file():
        # newline="": keep CRLF files CRLF (no universal-newline translation).
        with target.open(encoding="utf-8", newline="") as handle:
            existing = handle.read()
    lines = existing.splitlines(keepends=True)
    pending = dict(entries)
    filled: set[str] = set()
    for index, line in enumerate(lines):
        match = _ENV_ASSIGNMENT.match(line)
        if not match or match.group("key") not in pending:
            continue
        value = line[match.end() :].strip()
        # Blank as python-dotenv (every reader) sees it: `KEY=`, `KEY=""  # note`.
        # An unquoted `KEY=  # note` is the value "# note" to dotenv, so it is set.
        if value.split(" #", 1)[0].strip() not in _BLANK_VALUES:
            continue
        key = match.group("key")
        ending = line[len(line.rstrip("\r\n")) :]  # keep the file's own line ending
        lines[index] = f"{match.group('lead')}{key}={pending[key]}{ending}"
        filled.add(key)
    text = "".join(lines)
    appended = [f"{k}={v}" for k, v in pending.items() if k not in filled]
    if appended:
        if text and not text.endswith("\n"):
            text += "\n"
        text += "\n".join(appended) + "\n"
    _write_private(target, text)


def _write_private(path: Path, text: str) -> None:
    """Replace ``path`` with ``text``, readable by the owner only (0600)."""
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        os.chmod(tmp_name, ENV_FILE_MODE)
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def prompt_and_write_env(info: ProjectInfo, env: EnvView, *, profile: str, console: Console) -> int:
    """Prompt for each missing key and write the answers to ``env.env_file`` (see ``write_env``).

    Secret values are read with ``hide_input`` and never printed. An empty
    answer skips that key. A missing ``API_KEY`` under the shared-bearer policy
    is generated rather than prompted for. When stdin runs out (a CI job, a
    pipe), prompting stops and whatever was collected, the generated key
    included, is still written. Returns the number of keys written.
    """
    from graph_agents_cli.secrets._apply import GENERATED_KEY, generate_api_key

    missing = missing_env_keys(info, env, profile=profile)
    generate = needs_api_key(info, env)
    if not missing and not generate:
        console.print("  Nothing to write: every required key is already set.", style="dim")
        return 0
    console.print()
    console.print(f"  Writing missing keys to {env.env_file} (leave blank to skip).", style="bold")
    entries: dict[str, str] = {}
    if generate:
        entries[GENERATED_KEY] = generate_api_key()
        console.print(
            f"  Generated {GENERATED_KEY} for AUTH_POLICY=shared-bearer (value not shown).",
            style="green",
        )
    skipped: list[str] = []
    for position, (name, secret) in enumerate(missing):
        try:
            value = click.prompt(
                f"  {name}",
                default="",
                show_default=False,
                hide_input=secret,
            ).strip()
        except click.Abort:
            # EOF on stdin: nothing more to read. Keep what was collected.
            skipped = [n for n, _ in missing[position:]]
            click.echo("", err=True)
            break
        if value:
            entries[name] = value
    write_env(env.env_file, entries)
    if skipped:
        console.print(
            f"  No input for {', '.join(skipped)} (stdin closed); set them in "
            f"{env.env_file} or rerun `graph-agents-cli login --write-env` in a terminal.",
            style="yellow",
        )
    if entries:
        console.print(
            f"  Wrote {len(entries)} key(s) to {env.env_file} (values not shown).", style="green"
        )
    else:
        console.print("  No keys written.", style="dim")
    return len(entries)


# ── Report ──────────────────────────────────────────────────────────────────

_ICONS = {OK: ("✓", "green"), WARN: ("!", "yellow"), FAIL: ("✗", "red"), SKIP: ("-", "dim")}


def summarize(checks: list[Check]) -> dict[str, int]:
    summary = {OK: 0, WARN: 0, FAIL: 0, SKIP: 0}
    for check in checks:
        summary[check.status] += 1
    return summary


def build_report(
    info: ProjectInfo, env: EnvView, checks: list[Check], *, profile: str
) -> dict[str, Any]:
    provider, source = resolve_provider(info, env)
    summary = summarize(checks)
    return {
        "profile": profile,
        "project": {
            "root": info.root,
            "name": info.name,
            "provider": provider,
            "provider_source": source,
            "runtime": info.runtime if info.root else None,
            "deployment_target": info.deployment_target if info.root else None,
            "cd": info.cd if info.root else None,
        },
        "env_file": str(env.env_file),
        "checks": [asdict(c) for c in checks],
        "summary": summary,
        "ok": summary[FAIL] == 0,
    }


def print_report(report: dict[str, Any], console: Console) -> None:
    project = report["project"]
    console.print()
    console.print("Preflight", style="cyan bold")
    console.print()
    if project["root"]:
        console.print(f"  Project:  {project['name'] or '?'} ({project['root']})")
        console.print(
            f"  Runtime:  {project['runtime']}   target: {project['deployment_target']}"
            f"   cd: {project['cd']}"
        )
    else:
        console.print("  Project:  none (not inside a graph-agents-cli project)", style="dim")
    console.print(f"  Provider: {project['provider']} ({project['provider_source']})")
    console.print(f"  Env file: {report['env_file']}")
    console.print(f"  Profile:  {report['profile']}")
    console.print()
    for check in report["checks"]:
        icon, style = _ICONS[check["status"]]
        console.print(f"  [{style}]{icon}[/] {check['name']}: {check['detail']}", highlight=False)
        if check["hint"] and check["status"] in (WARN, FAIL):
            console.print(f"      {check['hint']}", style="dim", highlight=False)
    console.print()
    s = report["summary"]
    console.print(
        f"  {s[OK]} ok, {s[WARN]} warning(s), {s[FAIL]} failed, {s[SKIP]} skipped.",
        style="green" if report["ok"] else "red",
    )
    console.print("  Nothing is stored by the CLI.", style="dim")
    console.print()


# ── Command ─────────────────────────────────────────────────────────────────


@click.command("login")
@click.option(
    "--profile",
    type=click.Choice(PROFILES),
    default="default",
    show_default=True,
    help="'disconnected' fails on any hosted dependency.",
)
@click.option(
    "--cluster",
    is_flag=True,
    default=False,
    help="Also run 'kubectl cluster-info' against the current context (fails when unreachable).",
)
@click.option(
    "--write-env",
    is_flag=True,
    default=False,
    help=(
        "Prompt for missing keys and write them to .env (blank KEY= lines are filled in "
        "place; the file is kept 0600; values are never echoed)."
    ),
)
@click.option(
    "--env-file",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="The .env file to read (and write with --write-env). Default: <project>/.env or ./.env.",
)
@click.option(
    "--status",
    is_flag=True,
    default=False,
    help="Print the report and exit 0 even when a check fails.",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Emit the report as JSON.",
)
def cmd_login(*, profile, cluster, write_env, env_file, status, as_json):
    """Check model provider keys, LangSmith, and kubeconfig; optionally write .env.

    A preflight, not an authentication: nothing is stored. Exit code 1 when
    any check fails (0 with --status).
    """
    if write_env and as_json:
        raise click.UsageError(
            "--write-env prompts interactively and cannot be combined with --json."
        )

    console = Console()
    info = load_project_info()
    base = Path(info.root) if info.root else Path.cwd()
    env_path = env_file if env_file is not None else base / ".env"
    env = load_env(env_path)

    if write_env:
        if prompt_and_write_env(info, env, profile=profile, console=console):
            env = load_env(env_path)

    checks = run_preflight(info, env, profile=profile, cluster=cluster)
    report = build_report(info, env, checks, profile=profile)

    if as_json:
        click.echo(json.dumps(report, indent=2))
    else:
        print_report(report, console)

    if not report["ok"] and not status:
        click.get_current_context().exit(1)
