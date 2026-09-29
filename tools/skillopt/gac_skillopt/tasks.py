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

"""Benchmark tasks, their splits, and the skill documents rollouts install.

A task is a directory ``tasks/<skill short name>/<task id>/``:

- ``task.json``: the schema below (``validate`` checks it);
- ``fixture/`` (optional): files copied into the project (or, for an ``empty`` fixture, the
  workspace) before the agent starts; a leading ``dot_`` in a path part becomes ``.`` (the
  repository ignores ``.env*`` files);
- ``hidden/`` (optional): files only the verifier sees, copied in after the agent exits;
- ``gold.sh`` / ``broken.sh``: scripted solutions (no model) the verifier must accept and
  reject; ``selfcheck`` runs both, and the untouched fixture, for every task.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .paths import LONG, SHORT, SKILLS, SKILLS_DIR, SPLITS_DIR, TASKS_DIR

CHECK_TYPES = {"cmd", "file", "json", "yaml", "dotenv", "unchanged", "pyfile", "eval", "transcript"}
FIXTURE_KINDS = {"project", "empty"}
SPLITS = ("train", "val", "test")

# Every rollout's prompt starts with this; it says nothing about how to use the skill.
PROMPT_PREFIX = (
    "graph-agents-cli and its agent skills are installed. Generated projects here run on the "
    "deterministic fake model provider (MODEL_PROVIDER=fake), so no model API key is needed. "
    "Work only inside the current directory. I am not available for questions: when the task "
    "leaves a decision to me, do what is safe and explain it in your final answer.\n\n"
)


@dataclass
class Task:
    id: str
    skill: str
    family: str
    task_type: str
    prompt: str
    fixture: dict[str, Any]
    checks: list[dict[str, Any]]
    reference: str
    timeout_s: int = 900
    # The mandatory checks the scripted broken solution must fail, exactly (selfcheck).
    broken_fails: list[str] = field(default_factory=list)
    # The agent must run the project's local server itself (`eval run`, `run`): Codex rollouts
    # cannot until DESIGN section 9 issues 1 and 2 are fixed.
    needs_local_server: bool = False
    dir: Path = field(default_factory=Path)

    @property
    def project_name(self) -> str:
        return str(self.fixture.get("name") or "")

    @property
    def full_prompt(self) -> str:
        return PROMPT_PREFIX + self.prompt

    def item(self) -> dict[str, Any]:
        """The SkillOpt item (JSON-safe)."""
        return {
            "id": self.id,
            "skill": self.skill,
            "family": self.family,
            "task_type": self.task_type,
            "task_description": self.prompt,
            "reference_text": self.reference,
            "needs_local_server": self.needs_local_server,
        }

    def content_hash(self) -> str:
        h = hashlib.sha256()
        for path in sorted(p for p in self.dir.rglob("*") if p.is_file()):
            if "__pycache__" in path.parts:
                continue
            h.update(str(path.relative_to(self.dir)).encode())
            h.update(path.read_bytes())
        return h.hexdigest()[:16]


def validate(data: dict[str, Any], where: str) -> list[str]:
    """Problems with one ``task.json`` (empty when it is valid)."""
    problems = []
    for key in ("id", "skill", "family", "task_type", "prompt", "fixture", "checks", "reference"):
        if key not in data:
            problems.append(f"{where}: missing {key}")
    if problems:
        return problems
    if data["skill"] not in SKILLS:
        problems.append(f"{where}: unknown skill {data['skill']}")
    fixture = data["fixture"]
    if fixture.get("kind") not in FIXTURE_KINDS:
        problems.append(f"{where}: fixture.kind must be one of {sorted(FIXTURE_KINDS)}")
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,25}", str(fixture.get("name", ""))):
        problems.append(f"{where}: fixture.name must be a valid project name")
    ids = set()
    mandatory = 0
    for i, check in enumerate(data["checks"]):
        cid = check.get("id")
        if not cid or cid in ids:
            problems.append(f"{where}: check {i} needs a unique id")
        ids.add(cid)
        if check.get("type") not in CHECK_TYPES:
            problems.append(f"{where}: check {cid}: unknown type {check.get('type')}")
        if check.get("mandatory", True):
            mandatory += 1
    if not mandatory:
        problems.append(f"{where}: at least one check must be mandatory")
    if not str(data["reference"]).strip():
        problems.append(f"{where}: reference must describe a correct solution")
    mandatory_ids = {c.get("id") for c in data["checks"] if c.get("mandatory", True)}
    broken = data.get("broken_fails")
    if not broken:
        problems.append(f"{where}: broken_fails must name the mandatory checks broken.sh fails")
    elif set(broken) - mandatory_ids:
        problems.append(
            f"{where}: broken_fails names non-mandatory or unknown checks {sorted(set(broken) - mandatory_ids)}"
        )
    return problems


def load_task(task_dir: Path) -> Task:
    data = json.loads((task_dir / "task.json").read_text())
    problems = validate(data, str(task_dir))
    if data.get("id") != task_dir.name:
        problems.append(f"{task_dir}: id {data.get('id')!r} differs from the directory name")
    if SHORT.get(data.get("skill", "")) != task_dir.parent.name:
        problems.append(
            f"{task_dir}: lives under tasks/{task_dir.parent.name}/ but tests {data.get('skill')}"
        )
    if problems:
        raise ValueError("\n".join(problems))
    return Task(
        id=data["id"],
        skill=data["skill"],
        family=data["family"],
        task_type=data["task_type"],
        prompt=data["prompt"],
        fixture=data["fixture"],
        checks=data["checks"],
        reference=data["reference"],
        timeout_s=int(data.get("timeout_s", 900)),
        broken_fails=list(data.get("broken_fails", [])),
        needs_local_server=bool(data.get("needs_local_server", False)),
        dir=task_dir,
    )


def all_tasks(skill: str | None = None) -> list[Task]:
    """Every task (of one skill, by long or short name), sorted by id."""
    tasks = []
    roots = [TASKS_DIR / SHORT.get(skill, skill)] if skill else sorted(TASKS_DIR.iterdir())
    for root in roots:
        if not root.is_dir():
            continue
        for task_dir in sorted(root.iterdir()):
            if (task_dir / "task.json").is_file():
                tasks.append(load_task(task_dir))
    return tasks


def task_by_id(task_id: str) -> Task:
    for task in all_tasks():
        if task.id == task_id:
            return task
    raise KeyError(task_id)


# ── Splits ──────────────────────────────────────────────────────────────────


def load_split(skill: str) -> dict[str, list[str]]:
    """``splits/<short>.json``: ``{"train": [ids], "val": [ids], "test": [ids], "hashes": {}}``."""
    return json.loads((SPLITS_DIR / f"{SHORT.get(skill, skill)}.json").read_text())


def split_problems() -> list[str]:
    """Every task in exactly one split of its skill, no unknown ids, hashes current, and the
    hold-out rule (DESIGN section 5.4): test tasks are frozen and appear in no other split;
    train and val tasks may share a test task's family (variants within a family are held
    out, not whole families) but never its fixture project name or its prompt."""
    problems = []
    for skill in SKILLS:
        tasks = {t.id: t for t in all_tasks(skill)}
        path = SPLITS_DIR / f"{SHORT[skill]}.json"
        if not path.exists():
            problems.append(f"{path.name}: missing")
            continue
        split = load_split(skill)
        seen: dict[str, str] = {}
        for name in SPLITS:
            for tid in split.get(name, []):
                if tid in seen:
                    problems.append(f"{path.name}: {tid} in {seen[tid]} and {name}")
                seen[tid] = name
                if tid not in tasks:
                    problems.append(f"{path.name}: unknown task {tid}")
        for tid in tasks:
            if tid not in seen:
                problems.append(f"{path.name}: task {tid} is in no split")
        problems += holdout_problems(path.name, split, tasks)
        for tid, digest in (split.get("hashes") or {}).items():
            if tid in tasks and tasks[tid].content_hash() != digest:
                problems.append(f"{path.name}: {tid} changed since the split was frozen")
    return problems


def holdout_problems(where: str, split: dict[str, Any], tasks: dict[str, Task]) -> list[str]:
    """The hold-out rule for one skill's split (DESIGN section 5.4): at least one test task,
    every test task frozen, and a train or val task in a test task's family (a variant) differs
    from it in the fixture's project name and in the prompt."""
    problems = []
    held_out = [tasks[t] for t in split.get("test", []) if t in tasks]
    if not held_out:
        problems.append(f"{where}: no test task")
    for name in ("train", "val"):
        for tid in split.get(name, []):
            task = tasks.get(tid)
            for held in held_out if task else []:
                if task.family != held.family:
                    continue
                if task.project_name == held.project_name:
                    problems.append(
                        f"{where}: {tid} ({name}) reuses the project name of test task {held.id}; "
                        "a variant must differ in fixture, prompt and expected specifics"
                    )
                if task.prompt.strip() == held.prompt.strip():
                    problems.append(f"{where}: {tid} ({name}) copies the prompt of {held.id}")
    for tid in split.get("test", []):
        if tid in tasks and tid not in (split.get("hashes") or {}):
            problems.append(f"{where}: test task {tid} has no frozen hash")
    return problems


def freeze_hashes(skill: str) -> None:
    path = SPLITS_DIR / f"{SHORT[skill]}.json"
    split = load_split(skill)
    tasks = {t.id: t for t in all_tasks(skill)}
    split["hashes"] = {tid: tasks[tid].content_hash() for n in SPLITS for tid in split[n]}
    path.write_text(json.dumps(split, indent=2) + "\n")


# ── The skill document ──────────────────────────────────────────────────────

FRONTMATTER = re.compile(r"\A---\n.*?\n---\n", re.S)


def skill_source(skill: str) -> Path:
    return SKILLS_DIR / LONG.get(skill, skill)


def split_skill(text: str) -> tuple[str, str]:
    """``SKILL.md`` -> (frontmatter block, body)."""
    match = FRONTMATTER.match(text)
    if not match:
        return "", text
    return match.group(0), text[match.end() :]


def initial_body(skill: str) -> str:
    """The trainable document: the shipped ``SKILL.md`` without its frontmatter."""
    return split_skill((skill_source(skill) / "SKILL.md").read_text())[1]


def render_skill(skill: str, body: str) -> str:
    """Frontmatter of the shipped skill (held fixed: it decides triggering) + a candidate body.
    A candidate that carries frontmatter of its own gets the shipped one instead."""
    front = split_skill((skill_source(skill) / "SKILL.md").read_text())[0]
    _, body = split_skill(body.lstrip("\n")) if body.lstrip().startswith("---") else ("", body)
    return front + "\n" + body.lstrip("\n").rstrip() + "\n"
