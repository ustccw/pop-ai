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


def same_text(left: str, right: str) -> bool:
    """True when two selection strings are the same aside from surrounding space."""
    return left.replace("\r\n", "\n").strip() == right.replace("\r\n", "\n").strip()


def primary_is_selection(
    primary: str,
    clipboard: str,
    clipboard_known: bool,
    owner_in_window: bool,
) -> bool:
    """True when PRIMARY is highlighted text, not another window's clipboard.

    Text owned by this window is a selection, even if it was also copied.
    Text that differs from the clipboard is a selection too. The same text
    owned by a clipboard helper is just the clipboard.
    """
    if not primary.strip():
        return False
    if owner_in_window:
        return True
    if clipboard_known and same_text(primary, clipboard):
        return False
    return True


def accept_copied_text(copied: str, original: str, clipboard_known: bool, saw_token: bool) -> bool:
    """True when Ctrl+C returned a selection rather than the previous clipboard.

    After the sentinel has been seen, the following text is the selection,
    even when it matches the clipboard. The clipboard itself, still sitting
    there because nothing was copied, is not.
    """
    if not copied.strip():
        return False
    if saw_token or not clipboard_known:
        return True
    return not same_text(copied, original)


def owner_is_within(owner_id: int, window_id: int, parent_of: dict[int, int], root_id: int) -> bool:
    """True when window_id is the selection owner, or a parent of it below the root.

    Clipboard helpers are their own top-level windows. They are not inside
    the focused window, so ownership alone does not make their PRIMARY a selection.
    """
    if owner_id <= 0 or window_id <= 0 or owner_id == root_id:
        return False
    current = owner_id
    seen: set[int] = set()
    while current and current not in seen and current != root_id:
        if current == window_id:
            return True
        seen.add(current)
        current = parent_of.get(current, 0)
    return False


def primary_owner_in_window(window_id: int) -> bool:
    """True when the focused window, or a widget inside it, owns PRIMARY."""
    if window_id <= 0:
        return False
    try:
        disp = xdisplay.Display()
    except Exception:
        return False
    try:
        root = disp.screen().root
        owner = disp.get_selection_owner(disp.intern_atom("PRIMARY"))
        owner_id = getattr(owner, "id", 0)
        if not owner_id:
            return False
        parent_of: dict[int, int] = {}
        current = owner
        seen: set[int] = set()
        while current.id not in seen and current.id != root.id:
            seen.add(current.id)
            parent = current.query_tree().parent
            if parent is None or parent.id == current.id:
                break
            parent_of[current.id] = parent.id
            if parent.id == root.id:
                break
            current = parent
        return owner_is_within(owner_id, window_id, parent_of, root.id)
    except Exception:
        return False
    finally:
        disp.close()


def _copy_via_keyboard(backup: ClipboardBackup, copy_wait_ms: int, combo: str = "ctrl+c") -> str | None:
    """Ask the focused editable widget for its selection.

    PRIMARY is not used here. It can still hold an older selection from
    another window. A sentinel is written, then Ctrl+C. Text that replaces
    the sentinel is kept. The previous clipboard is kept only when that
    replacement happened, so an untouched clipboard is not a selection.
    """
    token = f"\u2063pop-ai-{os.getpid()}-{time.time_ns()}\u2063"
    backup.mark_dirty()
    write_clipboard(token)
    ok, current = read_text_selection("clipboard")
    saw_token = ok and current == token
    send_key(combo)
    deadline = time.monotonic() + (copy_wait_ms / 1000)
    while True:
        ok, current = read_text_selection("clipboard")
        if ok and current == token:
            saw_token = True
        elif ok and current != token and accept_copied_text(
            current, backup.original, backup.had_text, saw_token
        ):
            return current
        if time.monotonic() >= deadline:
            break
        time.sleep(0.05)
    backup.restore()
    return None


def _window_pid(window_id: int) -> int:
    if window_id <= 0:
        return 0
    try:
        disp = xdisplay.Display()
    except Exception:
        return 0
    try:
        window = disp.create_resource_object("window", window_id)
        prop = window.get_full_property(disp.intern_atom("_NET_WM_PID"), X.AnyPropertyType)
        if prop is None or not prop.value:
            return 0
        return int(prop.value[0])
    except Exception:
        return 0
    finally:
        disp.close()


def highlighted_text(count: int, text: str) -> str | None:
    """Return text only when the terminal reports a live highlight."""
    if count <= 0 or not text.strip():
        return None
    return text


def live_terminal_text(accessible: str, primary: str, owner_matches: bool) -> str | None:
    """Text of a highlight that accessibility already confirmed.

    The accessible text snapshot is often shorter than the scrollback, so
    its offsets come back empty. The terminal still stored the highlight
    on PRIMARY. That PRIMARY is used only for a confirmed highlight, and
    only while this terminal owns it.
    """
    if accessible.strip():
        return accessible
    if owner_matches and primary.strip():
        return primary
    return None


def _window_title(window_id: int) -> str:
    if window_id <= 0:
        return ""
    try:
        disp = xdisplay.Display()
    except Exception:
        return ""
    try:
        window = disp.create_resource_object("window", window_id)
        for atom in ("_NET_WM_NAME", "WM_NAME"):
            prop = window.get_full_property(disp.intern_atom(atom), X.AnyPropertyType)
            if prop is None or not isinstance(prop.value, (bytes, bytearray)):
                continue
            title = prop.value.decode("utf-8", "replace").replace("\x00", "")
            if title:
                return title
        return ""
    except Exception:
        return ""
    finally:
        disp.close()


def _primary_owner_pid() -> int:
    try:
        disp = xdisplay.Display()
    except Exception:
        return 0
    try:
        owner = disp.get_selection_owner(disp.intern_atom("PRIMARY"))
        owner_id = getattr(owner, "id", 0)
        if not owner_id:
            return 0
        prop = owner.get_full_property(disp.intern_atom("_NET_WM_PID"), X.AnyPropertyType)
        if prop is None or not prop.value:
            return 0
        return int(prop.value[0])
    except Exception:
        return 0
    finally:
        disp.close()


def _accessible_selection(acc, atspi) -> tuple[int, str]:
    """Return (selection count, accessible text). Text may be empty."""
    count = atspi.Text.get_n_selections(acc)
    if count <= 0:
        return 0, ""
    try:
        span = atspi.Text.get_selection(acc, 0)
        start = int(span.start_offset)
        end = int(span.end_offset)
        if end < start and end != -1:
            start, end = end, start
        if start < 0:
            start = 0
        text = atspi.Text.get_text(acc, start, end) or ""
    except Exception:
        text = ""
    return count, text


def _child_at(acc, index: int):
    try:
        return acc.get_child_at_index(index)
    except Exception:
        return None


def _walk_roles(acc, atspi, role, depth: int, found: list) -> None:
    if depth > 14:
        return
    try:
        if acc.get_role() == role:
            found.append(acc)
            if role == atspi.Role.FRAME:
                return
    except Exception:
        return
    try:
        count = acc.get_child_count()
    except Exception:
        return
    for index in range(count):
        child = _child_at(acc, index)
        if child is not None:
            _walk_roles(child, atspi, role, depth + 1, found)


def _frame_for_window(app, atspi, title: str):
    frames: list = []
    _walk_roles(app, atspi, atspi.Role.FRAME, 0, frames)
    if title:
        for frame in frames:
            try:
                if (frame.get_name() or "") == title:
                    return frame
            except Exception:
                continue
    for frame in frames:
        try:
            if frame.get_state_set().contains(atspi.StateType.ACTIVE):
                return frame
        except Exception:
            continue
    return None


def _showing_terminals(frame, atspi) -> list:
    found: list = []
    _walk_roles(frame, atspi, atspi.Role.TERMINAL, 0, found)
    showing = []
    for acc in found:
        try:
            if acc.get_state_set().contains(atspi.StateType.SHOWING):
                showing.append(acc)
        except Exception:
            continue
    return showing or found


def terminal_selection(window_id: int) -> tuple[str, str | None]:
    """Read the highlight in this terminal window.

    Returns ("unseen", None) when this window has no accessible terminal.
    Returns ("empty", None) when the visible terminal has no highlight.
    Returns ("text", selection) when text is highlighted.

    VTE drops the highlight without giving up PRIMARY, so a selection count
    of zero means the highlight is gone. Nothing is typed into the terminal.
    """
    pid = _window_pid(window_id)
    if pid <= 0:
        return "unseen", None
    try:
        import gi

        gi.require_version("Atspi", "2.0")
        from gi.repository import Atspi
    except Exception:
        return "unseen", None
    try:
        desktop = Atspi.get_desktop(0)
    except Exception:
        return "unseen", None
    app = None
    for index in range(desktop.get_child_count()):
        child = desktop.get_child_at_index(index)
        if child is not None and child.get_process_id() == pid:
            app = child
            break
    if app is None:
        return "unseen", None
    frame = _frame_for_window(app, Atspi, _window_title(window_id))
    if frame is None:
        return "empty", None
    for acc in _showing_terminals(frame, Atspi):
        try:
            count, accessible = _accessible_selection(acc, Atspi)
        except Exception:
            continue
        if count <= 0:
            continue
        text = live_terminal_text(accessible, "", False)
        if text:
            return "text", text
        ok, primary = read_text_selection("primary")
        text = live_terminal_text("", primary if ok else "", _primary_owner_pid() == pid)
        return "text", text
    return "empty", None


def capture_selection(
    surface: str,
    copy_wait_ms: int,
    window_id: int = 0,
) -> tuple[str | None, ClipboardBackup]:
    """Read a real selection. An empty selection stays empty.

    Terminals keep PRIMARY after the highlight is gone, so that text is
    not used. The accessible selection is used instead, and only while a
    highlight exists. Other read-only windows are read from PRIMARY. A
    clipboard helper may leave the clipboard there; that copy is ignored.
    Editors copy the current line when nothing is selected. That line is
    ignored unless PRIMARY holds the same text.
    """
    backup = ClipboardBackup().save()
    if surface == "terminal":
        status, text = terminal_selection(window_id)
        if status != "unseen":
            return text, backup
    available, primary = read_text_selection("primary")
    primary_text = primary if available else ""
    if surface != "editable":
        owned = primary_owner_in_window(window_id) if primary_text.strip() else False
        if primary_is_selection(primary_text, backup.original, backup.had_text, owned):
            return primary_text, backup
        return None, backup
    copied = _copy_via_keyboard(backup, copy_wait_ms)
    if copied is None:
        return None, backup
    if is_unselected_line_copy(copied, primary_text):
        return None, backup
    return copied, backup
