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

"""Rendered-project snapshots: the real `create` against the bundled template.

Not slow and needs no network: every combination in
``scripts/regen_fixtures.py`` is rendered in process with ``--skip-deps`` and
its file list and manifest are compared to ``tests/fixtures/rendered/<combo>/``.
On a deliberate template change, run ``uv run python scripts/regen_fixtures.py``
and review the fixture diff.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import regen_fixtures as rf
import yaml


@pytest.fixture(scope="module")
def rendered(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    root = tmp_path_factory.mktemp("rendered")
    return {name: rf.render_combination(name, root / name) for name in rf.COMBINATIONS}


@pytest.mark.parametrize("name", list(rf.COMBINATIONS))
def test_file_list_matches_fixture(rendered: dict[str, Path], name: str) -> None:
    expected_files, _ = rf.read_fixture(name)
    actual_files = rf.list_files(rendered[name])
    missing = sorted(set(expected_files) - set(actual_files))
    extra = sorted(set(actual_files) - set(expected_files))
    assert not missing and not extra, (
        f"{name}: rendered file list differs from tests/fixtures/rendered/{name}/files.json\n"
        f"  missing: {missing}\n  extra: {extra}\n"
        "  (run `uv run python scripts/regen_fixtures.py` after a deliberate template change)"
    )


@pytest.mark.parametrize("name", list(rf.COMBINATIONS))
def test_manifest_matches_fixture(rendered: dict[str, Path], name: str) -> None:
    _, expected_manifest = rf.read_fixture(name)
    actual_manifest = rf.normalized_manifest(rendered[name])
    assert actual_manifest == expected_manifest, (
        f"{name}: rendered manifest differs from tests/fixtures/rendered/{name}/manifest.yaml"
    )
    assert rf.GENERATED_AT_PLACEHOLDER in actual_manifest


@pytest.mark.parametrize("name", list(rf.COMBINATIONS))
def test_layout_follows_contracts(rendered: dict[str, Path], name: str) -> None:
    """CONTRACTS sections 2 and 3, independent of the exact fixture content."""
    combo = rf.COMBINATIONS[name]
    project = rendered[name]
    for rel in combo.expect_present:
        assert (project / rel).exists(), f"{name}: expected {rel}"
    for rel in combo.expect_absent:
        assert not (project / rel).exists(), f"{name}: did not expect {rel}"

    manifest = yaml.safe_load((project / rf.MANIFEST_FILENAME).read_text(encoding="utf-8"))
    params = manifest["create_params"]
    assert manifest["name"] == combo.project_name
    for key, value in combo.manifest.items():
        if key == "environments":
            assert ("environments" in manifest) is value, f"{name}: environments block"
            if value:
                assert set(manifest["environments"]) == {"dev", "staging", "prod"}
                for env, block in manifest["environments"].items():
                    assert block["namespace"] == f"{combo.project_name}-{env}"
        elif key == "product_api":
            assert ("product_api" in manifest) is value, f"{name}: product_api block"
            if value:
                assert manifest["product_api"]["policy_file"] == "product-policy.yaml"
        elif key == "secret_keys":
            assert manifest["secrets"]["keys"] == value, f"{name}: secrets.keys"
        elif key == "process":
            assert manifest.get("process") == value
        else:
            assert params[key] == value, f"{name}: create_params.{key}"
    assert "process" in manifest
    assert manifest["secrets"]["keys"], f"{name}: secrets.keys must not be empty"

    # Runtime files: exactly one lock, the runtime's Dockerfile.
    dockerfile = (project / "Dockerfile").read_text(encoding="utf-8")
    if params["runtime"] == "langgraph-server":
        assert "langgraph-api" in dockerfile
    else:
        assert "langgraph-api" not in dockerfile.split("\n")[0]
    assert (project / "uv.lock").is_file()
    lock_head = (project / "uv.lock").read_text(encoding="utf-8")
    assert "{{cookiecutter" not in lock_head
    assert f'name = "{combo.project_name}"' in lock_head


def test_fixture_directories_are_complete() -> None:
    present = {p.name for p in rf.FIXTURES_DIR.iterdir() if p.is_dir() and p.name != "_inputs"}
    assert present == set(rf.COMBINATIONS), (
        "tests/fixtures/rendered/ and scripts/regen_fixtures.py COMBINATIONS disagree; "
        "run `uv run python scripts/regen_fixtures.py`"
    )
