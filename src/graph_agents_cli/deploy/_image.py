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
"""Image reference checks run before ``docker build``, ``docker push`` or ``helm``.

``create`` writes the placeholder registry ``ghcr.io/CHANGE-ME`` when it finds
no git remote. Docker rejects it (a repository path must be lowercase) only
after the build starts, with a parser error that does not say which setting to
change. These helpers catch the placeholder and any other malformed reference
up front so ``build`` and ``deploy`` stop with a configuration error (exit 3)
that names the setting.

The grammar follows the distribution reference format Docker uses: an optional
registry host (with port), lowercase path components separated by ``/``, and a
tag of at most 128 word characters, dots and dashes.
"""

from __future__ import annotations

import re

PLACEHOLDER = "CHANGE-ME"

_DOMAIN_COMPONENT = r"(?:[A-Za-z0-9]|[A-Za-z0-9][A-Za-z0-9-]*[A-Za-z0-9])"
_DOMAIN_RE = re.compile(
    rf"^(?:{_DOMAIN_COMPONENT}(?:\.{_DOMAIN_COMPONENT})*|\[[0-9A-Fa-f:.]+\])(?::[0-9]+)?$"
)
_PATH_COMPONENT_RE = re.compile(r"^[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*$")
_TAG_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")
MAX_NAME_LENGTH = 255


def has_placeholder(value: str | None) -> bool:
    """True when ``value`` still carries the scaffold's ``CHANGE-ME`` placeholder."""
    return PLACEHOLDER.lower() in (value or "").lower()


def _split_domain(name: str) -> tuple[str | None, str]:
    """``(domain, path)`` using Docker's rule for what counts as a registry host.

    The first component is a host when it contains ``.`` or ``:``, is
    ``localhost``, or has an upper-case letter; otherwise the whole name is a
    path on the default registry.
    """
    first, sep, rest = name.partition("/")
    if not sep:
        return None, name
    if "." in first or ":" in first or first == "localhost" or first.lower() != first:
        return first, rest
    return None, name


def repository_problem(repository: str) -> str | None:
    """Why ``repository`` (``[host[:port]/]path``, no tag) is not a valid image name."""
    if not repository:
        return "the repository is empty"
    if len(repository) > MAX_NAME_LENGTH:
        return f"the repository name is longer than {MAX_NAME_LENGTH} characters"
    domain, path = _split_domain(repository)
    if domain is not None and not _DOMAIN_RE.match(domain):
        return f"{domain!r} is not a valid registry host"
    for component in path.split("/"):
        if not component:
            return "the repository path has an empty component (a doubled or trailing '/')"
        if not _PATH_COMPONENT_RE.match(component):
            if component.lower() != component:
                return f"the repository path component {component!r} must be lowercase"
            return (
                f"the repository path component {component!r} may only hold lowercase letters, "
                "digits and single '.', '_' or '-' separators"
            )
    return None


def tag_problem(tag: str) -> str | None:
    """Why ``tag`` is not a valid image tag (``None`` when it is)."""
    if not _TAG_RE.match(tag or ""):
        return (
            f"the tag {tag!r} must start with a letter, digit or '_' and hold at most 128 "
            "letters, digits, '_', '.' or '-'"
        )
    return None


def placeholder_message(registry: str) -> str:
    return (
        f"The image registry is still the placeholder {registry!r}.\n"
        "  Set create_params.registry in graph-agents-cli-manifest.yaml to your registry "
        "(for example ghcr.io/<org>); `build` also takes --registry."
    )


def reference_problem(
    repository: str, tag: str | None, *, registry: str | None = None
) -> str | None:
    """A user-facing reason the image ``repository[:tag]`` cannot be built or pushed.

    ``registry`` is the configured registry the repository was derived from; a
    placeholder there is reported as such (it names the setting to change)
    rather than as a grammar error.
    """
    if has_placeholder(registry):
        return placeholder_message(registry or "")
    if has_placeholder(repository):
        return (
            f"The image reference {repository!r} still holds the {PLACEHOLDER} placeholder; "
            "pass the real image."
        )
    problem = repository_problem(repository)
    if problem is None and tag is not None:
        problem = tag_problem(tag)
    if problem is None:
        return None
    ref = f"{repository}:{tag}" if tag is not None else repository
    return f"{ref!r} is not a valid image reference: {problem}."
