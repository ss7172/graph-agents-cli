# Load testing

[Locust](http://locust.io) drives `POST /chat`, the SSE endpoint this project serves.

Local, against `graph-agents-cli playground` or `uv run uvicorn {{cookiecutter.agent_directory}}.fast_api_app:app --port 8000`:

```bash
export GRAPH_AGENTS_CLI_API_KEY=<the API_KEY from .env>   # jwt: a token, e.g. from graph-agents-cli auth dev-token
export LOAD_TEST_PROMPT="<a request that exercises your tools>"   # optional
uv run --with locust locust -f tests/load_test/load_test.py -H http://127.0.0.1:8000 \
    -u 10 -r 2 -t 30s --headless
```

Against a deployed environment, point at its URL and send that environment's key:

```bash
export GRAPH_AGENTS_CLI_API_KEY=<staging API_KEY>
uv run --with locust locust -f tests/load_test/load_test.py -H https://agent.staging.example.com \
    -u 10 -r 2 -t 60s --headless --csv=tests/load_test/.results/report
```

`--csv` writes the latency and failure tables Locust prints at the end.
