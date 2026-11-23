"""Read the active window and the X11 selection.

X11 only. Supported versions are in README.md. Clipboard owners must
outlive this process: xclip is started in its own session so a later
restore is still served after we exit.
"""

from __future__ import annotations

import atexit
import os
import subprocess
import time

from Xlib import X, display as xdisplay


class X11Error(RuntimeError):
    pass


def _run(args: list[str], data: bytes | None = None, timeout: float = 2) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            args,
            input=data,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise X11Error(f"找不到命令 {args[0]}") from exc


def active_window() -> tuple[int, str, str]:
    """Return (window id, instance, class). Zeros and empty strings when unreadable."""
    try:
        disp = xdisplay.Display()
    except Exception:
        return 0, "", ""
    try:
        root = disp.screen().root
        prop = root.get_full_property(disp.intern_atom("_NET_ACTIVE_WINDOW"), X.AnyPropertyType)
        if prop is None or not prop.value:
            return 0, "", ""
        window_id = int(prop.value[0])
        window = disp.create_resource_object("window", window_id)
        wm_class = window.get_full_property(disp.intern_atom("WM_CLASS"), X.AnyPropertyType)
        if wm_class is None or not wm_class.value:
            return window_id, "", ""
        parts = [part.decode("utf-8", "replace") for part in bytes(wm_class.value).split(b"\x00") if part]
        instance = parts[0] if parts else ""
        klass = parts[1] if len(parts) > 1 else instance
        return window_id, instance, klass
    except Exception:
        return 0, "", ""
    finally:
        disp.close()


def pointer_position() -> tuple[int, int]:
    try:
        disp = xdisplay.Display()
    except Exception:
        return 0, 0
    try:
        pointer = disp.screen().root.query_pointer()
        return int(pointer.root_x), int(pointer.root_y)
    except Exception:
        return 0, 0
    finally:
        disp.close()


def release_modifiers() -> None:
    """Drop the hotkey modifiers so they are not still held for Ctrl+C."""
    _run([
        "xdotool", "keyup",
        "Control_L", "Control_R", "Alt_L", "Alt_R",
        "Shift_L", "Shift_R", "Super_L", "Super_R",
    ])


def read_text_selection(selection: str) -> tuple[bool, str]:
    """Return (available, text). available is False when the selection has no text."""
    for target in ("UTF8_STRING", "STRING"):
        result = _run(["xclip", "-selection", selection, "-o", "-t", target])
        if result.returncode == 0:
            text = result.stdout.decode("utf-8", "replace").replace("\x00", "")
            return True, text
    return False, ""


def write_clipboard(text: str) -> None:
    proc = subprocess.Popen(
        ["xclip", "-selection", "clipboard", "-i", "-t", "UTF8_STRING"],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    assert proc.stdin is not None
    proc.stdin.write(text.encode("utf-8"))
    proc.stdin.close()
    proc.wait(timeout=2)


def send_key(combo: str) -> None:
    _run(["xdotool", "key", "--clearmodifiers", combo])


class ClipboardBackup:
    """Remember the text clipboard so a copy can put it back.

    Non-text contents (an image, for example) cannot be restored. That only
    matters on the Ctrl+C path, when the clipboard is overwritten to read
    the selection.
    """

    def __init__(self) -> None:
        self.had_text = False
        self.original = ""
        self.dirty = False
        self._restored = False

    def save(self) -> ClipboardBackup:
        self.had_text, self.original = read_text_selection("clipboard")
        return self

    def mark_dirty(self) -> None:
        if not self.dirty:
            self.dirty = True
            atexit.register(self.restore)

    def restore(self) -> None:
        if self._restored or not self.dirty:
            return
        self._restored = True
        if self.had_text:
            write_clipboard(self.original)
        else:
            write_clipboard("")


def is_unselected_line_copy(copied: str, primary: str) -> bool:
    """True when an editor copied the current line because nothing was selected.

    VS Code and Cursor copy that line plus a trailing newline. A real
    selection of the same line is also stored in PRIMARY.
    """
    if not copied.endswith("\n"):
        return False
    line = copied[:-1]
    if not line.strip() or "\n" in line:
        return False
    return primary.replace("\r\n", "\n").strip("\n").strip() != line.strip()


def _copy_via_keyboard(backup: ClipboardBackup, copy_wait_ms: int) -> str | None:
    """Ask the focused editable widget for its selection.

    PRIMARY is not used here. It can still hold an older selection from
    another window. A sentinel makes "clipboard did not change" mean the
    copy failed, even when both texts are identical.
    """
    token = f"\u2063pop-ai-{os.getpid()}-{time.time_ns()}\u2063"
    backup.mark_dirty()
    write_clipboard(token)
    send_key("ctrl+c")
    deadline = time.monotonic() + (copy_wait_ms / 1000)
    while True:
        ok, current = read_text_selection("clipboard")
        if ok and current != token and current.strip():
            return current
        if time.monotonic() >= deadline:
            break
        time.sleep(0.05)
    backup.restore()
    return None


def capture_selection(
    surface: str,
    copy_wait_ms: int,
    _window_id: int = 0,
) -> tuple[str | None, ClipboardBackup]:
    """Read a real selection. An empty selection stays empty.

    Terminals and other read-only windows keep the selection in PRIMARY.
    Editors copy the current line when nothing is selected. That line is
    ignored unless PRIMARY holds the same text.
    """
    backup = ClipboardBackup().save()
    available, primary = read_text_selection("primary")
    primary_text = primary if available else ""
    if surface != "editable":
        if primary_text.strip():
            return primary_text, backup
        return None, backup
    copied = _copy_via_keyboard(backup, copy_wait_ms)
    if copied is None:
        return None, backup
    if is_unselected_line_copy(copied, primary_text):
        return None, backup
    return copied, backup
