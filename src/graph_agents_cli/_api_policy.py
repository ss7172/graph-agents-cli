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

"""The outbound API access policy (``api-policy.yaml``) as the CLI sees it.

A project declares every external API its tools may call in ``api-policy.yaml``
at the project root::

    apis:
      example:
        base_url_env: EXAMPLE_API_BASE_URL
        auth: bearer                 # none | bearer | forward
        token_env: EXAMPLE_API_TOKEN # auth: bearer only
        allowed_methods: [GET]       # required; ["*"] allows every method
        allowed_operations:          # optional; omitted = every operation
          - operationId: getItem
            path: /items/{item_id}

The schema rules and the matching rules live in the block between the
``SHARED API POLICY RULES`` markers. The scaffolded runtime
(``app_utils/api_client.py``) carries a byte-identical copy of that block, so
``create --api-policy``, ``lint`` and the running agent accept the same files,
report the same errors and refuse the same calls. The rest of this module is
CLI-only: reading the file, summarising it for the templates, and detecting
the retired single-API ``product_api`` format.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

import click
import yaml

# --- BEGIN SHARED API POLICY RULES ---
# Identical in graph-agents-cli (graph_agents_cli/_api_policy.py) and in every
# scaffolded project (app_utils/api_client.py). A CLI test keeps the two copies
# byte-identical: change both or neither.

POLICY_FILENAME = "api-policy.yaml"
AUTH_MODES = ("none", "bearer", "forward")
HTTP_METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS")
ANY_METHOD = "*"
DEFAULT_FORWARD_HEADER = "Authorization"
DEFAULT_TIMEOUTS_MS = (("connect", 2000), ("read", 5000))

API_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
API_NAME_RULE = "lowercase letters, digits and underscores, starting with a letter, 1-32 characters"
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
HEADER_NAME_RE = re.compile(r"^[A-Za-z0-9-]+$")
_PATH_SEGMENT_RE = re.compile(r"^(?:[^/?#\s{}]|\{[A-Za-z_][A-Za-z0-9_]*\})+$")
_PLACEHOLDER_SPLIT_RE = re.compile(r"(\{[^/{}]+\})")

_ESCAPE_RE = re.compile(r"%[0-9A-Fa-f]{2}")
_UNRESERVED = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")

_POLICY_KEYS = ("apis",)
_API_KEYS = (
    "base_url_env",
    "auth",
    "token_env",
    "forward_header",
    "allowed_methods",
    "allowed_operations",
    "denied_operations",
    "openapi",
    "timeouts_ms",
    "pagination",
)
_OPERATION_KEYS = ("operationId", "path", "methods")
_TIMEOUT_KEYS = ("connect", "read")
_PAGINATION_KEYS = ("page_size_param", "max_page_size")

LEGACY_POLICY_HINT = (
    "product_api: is the retired single-API format: move its fields under "
    "apis: <name>: (for example apis: example:), add the now required "
    "allowed_methods (for example [GET]), write auth: forward instead of "
    "forwarded-session, and name the file api-policy.yaml"
)


class PolicyLoader(yaml.SafeLoader):
    """``yaml.SafeLoader`` that refuses a key repeated within one mapping, at any level.

    Plain ``safe_load`` silently keeps the last duplicate, so a reviewer reading
    ``allowed_methods: [GET]`` would miss a later ``allowed_methods: ["*"]``
    that is the one applied. Merge keys (``<<: *anchor``) still work.
    """

    def construct_mapping(self, node: Any, deep: bool = False) -> Any:
        if isinstance(node, yaml.MappingNode):
            seen: set[Any] = set()
            for key_node, _value_node in node.value:
                if key_node.tag == "tag:yaml.org,2002:merge":
                    continue
                key = self.construct_object(key_node, deep=deep)
                try:
                    duplicate = key in seen
                except TypeError:  # an unhashable key: the base loader reports it
                    continue
                if duplicate:
                    raise yaml.constructor.ConstructorError(
                        "while constructing a mapping",
                        node.start_mark,
                        f"found duplicate key {key!r}",
                        key_node.start_mark,
                    )
                seen.add(key)
        return super().construct_mapping(node, deep=deep)


def parse_policy_yaml(text: str) -> tuple[Any, list[str]]:
    """Parse api-policy.yaml text: ``(data, [])``, or ``(None, [error])`` when it is
    not valid YAML (a duplicate key included)."""
    try:
        return yaml.load(text, Loader=PolicyLoader), []
    except yaml.YAMLError as exc:
        return None, [f"not valid YAML: {exc}"]


def policy_errors(data: Any) -> list[str]:
    """Every schema error in a parsed api-policy.yaml document; empty when it is valid.

    Strict: unknown keys at any level are errors, so a typo can never widen access.
    """
    if not isinstance(data, Mapping):
        return ["the document must be a mapping with a top-level 'apis' key"]
    errors: list[str] = []
    if "product_api" in data:
        errors.append(LEGACY_POLICY_HINT)
    for key in sorted(set(data) - set(_POLICY_KEYS) - {"product_api"}, key=str):
        errors.append(f"unknown top-level key {key!r} (allowed: apis)")
    if "apis" not in data:
        if "product_api" not in data:
            errors.append("apis: required (a mapping of API name to its settings)")
        return errors
    apis = data["apis"]
    if not isinstance(apis, Mapping) or not apis:
        errors.append("apis: must be a non-empty mapping of API name to its settings")
        return errors
    for name, api in apis.items():
        errors.extend(_api_errors(name, api))
    return errors


def _is_env_name(value: Any) -> bool:
    return isinstance(value, str) and ENV_NAME_RE.match(value) is not None


def _api_errors(name: Any, api: Any) -> list[str]:
    where = f"apis.{name}"
    errors: list[str] = []
    if not isinstance(name, str) or not API_NAME_RE.match(name):
        errors.append(f"{where}: invalid API name ({API_NAME_RULE})")
    if not isinstance(api, Mapping):
        errors.append(f"{where}: must be a mapping")
        return errors
    for key in sorted(set(api) - set(_API_KEYS), key=str):
        errors.append(f"{where}: unknown key {key!r}")

    if "base_url_env" not in api:
        errors.append(f"{where}.base_url_env: required")
    elif not _is_env_name(api["base_url_env"]):
        errors.append(f"{where}.base_url_env: must be an environment variable name")

    auth = api.get("auth")
    if "auth" not in api:
        errors.append(f"{where}.auth: required (one of none, bearer, forward)")
    elif auth not in AUTH_MODES:
        errors.append(f"{where}.auth: must be one of none, bearer, forward (got {auth!r})")

    if auth == "bearer":
        if "token_env" not in api:
            errors.append(f"{where}.token_env: required when auth is bearer")
        elif not _is_env_name(api["token_env"]):
            errors.append(f"{where}.token_env: must be an environment variable name")
    elif "token_env" in api:
        errors.append(f"{where}.token_env: only valid with auth: bearer")

    if "forward_header" in api:
        header = api["forward_header"]
        if auth != "forward":
            errors.append(f"{where}.forward_header: only valid with auth: forward")
        elif not (isinstance(header, str) and HEADER_NAME_RE.match(header)):
            errors.append(f"{where}.forward_header: must be an HTTP header name")

    if "allowed_methods" not in api:
        errors.append(f'{where}.allowed_methods: required (a list of HTTP methods, or ["*"])')
    else:
        errors.extend(_methods_errors(f"{where}.allowed_methods", api["allowed_methods"], True))

    if "allowed_operations" in api:
        errors.extend(
            _operations_errors(f"{where}.allowed_operations", api["allowed_operations"], False)
        )
    if "denied_operations" in api:
        errors.extend(
            _operations_errors(f"{where}.denied_operations", api["denied_operations"], True)
        )

    if "openapi" in api:
        openapi = api["openapi"]
        if not (isinstance(openapi, str) and openapi.strip()):
            errors.append(f"{where}.openapi: must be a file path")

    if "timeouts_ms" in api:
        errors.extend(_timeouts_errors(f"{where}.timeouts_ms", api["timeouts_ms"]))
    if "pagination" in api:
        errors.extend(_pagination_errors(f"{where}.pagination", api["pagination"]))
    return errors


def _methods_errors(where: str, value: Any, allow_any: bool) -> list[str]:
    if not isinstance(value, list) or not value:
        return [f"{where}: must be a non-empty list of HTTP methods"]
    errors: list[str] = []
    if allow_any and ANY_METHOD in value and len(value) != 1:
        errors.append(f'{where}: "*" must be the only entry when present')
    for method in value:
        if allow_any and method == ANY_METHOD:
            continue
        if not isinstance(method, str) or method.upper() not in HTTP_METHODS:
            errors.append(
                f"{where}: unknown HTTP method {method!r} (allowed: {', '.join(HTTP_METHODS)})"
            )
    return errors


def _operations_errors(where: str, value: Any, allow_empty: bool) -> list[str]:
    if not isinstance(value, list):
        return [f"{where}: must be a list of operations"]
    if not value and not allow_empty:
        return [
            f"{where}: must not be empty; omit the key to allow every operation "
            "within allowed_methods"
        ]
    errors: list[str] = []
    for index, entry in enumerate(value):
        at = f"{where}[{index}]"
        if not isinstance(entry, Mapping):
            errors.append(f"{at}: must be a mapping with operationId and/or path")
            continue
        for key in sorted(set(entry) - set(_OPERATION_KEYS), key=str):
            errors.append(f"{at}: unknown key {key!r}")
        if "operationId" not in entry and "path" not in entry:
            errors.append(f"{at}: needs operationId and/or path")
        if "operationId" in entry:
            op_id = entry["operationId"]
            if not isinstance(op_id, str) or not op_id or any(c.isspace() for c in op_id):
                errors.append(f"{at}.operationId: must be a non-empty string without spaces")
        if "path" in entry:
            problem = path_template_problem(entry["path"])
            if problem:
                errors.append(f"{at}.path: {problem}")
        if "methods" in entry:
            errors.extend(_methods_errors(f"{at}.methods", entry["methods"], False))
    return errors


def _is_positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _timeouts_errors(where: str, value: Any) -> list[str]:
    if not isinstance(value, Mapping):
        return [f"{where}: must be a mapping with connect and/or read (milliseconds)"]
    errors = [
        f"{where}: unknown key {key!r}" for key in sorted(set(value) - set(_TIMEOUT_KEYS), key=str)
    ]
    for key in _TIMEOUT_KEYS:
        if key in value and not _is_positive_int(value[key]):
            errors.append(f"{where}.{key}: must be a positive integer (milliseconds)")
    return errors


def _pagination_errors(where: str, value: Any) -> list[str]:
    if not isinstance(value, Mapping):
        return [f"{where}: must be a mapping with page_size_param and max_page_size"]
    errors = [
        f"{where}: unknown key {key!r}"
        for key in sorted(set(value) - set(_PAGINATION_KEYS), key=str)
    ]
    param = value.get("page_size_param")
    if "page_size_param" not in value:
        errors.append(f"{where}.page_size_param: required")
    elif not (isinstance(param, str) and param.strip()):
        errors.append(f"{where}.page_size_param: must be a non-empty string")
    if "max_page_size" not in value:
        errors.append(f"{where}.max_page_size: required")
    elif not _is_positive_int(value["max_page_size"]):
        errors.append(f"{where}.max_page_size: must be a positive integer")
    return errors


def path_template_problem(path: Any) -> str | None:
    """Why ``path`` is not a valid path template, or None.

    A template starts with ``/``; each segment holds literal characters and
    ``{name}`` placeholders only (no query, fragment, spaces, empty, ``.`` or
    ``..`` segments). One trailing slash is allowed.
    """
    if not isinstance(path, str) or not path.startswith("/"):
        return "must be a string starting with /"
    body = path[1:]
    if body.endswith("/"):
        body = body[:-1]
    if not body:
        return None
    for segment in body.split("/"):
        if segment in ("", ".", ".."):
            return "must not contain empty, '.' or '..' segments"
        if not _PATH_SEGMENT_RE.match(segment):
            return (
                "segments may hold literal characters and {name} placeholders only "
                "(no query, fragment or whitespace)"
            )
    return None


def normalize_path(path: str) -> str:
    """``path`` in the form policy paths are compared in.

    Percent-encoded unreserved characters are decoded (``/%61dmin`` is
    ``/admin``), other escapes are upper-cased (``%2f`` is ``%2F``), and one
    trailing slash is dropped (``/items/1/`` is ``/items/1``), so equivalent
    spellings of a path match the same entries.
    """

    def _escape(match: re.Match[str]) -> str:
        char = chr(int(match.group(0)[1:], 16))
        return char if char in _UNRESERVED else match.group(0).upper()

    path = _ESCAPE_RE.sub(_escape, path)
    return path[:-1] if len(path) > 1 and path.endswith("/") else path


def path_matches(template: str, path: str, *, ignore_case: bool = False) -> bool:
    """Whether ``path`` is covered by ``template``.

    A ``{name}`` placeholder matches exactly one non-empty segment, so
    ``/items/{item_id}`` covers ``/items/42``, ``/items/{id}`` and itself.
    Both sides are compared normalised (``normalize_path``). Letter case
    counts unless ``ignore_case``: denials ignore it, so ``/ADMIN/1`` cannot
    slip past a denial of ``/admin/{x}`` on a case-insensitive server.
    """
    template, path = normalize_path(template), normalize_path(path)
    if template == path or (ignore_case and template.casefold() == path.casefold()):
        return True
    pattern = "".join(
        "[^/]+" if part.startswith("{") and part.endswith("}") else re.escape(part)
        for part in _PLACEHOLDER_SPLIT_RE.split(template)
    )
    return re.fullmatch(pattern, path, re.IGNORECASE if ignore_case else 0) is not None


def _methods_match(entry: Mapping[str, Any], method: str) -> bool:
    methods = entry.get("methods")
    return not methods or method.upper() in {str(m).upper() for m in methods}


def operation_matches(
    entry: Mapping[str, Any], method: str, operation_id: str | None, path: str | None
) -> bool:
    """Whether an ``allowed_operations`` entry covers the call.

    AND semantics: every field the entry pins (``operationId``, ``path``,
    ``methods``) must match. A call that does not name a pinned field (no
    operation id, or no path) does not match: an allow must be shown.
    """
    if not _methods_match(entry, method):
        return False
    pinned_id = entry.get("operationId")
    if pinned_id is not None and operation_id != pinned_id:
        return False
    pinned_path = entry.get("path")
    if pinned_path is not None and (path is None or not path_matches(pinned_path, path)):
        return False
    return pinned_id is not None or pinned_path is not None


def denial_matches(
    entry: Mapping[str, Any], method: str, operation_id: str | None, path: str | None
) -> bool:
    """Whether a ``denied_operations`` entry may cover the call (fail closed).

    Every field the entry pins must match, as for an allow, but a pinned field
    the call does not name counts as matching: a call without an operation id
    cannot be told apart from a denied ``operationId``, so it is refused.
    Operation ids and paths are compared ignoring letter case.
    """
    if not _methods_match(entry, method):
        return False
    pinned_id = entry.get("operationId")
    if (
        pinned_id is not None
        and operation_id is not None
        and str(operation_id).casefold() != str(pinned_id).casefold()
    ):
        return False
    pinned_path = entry.get("path")
    if (
        pinned_path is not None
        and path is not None
        and not path_matches(pinned_path, path, ignore_case=True)
    ):
        return False
    return pinned_id is not None or pinned_path is not None


def describe_operation(entry: Mapping[str, Any]) -> str:
    """``operationId=getItem path=/items/{item_id} methods=[GET]`` for messages."""
    parts = []
    if entry.get("operationId") is not None:
        parts.append(f"operationId={entry['operationId']}")
    if entry.get("path") is not None:
        parts.append(f"path={entry['path']}")
    if entry.get("methods"):
        parts.append(f"methods={sorted(str(m).upper() for m in entry['methods'])}")
    return " ".join(parts)


def refusal_reason(
    api: Mapping[str, Any],
    method: str,
    operation_id: str | None = None,
    path: str | None = None,
) -> str | None:
    """Why the API's policy refuses the call, or None when it is allowed.

    Every rule must pass: the method is in ``allowed_methods`` (``["*"]``
    allows every method); no ``denied_operations`` entry may cover the call
    (denials win, and a call that does not name a field a denial pins is
    refused by it: ``denial_matches``); and, when ``allowed_operations`` is
    present, one of its entries matches (``operation_matches``).
    """
    method = method.upper()
    operation_id = operation_id or None
    path = path or None
    allowed = [str(m).upper() for m in api.get("allowed_methods") or []]
    if ANY_METHOD not in allowed and method not in allowed:
        return f"method {method} is not in allowed_methods {allowed}"
    for entry in api.get("denied_operations") or []:
        if denial_matches(entry, method, operation_id, path):
            reason = f"denied by denied_operations ({describe_operation(entry)})"
            unnamed = [
                name
                for name, pinned, given in (
                    ("operation_id", "operationId", operation_id),
                    ("path", "path", path),
                )
                if entry.get(pinned) is not None and given is None
            ]
            if unnamed:
                reason += (
                    f": the call names no {' and no '.join(unnamed)}, so it cannot be ruled "
                    "out; name it on the call and in API_CALLS"
                )
            return reason
    allowed_operations = api.get("allowed_operations")
    if allowed_operations is not None and not any(
        operation_matches(entry, method, operation_id, path) for entry in allowed_operations
    ):
        return "not in allowed_operations"
    return None


# --- END SHARED API POLICY RULES ---

# ---------------------------------------------------------------------------
# CLI-only helpers
# ---------------------------------------------------------------------------

MANIFEST_KEY = "api_policy"
LEGACY_POLICY_FILENAME = "product-policy.yaml"
LEGACY_MANIFEST_KEY = "product_api"
LEGACY_CALLS_NAME = "PRODUCT_CALLS"
CALLS_NAME = "API_CALLS"


class ApiPolicyFileError(click.ClickException):
    """An ``api-policy.yaml`` that cannot be read or breaks the schema (exit 3)."""

    exit_code = 3

    def __init__(self, path: str | Path, errors: list[str]) -> None:
        self.path = Path(path)
        self.errors = list(errors)
        lines = "\n".join(f"  - {e}" for e in self.errors)
        super().__init__(f"Invalid API policy {self.path}:\n{lines}")


class LegacyApiPolicyError(click.ClickException):
    """The project still uses the retired product API policy (exit 3)."""

    exit_code = 3


class ApiPolicyConfigError(click.ClickException):
    """The manifest names a policy file the agent would not load (exit 3)."""

    exit_code = 3


def load_policy_document(path: str | Path) -> dict[str, Any]:
    """Read and validate an api-policy file; raise ``ApiPolicyFileError`` listing every problem."""
    policy_path = Path(path)
    try:
        text = policy_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ApiPolicyFileError(policy_path, [f"cannot read the file: {exc}"]) from exc
    data, errors = parse_policy_yaml(text)
    errors = errors or policy_errors(data)
    if errors:
        raise ApiPolicyFileError(policy_path, errors)
    return dict(data)


def manifest_policy_file_problem(value: Any, manifest_name: str) -> str | None:
    """Why the manifest's ``api_policy.policy_file`` cannot be used, or None.

    The agent loads ``api-policy.yaml`` from the project root (or
    ``API_POLICY_PATH``) and the Dockerfiles copy only that file, so a manifest
    naming another file would make ``lint`` check a file the agent never
    enforces.
    """
    if not value or Path(str(value)) == Path(POLICY_FILENAME):
        return None
    return (
        f"api_policy.policy_file in {manifest_name} is {str(value)!r}, but the agent loads "
        f"{POLICY_FILENAME} from the project root (the Dockerfiles copy only that file), so "
        f"lint would check a file the agent never enforces. Rename the file to "
        f"{POLICY_FILENAME} and set api_policy: {{policy_file: {POLICY_FILENAME}}}."
    )


@dataclass(frozen=True)
class ApiSummary:
    """What the templates need to know about one declared API."""

    name: str
    base_url_env: str
    auth: str
    token_env: str = ""

    def as_context(self) -> dict[str, str]:
        return {
            "name": self.name,
            "base_url_env": self.base_url_env,
            "auth": self.auth,
            "token_env": self.token_env,
        }


def summarize(document: Mapping[str, Any]) -> tuple[ApiSummary, ...]:
    """One summary per declared API, in file order (the document must be valid)."""
    return tuple(
        ApiSummary(
            name=str(name),
            base_url_env=str(api["base_url_env"]),
            auth=str(api["auth"]),
            token_env=str(api.get("token_env") or ""),
        )
        for name, api in document["apis"].items()
    )


@dataclass(frozen=True)
class ExampleCall:
    """The GET that the rendered ``tools/example_api.py`` makes.

    Chosen when the project is rendered (``dev.policy_check.example_call``) so
    the example passes ``lint`` and the project's policy test from the first
    commit, whatever the seed policy allows.
    """

    api: str
    path: str
    operation_id: str | None = None

    method: ClassVar[str] = "GET"

    @property
    def params(self) -> tuple[str, ...]:
        """The path's ``{name}`` placeholders in order, each once: the tool's parameters."""
        return tuple(dict.fromkeys(_PLACEHOLDER_NAME_RE.findall(self.path)))

    def as_context(self) -> dict[str, Any]:
        return {
            "api": self.api,
            "method": self.method,
            "operation_id": self.operation_id or "",
            "path": self.path,
            "params": list(self.params),
        }


_PLACEHOLDER_NAME_RE = re.compile(r"\{([^/{}]+)\}")


def read_policy_document(path: str | Path | None) -> dict[str, Any] | None:
    """An existing project policy, validated; None (with a warning) when absent or invalid.

    Used when a project is re-rendered: the policy belongs to the project and
    ``lint`` reports its problems, so a broken file must not stop the re-render.
    """
    if not path or not Path(path).is_file():
        return None
    try:
        return load_policy_document(path)
    except ApiPolicyFileError as exc:
        logging.warning("%s", exc.format_message())
        return None


def read_summaries(path: str | Path | None) -> tuple[ApiSummary, ...]:
    """Summaries of an existing project policy; empty (with a warning) when unreadable."""
    document = read_policy_document(path)
    return summarize(document) if document is not None else ()


def bearer_token_envs(summaries: tuple[ApiSummary, ...] | list[ApiSummary]) -> list[str]:
    """The ``token_env`` of every ``auth: bearer`` API, first occurrence first."""
    envs: list[str] = []
    for summary in summaries:
        if summary.auth == "bearer" and summary.token_env and summary.token_env not in envs:
            envs.append(summary.token_env)
    return envs


def forward_runtime_problem(
    summaries: tuple[ApiSummary, ...] | list[ApiSummary], runtime: str
) -> str | None:
    """Why ``auth: forward`` cannot be used with ``runtime``, or None."""
    forward = [s.name for s in summaries if s.auth == "forward"]
    if runtime != "langgraph-server" or not forward:
        return None
    return (
        f"auth: forward (apis: {', '.join(forward)}) is not supported with runtime "
        "langgraph-server: LangGraph Server persists the run context, so forwarded "
        "credentials would be stored. Use auth: bearer or none, or the fastapi runtime."
    )


def legacy_findings(project_dir: str | Path) -> list[str]:
    """What still uses the retired product API policy in ``project_dir``."""
    root = Path(project_dir)
    findings: list[str] = []
    if (root / LEGACY_POLICY_FILENAME).is_file():
        findings.append(f"{LEGACY_POLICY_FILENAME} exists")
    manifest = root / "graph-agents-cli-manifest.yaml"
    if manifest.is_file():
        try:
            data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            data = None
        if isinstance(data, Mapping) and LEGACY_MANIFEST_KEY in data:
            findings.append(f"the manifest has a {LEGACY_MANIFEST_KEY}: block")
    return findings


def legacy_migration_message(findings: list[str]) -> str:
    """How to move a project from the retired product API policy to api-policy.yaml."""
    return (
        "This project uses the retired product API policy ("
        + "; ".join(findings)
        + "). Migrate it to api-policy.yaml, then re-run the command:\n"
        f"  1. Rename {LEGACY_POLICY_FILENAME} to {POLICY_FILENAME}.\n"
        f"  2. Replace its top-level `{LEGACY_MANIFEST_KEY}:` with `apis:` and move the fields\n"
        "     under an API name, adding the now required allowed_methods:\n"
        "       apis:\n"
        "         example:\n"
        "           base_url_env: EXAMPLE_API_BASE_URL\n"
        "           auth: bearer              # none | bearer | forward (was forwarded-session)\n"
        "           token_env: EXAMPLE_API_TOKEN\n"
        "           allowed_methods: [GET]\n"
        f"  3. In graph-agents-cli-manifest.yaml replace `{LEGACY_MANIFEST_KEY}:` with\n"
        f"     `{MANIFEST_KEY}: {{policy_file: {POLICY_FILENAME}}}`.\n"
        f"  4. In every tool module rename {LEGACY_CALLS_NAME} to {CALLS_NAME}, add\n"
        '     "api": "<name>" to each entry, and call the API through\n'
        '     app_utils.api_client.get_client("<name>").'
    )


def ensure_no_legacy_api_policy(project_dir: str | Path) -> None:
    """Raise ``LegacyApiPolicyError`` (exit 3) when the project uses the retired format."""
    findings = legacy_findings(project_dir)
    if findings:
        raise LegacyApiPolicyError(legacy_migration_message(findings))
