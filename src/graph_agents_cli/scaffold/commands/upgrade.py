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

"""Upgrade command for upgrading existing projects to newer graph-agents-cli builds.

The old snapshot of the three-way merge is rendered by the build that created
the project. Which build that is comes from the manifest: ``cli_version`` (a
release, fetched from its ``v<version>`` tag) and, since builds between two
releases share that version, ``cli_build`` (the build id, its commit and a
digest of what it renders; see ``scaffold.utils.build_record``). A project
whose manifest predates ``cli_build`` is compared by version only, and
``--baseline-ref`` names its build explicitly.
"""

import logging
import os
import pathlib
import tempfile

import click
from rich.markup import escape

from graph_agents_cli import _api_policy, _tools
from graph_agents_cli._output import Console
from graph_agents_cli._project import (
    MANIFEST_FILENAME,
    ManifestError,
    NotInProjectError,
    ProjectConfig,
    find_project_config,
    find_project_root,
)

from ..utils.backup import make_backup_pre_apply_hook
from ..utils.build_record import (
    MANIFEST_KEY,
    BuildRecord,
    MalformedRecordError,
    parse_build_record,
    read_manifest_data,
    recorded_build_for,
    running_build,
    template_digest,
    write_build_record,
)
from ..utils.generation_metadata import metadata_to_cli_args
from ..utils.merge import is_fetchable_version, run_create_command, run_three_way_merge
from ..utils.upgrade import update_cli_metadata
from ..utils.version import (
    INSTALL_SPEC_ENV,
    PACKAGE_NAME,
    REPO_URL,
    BaselineSource,
    InstallSpecError,
    commit_install_spec,
    get_current_version,
    pinned_install_spec,
    pinned_spec_unavailable,
    resolve_baseline_ref,
)

console = Console()

# The CLI-wide scheme: 0 ok, 1 refused or failed gate, 2 tool failure (uvx
# missing, or it could not fetch and run the prior build), 3 configuration
# error (the manifest's cli_version or cli_build, the install-spec override,
# a --baseline-ref that names no build or another version).
EXIT_TOOL_FAILURE = 2
_BASELINE_CURRENT_HINT = (
    "Nothing was changed. `--baseline current` compares against the current templates "
    "instead, but it cannot tell your edits from the template's own changes since then."
)
_BASELINE_REF_FORMS = (
    f"a commit or tag of {REPO_URL}, <clone>@<commit> for a local clone, a path to a "
    "checkout, or a full install spec (git+https://<mirror>@<commit>)"
)


class BaselineError(click.ClickException):
    """The build that created the project cannot be named or built as asked (exit 3)."""

    exit_code = 3


def _display_version_header(old_label: str, new_label: str, baseline_label: str | None) -> None:
    """Display the upgrade version header."""
    console.print()
    console.print(f"[bold blue]📦 Upgrading {escape(old_label)} → {escape(new_label)}[/bold blue]")
    if baseline_label:
        console.print(f"[dim]Baseline: {escape(baseline_label)}[/dim]")
    console.print()


def _build_label(version: str, build_id: str | None) -> str:
    return f"{version} (build {build_id})" if build_id and build_id != version else version


def _snapshot_digest(cli_args: list[str], project_name: str) -> str:
    """The digest of what this build renders from the manifest's settings."""
    with tempfile.TemporaryDirectory(prefix="gacli_upgrade_check_") as tmp:
        out = pathlib.Path(tmp)
        if not run_create_command(cli_args, out, project_name):
            console.print(
                "[bold red]Error:[/bold red] Could not render this CLI's templates to compare "
                "with the project's build; nothing was changed (rerun with --debug)."
            )
            raise SystemExit(EXIT_TOOL_FAILURE)
        return template_digest(out / project_name)


def _print_unrecorded_build(metadata: ProjectConfig, version: str, current_id: str) -> None:
    """The same-version answer for a manifest without ``cli_build``: compared by version only."""
    console.print(f"[bold green]✅[/bold green] Project is already at version {version}.")
    console.print(
        f"[dim]   Compared by version only: {MANIFEST_FILENAME} records no build "
        f"({MANIFEST_KEY}), so the project may come from an earlier {version} build whose "
        f"templates differ from this one ({current_id}). To upgrade from the build that "
        "created it, name that build:[/dim]"
    )
    console.print(
        "[dim]     graph-agents-cli scaffold upgrade --baseline-ref <ref> --dry-run[/dim]"
    )
    console.print(f"[dim]   <ref>: {_BASELINE_REF_FORMS}.[/dim]")
    if metadata.generated_at:
        console.print(
            "[dim]   Not sure which commit? A first candidate is the newest one before the "
            f"project was generated (generated_at {metadata.generated_at}):[/dim]"
        )
        console.print(
            f"[dim]     git -C <clone> log -1 --format=%H --before='{metadata.generated_at}'[/dim]"
        )
        console.print(
            "[dim]   The build may be older than that (a checkout behind its branch, or a "
            "cached build). Check with --dry-run: with the right build, only files you "
            "edited are listed under 'Will preserve' or as conflicts.[/dim]"
        )


def _recorded_build_source(
    recorded: BuildRecord | None, *, same_version: bool
) -> BaselineSource | None:
    """The baseline the recorded build implies; None means the ``cli_version`` release.

    A release build and a build of unknown source (no commit) are rendered from
    the version's release, except that an unknown build of the running version
    cannot be told from it and is refused. Any other build is its commit in the
    default repository; one with uncommitted changes has no commit that
    reproduces it, and an install-spec override (a mirror) can only name
    releases: both are refused with the ways out.
    """
    if recorded is None or recorded.is_release:
        return None
    if recorded.commit is None:
        if not same_version:
            return None
        raise BaselineError(
            f"The project was rendered by build {recorded.id} of unknown source (no commit "
            f"recorded) and its files differ from what this build renders. Name that build "
            f"with --baseline-ref: {_BASELINE_REF_FORMS}. Nothing was changed."
        )
    if recorded.dirty:
        raise BaselineError(
            f"The project was rendered by build {recorded.id}, which had uncommitted changes: "
            "no commit reproduces its templates, so an authentic baseline cannot be built. "
            f"Name the closest build with --baseline-ref (the commit it was built on is "
            f"{recorded.commit[:12]}). Nothing was changed."
        )
    if os.environ.get(INSTALL_SPEC_ENV, "").strip():
        raise BaselineError(
            f"The project was rendered by build {recorded.id} (commit {recorded.commit[:12]}), "
            f"a build between releases, and {INSTALL_SPEC_ENV} is set: its {{version}} names "
            "releases only, so it cannot fetch that commit. Name the build with "
            f"--baseline-ref: git+https://<mirror>@{recorded.commit}, "
            f"<clone>@{recorded.commit[:12]} or a checkout at that commit. Nothing was changed."
        )
    return BaselineSource(
        spec=commit_install_spec(recorded.commit),
        label=f"build {recorded.id}, commit {recorded.commit[:12]} of {REPO_URL}",
        commit=recorded.commit,
    )


def _release_source(version: str) -> BaselineSource:
    """The ``v<version>`` release as an explicit source (for a same-version upgrade)."""
    if not is_fetchable_version(version):
        raise ManifestError(
            f"cli_version {version!r} in {MANIFEST_FILENAME} is not a released "
            f"{PACKAGE_NAME} version, so its templates cannot be rebuilt.\n"
            f"  {_BASELINE_CURRENT_HINT}"
        )
    spec = pinned_install_spec(version)
    if spec is None:
        raise InstallSpecError(f"{pinned_spec_unavailable(version)}.\n  {_BASELINE_CURRENT_HINT}")
    return BaselineSource(spec=spec, label=f"the {version} release ({spec})")


@click.command()
@click.argument(
    "project_path",
    type=click.Path(exists=True, path_type=pathlib.Path),
    default=".",
    required=False,
)
@click.option(
    "--dry-run",
    "--dryrun",
    is_flag=True,
    help="Preview changes without applying them",
)
@click.option(
    "--auto-approve",
    "--yes",
    "-y",
    is_flag=True,
    help="Auto-apply non-conflicting changes without prompts",
)
@click.option(
    "--interactive",
    "-i",
    is_flag=True,
    default=False,
    help="Enable interactive prompts for human use",
)
@click.option(
    "--baseline",
    type=click.Choice(["authentic", "current"]),
    default="authentic",
    show_default=True,
    help=(
        "Which templates render the old snapshot: 'authentic' runs the exact build that "
        "created the project through uvx and stops if it cannot; 'current' is an explicit "
        "opt-in to compare against the current templates instead"
    ),
)
@click.option(
    "--baseline-ref",
    metavar="REF",
    default=None,
    help=(
        "The build that created the project, when the manifest cannot name it (a project "
        "from a build between releases, or a missing release tag): a commit or tag of the "
        "repository, <clone>@<commit> for a local clone, a path to a checkout or wheel, "
        "or a full install spec. Also upgrades a project of this same version."
    ),
)
@click.option(
    "--debug",
    is_flag=True,
    help="Enable debug logging",
)
def upgrade(
    project_path: pathlib.Path,
    dry_run: bool,
    auto_approve: bool,
    interactive: bool,
    baseline: str,
    baseline_ref: str | None,
    debug: bool,
) -> None:
    """Upgrade project to a newer graph-agents-cli version.

    Applies a 3-way merge between the old template, the new template, and your
    project: unmodified files are auto-updated, your customizations are preserved,
    and conflicts are surfaced for manual resolution (with --interactive) or kept as-is.

    The old template is regenerated by the exact CLI build that scaffolded the
    project: its release, or the commit the manifest records for a build between
    releases (--baseline-ref names it when the manifest cannot). If that build
    cannot be fetched and run, the upgrade stops with no changes; pass --baseline
    current to compare against the current templates instead (the summary is
    labelled accordingly).
    """
    if debug:
        logging.basicConfig(level=logging.DEBUG, force=True)
        console.print("[dim]Debug mode enabled[/dim]")
    if baseline_ref is not None and baseline == "current":
        raise click.UsageError(
            "--baseline-ref names the build to compare against; it cannot be combined "
            "with --baseline current."
        )

    project_dir = project_path.resolve()
    project_root_dir = find_project_root(project_dir)
    if project_root_dir is not None:
        project_dir = project_root_dir
        console.print(f"[dim]Resolved project root to: {project_dir}[/dim]")

    metadata = find_project_config(project_dir)
    if not metadata:
        # A configuration error (exit 3), like every other command outside a project.
        raise NotInProjectError(
            f"No {MANIFEST_FILENAME} found in {project_dir} or its parents.\n"
            "  Run scaffold upgrade from a project graph-agents-cli created, or pass its path."
        )

    # The retired product API policy must be migrated first: an upgrade would
    # otherwise re-render the project without it.
    _api_policy.ensure_no_legacy_api_policy(project_dir)

    language = metadata.language

    old_version = metadata.cli_version
    if not old_version:
        raise ManifestError(
            f"No cli_version found in {MANIFEST_FILENAME}.\n"
            "  It records the graph-agents-cli version that created the project, which "
            "upgrade needs to rebuild that version's templates; set it (for example "
            "cli_version: '0.1.0')."
        )

    new_version = get_current_version()
    current = running_build()
    try:
        recorded = recorded_build_for(metadata)
    except MalformedRecordError as e:
        console.print(
            f"[yellow]⚠️  {escape(str(e))} in {MANIFEST_FILENAME}; it is ignored.[/yellow]"
        )
        recorded = None

    project_name = metadata.project_name or project_dir.name
    agent_directory = metadata.agent_directory or "app"
    # The new snapshot is rendered by this CLI from the manifest's settings.
    cli_args = metadata_to_cli_args(metadata)

    source: BaselineSource | None = None
    if baseline_ref is not None:
        source = resolve_baseline_ref(baseline_ref)
    elif old_version == new_version:
        if baseline == "current":
            raise click.UsageError(
                "--baseline current renders the old snapshot with this CLI's own templates, "
                f"so for a project at this version ({new_version}) it can change nothing. Name "
                "the build that created the project with --baseline-ref instead."
            )
        if recorded is None:
            _print_unrecorded_build(metadata, new_version, current.id)
            return
        if recorded.same_build_as(current):
            console.print(
                f"[bold green]✅[/bold green] Project is already at version {new_version} "
                f"(build {current.id})"
            )
            return
        new_digest = _snapshot_digest(cli_args, project_name)
        if recorded.template_digest is not None and recorded.template_digest == new_digest:
            console.print(
                f"[bold green]✅[/bold green] Project is already at version {new_version}: "
                f"build {recorded.id} renders the same files for its settings as this build "
                f"({current.id})"
            )
            return
        source = _recorded_build_source(recorded, same_version=True) or _release_source(old_version)
    elif baseline == "authentic":
        source = _recorded_build_source(recorded, same_version=False)

    if baseline == "authentic":
        if source is None:
            # The cli_version release. Checked up front: without a release to
            # fetch, a usable install spec or uvx the authentic baseline cannot
            # be built, and the merge would stop anyway, after generating nothing.
            if not is_fetchable_version(old_version):
                raise ManifestError(
                    f"cli_version {old_version!r} in {MANIFEST_FILENAME} is not a released "
                    f"{PACKAGE_NAME} version, so its templates cannot be rebuilt.\n"
                    f"  {_BASELINE_CURRENT_HINT}"
                )
            if pinned_install_spec(old_version) is None:
                raise InstallSpecError(
                    f"{pinned_spec_unavailable(old_version)}.\n  {_BASELINE_CURRENT_HINT}"
                )
        try:
            _tools.require_tool("uvx")
        except _tools.ToolNotFoundError as e:
            console.print(f"[bold red]Error:[/bold red] {e.message}")
            console.print(
                f"[dim]The authentic {old_version} baseline needs uvx. Install uv, or re-run "
                "with --baseline current to compare against the current templates.[/dim]"
            )
            raise SystemExit(EXIT_TOOL_FAILURE) from e
    else:
        logging.warning(
            "--baseline current: the %s snapshot will be rendered with the current "
            "templates, not by %s@%s.",
            old_version,
            PACKAGE_NAME,
            old_version,
        )
        console.print(
            "[yellow]⚠️  --baseline current: comparing against the current templates instead "
            f"of {PACKAGE_NAME}@{old_version}. Deletions and updates between the two versions "
            "may be misclassified; review the result.[/yellow]"
        )

    baseline_label = source.label if source is not None else None
    if baseline == "authentic" and source is None:
        baseline_label = f"the {old_version} release"
    _display_version_header(
        _build_label(old_version, recorded.id if recorded else None),
        _build_label(new_version, current.id),
        baseline_label if baseline == "authentic" else None,
    )

    # The old snapshot is rendered by the CLI that scaffolded the project, which
    # may spell some values differently; the new one by this CLI.
    old_args = metadata_to_cli_args(
        metadata, cli_version=old_version if baseline == "authentic" else None
    )

    backup_hook = make_backup_pre_apply_hook(
        console=console,
        auto_approve=auto_approve,
        interactive=interactive,
    )

    digests: dict[str, str] = {}

    def _check_snapshots(old_dir: pathlib.Path, new_dir: pathlib.Path) -> None:
        """Refuse a baseline that is not the project's build; keep the new digest."""
        digests["new"] = template_digest(new_dir)
        if baseline != "authentic":
            return
        rendered = read_manifest_data(old_dir)
        rendered_version = str(rendered.get("cli_version") or "")
        if rendered_version and rendered_version != old_version:
            raise BaselineError(
                f"The baseline ({baseline_label}) is {PACKAGE_NAME} {rendered_version}, but "
                f"{MANIFEST_FILENAME} records {old_version}: it is not the build that created "
                "this project. Nothing was changed."
            )
        try:
            rendered_record = parse_build_record(rendered.get(MANIFEST_KEY))
        except MalformedRecordError:
            rendered_record = None
        if (
            recorded is not None
            and recorded.commit is not None
            and rendered_record is not None
            and rendered_record.commit is not None
            and rendered_record.commit != recorded.commit
        ):
            message = (
                f"The baseline ({baseline_label}) is build {rendered_record.id}, but the "
                f"project records build {recorded.id}."
            )
            if baseline_ref is None:
                raise BaselineError(
                    f"{message} Nothing was changed; name the project's build with --baseline-ref."
                )
            console.print(
                f"[yellow]⚠️  {escape(message)} Using it, as --baseline-ref asks.[/yellow]"
            )
        if baseline_ref is not None and template_digest(old_dir) == digests["new"]:
            console.print(
                f"[yellow]⚠️  The baseline ({escape(baseline_label or '')}) renders the same "
                "files as this "
                f"build ({current.id}) for this project's settings, so nothing can be upgraded "
                "from it. If it is not the build that created the project, name that build "
                "instead.[/yellow]"
            )

    def _record_new_build(proj_dir: pathlib.Path, lang: str) -> None:
        # The manifest is rewritten (without its comments) only when the version changes.
        if old_version != new_version:
            update_cli_metadata(proj_dir, {}, cli_version=new_version)
        write_build_record(proj_dir, BuildRecord.of(current, digests.get("new")))

    success = run_three_way_merge(
        project_dir=project_dir,
        project_name=project_name,
        agent_directory=agent_directory,
        language=language,
        old_args=old_args,
        new_args=cli_args,
        old_version=old_version,
        baseline=baseline,  # type: ignore[arg-type]
        old_source=source if baseline == "authentic" else None,
        auto_approve=auto_approve,
        dry_run=dry_run,
        interactive=interactive,
        operation_label="upgrade",
        pre_apply_hook=backup_hook,
        post_apply_hook=_record_new_build,
        snapshot_check=_check_snapshots,
    )

    if not success:
        what = baseline_label or f"{PACKAGE_NAME}@{old_version}"
        console.print(
            f"[bold red]Error:[/bold red] Could not build the baseline, {what} (the build "
            "that scaffolded this project), so the upgrade diff cannot be computed "
            "authentically."
        )
        if source is None:
            where = (
                f"that the v{old_version} tag exists, and retry; {INSTALL_SPEC_ENV} with "
                "{version} can point at a mirror or a local clone that has it, and "
                "--baseline-ref names the build directly (a clone: <clone>@<commit>)"
            )
        else:
            where = (
                "that the ref exists where it is looked up, and retry; a commit that is not "
                "on the remote is named with --baseline-ref <clone>@<commit> (a local clone "
                "that has it) or a path to a checkout at that commit"
            )
        console.print(
            f"[dim]Your project was not modified. Check your network/proxy and {where}. "
            "--baseline current compares against the current templates instead, but cannot "
            f"tell your edits from the template's changes since {old_version}: files you did "
            f"not edit keep their {old_version} content.[/dim]"
        )
        raise SystemExit(EXIT_TOOL_FAILURE)
