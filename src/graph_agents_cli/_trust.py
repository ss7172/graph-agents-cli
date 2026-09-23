# Copyright 2026 Google LLC
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

"""Trust tier decorators for agents CLI commands."""

import functools

import click


def require_confirmation(message: str):
    """Decorator that adds --yes/-y and --interactive/-i options to destructive commands.

    In interactive mode (-i), the user is prompted to confirm the action.
    In auto-approve mode (-y) or strict programmatic mode (no flags), proceeds silently.
    The decorated function receives an 'auto_approve' parameter.

    Args:
        message: Confirmation prompt shown to the user.
    """

    def decorator(f):
        @click.option(
            "--interactive",
            "-i",
            is_flag=True,
            default=False,
            help="Enable interactive confirmation prompt.",
        )
        @click.option(
            "-y",
            "--yes",
            "--auto-approve",
            "auto_approve",
            is_flag=True,
            default=False,
            help="Skip confirmation prompt.",
        )
        @functools.wraps(f)
        def wrapper(*args, interactive=False, auto_approve=False, **kwargs):
            if interactive and not auto_approve:
                click.echo()
                if not click.confirm(f"  {message}", default=False):
                    click.echo()
                    click.secho("  Aborted.", fg="yellow")
                    return
                click.echo()
            # In strict programmatic mode (no -i, no -y): auto-proceed silently.
            # `interactive` is not passed on: the prompt happens here, and no
            # decorated command has ever read it.
            return f(*args, auto_approve=auto_approve, **kwargs)

        return wrapper

    return decorator
