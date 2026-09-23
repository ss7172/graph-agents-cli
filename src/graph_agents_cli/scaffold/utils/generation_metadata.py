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

from graph_agents_cli._defaults import DEFAULT_AGENT_GUIDANCE_FILENAME
from graph_agents_cli._project import ProjectConfig
from graph_agents_cli.scaffold.utils import remote_template


def metadata_to_cli_args(
    metadata: ProjectConfig,
    *,
    for_enhance: bool = False,
) -> list[str]:
    """Convert a ProjectConfig into the ``create`` flags that reproduce it.

    Every create parameter is passed explicitly, including defaults, so a
    re-render is deterministic whatever the running CLI's defaults are.
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

    if metadata.agent_guidance_filename != DEFAULT_AGENT_GUIDANCE_FILENAME:
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
    args.extend(["--auth-policy", metadata.auth_policy])
    if metadata.process:
        args.extend(["--process", metadata.process])

    return args
