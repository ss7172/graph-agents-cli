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

"""The deterministic verifier: check types and scoring (DESIGN section 5.3).

Runs after the agent (or a scripted solution) exits, outside any sandbox, in the workspace with
the fake model provider, the slot's verifier port and an empty kubeconfig. No check calls a model
or touches a cluster. Every check has:

- ``id``, ``type``; ``mandatory`` (default true) and ``weight`` (default 1);
- ``cwd``: relative to the workspace (default: the task's project directory);
- ``why``: one line saying what the check proves (shown to the analyst on failure).

Types (keys beyond the common ones):

- ``cmd``: ``run`` (bash), ``expect_exit`` (list, default [0]), ``output_regex`` /
  ``output_not_regex`` on stdout+stderr, ``timeout_s``.
- ``file``: ``path`` (glob), ``exists`` (default true), ``regex`` (some matching file
  matches), ``not_regex`` (no matching file matches).
- ``json`` / ``yaml`` / ``dotenv``: ``file`` and ``expr``, a Python expression over ``data``
  (helpers: ``get(data, "a.b.0")``, ``re``, ``json``) that must be truthy.
- ``unchanged``: ``paths`` (files or directories) byte-identical to the fixture;
  ``except`` lists paths below them that may change.
- ``pyfile``: ``script`` (under ``hidden/``) run as ``uv run python <script>`` in the project;
  exit 0 passes.
- ``eval``: ``graph-agents-cli eval run`` (``args``), ``expect_exit``, and on the newest
  results file ``summary`` (a subset such as ``{"failed": 0}``), ``statuses`` (case id ->
  status), ``min_planned``.
- ``transcript``: ``must_run`` / ``must_not_run`` (regexes matched at the start of each simple
  command the agent ran, see ``command_segments``),
  ``final_regex`` / ``final_not_regex`` (regexes over its final message, case-insensitive).

``hard`` is 1 when every mandatory check passes; ``soft`` is the weighted fraction passed.
"""

from __future__ import annotations

import fnmatch
import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .trace import Trace
from .workspace import SNAPSHOT_SKIP, Workspace, file_hashes

OUTPUT_TAIL = 1500
SAFE_BUILTINS = {
    f.__name__: f
    for f in (
        any,
        all,
        len,
        set,
        sorted,
        str,
        int,
        float,
        list,
        dict,
        tuple,
        bool,
        min,
        max,
        sum,
        isinstance,
        enumerate,
    )
}


@dataclass
class CheckResult:
    id: str
    type: str
    passed: bool
    mandatory: bool
    weight: float
    detail: str
    output: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "passed": self.passed,
            "mandatory": self.mandatory,
            "weight": self.weight,
            "detail": self.detail,
            "output": self.output[-OUTPUT_TAIL:],
        }


def get(data: Any, path: str, default: Any = None) -> Any:
    """``get(data, "a.b.0.c")``: dict keys and list indexes; ``default`` when absent."""
    node = data
    for part in [p for p in path.split(".") if p]:
        if isinstance(node, dict) and part in node:
            node = node[part]
        elif (
            isinstance(node, list)
            and part.lstrip("-").isdigit()
            and -len(node) <= int(part) < len(node)
        ):
            node = node[int(part)]
        else:
            return default
    return node


def parse_dotenv(text: str) -> dict[str, str]:
    """``KEY=value`` lines (``export`` and surrounding quotes allowed); comments ignored."""
    out = {}
    for line in text.splitlines():
        match = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$", line)
        if not match or line.lstrip().startswith("#"):
            continue
        value = match.group(2)
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        out[match.group(1)] = value
    return out


_SEPARATORS = re.compile(r"&&|\|\||[;|\n]|\$\(|`")
# `uv run` options that take a value as the next word; every other option is a flag. Without
# the list, `uv run --quiet graph-agents-cli` and `uv run --project x graph-agents-cli` cannot
# both be read right.
_UV_RUN_VALUE_OPTS = (
    "project|directory|with|with-editable|with-requirements|env-file|package|python|extra|group"
    "|only-group|no-group|index|default-index|index-url|extra-index-url|find-links|cache-dir"
    "|config-file|color|link-mode|refresh-package|reinstall-package|upgrade-package"
    "|exclude-newer|prerelease|resolution|index-strategy|keyring-provider|python-platform"
)
_UV_RUN = (
    r"uv\s+run\s+(?:(?:--(?:" + _UV_RUN_VALUE_OPTS + r")\s+(?!-)\S+|-[pPif]\s+(?!-)\S+"
    r"|--[A-Za-z][-A-Za-z0-9]*=\S+|-[A-Za-z]+|--[A-Za-z][-A-Za-z0-9]*)\s+)*(?:--\s+)?"
)
_PREFIX = re.compile(
    r"^(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+"
    r"|env(?:\s+(?:-u\s*\S+|--unset[= ]\S+|-i|--ignore-environment|-))*\s+"
    # `uv run graph-agents-cli eval run` runs the CLI from PATH in the project's environment.
    r"|" + _UV_RUN + r"|(?:time|nohup|command|exec|sudo)\s+|timeout\s+\S+\s+|\(\s*"
    # Shell keywords that start a compound command's body: `if X; then Y; fi`, `do Y; done`.
    r"|(?:if|then|else|elif|while|until|do|!)\s+)+"
)
# A program named by its path (`/opt/x/bin/graph-agents-cli eval run`): keep its name only.
_PROGRAM_DIR = re.compile(r"^[^\s/]*(?:/[^\s/]*)*/(?=[^\s/]+(?:\s|$))")


_HEREDOC = re.compile(r"<<(-?)\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\2")


def _split_shell(command: str) -> list[str] | None:
    """Split a shell line at the operators that start a new simple command (``&&``, ``||``,
    ``;``, ``|``, newlines, ``$(`` and backticks), outside quotes only: a quoted ``'a|helm
    upgrade'`` is one argument (a Codex rollout's ``rg -n 'secrets apply|helm upgrade'`` was
    scored as running ``helm upgrade``). Inside double quotes only a command substitution
    starts a command. Comments and here-document bodies are dropped. ``None`` when the quotes
    do not balance, so the caller can fall back to the plain split."""
    parts: list[str] = []
    buf: list[str] = []
    heredocs: list[tuple[str, bool]] = []
    quote = ""
    i, n = 0, len(command)

    def cut() -> None:
        parts.append("".join(buf))
        buf.clear()

    while i < n:
        ch = command[i]
        if quote == "'":
            buf.append(ch)
            quote = "" if ch == "'" else quote
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            buf.append(command[i : i + 2])
            i += 2
            continue
        if quote == '"':
            if ch == '"':
                quote = ""
                buf.append(ch)
                i += 1
            elif command.startswith("$(", i) or ch == "`":
                cut()
                i += 2 if ch == "$" else 1
            else:
                buf.append(ch)
                i += 1
            continue
        if ch in "'\"":
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch == "#" and (not buf or buf[-1].isspace()):
            end = command.find("\n", i)
            i = n if end < 0 else end
            continue
        if ch == "<" and command.startswith("<<", i) and not command.startswith("<<<", i):
            m = _HEREDOC.match(command, i)
            if m:
                heredocs.append((m.group(3), m.group(1) == "-"))
                buf.append(m.group(0))
                i = m.end()
                continue
        if ch == "\n" and heredocs:
            cut()
            i += 1
            for word, strip_tabs in heredocs:  # skip each body up to its delimiter line
                while i < n:
                    end = command.find("\n", i)
                    line = command[i : n if end < 0 else end]
                    i = n if end < 0 else end + 1
                    if (line.lstrip("\t") if strip_tabs else line) == word:
                        break
            heredocs.clear()
            continue
        for sep in ("&&", "||", "$(", ";", "|", "\n", "`"):
            if command.startswith(sep, i):
                cut()
                i += len(sep)
                break
        else:
            buf.append(ch)
            i += 1
    if quote:
        return None
    cut()
    return parts


def command_segments(command: str) -> list[str]:
    """The simple commands of a shell line, each starting with its program name: split at
    ``&&``, ``||``, ``;``, ``|``, newlines and command substitutions outside quotes
    (``_split_shell``; the plain split when the quotes do not balance); leading variable
    assignments and wrappers (``timeout 60``, ``env -u X``, ``uv run --quiet``, ...) dropped,
    and a program named by its path reduced to its name. Transcript patterns match at the start
    of a segment, so text inside an ``echo``, a quoted pattern or a here-document is never a
    command, and ``/path/to/graph-agents-cli deploy`` or ``uv run graph-agents-cli deploy`` is
    matched like ``graph-agents-cli deploy`` (by ``must_run`` and ``must_not_run``)."""
    out = []
    pieces = _split_shell(command)
    for part in pieces if pieces is not None else _SEPARATORS.split(command):
        part = part.strip()
        while True:
            stripped = _PROGRAM_DIR.sub("", _PREFIX.sub("", part), count=1)
            if stripped == part:
                break
            part = stripped
        if part:
            out.append(part)
    return out


def _run(cmd: str, *, cwd: Path, env: dict[str, str], timeout: int) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            ["/bin/bash", "-c", cmd],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
        return proc.returncode, proc.stdout + proc.stderr
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or b"") + (exc.stderr or b"")
        text = out.decode(errors="replace") if isinstance(out, bytes) else str(out)
        return 124, text + f"\n[timed out after {timeout}s]"


class Verifier:
    def __init__(self, ws: Workspace, env: dict[str, str], trace: Trace) -> None:
        self.ws = ws
        self.env = env
        self.trace = trace

    def run(self, checks: list[dict[str, Any]]) -> list[CheckResult]:
        hidden_src = self.ws.task.dir / "hidden"
        if hidden_src.is_dir():
            shutil.rmtree(self.ws.hidden, ignore_errors=True)
            shutil.copytree(hidden_src, self.ws.hidden)
        results = []
        for check in checks:
            try:
                passed, detail, output = getattr(self, f"_{check['type']}")(check)
            except Exception as exc:  # a broken check is a failed check, with the reason
                passed, detail, output = False, f"check error: {type(exc).__name__}: {exc}", ""
            why = check.get("why")
            if not passed and why:
                detail = f"{why} -- {detail}"
            results.append(
                CheckResult(
                    id=check["id"],
                    type=check["type"],
                    passed=bool(passed),
                    mandatory=bool(check.get("mandatory", True)),
                    weight=float(check.get("weight", 1)),
                    detail=detail,
                    output=output,
                )
            )
        return results

    def _cwd(self, check: dict[str, Any]) -> Path:
        rel = check.get("cwd")
        if rel is None:
            return self.ws.project
        return (self.ws.root / rel).resolve()

    # ── check types ──

    def _cmd(self, check: dict[str, Any]) -> tuple[bool, str, str]:
        cwd = self._cwd(check)
        if not cwd.is_dir():
            return False, f"{cwd.name}/ does not exist", ""
        code, out = _run(
            check["run"], cwd=cwd, env=self.env, timeout=int(check.get("timeout_s", 300))
        )
        expect = check.get("expect_exit", [0])
        if code not in expect:
            return False, f"`{check['run']}` exited {code}, expected {expect}", out
        if check.get("output_regex") and not re.search(check["output_regex"], out, re.M):
            return False, f"`{check['run']}` output lacks /{check['output_regex']}/", out
        if check.get("output_not_regex") and re.search(check["output_not_regex"], out, re.M):
            return False, f"`{check['run']}` output matches /{check['output_not_regex']}/", out
        return True, f"`{check['run']}` exited {code}", out

    def _file(self, check: dict[str, Any]) -> tuple[bool, str, str]:
        cwd = self._cwd(check)
        matches = sorted(p for p in cwd.glob(check["path"]) if p.is_file()) if cwd.is_dir() else []
        if not check.get("exists", True):
            if matches:
                return (
                    False,
                    f"{check['path']} exists ({', '.join(str(m.relative_to(cwd)) for m in matches[:3])})",
                    "",
                )
            return True, f"{check['path']} is absent", ""
        if not matches:
            return False, f"no file matches {check['path']}", ""
        if check.get("regex"):
            hits = [
                m for m in matches if re.search(check["regex"], m.read_text(errors="replace"), re.M)
            ]
            if not hits:
                return False, f"{check['path']} does not match /{check['regex']}/", ""
        if check.get("not_regex"):
            hits = [
                m
                for m in matches
                if re.search(check["not_regex"], m.read_text(errors="replace"), re.M)
            ]
            if hits:
                return False, f"{hits[0].relative_to(cwd)} matches /{check['not_regex']}/", ""
        return True, f"{check['path']} ok", ""

    def _structured(self, kind: str, check: dict[str, Any]) -> tuple[bool, str, str]:
        path = self._cwd(check) / check["file"]
        if not path.is_file():
            return False, f"{check['file']} does not exist", ""
        text = path.read_text()
        try:
            if kind == "json":
                data = json.loads(text)
            elif kind == "yaml":
                data = yaml.safe_load(text)
            else:
                data = parse_dotenv(text)
        except Exception as exc:
            return False, f"{check['file']} does not parse as {kind}: {exc}", text[:500]
        # Task files are trusted; names go in globals so comprehensions see them.
        scope = {"__builtins__": SAFE_BUILTINS, "data": data, "get": get, "re": re, "json": json}
        value = eval(check["expr"], scope)
        if not value:
            return False, f"{check['file']}: `{check['expr']}` is false", ""
        return True, f"{check['file']}: `{check['expr']}`", ""

    def _json(self, check: dict[str, Any]) -> tuple[bool, str, str]:
        return self._structured("json", check)

    def _yaml(self, check: dict[str, Any]) -> tuple[bool, str, str]:
        return self._structured("yaml", check)

    def _dotenv(self, check: dict[str, Any]) -> tuple[bool, str, str]:
        return self._structured("dotenv", check)

    def _unchanged(self, check: dict[str, Any]) -> tuple[bool, str, str]:
        base = self.ws.project
        now = file_hashes(base, skip=SNAPSHOT_SKIP)
        before = self.ws.snapshot
        allowed = check.get("except", [])
        changed = []
        for rel in check["paths"]:
            rel = rel.rstrip("/")

            def inside(path: str, rel: str = rel) -> bool:
                return path == rel or path.startswith(rel + "/") or rel in ("", ".")

            keys = {k for k in before if inside(k)} | {k for k in now if inside(k)}
            for key in sorted(keys):
                if any(
                    key == a or key.startswith(a.rstrip("/") + "/") or fnmatch.fnmatch(key, a)
                    for a in allowed
                ):
                    continue
                if before.get(key) != now.get(key):
                    state = (
                        "added" if key not in before else "removed" if key not in now else "changed"
                    )
                    changed.append(f"{key} ({state})")
        if changed:
            return (
                False,
                "changed: " + ", ".join(changed[:8]) + (" ..." if len(changed) > 8 else ""),
                "",
            )
        return True, f"unchanged: {', '.join(check['paths'])}", ""

    def _pyfile(self, check: dict[str, Any]) -> tuple[bool, str, str]:
        script = self.ws.hidden / check["script"]
        if not script.is_file():
            return False, f"hidden script {check['script']} is missing", ""
        cwd = self._cwd(check)
        if not cwd.is_dir():
            return False, f"{cwd.name}/ does not exist", ""
        args = " ".join(str(a) for a in check.get("args", []))
        code, out = _run(
            f"uv run --quiet python {script} {args}",
            cwd=cwd,
            env=self.env,
            timeout=int(check.get("timeout_s", 300)),
        )
        lines = [line for line in out.strip().splitlines() if line.strip()]
        last = lines[-1] if lines else ""
        if code != 0:
            return False, f"{check['script']} failed: {last[:300]}", out
        return True, f"{check['script']}: {last[:200]}", out

    def _eval(self, check: dict[str, Any]) -> tuple[bool, str, str]:
        cwd = self._cwd(check)
        if not cwd.is_dir():
            return False, f"{cwd.name}/ does not exist", ""
        results_dir = cwd / "artifacts" / "grade_results"
        before = set(results_dir.glob("*.json")) if results_dir.is_dir() else set()
        cmd = "graph-agents-cli eval run " + " ".join(check.get("args", []))
        code, out = _run(cmd, cwd=cwd, env=self.env, timeout=int(check.get("timeout_s", 600)))
        expect = check.get("expect_exit", [0])
        if code not in expect:
            return False, f"`{cmd.strip()}` exited {code}, expected {expect}", out
        new = (
            sorted(set(results_dir.glob("*.json")) - before, key=lambda p: p.stat().st_mtime)
            if results_dir.is_dir()
            else []
        )
        if not new and (check.get("summary") or check.get("statuses") or check.get("min_planned")):
            return False, "eval run wrote no results file", out
        if new:
            data = json.loads(new[-1].read_text())
            summary = data.get("summary") or {}
            for key, want in (check.get("summary") or {}).items():
                if summary.get(key) != want:
                    return (
                        False,
                        f"results summary.{key} = {summary.get(key)}, expected {want}",
                        out,
                    )
            statuses = {c.get("id"): c.get("status") for c in data.get("cases", [])}
            for cid, want in (check.get("statuses") or {}).items():
                if statuses.get(cid) != want:
                    return False, f"case {cid} is {statuses.get(cid)}, expected {want}", out
            planned = data.get("planned")
            n_planned = len(planned) if isinstance(planned, list) else int(planned or len(statuses))
            if n_planned < int(check.get("min_planned", 0)):
                return (
                    False,
                    f"{n_planned} planned cases, expected at least {check['min_planned']}",
                    out,
                )
        return True, f"`{cmd.strip()}` exited {code}", out

    def _transcript(self, check: dict[str, Any]) -> tuple[bool, str, str]:
        segments = [s for c in self.trace.commands for s in command_segments(c)]
        for pattern in check.get("must_run", []):
            if not any(re.match(pattern, s) for s in segments):
                return False, f"the agent never ran a command matching /{pattern}/", ""
        for pattern in check.get("must_not_run", []):
            hits = [s for s in segments if re.match(pattern, s)]
            if hits:
                return False, f"the agent ran /{pattern}/: {hits[0][:200]}", ""
        final = self.trace.final or ""
        for pattern in check.get("final_regex", []):
            if not re.search(pattern, final, re.I | re.S):
                return False, f"the final answer lacks /{pattern}/", final[-600:]
        for pattern in check.get("final_not_regex", []):
            if re.search(pattern, final, re.I | re.S):
                return False, f"the final answer matches /{pattern}/", final[-600:]
        return True, "transcript ok", ""


def score(results: list[CheckResult]) -> tuple[int, float]:
    mandatory_ok = all(r.passed for r in results if r.mandatory)
    total = sum(r.weight for r in results) or 1.0
    soft = sum(r.weight for r in results if r.passed) / total
    return (1 if mandatory_ok else 0), round(soft, 4)


def fail_reason(results: list[CheckResult]) -> str:
    failed = [r for r in results if not r.passed]
    if not failed:
        return ""
    first = [r for r in failed if r.mandatory] or failed
    return "; ".join(f"{r.id}: {r.detail}" for r in first[:3])
