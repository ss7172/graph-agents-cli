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

"""A project's response schema (structured final answers): the check the CLI applies.

A project that declares `<agent directory>/response_schema.json` answers in
JSON of that shape (the template's `app_utils/structured.py`). The schema may
use only the JSON Schema subset the runtime checks answers with, and its root
is an object; `create --response-schema` refuses to seed any other file, and
`lint` reports one. The rules are the template's own (the SHARED block below,
byte-identical there), so a schema the CLI accepts starts the agent.
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any

import click

SCHEMA_FILENAME = "response_schema.json"


class ResponseSchemaFileError(click.ClickException):
    """A response schema that cannot be read or uses what the answer check cannot check (exit 3)."""

    exit_code = 3

    def __init__(self, path: str | Path, problems: list[str]) -> None:
        self.path = Path(path)
        self.problems = list(problems)
        lines = "\n".join(f"  - {p}" for p in self.problems)
        super().__init__(f"Invalid response schema {self.path}:\n{lines}")


# --- BEGIN SHARED RESPONSE SCHEMA RULES ---
# The JSON Schema subset a response schema may use, and the check of a schema
# against it. The same block is in graph_agents_cli/_response_schema.py, which
# `create --response-schema` and `lint` use: keep the two byte-identical
# (tests/dev/test_response_schema_parity.py), so a schema the CLI accepts
# starts the agent. No Jinja, no imports but json, math, re and typing.Any.

# Keywords that are checked, and annotations that are allowed and ignored.
VALIDATION_KEYWORDS = frozenset(
    {
        "type",
        "enum",
        "const",
        "properties",
        "required",
        "additionalProperties",
        "minProperties",
        "maxProperties",
        "items",
        "minItems",
        "maxItems",
        "uniqueItems",
        "minLength",
        "maxLength",
        "pattern",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "anyOf",
        "oneOf",
        "allOf",
        "not",
        "$ref",
        "$defs",
        "definitions",
    }
)
ANNOTATION_KEYWORDS = frozenset(
    {
        "$schema",
        "$id",
        "$comment",
        "title",
        "description",
        "default",
        "examples",
        "deprecated",
        "readOnly",
        "writeOnly",
        "format",
    }
)
SUPPORTED_KEYWORDS = VALIDATION_KEYWORDS | ANNOTATION_KEYWORDS
JSON_TYPES = frozenset({"object", "array", "string", "integer", "number", "boolean", "null"})
_SCHEMA_LISTS = ("anyOf", "oneOf", "allOf")
_SCHEMA_MAPS = ("properties", "$defs", "definitions")
_SCHEMA_VALUES = ("items", "additionalProperties", "not")
_COUNTS = ("minProperties", "maxProperties", "minItems", "maxItems", "minLength", "maxLength")
_NUMBERS = ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf")


def _pointer(schema: Any, ref: str) -> Any:
    """The subschema a local `$ref` (`#`, `#/$defs/name`, ...) names; KeyError when none."""
    if ref == "#":
        return schema
    if not ref.startswith("#/"):
        raise KeyError(ref)
    node = schema
    for raw in ref[2:].split("/"):
        key = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(node, dict) and key in node:
            node = node[key]
        elif isinstance(node, list) and key.isdigit() and int(key) < len(node):
            node = node[int(key)]
        else:
            raise KeyError(ref)
    return node


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def schema_problems(schema: Any) -> list[str]:
    """What stops `schema` from being a response schema; empty when it can be one.

    The root is an object schema (`"type": "object"`); every keyword anywhere
    is one `SUPPORTED_KEYWORDS` lists, with a value of the right kind; every
    `$ref` points inside the document; every `pattern` compiles.
    """
    if not isinstance(schema, dict):
        return ["the schema must be a JSON object"]
    problems: list[str] = []
    if schema.get("type") != "object":
        problems.append('$: the root must be an object schema ("type": "object")')

    def walk(node: Any, where: str) -> None:
        if isinstance(node, bool):
            return
        if not isinstance(node, dict):
            problems.append(f"{where}: a schema must be an object or a boolean")
            return
        for key, value in node.items():
            at = f"{where}.{key}"
            if key not in SUPPORTED_KEYWORDS:
                problems.append(f"{at}: `{key}` is not a keyword the answer check supports")
            elif key == "type":
                types = value if isinstance(value, list) else [value]
                if not types or not all(t in JSON_TYPES for t in types):
                    problems.append(f"{at}: must name JSON types ({', '.join(sorted(JSON_TYPES))})")
            elif key == "required":
                if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                    problems.append(f"{at}: must be a list of property names")
            elif key == "enum":
                if not isinstance(value, list) or not value:
                    problems.append(f"{at}: must be a non-empty list")
            elif key in _COUNTS:
                if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                    problems.append(f"{at}: must be a whole number >= 0")
            elif key in _NUMBERS:
                if not _is_number(value) or (key == "multipleOf" and value <= 0):
                    problems.append(
                        f"{at}: must be a number" + (" > 0" if key == "multipleOf" else "")
                    )
            elif key == "uniqueItems":
                if not isinstance(value, bool):
                    problems.append(f"{at}: must be true or false")
            elif key == "pattern":
                try:
                    re.compile(value)
                except (re.error, TypeError) as exc:
                    problems.append(f"{at}: not a regular expression ({exc})")
            elif key == "$ref":
                try:
                    _pointer(schema, value if isinstance(value, str) else "")
                except KeyError:
                    problems.append(f"{at}: {value!r} does not point inside this schema")
            elif key in _SCHEMA_LISTS:
                if not isinstance(value, list) or not value:
                    problems.append(f"{at}: must be a non-empty list of schemas")
                else:
                    for i, sub in enumerate(value):
                        walk(sub, f"{at}[{i}]")
            elif key in _SCHEMA_MAPS:
                if not isinstance(value, dict):
                    problems.append(f"{at}: must map names to schemas")
                else:
                    for name, sub in value.items():
                        walk(sub, f"{at}.{name}")
            elif key in _SCHEMA_VALUES:
                if key == "items" and isinstance(value, list):
                    problems.append(
                        f"{at}: must be one schema (a list of schemas is not supported)"
                    )
                else:
                    walk(value, at)

    walk(schema, "$")
    return problems


# --- END SHARED RESPONSE SCHEMA RULES ---


def read_schema(path: Path) -> tuple[dict[str, Any] | None, list[str]]:
    """``(schema, [])`` for a file that can be the response schema, else ``(None, problems)``."""
    try:
        schema = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, [f"not a JSON file ({exc})"]
    problems = schema_problems(schema)
    return (None, problems) if problems else (schema, [])


def load_schema(path: str | Path) -> dict[str, Any]:
    """The schema in ``path``; ``ResponseSchemaFileError`` listing every problem."""
    schema, problems = read_schema(Path(path))
    if schema is None:
        raise ResponseSchemaFileError(path, problems)
    return schema


def schema_file(project_root: Path, agent_directory: str) -> Path:
    """Where a project keeps its response schema."""
    return project_root / agent_directory / SCHEMA_FILENAME


def wired(project_root: Path, agent_directory: str) -> bool:
    """Whether the project's ``agent.py`` builds its agent with ``response_format(...)``."""
    try:
        text = (project_root / agent_directory / "agent.py").read_text(encoding="utf-8")
    except OSError:
        return False
    return "response_format(" in text
