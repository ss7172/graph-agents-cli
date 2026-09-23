# Copyright 2026 Google LLC
# Modifications Copyright 2026 graph-agents-cli contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Experiment flags.

The registry is intentionally empty (no feature is experimental today): the
mechanism is kept so a future flag can be gated behind
``GRAPH_AGENTS_CLI_EXPERIMENTS='{"label": value}'`` without re-inventing it.
The former ``build_command`` gate is gone; ``build`` is always available.
"""

import json
import logging
import os
from typing import Any, NamedTuple

EXPERIMENTS_ENV = "GRAPH_AGENTS_CLI_EXPERIMENTS"


class Experiment(NamedTuple):
    label: str
    value_type: type
    default_value: Any


# Central registry of experiments: new experiment flags get added here.
_REGISTRY: dict[str, Experiment] = {}


def resolve_experiment(label: str) -> Any:
    """Returns the value for the given experiment label."""
    if label not in _REGISTRY:
        raise ValueError(f"Unknown experiment: {label}")

    exp = _REGISTRY[label]

    # Check for environment variable override
    env_val = os.environ.get(EXPERIMENTS_ENV)
    if env_val:
        try:
            overrides = json.loads(env_val)
            if label in overrides:
                val = overrides[label]
                # Cast to correct type if necessary
                return exp.value_type(val)
        except Exception as e:
            logging.warning(
                f"Failed to apply {EXPERIMENTS_ENV} override for '{label}', "
                f"using default ({exp.default_value}). Error: {e}"
            )

    return exp.default_value
