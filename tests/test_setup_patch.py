"""Tests for the managed `hermes setup` menu patch.

Nothing here touches the real hermes_cli/setup.py: every test points
``setup_py_path`` at a synthetic copy and ``PLUGIN_DIR`` at tmp_path.
"""

from __future__ import annotations

import ast

import pytest

from novita_setup_patch import (
    BEGIN,
    END,
    HANDLER_ANCHOR,
    MENU_ANCHOR,
    MENU_BLOCK,
    HANDLER_BLOCK,
    apply,
    detect,
    is_patched,
    revert,
    status_line,
)

# A faithful stand-in for the real shape of setup_terminal_backend().
SYNTHETIC = (
    "def setup_terminal_backend(config: dict):\n"
    "    terminal_choices = [\n"
    '        "Local - run directly on this machine (default)",\n'
    "    ]\n"
    '    idx_to_backend = {0: "local"}\n'
    '    backend_to_idx = {"local": 0}\n'
    "\n"
    "    next_idx = 5\n"
    + MENU_ANCHOR
    + "    keep_current_idx = next_idx\n"
    '    terminal_choices.append("Keep current")\n'
    "\n"
    '    terminal_idx = prompt_choice("Select terminal backend:", terminal_choices,'
    " keep_current_idx)\n"
    "\n"
    "    selected_backend = idx_to_backend.get(terminal_idx)\n"
    "    if terminal_idx == keep_current_idx:\n"
    "        return\n"
    '    config.setdefault("terminal", {})["backend"] = selected_backend\n'
    "\n"
    '    if selected_backend == "local":\n'
    '        print_success("local")\n'
    + HANDLER_ANCHOR
    + '        print_success("Terminal backend: SSH")\n'
    "\n"
    "    return None\n"
)


@pytest.fixture
def patched_env(tmp_path, monkeypatch):
    """Point the module at a temp copy of setup.py and a temp plugin dir."""
    import novita_setup_patch as sp

    target = tmp_path / "setup.py"
    target.write_text(SYNTHETIC)
    monkeypatch.setattr(sp, "setup_py_path", lambda: target)
    monkeypatch.setattr(sp, "PLUGIN_DIR", tmp_path)
    return target


# ----------------------------------------------------------------------
# emitted code must be valid Python
# ----------------------------------------------------------------------


def test_emitted_blocks_parse_in_context():
    ast.parse(
        "def f():\n"
        "    next_idx = 5\n"
        "    terminal_choices = []\n"
        "    idx_to_backend = {}\n"
        "    backend_to_idx = {}\n" + MENU_BLOCK
    )
    ast.parse("def f(config):\n    if True:\n        pass\n" + HANDLER_BLOCK)


def test_handler_block_preserves_literal_braces():
    """The inserted code's own {} must survive, not be eaten by an f-string."""
    assert 'setdefault("plugins", {})' in HANDLER_BLOCK
    assert "novita-sandbox" in HANDLER_BLOCK
    assert "@@KEY@@" not in HANDLER_BLOCK
    assert HANDLER_BLOCK.startswith(BEGIN)
    assert END in HANDLER_BLOCK


def test_menu_block_is_guarded_for_idempotency():
    assert '"novita" not in backend_to_idx' in MENU_BLOCK


# ----------------------------------------------------------------------
# detect
# ----------------------------------------------------------------------


def test_detect_unpatched(patched_env):
    assert detect()["state"] == "unpatched"
    assert "never been applied" in detect()["detail"]


def test_detect_reports_missing_anchors(tmp_path, monkeypatch):
    import novita_setup_patch as sp

    target = tmp_path / "setup.py"
    target.write_text("def setup_terminal_backend(config):\n    pass\n")
    monkeypatch.setattr(sp, "setup_py_path", lambda: target)
    monkeypatch.setattr(sp, "PLUGIN_DIR", tmp_path)

    info = detect()

    assert info["state"] == "shape_unknown"
    assert "menu anchor" in info["detail"]


def test_detect_reports_ambiguous_anchor(tmp_path, monkeypatch):
    import novita_setup_patch as sp

    target = tmp_path / "setup.py"
    target.write_text(SYNTHETIC + SYNTHETIC)  # every anchor appears twice
    monkeypatch.setattr(sp, "setup_py_path", lambda: target)
    monkeypatch.setattr(sp, "PLUGIN_DIR", tmp_path)

    assert detect()["state"] == "shape_unknown"


def test_detect_without_setup_module(monkeypatch):
    import novita_setup_patch as sp

    monkeypatch.setattr(sp, "setup_py_path", lambda: None)
    assert detect()["state"] == "no_setup_module"


# ----------------------------------------------------------------------
# apply
# ----------------------------------------------------------------------


def test_apply_inserts_both_blocks(patched_env):
    ok, message = apply()

    assert ok, message
    text = patched_env.read_text()
    assert is_patched(text)
    assert "Novita - Novita Agent Sandbox (cloud)" in text
    assert 'elif selected_backend == "novita":' in text
    ast.parse(text)


def test_apply_is_idempotent(patched_env):
    apply()
    first = patched_env.read_text()

    ok, message = apply()

    assert ok
    assert "already patched" in message
    assert patched_env.read_text() == first


def test_apply_adds_menu_row_before_keep_current(patched_env):
    apply()
    text = patched_env.read_text()

    assert text.index("Novita Agent Sandbox") < text.index("keep_current_idx = next_idx")


def test_apply_inserts_handler_before_ssh_branch(patched_env):
    apply()
    text = patched_env.read_text()

    assert text.index('"novita"') < text.index("Terminal backend: SSH")


def test_apply_writes_a_backup_once(patched_env):
    apply()
    backup = patched_env.with_suffix(patched_env.suffix + ".novita-orig")

    assert backup.is_file()
    assert backup.read_text() == SYNTHETIC

    apply()  # second call must not clobber the original backup
    assert backup.read_text() == SYNTHETIC


def test_apply_records_state(patched_env):
    apply()

    assert detect()["state"] == "patched"
    state = (patched_env.parent / ".setup-patch.json").read_text()
    assert '"applied": true' in state


def test_apply_refuses_when_shape_changed(tmp_path, monkeypatch):
    import novita_setup_patch as sp

    target = tmp_path / "setup.py"
    original = "def setup_terminal_backend(config):\n    pass\n"
    target.write_text(original)
    monkeypatch.setattr(sp, "setup_py_path", lambda: target)
    monkeypatch.setattr(sp, "PLUGIN_DIR", tmp_path)

    ok, message = apply()

    assert not ok
    assert "refusing to patch" in message
    assert target.read_text() == original, "a refused patch must not touch the file"


def test_apply_never_writes_unparseable_code(patched_env, monkeypatch):
    """If the anchors sit somewhere that would break syntax, refuse."""
    import novita_setup_patch as sp

    # Anchor present exactly once, but at top level (0 indent) would break the
    # insertion; emulate by making the anchor dedented.
    broken = SYNTHETIC.replace(MENU_ANCHOR, MENU_ANCHOR.lstrip()).replace(
        HANDLER_ANCHOR, HANDLER_ANCHOR.lstrip()
    )
    patched_env.write_text(broken)
    ok, message = apply()

    # Either it refused, or it produced something that parses.
    if ok:
        ast.parse(patched_env.read_text())
    else:
        assert "refusing" in message or "parse" in message


def test_apply_survives_unwritable_target(patched_env, monkeypatch):
    import novita_setup_patch as sp

    monkeypatch.setattr(sp, "setup_py_path", lambda: patched_env)
    monkeypatch.setattr(
        "pathlib.Path.write_text", lambda *a, **k: (_ for _ in ()).throw(OSError("read-only"))
    )

    ok, message = apply()

    assert not ok
    assert "cannot write" in message


# ----------------------------------------------------------------------
# revert
# ----------------------------------------------------------------------


def test_revert_restores_original_shape(patched_env):
    apply()

    ok, message = revert()

    assert ok, message
    text = patched_env.read_text()
    assert not is_patched(text)
    assert "Novita - Novita Agent Sandbox" not in text
    ast.parse(text)
    assert text == SYNTHETIC, "revert must restore the file byte for byte"


def test_revert_when_not_patched(patched_env):
    ok, message = revert()

    assert ok
    assert "nothing to revert" in message
    assert patched_env.read_text() == SYNTHETIC


def test_revert_clears_state(patched_env):
    apply()
    revert()

    assert detect()["state"] == "unpatched"


def test_apply_revert_apply_round_trip(patched_env):
    apply()
    revert()
    ok, _ = apply()

    assert ok
    assert is_patched(patched_env.read_text())


# ----------------------------------------------------------------------
# revert detection (the update scenario)
# ----------------------------------------------------------------------


def test_detect_reports_reverted_after_external_restore(patched_env):
    """`hermes update` runs git reset --hard; the markers vanish."""
    apply()
    assert detect()["state"] == "patched"

    patched_env.write_text(SYNTHETIC)  # simulate git reset --hard

    info = detect()
    assert info["state"] == "reverted"
    assert "update" in info["detail"]
    assert "reverted" in status_line()


@pytest.mark.parametrize(
    "state,expected",
    [
        ("patched", "[ok]"),
        ("unpatched", "[skip]"),
        ("reverted", "[FAIL]"),
        ("shape_unknown", "[FAIL]"),
        ("no_setup_module", "[FAIL]"),
    ],
)
def test_status_line_covers_every_state(patched_env, monkeypatch, state, expected):
    monkeypatch.setattr(
        "novita_setup_patch.detect", lambda: {"state": state, "detail": "d"}
    )
    assert status_line().startswith(expected)
