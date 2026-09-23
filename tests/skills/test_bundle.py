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

"""Structural tests for the skills bundle (installed offline by `setup`).

- every ``skills/graph-agents-cli-*/`` directory has a ``SKILL.md`` with valid
  frontmatter whose ``name`` matches the directory and whose
  ``metadata.version`` is ``0.1.0``;
- the six skill names are exactly the ones present;
- every ``references/<file>.md`` a SKILL.md mentions exists, and every reference
  file is mentioned;
- Google Cloud product names appear only under a "Migration note" heading;
- ``skills/`` and ``src/graph_agents_cli/skills/data/`` are byte-identical.

No network, no cluster, no model key.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILLS_DIR = REPO_ROOT / "skills"
BUNDLE_DIR = REPO_ROOT / "src" / "graph_agents_cli" / "skills" / "data"

SKILL_PREFIX = "graph-agents-cli-"
EXPECTED_SKILLS = {
    "graph-agents-cli-workflow",
    "graph-agents-cli-langgraph-code",
    "graph-agents-cli-scaffold",
    "graph-agents-cli-eval",
    "graph-agents-cli-deploy",
    "graph-agents-cli-observability",
}
EXPECTED_VERSION = "0.1.0"

# Editor and OS droppings that must not affect the byte-identical comparison.
IGNORED_NAMES = {".DS_Store", "__pycache__", "Thumbs.db"}

# Google Cloud product names that may appear only in a migration note.
FORBIDDEN_TERMS = (
    "Google Cloud",
    "Vertex",
    "Cloud Run",
    "GKE",
    "Agent Runtime",
    "Agent Engine",
    "Cloud Trace",
    "Cloud Logging",
    "Cloud Build",
    "BigQuery",
    "Secret Manager",
    "Gemini Enterprise",
    "gcloud",
    "GCS",
    "ADK",
)


def _skill_dirs(root: Path) -> list[Path]:
    return sorted(p for p in root.iterdir() if p.is_dir() and p.name.startswith(SKILL_PREFIX))


def _split_frontmatter(text: str) -> tuple[dict, str]:
    assert text.startswith("---\n"), "SKILL.md must start with YAML frontmatter"
    _, front, body = text.split("---", 2)
    data = yaml.safe_load(front)
    assert isinstance(data, dict), "frontmatter must be a mapping"
    return data, body


def _walk(root: Path) -> dict[str, Path]:
    files: dict[str, Path] = {}
    for path in root.rglob("*"):
        if any(part in IGNORED_NAMES for part in path.relative_to(root).parts):
            continue
        if path.is_file():
            files[path.relative_to(root).as_posix()] = path
    return files


@pytest.fixture(scope="module")
def skill_dirs() -> list[Path]:
    dirs = _skill_dirs(SKILLS_DIR)
    assert dirs, f"no {SKILL_PREFIX}* directories under {SKILLS_DIR}"
    return dirs


def test_exactly_the_six_skills_exist(skill_dirs: list[Path]) -> None:
    assert {d.name for d in skill_dirs} == EXPECTED_SKILLS


@pytest.mark.parametrize("skill", sorted(EXPECTED_SKILLS))
def test_skill_frontmatter(skill: str) -> None:
    skill_md = SKILLS_DIR / skill / "SKILL.md"
    assert skill_md.is_file(), f"{skill} has no SKILL.md"
    data, body = _split_frontmatter(skill_md.read_text(encoding="utf-8"))

    assert data["name"] == skill, f"frontmatter name {data['name']!r} != directory {skill!r}"
    assert isinstance(data.get("description"), str) and data["description"].strip()
    metadata = data["metadata"]
    assert str(metadata["version"]) == EXPECTED_VERSION
    assert metadata["license"] == "Apache-2.0"
    assert metadata["requires"]["bins"] == ["graph-agents-cli"]
    assert "graph-agents-cli" in metadata["requires"]["install"]
    assert body.strip(), "SKILL.md body is empty"


@pytest.mark.parametrize("skill", sorted(EXPECTED_SKILLS))
def test_skill_says_what_it_does_not_cover(skill: str) -> None:
    text = (SKILLS_DIR / skill / "SKILL.md").read_text(encoding="utf-8")
    assert re.search(r"^## Not covered", text, re.MULTILINE), (
        f"{skill}: missing 'Not covered' section"
    )
    assert re.search(r"^## Migration note", text, re.MULTILINE), f"{skill}: missing migration note"


@pytest.mark.parametrize("skill", sorted(EXPECTED_SKILLS))
def test_references_resolve_both_ways(skill: str) -> None:
    skill_dir = SKILLS_DIR / skill
    text = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    mentioned = set(re.findall(r"`references/([A-Za-z0-9_.-]+\.md)`", text))
    present = {p.name for p in (skill_dir / "references").glob("*.md")}
    assert mentioned, f"{skill}: SKILL.md mentions no references/*.md"
    assert mentioned == present, (
        f"{skill}: mentioned but missing {sorted(mentioned - present)}; "
        f"present but unmentioned {sorted(present - mentioned)}"
    )


def _sections(text: str) -> list[tuple[str, str]]:
    """Split markdown into (heading, body) pairs; text before the first heading has heading ''."""
    sections: list[tuple[str, str]] = []
    heading = ""
    buf: list[str] = []
    for line in text.splitlines():
        if line.startswith("#"):
            sections.append((heading, "\n".join(buf)))
            heading, buf = line, []
        else:
            buf.append(line)
    sections.append((heading, "\n".join(buf)))
    return sections


@pytest.mark.parametrize(
    "md_file",
    sorted(p.relative_to(SKILLS_DIR).as_posix() for p in SKILLS_DIR.rglob("*.md")),
)
def test_google_cloud_products_only_in_migration_note(md_file: str) -> None:
    text = (SKILLS_DIR / md_file).read_text(encoding="utf-8")
    offenders: list[str] = []
    for heading, body in _sections(text):
        if "migration" in heading.lower():
            continue
        for term in FORBIDDEN_TERMS:
            if re.search(rf"\b{re.escape(term)}\b", body):
                offenders.append(f"{term!r} under {heading or '<top>'!r}")
    assert not offenders, f"{md_file}: {offenders}"


def test_workflow_skill_defers_to_a_declared_process() -> None:
    text = (SKILLS_DIR / "graph-agents-cli-workflow" / "SKILL.md").read_text(encoding="utf-8")
    assert "process:" in text
    assert re.search(r"^## Process deference", text, re.MULTILINE)
    for rule in ("3-strikes", "NEVER change the model", "human approval", "code preservation"):
        assert rule.lower() in text.lower(), f"workflow skill lost the rule {rule!r}"


def test_readme_lists_every_skill() -> None:
    readme = (SKILLS_DIR / "README.md").read_text(encoding="utf-8")
    for skill in EXPECTED_SKILLS:
        assert f"`{skill}`" in readme, f"README.md does not list {skill}"


def test_bundle_is_byte_identical() -> None:
    assert BUNDLE_DIR.is_dir(), f"missing bundle dir {BUNDLE_DIR}"
    source = _walk(SKILLS_DIR)
    bundled = _walk(BUNDLE_DIR)
    assert set(source) == set(bundled), (
        f"only in skills/: {sorted(set(source) - set(bundled))}; "
        f"only in bundle: {sorted(set(bundled) - set(source))}"
    )
    differing = [rel for rel in source if source[rel].read_bytes() != bundled[rel].read_bytes()]
    assert not differing, f"files differ between skills/ and the bundle: {differing}"


def test_bundle_helper_finds_the_skills() -> None:
    from graph_agents_cli.skills._bundle import get_bundled_skills_dir, is_skill_dir

    bundle = get_bundled_skills_dir()
    assert bundle == BUNDLE_DIR
    assert {p.name for p in bundle.iterdir() if is_skill_dir(p)} == EXPECTED_SKILLS
