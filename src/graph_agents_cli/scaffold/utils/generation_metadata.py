# Copyright 2026 Google LLC
# Modifications Copyright 2026 graph-agents-cli contributors
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

"""Map the project manifest back to ``create`` flags (used by upgrade and enhance)."""

from packaging import version as pkg_version

from graph_agents_cli._defaults import LEGACY_AUTH_POLICY_ALIASES
from graph_agents_cli._project import ProjectConfig
from graph_agents_cli.scaffold.utils import remote_template

# The first CLI version that knows the current auth policy names; an older CLI
# (an authentic upgrade baseline) is given the retired name instead.
AUTH_POLICY_RENAME_VERSION = "0.2.0"


def auth_policy_for_version(auth_policy: str, cli_version: str | None) -> str:
    """``auth_policy`` as the CLI ``cli_version`` spells it (None: the running CLI)."""
    if not cli_version:
        return auth_policy
    try:
        older = pkg_version.parse(cli_version) < pkg_version.parse(AUTH_POLICY_RENAME_VERSION)
    except pkg_version.InvalidVersion:
        return auth_policy
    if not older:
        return auth_policy
    retired = {new: old for old, new in LEGACY_AUTH_POLICY_ALIASES.items()}
    return retired.get(auth_policy, auth_policy)


def metadata_to_cli_args(
    metadata: ProjectConfig,
    *,
    for_enhance: bool = False,
    cli_version: str | None = None,
) -> list[str]:
    """Convert a ProjectConfig into the ``create`` flags that reproduce it.

    Every create parameter is passed explicitly, including defaults, so a
    re-render is deterministic whatever the running CLI's defaults are.
    ``cli_version`` names the CLI that will receive the flags when it is not
    the running one (an authentic upgrade baseline), so retired spellings are
    used where that version needs them.
    """
    args: list[str] = []

    if metadata.base_template:
        is_spec = remote_template.is_template_spec(metadata.base_template)
        if not for_enhance:
            args.extend(["--agent", metadata.base_template])
        elif is_spec:
            # A spec goes in `enhance`'s positional argument, which is what it
            # fetches. --base-template only names a template inside the wheel.
            args.append(metadata.base_template)
        else:
            args.extend(["--base-template", metadata.base_template])

    if metadata.agent_directory and metadata.agent_directory != "app":
        args.extend(["--agent-directory", metadata.agent_directory])

    args.extend(["--agent-guidance-filename", metadata.agent_guidance_filename])

    args.extend(["--deployment-target", metadata.deployment_target])
    args.extend(["--runtime", metadata.runtime])
    args.extend(["--model-provider", metadata.model_provider])
    if metadata.model:
        args.extend(["--model", metadata.model])
    args.extend(["--checkpointer", metadata.checkpointer])
    if metadata.registry:
        args.extend(["--registry", metadata.registry])
    args.extend(["--cd", metadata.cd])
    args.extend(["--auth-policy", auth_policy_for_version(metadata.auth_policy, cli_version)])
    if metadata.process:
        args.extend(["--process", metadata.process])

    return args
