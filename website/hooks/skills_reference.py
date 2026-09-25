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

"""MkDocs hook: generate the Skills reference from skills/*/SKILL.md.

``reference/skills.md`` holds the page's introduction and a marker line; at
build time the marker is replaced with one section per bundled skill, read from
its SKILL.md frontmatter (name, description, version) and its ``references/``
directory, the entry-point skill first. Nothing is written to disk, so the page can never drift from the
skills it describes. A skills directory without skills, or a SKILL.md without
frontmatter, fails the build.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml
from mkdocs.exceptions import PluginError

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILLS_DIR = REPO_ROOT / "skills"
SKILLS_PAGE = "reference/skills.md"
MARKER = "<!-- skills-reference:generated -->"
# The entry point every request starts from is listed first; the rest follow by name.
ENTRY_SKILL = "graph-agents-cli-workflow"

_blob_base: dict[str, str] = {"url": ""}


def _frontmatter(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    match = re.match(r"^---\n(.+?)\n---\n", text, re.DOTALL)
    if not match:
        raise PluginError(f"skills_reference hook: {path} has no YAML frontmatter.")
    data = yaml.safe_load(match.group(1)) or {}
    if not data.get("name"):
        raise PluginError(f"skills_reference hook: {path} frontmatter has no name.")
    return data


def _split_description(description: str) -> tuple[list[str], str, str]:
    """(trigger phrases, what it covers, what it hands off) from a routing description."""
    text = " ".join(description.split())
    # A folded YAML line break after a slash reads "dev/staging/ prod".
    text = re.sub(r"(?<=\S)/ (?=\w)", "/", text)
    handoff = ""
    if "Do NOT use for" in text:
        text, _, handoff = text.partition("Do NOT use for")
        handoff = handoff.strip().rstrip(".")
    text = text.replace("Part of the graph-agents-cli skills suite.", "").strip()
    triggers: list[str] = []
    covers = text
    start = re.search(r"\b(Covers|Entrypoint|Always active)\b", text)
    if start:
        lead, covers = text[: start.start()], text[start.start() :]
        triggers = re.findall(r'"([^"]+)"', lead)
    return triggers, " ".join(covers.split()), handoff


def _skill_section(skill_dir: Path) -> str:
    meta = _frontmatter(skill_dir / "SKILL.md")
    name = meta["name"]
    version = str((meta.get("metadata") or {}).get("version", ""))
    triggers, covers, handoff = _split_description(str(meta.get("description", "")))
    blob = _blob_base["url"]

    lines = [f"## `{name}`", ""]
    if covers:
        lines += [covers, ""]
    if triggers:
        quoted = ", ".join(f"“{t}”" for t in triggers)
        lines += [f"**Picked up by requests like** {quoted}.", ""]
    if handoff:
        linked = re.sub(r"\((graph-agents-cli-[a-z-]+)\)", r"([`\1`](#\1))", handoff)
        lines += [f"**Not for** {linked}.", ""]
    refs = sorted((skill_dir / "references").glob("*.md"))
    source = f"[`SKILL.md`]({blob}skills/{name}/SKILL.md)"
    if refs:
        ref_links = ", ".join(
            f"[`{ref.stem}`]({blob}skills/{name}/references/{ref.name})" for ref in refs
        )
        lines += [f"**Source:** {source} · **References:** {ref_links}", ""]
    else:
        lines += [f"**Source:** {source}", ""]
    if version:
        lines += [f"<small>Skill version {version}</small>", ""]
    return "\n".join(lines)


def on_config(config, **kwargs):
    repo_url = (config.repo_url or "").rstrip("/")
    _blob_base["url"] = f"{repo_url}/blob/main/" if repo_url else ""
    return config


def on_page_markdown(markdown, page, **kwargs):
    if page.file.src_uri != SKILLS_PAGE:
        return markdown
    if MARKER not in markdown:
        raise PluginError(f"skills_reference hook: {SKILLS_PAGE} lost its marker {MARKER!r}.")
    skill_dirs = sorted(
        (p.parent for p in SKILLS_DIR.glob("*/SKILL.md")),
        key=lambda d: (d.name != ENTRY_SKILL, d.name),
    )
    if not skill_dirs:
        raise PluginError(f"skills_reference hook: no skills under {SKILLS_DIR}.")
    sections = "\n---\n\n".join(_skill_section(d) for d in skill_dirs)
    return markdown.replace(MARKER, sections)
