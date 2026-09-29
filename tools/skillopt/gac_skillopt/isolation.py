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

"""Isolated environments for rollouts on Claude Code and Codex (verified in DESIGN section 3).

Every process a rollout starts gets an environment built from an allowlist, never the caller's:
a rollout launched from inside another agent session must not inherit that session's variables
(``CLAUDECODE``, session ids, messaging sockets and tokens, provider keys, ``KUBECONFIG``).

Claude Code keeps the developer's user configuration for authentication (a scratch
``CLAUDE_CONFIG_DIR`` is not logged in) and switches everything else off with flags and
variables; the skill under test is a project skill of the workspace and the Bash sandbox comes
from the workspace's ``.claude/settings.json``. Codex runs with a scratch ``CODEX_HOME`` (API-key
login, generated ``config.toml`` with a permission profile) and a scratch ``HOME``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from .paths import REPO, Bench

# Variables a CLI needs to run; everything else is dropped.
ENV_ALLOW = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TERM",
    "__CF_USER_TEXT_ENCODING",
    "SSL_CERT_FILE",
)

# System and Homebrew directories only: a developer's ~/.local/bin may hold other agent CLIs
# (google-agents-cli, agents-cli) that would confound a rollout.
SYSTEM_PATH = ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin")

CLAUDE_BIN = os.environ.get("GAC_SKILLOPT_CLAUDE_BIN") or shutil.which("claude") or "claude"
CODEX_BIN = os.environ.get("GAC_SKILLOPT_CODEX_BIN") or shutil.which("codex") or "codex"


def key_file() -> Path | None:
    """The file with one ``OPENAI_API_KEY=`` line (Codex only), from GAC_SKILLOPT_OPENAI_KEY_FILE."""
    value = os.environ.get("GAC_SKILLOPT_OPENAI_KEY_FILE", "")
    return Path(value) if value else None


# Under the home directory, denied to every rollout: credentials, the Codex login
# (auth.json), and this CLI's per-user state, whose `scaffold enhance`/`upgrade` backups hold
# copies of projects' `.env` files. The sandboxed shell can read the rest of the home directory
# (a rollout ran `find / -maxdepth 6` and listed those backups); name more with
# GAC_SKILLOPT_DENY_READ.
HOME_SECRET_DIRS = (
    ".ssh",
    ".kube",
    ".aws",
    ".docker",
    ".config/gh",
    ".config/gcloud",
    ".azure",
    ".gnupg",
    ".codex",
    ".graph-agents-cli",
)


def secret_dirs() -> tuple[Path, ...]:
    """Directories no rollout may read: credentials, the key file's directory, and
    ``GAC_SKILLOPT_DENY_READ`` (os.pathsep-separated). Set the key file variable for Claude runs
    too: without it the key's directory is not denied (it is only needed to log Codex in)."""
    home = Path.home()
    dirs = [home / d for d in HOME_SECRET_DIRS]
    kf = key_file()
    if kf is not None and kf.parent != Path("/"):
        dirs.insert(0, kf.parent.resolve())
    extra = os.environ.get("GAC_SKILLOPT_DENY_READ", "")
    dirs += [Path(p).resolve() for p in extra.split(os.pathsep) if p.strip()]
    return tuple(dict.fromkeys(dirs))


def checkout_roots() -> tuple[Path, ...]:
    """This checkout and, for a git worktree, the main checkout that holds it."""
    roots = [REPO]
    try:
        common = subprocess.run(
            ["git", "-C", str(REPO), "rev-parse", "--path-format=absolute", "--git-common-dir"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        if common:
            roots.append(Path(common).parent)
    except (OSError, subprocess.CalledProcessError):
        pass
    return tuple(dict.fromkeys(r.resolve() for r in roots))


# Under the home directory, denied to every rollout because they hold copies of skills: uv's
# cache unpacks every graph-agents-cli wheel ever installed, bundled skills included (59 copies
# on the development machine; a round-2 rollout grepped one), and uv's tool directory holds
# other agent CLIs (google-agents-cli) with their own skills. Rollouts never need either: each
# has its own UV_CACHE_DIR, and PATH leaves out the developer's tools.
HOME_SKILL_COPY_DIRS = (".cache/uv", ".local/share/uv/tools")


def bundled_skill_dirs(bench: Bench) -> tuple[Path, ...]:
    """The shipped skills inside the scratch CLI build (``graph_agents_cli/skills/data``): a
    rollout that reads them sees the shipped text instead of the candidate under test (a round-3b
    Codex rollout read the bundled workflow ``SKILL.md``). Only ``graph-agents-cli setup`` reads
    that directory, so every command a task needs still works; the rest of the package stays
    readable (the CLI runs from it)."""
    found = bench.uv_tools.glob(
        "graph-agents-cli/lib/python*/site-packages/graph_agents_cli/skills/data"
    )
    return tuple(sorted(p.resolve() for p in found if p.is_dir()))


def skill_copy_dirs(bench: Bench) -> tuple[Path, ...]:
    """Every directory outside the workspace known to hold a copy of a skill: the scratch CLI's
    bundled skills and the existing ``HOME_SKILL_COPY_DIRS``. The checkout's ``skills/`` and the
    developer's skill directories are denied elsewhere (``checkout_roots``,
    ``GAC_SKILLOPT_DENY_READ``)."""
    home = Path.home()
    return (
        *bundled_skill_dirs(bench),
        *(home / d for d in HOME_SKILL_COPY_DIRS if (home / d).is_dir()),
    )


def no_read(bench: Bench) -> tuple[Path, ...]:
    """What a rollout may not read beyond credentials: the checkout (the benchmark's hidden
    checks and scripted solutions, the other skills), copies of the skills outside the
    workspace, other rollouts' outputs and the Codex login."""
    return tuple(
        dict.fromkeys(
            (
                *secret_dirs(),
                *checkout_roots(),
                *skill_copy_dirs(bench),
                bench.runs,
                bench.scratch / "codex",
            )
        )
    )


# ── The CLI build and uv ────────────────────────────────────────────────────

CLI_WRAPPER = """#!/bin/sh
# gac-bench wrapper: the CLI keeps its per-user state (scaffold enhance/upgrade backups) inside
# the rollout workspace, the only place a sandboxed rollout may write.
if [ -n "${{GAC_CLI_HOME:-}}" ]; then HOME="$GAC_CLI_HOME"; export HOME; fi
exec '{real}' "$@"
"""


def _run(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, check=True, capture_output=True, text=True, **kw)  # type: ignore[call-overload]


def _install_cli(bench: Bench) -> None:
    # --refresh-package: uv keys a local build on the git commit, but in a git worktree (.git is
    # a file there) that key does not change, and uv reinstalled a wheel cached at an older
    # commit (seen: a build recording b35b246 at HEAD a4ed387). Refreshing only this package
    # rebuilds it (about a second) and keeps its dependencies cached.
    _run(
        [
            "uv",
            "tool",
            "install",
            "--force",
            "--quiet",
            "--refresh-package",
            "graph-agents-cli",
            "--from",
            str(REPO),
            "graph-agents-cli",
        ],
        env={
            **os.environ,
            "UV_TOOL_DIR": str(bench.uv_tools),
            "UV_TOOL_BIN_DIR": str(bench.scratch / "tool-bin"),
        },
    )


# ── Stale CLI builds ────────────────────────────────────────────────────────

# What goes into the wheel (graph_agents_cli._build.SOURCE_PATHS): a change under one of these
# since the build's commit makes the build stale.
CLI_SOURCE_PATHS = ("src", "pyproject.toml", "hatch_build.py")


def build_info(bench: Bench) -> dict:
    """The scratch CLI's recorded build (``_build_info.json`` in its package): commit and dirty
    flag. Empty when the build records no source."""
    for path in sorted(
        bench.uv_tools.glob(
            "graph-agents-cli/lib/python*/site-packages/graph_agents_cli/_build_info.json"
        )
    ):
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            return {}
    return {}


def _git_ok(*args: str) -> bool | None:
    """True/False for a git predicate in the checkout; None when git cannot answer."""
    try:
        proc = subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True)
    except OSError:
        return None
    if proc.returncode in (0, 1):
        return proc.returncode == 0
    return None


def stale_reason(
    commit: str | None,
    dirty: bool,
    head: str | None,
    *,
    is_ancestor: bool | None,
    sources_unchanged: bool | None,
) -> str:
    """Why a CLI build does not match the checkout, or "" when it does.

    The build must come from a commit that is HEAD or an ancestor of it, with no uncommitted
    source change, and the wheel's sources (``CLI_SOURCE_PATHS``) must not have changed between
    that commit and the checkout. An older ancestor build with the same sources is current (a
    build is only as old as its sources); one built before a later ``src/`` fix is stale."""
    if not commit:
        return "the build records no source commit"
    if dirty:
        return f"the build {commit[:7]} had uncommitted source changes"
    if not head:
        return "the checkout's HEAD is unknown"
    if is_ancestor is None or sources_unchanged is None:
        return f"git could not compare the build {commit[:7]} with HEAD {head[:7]}"
    if not is_ancestor:
        return f"the build's commit {commit[:7]} is not HEAD {head[:7]} or an ancestor of it"
    if not sources_unchanged:
        return (
            f"the CLI sources ({', '.join(CLI_SOURCE_PATHS)}) changed since the build's commit "
            f"{commit[:7]}"
        )
    return ""


def cli_staleness(bench: Bench) -> str:
    """``stale_reason`` for the scratch CLI against this checkout (sources compared with the
    working tree, so an uncommitted ``src/`` change also makes the build stale)."""
    info = build_info(bench)
    commit = str(info.get("commit") or "") or None
    head = None
    try:
        head = subprocess.run(
            ["git", "-C", str(REPO), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        pass
    is_anc = unchanged = None
    if commit and head:
        is_anc = _git_ok("merge-base", "--is-ancestor", commit, head)
        unchanged = _git_ok("diff", "--quiet", commit, "--", *CLI_SOURCE_PATHS)
        if unchanged:
            untracked = subprocess.run(
                [
                    "git",
                    "-C",
                    str(REPO),
                    "ls-files",
                    "--others",
                    "--exclude-standard",
                    "--",
                    *CLI_SOURCE_PATHS,
                ],
                capture_output=True,
                text=True,
            ).stdout.strip()
            unchanged = not untracked
    return stale_reason(
        commit,
        info.get("dirty") is not False and bool(info),
        head,
        is_ancestor=is_anc,
        sources_unchanged=unchanged,
    )


def ensure_bin(bench: Bench, *, rebuild: bool = False, on_stale: str = "refuse") -> Path:
    """The scratch bin rollouts put first on PATH: ``graph-agents-cli`` built from this checkout
    (behind a wrapper) and a current ``uv`` (uv older than 0.9.29 panics in both sandboxes).

    A CLI build that does not match the checkout (``cli_staleness``) is rebuilt when
    ``on_stale="rebuild"`` (``setup``) and refused otherwise: runs that share a scratch must not
    rebuild the CLI under each other."""
    bench.bin.mkdir(parents=True, exist_ok=True)
    if rebuild or not bench.cli_real.exists():
        _install_cli(bench)
    elif os.environ.get("GAC_SKILLOPT_ALLOW_STALE_CLI") != "1":
        reason = cli_staleness(bench)
        if reason and on_stale == "rebuild":
            print(f"rebuilding the scratch CLI: {reason}")
            _install_cli(bench)
            reason = cli_staleness(bench)
        if reason:
            raise SystemExit(
                f"the scratch CLI {bench.cli_real} is stale: {reason}. Rebuild it with "
                "`python -m gac_skillopt setup --rebuild-cli` (GAC_SKILLOPT_ALLOW_STALE_CLI=1 "
                "runs it anyway, e.g. to reproduce an old run)"
            )
    if bench.cli_real.read_text(errors="replace").startswith(
        CLI_WRAPPER.splitlines()[0] + "\n# gac-bench"
    ):
        raise SystemExit(
            f"{bench.cli_real} is a gac-bench wrapper, not the CLI: rerun with --rebuild-cli"
        )
    wrapper = bench.bin / "graph-agents-cli"
    text = CLI_WRAPPER.format(real=bench.cli_real)
    if wrapper.is_symlink():  # e.g. a uv tool bin link: never write through it onto the CLI
        wrapper.unlink()
    if not wrapper.exists() or wrapper.read_text() != text:
        wrapper.write_text(text)
        wrapper.chmod(0o755)
    if not (bench.uv_venv / "bin" / "uv").exists():
        _run(["uv", "venv", "-q", str(bench.uv_venv)])
        _run(
            ["uv", "pip", "install", "-q", "uv>=0.9.29"],
            env={**os.environ, "VIRTUAL_ENV": str(bench.uv_venv)},
        )
    for tool in ("uv", "uvx"):
        link = bench.bin / tool
        if not link.exists():
            link.symlink_to(bench.uv_venv / "bin" / tool)
    bench.kubeconfig.touch(exist_ok=True)
    return bench.bin


def project_python(bench: Bench) -> str:
    """The interpreter generated projects are synced with (the CLI build's own, resolved), so a
    rollout never needs uv to discover or download one."""
    return str(bench.cli_python.resolve())


def cli_version(bench: Bench) -> str:
    return _run([str(bench.cli_real), "--version"]).stdout.strip()


# ── Environments ────────────────────────────────────────────────────────────


def base_env(
    bench: Bench,
    bench_dir: Path,
    *,
    port: int,
    home: Path | None = None,
) -> dict[str, str]:
    """Allowlisted environment plus the scratch-only settings every rollout process gets.

    ``bench_dir`` is the workspace's private directory (temp files, uv cache, the CLI's home,
    an empty kubeconfig, helm's directories). ``home`` replaces HOME (Codex, the verifier).
    """
    env = {k: os.environ[k] for k in ENV_ALLOW if k in os.environ}
    env["PATH"] = ":".join([str(bench.bin), *SYSTEM_PATH])
    if home is not None:
        env["HOME"] = str(home)
    kube = bench_dir / "kube" / "config"
    for sub in ("tmp", "uv-cache", "home", "kube", "helm/cache", "helm/config", "helm/data"):
        (bench_dir / sub).mkdir(parents=True, exist_ok=True)
    kube.touch(exist_ok=True)
    env.update(
        {
            "TMPDIR": str(bench_dir / "tmp"),
            # zsh (Codex runs every command with `zsh -lc`) writes here-documents under
            # $TMPPREFIX, /tmp/zsh by default, which the rollout sandbox makes read-only.
            "TMPPREFIX": str(bench_dir / "tmp" / "zsh"),
            "UV_CACHE_DIR": str(bench_dir / "uv-cache"),
            "UV_PYTHON": project_python(bench),
            "UV_PYTHON_DOWNLOADS": "never",
            "GAC_CLI_HOME": str(bench_dir / "home"),
            "KUBECONFIG": str(kube),
            # No container engine: a rollout (or a scripted solution) that runs a real `deploy`
            # or `build` fails at docker instead of building or pushing images on the host.
            "DOCKER_HOST": f"unix://{bench_dir / 'no-docker.sock'}",
            "HELM_CACHE_HOME": str(bench_dir / "helm/cache"),
            "HELM_CONFIG_HOME": str(bench_dir / "helm/config"),
            "HELM_DATA_HOME": str(bench_dir / "helm/data"),
            "GRAPH_AGENTS_CLI_NO_UPDATE_CHECK": "1",
            "GRAPH_AGENTS_CLI_RUN_PORT": str(port),
            # Generated projects run their model calls on the deterministic fake provider.
            "MODEL_PROVIDER": "fake",
            "DISABLE_AUTOUPDATER": "1",
        }
    )
    return env


# ── Claude Code ─────────────────────────────────────────────────────────────

CLAUDE_ISOLATION_FLAGS = (
    "--setting-sources",
    "project",
    "--strict-mcp-config",
    "--mcp-config",
    '{"mcpServers":{}}',
    "--settings",
    '{"disableAllHooks":true}',
    "--no-session-persistence",
)

CLAUDE_ISOLATION_ENV = {
    "ENABLE_CLAUDEAI_MCP_SERVERS": "false",
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
    # Claude Code's own bundled skills are not the developer's, but they would still compete
    # with the skill under test.
    "CLAUDE_CODE_DISABLE_BUNDLED_SKILLS": "1",
    "CLAUDE_CODE_DISABLE_CLAUDE_API_SKILL": "1",
    "CLAUDE_CODE_DISABLE_POLICY_SKILLS": "1",
    "CLAUDE_CODE_DISABLE_WORKFLOWS": "1",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    "DISABLE_DOCTOR_COMMAND": "1",
    "DISABLE_BUILTIN_AGENTS": "1",
}

CLAUDE_TOOLS = "Read,Write,Edit,Glob,Grep,Bash,Skill"
# The rollouts' permission mode (with `--permission-prompts none`): round 1's. The owner's
# 2026-09-27 decision moves rollouts to bypassPermissions with the Bash sandbox kept, once a
# preflight proves the sandbox still holds in that mode (`preflight --permission-mode`).
CLAUDE_PERMISSION_MODE = "acceptEdits"
PERMISSION_MODES = ("acceptEdits", "bypassPermissions")
ALLOWED_DOMAINS = ("pypi.org", "files.pythonhosted.org")


def claude_settings(bench: Bench, work: Path | None = None) -> dict:
    """The workspace ``.claude/settings.json``: the Bash sandbox takes effect only from a
    settings file, with ``--permission-prompts none`` (anything that would prompt is denied)."""
    # The Read/Edit/Write tools follow permissions, not the Bash sandbox: deny them the same paths.
    deny = [f"Read(/{p}/**)" for p in no_read(bench)]
    deny += [f"{tool}(/{p}/**)" for p in bench.deny_write for tool in ("Edit", "Write")]
    if work is not None:
        # The sandbox settings and the skill under test: Claude Code reloads a changed settings
        # file, so an edit could loosen the sandbox mid-session (the integrity hash only zeroes
        # the score afterwards).
        deny += [f"{tool}(/{work.resolve()}/.claude/**)" for tool in ("Edit", "Write")]
    return {
        "disableAllHooks": True,
        "permissions": {"deny": deny},
        "sandbox": {
            "enabled": True,
            "autoAllowBashIfSandboxed": True,
            "allowUnsandboxedCommands": False,
            "network": {
                "allowedDomains": list(ALLOWED_DOMAINS),
                # eval/run start the project's server on 127.0.0.1:<GRAPH_AGENTS_CLI_RUN_PORT>.
                "allowLocalBinding": True,
            },
            "filesystem": {
                "denyRead": [str(p) for p in no_read(bench)],
                "denyWrite": [str(p) for p in bench.deny_write],
            },
        },
    }


def claude_cmd(
    prompt: str,
    *,
    model: str,
    effort: str | None,
    max_turns: int | None,
    tools: str = CLAUDE_TOOLS,
    permission_mode: str | None = None,
) -> list[str]:
    mode = permission_mode or CLAUDE_PERMISSION_MODE
    if mode not in PERMISSION_MODES:
        raise ValueError(f"permission mode must be one of {PERMISSION_MODES}, not {mode!r}")
    cmd = [
        CLAUDE_BIN,
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--include-hook-events",
        "--model",
        model,
        "--permission-mode",
        mode,
        "--permission-prompts",
        "none",
        "--tools",
        tools,
        *CLAUDE_ISOLATION_FLAGS,
    ]
    if effort:
        cmd += ["--effort", effort]
    if max_turns:
        cmd += ["--max-turns", str(max_turns)]
    return [*cmd, "--", prompt]


def claude_env(bench: Bench, bench_dir: Path, *, port: int) -> dict[str, str]:
    env = base_env(bench, bench_dir, port=port)
    env.update(CLAUDE_ISOLATION_ENV)
    return env


# ── Codex ───────────────────────────────────────────────────────────────────

CODEX_FEATURES_OFF = (
    "apps",
    "browser_use",
    "browser_use_external",
    "computer_use",
    "image_generation",
    "in_app_browser",
    "multi_agent",
    "plugins",
    "remote_plugin",
    "hooks",
    "memories",
)

# Codex installs these into $CODEX_HOME/skills/.system on first run; they would compete with the
# skill under test.
CODEX_SYSTEM_SKILLS = (
    "imagegen",
    "openai-docs",
    "plugin-creator",
    "skill-creator",
    "skill-installer",
)


def codex_readable_install(denied: list[Path] | tuple[Path, ...]) -> Path | None:
    """The Codex installation, when it lies inside a denied directory: Codex re-executes its own
    binary inside the sandbox (the helper that reads AGENTS.md at session start), so the
    standalone installer's ``~/.codex/packages`` must stay readable while the rest of ``~/.codex``
    (the login, sessions, history) is denied. The more specific ``read`` rule wins in Codex's
    permission profile. Only the top-level entry under the denied directory that holds the
    binary is opened, and never the login (``auth.json``)."""
    try:
        real = Path(shutil.which(CODEX_BIN) or CODEX_BIN).resolve()
    except OSError:
        return None
    for d in denied:
        d = Path(d).resolve()
        if d in real.parents:
            top = d / real.relative_to(d).parts[0]
            return None if top.name == "auth.json" or top == real else top
    return None


def codex_config_toml(bench: Bench, codex_home: Path, *, model: str, effort: str) -> str:
    """Scratch ``config.toml`` with the ``rollout`` permission profile: workspace writes, network
    only to PyPI through Codex's proxy, local binding for the project's own server, ``/tmp``
    read-only, credentials and the Codex login unreadable (verified in DESIGN section 3.2)."""
    deny = [str(p) for p in no_read(bench)]
    deny.append(str(Path.home() / ".codex" / "auth.json"))
    deny.append(str(codex_home.resolve()))
    lines = [
        "# Scratch CODEX_HOME for gac-bench rollouts: nothing from the developer's ~/.codex.",
        f'model = "{model}"',
        f'model_reasoning_effort = "{effort}"',
        'approval_policy = "never"',
        'web_search = "disabled"',
        'default_permissions = "rollout"',
        "",
    ]
    for name in CODEX_SYSTEM_SKILLS:
        lines += ["[[skills.config]]", f'name = "{name}"', "enabled = false", ""]
    lines.append("[features]")
    lines += [f"{f} = false" for f in CODEX_FEATURES_OFF]
    lines += [
        "network_proxy = true",
        "",
        "[permissions.rollout]",
        'extends = ":workspace"',
        "",
        "[permissions.rollout.network]",
        "enabled = true",
        "allow_local_binding = true",
        "",
        "[permissions.rollout.network.domains]",
        *[f'"{d}" = "allow"' for d in ALLOWED_DOMAINS],
        "",
        "[permissions.rollout.filesystem]",
        '":slash_tmp" = "read"',
        *[f'"{d}" = "deny"' for d in dict.fromkeys(deny)],
    ]
    install = codex_readable_install([Path(d) for d in deny])
    if install is not None:
        lines.append(f'"{install}" = "read"')
    return "\n".join(lines) + "\n"


def prepare_codex_home(bench: Bench, codex_home: Path, *, model: str, effort: str) -> None:
    """A scratch CODEX_HOME logged in with the API key, read from the key file and passed to
    ``codex login --with-api-key`` on stdin: never on a command line, in an environment or in
    output."""
    codex_home.mkdir(parents=True, exist_ok=True)
    (codex_home / "config.toml").write_text(
        codex_config_toml(bench, codex_home, model=model, effort=effort)
    )
    if (codex_home / "auth.json").exists():
        return
    kf = key_file()
    if kf is None or not kf.is_file():
        raise SystemExit(
            "Codex rollouts need GAC_SKILLOPT_OPENAI_KEY_FILE (one OPENAI_API_KEY= line)"
        )
    key = ""
    for line in kf.read_text().splitlines():
        if line.startswith("OPENAI_API_KEY="):
            key = line.split("=", 1)[1].strip().strip('"').strip("'")
    if not key:
        raise SystemExit(f"no OPENAI_API_KEY line in {kf}")
    env = {k: os.environ[k] for k in ENV_ALLOW if k in os.environ}
    env["CODEX_HOME"] = str(codex_home)
    env["HOME"] = str(codex_home.parent / "home")
    Path(env["HOME"]).mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [CODEX_BIN, "login", "--with-api-key"],
        input=key + "\n",
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    del key
    if proc.returncode != 0:
        raise SystemExit(f"codex login failed: exit {proc.returncode}")
    os.chmod(codex_home / "auth.json", 0o600)


def forget_codex_login(codex_home: Path) -> None:
    """Delete the scratch login (it holds the API key) when a run ends."""
    (codex_home / "auth.json").unlink(missing_ok=True)


def codex_env(bench: Bench, bench_dir: Path, codex_home: Path, *, port: int) -> dict[str, str]:
    """Codex also discovers user skills in ``$HOME/.agents/skills``: HOME is scratch."""
    env = base_env(bench, bench_dir, port=port, home=bench_dir / "home")
    env["CODEX_HOME"] = str(codex_home)
    return env


def codex_cmd(
    prompt: str, *, work_dir: Path, model: str, effort: str | None, last_message: Path
) -> list[str]:
    """No ``--sandbox``: that flag selects the legacy settings; the profile applies without it."""
    cmd = [
        CODEX_BIN,
        "exec",
        "--json",
        "--skip-git-repo-check",
        "--color",
        "never",
        "-C",
        str(work_dir),
        "-m",
        model,
        "--output-last-message",
        str(last_message),
        "--ephemeral",
        "--ignore-rules",
    ]
    if effort:
        cmd += ["-c", f'model_reasoning_effort="{effort}"']
    return [*cmd, "--", prompt]


def write_claude_settings(bench: Bench, work: Path) -> Path:
    path = work / ".claude" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(claude_settings(bench, work), indent=2) + "\n")
    return path
