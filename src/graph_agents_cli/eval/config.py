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

"""``tests/eval/eval_config.yaml``: judge identity, quality metrics, rubrics.

Shape::

    judge: { provider: null, model: null }          # null = agent's provider/model
                                                     # max_tool_result_chars: 50000 (null = never cut)
    quality_metrics:                                 # only these may be below 100 percent
      response_quality: { threshold: 4, min_pass_rate: 0.9 }
    judges:                                          # rubric text is versioned here
      response_quality: { scale: 5, rubric: "...", prompt_template: "..." }
    custom_metrics: []                               # python callables: module:function

Built-in judges (``response_quality``, ``task_success``, ``groundedness``) work
without a ``judges:`` block; an entry there overrides any of their fields or
defines a new rubric.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from graph_agents_cli.eval._common import EvalConfigError
from graph_agents_cli.eval.transcript import DEFAULT_MAX_TOOL_RESULT_CHARS, render_tool_calls

DEFAULT_SCALE = 5

DEFAULT_PROMPT_TEMPLATE = """You are a strict, impartial evaluator of an AI agent's reply.

Rubric ({metric}):
{rubric}

Conversation so far (earlier turns in full: the user's message, each tool call the agent made \
with its result, and the agent's reply; then the user's latest message):
{conversation}

Agent response (the reply to the latest message; this is what you score):
{response}
{reference_section}{context_section}{tool_calls_section}
Score the agent response from 1 to {scale} against the rubric ({scale} is best), taking the \
earlier turns into account.
Reply with JSON only, on one line: {{"score": <number>, "reasoning": "<one or two sentences>"}}"""

# Placeholders a judge prompt_template may use; any other one is exit 3.
PROMPT_PLACEHOLDERS: tuple[str, ...] = (
    "metric",
    "rubric",
    "scale",
    "conversation",
    "transcript",
    "response",
    "reference",
    "context",
    "reference_section",
    "context_section",
    "tool_calls_section",
)

JUDGE_KEYS: tuple[str, ...] = ("provider", "model", "max_tool_result_chars")

BUILTIN_JUDGES: dict[str, dict[str, Any]] = {
    "response_quality": {
        "description": "Accuracy, relevance and clarity of the final response.",
        "scale": DEFAULT_SCALE,
        "rubric": (
            "Rate the response for accuracy, relevance to the user's request, and clarity. "
            "5: fully correct, directly answers, clear and concise. 3: mostly correct but "
            "incomplete, vague or verbose. 1: wrong, off-topic, or unusable. When a reference "
            "answer is given, penalize factual disagreement with it."
        ),
    },
    "task_success": {
        "description": "Whether the agent accomplished what the user asked for.",
        "scale": DEFAULT_SCALE,
        "rubric": (
            "Judge whether the agent completed the user's task end to end, including using "
            "tools when the task required it. In a multi-turn conversation the task is the "
            "latest request in light of the earlier turns; what the agent already did or said "
            "in an earlier turn counts and need not be repeated. 5: task fully accomplished "
            "with a correct outcome. 3: partially accomplished or needs a follow-up from the "
            "user. 1: task not attempted, abandoned, or completed incorrectly."
        ),
    },
    "groundedness": {
        "description": "Whether every claim is supported by the supplied context or tool results.",
        "scale": DEFAULT_SCALE,
        "rubric": (
            "Check every factual claim in the response against the supplied context and the "
            "tool results of every turn, including earlier turns of the conversation. A tool "
            "result marked TRUNCATED was cut for length: a claim that could come from its "
            "omitted part is not unsupported. 5: every claim is supported. 3: mostly "
            "supported with minor unsupported detail. 1: contradicts the context or invents "
            "facts."
        ),
    },
}


@dataclass
class JudgeSpec:
    name: str
    scale: float = DEFAULT_SCALE
    rubric: str = ""
    prompt_template: str = DEFAULT_PROMPT_TEMPLATE
    description: str = ""
    threshold: float | None = None
    builtin: bool = False


@dataclass
class QualityMetric:
    name: str
    threshold: float
    min_pass_rate: float = 1.0


@dataclass
class CustomMetric:
    name: str
    callable: str  # "module:function", imported inside the project's environment
    threshold: float = 1.0
    description: str = ""


@dataclass
class EvalConfig:
    judge_provider: str | None = None
    judge_model: str | None = None
    # Characters of one tool result shown to a judge; None = never cut.
    max_tool_result_chars: int | None = DEFAULT_MAX_TOOL_RESULT_CHARS
    quality_metrics: dict[str, QualityMetric] = field(default_factory=dict)
    judges: dict[str, JudgeSpec] = field(default_factory=dict)
    custom_metrics: dict[str, CustomMetric] = field(default_factory=dict)
    source: Path | None = None

    def metric_names(self) -> set[str]:
        return set(self.judges) | set(self.custom_metrics)

    def is_quality(self, metric: str) -> bool:
        return metric in self.quality_metrics


def builtin_judges() -> dict[str, JudgeSpec]:
    return {
        name: JudgeSpec(
            name=name,
            scale=spec["scale"],
            rubric=spec["rubric"],
            description=spec["description"],
            builtin=True,
        )
        for name, spec in BUILTIN_JUDGES.items()
    }


def _number(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise EvalConfigError(f"{where} must be a number, got {value!r}")
    return float(value)


def _parse_judges(raw: Any, where: str) -> dict[str, JudgeSpec]:
    judges = builtin_judges()
    if raw is None:
        return judges
    if not isinstance(raw, dict):
        raise EvalConfigError(f"{where}: 'judges' must be a mapping of metric name to spec")
    for name, spec in raw.items():
        if spec is None:
            spec = {}
        if not isinstance(spec, dict):
            raise EvalConfigError(f"{where}: judges.{name} must be a mapping")
        base = judges.get(name) or JudgeSpec(name=str(name))
        if "scale" in spec and spec["scale"] is not None:
            base.scale = _number(spec["scale"], f"{where}: judges.{name}.scale")
        if "rubric" in spec and spec["rubric"] is not None:
            base.rubric = str(spec["rubric"])
        if "prompt_template" in spec and spec["prompt_template"] is not None:
            base.prompt_template = str(spec["prompt_template"])
        if "description" in spec and spec["description"] is not None:
            base.description = str(spec["description"])
        if "threshold" in spec and spec["threshold"] is not None:
            base.threshold = _number(spec["threshold"], f"{where}: judges.{name}.threshold")
        if not base.rubric:
            raise EvalConfigError(f"{where}: judges.{name} needs a 'rubric'")
        _check_placeholders(base, where)
        judges[str(name)] = base
    return judges


def _check_placeholders(spec: JudgeSpec, where: str) -> None:
    """A template with an unknown placeholder fails when the config loads (exit 3).

    ``eval run`` loads the config before generating, so the mistake costs no
    agent calls instead of surfacing at the first judged case.
    """
    try:
        spec.prompt_template.format(**dict.fromkeys(PROMPT_PLACEHOLDERS, ""))
    except (KeyError, IndexError, ValueError) as exc:
        raise EvalConfigError(
            f"{where}: judges.{spec.name}.prompt_template has an unknown placeholder: {exc} "
            f"(allowed: {', '.join('{' + p + '}' for p in PROMPT_PLACEHOLDERS)})"
        ) from exc


def _parse_custom_metrics(raw: Any, where: str) -> dict[str, CustomMetric]:
    metrics: dict[str, CustomMetric] = {}
    if raw is None:
        return metrics
    if not isinstance(raw, list):
        raise EvalConfigError(f"{where}: 'custom_metrics' must be a list")
    for i, item in enumerate(raw):
        if isinstance(item, str):
            item = {"callable": item}
        if not isinstance(item, dict):
            raise EvalConfigError(
                f"{where}: custom_metrics[{i}] must be 'module:function' or a mapping"
            )
        target = item.get("callable") or item.get("function")
        if not isinstance(target, str) or ":" not in target:
            raise EvalConfigError(
                f"{where}: custom_metrics[{i}] needs a 'callable' of the form module:function"
            )
        name = str(item.get("name") or target.rsplit(":", 1)[1])
        threshold = item.get("threshold", 1.0)
        metric = CustomMetric(
            name=name,
            callable=target,
            threshold=_number(threshold, f"{where}: custom_metrics[{i}].threshold"),
            description=str(item.get("description") or ""),
        )
        if name in metrics:
            raise EvalConfigError(f"{where}: duplicate custom metric {name!r}")
        metrics[name] = metric
    return metrics


def _parse_quality_metrics(raw: Any, where: str, known: set[str]) -> dict[str, QualityMetric]:
    metrics: dict[str, QualityMetric] = {}
    if raw is None:
        return metrics
    if not isinstance(raw, dict):
        raise EvalConfigError(f"{where}: 'quality_metrics' must be a mapping")
    for name, spec in raw.items():
        if spec is None:
            spec = {}
        if isinstance(spec, int | float) and not isinstance(spec, bool):
            spec = {"threshold": spec}
        if not isinstance(spec, dict):
            raise EvalConfigError(f"{where}: quality_metrics.{name} must be a mapping")
        if name not in known:
            raise EvalConfigError(
                f"{where}: quality metric {name!r} is not a known judge or custom metric "
                f"(known: {', '.join(sorted(known))})"
            )
        if spec.get("threshold") is None:
            raise EvalConfigError(f"{where}: quality metric {name!r} has no threshold")
        threshold = _number(spec["threshold"], f"{where}: quality_metrics.{name}.threshold")
        rate = spec.get("min_pass_rate", 1.0)
        rate = _number(rate, f"{where}: quality_metrics.{name}.min_pass_rate")
        if not 0.0 <= rate <= 1.0:
            raise EvalConfigError(
                f"{where}: quality_metrics.{name}.min_pass_rate must be between 0 and 1"
            )
        metrics[str(name)] = QualityMetric(name=str(name), threshold=threshold, min_pass_rate=rate)
    return metrics


def _max_tool_result_chars(judge: dict[str, Any], where: str) -> int | None:
    """``judge.max_tool_result_chars``: absent = the default, null = never cut, else >= 1."""
    if "max_tool_result_chars" not in judge:
        return DEFAULT_MAX_TOOL_RESULT_CHARS
    value = judge["max_tool_result_chars"]
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise EvalConfigError(
            f"{where}: judge.max_tool_result_chars must be a whole number of characters "
            f">= 1, or null to never cut tool results (got {value!r})"
        )
    return value


def parse_eval_config(data: Any, source: Path | None = None) -> EvalConfig:
    """Validate a parsed YAML mapping into an :class:`EvalConfig`."""
    where = str(source) if source else "eval config"
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise EvalConfigError(f"{where}: top level must be a mapping")
    unknown = sorted(set(data) - {"judge", "quality_metrics", "judges", "custom_metrics"})
    if unknown:
        raise EvalConfigError(f"{where}: unknown top-level key(s) {', '.join(unknown)}")

    judge = data.get("judge") or {}
    if not isinstance(judge, dict):
        raise EvalConfigError(f"{where}: 'judge' must be a mapping with provider/model")
    unknown_judge = sorted(str(k) for k in set(judge) - set(JUDGE_KEYS))
    if unknown_judge:
        raise EvalConfigError(
            f"{where}: unknown judge key(s) {', '.join(unknown_judge)}; "
            f"known: {', '.join(JUDGE_KEYS)}"
        )
    max_chars = _max_tool_result_chars(judge, where)
    judges = _parse_judges(data.get("judges"), where)
    custom = _parse_custom_metrics(data.get("custom_metrics"), where)
    overlap = set(judges) & set(custom)
    if overlap:
        raise EvalConfigError(
            f"{where}: {', '.join(sorted(overlap))} defined both as judge and custom metric"
        )
    quality = _parse_quality_metrics(data.get("quality_metrics"), where, set(judges) | set(custom))
    provider = judge.get("provider")
    model = judge.get("model")
    return EvalConfig(
        judge_provider=str(provider) if provider else None,
        judge_model=str(model) if model else None,
        max_tool_result_chars=max_chars,
        quality_metrics=quality,
        judges=judges,
        custom_metrics=custom,
        source=source,
    )


def load_eval_config(path: Path | None, *, required: bool = False) -> EvalConfig:
    """Load ``eval_config.yaml``; a missing optional file yields the defaults."""
    if path is None:
        return parse_eval_config({}, None)
    path = Path(path)
    if not path.is_file():
        if required:
            raise EvalConfigError(f"eval config not found: {path}")
        return parse_eval_config({}, None)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise EvalConfigError(f"eval config {path} is not valid YAML: {exc}") from exc
    return parse_eval_config(data, path)


def resolve_threshold(config: EvalConfig, metric: str, case_spec: dict[str, Any] | None) -> float:
    """The per-case threshold for ``metric``.

    Precedence: the case's ``judge.<metric>.threshold`` > ``quality_metrics.<metric>.threshold``
    > ``judges.<metric>.threshold`` > a custom metric's threshold. None of them
    is a configuration error (a quality metric with no threshold cannot gate).
    """
    if case_spec and case_spec.get("threshold") is not None:
        return float(case_spec["threshold"])
    quality = config.quality_metrics.get(metric)
    if quality is not None:
        return quality.threshold
    judge = config.judges.get(metric)
    if judge is not None and judge.threshold is not None:
        return judge.threshold
    custom = config.custom_metrics.get(metric)
    if custom is not None:
        return custom.threshold
    raise EvalConfigError(
        f"metric {metric!r} has no threshold: set judge.{metric}.threshold on the case, "
        f"or quality_metrics.{metric}.threshold / judges.{metric}.threshold in eval_config.yaml"
    )


def render_judge_prompt(
    spec: JudgeSpec,
    *,
    conversation: str,
    response: str,
    reference: str | None,
    context: str | None,
    tool_calls: list[dict[str, Any]] | None,
    transcript: str | None = None,
    max_tool_result_chars: int | None = DEFAULT_MAX_TOOL_RESULT_CHARS,
) -> str:
    """Fill the judge prompt template; an unknown placeholder is a configuration error.

    ``conversation`` is everything before the reply being scored (earlier turns
    in full, see :mod:`graph_agents_cli.eval.transcript`), ``tool_calls`` the
    final turn's calls and ``transcript`` the whole case; without one it is
    assembled from the other three. A tool result longer than
    ``max_tool_result_chars`` is cut with an explicit marker, never silently.
    """
    reference_section = f"\nReference answer (ground truth):\n{reference}\n" if reference else ""
    context_section = f"\nGrounding context:\n{context}\n" if context else ""
    lines, _, _ = render_tool_calls(tool_calls, max_tool_result_chars)
    tool_calls_section = ""
    if lines:
        tool_calls_section = (
            "\nTool calls the agent made for this response:\n"
            + "\n".join(f"- {line}" for line in lines)
            + "\n"
        )
    if transcript is None:
        transcript = "\n".join(
            [conversation, *(f"agent tool call: {line}" for line in lines), f"agent: {response}"]
        )
    values = {
        "metric": spec.name,
        "rubric": spec.rubric,
        "conversation": conversation,
        "transcript": transcript,
        "response": response,
        "reference": reference or "",
        "context": context or "",
        "reference_section": reference_section,
        "context_section": context_section,
        "tool_calls_section": tool_calls_section,
        "scale": int(spec.scale) if float(spec.scale).is_integer() else spec.scale,
    }
    try:
        return spec.prompt_template.format(**values)
    except (KeyError, IndexError, ValueError) as exc:
        raise EvalConfigError(
            f"judges.{spec.name}.prompt_template has an unknown placeholder: {exc} "
            f"(allowed: {', '.join('{' + p + '}' for p in PROMPT_PLACEHOLDERS)})"
        ) from exc
