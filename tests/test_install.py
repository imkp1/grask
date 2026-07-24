"""Tests for wiring grask into Claude Code.

The settings.json merge is the delicate part: it edits a file the developer did
not ask us to own, so the things that would corrupt it — a second install
doubling the hook, an edit that drops a foreign tool's hook, an uninstall that
removes more than grask's — are each pinned here rather than trusted.
"""

from __future__ import annotations

import json
from pathlib import Path

import grask.install as install_mod
from grask.install import (
    HOOK_COMMAND,
    _checks,
    doctor,
    hook_configured,
    install,
    merge_hook,
    remove_hook,
    runner_shim_text,
    uninstall,
    write_runner_shim,
)


def _grask_group() -> dict:
    return {"hooks": [{"type": "command", "command": HOOK_COMMAND}]}


def _foreign_group() -> dict:
    return {"hooks": [{"type": "command", "command": "some-other-tool"}]}


def test_merge_into_empty_settings_adds_one_hook():
    settings: dict = {}
    assert merge_hook(settings) is True
    assert settings["hooks"]["SessionEnd"] == [_grask_group()]


def test_merge_is_idempotent():
    settings: dict = {}
    merge_hook(settings)
    assert merge_hook(settings) is False, "a second install must not add a duplicate"
    commands = [
        h["command"]
        for group in settings["hooks"]["SessionEnd"]
        for h in group["hooks"]
    ]
    assert commands == [HOOK_COMMAND]


def test_merge_preserves_foreign_hooks():
    settings = {"hooks": {"SessionEnd": [_foreign_group()]}}
    assert merge_hook(settings) is True
    groups = settings["hooks"]["SessionEnd"]
    assert _foreign_group() in groups
    assert _grask_group() in groups


def test_remove_takes_only_grasks_hook():
    settings = {"hooks": {"SessionEnd": [_foreign_group(), _grask_group()]}}
    assert remove_hook(settings) is True
    assert settings["hooks"]["SessionEnd"] == [_foreign_group()]


def test_remove_prunes_empties_but_not_foreign_events():
    settings = {
        "hooks": {
            "SessionEnd": [_grask_group()],
            "PreToolUse": [_foreign_group()],
        }
    }
    assert remove_hook(settings) is True
    assert "SessionEnd" not in settings["hooks"], "an emptied event should not linger"
    assert settings["hooks"]["PreToolUse"] == [_foreign_group()]


def test_remove_when_nothing_to_remove():
    settings = {"hooks": {"SessionEnd": [_foreign_group()]}}
    assert remove_hook(settings) is False
    assert settings == {"hooks": {"SessionEnd": [_foreign_group()]}}


def test_runner_shim_runs_grask_under_python3_from_the_plugin_src():
    """The shim must run grask with plain `python3` and the plugin's `src/` on
    PYTHONPATH — no `uv`, no venv — not a bare `grask` on PATH. The root is quoted
    so a plugin dir with a space in it still resolves."""
    text = runner_shim_text("/plugins/grask/0.1.0-rc1")
    assert text.startswith("#!/bin/sh\n")
    assert 'env PYTHONPATH="/plugins/grask/0.1.0-rc1/src" python3 -m grask.cli "$@"' in text
    assert "uv" not in text


def test_write_runner_shim_is_executable_and_lands_in_home(tmp_path: Path):
    home = tmp_path / "grask-home"
    shim = write_runner_shim("/plugins/grask/0.1.0-rc1", home=home)
    assert shim == home / "grask"
    assert shim.read_text(encoding="utf-8") == runner_shim_text("/plugins/grask/0.1.0-rc1")
    assert shim.stat().st_mode & 0o111, "the skill calls the shim directly; it must be executable"


def test_write_runner_shim_refreshes_a_stale_shim(tmp_path: Path):
    """SessionStart rewrites the shim every session because the root moves on
    upgrade — a second write points it at the new root, not the old."""
    home = tmp_path / "grask-home"
    write_runner_shim("/plugins/grask/OLD", home=home)
    shim = write_runner_shim("/plugins/grask/NEW", home=home)
    assert "/plugins/grask/NEW" in shim.read_text(encoding="utf-8")
    assert "/plugins/grask/OLD" not in shim.read_text(encoding="utf-8")


def test_install_writes_skill_and_hook(tmp_path: Path, capsys):
    skills = tmp_path / "skills"
    settings = tmp_path / "settings.json"

    code = install(skills=skills, settings=settings)
    capsys.readouterr()

    assert code == 0
    assert (skills / "grask" / "SKILL.md").is_file()
    written = json.loads(settings.read_text(encoding="utf-8"))
    assert written["hooks"]["SessionEnd"] == [_grask_group()]


def test_install_twice_leaves_one_hook(tmp_path: Path, capsys):
    skills = tmp_path / "skills"
    settings = tmp_path / "settings.json"

    install(skills=skills, settings=settings)
    install(skills=skills, settings=settings)
    capsys.readouterr()

    written = json.loads(settings.read_text(encoding="utf-8"))
    assert written["hooks"]["SessionEnd"] == [_grask_group()]


def test_install_preserves_an_existing_settings_file(tmp_path: Path, capsys):
    skills = tmp_path / "skills"
    settings = tmp_path / "settings.json"
    settings.write_text(
        json.dumps({"model": "opus", "hooks": {"SessionEnd": [_foreign_group()]}}),
        encoding="utf-8",
    )

    install(skills=skills, settings=settings)
    capsys.readouterr()

    written = json.loads(settings.read_text(encoding="utf-8"))
    assert written["model"] == "opus", "unrelated settings must survive"
    assert _foreign_group() in written["hooks"]["SessionEnd"]
    assert _grask_group() in written["hooks"]["SessionEnd"]


def test_uninstall_reverses_install(tmp_path: Path, capsys):
    skills = tmp_path / "skills"
    settings = tmp_path / "settings.json"
    install(skills=skills, settings=settings)

    code = uninstall(skills=skills, settings=settings)
    capsys.readouterr()

    assert code == 0
    assert not (skills / "grask").exists()
    written = json.loads(settings.read_text(encoding="utf-8"))
    assert "hooks" not in written


def test_uninstall_leaves_foreign_settings_alone(tmp_path: Path, capsys):
    skills = tmp_path / "skills"
    settings = tmp_path / "settings.json"
    settings.write_text(
        json.dumps({"model": "opus", "hooks": {"SessionEnd": [_foreign_group()]}}),
        encoding="utf-8",
    )
    install(skills=skills, settings=settings)

    uninstall(skills=skills, settings=settings)
    capsys.readouterr()

    written = json.loads(settings.read_text(encoding="utf-8"))
    assert written["model"] == "opus"
    assert written["hooks"]["SessionEnd"] == [_foreign_group()]


def test_hook_configured_reads_the_file(tmp_path: Path):
    settings = tmp_path / "settings.json"
    assert hook_configured(settings=settings) is False, "missing file reads as unwired"

    settings.write_text(json.dumps({"hooks": {"SessionEnd": [_grask_group()]}}), "utf-8")
    assert hook_configured(settings=settings) is True


def test_hook_configured_survives_a_broken_file(tmp_path: Path):
    settings = tmp_path / "settings.json"
    settings.write_text("{ this is not json", encoding="utf-8")
    assert hook_configured(settings=settings) is False


def test_doctor_checks_are_structured_data(tmp_path: Path):
    """Kept as (label, ok, detail) triples so a future `doctor --json` is a
    rendering change, not a rewrite."""
    skills = tmp_path / "skills"
    settings = tmp_path / "settings.json"
    checks = _checks(skills, settings)

    labels = [label for label, _, _ in checks]
    assert labels == [
        "claude on PATH",
        "python3 ≥ 3.8",
        "delivery skill present",
        "capture hook wired",
    ]
    assert all(isinstance(ok, bool) for _, ok, _ in checks)


def test_doctor_fails_when_environment_is_incomplete(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setattr(install_mod.shutil, "which", lambda _: None)
    code = doctor(skills=tmp_path / "skills", settings=tmp_path / "settings.json")
    capsys.readouterr()
    assert code == 1


def test_doctor_passes_when_everything_is_present(tmp_path: Path, monkeypatch, capsys):
    skills = tmp_path / "skills"
    settings = tmp_path / "settings.json"
    install(skills=skills, settings=settings)
    monkeypatch.setattr(install_mod.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(install_mod, "_python_version", lambda _p: (3, 12, 4))

    code = doctor(skills=skills, settings=settings)
    capsys.readouterr()
    assert code == 0


def test_doctor_flags_a_python_older_than_the_floor(tmp_path: Path, monkeypatch, capsys):
    """The gate that replaces `uv`: an interpreter present but too old is a FAIL
    with a legible reason, not a silent pass into a hook that won't import."""
    skills = tmp_path / "skills"
    settings = tmp_path / "settings.json"
    install(skills=skills, settings=settings)
    monkeypatch.setattr(install_mod.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(install_mod, "_python_version", lambda _p: (3, 7, 5))

    code = doctor(skills=skills, settings=settings)
    out = capsys.readouterr().out
    assert code == 1
    assert "grask needs ≥ 3.8" in out


def test_python_check_reports_missing_interpreter(monkeypatch):
    monkeypatch.setattr(install_mod.shutil, "which", lambda _: None)
    ok, detail = install_mod._python_check()
    assert ok is False
    assert "python3" in detail


def test_doctor_counts_the_plugin_shim_as_wired(tmp_path: Path, monkeypatch, capsys):
    """A plugin-only install has no standalone skill or settings.json hook, but the
    runner shim proves the plugin's SessionStart ran — so both surfaces read as
    provided, not as two failures. (conftest points GRASK_HOME at tmp_path, which
    is where write_runner_shim lands.)"""
    from grask.install import write_runner_shim

    write_runner_shim(str(tmp_path / "plugin-root"))  # the plugin's fingerprint
    monkeypatch.setattr(install_mod.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(install_mod, "_python_version", lambda _p: (3, 12, 4))

    # Empty standalone locations: no ~/.claude/skills skill, no settings.json hook.
    code = doctor(skills=tmp_path / "skills", settings=tmp_path / "settings.json")
    out = capsys.readouterr().out
    assert code == 0
    assert "provided by the plugin" in out
