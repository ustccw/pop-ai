"""Decide whether the focused window may receive Ctrl+C.

Terminals are always read-only. Ctrl+C in a terminal is SIGINT.
"""

from __future__ import annotations

TERMINAL_WM_CLASSES = frozenset({
    "gnome-terminal",
    "gnome-terminal-server",
    "kgx",
    "org.gnome.console",
    "kitty",
    "alacritty",
    "xterm",
    "konsole",
    "tilix",
    "terminator",
    "wezterm",
    "org.wezfurlong.wezterm",
    "xfce4-terminal",
    "mate-terminal",
    "ghostty",
    "com.mitchellh.ghostty",
    "urxvt",
    "rxvt",
    "st",
    "io.elementary.terminal",
    "terminology",
    "hyper",
    "tabby",
    "contour",
    "foot",
    "footclient",
})


def window_names(instance: str, klass: str) -> set[str]:
    return {name.lower() for name in (instance, klass) if name}


def is_terminal(instance: str, klass: str) -> bool:
    return bool(window_names(instance, klass) & TERMINAL_WM_CLASSES)


def classify_surface(
    instance: str,
    klass: str,
    editable_classes: list[str],
    readonly_classes: list[str],
) -> str:
    """Return "editable" only for a listed window that is not a terminal.

    Editable windows are asked for the selection with Ctrl+C. Every other
    window is read from the PRIMARY selection and never receives a key.
    """
    names = window_names(instance, klass)
    if not names or names & TERMINAL_WM_CLASSES:
        return "readonly"
    editable = {item.lower() for item in editable_classes}
    if names & editable:
        return "editable"
    readonly = {item.lower() for item in readonly_classes}
    if names & readonly:
        return "readonly"
    return "readonly"
