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

"""The key-by-key merge enhance falls back on when a line merge of a config file fails."""

from __future__ import annotations

import yaml

from graph_agents_cli.scaffold.utils.keymerge import MISSING, merge_keys
from graph_agents_cli.scaffold.utils.merge3 import merge3_checked

VALUES = "deployment/helm/agent/values.yaml"

BASE = """\
# Chart defaults.
replicaCount: 1

# Informational.
runtime: fastapi

env:
  APP_ENV: prod
  MODEL_PROVIDER: openai
  MODEL_NAME: gpt-5-mini
  CHECKPOINTER: postgres
  AUTH_POLICY: shared-bearer
  PORT: "8000"

redis:
  enabled: false
"""

THEIRS = """\
# Chart defaults.
replicaCount: 1

# Informational.
runtime: langgraph-server

env:
  APP_ENV: prod
  MODEL_PROVIDER: anthropic
  MODEL_NAME: claude-sonnet-5
  AUTH_POLICY: shared-bearer
  PORT: "8000"

redis:
  enabled: false
"""


def _edit(text: str, old: str, new: str) -> str:
    assert old in text
    return text.replace(old, new, 1)


def test_adjacent_edits_defeat_the_line_merge_but_not_the_key_merge() -> None:
    ours = _edit(BASE, "  CHECKPOINTER: postgres\n", "  CHECKPOINTER: postgres\n  MINE: x\n")
    ours = _edit(ours, "replicaCount: 1", "replicaCount: 3  # scaled")
    assert merge3_checked(BASE, ours, THEIRS, VALUES) is None

    result = merge_keys(BASE, ours, THEIRS, VALUES)
    assert result is not None and not result.conflicts
    expected = _edit(THEIRS, "replicaCount: 1", "replicaCount: 3  # scaled")
    expected = _edit(expected, "claude-sonnet-5\n", "claude-sonnet-5\n  MINE: x\n")
    assert result.text == expected
    assert ("runtime",) in result.applied and ("env", "CHECKPOINTER") in result.applied


def test_a_key_the_developer_changed_is_kept_and_reported() -> None:
    ours = _edit(BASE, "MODEL_NAME: gpt-5-mini", "MODEL_NAME: gpt-4.1")
    result = merge_keys(BASE, ours, THEIRS, VALUES)
    assert result is not None
    assert [c.path for c in result.conflicts] == [("env", "MODEL_NAME")]
    assert result.conflicts[0].describe() == (
        "env.MODEL_NAME: you set 'gpt-4.1'; the new settings set 'claude-sonnet-5'"
    )
    data = yaml.safe_load(result.text)
    assert data["runtime"] == "langgraph-server"
    assert data["env"]["MODEL_PROVIDER"] == "anthropic"
    assert data["env"]["MODEL_NAME"] == "gpt-4.1"
    assert "CHECKPOINTER" not in data["env"]


def test_a_removed_key_is_reported_not_re_added() -> None:
    ours = _edit(BASE, "# Informational.\nruntime: fastapi\n", "")
    result = merge_keys(BASE, ours, THEIRS, VALUES)
    assert result is not None
    conflict = next(c for c in result.conflicts if c.path == ("runtime",))
    assert conflict.yours is MISSING
    assert "you removed it; the new settings set 'langgraph-server'" in conflict.describe()
    assert "runtime:" not in result.text


def test_added_entries_bring_their_comments_and_land_next_to_their_neighbour() -> None:
    base = "env:\n  APP_ENV: prod\n  AUTH_POLICY: shared-bearer\n  A2A_NAME: app\n"
    theirs = (
        "env:\n  APP_ENV: prod\n  AUTH_POLICY: jwt\n"
        '  # Verified tokens: set the issuer.\n  AUTH_JWT_ISSUER: ""\n  A2A_NAME: app\n'
    )
    # Four-space indentation and a key of the developer's right after AUTH_POLICY.
    ours = (
        "env:\n    APP_ENV: prod\n    AUTH_POLICY: shared-bearer\n    MINE: x\n    A2A_NAME: app\n"
    )
    result = merge_keys(base, ours, theirs, VALUES)
    assert result is not None and not result.conflicts
    assert result.text == (
        "env:\n    APP_ENV: prod\n    AUTH_POLICY: jwt\n"
        '    # Verified tokens: set the issuer.\n    AUTH_JWT_ISSUER: ""\n'
        "    MINE: x\n    A2A_NAME: app\n"
    )


def test_a_removed_entry_takes_only_the_templates_own_comment_with_it() -> None:
    base = "a: 1\n# Section heading\n# about b only\nb: 2\nc: 3\n"
    theirs = "a: 1\n# Section heading\nc: 3\n"
    ours = "a: 9\n# Section heading\n# about b only\nb: 2\nc: 3\n"
    result = merge_keys(base, ours, theirs, "values-dev.yaml")
    assert result is not None
    # a: 9 is the developer's; b and its own comment go; the heading stays.
    assert result.text == "a: 9\n# Section heading\nc: 3\n"


def test_a_block_added_by_the_template_next_to_a_developer_block_is_not_duplicated() -> None:
    base = "postgresql:\n  enabled: true\ngateway:\n  enabled: false\n"
    theirs = "postgresql:\n  enabled: true\nredis:\n  enabled: true\ngateway:\n  enabled: false\n"
    ours = "postgresql:\n  enabled: true\nredis:\n  enabled: true\n  mine: 1\ngateway:\n  enabled: false\n"
    result = merge_keys(base, ours, theirs, "values-dev.yaml")
    assert result is not None
    assert result.text == ours
    assert [c.path for c in result.conflicts] == [("redis",)]


def test_flow_mappings_are_left_to_the_developer() -> None:
    base = "env: {A: 1, B: 2}\nother: 1\n"
    theirs = "env: {A: 1, B: 2, C: 3}\nother: 1\n"
    ours = "env: {A: 1, B: 2}\nother: 2\n"
    result = merge_keys(base, ours, theirs, "values.yaml")
    assert result is not None
    assert result.text == ours
    assert [c.path for c in result.conflicts] == [("env", "C")]


def test_crlf_and_a_missing_final_newline_are_kept() -> None:
    base = "a: 1\r\nb: 2\r\nc: x"
    theirs = "a: 1\r\nb: 3\r\nc: x"
    ours = "a: 0\r\nb: 2\r\nc: x"
    result = merge_keys(base, ours, theirs, "values.yaml")
    assert result is not None
    assert result.text == "a: 0\r\nb: 3\r\nc: x"


def test_env_files_are_merged_by_variable() -> None:
    base = "# Model\nMODEL_PROVIDER=openai\nOPENAI_API_KEY=\n# memory | postgres\nCHECKPOINTER=memory\nAPI_KEY=\n"
    theirs = "# Model\nMODEL_PROVIDER=anthropic\nANTHROPIC_API_KEY=\nAPI_KEY=\n"
    ours = "# Model\nMODEL_PROVIDER=openai\nMINE=1\nOPENAI_API_KEY=\n# memory | postgres\nCHECKPOINTER=memory\nAPI_KEY=\n"
    assert merge3_checked(base, ours, theirs, ".env.example") is None
    result = merge_keys(base, ours, theirs, ".env.example")
    assert result is not None and not result.conflicts
    assert (
        result.text == "# Model\nMODEL_PROVIDER=anthropic\nANTHROPIC_API_KEY=\nMINE=1\nAPI_KEY=\n"
    )

    agent_env = merge_keys(
        "RUNTIME=fastapi\nCD=skip\n",
        "X=1\nRUNTIME=fastapi\nCD=skip\n",
        "RUNTIME=langgraph-server\nCD=skip\n",
        ".github/agent.env",
    )
    assert agent_env is not None
    assert agent_env.text == "X=1\nRUNTIME=langgraph-server\nCD=skip\n"


def test_unsupported_or_unparseable_files_return_none() -> None:
    assert merge_keys("a\n", "b\n", "c\n", "Dockerfile") is None
    assert merge_keys("a: 1\n", "a: [\n", "a: 2\n", "values.yaml") is None
    assert merge_keys("- 1\n", "- 2\n", "- 3\n", "values.yaml") is None
    # A line separator PyYAML counts but a split on newlines would not.
    assert merge_keys("a: 1\n", "a: 1\rb: 2\n", "a: 2\n", "values.yaml") is None
