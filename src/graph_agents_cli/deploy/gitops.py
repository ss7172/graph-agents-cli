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
"""argocd-mode desired-state writer: values tag rewrite, branch, pull request.

``deploy`` never runs helm in argocd mode (Argo CD applies the change). It takes ``values-<env>.yaml``
as ``origin/main`` holds it (the pull request's base; ``HEAD`` when that ref
is unavailable), rewrites ``image.tag`` in that text and nothing else, commits
the one file on ``deploy/<env>/<tag>`` with git plumbing (the developer's
checkout, index and working tree are left alone), pushes, and opens or
updates a pull request through ``gh``, or the GitHub REST API with
``GITHUB_TOKEN`` when ``gh`` is not installed. Only
GitHub and GitHub Enterprise Server remotes are supported. Index paths are
relative to the repository root, so a project below the git root is handled.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from graph_agents_cli._output import Console
from graph_agents_cli.deploy import _kube
from graph_agents_cli.deploy._kube import ConfigError, Refused, ToolFailed, run_cmd
from graph_agents_cli.deploy._values import rewrite_image_tag

DEFAULT_BASE_BRANCH = "main"


@dataclass(frozen=True)
class Remote:
    host: str
    owner: str
    repo: str

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}"

    @property
    def gh_repo(self) -> str:
        return f"{self.host}/{self.owner}/{self.repo}"

    @property
    def api_base(self) -> str:
        if self.host == "github.com":
            return "https://api.github.com"
        return f"https://{self.host}/api/v3"


_REMOTE_RES = (
    re.compile(
        r"^(?:https?|ssh|git)://(?:[^@/]+@)?(?P<host>[^/:]+)(?::\d+)?/(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$"
    ),
    re.compile(r"^(?:[^@]+@)?(?P<host>[^:/]+):(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$"),
)


def parse_remote(url: str) -> Remote:
    url = url.strip()
    for pattern in _REMOTE_RES:
        m = pattern.match(url)
        if m:
            return Remote(m.group("host").lower(), m.group("owner"), m.group("repo"))
    raise Refused(f"Could not parse the git remote URL {url!r}.")


def enterprise_hosts() -> set[str]:
    """Hosts the operator declared as GitHub Enterprise Server (GH_HOST / GITHUB_SERVER_URL)."""
    hosts: set[str] = set()
    for var in ("GH_HOST", "GITHUB_HOST"):
        value = os.environ.get(var, "").strip().lower()
        if value:
            hosts.add(value)
    server = os.environ.get("GITHUB_SERVER_URL", "").strip().lower()
    if server:
        hosts.add(re.sub(r"^https?://", "", server).split("/")[0])
    return hosts


def ensure_github(remote: Remote) -> None:
    if remote.host == "github.com" or remote.host in enterprise_hosts():
        return
    raise Refused(
        f"The origin remote is on {remote.host!r}, which is not GitHub.\n"
        "  argocd mode opens pull requests only on GitHub or GitHub Enterprise Server in v1.\n"
        "  For a GitHub Enterprise Server, set GH_HOST=<host> (and GH_ENTERPRISE_TOKEN or GITHUB_TOKEN)."
    )


def origin_url() -> str:
    result = run_cmd(["git", "remote", "get-url", "origin"], check=False, quiet=True)
    url = (result.stdout or "").strip()
    if result.returncode != 0 or not url:
        raise ConfigError("This project has no git remote named 'origin'; argocd mode needs one.")
    return url


def short_sha() -> str | None:
    try:
        result = run_cmd(["git", "rev-parse", "--short", "HEAD"], check=False, quiet=True)
    except ToolFailed:
        return None
    if result.returncode != 0:
        return None
    return (result.stdout or "").strip() or None


def branch_name(env: str, tag: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", tag).strip("-.") or "image"
    return f"deploy/{env}/{safe}"


def github_token() -> str | None:
    for var in ("GITHUB_TOKEN", "GH_TOKEN", "GH_ENTERPRISE_TOKEN"):
        value = os.environ.get(var)
        if value:
            return value
    return None


@dataclass
class PullRequestResult:
    branch: str
    url: str | None
    created: bool
    changed: bool


def repo_relative(path: Path) -> str:
    """``path`` (project-relative, cwd = project root) as git index and tree paths need it.

    ``git rev-parse --show-prefix`` is empty at the repository toplevel and
    ``apps/agent/`` for a project below it; ``update-index --cacheinfo`` and
    ``<ref>:<path>`` take that repo-relative form, unlike ``hash-object``.
    """
    result = run_cmd(["git", "rev-parse", "--show-prefix"], check=False, quiet=True)
    if result.returncode != 0:
        raise ConfigError("This project is not inside a git repository; argocd mode needs one.")
    prefix = (result.stdout or "").strip()
    if path.is_absolute():
        try:
            path = path.resolve().relative_to(Path.cwd().resolve())
        except ValueError as e:
            raise ConfigError(f"{path} is outside the project directory {Path.cwd()}.") from e
    rel = path.as_posix()
    if rel == ".." or rel.startswith("../") or "/../" in rel:
        raise ConfigError(f"{path} is outside the project directory; argocd mode cannot commit it.")
    return f"{prefix}{rel}"


def _resolve_base(base: str, *, dry_run: bool, console: Console) -> str:
    """Fetch ``origin/<base>`` and return the ref to branch from (``HEAD`` when unavailable)."""
    run_cmd(["git", "fetch", "origin", base], check=False, dry_run=dry_run, console=console)
    base_ref = f"origin/{base}"
    probe = run_cmd(["git", "rev-parse", "--verify", "--quiet", base_ref], check=False, quiet=True)
    if probe.returncode != 0:
        if dry_run:
            console.print(
                f"  [dry-run] origin/{base} is not available locally; the real run would branch "
                "from HEAD.",
                style="yellow",
            )
        else:
            console.print(
                f"  origin/{base} is not available locally; branching from HEAD instead.",
                style="yellow",
            )
        base_ref = "HEAD"
    return base_ref


def _base_values_text(base_ref: str, index_path: str, values_path: Path, *, dry_run: bool) -> str:
    """The values file as ``base_ref`` holds it (the content the PR is built on)."""
    result = run_cmd(
        ["git", "cat-file", "blob", f"{base_ref}:{index_path}"], check=False, quiet=True
    )
    if result.returncode == 0 and (result.stdout or "").strip():
        return result.stdout
    if dry_run and values_path.is_file():
        return values_path.read_text(encoding="utf-8")
    raise ConfigError(
        f"{index_path} is not committed on {base_ref}; commit it there first.\n"
        "  argocd mode rewrites image.tag in the copy the pull request is based on, "
        "never the working tree."
    )


def _commit_single_file(
    *,
    index_path: str,
    content: str,
    branch: str,
    base_ref: str,
    message: str,
    dry_run: bool,
    console: Console,
) -> None:
    """Commit ``content`` at ``index_path`` on ``branch`` from ``base_ref`` (no checkout switch)."""
    blob = run_cmd(
        ["git", "hash-object", "-w", "--stdin"],
        input_text=content,
        dry_run=dry_run,
        console=console,
    ).stdout.strip()
    blob = blob or "<blob>"
    with tempfile.TemporaryDirectory(prefix="graph-agents-cli-index-") as tmp:
        env = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
        run_cmd(["git", "read-tree", base_ref], env=env, dry_run=dry_run, console=console)
        run_cmd(
            ["git", "update-index", "--add", "--cacheinfo", f"100644,{blob},{index_path}"],
            env=env,
            dry_run=dry_run,
            console=console,
        )
        tree = run_cmd(
            ["git", "write-tree"], env=env, dry_run=dry_run, console=console
        ).stdout.strip()
    tree = tree or "<tree>"
    commit = run_cmd(
        ["git", "commit-tree", tree, "-p", base_ref, "-m", message],
        dry_run=dry_run,
        console=console,
    ).stdout.strip()
    commit = commit or "<commit>"
    run_cmd(["git", "update-ref", f"refs/heads/{branch}", commit], dry_run=dry_run, console=console)
    run_cmd(
        ["git", "push", "--force-with-lease", "-u", "origin", f"{branch}:{branch}"],
        dry_run=dry_run,
        console=console,
        capture=False,
    )


def _pr_via_gh(
    *,
    remote: Remote,
    branch: str,
    base: str,
    title: str,
    body: str,
    dry_run: bool,
    console: Console,
) -> tuple[str | None, bool]:
    listing = run_cmd(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            remote.gh_repo,
            "--head",
            branch,
            "--base",
            base,
            "--state",
            "open",
            "--json",
            "number,url",
        ],
        dry_run=dry_run,
        console=console,
    )
    existing: list[dict] = []
    if listing.stdout.strip():
        try:
            existing = json.loads(listing.stdout)
        except json.JSONDecodeError:
            existing = []
    if existing:
        number = str(existing[0].get("number"))
        run_cmd(
            [
                "gh",
                "pr",
                "edit",
                number,
                "--repo",
                remote.gh_repo,
                "--title",
                title,
                "--body",
                body,
            ],
            dry_run=dry_run,
            console=console,
        )
        return existing[0].get("url"), False
    created = run_cmd(
        [
            "gh",
            "pr",
            "create",
            "--repo",
            remote.gh_repo,
            "--base",
            base,
            "--head",
            branch,
            "--title",
            title,
            "--body",
            body,
        ],
        dry_run=dry_run,
        console=console,
    )
    url = (created.stdout or "").strip().splitlines()
    return (url[-1] if url else None), True


def _pr_via_rest(
    *,
    remote: Remote,
    token: str,
    branch: str,
    base: str,
    title: str,
    body: str,
    dry_run: bool,
    console: Console,
) -> tuple[str | None, bool]:
    import httpx

    pulls = f"{remote.api_base}/repos/{remote.owner}/{remote.repo}/pulls"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    console.print(
        f"  {'[dry-run] ' if dry_run else ''}GET {pulls}?head={remote.owner}:{branch}&base={base}",
        style="cyan",
        markup=False,
    )
    if dry_run:
        console.print(
            f"  [dry-run] POST {pulls} (or PATCH the open pull request)", style="cyan", markup=False
        )
        return None, False
    with httpx.Client(headers=headers, timeout=30) as client:
        resp = client.get(
            pulls, params={"head": f"{remote.owner}:{branch}", "base": base, "state": "open"}
        )
        if resp.status_code != 200:
            raise ToolFailed(
                f"GitHub API GET {pulls} returned {resp.status_code}: {resp.text[:200]}"
            )
        existing = resp.json()
        if existing:
            number = existing[0]["number"]
            resp = client.patch(f"{pulls}/{number}", json={"title": title, "body": body})
            if resp.status_code not in (200, 201):
                raise ToolFailed(f"GitHub API PATCH {pulls}/{number} returned {resp.status_code}")
            return existing[0].get("html_url"), False
        resp = client.post(pulls, json={"title": title, "body": body, "head": branch, "base": base})
        if resp.status_code not in (200, 201):
            raise ToolFailed(
                f"GitHub API POST {pulls} returned {resp.status_code}: {resp.text[:200]}"
            )
        return resp.json().get("html_url"), True


def write_desired_state(
    *,
    env: str,
    values_path: Path,
    image_repository: str | None,
    tag: str,
    project_name: str,
    base: str = DEFAULT_BASE_BRANCH,
    dry_run: bool = False,
    console: Console | None = None,
) -> PullRequestResult:
    """Rewrite the tag on the base branch's copy, commit on ``deploy/<env>/<tag>``, push, open the PR.

    The content comes from ``origin/<base>`` (``HEAD`` when unavailable), so a
    checkout behind ``main`` or with uncommitted edits never leaks into the
    pull request, and "nothing to change" is judged against the base branch.
    The developer's working tree is not modified.
    """
    console = console or Console()
    remote = parse_remote(origin_url())
    ensure_github(remote)

    index_path = repo_relative(values_path)
    base_ref = _resolve_base(base, dry_run=dry_run, console=console)
    base_text = _base_values_text(base_ref, index_path, values_path, dry_run=dry_run)
    old, new_text, changed = rewrite_image_tag(base_text, tag, source=f"{base_ref}:{index_path}")
    if not changed:
        console.print(
            f"  {base_ref}:{index_path} already has image.tag {tag!r}; nothing to change.",
            markup=False,
        )
        return PullRequestResult(
            branch=branch_name(env, tag), url=None, created=False, changed=False
        )
    verb = "[dry-run] would set" if dry_run else "Setting"
    console.print(
        f"  {verb} image.tag {old!r} -> {tag!r} in {index_path} (from {base_ref}; "
        "the working tree is left unchanged).",
        style="cyan" if dry_run else None,
        markup=False,
    )

    branch = branch_name(env, tag)
    image = f"{image_repository}:{tag}" if image_repository else tag
    title = f"deploy({env}): {project_name} -> {tag}"
    body = (
        f"Set `image.tag` to `{tag}` in `{index_path}`.\n\n"
        f"Image: `{image}`\n\n"
        "Opened by `graph-agents-cli deploy`. Argo CD reconciles this environment from "
        f"`{base}` once the pull request merges."
    )
    _commit_single_file(
        index_path=index_path,
        content=new_text,
        branch=branch,
        base_ref=base_ref,
        message=title,
        dry_run=dry_run,
        console=console,
    )

    if _kube.tool_available("gh"):
        url, created = _pr_via_gh(
            remote=remote,
            branch=branch,
            base=base,
            title=title,
            body=body,
            dry_run=dry_run,
            console=console,
        )
    else:
        token = github_token()
        if not token:
            raise ConfigError(
                "Neither the gh CLI nor a GITHUB_TOKEN is available to open the pull request.\n"
                f"  The branch {branch} was pushed; install gh (https://cli.github.com/) or export "
                "GITHUB_TOKEN and re-run, or open the pull request by hand."
            )
        url, created = _pr_via_rest(
            remote=remote,
            token=token,
            branch=branch,
            base=base,
            title=title,
            body=body,
            dry_run=dry_run,
            console=console,
        )
    return PullRequestResult(branch=branch, url=url, created=created, changed=changed)
