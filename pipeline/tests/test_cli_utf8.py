"""Regression test for the outer CLI crashing on non-ASCII output.

`contenido-bionico` (the outer process, not the agent subprocess it may
spawn) used to inherit whatever codepage the parent gave it. On Windows,
when stdout/stderr are piped or redirected (dashboard/watcher invocations),
that is a legacy codepage (cp1252), not UTF-8. A change could succeed end to
end and still get reported as FAILED because `print()`-ing a result summary
containing a non-ASCII character (an arrow "->" / U+2192, an emoji, an
accented letter) raised `UnicodeEncodeError` and the process exited 1.

`main()` must reconfigure `sys.stdout`/`sys.stderr` to UTF-8 (errors=
"replace") as its very first action, before any subcommand dispatch, so
this can never happen regardless of how the process was invoked.
"""
from __future__ import annotations

import sys

import contenido_bionico.cli as cli


class _RecordingStream:
    """Stands in for sys.stdout/sys.stderr. Records whether/how
    `.reconfigure` was called, without touching the real console."""

    def __init__(self):
        self.reconfigure_calls: list[dict] = []

    def reconfigure(self, **kwargs):
        self.reconfigure_calls.append(kwargs)

    # main() prints help/status text through these before returning; keep
    # them harmless no-ops so the test doesn't depend on real stdout.
    def write(self, _text):
        pass

    def flush(self):
        pass


def test_main_reconfigures_stdout_and_stderr_to_utf8(monkeypatch):
    fake_out = _RecordingStream()
    fake_err = _RecordingStream()
    monkeypatch.setattr(sys, "stdout", fake_out)
    monkeypatch.setattr(sys, "stderr", fake_err)

    # Empty argv exercises main()'s top (where the reconfigure must happen)
    # and returns via parser.print_help() + return 2 -- no subprocess, no
    # SystemExit, no pipeline side effects.
    rc = cli.main([])

    assert rc == 2
    assert fake_out.reconfigure_calls == [{"encoding": "utf-8", "errors": "replace"}]
    assert fake_err.reconfigure_calls == [{"encoding": "utf-8", "errors": "replace"}]


def test_main_tolerates_stream_without_reconfigure(monkeypatch):
    """A stream that predates Python 3.7's TextIO.reconfigure (or has been
    swapped for something minimal, e.g. in another test's monkeypatch) must
    not crash main() -- the hasattr guard makes this a safe no-op."""

    class _NoReconfigureStream:
        def write(self, _text):
            pass

        def flush(self):
            pass

    monkeypatch.setattr(sys, "stdout", _NoReconfigureStream())
    monkeypatch.setattr(sys, "stderr", _NoReconfigureStream())

    rc = cli.main([])

    assert rc == 2


def test_main_does_not_crash_printing_non_ascii_summary(monkeypatch, capsys):
    """End-to-end regression: after main()'s reconfigure, printing a string
    containing a non-ASCII arrow (the exact character that crashed the real
    run: "2_short -> 9_short") must not raise UnicodeEncodeError, proving
    the fix actually neutralizes the failure mode described in the bug
    report -- not just that `.reconfigure` was called."""
    monkeypatch.setattr(sys, "argv", ["contenido-bionico"])

    rc = cli.main([])

    assert rc == 2
    # If stdout were still on a codepage that can't encode "->" (U+2192),
    # this print would raise UnicodeEncodeError and fail the test.
    print("[pipeline] changed: video (2_short → 9_short)")
    captured = capsys.readouterr()
    assert "→" in captured.out
