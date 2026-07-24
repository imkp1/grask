"""The plugin is just another distribution of grask, carried in this same repo.

These guard the two ways that distribution silently rots: the plugin's copy of
SKILL.md drifting from the packaged canonical one, and a hand-edited manifest or
hooks file that no longer parses. `claude plugin validate` in CI is the fuller
check; these run offline with no Claude Code installed.
"""

from __future__ import annotations

import json
from pathlib import Path

from grask.install import packaged_skill_text

REPO = Path(__file__).resolve().parent.parent


def test_plugin_skill_mirrors_the_packaged_one():
    """`src/grask/SKILL.md` is canonical (the wheel needs it there); the plugin
    needs its own copy at `skills/grask/SKILL.md`. A CI-guarded identical copy is
    the boring, robust alternative to a symlink that zip archives and Windows
    break."""
    canonical = packaged_skill_text()
    mirror = (REPO / "skills" / "grask" / "SKILL.md").read_text(encoding="utf-8")
    assert mirror == canonical, "run: cp src/grask/SKILL.md skills/grask/SKILL.md"


def test_plugin_manifest_is_valid_json_and_named_grask():
    manifest = json.loads((REPO / ".claude-plugin" / "plugin.json").read_text("utf-8"))
    assert manifest["name"] == "grask"


def test_marketplace_lists_the_plugin_at_repo_root():
    marketplace = json.loads(
        (REPO / ".claude-plugin" / "marketplace.json").read_text("utf-8")
    )
    (plugin,) = marketplace["plugins"]
    assert plugin["name"] == "grask"
    # Relative source must start with ./ and resolve to the repo root.
    assert plugin["source"] == "./"


def test_hooks_register_capture_on_session_end():
    hooks = json.loads((REPO / "hooks" / "hooks.json").read_text("utf-8"))["hooks"]
    end = [h["command"] for group in hooks.get("SessionEnd", []) for h in group["hooks"]]
    start = [h["command"] for group in hooks.get("SessionStart", []) for h in group["hooks"]]

    # SessionEnd runs capture via plain python3 against the plugin's src — no uv,
    # no venv — and reaches it through the plugin root only it can see.
    assert any("grask.hook" in c and "CLAUDE_PLUGIN_ROOT" in c for c in end)

    # SessionStart writes the runner shim the /grask skill calls, passing it the
    # plugin root — the skill has no CLAUDE_PLUGIN_ROOT of its own.
    assert start, "the shim-writing SessionStart hook must not be dropped"
    assert any("grask.cli shim" in c and "--root" in c for c in start)

    # The whole point of this layer: nothing here depends on uv any more.
    assert not any("uv " in c or "uv\t" in c for c in end + start)
