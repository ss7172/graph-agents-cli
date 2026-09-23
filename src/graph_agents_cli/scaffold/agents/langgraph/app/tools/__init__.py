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

"""Tool registry.

Convention: every module in this package declares ``API_CALLS``, a
module-level list of ``{"api", "method", "operation_id", "path"}`` dicts naming
every external API call it makes (``[]`` when it makes none), and ``TOOLS``,
the list of tool objects it contributes. External calls go through
``app_utils.api_client.get_client`` only, which enforces `api-policy.yaml` at
runtime; `graph-agents-cli lint` checks the declarations against the same file.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
from typing import Any

logger = logging.getLogger(__name__)


def get_tools() -> list[Any]:
    """Every tool contributed by the modules of this package, in module-name order."""
    tools: list[Any] = []
    for info in sorted(pkgutil.iter_modules(__path__), key=lambda i: i.name):
        module = importlib.import_module(f"{__name__}.{info.name}")
        if not hasattr(module, "API_CALLS"):
            logger.warning(
                "%s declares no API_CALLS (use [] when it calls no external API).",
                module.__name__,
            )
        tools.extend(getattr(module, "TOOLS", []))
    return tools
