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
"""Shared Secret provisioning used by ``secrets apply`` and direct-mode ``deploy``.

Which env file: ``--env-file`` when given, else ``.env.<env>``. Only ``dev``
falls back to ``.env``: that file holds a developer's local keys (``login
--write-env`` writes a local ``API_KEY`` into it), so reading it for staging or
prod would push local values over the environment's Secret.

What goes in: only the manifest's allow-listed keys, merged over the live
Secret. A key the env file sets replaces the live value, except ``API_KEY``:
the live key always wins unless the env file sets a different one *and*
``--rotate-api-key`` is passed, because replacing it logs out every client of
the environment. An allow-listed key the env file does not set is kept from the
live Secret, so a partial env file never deletes the rest. ``API_KEY`` is
generated (32 random bytes, hex) only for the ``shared-bearer`` policy (the
only one that reads it) and only when it is in neither, and is then written
into the env file (mode 0600) after the apply succeeds, never printed.

``METRICS_TOKEN``, when the Secret holds it, is also written alone into a
second Secret ``<name>-metrics``: the Prometheus ServiceMonitor reads its
bearer token from there, so the scraper never needs access to the app Secret
and every other credential in it.

How: the manifest is built with ``kubectl create secret generic
--from-env-file=<0600 temp file> --dry-run=client -o yaml`` and piped into
``kubectl apply --server-side`` under the ``graph-agents-cli`` field manager,
so no value appears on a command line and none is copied into the
``last-applied-configuration`` annotation that a client-side apply writes (an
annotation left by an older client-side apply is removed). The namespace is
created first when it does not exist. Values must be single-line:
``--from-env-file`` splits on newlines.
"""

from __future__ import annotations

import base64
import binascii
import io
import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from graph_agents_cli._output import Console
from graph_agents_cli.deploy import _kube, _modes
from graph_agents_cli.deploy._kube import ConfigError, Target, echo_cmd

GENERATED_KEY = "API_KEY"
METRICS_TOKEN_KEY = "METRICS_TOKEN"
# Placeholder shown in a --dry-run manifest for a key that the real run keeps
# from the live Secret or generates (the cluster is not read under --dry-run).
PENDING_PLACEHOLDER = "<kept-or-generated>"
FIELD_MANAGER = "graph-agents-cli"
LAST_APPLIED_ANNOTATION = "kubectl.kubernetes.io/last-applied-configuration"


def resolve_env_file(env: str, explicit: str | None) -> Path | None:
    """``--env-file`` when given (must exist), else ``.env.<env>``; ``dev`` also tries ``.env``."""
    if explicit:
        path = Path(explicit)
        if not path.is_file():
            raise ConfigError(f"Env file not found: {path}")
        return path
    candidates = [Path(f".env.{env}")]
    if _modes.is_dev_env(env):
        candidates.append(Path(".env"))
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def provision_hint(env: str) -> str:
    """The command that provisions the Secret, as it can be run from the project root.

    ``secrets apply`` finds the environment's env file itself (``.env.<env>``;
    dev also falls back to ``.env``); naming a file that does not exist would
    make the printed command fail.
    """
    command = f"graph-agents-cli secrets apply --env {env}"
    if resolve_env_file(env, None) is not None:
        return command
    where = f".env.{env} (or .env)" if _modes.is_dev_env(env) else f".env.{env}"
    return f"create {where} with the allow-listed keys, then run {command}"


def missing_env_file_error(env: str, allowed: list[str]) -> ConfigError:
    """The exit-3 error for an environment with no env file."""
    if _modes.is_dev_env(env):
        where = "create .env.dev (or .env)"
    else:
        where = (
            f"create .env.{env} (the local .env is never used for {env}: it holds your "
            "development keys)"
        )
    return ConfigError(
        f"No env file for {env}: pass --env-file or {where} with the allow-listed keys: "
        + ", ".join(allowed)
    )


def read_env_file(path: Path) -> dict[str, str]:
    from dotenv import dotenv_values

    content = path.read_text(encoding="utf-8")
    return {k: v for k, v in dotenv_values(stream=io.StringIO(content)).items() if v is not None}


def select_allowed(values: dict[str, str], allowed: list[str]) -> dict[str, str]:
    """Keep only allow-listed, non-empty values (never the whole file)."""
    return {k: values[k] for k in allowed if k in values and values[k] != ""}


def generate_api_key() -> str:
    return os.urandom(32).hex()


def _reject_multiline(data: dict[str, str], *, where: str = "") -> None:
    """Refuse values ``--from-env-file`` cannot carry (one single-line value per key).

    kubectl splits the env file on newlines: the value would be truncated and
    the remaining lines read as extra keys (a bare ``NAME`` line is even looked
    up in the local environment). Only key names are reported, never values.
    """
    bad = [k for k, v in data.items() if "\n" in v or "\r" in v]
    if bad:
        raise ConfigError(
            f"Secret values must be single-line{where}: kubectl --from-env-file splits on "
            "newlines, truncating the value and reading the remaining lines as extra keys. "
            f"Offending key(s): {', '.join(bad)}. Put the value on one line (for example "
            "base64-encode it) or create the Secret with kubectl directly."
        )


def check_file_values(
    values: dict[str, str], allowed: list[str], *, rotate_api_key: bool, source: Path | None
) -> None:
    """Checks on the env file alone, run before the cluster is touched (exit 3)."""
    _reject_multiline(select_allowed(values, allowed))
    if not rotate_api_key:
        return
    if GENERATED_KEY not in allowed:
        raise ConfigError("--rotate-api-key: API_KEY is not in the manifest's secrets.keys.")
    if not select_allowed(values, allowed).get(GENERATED_KEY):
        raise ConfigError(
            f"--rotate-api-key replaces the live API_KEY with the one in the env file, but "
            f"{source or 'the env file'} sets no API_KEY."
        )


@dataclass
class LiveSecret:
    """What the cluster holds for the app Secret (values are never printed)."""

    exists: bool = False
    values: dict[str, str] = field(default_factory=dict)  # UTF-8 text values
    undecodable: set[str] = field(default_factory=set)  # binary values (cannot be carried)
    has_last_applied: bool = False
    # The base64 ``data`` exactly as the cluster holds it, to restore it byte for byte.
    raw: dict[str, str] = field(default_factory=dict)
    resource_version: str = ""
    # ``data`` keys the graph-agents-cli field manager applied: a server-side apply
    # that leaves one out removes it.
    owned: set[str] = field(default_factory=set)

    @property
    def keys(self) -> set[str]:
        return set(self.values) | self.undecodable


def read_live_secret(name: str, target: Target) -> LiveSecret:
    """The live Secret, ``exists=False`` when it (or its namespace) is absent.

    Only kubectl's ``(NotFound)`` means absent; any other failure (connection,
    credentials, RBAC) is a tool failure (exit 2), never a reason to generate or
    overwrite keys.
    """
    result = _kube.kubectl(["get", "secret", name, "-o", "json"], target, check=False, quiet=True)
    if result.returncode != 0:
        if "(NotFound)" in (result.stderr or ""):
            return LiveSecret()
        raise _kube.ToolFailed(
            f"kubectl get secret {name} failed (exit code {result.returncode}) in namespace "
            f"{target.namespace}:\n{(result.stderr or result.stdout or '').strip()}"
        )
    if not (result.stdout or "").strip():
        return LiveSecret()
    try:
        body = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise _kube.ToolFailed(f"kubectl returned invalid JSON: {e}") from e
    live = LiveSecret(exists=True)
    live.resource_version = str((body.get("metadata") or {}).get("resourceVersion") or "")
    annotations = (body.get("metadata") or {}).get("annotations") or {}
    live.has_last_applied = LAST_APPLIED_ANNOTATION in annotations
    for key, raw in (body.get("data") or {}).items():
        live.raw[key] = str(raw)
        try:
            live.values[key] = base64.b64decode(str(raw), validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            live.undecodable.add(key)
    for key, raw in (body.get("stringData") or {}).items():
        live.values[key] = str(raw)
        live.raw[key] = base64.b64encode(str(raw).encode("utf-8")).decode("ascii")
    live.owned = _owned_data_keys(body)
    return live


def _owned_data_keys(body: dict[str, Any]) -> set[str]:
    """The ``data`` keys the graph-agents-cli field manager owns (from ``managedFields``)."""
    owned: set[str] = set()
    for entry in (body.get("metadata") or {}).get("managedFields") or []:
        if not isinstance(entry, dict) or entry.get("manager") != FIELD_MANAGER:
            continue
        data = (entry.get("fieldsV1") or {}).get("f:data")
        if isinstance(data, dict):
            owned |= {k[2:] for k in data if isinstance(k, str) and k.startswith("f:")}
    return owned


@dataclass
class SecretPlan:
    name: str
    target: Target
    data: dict[str, str]
    generated: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # allow-listed keys in neither file nor Secret
    reused: list[str] = field(default_factory=list)  # kept from the live Secret
    pending: list[str] = field(default_factory=list)  # --dry-run: kept or generated by the real run
    # --dry-run: allow-listed keys the file does not set, kept when the live Secret has them
    kept_if_live: list[str] = field(default_factory=list)
    api_key_conflict: bool = False  # the file's API_KEY differs; the live one was kept
    api_key_rotated: bool = False  # the file's API_KEY replaced the live one (--rotate-api-key)
    rotate_requested: bool = False
    source: Path | None = None  # the env file (a generated API_KEY is saved there)
    env: str = ""  # the environment, for the hints printed after the apply
    # --dry-run with the live Secret read: API_KEY is in neither and would be generated.
    would_generate: list[str] = field(default_factory=list)
    # --dry-run: why the live Secret could not be read (its keys are then unknown).
    live_unread: str = ""
    # The Secret that receives METRICS_TOKEN alone (for the ServiceMonitor); "" for none.
    metrics_name: str = ""

    @property
    def metrics_token(self) -> str | None:
        """The METRICS_TOKEN this apply writes (``None`` when absent or unknown)."""
        token = self.data.get(METRICS_TOKEN_KEY)
        return token if token and token != PENDING_PLACEHOLDER else None

    def manifest(self, *, redact: bool = True) -> str:
        body = {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": self.name, "namespace": self.target.namespace},
            "type": "Opaque",
            "stringData": {k: ("<redacted>" if redact else v) for k, v in self.data.items()},
        }
        return yaml.safe_dump(body, sort_keys=False)


def build_plan(
    *,
    name: str,
    target: Target,
    allowed: list[str],
    values: dict[str, str],
    existing: dict[str, str] | None = None,
    dry_run: bool = False,
    rotate_api_key: bool = False,
    source: Path | None = None,
    mint_api_key: bool = True,
) -> SecretPlan:
    """Decide the Secret's content from the env file ``values`` and the live ``existing`` values.

    Pure: the cluster is read by the caller (``read_live_secret``); ``existing``
    is ``None`` when it was not read (``--dry-run``). File values win over live
    ones except ``API_KEY`` (the live key wins unless ``rotate_api_key``);
    allow-listed live keys the file does not set are kept; a missing
    ``API_KEY`` is generated when ``mint_api_key`` (the ``shared-bearer``
    policy; recorded as pending or would-generate under ``dry_run``).
    """
    file_values = select_allowed(values, allowed)
    _reject_multiline(file_values)
    live = {k: v for k, v in (existing or {}).items() if k in allowed and v != ""}
    plan = SecretPlan(
        name=name, target=target, data={}, rotate_requested=rotate_api_key, source=source
    )
    for key in allowed:
        if key in file_values:
            live_value = live.get(key)
            if key == GENERATED_KEY and live_value and live_value != file_values[key]:
                if rotate_api_key:
                    plan.data[key] = file_values[key]
                    plan.api_key_rotated = True
                else:
                    plan.data[key] = live_value
                    plan.api_key_conflict = True
            else:
                plan.data[key] = file_values[key]
        elif key in live:
            plan.data[key] = live[key]
            plan.reused.append(key)
        elif existing is None and dry_run and key != GENERATED_KEY:
            plan.kept_if_live.append(key)
    _reject_multiline({k: plan.data[k] for k in plan.reused}, where=" (kept from the live Secret)")
    if mint_api_key and GENERATED_KEY in allowed and GENERATED_KEY not in plan.data:
        if dry_run:
            plan.data[GENERATED_KEY] = PENDING_PLACEHOLDER
            (plan.pending if existing is None else plan.would_generate).append(GENERATED_KEY)
        else:
            plan.data[GENERATED_KEY] = generate_api_key()
            plan.generated.append(GENERATED_KEY)
    plan.skipped = [k for k in allowed if k not in plan.data and k not in plan.kept_if_live]
    return plan


def _create_cmd(plan: SecretPlan, env_file: str) -> list[str]:
    return _kube.kubectl_args(
        [
            "create",
            "secret",
            "generic",
            plan.name,
            f"--from-env-file={env_file}",
            "--dry-run=client",
            "-o",
            "yaml",
        ],
        plan.target,
    )


def _apply_cmd(plan: SecretPlan) -> list[str]:
    return _kube.kubectl_args(
        [
            "apply",
            "--server-side",
            f"--field-manager={FIELD_MANAGER}",
            "--force-conflicts",
            "-f",
            "-",
        ],
        plan.target,
    )


def _remove_last_applied_cmd(plan: SecretPlan) -> list[str]:
    return _kube.kubectl_args(
        ["annotate", "secret", plan.name, f"{LAST_APPLIED_ANNOTATION}-"], plan.target
    )


_KEY_LINE = re.compile(r"^(?P<prefix>\s*(?:export\s+)?)(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*=")


def save_key_to_env_file(path: Path, key: str, value: str) -> None:
    """Set ``key=value`` in the env file (replacing the line dotenv reads, else appending), mode 0600.

    Written to a 0600 temp file in the same directory and renamed over the
    original, so the file is never readable by others, even briefly.
    """
    path = path.resolve()  # a symlinked env file keeps its link; its target is rewritten
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    lines = text.splitlines(keepends=True)
    index = None
    for i, line in enumerate(lines):
        match = _KEY_LINE.match(line)
        if match and match.group("key") == key:
            index = i  # the last assignment is the one dotenv honours
    if index is not None:
        prefix = _KEY_LINE.match(lines[index]).group("prefix")  # type: ignore[union-attr]
        lines[index] = f"{prefix}{key}={value}\n"
    else:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.append(f"{key}={value}\n")
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.chmod(tmp, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("".join(lines))
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.chmod(path, 0o600)


def _print_plan_notes(plan: SecretPlan, *, dry_run: bool, console: Console) -> None:
    source = plan.source or "the env file"
    if plan.reused:
        console.print(
            f"  Kept from the live Secret (not in {source}): {', '.join(plan.reused)}.",
            style="dim",
            markup=False,
        )
    if plan.api_key_conflict:
        console.print(
            f"  API_KEY in {source} differs from the live Secret; the live key is kept. Pass "
            "--rotate-api-key to replace it (clients using the old key then get 401).",
            style="yellow",
            markup=False,
        )
    if plan.api_key_rotated:
        console.print(f"  API_KEY rotated to the value in {source}.", style="yellow", markup=False)
    if not dry_run:
        return
    if plan.live_unread:
        console.print(
            f"  [dry-run] could not read the live Secret ({plan.live_unread}); the keys it "
            "holds are unknown.",
            style="yellow",
            markup=False,
        )
    for key in plan.would_generate:
        console.print(
            f"  {key} is in neither the env file nor the live Secret: the real run generates "
            "one and saves it to the env file (nothing is written under --dry-run).",
            style="yellow",
            markup=False,
        )
    for key in plan.pending:
        console.print(
            f"  {key} is not in the env file: it would be kept from the live Secret if "
            "present, otherwise generated and saved to the env file (nothing is written "
            "under --dry-run).",
            style="yellow",
        )
    if plan.kept_if_live:
        console.print(
            "  Allow-listed keys absent from the env file are kept from the live Secret if "
            f"present: {', '.join(plan.kept_if_live)}.",
            style="yellow",
            markup=False,
        )
    if (
        GENERATED_KEY in plan.data
        and GENERATED_KEY not in plan.pending + plan.would_generate
        and GENERATED_KEY not in plan.reused
    ):
        verb = "replaces" if plan.rotate_requested else "does not replace"
        console.print(
            f"  API_KEY from the env file {verb} a different live API_KEY"
            + ("" if plan.rotate_requested else " (pass --rotate-api-key to replace it)")
            + ".",
            style="dim",
            markup=False,
        )


def _server_side_apply(plan: SecretPlan, *, console: Console) -> None:
    """``kubectl create secret generic --from-env-file | kubectl apply --server-side``.

    The values go through a 0600 temp file and a pipe, never a command line.
    """
    fd, tmp_path = tempfile.mkstemp(prefix="graph-agents-cli-secret-", suffix=".env")
    try:
        os.chmod(tmp_path, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            for key, value in plan.data.items():
                handle.write(f"{key}={value}\n")
        create = _create_cmd(plan, tmp_path)
        apply = _apply_cmd(plan)
        echo_cmd(create, console=console)
        rendered = _kube.run_cmd(create, quiet=True)
        echo_cmd(apply, console=console)
        _kube.run_cmd(apply, input_text=rendered.stdout, quiet=True)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def metrics_plan(plan: SecretPlan) -> SecretPlan | None:
    """The ``<name>-metrics`` Secret this apply also writes (METRICS_TOKEN alone), if any."""
    token = plan.metrics_token
    if not plan.metrics_name or token is None:
        return None
    return SecretPlan(
        name=plan.metrics_name,
        target=plan.target,
        data={METRICS_TOKEN_KEY: token},
        env=plan.env,
        source=plan.source,
    )


def apply_plan(
    plan: SecretPlan,
    *,
    dry_run: bool = False,
    console: Console | None = None,
    live: LiveSecret | None = None,
) -> None:
    """Create the namespace if needed, then create or update the Secret (server-side apply).

    With ``dry_run`` print the pipeline and a redacted manifest. A generated
    ``API_KEY`` is saved to ``plan.source`` only after the apply succeeded.
    When the Secret holds ``METRICS_TOKEN`` and ``plan.metrics_name`` is set,
    the token alone is applied to that Secret too (the ServiceMonitor's).
    """
    console = console or Console()
    if not plan.data:
        raise ConfigError(
            "No allow-listed secret values to apply. The env file has none of: "
            + ", ".join(plan.skipped)
        )
    _print_plan_notes(plan, dry_run=dry_run, console=console)
    metrics = metrics_plan(plan)
    if dry_run:
        _kube.ensure_namespace(plan.target, dry_run=True, console=console)
        for each in (plan, metrics):
            if each is None:
                continue
            create = _create_cmd(each, "TMP_ENV_FILE_WITH_ALLOW_LISTED_KEYS")
            console.print(
                f"  [dry-run] {_kube.pipe_description(create, _apply_cmd(each))}",
                style="cyan",
                highlight=False,
                markup=False,
            )
            console.print(each.manifest(redact=True), highlight=False, markup=False)
        return
    _kube.ensure_namespace(plan.target, console=console)
    _server_side_apply(plan, console=console)
    if live is not None and live.has_last_applied:
        # Written by an earlier client-side `kubectl apply`: it holds a copy of the values.
        _kube.run_cmd(_remove_last_applied_cmd(plan), console=console)
        console.print(
            f"  Removed the {LAST_APPLIED_ANNOTATION} annotation (it held a copy of the values).",
            style="dim",
            markup=False,
        )
    console.print(
        f"  Secret {plan.name} applied in namespace {plan.target.namespace} "
        f"({len(plan.data)} key(s): {', '.join(plan.data)})."
    )
    if metrics is not None:
        _server_side_apply(metrics, console=console)
        console.print(
            f"  Secret {metrics.name} applied ({METRICS_TOKEN_KEY} only: the Prometheus "
            "ServiceMonitor reads its bearer token there, never the app Secret)."
        )
    for key in plan.generated:
        _save_generated(plan, key, console=console)
    if plan.skipped:
        console.print(
            f"  Allow-listed keys in neither the env file nor the live Secret: "
            f"{', '.join(plan.skipped)}",
            style="yellow",
        )
    if plan.api_key_rotated:
        console.print(
            "  Running pods read the Secret at start: restart them with "
            f"`graph-agents-cli deploy --env {plan.env or '<env>'} --restart` to use the new "
            "API_KEY.",
            style="yellow",
            markup=False,
        )


def _save_generated(plan: SecretPlan, key: str, *, console: Console) -> None:
    read_back = (
        f"kubectl get secret {plan.name} -n {plan.target.namespace}"
        + (f" --context {plan.target.context}" if plan.target.context else "")
        + f" -o jsonpath='{{.data.{key}}}' | base64 -d"
    )
    if plan.source is None:
        console.print(
            f"  Generated {key} (it was in neither the env file nor the live Secret); read it "
            f"with: {read_back}",
            style="yellow",
            markup=False,
        )
        return
    try:
        save_key_to_env_file(plan.source, key, plan.data[key])
    except OSError as e:
        console.print(
            f"  Generated {key} but could not save it to {plan.source} ({e.strerror or e}); "
            f"read it with: {read_back}",
            style="yellow",
            markup=False,
        )
        return
    console.print(
        f"  Generated {key} (it was in neither the env file nor the live Secret) and saved it "
        f"to {plan.source} (mode 0600).",
        style="yellow",
        markup=False,
    )


def prepare(
    *,
    name: str,
    env: str,
    target: Target,
    allowed: list[str],
    path: Path,
    values: dict[str, str],
    rotate_api_key: bool,
    dry_run: bool,
    mint_api_key: bool = True,
    metrics_name: str = "",
    read_live_on_dry_run: bool = False,
) -> tuple[SecretPlan, LiveSecret | None]:
    """Read the live Secret and plan the apply; changes nothing.

    Under ``dry_run`` the live Secret is read only with ``read_live_on_dry_run``
    (``deploy --dry-run``, whose required-key check needs it), and a failed read
    is then reported instead of raised: the keys it holds are unknown.
    """
    live: LiveSecret | None = None
    unread = ""
    if not dry_run:
        live = read_live_secret(name, target)
    elif read_live_on_dry_run:
        try:
            live = read_live_secret(name, target)
        except _kube.ToolFailed as e:
            lines = [line.strip() for line in str(e).splitlines() if line.strip()]
            unread = lines[-1] if lines else "kubectl failed"
    if live is not None:
        from_file = select_allowed(values, allowed)
        carried = [k for k in allowed if k in live.undecodable and k not in from_file]
        if carried:
            raise ConfigError(
                f"Secret {name} holds a value for {', '.join(carried)} that is not UTF-8 text; "
                "it cannot be carried through --from-env-file. Set it in the env file or manage "
                "the Secret with kubectl."
            )
        if GENERATED_KEY in live.undecodable and GENERATED_KEY in from_file and not rotate_api_key:
            # It cannot be compared with the file's key, so it is never replaced silently.
            raise ConfigError(
                f"Secret {name} holds an API_KEY that is not UTF-8 text and the env file sets "
                "another; pass --rotate-api-key to replace it, or drop API_KEY from the env file."
            )
    plan = build_plan(
        name=name,
        target=target,
        allowed=allowed,
        values=values,
        existing=None if live is None else live.values,
        dry_run=dry_run,
        rotate_api_key=rotate_api_key,
        source=path,
        mint_api_key=mint_api_key,
    )
    plan.env = env
    plan.live_unread = unread
    plan.metrics_name = metrics_name
    return plan, live


def provision(
    *,
    name: str,
    env: str,
    target: Target,
    allowed: list[str],
    path: Path,
    values: dict[str, str],
    rotate_api_key: bool,
    dry_run: bool,
    console: Console,
    mint_api_key: bool = True,
    metrics_name: str = "",
) -> SecretPlan:
    """Plan and apply (``secrets apply``): :func:`prepare` then :func:`apply_plan`."""
    plan, live = prepare(
        name=name,
        env=env,
        target=target,
        allowed=allowed,
        path=path,
        values=values,
        rotate_api_key=rotate_api_key,
        dry_run=dry_run,
        mint_api_key=mint_api_key,
        metrics_name=metrics_name,
    )
    apply_plan(plan, dry_run=dry_run, console=console, live=live)
    return plan


def secret_keys_present(
    name: str, target: Target, *, dry_run: bool = False, console: Console | None = None
) -> set[str] | None:
    """Keys present in the Secret, ``None`` when it does not exist (or on dry-run).

    Only kubectl's ``(NotFound)`` reason means "absent" (a missing Secret or
    namespace); a connection, credential or RBAC failure is a tool failure
    (exit 2) with kubectl's own message, never a fabricated "missing" report.
    """
    cmd = _kube.kubectl_args(["get", "secret", name, "-o", "json"], target)
    result = _kube.run_cmd(cmd, check=False, dry_run=dry_run, console=console)
    if dry_run:
        return None
    stderr = (result.stderr or "").strip()
    if result.returncode != 0:
        if "(NotFound)" in stderr:
            return None
        detail = stderr or (result.stdout or "").strip()
        raise _kube.ToolFailed(
            f"Command failed (exit code {result.returncode}): {_kube.format_cmd(cmd)}"
            + (f"\n{detail}" if detail else "")
        )
    if not (result.stdout or "").strip():
        raise _kube.ToolFailed(f"kubectl get secret {name} returned no output")

    try:
        body = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise _kube.ToolFailed(f"kubectl returned invalid JSON: {e}") from e
    data = body.get("data") or {}
    string_data = body.get("stringData") or {}
    return set(data) | set(string_data)


def _env_file_note(env: str) -> str:
    fallback = ", else .env" if _modes.is_dev_env(env) else ""
    return f"reads .env.{env}{fallback}; --env-file <file> reads another"


# --------------------------------------------------------------------------- snapshot / restore


@dataclass
class Snapshot:
    """A Secret as it was before this run applied it, to restore after a failed deploy.

    ``applied`` is what this run wrote (plain values, never printed) and
    ``managed`` the keys it manages (the allow-list, or ``METRICS_TOKEN``).
    """

    name: str
    target: Target
    before: LiveSecret
    applied: dict[str, str]
    managed: frozenset[str]
    env: str = ""

    @property
    def changed(self) -> list[str]:
        """Keys this run added or changed (names only)."""
        return sorted(
            k
            for k, v in self.applied.items()
            if k in self.before.undecodable or self.before.values.get(k) != v
        )

    @property
    def removed(self) -> list[str]:
        """Keys this run's server-side apply removes: ones graph-agents-cli applied before
        and this run leaves out (a key dropped from the allow-list)."""
        return sorted(
            k for k in self.before.raw if k in self.before.owned and k not in self.applied
        )

    @property
    def touched(self) -> list[str]:
        """Keys this run added, changed or removed (names only)."""
        return sorted({*self.changed, *self.removed})


def snapshot(plan: SecretPlan, live: LiveSecret | None, *, managed: list[str]) -> list[Snapshot]:
    """The app Secret (``live``, read by :func:`prepare`) and the metrics Secret before the apply.

    Changes nothing; the metrics Secret is read only when this apply writes it.
    """
    snaps: list[Snapshot] = []
    if live is not None:
        snaps.append(
            Snapshot(plan.name, plan.target, live, dict(plan.data), frozenset(managed), plan.env)
        )
    metrics = metrics_plan(plan)
    if metrics is not None:
        snaps.append(
            Snapshot(
                metrics.name,
                metrics.target,
                read_live_secret(metrics.name, metrics.target),
                dict(metrics.data),
                frozenset({METRICS_TOKEN_KEY}),
                plan.env,
            )
        )
    return snaps


def _restore_manifest(snap: Snapshot, current: LiveSecret) -> str:
    """The previous values of the keys this run manages, plus any key it removed.

    Keys this run added are left out, so the server-side apply removes them (the
    graph-agents-cli field manager owns them). ``resourceVersion`` makes the apply
    fail rather than overwrite a change made after the check.
    """
    data = {
        k: raw for k, raw in snap.before.raw.items() if k in snap.managed or k not in current.raw
    }
    body: dict[str, object] = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": snap.name, "namespace": snap.target.namespace},
        "type": "Opaque",
        "data": data,
    }
    if current.resource_version:
        body["metadata"]["resourceVersion"] = current.resource_version  # type: ignore[index]
    return yaml.safe_dump(body, sort_keys=False)


def _is_metrics(snap: Snapshot) -> bool:
    return snap.managed == frozenset({METRICS_TOKEN_KEY})


def _readers(snap: Snapshot) -> str:
    """Who reads the Secret's new values, for the lines about a Secret left in place."""
    if _is_metrics(snap):
        return "Prometheus sends it at its next scrape"
    return "running pods read them at their next restart"


def restore(snaps: list[Snapshot], *, console: Console) -> list[str]:
    """Put each Secret back as it was before this run; returns one line per Secret for the error.

    A Secret is restored only while it still holds exactly what this run applied:
    if anything changed it since (another deploy, a ``secrets apply``), it is left
    alone and the line says so. One this run created is deleted. Keys this run
    removed (dropped from the allow-list) are put back too. The metrics Secret
    follows the app Secret: when the app Secret is left with this run's values,
    so is the metrics Secret, so both keep the same ``METRICS_TOKEN``. Never raises.
    """
    notes: list[str] = []
    app_left: Snapshot | None = None  # the app Secret, when it keeps this run's values
    for snap in sorted(snaps, key=_is_metrics):  # the app Secret first
        if not snap.touched:
            continue
        keys = ", ".join(snap.touched)
        if _is_metrics(snap) and app_left is not None:
            notes.append(
                f"Secret {snap.name} was left as it is too, so its METRICS_TOKEN matches "
                f"{app_left.name}'s (this deploy had changed {keys})."
            )
            continue
        restored = False
        try:
            current = read_live_secret(snap.name, snap.target)
            if (
                current.exists == snap.before.exists
                and all(current.raw.get(k) == snap.before.raw.get(k) for k in snap.applied)
                and all(k in current.raw for k in snap.removed)
            ):
                continue  # the apply never landed: nothing to put back
            if not current.exists or any(
                current.values.get(k) != v for k, v in snap.applied.items()
            ):
                notes.append(
                    f"Secret {snap.name} was changed by someone else after this deploy applied "
                    f"it, so it was left as it is (this deploy had changed {keys})."
                )
            elif not snap.before.exists:
                _kube.kubectl(
                    ["delete", "secret", snap.name, "--ignore-not-found"],
                    snap.target,
                    console=console,
                )
                notes.append(f"Secret {snap.name}, which this deploy created, was deleted.")
                restored = True
            else:
                result = _kube.run_cmd(
                    _apply_cmd(SecretPlan(snap.name, snap.target, {})),
                    input_text=_restore_manifest(snap, current),
                    check=False,
                    console=console,
                )
                if result.returncode != 0 and "has been modified" in (result.stderr or ""):
                    # The resourceVersion precondition: changed between the check and the apply.
                    notes.append(
                        f"Secret {snap.name} was changed by someone else while this deploy "
                        f"restored it, so it was left as it is (this deploy had changed {keys})."
                    )
                elif result.returncode != 0:
                    raise _kube.ToolFailed(
                        (result.stderr or result.stdout or "").strip()
                        or f"kubectl apply exited {result.returncode}"
                    )
                else:
                    notes.append(
                        f"Secret {snap.name} was restored to its values from before this deploy "
                        f"({keys})."
                    )
                    restored = True
        except _kube.DeployError as e:
            first = next((line for line in str(e).splitlines() if line.strip()), "kubectl failed")
            readers = (
                "Prometheus sends at its next scrape"
                if _is_metrics(snap)
                else "running pods read at their next restart"
            )
            notes.append(
                f"Secret {snap.name} could NOT be restored ({first.strip()}): it still holds "
                f"this deploy's values for {keys}, which {readers}. Re-apply the previous "
                f"values with `graph-agents-cli secrets apply --env {snap.env or '<env>'} "
                "--env-file <previous env file>`."
            )
        if not restored and not _is_metrics(snap):
            app_left = snap
    return notes


def describe_unrestored(snaps: list[Snapshot], env: str, why: str) -> list[str]:
    """Lines for Secrets this run changed and left in place (the release was not restored)."""
    return [
        f"Secret {snap.name} keeps this deploy's values for {', '.join(snap.touched)} ({why}); "
        f"{_readers(snap)}. To go back, re-apply the previous values: "
        f"`graph-agents-cli secrets apply --env {env} --env-file <previous env file>`."
        for snap in snaps
        if snap.touched
    ]


def provisioning_procedure(*, project: str, env: str, owner: str, mode: str) -> str:
    """Text printed when deploy refuses to touch Secrets in a CD mode."""
    who = owner or "the platform operator named in secrets.owner"
    return (
        f"In {mode} mode `deploy` never touches Secrets. {who} provisions the Secret "
        f"once per environment from a workstation with cluster access:\n"
        f"    graph-agents-cli secrets apply --env {env}   ({_env_file_note(env)})\n"
        f"  (or `kubectl create secret generic {project}-app ...`). Rotate with `secrets apply` "
        f"followed by `graph-agents-cli deploy --env {env} --restart`."
    )
