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

"""Load test against `POST /chat`, the SSE endpoint every client uses.

Sends the bearer key from `GRAPH_AGENTS_CLI_API_KEY` (or `API_KEY`) and counts
a request as successful once the first `message.delta` arrives.
"""

from __future__ import annotations

import logging
import os
import time
import uuid

from locust import HttpUser, between, task

ENDPOINT = "/chat"
PROMPT = os.environ.get("LOAD_TEST_PROMPT", "What's the weather in San Francisco?")

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


def _headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
    key = os.environ.get("GRAPH_AGENTS_CLI_API_KEY") or os.environ.get("API_KEY")
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return headers


class ChatStreamUser(HttpUser):
    """Each user keeps one thread and sends messages the way a product would."""

    wait_time = between(1, 3)

    def on_start(self) -> None:
        self.thread_id = str(uuid.uuid4())

    @task
    def chat_stream(self) -> None:
        body = {"thread_id": self.thread_id, "message": PROMPT, "metadata": {"source": "load_test"}}
        start_time = time.time()
        with self.client.post(
            ENDPOINT,
            name=f"{ENDPOINT} (SSE)",
            headers=_headers(),
            json=body,
            catch_response=True,
            stream=True,
        ) as response:
            if response.status_code != 200:
                response.failure(f"Got status code {response.status_code}")
                return
            first_delta = None
            for line in response.iter_lines():
                if line and line.startswith(b"event: message.delta"):
                    first_delta = time.time() - start_time
                    break
            if first_delta is None:
                response.failure("Stream closed without a message.delta")
            else:
                response.success()
                logger.info("First delta in %.2fs", first_delta)
