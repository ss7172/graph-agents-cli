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

The documentation lives in ``website/src/`` (the MkDocs site, checked by its strict build);
the README links into it with relative links, so they work on GitHub before the site is
published. Nothing else checks those links, and the site's version hook does not read the
README, so both are checked here.
"""

from __future__ import annotations

import re
from pathlib import Path

from graph_agents_cli import __version__

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
# ](target) and ](target#anchor) of inline links and images; absolute URLs and in-page
# anchors are left to the reader.
_LINK = re.compile(r"\]\((?!https?://|mailto:|#)([^)\s#]+)(?:#[^)\s]*)?\)")
_INSTALL_TAG = re.compile(r"graph-agents-cli(?:@|#)v(\d+\.\d+\.\d+)")


def test_readme_relative_links_point_at_files_that_exist() -> None:
    text = README.read_text(encoding="utf-8")
    targets = sorted(set(_LINK.findall(text)))
    assert targets, "the README links nowhere"
    missing = [t for t in targets if not (ROOT / t).exists()]
    assert not missing, f"README links to missing files: {missing}"


def test_readme_links_the_documentation_site() -> None:
    targets = set(_LINK.findall(README.read_text(encoding="utf-8")))
    for page in ("getting-started/index.md", "guides/index.md", "reference/index.md"):
        assert f"website/src/{page}" in targets, page


def test_readme_installs_the_current_release() -> None:
    tags = set(_INSTALL_TAG.findall(README.read_text(encoding="utf-8")))
    assert tags == {__version__.split("+", 1)[0]}, tags
