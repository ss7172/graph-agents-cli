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

"""Evaluate an extension's declared graph-agents-cli compatibility range.

An extension may declare ``requires.agents_cli`` (e.g. ``">=1.1,<2"``), which is a
PEP 440 specifier set evaluated against the running CLI version.
"""

from __future__ import annotations

import logging

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version


def parse_range(spec: str) -> SpecifierSet | None:
    """Parse a declared range, or None if it is unusable.

    The single place a range string is interpreted, so validation and the
    compatibility check cannot disagree about what is parseable.
    """
    if not spec.strip():
        # packaging accepts "" as "any version"; an empty range in a manifest is
        # an authoring mistake, so drop it rather than record a no-op.
        return None
    try:
        return SpecifierSet(spec)
    except InvalidSpecifier:
        return None


def validate_range(spec: str) -> bool:
    """True iff ``spec`` is a usable version range."""
    return parse_range(spec) is not None


def is_compatible(version: str, spec: str | None) -> bool:
    """Return whether ``version`` satisfies the range ``spec``.

    Assumes ``spec`` already passed ``validate_range``: ``_spec._parse_requires``
    warns and drops a bad range at parse time, so an unparseable one never
    reaches here (and is treated as no constraint if it somehow does).

    No constraint means compatible. A source build (``DEV_VERSION``, or any
    version packaging can't read) is always compatible: running from a checkout
    must never be blocked by an extension's range.
    """
    if not spec:
        return True
    try:
        running = Version(version)
    except InvalidVersion:
        # The *running* CLI reporting an unreadable version is a broken install,
        # not an extension problem, so say so rather than fail it closed.
        logging.warning(
            "Cannot read the running graph-agents-cli version %r; skipping the "
            "compatibility check against %r.",
            version,
            spec,
        )
        return True
    if running.is_devrelease:
        logging.debug(
            "Running a dev build (%s); not enforcing extension range %r.",
            version,
            spec,
        )
        return True
    parsed = parse_range(spec)
    if parsed is None:
        return True
    # prereleases=True so an rc of an in-range release still counts as in range.
    return parsed.contains(running, prereleases=True)
