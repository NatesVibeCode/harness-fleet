"""Terminal output: colour belongs to a human, not to a log file.

Every escape code in `harness_fleet.ui` used to be emitted unconditionally, so a
redirect, a pipe, or a captured subprocess got escape sequences in its output. A
board startup message captured to a file read as `\\033[31m✖ this database has no
runs yet\\033[0m`, which is what a person pastes into a bug report.
"""
from __future__ import annotations

import importlib
import io
import sys
from contextlib import redirect_stdout

import pytest

from harness_fleet import ui

ESCAPE = "\033"


def _reload_with(monkeypatch, **env):
    """Re-import the module under a given environment, since the decision is made
    at import time (that is the only moment `isatty` is meaningful)."""
    for key in ("NO_COLOR", "FORCE_COLOR", "TERM"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return importlib.reload(ui)


@pytest.fixture(autouse=True)
def _restore_module():
    """Leave the module as the suite found it: it is imported process-wide."""
    yield
    importlib.reload(ui)


def test_piped_output_carries_no_escape_codes():
    """The reported symptom. Under pytest, stdout is captured, so it is not a
    tty and colour must be off."""
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        ui.error("this database has no runs yet")
        ui.info("reading sources")
        ui.success("done")
    output = buffer.getvalue()
    assert ESCAPE not in output, f"escape codes leaked into captured output: {output!r}"
    assert "this database has no runs yet" in output


def test_no_color_disables_colour(monkeypatch):
    module = _reload_with(monkeypatch, NO_COLOR="1")
    assert module.colors_enabled() is False
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        module.error("plain please")
    assert ESCAPE not in buffer.getvalue()


def test_term_dumb_disables_colour(monkeypatch):
    module = _reload_with(monkeypatch, TERM="dumb")
    assert module.colors_enabled() is False


def test_force_color_overrides_and_survives_no_color(monkeypatch):
    """Someone piping on purpose must still be able to ask for colour."""
    module = _reload_with(monkeypatch, FORCE_COLOR="1")
    assert module.colors_enabled() is True
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        module.error("coloured")
    assert ESCAPE in buffer.getvalue()

    module = _reload_with(monkeypatch, FORCE_COLOR="1", NO_COLOR="1")
    assert module.colors_enabled() is True

    module = _reload_with(monkeypatch, FORCE_COLOR="0")
    assert module.colors_enabled() is False


def test_plain_context_manager_silences_colour_and_restores_it(monkeypatch):
    """For anything rendering output destined for a file."""
    module = _reload_with(monkeypatch, FORCE_COLOR="1")
    assert module.GREEN != ""

    with module.plain():
        assert module.GREEN == "" and module.RESET == ""
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            module.error("into a file")
        assert ESCAPE not in buffer.getvalue()

    assert module.GREEN != "", "colour must come back afterwards"
    assert module.RESET != ""


def test_plain_restores_even_when_the_body_raises(monkeypatch):
    module = _reload_with(monkeypatch, FORCE_COLOR="1")
    with pytest.raises(RuntimeError):
        with module.plain():
            raise RuntimeError("boom")
    assert module.GREEN != ""


def test_plain_disables_the_table_renderers_too(monkeypatch):
    """The tables build their own header strings from the same constants."""
    module = _reload_with(monkeypatch, FORCE_COLOR="1")
    buffer = io.StringIO()
    with module.plain(), redirect_stdout(buffer):
        module.print_sessions_table({"s-1": {"worker_idx": 1, "route_id": "r", "status": "completed"}})
        module.print_cooldowns_table([])
    assert ESCAPE not in buffer.getvalue()
    assert "SESSION ID" in buffer.getvalue()


def test_banner_is_plain_when_piped(monkeypatch):
    module = _reload_with(monkeypatch)
    module._apply(False)
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        module.banner()
    assert ESCAPE not in buffer.getvalue()
