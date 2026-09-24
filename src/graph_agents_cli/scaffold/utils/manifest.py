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

"""Post-render reconciliation of the generated project manifest.

The ``_shared`` layer renders ``graph-agents-cli-manifest.yaml`` from the
cookiecutter variables. ``finalize_manifest`` then guarantees the contract
whatever the template rendered: every create parameter is recorded, the
``environments`` block exists only for the kubernetes target, ``secrets.keys``
is never empty (and carries the ``token_env`` of every ``auth: bearer`` API in
``api-policy.yaml``), ``api_policy`` is present only with a policy file, and
``process`` is always a key. Values the template already rendered correctly
are left alone; the file is rewritten only when something had to change.
"""

from __future__ import annotations

import logging
import pathlib
from dataclasses import dataclass
from typing import Any

import yaml

from graph_agents_cli._api_policy import (
    POLICY_FILENAME as API_POLICY_FILENAME,
)
from graph_agents_cli._api_policy import (
    ApiSummary,
    ExampleCall,
    bearer_token_envs,
)
from graph_agents_cli._defaults import (
    ENVIRONMENTS,
    auth_policy_implemented_default,
    default_secret_keys,
)

MANIFEST_FILENAME = "graph-agents-cli-manifest.yaml"


@dataclass(frozen=True)
class CreateParams:
    """The resolved create parameters recorded in the manifest."""

    deployment_target: str
    runtime: str
    model_provider: str
    model: str
    checkpointer: str
    registry: str
    cd: str
    auth_policy: str
    has_api_policy: bool = False
    process: str | None = None
    # ``None`` derives the flag from ``auth_policy`` (a fresh scaffold). A recorded
    # value is carried through by ``enhance``/``upgrade`` so the developer's flip
    # after implementing the ``custom`` stub survives a re-render.
    auth_policy_implemented: bool | None = None
    # The APIs declared in ``api-policy.yaml`` (empty without a policy): the
    # templates document their variables and every ``auth: bearer`` API's
    # ``token_env`` joins ``secrets.keys`` so the token reaches the Secret.
    apis: tuple[ApiSummary, ...] = ()
    # The call the rendered example tool makes (the first operation the policy's
    # first API allows, any method), chosen so lint and the project's policy test
    # accept it; None when that API allows none the example can make (the example
    # is then not rendered).
    example_call: ExampleCall | None = None

    @property
    def api_token_envs(self) -> list[str]:
        return bearer_token_envs(self.apis)

    def resolved_auth_policy_implemented(self) -> bool:
        if self.auth_policy_implemented is not None:
            return bool(self.auth_policy_implemented)
        return auth_policy_implemented_default(self.auth_policy)

    def as_manifest_dict(self) -> dict[str, Any]:
        """The ``create_params`` keys this object owns (agent_guidance_filename excluded)."""
        return {
            "deployment_target": self.deployment_target,
            "runtime": self.runtime,
            "model_provider": self.model_provider,
            "model": self.model,
            "checkpointer": self.checkpointer,
            "registry": self.registry,
            "cd": self.cd,
            "auth_policy": self.auth_policy,
            "auth_policy_implemented": self.resolved_auth_policy_implemented(),
        }


def recompute_secret_keys(
    recorded: list[str], old_defaults: list[str], new_defaults: list[str]
) -> list[str]:
    """The ``secrets.keys`` allow-list after a provider or runtime change.

    Template-managed keys follow the new defaults, in their order (so an
    untouched list ends up exactly as a fresh ``create`` writes it): a default
    of the old settings the new ones drop goes, a new default comes in. Keys
    the developer added are kept, after the defaults, and a default the
    developer deliberately removed stays removed while it remains a default.
    """
    managed = [k for k in new_defaults if k not in old_defaults or k in recorded]
    added_by_user = [k for k in recorded if k not in old_defaults and k not in new_defaults]
    result: list[str] = []
    for key in [*managed, *added_by_user]:
        if key not in result:
            result.append(key)
    return result


def reconcile_secret_keys(
    project_dir: pathlib.Path, *, previous: CreateParams, current: CreateParams
) -> tuple[list[str], list[str]]:
    """Rewrite the manifest's ``secrets.keys`` for ``current``; return ``(added, removed)``.

    ``previous`` holds the settings the recorded list was derived from. Nothing
    is written when the list does not change or the manifest cannot be read.
    """
    manifest_path = project_dir / MANIFEST_FILENAME
    try:
        with open(manifest_path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError) as e:
        logging.warning("Could not read %s: %s", manifest_path, e)
        return [], []
    if not isinstance(data, dict):
        return [], []
    secrets = data.get("secrets")
    if not isinstance(secrets, dict):
        secrets = {}
        data["secrets"] = secrets
    raw_keys = secrets.get("keys")
    if isinstance(raw_keys, str):
        raw_keys = [raw_keys]
    recorded = [str(k) for k in raw_keys] if raw_keys else []
    old_defaults = default_secret_keys(
        previous.model_provider, previous.runtime, previous.api_token_envs
    )
    new_defaults = default_secret_keys(
        current.model_provider, current.runtime, current.api_token_envs
    )
    keys = recompute_secret_keys(recorded or old_defaults, old_defaults, new_defaults)
    if keys == recorded:
        return [], []
    secrets["keys"] = keys
    with open(manifest_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, default_flow_style=False, sort_keys=False)
    return [k for k in keys if k not in recorded], [k for k in recorded if k not in keys]


def default_environments(project_name: str) -> dict[str, dict[str, str]]:
    """dev/staging/prod with namespace ``<name>-<env>`` and an empty context."""
    return {env: {"context": "", "namespace": f"{project_name}-{env}"} for env in ENVIRONMENTS}


def finalize_manifest(
    project_dir: pathlib.Path,
    *,
    project_name: str,
    params: CreateParams,
    cli_version: str | None = None,
) -> bool:
    """Reconcile the rendered manifest with the contract; return True if it was rewritten."""
    manifest_path = project_dir / MANIFEST_FILENAME
    if not manifest_path.exists():
        logging.warning("No %s was rendered in %s", MANIFEST_FILENAME, project_dir)
        return False

    try:
        with open(manifest_path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception as e:
        logging.warning("Could not read %s: %s", manifest_path, e)
        return False
    if not isinstance(data, dict):
        logging.warning("%s is not a mapping; leaving it alone", manifest_path)
        return False

    changed = False

    if data.get("name") != project_name:
        data["name"] = project_name
        changed = True
    if cli_version and data.get("cli_version") != cli_version:
        data["cli_version"] = cli_version
        changed = True

    create_params = data.get("create_params")
    if not isinstance(create_params, dict):
        create_params = {}
        data["create_params"] = create_params
        changed = True
    for key, value in params.as_manifest_dict().items():
        if create_params.get(key) != value:
            if key == "auth_policy_implemented" and key in create_params:
                logging.info(
                    "%s: auth_policy_implemented %r -> %r",
                    manifest_path,
                    create_params.get(key),
                    value,
                )
            create_params[key] = value
            changed = True

    if params.deployment_target == "kubernetes":
        environments = data.get("environments")
        if not isinstance(environments, dict):
            environments = {}
        merged = dict(environments)
        for env, defaults in default_environments(project_name).items():
            recorded = merged.get(env)
            if not isinstance(recorded, dict):
                merged[env] = dict(defaults)
            else:
                merged[env] = {
                    "context": recorded.get("context") or "",
                    "namespace": recorded.get("namespace") or defaults["namespace"],
                }
        if merged != environments:
            data["environments"] = merged
            changed = True
    elif "environments" in data:
        del data["environments"]
        changed = True

    secrets = data.get("secrets")
    if not isinstance(secrets, dict):
        secrets = {}
        data["secrets"] = secrets
        changed = True
    raw_keys = secrets.get("keys")
    if isinstance(raw_keys, str):
        raw_keys = [raw_keys]
    keys = [str(k) for k in raw_keys] if raw_keys else []
    if not keys:
        keys = default_secret_keys(params.model_provider, params.runtime, params.api_token_envs)
    else:
        # Every bearer API needs its token in the Secret; user-added keys stay.
        keys = [*keys, *(k for k in params.api_token_envs if k not in keys)]
    if secrets.get("keys") != keys:
        secrets["keys"] = keys
        changed = True
    if "owner" not in secrets or secrets.get("owner") is None:
        secrets["owner"] = ""
        changed = True

    if params.has_api_policy:
        api_policy = data.get("api_policy")
        if not isinstance(api_policy, dict) or not api_policy.get("policy_file"):
            data["api_policy"] = {"policy_file": API_POLICY_FILENAME}
            changed = True
    elif "api_policy" in data:
        del data["api_policy"]
        changed = True

    process = params.process or None
    if "process" not in data or data.get("process") != process:
        data["process"] = process
        changed = True

    if changed:
        with open(manifest_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, default_flow_style=False, sort_keys=False)
        logging.debug("Reconciled %s with the manifest contract", manifest_path)
    return changed
