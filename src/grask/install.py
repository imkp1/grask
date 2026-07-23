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
import subprocess
from importlib import resources
from pathlib import Path
from typing import Any

from grask.storage import grask_home

# The command the standalone hook registers. The plugin path uses a different
# command (env PYTHONPATH=…/src python3 -m grask.hook) written into the plugin's
# own hooks.json; this one is for the settings.json that `grask install` maintains.
HOOK_COMMAND = "grask-hook"

# Grask needs a Python this new; `grask doctor` gates on it. Kept as a constant so
# the shim, the docs, and the check cannot drift to three different numbers.
MIN_PYTHON = (3, 12)

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


def runner_shim_text(root: str) -> str:
    """The tiny sh shim the `/grask` skill calls so it never assumes a `grask` is
    on PATH. It runs grask exactly as the SessionEnd hook does: plain `python3`
    with the plugin's `src/` on `PYTHONPATH`. grask has no third-party
    dependencies and reaches the model by running the `claude` binary, so it needs
    no virtualenv — only a Python `grask doctor` vouches for. `${CLAUDE_PLUGIN_ROOT}`
    is substituted only inside `hooks/hooks.json`, never in a skill's shell, so the
    root has to be baked in here at SessionStart rather than read back later."""
    return f'#!/bin/sh\nexec env PYTHONPATH="{root}/src" python3 -m grask.cli "$@"\n'


def write_runner_shim(root: str, home: Path | None = None) -> Path:
    """Write, and mark executable, the runner shim under grask's home. Refreshed
    every SessionStart because the plugin root carries a version in its path and
    moves on upgrade — a shim written once would soon point at a directory the
    upgrade deleted."""
    home = home or grask_home()
    home.mkdir(parents=True, exist_ok=True)
    shim = home / "grask"
    shim.write_text(runner_shim_text(root), encoding="utf-8")
    shim.chmod(0o755)
    return shim


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


def _python_version(python3: str) -> tuple[int, int, int] | None:
    """The (major, minor, micro) of a `python3` binary, or None if it cannot be
    determined. Probed by running it rather than by parsing its name, because
    `python3` is a symlink that says nothing about the version behind it."""
    try:
        out = subprocess.run(
            [python3, "-c", "import sys;print('%d %d %d' % sys.version_info[:3])"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout.split()
        return (int(out[0]), int(out[1]), int(out[2]))
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return None


def _python_check() -> tuple[bool, str]:
    """Is there a `python3` on PATH new enough to run grask? This is the one
    environmental thing the plugin needs — no venv, no uv, just an interpreter."""
    python3 = shutil.which("python3")
    if python3 is None:
        return False, "not found — the plugin runs grask with `python3`"
    version = _python_version(python3)
    if version is None:
        return False, f"{python3} — could not determine its version"
    got = ".".join(str(n) for n in version)
    need = ".".join(str(n) for n in MIN_PYTHON)
    if version[:2] < MIN_PYTHON:
        return False, f"{python3} is Python {got} — grask needs ≥ {need}"
    return True, f"{python3} (Python {got})"


def _checks(skills: Path, settings: Path) -> list[tuple[str, bool, str]]:
    """The diagnostics, as (label, ok, detail) triples. Kept as data rather than
    prose so a future `grask doctor --json` is a rendering change, not a rewrite.

    Two ways to be wired, checked together: the standalone install (skill under
    `~/.claude/skills`, `grask-hook` in `settings.json`) and the plugin. The
    runner shim is the plugin's fingerprint — SessionStart writes it, so its
    presence means the plugin's hooks (and its bundled skill) are the ones live —
    and it stands in for both surfaces so a healthy plugin install does not read
    as two failures."""
    skill_file = skills / "grask" / "SKILL.md"
    try:
        wired = _command_present(_session_end_groups(_load_settings(settings)), HOOK_COMMAND)
    except (ValueError, json.JSONDecodeError):
        wired = False

    shim = grask_home() / "grask"
    via_plugin = shim.is_file()

    if skill_file.is_file():
        skill_ok, skill_detail = True, str(skill_file)
    elif via_plugin:
        skill_ok, skill_detail = True, f"provided by the plugin ({shim})"
    else:
        skill_ok, skill_detail = False, str(skill_file)

    if wired:
        hook_ok, hook_detail = True, f"SessionEnd `{HOOK_COMMAND}` in {settings}"
    elif via_plugin:
        hook_ok, hook_detail = True, "provided by the plugin's SessionEnd hook"
    else:
        hook_ok, hook_detail = False, f"SessionEnd `{HOOK_COMMAND}` in {settings}"

    claude = shutil.which("claude")
    no_claude = "not found — grask has no model access without it"
    py_ok, py_detail = _python_check()
    need = ".".join(str(n) for n in MIN_PYTHON)
    return [
        ("claude on PATH", claude is not None, claude or no_claude),
        (f"python3 ≥ {need}", py_ok, py_detail),
        ("delivery skill present", skill_ok, skill_detail),
        ("capture hook wired", hook_ok, hook_detail),
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
