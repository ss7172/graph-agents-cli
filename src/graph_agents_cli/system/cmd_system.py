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

"""graph-agents-cli system commands: agent projects that call each other, seen as one.

``graph-agents-system.yaml`` names the agents, their projects and who calls whom
(``_model``). ``check`` reports what would keep them from calling each other,
``apply`` writes each project's side of every edge, ``graph`` draws the system,
``delegations`` prints what the token issuer must allow, and ``deploy`` deploys
every project, callees first, a few at a time. The file is optional: each
project stays complete on its own (``peer add``).
"""

from __future__ import annotations

import json
from typing import Any

import click
from rich.markup import escape

from graph_agents_cli._click import LazyGroup
from graph_agents_cli._output import Console
from graph_agents_cli.api._files import print_diff
from graph_agents_cli.system import _apply, _checks, _deploy, _views
from graph_agents_cli.system._model import EXIT_INVALID, MAX_PARALLEL, SystemFileError
from graph_agents_cli.system._system import LOCAL, System, locate, resolve

EXIT_FINDING = 1


@click.group("system", cls=LazyGroup)
def system_group() -> None:
    """Check, wire and deploy agents that call each other, as one system.

    graph-agents-system.yaml (in this directory or above, or --file) names the
    agent projects that call each other over A2A: each agent's project, the
    client id it exchanges tokens as, the agents it calls, and the environments
    they run in. Every project stays complete on its own (`peer add`); the file
    adds checks across projects, writes both sides of each edge at once, and
    deploys them in order.

    \b
    Exit codes:
      0  ok
      1  check: a finding with an error; deploy: a deploy or the live check failed
      2  usage error
      3  the file is unreadable or invalid, names an unknown project or an
         ambiguous edge, or a project cannot take its edges
    """


def _file_option(function: Any) -> Any:
    return click.option(
        "--file",
        "file",
        default=None,
        metavar="F",
        help="The system file (default: graph-agents-system.yaml here or in a parent directory).",
    )(function)


def _load(file: str | None) -> System:
    return resolve(locate(file))


def _print_findings(console: Console, findings: list[_checks.Finding]) -> None:
    for finding in findings:
        style = "red" if finding.severity == _checks.ERROR else "yellow"
        where = f" [{finding.env}]" if finding.env else ""
        console.print(
            escape(f"{finding.severity} {finding.check}{where} {finding.message}"),
            style=style,
            highlight=False,
        )
        if finding.fix:
            console.print(f"    fix: {escape(finding.fix)}", highlight=False)


@system_group.command("check")
@_file_option
@click.option("--env", "env", default=None, help="Check one environment (default: every one).")
@click.option(
    "--live",
    "live",
    is_flag=True,
    default=False,
    help="Also ask the cluster and the network: Services, card URLs, the token URL, Secrets.",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Print JSON.")
def cmd_check(file: str | None, env: str | None, live: bool, as_json: bool) -> None:
    """Report what would keep the agents from calling each other (SC01-SC15); exit 1 on an error.

    \b
    SC01 projects, 0.3 runtimes, charts      SC09 cycles, delegation depth
    SC02 each edge is a peer, as the file    SC10 shared database connections
         says; tools/a2a_peers.py in step    SC11 the callers' secrets.keys, the salt
    SC03 the path is the callee's A2A mount  SC12 exchange/forward on langgraph-server
    SC04 issuer, audience, auth policies     SC13 a callee still published publicly
    SC05 appUrl = the URL the caller dials   SC14 (--live) Services, cards, issuer
    SC06 replicas with in-memory A2A tasks   SC15 (--live) the Secrets' keys (names)
    SC07 relays the callee's gates refuse
    SC08 AUTH_ALLOWED_ACTORS lacks a caller

    --live uses each project's recorded kube context (environments.<env>.context), and
    outside dev never the kubeconfig's current one. A local environment's settings are
    in .env, which is never read.
    """
    system = _load(file)
    envs = system.select([env] if env else None)
    findings = _checks.run(system, envs, live=live)
    errors = [f for f in findings if f.severity == _checks.ERROR]
    if as_json:
        click.echo(
            json.dumps(
                {
                    "file": str(system.path),
                    "system": system.file.name,
                    "environments": envs,
                    "live": live,
                    "ok": not errors,
                    "findings": [f.as_dict() for f in findings],
                },
                indent=2,
            )
        )
    else:
        console = Console()
        _print_findings(console, findings)
        warnings = len(findings) - len(errors)
        local = [e for e in envs if system.envs[e].kind == LOCAL]
        summary = (
            f"{len(system.nodes)} agents, {len(system.links)} edges, environments "
            f"{', '.join(envs) or '(none)'}: {len(errors)} error(s), {warnings} warning(s)"
        )
        console.print(summary, style="red" if errors else "green", highlight=False)
        if local:
            console.print(
                f"Not checked for {', '.join(local)}: the settings in each project's .env.",
                style="dim",
            )
    if errors:
        raise SystemExit(EXIT_FINDING)


@system_group.command("apply")
@_file_option
@click.option(
    "--env",
    "envs",
    multiple=True,
    help="Write the settings of these environments only (repeatable; default: every one).",
)
@click.option("--dry-run", "dry_run", is_flag=True, default=False, help="Print the diffs only.")
def cmd_apply(file: str | None, envs: tuple[str, ...], dry_run: bool) -> None:
    """Write each project's side of every edge; idempotent, one diff per project.

    \b
    In each caller, per edge: what `peer add` writes (the policy entry, the
    manifest's secrets.keys, .env.example, the chart values, tools/a2a_peers.py),
    with the called agent's A2A path and its name as the audience; per
    environment, <PEER>_AGENT_URL, TOKEN_EXCHANGE_URL, and networkPolicy.egressTo
    to the called agent's pods; TOKEN_EXCHANGE_CLIENT_ID is the client_id.
    In each called agent: appUrl per environment (the URL its callers dial),
    AUTH_JWT_AUDIENCE when empty, the callers' client ids in AUTH_ALLOWED_ACTORS,
    and networkPolicy.ingressFrom for the callers' pods (plus the Gateway's
    namespace while the route publishes paths).

    Never written: approval gates (the `api approval ... --decide-with relayed`
    line a relay needs is printed), secrets, .env, and local environments
    (printed). A peer of an agent of the file is removed when it has left `calls`;
    other peers and APIs are never touched. An existing peer keeps its limits,
    timeouts, approval timeout and description.
    """
    system = _load(file)
    selected = system.select(list(envs) or None)
    results = _apply.plan_system(system, selected)
    console = Console()
    changed = False
    for result in results:
        node = result.node
        plan = result.plan
        header = f"== {node.name} ({escape(str(node.root))}) =="
        console.print(header, style="bold", highlight=False)
        if not plan.effective:
            console.print("Nothing to change.")
        else:
            changed = True
            print_diff(plan.diff())
            if not dry_run:
                plan.write()
                written = ", ".join(change.path for change in plan.effective)
                console.print(f"Wrote {escape(written)}.", style="green")
        for note in result.notes:
            console.print(f"Note: {escape(note)}", style="yellow", highlight=False)
        if result.todos:
            console.print("Left for you:", style="bold")
            for item in result.todos:
                console.print(f"  - {escape(item)}", highlight=False)
    commands = _apply.relay_commands(system, results)
    if commands:
        console.print(
            "Relays the called agents refuse (never written here: a reviewed loosening there, "
            "SC07):",
            style="bold",
        )
        for callee, why, command in commands:
            where = _checks._where(system, callee)
            console.print(f"  - {escape(why)}:", highlight=False)
            console.print(f"      cd {escape(where)} && {escape(command)}", highlight=False)
    if any(link.auth == "exchange" for link in system.links):
        console.print(
            "At the issuer: register each client and what it may exchange for "
            "(`graph-agents-cli system delegations`).",
            highlight=False,
        )
    if dry_run and changed:
        console.print("Dry run: nothing was written.", style="yellow")
    elif not changed:
        console.print("Every project agrees with the file.", style="green")


@system_group.command("graph")
@_file_option
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["mermaid", "dot", "json"]),
    default="mermaid",
    show_default=True,
)
def cmd_graph(file: str | None, fmt: str) -> None:
    """Draw the system: edges with their auth, relay or deny, and how the callee decides;
    agents with their replicas and A2A task store per environment."""
    data = _views.graph_data(_load(file))
    if fmt == "json":
        click.echo(json.dumps(data, indent=2))
    elif fmt == "dot":
        click.echo(_views.dot(data), nl=False)
    else:
        click.echo(_views.mermaid(data), nl=False)


@system_group.command("delegations")
@_file_option
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["table", "json"]),
    default="table",
    show_default=True,
)
def cmd_delegations(file: str | None, fmt: str) -> None:
    """Print what the token issuer must let each client exchange for (auth: exchange edges),
    and what it must guarantee."""
    data = _views.delegations(_load(file))
    if fmt == "json":
        click.echo(json.dumps(data, indent=2))
        return
    if not data["rows"]:
        click.echo("No edge uses auth: exchange: the issuer grants nothing for this system.")
        return
    if data["issuer"]:
        click.echo(f"Issuer: {data['issuer']}\n")
    click.echo(_views.delegations_table(data), nl=False)
    click.echo("\nThe issuer must also guarantee (graph-agents-cli cannot enforce these):")
    for requirement in data["requirements"]:
        click.echo(f"  - {requirement}")


def _parse_only(system: System, only: str | None) -> list[str]:
    if not only:
        return list(system.nodes)
    names = [n.strip() for n in only.split(",") if n.strip()]
    unknown = [n for n in names if n not in system.nodes]
    if unknown:
        raise click.UsageError(
            f"--only: {', '.join(unknown)} is not an agent of {system.path.name} (agents: "
            f"{', '.join(system.nodes)})"
        )
    return [n for n in system.nodes if n in names]


@system_group.command("deploy")
@click.option("--env", "env", required=True, help="The environment to deploy.")
@_file_option
@click.option(
    "--parallel",
    "parallel",
    type=click.IntRange(1, MAX_PARALLEL),
    default=None,
    help="Deploys at once (default: deploy.parallel in the file, else 3).",
)
@click.option("--only", "only", default=None, metavar="A,B", help="Deploy these agents only.")
@click.option(
    "--keep-going", "keep_going", is_flag=True, default=False, help="Go on after a failed wave."
)
@click.option(
    "--skip-check",
    "skip_check",
    is_flag=True,
    default=False,
    help="Skip `system check` before (static) and after (--live) the deploys.",
)
@click.option(
    "--dry-run",
    "dry_run",
    is_flag=True,
    default=False,
    help="Pass --dry-run to each deploy (helm template; nothing changes); no live check.",
)
def cmd_deploy(
    env: str,
    file: str | None,
    parallel: int | None,
    only: str | None,
    keep_going: bool,
    skip_check: bool,
    dry_run: bool,
) -> None:
    """Deploy every project (`graph-agents-cli deploy --env ENV`), callees first, a few at a time.

    \b
    1. `system check --env ENV` (the files only): an error stops here.
    2. The agents deploy in waves, each once the agents it calls are deployed
       (a cycle is broken in file order, with a warning), at most --parallel at
       once; a failed wave stops the run unless --keep-going.
    3. Each agent's build, image load and rollout times are printed.
    4. `system check --live --env ENV`.

    Outside dev every project must record its kube context
    (environments.<env>.context in its manifest): the kubeconfig's current
    context is never used there, and no deploy is asked to confirm one.
    """
    system = _load(file)
    system.select([env])
    spec = system.envs[env]
    if spec.kind == LOCAL:
        raise click.UsageError(f"{env} is a local environment (port_base): nothing to deploy")
    names = _parse_only(system, only)
    console = Console()
    unrecorded = [
        system.nodes[n].name for n in names if env != "dev" and system.nodes[n].context(env) is None
    ]
    if unrecorded:
        raise SystemFileError(
            f"{env}: no kube context recorded for {', '.join(unrecorded)} (environments.{env}."
            "context in each manifest). Outside dev the kubeconfig's current context is never "
            "used; nothing was deployed."
        )
    if not skip_check:
        findings = _checks.run(system, [env])
        errors = [f for f in findings if f.severity == _checks.ERROR]
        if findings:
            _print_findings(console, findings)
        if errors:
            console.print(
                f"system check found {len(errors)} error(s) for {env}: nothing was deployed "
                "(--skip-check deploys anyway).",
                style="red",
            )
            raise SystemExit(EXIT_FINDING)
    width = parallel or system.file.deploy.parallel
    console.print(
        f"Deploying {len(names)} agent(s) to {env}, at most {width} at once"
        + (" (dry run)" if dry_run else ""),
        highlight=False,
    )
    outcomes = _deploy.deploy(
        system,
        env,
        names,
        parallel=width,
        keep_going=keep_going,
        extra=["--dry-run"] if dry_run else [],
        report=lambda line: console.print(escape(line), highlight=False),
    )
    failed = [o for o in outcomes if not o.ok]
    missing = [n for n in names if n not in {o.agent for o in outcomes}]
    if failed or missing:
        console.print(
            f"{len(failed)} deploy(s) failed"
            + (f"; not deployed: {', '.join(missing)}" if missing else ""),
            style="red",
        )
        raise SystemExit(EXIT_FINDING)
    console.print(f"Deployed {len(outcomes)} agent(s) to {env}.", style="green")
    if skip_check or dry_run:
        return
    findings = _checks.run(system, [env], live=True)
    live = [f for f in findings if f.check in ("SC14", "SC15")]
    _print_findings(console, live)
    if any(f.severity == _checks.ERROR for f in live):
        raise SystemExit(EXIT_FINDING)
    console.print(f"system check --live --env {env}: the agents answer.", style="green")


__all__ = ["EXIT_INVALID", "system_group"]
