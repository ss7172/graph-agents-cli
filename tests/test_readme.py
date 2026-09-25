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

"""The repository README: a short entry point whose links and install command stay valid.

The README is also the package's long description on PyPI, which does not resolve relative
links, so every link is absolute: documentation pages point at the site
(``https://ss7172.github.io/graph-agents-cli/<page>/``, built from ``website/src/``) and
repository files at GitHub (``.../blob/main/<path>``). Nothing else checks those links,
and the site's version hook does not read the README, so all three are checked here.
"""

from __future__ import annotations

import re
from pathlib import Path

from graph_agents_cli import __version__

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
SITE_SRC = ROOT / "website" / "src"
SITE_URL = "https://ss7172.github.io/graph-agents-cli/"
BLOB_URL = "https://github.com/ss7172/graph-agents-cli/blob/main/"

# ](target) of inline links and images, anchors dropped.
_LINK = re.compile(r"\]\(([^)\s#]*)(?:#[^)\s]*)?\)")
_INSTALL_TAG = re.compile(r"graph-agents-cli(?:@|#)v(\d+\.\d+\.\d+)")


def _targets() -> set[str]:
    return set(_LINK.findall(README.read_text(encoding="utf-8")))


def _site_page(url: str) -> Path | None:
    """The page source a site URL is built from (``a/b/`` -> ``a/b.md`` or ``a/b/index.md``)."""
    path = url.removeprefix(SITE_URL).strip("/")
    if not path:
        candidates = [SITE_SRC / "index.md"]
    else:
        candidates = [SITE_SRC / f"{path}.md", SITE_SRC / path / "index.md"]
    return next((c for c in candidates if c.is_file()), None)


def test_readme_links_are_absolute() -> None:
    """PyPI renders the README without the repository around it: no relative link works."""
    relative = sorted(t for t in _targets() if t and not t.startswith(("https://", "mailto:")))
    assert not relative, f"README has relative links (they 404 on PyPI): {relative}"


def test_readme_site_links_point_at_pages_that_exist() -> None:
    site = sorted(t for t in _targets() if t.startswith(SITE_URL))
    assert site, "the README links nowhere on the documentation site"
    missing = [t for t in site if _site_page(t) is None]
    assert not missing, f"README links to site pages with no source in website/src: {missing}"


def test_readme_repository_links_point_at_files_that_exist() -> None:
    files = sorted(t.removeprefix(BLOB_URL) for t in _targets() if t.startswith(BLOB_URL))
    assert files, "the README links to no repository file"
    missing = [f for f in files if not (ROOT / f).exists()]
    assert not missing, f"README links to missing repository files: {missing}"


def test_readme_links_the_documentation_site() -> None:
    targets = _targets()
    for section in ("getting-started/", "guides/", "reference/"):
        assert f"{SITE_URL}{section}" in targets, section


def test_readme_installs_the_current_release() -> None:
    tags = set(_INSTALL_TAG.findall(README.read_text(encoding="utf-8")))
    assert tags == {__version__.split("+", 1)[0]}, tags
