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

"""Isolated environments for SkillOpt rollouts on Claude Code and Codex (spike).

Contributor tooling only: nothing here is imported by the CLI or by generated projects.

Both harnesses start from an allowlisted environment, never from the caller's: a rollout
launched from inside another agent session must not inherit that session's variables
(``CLAUDECODE``, ``CLAUDE_CODE_SESSION_ID``, messaging sockets and tokens, provider keys,
``KUBECONFIG``).

Claude Code (verified on 2.1.283, see ``../DESIGN.md``): the developer's plan authenticates through
the user config (OAuth in the macOS keychain); a scratch ``CLAUDE_CONFIG_DIR`` answers "Not
logged in". Isolation is by flags and variables instead: only the ``project`` setting source
(drops user hooks, enabled and synced plugins, user skills), an empty ``--mcp-config`` with
``--strict-mcp-config``, hooks disabled, claude.ai connectors off, Claude Code's bundled
skills, workflows, doctor and built-in agents off, auto-memory off, no session persistence,
and a workspace outside the home tree (no ancestor ``CLAUDE.md``/``AGENTS.md``). The skill
under test is a project skill in the workspace (``.claude/skills/<name>/SKILL.md``). The Bash
sandbox comes from the workspace's ``.claude/settings.json`` (``claude_project_settings``).

Codex: a scratch ``CODEX_HOME`` holding only an API-key login (``codex login --with-api-key``
reading the key on stdin) and a generated ``config.toml`` (system skills, plugins, apps,
browser/computer use, hooks and memories off); a scratch ``HOME`` so ``~/.agents/skills`` is
not discovered; the developer's ``~/.codex`` (config, notify hook, trusted projects, AGENTS.md,
skills) is never read.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

# File holding one line OPENAI_API_KEY=... for Codex's API-key login. The key is read only by
# prepare_codex_home and passed to `codex login --with-api-key` on stdin.
_KEY_FILE_ENV = os.environ.get("GAC_SKILLOPT_OPENAI_KEY_FILE", "")
KEY_FILE = (
    Path(_KEY_FILE_ENV) if _KEY_FILE_ENV else Path("/nonexistent/GAC_SKILLOPT_OPENAI_KEY_FILE")
)

# Resolved once from the caller's PATH; the rollout's own PATH does not contain them.
CLAUDE_BIN = os.environ.get("GAC_SKILLOPT_CLAUDE_BIN") or shutil.which("claude") or "claude"
CODEX_BIN = os.environ.get("GAC_SKILLOPT_CODEX_BIN") or shutil.which("codex") or "codex"

# Variables a CLI needs to run; everything else is dropped.
ENV_ALLOW = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "TMPDIR",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TERM",
    "__CF_USER_TEXT_ENCODING",
    "SSL_CERT_FILE",
    "UV_CACHE_DIR",
)


# System and Homebrew directories only: the developer's ~/.local/bin holds google-agents-cli and
# agents-cli (confounds), and plugin bin directories must not reach a rollout either.
SYSTEM_PATH = ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin")


def rollout_path(scratch_bin: Path | None) -> str:
    """PATH for a rollout: the scratch bin (graph-agents-cli build, uv, uvx) first."""
    parts = [str(scratch_bin)] if scratch_bin else []
    return ":".join(parts + list(SYSTEM_PATH))


def base_env(scratch: Path, *, scratch_bin: Path | None = None) -> dict[str, str]:
    """Allowlisted environment plus the scratch-only safety variables every rollout gets."""
    env = {k: os.environ[k] for k in ENV_ALLOW if k in os.environ}
    env["PATH"] = rollout_path(scratch_bin)
    kubeconfig = scratch / "kubeconfig-empty"
    kubeconfig.parent.mkdir(parents=True, exist_ok=True)
    kubeconfig.touch(exist_ok=True)
    env.update(
        {
            "KUBECONFIG": str(kubeconfig),
            "GRAPH_AGENTS_CLI_NO_UPDATE_CHECK": "1",
            # Generated projects run their model calls on the fake provider.
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
    # Claude Code's own bundled skills (verify, debug, run, code-review, claude-api, ...) are
    # not the developer's, but they would still compete with the skill under test.
    "CLAUDE_CODE_DISABLE_BUNDLED_SKILLS": "1",
    "CLAUDE_CODE_DISABLE_CLAUDE_API_SKILL": "1",
    "CLAUDE_CODE_DISABLE_POLICY_SKILLS": "1",
    "CLAUDE_CODE_DISABLE_WORKFLOWS": "1",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    "DISABLE_DOCTOR_COMMAND": "1",
    "DISABLE_BUILTIN_AGENTS": "1",
}


def claude_cmd(
    prompt: str,
    *,
    model: str,
    tools: str = "Read,Write,Edit,Glob,Grep,Bash,Skill",
    isolated: bool = True,
    extra: tuple[str, ...] = (),
    effort: str | None = None,
) -> list[str]:
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
        "bypassPermissions",
        "--tools",
        tools,
    ]
    if effort:
        cmd += ["--effort", effort]
    if isolated:
        cmd += list(CLAUDE_ISOLATION_FLAGS)
    cmd += list(extra)
    cmd += ["--", prompt]
    return cmd


# Where rollout workspaces live. Claude Code's Bash sandbox always lets commands write under its
# own temp root (/private/tmp/claude-<uid>), which contains the whole session scratchpad
# (ledger, other workspaces); so workspaces go outside it, and the scratchpad is write-denied.
WORKSPACE_ROOT = Path("/private/tmp/gac-x-skillopt")

SECRET_DIRS = (
    *((KEY_FILE.parent,) if _KEY_FILE_ENV and KEY_FILE.parent != Path("/") else ()),
    Path.home() / ".ssh",
    Path.home() / ".codex",
    Path.home() / ".kube",
    Path.home() / ".aws",
    Path.home() / ".docker",
)


def claude_project_settings(
    *,
    deny_write: tuple[Path, ...],
    allowed_domains: tuple[str, ...] = ("pypi.org", "files.pythonhosted.org"),
    local_binding: bool = True,
) -> dict:
    """Workspace ``.claude/settings.json`` for a sandboxed rollout (verified on 2.1.283).

    The sandbox only takes effect from a settings file (not from ``--settings`` with
    ``bypassPermissions``); run with ``--permission-mode acceptEdits`` and
    ``--permission-prompts none`` so anything outside it is denied, not prompted.
    """
    return {
        "disableAllHooks": True,
        "sandbox": {
            "enabled": True,
            "autoAllowBashIfSandboxed": True,
            "allowUnsandboxedCommands": False,
            "network": {
                "allowedDomains": list(allowed_domains),
                # eval/run start the project's server on 127.0.0.1:<GRAPH_AGENTS_CLI_RUN_PORT>.
                "allowLocalBinding": local_binding,
            },
            "filesystem": {
                "denyRead": [str(p) for p in SECRET_DIRS],
                "denyWrite": [str(p) for p in deny_write],
            },
        },
    }


def claude_env(
    scratch: Path, *, isolated: bool = True, scratch_bin: Path | None = None
) -> dict[str, str]:
    env = base_env(scratch, scratch_bin=scratch_bin)
    if isolated:
        env.update(CLAUDE_ISOLATION_ENV)
    return env


def parse_stream_json(stdout: str) -> dict:
    """Split Claude Code stream-json output into init, hooks, tool calls and the result."""
    events = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    init = next((e for e in events if e.get("type") == "system" and e.get("subtype") == "init"), {})
    result = next((e for e in reversed(events) if e.get("type") == "result"), {})
    hooks = [e for e in events if "hook" in str(e.get("subtype", "")).lower()]
    tool_calls = []
    for e in events:
        if e.get("type") != "assistant":
            continue
        for block in (e.get("message") or {}).get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                tool_calls.append({"name": block.get("name"), "input": block.get("input")})
    return {
        "events": len(events),
        "init": init,
        "hooks": hooks,
        "tool_calls": tool_calls,
        "result": result,
    }


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


def codex_config_toml(
    *,
    model: str,
    effort: str,
    network: str = "off",
    allowed_domains: tuple[str, ...] = ("pypi.org", "files.pythonhosted.org"),
    codex_home: Path | None = None,
) -> str:
    """Scratch ``config.toml``. ``network``: ``off`` (legacy workspace-write, no network),
    ``on`` (legacy workspace-write, unrestricted network) or ``proxy`` (permission profile:
    workspace writes, network only to ``allowed_domains`` through Codex's proxy, local binding
    for the project's own server). Profiles and ``sandbox_mode`` do not compose, and
    ``codex exec --sandbox`` selects the legacy settings, so ``proxy`` runs omit it."""
    # Sandboxed commands must not read credentials: the developer's secrets, the developer's Codex login
    # and this scratch CODEX_HOME (its auth.json holds the API key). ~/.codex itself stays
    # readable: Codex puts its bundled tools (rg) on the command PATH from there.
    codex_deny_read = [str(p) for p in SECRET_DIRS if p.name != ".codex"]
    codex_deny_read.append(str(Path.home() / ".codex" / "auth.json"))
    if codex_home is not None:
        codex_deny_read.append(str(codex_home))
    lines = [
        "# Scratch CODEX_HOME for SkillOpt rollouts: nothing from the developer's ~/.codex.",
        f'model = "{model}"',
        f'model_reasoning_effort = "{effort}"',
        'approval_policy = "never"',
        'web_search = "disabled"',
    ]
    if network == "proxy":
        lines.append('default_permissions = "rollout"')
    else:
        lines.append('sandbox_mode = "workspace-write"')
    lines.append("")
    # Codex installs its own system skills into $CODEX_HOME/skills/.system on first run; they
    # would compete with the skill under test.
    for name in CODEX_SYSTEM_SKILLS:
        lines += ["[[skills.config]]", f'name = "{name}"', "enabled = false", ""]
    lines.append("[features]")
    lines += [f"{f} = false" for f in CODEX_FEATURES_OFF]
    if network == "proxy":
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
        ]
        lines += [f'"{d}" = "allow"' for d in allowed_domains]
        # ":workspace" also makes /tmp and $TMPDIR writable; TMPDIR points inside the workspace
        # and /tmp (which holds the session scratchpad, the CLI build and uv) is read-only
        # ("deny" would also stop the rollout executing the scratch graph-agents-cli and uv).
        # Credential directories are denied outright.
        lines += ["", "[permissions.rollout.filesystem]", '":slash_tmp" = "read"']
        lines += [f'"{d}" = "deny"' for d in codex_deny_read]
    else:
        lines += [
            "",
            "[sandbox_workspace_write]",
            f"network_access = {'true' if network == 'on' else 'false'}",
            # workspace-write also allows /tmp and $TMPDIR by default; a rollout writes only in
            # its workspace (TMPDIR is pointed inside it).
            "exclude_slash_tmp = true",
            "exclude_tmpdir_env_var = true",
        ]
    return "\n".join(lines) + "\n"


CODEX_SYSTEM_SKILLS = (
    "imagegen",
    "openai-docs",
    "plugin-creator",
    "skill-creator",
    "skill-installer",
)


def prepare_codex_home(
    codex_home: Path,
    key_file: Path,
    *,
    model: str,
    effort: str = "medium",
    network: str | bool = "off",
) -> None:
    """Create a scratch CODEX_HOME logged in with the API key (read from the key file on stdin).

    The key never appears on a command line, in the environment of this process, or in output.
    """
    codex_home.mkdir(parents=True, exist_ok=True)
    if isinstance(network, bool):
        network = "on" if network else "off"
    (codex_home / "config.toml").write_text(
        codex_config_toml(
            model=model, effort=effort, network=network, codex_home=codex_home.resolve()
        )
    )
    if (codex_home / "auth.json").exists():
        return
    key = ""
    for line in key_file.read_text().splitlines():
        if line.startswith("OPENAI_API_KEY="):
            key = line.split("=", 1)[1].strip().strip('"').strip("'")
    if not key:
        raise SystemExit(f"no OPENAI_API_KEY line in {key_file}")
    env = base_env(codex_home.parent)
    env["CODEX_HOME"] = str(codex_home)
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


def codex_env(
    scratch: Path,
    codex_home: Path,
    *,
    scratch_bin: Path | None = None,
    home: Path | None = None,
    tmpdir: Path | None = None,
) -> dict[str, str]:
    """Codex environment. ``home`` replaces HOME: Codex also discovers user skills in
    ``$HOME/.agents/skills`` (a developer machine may hold dozens there), independent of CODEX_HOME."""
    env = base_env(scratch, scratch_bin=scratch_bin)
    env["CODEX_HOME"] = str(codex_home)
    if tmpdir is not None:
        tmpdir.mkdir(parents=True, exist_ok=True)
        env["TMPDIR"] = str(tmpdir)
    if home is not None:
        home.mkdir(parents=True, exist_ok=True)
        env["HOME"] = str(home)
    return env


def codex_cmd(
    prompt: str,
    *,
    work_dir: Path,
    model: str,
    last_message: Path,
    effort: str | None = None,
    profile: bool = False,
) -> list[str]:
    """``profile=True`` for a CODEX_HOME written with ``network="proxy"`` (no ``--sandbox``)."""
    cmd = [
        CODEX_BIN,
        "exec",
        "--json",
        "--skip-git-repo-check",
        "--color",
        "never",
    ]
    if not profile:
        cmd += ["--sandbox", "workspace-write"]
    cmd += [
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
    cmd += ["--", prompt]
    return cmd


def parse_codex_jsonl(stdout: str) -> dict:
    events = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    usage = {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0}
    for e in events:
        if e.get("type") == "turn.completed":
            for k in usage:
                usage[k] += int((e.get("usage") or {}).get(k, 0) or 0)
    items = [e.get("item") for e in events if e.get("type") == "item.completed"]
    return {
        "events": len(events),
        "types": sorted({e.get("type") for e in events}),
        "usage": usage,
        "items": items,
    }
