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

"""One rollout's workspace: the fixture, the installed skill, snapshots, and cleanup.

Layout (``W`` = ``<workspace root>/<run>/<slot>-<task>``, the agent's working directory)::

    W/<project>/           the project (built by the fixture, or created by the agent)
    W/.claude/, W/.agents/ the skill under test (and Claude Code's sandbox settings)
    W/.bench/              temp files, uv cache, the CLI's home, an empty kubeconfig, helm dirs,
                           and (after the agent exits) the hidden verifier files

The fixture is built outside any sandbox with the scratch CLI build and the shared warm uv cache;
the agent then gets an APFS clone of that cache (``cp -c``), so an ``install`` inside the
sandbox needs no download.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import isolation
from .paths import Bench
from .tasks import Task, render_skill, skill_source

# Not part of a project's content for snapshots and `unchanged` checks.
SNAPSHOT_SKIP = {
    ".venv",
    "__pycache__",
    ".ruff_cache",
    ".pytest_cache",
    "artifacts",
    ".graph-agents-cli",
}


class FixtureError(RuntimeError):
    """The fixture could not be built: an infrastructure failure, not the agent's."""


@dataclass
class Workspace:
    task: Task
    root: Path
    slot: int
    snapshot: dict[str, str] = field(default_factory=dict)
    skill_files: dict[str, str] = field(default_factory=dict)

    @property
    def bench_dir(self) -> Path:
        return self.root / ".bench"

    @property
    def project(self) -> Path:
        return self.root / self.task.project_name

    @property
    def hidden(self) -> Path:
        return self.bench_dir / "hidden"


def file_hashes(root: Path, skip: set[str] = SNAPSHOT_SKIP) -> dict[str, str]:
    out: dict[str, str] = {}
    if not root.is_dir():
        return out
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in skip)
        for name in sorted(filenames):
            path = Path(dirpath) / name
            if path.is_symlink() or not path.is_file():
                continue
            out[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def _dest_name(part: str) -> str:
    return "." + part[4:] if part.startswith("dot_") else part


def copy_overlay(src: Path, dest: Path) -> list[str]:
    """Copy ``src`` into ``dest``; ``dot_x`` path parts become ``.x``. Returns written paths."""
    written = []
    if not src.is_dir():
        return written
    for path in sorted(src.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        rel = Path(*[_dest_name(p) for p in path.relative_to(src).parts])
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        written.append(str(rel))
    return written


def _sh(
    cmd: str, *, cwd: Path, env: dict[str, str], timeout: int = 600
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/bash", "-c", cmd],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        stdin=subprocess.DEVNULL,
    )


def harness_env(bench: Bench, ws: Workspace, *, port: int) -> dict[str, str]:
    """The environment of the harness's own commands in a workspace (fixture, verifier,
    scripted solutions): the rollout's settings with a scratch HOME, no sandbox."""
    return isolation.base_env(bench, ws.bench_dir, port=port, home=ws.bench_dir / "home")


def _write_env_file(project: Path, overrides: dict[str, str]) -> None:
    """``.env`` from ``.env.example`` with ``overrides`` applied (appended when absent)."""
    example = project / ".env.example"
    lines = example.read_text().splitlines() if example.exists() else []
    done = set()
    out = []
    for line in lines:
        match = re.match(r"^([A-Z][A-Z0-9_]*)=", line)
        if match and match.group(1) in overrides:
            key = match.group(1)
            out.append(f"{key}={overrides[key]}")
            done.add(key)
        else:
            out.append(line)
    out += [f"{k}={v}" for k, v in overrides.items() if k not in done]
    (project / ".env").write_text("\n".join(out) + "\n")


def _vendor_charts(bench: Bench, project: Path, env: dict[str, str]) -> None:
    """Chart dependencies into ``charts/`` from a scratch cache (``deploy --dry-run`` would
    otherwise fetch them from registry-1.docker.io, which the sandboxes do not reach)."""
    for chart in sorted((project / "deployment" / "helm").glob("*/Chart.yaml")):
        text = chart.read_text()
        if "dependencies:" not in text:
            continue
        deps = text[text.index("dependencies:") :]
        key = hashlib.sha256(deps.encode()).hexdigest()[:16]
        cached = bench.chart_cache / key
        chart_dir = chart.parent
        if not (cached / "Chart.lock").exists():
            proc = _sh(f"helm dependency build {chart_dir}", cwd=project, env=env, timeout=300)
            if proc.returncode != 0:
                raise FixtureError(f"helm dependency build failed: {proc.stderr[-500:]}")
            tmp = cached.with_name(key + f".tmp{os.getpid()}")
            shutil.rmtree(tmp, ignore_errors=True)
            shutil.copytree(chart_dir / "charts", tmp / "charts")
            shutil.copy2(chart_dir / "Chart.lock", tmp / "Chart.lock")
            try:
                tmp.rename(cached)
            except OSError:
                shutil.rmtree(tmp, ignore_errors=True)  # another slot cached it first
        else:
            shutil.copytree(cached / "charts", chart_dir / "charts", dirs_exist_ok=True)
            shutil.copy2(cached / "Chart.lock", chart_dir / "Chart.lock")


def build(bench: Bench, task: Task, run_dir_name: str, slot: int, tag: str = "") -> Workspace:
    """Create the workspace and its fixture. Raises ``FixtureError`` on an infrastructure
    failure."""
    label = f"{slot:02d}-{task.id}{'-' + tag if tag else ''}"
    root = bench.workspace_root / run_dir_name / label
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    ws = Workspace(task=task, root=root, slot=slot)
    env = harness_env(bench, ws, port=bench.verifier_port(slot))
    env["UV_CACHE_DIR"] = str(bench.uv_cache)  # the shared warm cache, outside the sandbox
    fixture = task.fixture
    try:
        if fixture["kind"] == "project":
            args = [
                "graph-agents-cli",
                "create",
                task.project_name,
                "-o",
                str(root),
                "-y",
                "--skip-checks",
            ]
            args += [str(a) for a in fixture.get("create_args", [])]
            proc = subprocess.run(
                args,
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
                timeout=300,
                stdin=subprocess.DEVNULL,
            )
            if proc.returncode != 0:
                raise FixtureError(
                    f"create failed ({proc.returncode}): {(proc.stdout + proc.stderr)[-800:]}"
                )
            dotenv = {"MODEL_PROVIDER": "fake", "API_KEY": "dev", **fixture.get("env", {})}
            _write_env_file(ws.project, {k: str(v) for k, v in dotenv.items()})
            copy_overlay(task.dir / "fixture", ws.project)
            for cmd in fixture.get("setup", []):
                proc = _sh(cmd, cwd=ws.project, env=env)
                if proc.returncode != 0:
                    raise FixtureError(
                        f"setup {cmd!r} failed: {(proc.stdout + proc.stderr)[-800:]}"
                    )
            if fixture.get("install", True):
                proc = _sh("graph-agents-cli install", cwd=ws.project, env=env, timeout=600)
                if proc.returncode != 0:
                    raise FixtureError(f"install failed: {(proc.stdout + proc.stderr)[-800:]}")
            if fixture.get("vendor_charts", True):
                _vendor_charts(bench, ws.project, env)
        else:
            copy_overlay(task.dir / "fixture", root)
            for cmd in fixture.get("setup", []):
                proc = _sh(cmd, cwd=root, env=env)
                if proc.returncode != 0:
                    raise FixtureError(
                        f"setup {cmd!r} failed: {(proc.stdout + proc.stderr)[-800:]}"
                    )
    except subprocess.TimeoutExpired as exc:
        raise FixtureError(f"fixture timed out: {exc}") from exc
    # The agent's own uv cache: a clone of the warm one (APFS clonefile; about 1.5 s).
    agent_cache = ws.bench_dir / "uv-cache"
    shutil.rmtree(agent_cache, ignore_errors=True)
    if bench.uv_cache.is_dir():
        subprocess.run(["cp", "-cR", str(bench.uv_cache), str(agent_cache)], check=False)
    agent_cache.mkdir(parents=True, exist_ok=True)
    ws.snapshot = file_hashes(ws.project) if fixture["kind"] == "project" else {}
    return ws


def install_skill(bench: Bench, ws: Workspace, *, harness: str, body: str | None) -> Path:
    """The skill under test as a native skill of the harness: ``SKILL.md`` rendered from the
    candidate body (``None``: the shipped one) plus the shipped ``references/``."""
    skill = ws.task.skill
    base = ws.root / (".claude/skills" if harness == "claude" else ".agents/skills") / skill
    if base.exists():
        shutil.rmtree(base)
    src = skill_source(skill)
    shutil.copytree(src, base, ignore=shutil.ignore_patterns(".DS_Store", "__pycache__"))
    if body is not None:
        (base / "SKILL.md").write_text(render_skill(skill, body))
    if harness == "claude":
        isolation.write_claude_settings(bench, ws.root)
    ws.skill_files = integrity_hashes(ws, harness)
    return base


def integrity_hashes(ws: Workspace, harness: str) -> dict[str, str]:
    """The files a rollout must not change: the skill and Claude Code's settings."""
    base = ".claude/skills" if harness == "claude" else ".agents/skills"
    root = ws.root / base / ws.task.skill
    out = {f"{base}/{ws.task.skill}/{rel}": d for rel, d in file_hashes(root, skip=set()).items()}
    settings = ws.root / ".claude" / "settings.json"
    if harness == "claude" and settings.exists():
        out[".claude/settings.json"] = hashlib.sha256(settings.read_bytes()).hexdigest()
    return out


def listening_pids(port: int) -> list[int]:
    proc = subprocess.run(
        ["lsof", "-t", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"], capture_output=True, text=True
    )
    return [int(p) for p in proc.stdout.split() if p.strip().isdigit()]


def _cmdline(pid: int) -> str:
    return subprocess.run(
        ["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True
    ).stdout


# Characters of a leftover's command line kept in its note: enough to tell a uvicorn server
# from a stray `uv run` or a test process.
ARGV_CHARS = 300


def leftover_note(pid: int, argv: str, workspace: Path | None = None) -> str:
    """``stopped leftover pid <pid>: <argv>``, the workspace path shortened to ``W``."""
    argv = " ".join(argv.split())
    if workspace is not None:
        argv = argv.replace(str(workspace), "W")
    return f"stopped leftover pid {pid}: {argv[:ARGV_CHARS] or '(exited)'}"


def cleanup_processes(bench: Bench, ws: Workspace) -> list[str]:
    """Stop what a rollout left behind: servers on the slot's ports and any process whose
    command line is inside the workspace. Never touches a process outside it. Each note names
    the process's command line, so the report can say where a leftover came from."""
    notes = []
    marker = str(ws.root)
    pids: set[int] = set()
    for port in (bench.agent_port(ws.slot), bench.verifier_port(ws.slot)):
        for pid in listening_pids(port):
            if marker in _cmdline(pid):
                pids.add(pid)
            else:
                notes.append(f"port {port} held by pid {pid} outside the workspace (left alone)")
    found = subprocess.run(["pgrep", "-f", marker], capture_output=True, text=True).stdout
    pids.update(int(p) for p in found.split() if p.strip().isdigit() and int(p) != os.getpid())
    for pid in sorted(pids):
        argv = _cmdline(pid)  # before the signal: afterwards the process may be gone
        try:
            os.kill(pid, signal.SIGTERM)
            notes.append(leftover_note(pid, argv, ws.root))
        except ProcessLookupError:
            pass
    if pids:
        time.sleep(1.0)
        for pid in pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    return notes


def remove(ws: Workspace) -> None:
    shutil.rmtree(ws.root, ignore_errors=True)
    parent = ws.root.parent
    try:
        parent.rmdir()  # the run directory, once its last workspace is gone
    except OSError:
        pass
