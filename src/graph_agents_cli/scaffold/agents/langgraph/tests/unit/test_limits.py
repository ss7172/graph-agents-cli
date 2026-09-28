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

"""Limits and settings: defaults in code, bad values refused, metadata caps, model limits."""

from __future__ import annotations

import pytest

from {{cookiecutter.agent_directory}}.app_utils import limits
from {{cookiecutter.agent_directory}}.app_utils.chat import forward_header_names, select_forward_headers
from {{cookiecutter.agent_directory}}.app_utils.checkpointer import pool_sizes
from {{cookiecutter.agent_directory}}.app_utils.limits import SettingsError, check_metadata
from {{cookiecutter.agent_directory}}.app_utils.model import (
    build_model,
    get_judge_model,
    get_model,
    model_api_options,
    model_limits,
    model_options,
)

ENV = (
    "RUN_TIMEOUT_S",
    "RECURSION_LIMIT",
    "MAX_REQUEST_BYTES",
    "MAX_METADATA_KEYS",
    "MAX_METADATA_VALUE_CHARS",
    "SSE_HEARTBEAT_S",
    "RETENTION_DAYS",
    "MODEL_TIMEOUT_S",
    "MODEL_MAX_RETRIES",
    "MODEL_REASONING_EFFORT",
    "MODEL_USE_RESPONSES_API",
    "JUDGE_MODEL_PROVIDER",
    "JUDGE_REASONING_EFFORT",
    "JUDGE_USE_RESPONSES_API",
    "DB_POOL_MIN_SIZE",
    "DB_POOL_MAX_SIZE",
    "AUTH_FORWARD_HEADERS",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ENV:
        monkeypatch.delenv(name, raising=False)


def test_defaults_are_finite() -> None:
    assert limits.run_timeout_s() == 300
    assert limits.recursion_limit() == 50
    # Room for 24 tool calls one after another (two steps each) plus the answer.
    assert limits.sequential_tool_calls(limits.recursion_limit()) == 24
    assert limits.max_request_bytes() == 1_048_576
    assert limits.max_metadata_keys() == 16
    assert limits.max_metadata_value_chars() == 256
    assert limits.sse_heartbeat_s() == 15
    assert limits.retention_days() == 0
    assert model_limits() == (60.0, 2)
    assert pool_sizes() == (1, 10)
    limits.check_settings()


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("RUN_TIMEOUT_S", "abc"),
        ("RUN_TIMEOUT_S", "0"),
        ("RUN_TIMEOUT_S", "inf"),
        ("RUN_TIMEOUT_S", "nan"),
        ("RECURSION_LIMIT", "0"),
        ("RECURSION_LIMIT", "2.5"),
        ("MAX_REQUEST_BYTES", "1MB"),
        ("MAX_METADATA_KEYS", "-1"),
        ("SSE_HEARTBEAT_S", "-5"),
        ("RETENTION_DAYS", "seven"),
    ],
)
def test_a_bad_value_is_a_startup_error_not_a_silent_default(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(SettingsError) as exc:
        limits.check_settings()
    assert name in str(exc.value)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("MODEL_TIMEOUT_S", "fast"),
        ("MODEL_TIMEOUT_S", "-1"),
        ("MODEL_MAX_RETRIES", "-1"),
        ("MODEL_MAX_RETRIES", "two"),
    ],
)
def test_bad_model_limits_are_refused(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(SettingsError):
        model_limits()


@pytest.mark.parametrize(("low", "high"), [("5", "2"), ("0", "0"), ("x", "3"), ("-1", "3")])
def test_bad_pool_sizes_are_refused(monkeypatch: pytest.MonkeyPatch, low: str, high: str) -> None:
    monkeypatch.setenv("DB_POOL_MIN_SIZE", low)
    monkeypatch.setenv("DB_POOL_MAX_SIZE", high)
    with pytest.raises(SettingsError):
        pool_sizes()


def test_provider_models_get_a_timeout_and_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    model = build_model("openai", "gpt-test", api_key="sk-test")
    assert model.request_timeout == 60.0 and model.max_retries == 2
    monkeypatch.setenv("MODEL_TIMEOUT_S", "12.5")
    monkeypatch.setenv("MODEL_MAX_RETRIES", "0")
    model = build_model("anthropic", "claude-test", api_key="k")
    assert model.default_request_timeout == 12.5 and model.max_retries == 0
    monkeypatch.setenv("MODEL_TIMEOUT_S", "0")  # the provider SDK's own default
    model = build_model("openai", "gpt-test", api_key="sk-test")
    assert model.request_timeout is None
    # An explicit argument wins over the environment.
    model = build_model("openai", "gpt-test", api_key="sk-test", timeout=3, max_retries=5)
    assert model.request_timeout == 3 and model.max_retries == 5


# --- reasoning effort and the Responses API (OpenAI-API models) ---------------------------


@pytest.fixture
def openai_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODEL_PROVIDER", "openai")
    monkeypatch.setenv("MODEL_NAME", "gpt-test")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")


def test_the_model_api_settings_are_unset_by_default(openai_model: None) -> None:
    assert model_options() == ({}, {})
    model = get_model()
    assert model.reasoning_effort is None and model.use_responses_api is None


@pytest.mark.parametrize(
    ("effort", "switch", "expected"),
    [
        ("high", "true", {"reasoning_effort": "high", "use_responses_api": True}),
        (" NONE ", "False", {"reasoning_effort": "none", "use_responses_api": False}),
        ("xhigh", "", {"reasoning_effort": "xhigh"}),
        ("", "1", {"use_responses_api": True}),
        ("minimal", "off", {"reasoning_effort": "minimal", "use_responses_api": False}),
    ],
)
def test_the_model_api_settings_reach_the_model(
    openai_model: None,
    monkeypatch: pytest.MonkeyPatch,
    effort: str,
    switch: str,
    expected: dict[str, object],
) -> None:
    monkeypatch.setenv("MODEL_REASONING_EFFORT", effort)
    monkeypatch.setenv("MODEL_USE_RESPONSES_API", switch)
    assert model_api_options() == expected
    model = get_model()
    assert model.reasoning_effort == expected.get("reasoning_effort")
    assert model.use_responses_api == expected.get("use_responses_api")
    # An explicit argument wins over the environment.
    assert get_model(reasoning_effort="low").reasoning_effort == "low"


@pytest.mark.parametrize(
    ("name", "value", "fragment"),
    [
        (
            "MODEL_REASONING_EFFORT",
            "hihg",
            "must be one of none, minimal, low, medium, high, xhigh",
        ),
        ("MODEL_USE_RESPONSES_API", "maybe", "must be true or false"),
        ("JUDGE_REASONING_EFFORT", "max", "must be one of"),
        ("JUDGE_USE_RESPONSES_API", "2", "must be true or false"),
    ],
)
def test_a_bad_model_api_setting_stops_startup(
    openai_model: None, monkeypatch: pytest.MonkeyPatch, name: str, value: str, fragment: str
) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(SettingsError) as exc:
        limits.check_settings(extra=(model_options,))
    assert name in str(exc.value) and fragment in str(exc.value)


@pytest.mark.parametrize("provider", ["anthropic", "gemini"])
def test_the_model_api_settings_are_refused_for_another_provider(
    monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    """They would be ignored (they are OpenAI API parameters): startup says so."""
    monkeypatch.setenv("MODEL_PROVIDER", provider)
    monkeypatch.setenv("MODEL_REASONING_EFFORT", "high")
    with pytest.raises(SettingsError) as exc:
        model_options()
    assert "MODEL_REASONING_EFFORT applies to an OpenAI-API model" in str(exc.value)
    assert repr(provider) in str(exc.value)
    monkeypatch.setenv("MODEL_PROVIDER", "fake")  # the test model takes and ignores them
    assert model_options() == ({}, {})


def test_the_judge_keeps_the_agents_settings_or_takes_its_own(
    openai_model: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MODEL_REASONING_EFFORT", "high")
    monkeypatch.setenv("MODEL_USE_RESPONSES_API", "true")
    agent = {"reasoning_effort": "high", "use_responses_api": True}
    assert model_options() == (agent, agent)
    judge = get_judge_model()
    assert (judge.reasoning_effort, judge.use_responses_api) == ("high", True)
    monkeypatch.setenv("JUDGE_REASONING_EFFORT", "low")
    monkeypatch.setenv("JUDGE_USE_RESPONSES_API", "false")
    assert model_api_options(judge=True) == {"reasoning_effort": "low", "use_responses_api": False}
    assert model_api_options() == agent
    # A judge that is not an OpenAI-API model drops the agent's settings ...
    monkeypatch.delenv("JUDGE_REASONING_EFFORT")
    monkeypatch.delenv("JUDGE_USE_RESPONSES_API")
    monkeypatch.setenv("JUDGE_MODEL_PROVIDER", "anthropic")
    assert model_options() == (agent, {})
    # ... and refuses its own.
    monkeypatch.setenv("JUDGE_USE_RESPONSES_API", "true")
    with pytest.raises(SettingsError) as exc:
        model_options()
    assert "JUDGE_USE_RESPONSES_API applies to an OpenAI-API model" in str(exc.value)


def test_metadata_caps(monkeypatch: pytest.MonkeyPatch) -> None:
    ok = {"a": "x" * 256, "b": 1, "c": 2.5, "d": True, "e": None}
    assert check_metadata(ok) == ok
    for bad in (
        {f"k{i}": 1 for i in range(17)},
        {"a": "x" * 257},
        {"x" * 257: 1},
        {"a": {"nested": 1}},
        {"a": [1]},
        {"": 1},
        {"a": float("nan")},
    ):
        with pytest.raises(ValueError):
            check_metadata(bad)
    monkeypatch.setenv("MAX_METADATA_KEYS", "0")
    assert check_metadata({}) == {}
    with pytest.raises(ValueError):
        check_metadata({"a": 1})


@pytest.mark.parametrize(
    ("thread_id", "valid"),
    [
        ("11111111-1111-1111-1111-111111111111", True),
        ("t-demo_1.a:b", True),
        ("x" * 128, True),
        ("x" * 129, False),
        ("", False),
        ("a b", False),
        ("a/b", False),
        ("a\nb", False),
        ("ümlaut", False),
    ],
)
def test_thread_id_form(thread_id: str, valid: bool) -> None:
    assert limits.valid_thread_id(thread_id) is valid


def test_forwarded_headers_are_generic_and_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    headers = {
        "Authorization": "Bearer k",
        "cookie": "sid=1",
        "x-session-token": "t",
        "x-api-key": "k2",
    }
    assert forward_header_names() == {"authorization", "cookie"}
    assert select_forward_headers(headers) == {"Authorization": "Bearer k", "cookie": "sid=1"}
    monkeypatch.setenv("AUTH_FORWARD_HEADERS", " X-Api-Key , cookie ")
    assert select_forward_headers(headers) == {"cookie": "sid=1", "x-api-key": "k2"}
    monkeypatch.setenv("AUTH_FORWARD_HEADERS", "")
    assert select_forward_headers(headers) == {}
