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

"""Turn SIGTERM and SIGHUP into the same clean shutdown as Ctrl-C.

Python's default SIGTERM action ends the interpreter on the spot: no
``finally``, no ``except BaseException``. The commands that start servers
(``run``, ``eval generate``, ``playground``) rely on exactly that cleanup to stop
the server they started and remove its pid file, so an IDE stop button, a CI
job timeout or a plain ``kill`` used to leave a detached server behind. Inside
:func:`terminate_like_interrupt` those signals raise :class:`TerminationSignal`
(a ``KeyboardInterrupt``), so every existing Ctrl-C cleanup path runs, and the
CLI exits with ``128 + signal number`` (143 for SIGTERM).

The cleanup itself runs under :func:`shielded`: a signal that arrives while a
server is being stopped (the first one, during a normal teardown) is held until
the pid file is gone, then handled, so no stale record is left behind.
"""

from __future__ import annotations

import contextlib
import signal
import threading
from collections.abc import Iterator
from types import FrameType


class TerminationSignal(KeyboardInterrupt):
    """SIGTERM/SIGHUP received: unwinds like Ctrl-C, exits with ``128 + signum``."""

    def __init__(self, signum: int) -> None:
        super().__init__(signum)
        self.signum = signum

    @property
    def exit_code(self) -> int:
        return 128 + self.signum


def _signals() -> list[int]:
    return [
        sig for sig in (getattr(signal, "SIGTERM", None), getattr(signal, "SIGHUP", None)) if sig
    ]


def _raise(signum: int, _frame: FrameType | None) -> None:
    # The cleanup this starts (stopping a server, removing its pid file) must
    # not be cut short by a second signal: ignore them until the block exits.
    for sig in _signals():
        with contextlib.suppress(ValueError, OSError):
            signal.signal(sig, signal.SIG_IGN)
    raise TerminationSignal(signum)


def _deliver(signum: int, handler: object) -> None:
    """Act on a signal held by :func:`shielded` as the handler it was held from would."""
    if handler is signal.SIG_IGN:
        return
    if handler is None or handler is signal.SIG_DFL:
        # The default action (end the process), now that the cleanup is done.
        signal.raise_signal(signum)
        return
    if callable(handler):
        handler(signum, None)  # raises TerminationSignal / KeyboardInterrupt


@contextlib.contextmanager
def shielded() -> Iterator[None]:
    """Hold SIGINT, SIGTERM and SIGHUP until the block ends, then deliver the first one.

    For cleanup that must not stop halfway: stopping a server and removing its
    pid file. Unlike :func:`terminate_like_interrupt`, which turns the signal
    into an exception wherever the main thread is (inside ``psutil.wait_procs``
    of a teardown, say), a signal arriving here is only recorded; once the
    block is done the previous handlers are back and the signal is handled as
    they would have (a :class:`TerminationSignal`, a ``KeyboardInterrupt``, or
    the default action). A no-op outside the main thread.
    """
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    held: list[int] = []

    def _hold(signum: int, _frame: FrameType | None) -> None:
        if not held:
            held.append(signum)

    previous: dict[int, object] = {}
    for sig in (signal.SIGINT, *_signals()):
        try:
            previous[sig] = signal.signal(sig, _hold)
        except (ValueError, OSError):  # not supported on this platform
            continue
    try:
        yield
    finally:
        for sig, handler in previous.items():
            restore = signal.SIG_DFL if handler is None else handler
            with contextlib.suppress(ValueError, OSError, TypeError):
                signal.signal(sig, restore)  # type: ignore[arg-type]
        if held:
            _deliver(held[0], previous.get(held[0]))


@contextlib.contextmanager
def terminate_like_interrupt() -> Iterator[None]:
    """Within the block, SIGTERM and SIGHUP raise :class:`TerminationSignal`.

    A no-op outside the main thread (only it may install handlers). The
    previous handlers are restored on exit; once one signal has arrived, further
    ones are ignored so the cleanup it triggers runs to the end.
    """
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous: dict[int, object] = {}
    for sig in _signals():
        try:
            previous[sig] = signal.signal(sig, _raise)
        except (ValueError, OSError):  # not supported on this platform
            continue
    try:
        yield
    finally:
        for sig, handler in previous.items():
            # None: the previous handler was not installed from Python.
            restore = signal.SIG_DFL if handler is None else handler
            with contextlib.suppress(ValueError, OSError, TypeError):
                signal.signal(sig, restore)  # type: ignore[arg-type]
