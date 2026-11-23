"""One-shot entry: a GNOME shortcut starts this, the window closes, the process exits."""

from __future__ import annotations

import argparse
import fcntl
import os
import sys
from pathlib import Path

from pop_ai.config import TomlError, load_config
from pop_ai.prompt import initial_text
from pop_ai.shortcuts import ShortcutError, confirm_replace, find_conflicts, install_shortcuts
from pop_ai.windows import classify_surface
from pop_ai.x11util import X11Error, active_window, capture_selection, release_modifiers
from pop_ai import ui


def _runtime_error() -> str | None:
    """Return a message when this process is outside the supported environment."""
    if sys.version_info < (3, 10) or sys.version_info[:2] > (3, 15):
        return "需要 Python 3.10 到 3.15。"
    if os.environ.get("XDG_SESSION_TYPE") != "x11":
        return "需要 X11 桌面。Ubuntu 22.04 和 24.04 请在登录界面选「Ubuntu on Xorg」。"
    return None


def _lock():
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if not runtime:
        return os.open(os.devnull, os.O_RDONLY)
    path = Path(runtime) / "pop-ai.lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


def _fail(message: str) -> int:
    print(f"pop-ai: {message}", file=sys.stderr)
    return 1


def _ask() -> int:
    held = _lock()
    if held is None:
        return 0
    try:
        cfg = load_config()
    except TomlError as exc:
        print(f"pop-ai: {exc}", file=sys.stderr)
        return 1
    try:
        release_modifiers()
        window_id, instance, klass = active_window()
        surface = classify_surface(
            instance, klass, cfg.editable_wm_class, cfg.readonly_wm_class,
        )
        text, backup = capture_selection(surface, cfg.copy_wait_ms, window_id)
        try:
            draft = initial_text(text, cfg.prefix)
        finally:
            backup.restore()
        ui.open_popup(cfg, draft)
        return 0
    except (X11Error, OSError, RuntimeError) as exc:
        return _fail(str(exc))
    finally:
        os.close(held)


def _install() -> int:
    try:
        cfg = load_config()
    except TomlError as exc:
        print(f"pop-ai: {exc}", file=sys.stderr)
        return 1
    launcher = Path(sys.argv[0]).resolve()
    if launcher.name == "__main__.py":
        launcher = Path(__file__).resolve().parents[1] / "pop-ai"
    try:
        conflicts = find_conflicts(cfg.hotkey)
    except ShortcutError as exc:
        print(f"pop-ai: {exc}", file=sys.stderr)
        return 1
    if conflicts and not confirm_replace(cfg.hotkey, conflicts, cfg.path):
        print("未修改快捷键。")
        print(f"请编辑 {cfg.path} 里的 [hotkeys] ask，换成别的按键，再执行一次 install-shortcuts。")
        return 0
    try:
        written, released = install_shortcuts(cfg, launcher, conflicts)
    except ShortcutError as exc:
        print(f"pop-ai: {exc}", file=sys.stderr)
        return 1
    if released:
        print("已从系统快捷键中去掉同一个按键：")
        for schema, key, value in released:
            shown = value if value else "（空）"
            print(f"  {schema} {key} = {shown}")
    print("已注册快捷键：")
    for _name, binding, command in written:
        print(f"  {binding}")
        print(f"    {command}")
    print("这个按键现在只打开 pop-ai。")
    print(f"修改 {cfg.path} 里的快捷键后，再执行一次 install-shortcuts。")
    return 0


def main(argv: list[str] | None = None) -> int:
    problem = _runtime_error()
    if problem:
        print(f"pop-ai: {problem}", file=sys.stderr)
        return 1
    parser = argparse.ArgumentParser(prog="pop-ai", description="弹出输入框，把文字发给模型，并把回复接在后面。")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("ask", help="打开输入框。有选区时在前面加上配置的前缀。")
    sub.add_parser("install-shortcuts", help="按配置文件把快捷键写进 GNOME")
    args = parser.parse_args(argv)
    if args.cmd == "install-shortcuts":
        return _install()
    return _ask()


if __name__ == "__main__":
    sys.exit(main())
