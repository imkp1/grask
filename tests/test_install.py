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
    uninstall,
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
        "uv on PATH",
        "skill installed",
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

    code = doctor(skills=skills, settings=settings)
    capsys.readouterr()
    assert code == 0
