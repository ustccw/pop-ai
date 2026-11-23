"""Register the hotkey as a GNOME custom shortcut without dropping existing ones."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from pop_ai.config import Config

SCHEMA = "org.gnome.settings-daemon.plugins.media-keys"
ITEM = "org.gnome.settings-daemon.plugins.media-keys.custom-keybinding"
PREFIX = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/"


class ShortcutError(RuntimeError):
    pass


def gvariant_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def gvariant_strv(items: list[str]) -> str:
    if not items:
        return "@as []"
    return "[" + ", ".join(gvariant_string(item) for item in items) + "]"


def parse_strv(raw: str) -> list[str]:
    text = raw.strip()
    if text.startswith("@as "):
        text = text[4:].strip()
    found: list[str] = []
    for match in re.finditer(r"'([^']*)'|\"((?:\\.|[^\"])*)\"", text):
        if match.group(1) is not None:
            found.append(match.group(1))
        else:
            found.append(bytes(match.group(2), "utf-8").decode("unicode_escape"))
    return found


def merge_paths(existing: list[str], additions: list[str]) -> list[str]:
    merged = list(existing)
    for path in additions:
        if path not in merged:
            merged.append(path)
    return merged


def same_accelerator(left: str, right: str) -> bool:
    return left.strip().lower() == right.strip().lower()


def without_accelerator(bindings: list[str], hotkey: str) -> list[str]:
    return [item for item in bindings if not same_accelerator(item, hotkey)]


def _value_kind(raw: str) -> str:
    text = raw.strip()
    if text.startswith("@as") or text.startswith("["):
        return "array"
    if text.startswith("'") or text.startswith('"'):
        return "string"
    return "other"


def _gsettings_get(schema: str, key: str) -> str:
    try:
        result = subprocess.run(
            ["gsettings", "get", schema, key],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise ShortcutError("找不到 gsettings，无法注册 GNOME 快捷键") from exc
    if result.returncode != 0:
        raise ShortcutError(result.stderr.strip() or "读取 GNOME 快捷键失败")
    return result.stdout


def _gsettings_set(schema: str, key: str, value: str) -> None:
    result = subprocess.run(
        ["gsettings", "set", schema, key, value],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise ShortcutError(detail or f"写入 {key} 失败")


def _gsettings_list() -> str:
    try:
        result = subprocess.run(
            ["gsettings", "list-recursively"],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise ShortcutError("找不到 gsettings，无法注册 GNOME 快捷键") from exc
    if result.returncode != 0:
        raise ShortcutError(result.stderr.strip() or "读取 GNOME 快捷键失败")
    return result.stdout


def _split_rec_line(line: str) -> tuple[str, str, str] | None:
    parts = line.strip().split(None, 2)
    if len(parts) != 3:
        return None
    return parts[0], parts[1], parts[2]


KNOWN_BINDINGS = {
    ("org.gnome.mutter.keybindings", "switch-monitor"): "切换显示器",
}


class Conflict:
    def __init__(self, schema: str, key: str, kind: str, kept: list[str]) -> None:
        self.schema = schema
        self.key = key
        self.kind = kind
        self.kept = kept

    @property
    def label(self) -> str:
        return KNOWN_BINDINGS.get((self.schema, self.key), f"{self.schema} {self.key}")


def display_hotkey(hotkey: str) -> str:
    """Turn a GTK accelerator into the text shown in the conflict dialog."""
    body = hotkey.strip()
    for src, dst in (
        ("<Super>", "Super+"),
        ("<Primary>", "Ctrl+"),
        ("<Control>", "Ctrl+"),
        ("<Shift>", "Shift+"),
        ("<Alt>", "Alt+"),
        ("<Mod4>", "Super+"),
    ):
        body = body.replace(src, dst).replace(src.lower(), dst)
    body = body.replace("<", "").replace(">", "")
    if len(body) >= 2 and body[-2] == "+" and body[-1].isalpha():
        body = body[:-1] + body[-1].upper()
    return body


def conflict_message(hotkey: str, conflicts: list[Conflict], config_path: Path) -> str:
    shown = display_hotkey(hotkey)
    lines = [f"{shown} 已经用于："]
    for conflict in conflicts:
        lines.append(f"· {conflict.label}")
    lines.append("")
    lines.append(f"替换后，{shown} 只打开 pop-ai。")
    lines.append("不替换的话，请修改")
    lines.append(str(config_path))
    lines.append("里的 [hotkeys] ask，换成别的按键，再执行一次 install-shortcuts。")
    return "\n".join(lines)


def find_conflicts(hotkey: str) -> list[Conflict]:
    """List other GNOME bindings that use this accelerator. pop-ai's own key is skipped."""
    found: list[Conflict] = []
    own = f"{PREFIX}pop-ai-ask/"
    try:
        custom_paths = parse_strv(_gsettings_get(SCHEMA, "custom-keybindings"))
    except ShortcutError:
        custom_paths = []
    for path in custom_paths:
        if path == own:
            continue
        item_schema = f"{ITEM}:{path}"
        try:
            current = parse_strv(_gsettings_get(item_schema, "binding"))
        except ShortcutError:
            continue
        if len(current) == 1 and same_accelerator(current[0], hotkey):
            found.append(Conflict(item_schema, "binding", "string", []))

    for line in _gsettings_list().splitlines():
        parsed = _split_rec_line(line)
        if parsed is None:
            continue
        schema, key, raw = parsed
        if "pop-ai-ask" in schema:
            continue
        kind = _value_kind(raw)
        if kind == "array":
            items = parse_strv(raw)
            if any(same_accelerator(item, hotkey) for item in items):
                found.append(Conflict(schema, key, "array", without_accelerator(items, hotkey)))
        elif kind == "string":
            values = parse_strv(raw)
            if len(values) == 1 and same_accelerator(values[0], hotkey):
                found.append(Conflict(schema, key, "string", []))
    return found


def release_conflicts(conflicts: list[Conflict]) -> list[tuple[str, str, str]]:
    """Remove this accelerator from the bindings the user agreed to replace."""
    changed: list[tuple[str, str, str]] = []
    for conflict in conflicts:
        if conflict.kind == "array":
            value = gvariant_strv(conflict.kept)
        else:
            value = gvariant_string("")
        _gsettings_set(conflict.schema, conflict.key, value)
        changed.append((conflict.schema, conflict.key, value))
    return changed


def _fit_dialog(dialog) -> None:
    """Wrap the text and keep the dialog inside the monitor work area."""
    from gi.repository import Gdk, Gtk, Pango

    area = dialog.get_message_area()
    if area is not None:
        for child in area.get_children():
            if isinstance(child, Gtk.Label):
                child.set_line_wrap(True)
                child.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
                child.set_max_width_chars(42)
    display = Gdk.Display.get_default()
    if display is None:
        return
    seat = display.get_default_seat()
    device = seat.get_pointer() if seat is not None else None
    px = py = 0
    if device is not None:
        _screen, px, py = device.get_position()
    monitor = display.get_monitor_at_point(px, py)
    if monitor is None:
        return
    work = monitor.get_workarea()
    margin = 16
    max_w = max(280, work.width - margin * 2)
    max_h = max(160, work.height - margin * 2)
    dialog.set_default_size(min(480, max_w), -1)
    dialog.show_all()
    width, height = dialog.get_size()
    width = min(width, max_w)
    height = min(height, max_h)
    dialog.resize(width, height)
    dialog.move(work.x + margin, work.y + margin)


def confirm_replace(hotkey: str, conflicts: list[Conflict], config_path: Path) -> bool:
    """Ask whether to take the key. False means the user will edit the config."""
    message = conflict_message(hotkey, conflicts, config_path)
    try:
        import gi

        gi.require_version("Gtk", "3.0")
        from gi.repository import Gtk
    except Exception:
        print(message)
        print("无法打开对话框，未替换。")
        return False
    result = Gtk.init_check([])
    ok = result[0] if isinstance(result, tuple) else bool(result)
    if not ok:
        print(message)
        print("无法打开对话框，未替换。")
        return False
    dialog = Gtk.MessageDialog(
        message_type=Gtk.MessageType.QUESTION,
        buttons=Gtk.ButtonsType.NONE,
        text=f"{display_hotkey(hotkey)} 已被占用",
    )
    dialog.format_secondary_text(message)
    dialog.add_button("不替换，我去改配置", Gtk.ResponseType.CANCEL)
    dialog.add_button("替换", Gtk.ResponseType.OK)
    dialog.set_default_response(Gtk.ResponseType.CANCEL)
    dialog.set_keep_above(True)
    _fit_dialog(dialog)
    response = dialog.run()
    dialog.destroy()
    while Gtk.events_pending():
        Gtk.main_iteration_do(False)
    return response == Gtk.ResponseType.OK


def install_shortcuts(
    cfg: Config,
    launcher: Path,
    conflicts: list[Conflict] | None = None,
) -> tuple[list[tuple[str, str, str]], list[tuple[str, str, str]]]:
    launcher = launcher.resolve()
    released = release_conflicts(conflicts or [])
    entries = [
        ("pop-ai-ask", "Pop AI Ask", f"{launcher} ask", cfg.hotkey),
    ]
    existing = parse_strv(_gsettings_get(SCHEMA, "custom-keybindings"))
    paths = []
    written = []
    for slug, name, command, binding in entries:
        path = f"{PREFIX}{slug}/"
        paths.append(path)
        item_schema = f"{ITEM}:{path}"
        _gsettings_set(SCHEMA, "custom-keybindings", gvariant_strv(merge_paths(existing, paths)))
        _gsettings_set(item_schema, "name", gvariant_string(name))
        _gsettings_set(item_schema, "command", gvariant_string(command))
        # Clear first so the key grab is retried after the system binding is gone.
        _gsettings_set(item_schema, "binding", gvariant_string(""))
        _gsettings_set(item_schema, "binding", gvariant_string(binding))
        written.append((name, binding, command))
    return written, released
