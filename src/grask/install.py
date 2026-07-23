"""Wiring grask into Claude Code without hand-editing settings.json.

`grask install` owns exactly one contract: *grask's own surfaces are configured*
— the `/grask` skill is written and the `SessionEnd` capture hook is present in
`settings.json`. It deliberately does not check for the `claude` binary or for a
live authentication. Someone may install grask before Claude, or authenticate
later; those are environmental concerns, and diagnosing them belongs to
`grask doctor`, not to a prerequisite that would fail an otherwise-correct install.

The `settings.json` merge is the one delicate part. It must be idempotent (running
`install` twice adds one hook, not two), it must preserve hooks that other tools
put there, and `uninstall` must remove grask's entry and nothing else. Every one of
those is a way to corrupt a file the developer did not ask us to touch, so each is
enforced here and tested rather than trusted.
"""

from __future__ import annotations

import json
import shutil
from importlib import resources
from pathlib import Path
from typing import Any

from grask.storage import grask_home

# The command the standalone hook registers. The plugin path uses a different
# command (uv run … grask-hook) written into the plugin's own hooks.json; this
# one is for the settings.json that `grask install` maintains.
HOOK_COMMAND = "grask-hook"

# Claude Code's user-level config. Both are under ~/.claude; kept as functions so
# tests point them at a tmp dir and never touch the developer's real setup.
def settings_path() -> Path:
    return Path.home() / ".claude" / "settings.json"


def skills_dir() -> Path:
    return Path.home() / ".claude" / "skills"


def _load_settings(path: Path) -> dict[str, Any]:
    """Read settings.json, or start from empty. A missing or empty file is not an
    error — it is the common case on a fresh machine."""
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return {}
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError(f"{path} is not a JSON object")
    return data


def _write_settings(path: Path, settings: dict[str, Any]) -> None:
    """Write settings back with stable, human-friendly formatting and a trailing
    newline, so a diff of what we changed stays small."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")


def _session_end_groups(settings: dict[str, Any]) -> list[Any]:
    """The SessionEnd hook groups, coerced to a list we can mutate in place."""
    hooks = settings.setdefault("hooks", {})
    groups: list[Any] = hooks.setdefault("SessionEnd", [])
    return groups


def _command_present(groups: list[Any], command: str) -> bool:
    return any(
        isinstance(group, dict)
        and any(
            isinstance(h, dict) and h.get("command") == command
            for h in group.get("hooks", [])
        )
        for group in groups
    )


def merge_hook(settings: dict[str, Any], command: str = HOOK_COMMAND) -> bool:
    """Add grask's SessionEnd hook if it is not already there. Returns True if it
    changed anything. Idempotent by design: a second install is a no-op, not a
    duplicate."""
    groups = _session_end_groups(settings)
    if _command_present(groups, command):
        return False
    groups.append({"hooks": [{"type": "command", "command": command}]})
    return True


def remove_hook(settings: dict[str, Any], command: str = HOOK_COMMAND) -> bool:
    """Remove grask's hook and only grask's hook. Returns True if it changed
    anything. Empties are pruned so uninstall does not leave `"SessionEnd": []`
    behind, but foreign hooks in the same event are untouched."""
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict) or "SessionEnd" not in hooks:
        return False

    changed = False
    surviving_groups = []
    for group in hooks["SessionEnd"]:
        if not isinstance(group, dict):
            surviving_groups.append(group)
            continue
        kept = [
            h
            for h in group.get("hooks", [])
            if not (isinstance(h, dict) and h.get("command") == command)
        ]
        if len(kept) != len(group.get("hooks", [])):
            changed = True
        if kept:
            group["hooks"] = kept
            surviving_groups.append(group)

    if surviving_groups:
        hooks["SessionEnd"] = surviving_groups
    else:
        del hooks["SessionEnd"]
    if not hooks:
        del settings["hooks"]
    return changed


def _write_skill(target_dir: Path) -> Path:
    """Write the shipped SKILL.md into <dir>/grask/SKILL.md. The directory name is
    the slash command, so it has to be `grask/` for `/grask` to exist."""
    text = (resources.files("grask") / "SKILL.md").read_text(encoding="utf-8")
    target = target_dir / "grask" / "SKILL.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


def install(skills: Path | None = None, settings: Path | None = None) -> int:
    """Write the skill and merge the hook. Prints what it did; validates that both
    grask-owned surfaces are in place before claiming success."""
    skills = skills or skills_dir()
    settings = settings or settings_path()

    skill_file = _write_skill(skills)
    print(f"skill:  {skill_file}")

    data = _load_settings(settings)
    if merge_hook(data):
        _write_settings(settings, data)
        print(f"hook:   added SessionEnd hook to {settings}")
    else:
        print(f"hook:   already present in {settings}")

    # Validate only what grask owns.
    wired = _command_present(_session_end_groups(_load_settings(settings)), HOOK_COMMAND)
    if not (skill_file.is_file() and wired):
        print("install did not leave grask's configuration in place")
        return 1
    print("grask is configured. run `grask doctor` to check the environment.")
    return 0


def uninstall(skills: Path | None = None, settings: Path | None = None) -> int:
    """Undo `install`: remove the skill and grask's hook. Leaves the database in
    place — uninstalling the wiring is not the same as throwing away the data —
    and prints where it is."""
    skills = skills or skills_dir()
    settings = settings or settings_path()

    skill_dir = skills / "grask"
    if skill_dir.exists():
        shutil.rmtree(skill_dir)
        print(f"skill:  removed {skill_dir}")
    else:
        print(f"skill:  nothing at {skill_dir}")

    if settings.exists():
        data = _load_settings(settings)
        if remove_hook(data):
            _write_settings(settings, data)
            print(f"hook:   removed grask's SessionEnd hook from {settings}")
        else:
            print(f"hook:   none of grask's hooks were in {settings}")
    else:
        print(f"hook:   no {settings} to edit")

    print(f"data:   left untouched at {grask_home()}")
    return 0


def _checks(skills: Path, settings: Path) -> list[tuple[str, bool, str]]:
    """The diagnostics, as (label, ok, detail) triples. Kept as data rather than
    prose so a future `grask doctor --json` is a rendering change, not a rewrite."""
    skill_file = skills / "grask" / "SKILL.md"
    try:
        wired = _command_present(_session_end_groups(_load_settings(settings)), HOOK_COMMAND)
    except (ValueError, json.JSONDecodeError):
        wired = False

    uv = shutil.which("uv")
    claude = shutil.which("claude")
    no_claude = "not found — grask has no model access without it"
    no_uv = "not found — the plugin runtime needs it"
    return [
        ("claude on PATH", claude is not None, claude or no_claude),
        ("uv on PATH", uv is not None, uv or no_uv),
        ("skill installed", skill_file.is_file(), str(skill_file)),
        ("capture hook wired", wired, f"SessionEnd `{HOOK_COMMAND}` in {settings}"),
    ]


def doctor(skills: Path | None = None, settings: Path | None = None) -> int:
    """Report on grask's own config and the environment it needs. The one owner of
    diagnosis: interactive surfaces call these same checks rather than duplicating
    them. Exit 0 when everything passes, 1 when anything is off, so it is usable in
    CI and in a bug report."""
    skills = skills or skills_dir()
    settings = settings or settings_path()

    checks = _checks(skills, settings)
    for label, ok, detail in checks:
        mark = "ok  " if ok else "FAIL"
        print(f"{mark}  {label}: {detail}")
    return 0 if all(ok for _, ok, _ in checks) else 1


def hook_configured(settings: Path | None = None) -> bool:
    """Is grask's capture hook wired into settings.json? The bare `grask` command
    uses this to nudge a developer who installed the package but never ran
    `grask install`. Never raises — a broken settings file just reads as unwired."""
    settings = settings or settings_path()
    try:
        return _command_present(_session_end_groups(_load_settings(settings)), HOOK_COMMAND)
    except (ValueError, json.JSONDecodeError, OSError):
        return False
