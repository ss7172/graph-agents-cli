# Copyright 2026 Google LLC
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

"""Parse extension references for `graph-agents-cli extension add`."""

from __future__ import annotations

import dataclasses
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

# First-party public repository. Keep in sync with _tools.DEFAULT_SKILLS_SOURCE.
FIRST_PARTY_REPO = "ss7172/graph-agents-cli"

# Schemes an extension may be cloned from: the set `--agent` already accepts for
# a template repo (scaffold/utils/remote_template.py), plus ssh://, which is how
# a self-hosted host is usually reached, and file://, for a bare repo on a shared
# mount — `local@` copies a working tree and cannot pin a commit against one.
# `git://` is left out deliberately: it is unauthenticated, and an extension's
# commands run arbitrary code on the machine that installs it.
GIT_URL_SCHEMES = ("https", "http", "ssh", "file")

_SCHEME_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*)://")
# scp-style, as offered by most hosts' "clone with SSH" button: git@host:org/repo.
_SCP_RE = re.compile(r"^[A-Za-z0-9._-]+@[A-Za-z0-9._-]+:(?P<path>.+)$")
# A reference written as a filesystem path: absolute, ./ or ../ relative, ~, or a
# Windows drive. These are local sources even without the `local@` prefix
# (`org/repo` stays the github.com shorthand: it is not written as a path).
_PATH_RE = re.compile(r"^(?:/|\.{1,2}(?:[/\\]|$)|~|[A-Za-z]:[/\\])")


class RefParseError(Exception):
    """Raised when an extension reference cannot be parsed."""


@dataclass(frozen=True)
class ExtensionRef:
    raw: str
    kind: str  # "first_party" | "github" | "git" | "local"
    repo: str | None
    # Clone URL, set only for kind == "git". A github/first_party ref names a
    # repo on github.com and lets the resolver build the URL from it.
    url: str | None
    local_path: Path | None
    selector: str | None
    ref: str | None


def looks_like_path(value: str) -> bool:
    """True for a reference written as a filesystem path rather than a repo."""
    return bool(_PATH_RE.match(value))


def anchor_local(ref: ExtensionRef, base: Path) -> ExtensionRef:
    """``ref`` with a relative local path resolved against ``base`` (a scope root).

    A recorded ``local@../team-tools`` is relative to the scope root it was
    recorded in, so ``install``/``update`` find it from any working directory.
    """
    if ref.kind != "local" or ref.local_path is None or ref.local_path.is_absolute():
        return ref
    return dataclasses.replace(ref, local_path=base / ref.local_path)


def _split_selector(value: str) -> tuple[str, str | None]:
    if "#" in value:
        base, selector = value.split("#", 1)
        return base, (selector or None)
    return value, None


def _as_git_url(base: str) -> str | None:
    """Return ``base`` unchanged if it is a clonable git URL, else None.

    A string carrying a scheme git cannot clone is an error rather than a None,
    so that `ftp://host/org/repo` is reported as a bad scheme instead of falling
    through and being read as the `org/repo` shorthand `ftp:/host/org`.
    """
    scheme = _SCHEME_RE.match(base)
    if scheme is not None:
        if scheme.group(1).lower() not in GIT_URL_SCHEMES:
            raise RefParseError(
                f"unsupported scheme in {base!r}: use "
                f"{', '.join(s + '://' for s in GIT_URL_SCHEMES)}, a 'git@host:org/repo' "
                "URL, an 'org/repo' shorthand, or 'local@<path>'."
            )
        return base
    return base if _SCP_RE.match(base) else None


def _repo_identity(url: str) -> str:
    """Return an ``org/repo`` label for a clone URL.

    Used for messages, for the vendored directory's fallback name and for the
    readable part of the cache directory. The clone itself always uses the URL
    as given, so nothing here has to round-trip.
    """
    scp = _SCP_RE.match(url)
    path = scp.group("path") if scp is not None else urlsplit(url).path
    parts = [p for p in path.split("/") if p]
    if not parts:
        raise RefParseError(f"no repository path in {url!r}.")
    if parts[-1].endswith(".git"):
        parts[-1] = parts[-1].removesuffix(".git")
    if not parts[-1]:
        raise RefParseError(f"no repository name in {url!r}.")
    return "/".join(parts[-2:])


def parse_ref(raw: str, *, ref_override: str | None = None) -> ExtensionRef:
    """Parse an extension reference string into a structured ExtensionRef.

    Supported forms:
      - ``soc2``                            first-party shorthand
      - ``acme/acli-extensions``            any org/repo on github.com
      - ``acme/acli-extensions#soc2``       org/repo with extension selector
      - ``https://git.example.com/acme/acli-extensions``   any git host
      - ``git@git.example.com:acme/acli-extensions``       the same, over ssh
      - ``local@../my-extension``           local path
      - ``../my-extension``, ``/abs/dir``, ``~/dir``   a local path without ``local@``
      - pass ``ref_override`` to pin a branch, tag, or SHA
    """
    raw = (raw or "").strip()
    if not raw:
        raise RefParseError("empty extension reference.")

    if raw.startswith("local@") or looks_like_path(raw):
        body = raw[len("local@") :] if raw.startswith("local@") else raw
        path_part, selector = _split_selector(body)
        if not path_part:
            raise RefParseError(f"local reference missing a path: {raw!r}.")
        return ExtensionRef(
            raw=raw if raw.startswith("local@") else f"local@{raw}",
            kind="local",
            repo=None,
            url=None,
            local_path=Path(os.path.expanduser(path_part)),
            selector=selector,
            ref=ref_override,
        )

    base, selector = _split_selector(raw)

    # Checked before the org/repo shorthand: a URL contains slashes too, and
    # would otherwise be silently mangled into one.
    url = _as_git_url(base)
    if url is not None:
        return ExtensionRef(
            raw=raw,
            kind="git",
            repo=_repo_identity(url),
            url=url,
            local_path=None,
            selector=selector,
            ref=ref_override,
        )

    if "/" in base:
        return ExtensionRef(
            raw=raw,
            kind="github",
            repo=base,
            url=None,
            local_path=None,
            selector=selector,
            ref=ref_override,
        )

    # First-party shorthand: a bare name selects that extension from the
    # first-party repo (ss7172/graph-agents-cli).
    return ExtensionRef(
        raw=raw,
        kind="first_party",
        repo=FIRST_PARTY_REPO,
        url=None,
        local_path=None,
        selector=selector or base,
        ref=ref_override,
    )
