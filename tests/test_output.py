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

"""Tables in CI logs and pipes keep their cells whole; rules keep the normal width."""

from __future__ import annotations

import io

import pytest
from rich.table import Table

from graph_agents_cli import _output

LONG_TOOL = "orders_with_a_rather_long_module_name.list_orders_for_a_customer"
LONG_PATH = "/customers/{customer_id}/orders/{order_id}/lines/{line_id}/history"


def _table(*, fold: bool = False) -> Table:
    table = Table(title="API policy check")
    for header in ("Tool", "Operation", "Reason"):
        table.add_column(header, overflow="fold" if fold else "ellipsis")
    table.add_row(
        LONG_TOOL, LONG_PATH, "allowed by allowed_operations entry listOrdersForACustomer"
    )
    return table


@pytest.fixture(autouse=True)
def _no_columns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("COLUMNS", raising=False)


def test_off_a_terminal_a_table_grows_to_fit_its_cells() -> None:
    buffer = io.StringIO()
    console = _output.Console(file=buffer)
    assert not console.is_terminal and console.width == 80
    _output.print_table(console, _table())
    text = buffer.getvalue()
    assert LONG_TOOL in text and LONG_PATH in text and "…" not in text
    assert max(len(line) for line in text.splitlines()) > 80
    assert console.width == 80  # restored: a rule after the table is not stretched
    console.rule("next")
    assert len(buffer.getvalue().splitlines()[-1]) == 80


def test_a_chosen_width_is_kept_and_fold_keeps_the_cells() -> None:
    buffer = io.StringIO()
    console = _output.Console(file=buffer, width=70)
    _output.print_table(console, _table(fold=True))
    text = buffer.getvalue()
    assert max(len(line) for line in text.splitlines()) <= 70
    assert "…" not in text
    assert "".join(text.split()).count("orders_with_a_rather") == 1


def test_columns_is_honoured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COLUMNS", "90")
    buffer = io.StringIO()
    console = _output.Console(file=buffer)
    _output.print_table(console, _table(fold=True))
    assert max(len(line) for line in buffer.getvalue().splitlines()) <= 90


def test_a_very_wide_table_stops_at_the_cap() -> None:
    table = Table()
    table.add_column("cell", overflow="fold")
    table.add_row("x" * 600)
    buffer = io.StringIO()
    _output.print_table(_output.Console(file=buffer), table)
    lines = buffer.getvalue().splitlines()
    assert max(len(line) for line in lines) == _output.NON_TERMINAL_TABLE_WIDTH
    assert "".join(lines).count("x") == 600
