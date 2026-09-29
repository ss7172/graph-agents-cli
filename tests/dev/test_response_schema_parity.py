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

"""`create --response-schema`/`lint` and the scaffolded runtime judge a response schema alike.

The CLI (``graph_agents_cli._response_schema``) and the template's
``app_utils/structured.py`` carry the same block of rules (the JSON Schema
subset the runtime checks answers with). These tests keep the two copies
byte-identical and feed the same schemas to both: a schema the CLI seeds is
one the agent starts with, and one the agent refuses is refused before a
project is created.
"""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any

import pytest

from graph_agents_cli import _response_schema as cli

TEMPLATE = (
    Path(cli.__file__).resolve().parent
    / "scaffold"
    / "agents"
    / "langgraph"
    / "app"
    / "app_utils"
    / "structured.py"
)
BEGIN = "# --- BEGIN SHARED RESPONSE SCHEMA RULES ---"
END = "# --- END SHARED RESPONSE SCHEMA RULES ---"


def _block(text: str) -> str:
    return text[text.index(BEGIN) : text.index(END) + len(END)]


def test_the_shared_block_is_byte_identical_and_free_of_jinja() -> None:
    cli_block = _block(Path(cli.__file__).read_text(encoding="utf-8"))
    runtime_block = _block(TEMPLATE.read_text(encoding="utf-8"))
    assert cli_block == runtime_block, (
        "The SHARED RESPONSE SCHEMA RULES blocks of graph_agents_cli/_response_schema.py and "
        "the template's app_utils/structured.py differ; copy the CLI block into the template."
    )
    assert "{{" not in runtime_block and "{%" not in runtime_block


@pytest.fixture(scope="module")
def runtime() -> dict[str, Any]:
    """The template's block, run on its own (the module around it has template variables)."""
    namespace: dict[str, Any] = {"Any": Any, "math": math, "re": re}
    exec(compile(_block(TEMPLATE.read_text(encoding="utf-8")), str(TEMPLATE), "exec"), namespace)
    return namespace


SCHEMAS: list[Any] = [
    {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]},
    {
        "type": "object",
        "properties": {"o": {"$ref": "#/$defs/o"}, "n": {"anyOf": [{"type": "null"}, {}]}},
        "$defs": {"o": {"type": "object", "additionalProperties": False}},
    },
    {"type": "array"},
    {"type": "object", "if": {}},
    {"type": "object", "properties": {"a": {"pattern": "("}}},
    {"type": "object", "properties": {"a": {"$ref": "#/nowhere"}}},
    {"type": "object", "properties": {"a": {"items": [{}]}}},
    {"type": "object", "properties": {"a": {"minimum": "1"}}},
    "not a schema",
]


@pytest.mark.parametrize("schema", SCHEMAS)
def test_both_sides_accept_and_refuse_the_same_schemas(runtime, schema: Any) -> None:
    assert cli.schema_problems(schema) == runtime["schema_problems"](schema)


def test_the_cli_reads_a_schema_file(tmp_path: Path) -> None:
    good = tmp_path / "good.json"
    good.write_text('{"type": "object"}', encoding="utf-8")
    assert cli.load_schema(good) == {"type": "object"}
    bad = tmp_path / "bad.json"
    bad.write_text('{"type": "object", "then": {}}', encoding="utf-8")
    with pytest.raises(cli.ResponseSchemaFileError) as caught:
        cli.load_schema(bad)
    assert caught.value.exit_code == 3 and "`then` is not a keyword" in caught.value.message
    broken = tmp_path / "broken.json"
    broken.write_text("{", encoding="utf-8")
    assert cli.read_schema(broken)[1][0].startswith("not a JSON file")
