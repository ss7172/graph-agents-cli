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
is never empty (and carries the bearer token variable of an ``auth: bearer``
product policy), ``product_api`` is present only with a policy file, and
``process`` is always a key. Values the template already rendered correctly
are left alone; the file is rewritten only when something had to change.
"""

from __future__ import annotations

import logging
import pathlib
from dataclasses import dataclass
from typing import Any

import yaml

from graph_agents_cli._defaults import ENVIRONMENTS, default_secret_keys

MANIFEST_FILENAME = "graph-agents-cli-manifest.yaml"
PRODUCT_POLICY_FILENAME = "product-policy.yaml"
DEFAULT_PRODUCT_TOKEN_ENV = "PRODUCT_API_TOKEN"


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
    has_product_policy: bool = False
    process: str | None = None
    # ``None`` derives the flag from ``auth_policy`` (a fresh scaffold). A recorded
    # value is carried through by ``enhance``/``upgrade`` so the developer's flip
    # after implementing the product-session stub survives (Section 7 item 11).
    auth_policy_implemented: bool | None = None
    # The variable holding the bearer token when the product policy sets
    # ``auth: bearer`` (``token_env``, default ``PRODUCT_API_TOKEN``); it joins
    # ``secrets.keys`` (Section 7 item 21, CONTRACTS section 4).
    product_token_env: str | None = None

    def resolved_auth_policy_implemented(self) -> bool:
        if self.auth_policy_implemented is not None:
            return bool(self.auth_policy_implemented)
        return self.auth_policy != "product-session"

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


def bearer_token_env(path: str | pathlib.Path | None) -> str | None:
    """The token variable an ``auth: bearer`` product policy reads, else ``None``.

    Accepts the documented shape (top-level ``product_api:``) and a file whose
    keys are the policy fields directly. A missing, unreadable or non-bearer
    policy yields ``None`` (with a warning when the file exists but cannot be
    parsed), so the caller adds nothing to ``secrets.keys``.
    """
    if not path:
        return None
    policy_path = pathlib.Path(path)
    if not policy_path.is_file():
        return None
    try:
        with open(policy_path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError) as e:
        logging.warning("Could not read %s to derive the token variable: %s", policy_path, e)
        return None
    if not isinstance(data, dict):
        return None
    section = data.get("product_api", data)
    if not isinstance(section, dict):
        return None
    if str(section.get("auth") or "none") != "bearer":
        return None
    return str(section.get("token_env") or DEFAULT_PRODUCT_TOKEN_ENV)


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
        keys = default_secret_keys(params.model_provider, params.runtime, params.product_token_env)
    elif params.product_token_env and params.product_token_env not in keys:
        # A bearer product policy needs its token in the Secret (CONTRACTS section 4).
        keys = [*keys, params.product_token_env]
    if secrets.get("keys") != keys:
        secrets["keys"] = keys
        changed = True
    if "owner" not in secrets or secrets.get("owner") is None:
        secrets["owner"] = ""
        changed = True

    if params.has_product_policy:
        product_api = data.get("product_api")
        if not isinstance(product_api, dict) or not product_api.get("policy_file"):
            data["product_api"] = {"policy_file": PRODUCT_POLICY_FILENAME}
            changed = True
    elif "product_api" in data:
        del data["product_api"]
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
