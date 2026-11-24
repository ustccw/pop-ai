"""A focusable popup. The transcript is read-only; the next question is typed below it."""

from __future__ import annotations

import math
import os
import threading

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk, Pango

from pop_ai import __version__
from pop_ai.config import Config, state_dir
from pop_ai.deepseek import DeepSeekError, assistant_message, complete
from pop_ai.prompt import (
    append_question,
    finish_turn,
    parse_command,
    plain_reply,
    session_file,
    source_note,
)
from pop_ai.x11util import pointer_position

HINT_IDLE = "Enter 发送，Shift+Enter 换行，Esc 关闭\n/save 保存会话，/clear 清除会话"
HINT_WAIT = "正在回复，可先输入下一句\n回复后 Enter 发送，Esc 关闭"
TITLE = f"pop-ai (v{__version__})"
INPUT_HEIGHT = 96
_live = 0


class WrappingTextView(Gtk.TextView):
    """Wrap inside the scrolled window instead of demanding the text's full width.

    A normal text view reports the unwrapped line as its minimum width. The
    window then refuses to become narrower, so dragging the right edge barely
    moves.
    """

    def do_get_preferred_width(self):
        return 1, 1

    def do_get_preferred_width_for_height(self, height):
        return 1, 1


def _init_gtk() -> None:
    result = Gtk.init_check([])
    ok = result[0] if isinstance(result, tuple) else bool(result)
    if not ok:
        raise RuntimeError("无法打开图形界面")


class Popup:
    def __init__(self, cfg: Config, initial: str) -> None:
        _init_gtk()
        self.cfg = cfg
        self.closed = False
        self._presented = False
        self.loading = False
        self.generation = 0
        self.messages: list[dict[str, str]] = []
        self.waiting: tuple[int, int] | None = None
        self.pointer = pointer_position()
        self.user_moved = False
        self._drag_offset: tuple[float, float] | None = None
        self._pointer_grabbed = False
        self._shaped = (0, 0)

        self.window = Gtk.Window(type=Gtk.WindowType.TOPLEVEL)
        self.window.set_name("pop-ai")
        self.window.set_title(TITLE)
        self.window.set_decorated(False)
        self.window.set_resizable(True)
        self.window.set_default_size(cfg.width, cfg.height)
        self.window.set_keep_above(True)
        self.window.set_skip_taskbar_hint(True)
        self.window.set_skip_pager_hint(True)
        self.window.set_accept_focus(True)
        self.window.set_focus_on_map(True)
        self.window.set_type_hint(Gdk.WindowTypeHint.NORMAL)
        self.window.add_events(
            Gdk.EventMask.KEY_PRESS_MASK
            | Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.BUTTON_RELEASE_MASK
            | Gdk.EventMask.POINTER_MOTION_MASK
        )
        self.window.connect("key-press-event", self._on_key)
        self.window.connect("button-press-event", self._on_button)
        self.window.connect("button-release-event", self._on_drag_release)
        self.window.connect("motion-notify-event", self._on_drag_motion)
        self.window.connect("size-allocate", self._on_size_allocate)
        self.window.connect("destroy", self._on_destroy)
        self.window.connect("delete-event", lambda *_args: self._finish() or True)

        self.drag_bar = Gtk.EventBox()
        self.drag_bar.get_style_context().add_class("drag-bar")
        self.drag_bar.add_events(Gdk.EventMask.BUTTON_PRESS_MASK)
        self.drag_bar.connect("button-press-event", self._on_drag_press)
        self.drag_bar.connect("realize", lambda _widget: self._install_drag_cursor())
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        header.set_border_width(6)
        self.title = Gtk.Label(label=TITLE, xalign=0)
        self.title.set_hexpand(True)
        self.title.set_halign(Gtk.Align.START)
        self.title.set_valign(Gtk.Align.CENTER)
        self.title.get_style_context().add_class("title")
        self.close_button = self._header_button("×", lambda _button: self._finish())
        self.close_button.get_style_context().add_class("close")
        header.pack_start(self.title, True, True, 0)
        header.pack_end(self.close_button, False, False, 0)
        self.drag_bar.add(header)

        self.scroll = Gtk.ScrolledWindow()
        self.scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.scroll.set_propagate_natural_width(False)
        self.scroll.set_propagate_natural_height(False)
        self.scroll.set_size_request(360, 140)
        self.scroll.set_hexpand(True)
        self.scroll.set_vexpand(True)
        self.scroll.set_overlay_scrolling(False)
        self.transcript = self._text_view(editable=False)
        self.transcript.set_cursor_visible(False)
        self.transcript.connect("populate-popup", self._style_menu)
        self.scroll.add(self.transcript)

        self.input_scroll = Gtk.ScrolledWindow()
        self.input_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.input_scroll.set_propagate_natural_width(False)
        self.input_scroll.set_propagate_natural_height(False)
        self.input_scroll.set_size_request(360, 48)
        self.input_scroll.set_hexpand(True)
        self.input_scroll.set_vexpand(True)
        self.input_scroll.set_overlay_scrolling(False)
        self.composer = self._text_view(editable=True)
        self.composer.connect("key-press-event", self._on_key)
        self.composer.connect("populate-popup", self._style_menu)
        self.input_scroll.add(self.composer)

        self.hint = Gtk.Label(xalign=0)
        self.hint.set_line_wrap(True)
        self.hint.set_max_width_chars(48)
        self.hint.get_style_context().add_class("hint")
        self._set_idle_hint()

        self.paned = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL)
        self.paned.set_wide_handle(True)
        self.paned.pack1(self.scroll, True, False)
        self.paned.pack2(self.input_scroll, True, False)
        self.paned.set_position(max(160, cfg.height - 220))
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        content.set_border_width(10)
        content.pack_start(self.paned, True, True, 0)
        content.pack_start(self.hint, False, False, 0)
        shell = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        shell.pack_start(self.drag_bar, False, False, 0)
        shell.pack_start(content, True, True, 0)
        shell.set_hexpand(True)
        shell.set_vexpand(True)
        self.window.add(self._resize_frame(shell))
        self._apply_style()
        self._set_composer(initial)

    def _style_menu(self, _view, menu) -> None:
        menu.set_name("pop-ai-menu")

    def _resize_frame(self, content: Gtk.Widget) -> Gtk.Widget:
        """Eight edge grips. The window has no frame, so these are the resize handles."""
        frame = Gtk.Grid()
        edges = (
            (0, 0, False, False, Gdk.WindowEdge.NORTH_WEST, "nw-resize"),
            (1, 0, True, False, Gdk.WindowEdge.NORTH, "n-resize"),
            (2, 0, False, False, Gdk.WindowEdge.NORTH_EAST, "ne-resize"),
            (0, 1, False, True, Gdk.WindowEdge.WEST, "w-resize"),
            (2, 1, False, True, Gdk.WindowEdge.EAST, "e-resize"),
            (0, 2, False, False, Gdk.WindowEdge.SOUTH_WEST, "sw-resize"),
            (1, 2, True, False, Gdk.WindowEdge.SOUTH, "s-resize"),
            (2, 2, False, False, Gdk.WindowEdge.SOUTH_EAST, "se-resize"),
        )
        for left, top, hexpand, vexpand, edge, cursor in edges:
            frame.attach(self._resize_grip(edge, cursor, hexpand, vexpand), left, top, 1, 1)
        frame.attach(content, 1, 1, 1, 1)
        return frame

    def _resize_grip(self, edge: Gdk.WindowEdge, cursor: str, hexpand: bool, vexpand: bool) -> Gtk.EventBox:
        thickness = 8
        grip = Gtk.EventBox()
        grip.get_style_context().add_class("resize-edge")
        grip.set_hexpand(hexpand)
        grip.set_vexpand(vexpand)
        if hexpand and not vexpand:
            grip.set_size_request(-1, thickness)
        elif vexpand and not hexpand:
            grip.set_size_request(thickness, -1)
        else:
            grip.set_size_request(thickness, thickness)
        grip.connect("button-press-event", lambda _widget, event, side=edge: self._begin_resize(side, event))
        grip.connect("realize", lambda widget, name=cursor: self._set_cursor(widget, name))
        return grip

    def _set_cursor(self, widget: Gtk.Widget, name: str) -> None:
        gdk_window = widget.get_window()
        display = widget.get_display()
        if gdk_window is None or display is None:
            return
        cursor = Gdk.Cursor.new_from_name(display, name)
        if cursor is not None:
            gdk_window.set_cursor(cursor)

    def _begin_resize(self, edge: Gdk.WindowEdge, event) -> bool:
        if event.button != 1 or event.type != Gdk.EventType.BUTTON_PRESS:
            return False
        self.window.begin_resize_drag(
            edge,
            event.button,
            int(event.x_root),
            int(event.y_root),
            event.time,
        )
        return True

    def _header_button(self, label: str, callback) -> Gtk.Button:
        button = Gtk.Button(label=label)
        button.set_relief(Gtk.ReliefStyle.NONE)
        button.set_can_focus(False)
        button.set_valign(Gtk.Align.CENTER)
        button.connect("clicked", callback)
        return button

    def _text_view(self, editable: bool) -> Gtk.TextView:
        view = WrappingTextView()
        view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        view.set_editable(editable)
        view.set_cursor_visible(editable)
        view.set_left_margin(10)
        view.set_right_margin(10)
        view.set_top_margin(8)
        view.set_bottom_margin(8)
        view.set_accepts_tab(False)
        return view

    def _set_idle_hint(self) -> None:
        if not self.cfg.api_key:
            self.hint.set_text(f"把 DeepSeek 的 API key 写进\n{self.cfg.api_key_path}")
            return
        self.hint.set_text(HINT_IDLE)

    def _apply_style(self) -> None:
        provider = Gtk.CssProvider()
        provider.load_from_data(b"""
            #pop-ai {
                background-color: #222222;
                border: 1px solid #3a3a3a;
                border-radius: 6px;
            }
            #pop-ai .title { color: #eeeeee; font-weight: bold; }
            #pop-ai .hint { color: #999999; }
            #pop-ai .resize-edge { background-color: #222222; }
            #pop-ai .drag-bar {
                background-color: #1a1a1a;
                border-bottom: 2px solid #F9690E;
            }
            #pop-ai textview, #pop-ai textview text {
                background-color: #222222;
                color: #eeeeee;
            }
            #pop-ai textview text selection {
                background-color: #d6e4ff;
                color: #111111;
            }
            #pop-ai-menu {
                background-color: #f4f4f4;
                color: #111111;
            }
            #pop-ai-menu menuitem,
            #pop-ai-menu menuitem label {
                background-color: transparent;
                color: #111111;
            }
            #pop-ai-menu menuitem:hover,
            #pop-ai-menu menuitem:hover label {
                background-color: #d6e4ff;
                color: #111111;
            }
            #pop-ai separator,
            #pop-ai paned > separator {
                background-color: #3a3a3a;
                background-image: none;
                min-height: 8px;
            }
            #pop-ai paned > separator:hover {
                background-color: #F9690E;
            }
            #pop-ai button {
                background: transparent;
                border: none;
                border-radius: 4px;
                padding: 0;
                color: #dddddd;
            }
            #pop-ai button.close {
                min-width: 38px;
                min-height: 38px;
                font-size: 20px;
            }
            #pop-ai button:hover { background-color: #2e2e2e; }
        """)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(),
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )
        self._font = Pango.FontDescription.from_string(self.cfg.font)

    def present(self) -> None:
        global _live
        self.title.override_font(self._font)
        self.hint.override_font(self._font)
        self.transcript.override_font(self._font)
        self.composer.override_font(self._font)
        if not self._presented:
            self._presented = True
            _live += 1
        self._fit_on_screen()
        self.window.show_all()
        self._install_drag_cursor()
        self._focus_input()
        GLib.idle_add(self._fit_on_screen)
        GLib.idle_add(self._focus_input)
        if Gtk.main_level() == 0:
            Gtk.main()

    def _focus_input(self) -> bool:
        """Ask the window manager for the keyboard, then put the cursor in the input."""
        if self.closed:
            return False
        gdk_window = self.window.get_window()
        if gdk_window is not None:
            timestamp = Gdk.CURRENT_TIME
            try:
                from gi.repository import GdkX11

                timestamp = GdkX11.x11_get_server_time(gdk_window)
            except Exception:
                timestamp = Gdk.CURRENT_TIME
            self.window.present_with_time(timestamp)
        self.composer.grab_focus()
        return False

    def _transcript_text(self) -> str:
        buf = self.transcript.get_buffer()
        return buf.get_text(buf.get_start_iter(), buf.get_end_iter(), True)

    def _composer_text(self) -> str:
        buf = self.composer.get_buffer()
        return buf.get_text(buf.get_start_iter(), buf.get_end_iter(), True)

    def _set_transcript(self, text: str) -> None:
        buf = self.transcript.get_buffer()
        buf.set_text(text)
        buf.place_cursor(buf.get_end_iter())
        GLib.idle_add(self._scroll_transcript)

    def _set_composer(self, text: str) -> None:
        buf = self.composer.get_buffer()
        buf.set_text(text)
        buf.place_cursor(buf.get_end_iter())

    def _scroll_transcript(self) -> bool:
        if self.closed:
            return False
        buf = self.transcript.get_buffer()
        self.transcript.scroll_to_mark(buf.get_insert(), 0.0, True, 0.0, 1.0)
        return False

    def _on_destroy(self, _widget) -> None:
        global _live
        self.closed = True
        self.generation += 1
        self._end_drag()
        if self._presented:
            self._presented = False
            _live -= 1
            if _live <= 0 and Gtk.main_level() > 0:
                Gtk.main_quit()

    def _finish(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.generation += 1
        self._end_drag()
        self.window.destroy()

    def _on_button(self, _widget, event) -> bool:
        if self._drag_offset is not None:
            return True
        return False

    def _install_drag_cursor(self) -> None:
        gdk_window = self.drag_bar.get_window()
        display = self.window.get_display()
        if gdk_window is None or display is None:
            return
        cursor = Gdk.Cursor.new_from_name(display, "move")
        if cursor is None:
            cursor = Gdk.Cursor.new_for_display(display, Gdk.CursorType.FLEUR)
        gdk_window.set_cursor(cursor)

    def _on_drag_press(self, _widget, event) -> bool:
        if event.button != 1 or event.type != Gdk.EventType.BUTTON_PRESS:
            return False
        origin_x, origin_y = self.window.get_position()
        self._drag_offset = (event.x_root - origin_x, event.y_root - origin_y)
        if not self._grab_pointer(event.time):
            self._drag_offset = None
        return True

    def _grab_pointer(self, time_ms: int) -> bool:
        display = Gdk.Display.get_default()
        seat = display.get_default_seat() if display is not None else None
        pointer = seat.get_pointer() if seat is not None else None
        gdk_window = self.window.get_window()
        if pointer is None or gdk_window is None:
            return False
        mask = Gdk.EventMask.BUTTON_RELEASE_MASK | Gdk.EventMask.POINTER_MOTION_MASK
        status = pointer.grab(
            gdk_window,
            Gdk.GrabOwnership.APPLICATION,
            False,
            mask,
            None,
            time_ms,
        )
        if status != Gdk.GrabStatus.SUCCESS:
            return False
        self._pointer_grabbed = True
        return True

    def _on_drag_motion(self, _widget, event) -> bool:
        if self._drag_offset is None:
            return False
        origin_x, origin_y = self._drag_offset
        next_x = int(round(event.x_root - origin_x))
        next_y = int(round(event.y_root - origin_y))
        if (next_x, next_y) != self.window.get_position():
            self.user_moved = True
            self.window.move(next_x, next_y)
        return True

    def _on_drag_release(self, _widget, event) -> bool:
        if self._drag_offset is None or event.button != 1:
            return False
        self._end_drag(event.time)
        return True

    def _end_drag(self, time_ms: int = Gdk.CURRENT_TIME) -> None:
        grabbed = self._pointer_grabbed
        self._drag_offset = None
        self._pointer_grabbed = False
        if not grabbed:
            return
        display = Gdk.Display.get_default()
        seat = display.get_default_seat() if display is not None else None
        pointer = seat.get_pointer() if seat is not None else None
        if pointer is not None:
            pointer.ungrab(time_ms)

    def _on_size_allocate(self, _widget, allocation) -> None:
        self._round_corners(allocation.width, allocation.height)

    def _round_corners(self, width: int, height: int) -> None:
        if (width, height) == self._shaped or width < 4 or height < 4:
            return
        gdk_window = self.window.get_window()
        if gdk_window is None:
            return
        try:
            import cairo

            radius = min(6, width / 2, height / 2)
            surface = cairo.ImageSurface(cairo.FORMAT_A8, width, height)
            context = cairo.Context(surface)
            context.new_sub_path()
            context.arc(width - radius, radius, radius, -math.pi / 2, 0)
            context.arc(width - radius, height - radius, radius, 0, math.pi / 2)
            context.arc(radius, height - radius, radius, math.pi / 2, math.pi)
            context.arc(radius, radius, radius, math.pi, 3 * math.pi / 2)
            context.close_path()
            context.set_source_rgba(0, 0, 0, 1)
            context.fill()
            region = Gdk.cairo_region_create_from_surface(surface)
            gdk_window.shape_combine_region(region, 0, 0)
        except Exception:
            return
        self._shaped = (width, height)

    def _fit_on_screen(self) -> bool:
        """Keep the whole window, including the input, inside the monitor work area."""
        if self.closed or self.user_moved:
            return False
        display = Gdk.Display.get_default()
        if display is None:
            return False
        px, py = self.pointer
        monitor = display.get_monitor_at_point(px, py)
        if monitor is None:
            return False
        area = monitor.get_workarea()
        if area.width < 2 or area.height < 2:
            area = monitor.get_geometry()
        margin = 8
        avail_w = max(320, area.width - margin * 2)
        avail_h = max(220, area.height - margin * 2)
        if self.window.get_mapped():
            width, height = self.window.get_size()
        else:
            width, height = self.window.get_default_size()
        if width < 2 or height < 2:
            width, height = self.cfg.width, self.cfg.height
        width = min(width, avail_w)
        height = min(height, avail_h)
        self.window.resize(width, height)
        ax, ay = px + 16, py + 16
        x = min(max(ax, area.x + margin), area.x + margin + max(0, avail_w - width))
        y = min(max(ay, area.y + margin), area.y + margin + max(0, avail_h - height))
        self.window.move(x, y)
        return False

    def _enter_action(self, event) -> str:
        """Give Enter to the input method, then decide what this process does.

        A real composition commits characters and must stay in the box.
        With no composition, the same call commits a newline; that newline
        is removed and Enter sends the draft instead.
        """
        buf = self.composer.get_buffer()
        before = self._composer_text()
        cursor = buf.get_iter_at_mark(buf.get_insert()).get_offset()
        handled = bool(self.composer.im_context_filter_keypress(event))
        after = self._composer_text()
        if not handled:
            return "send"
        if after == before:
            return "ignore"
        if after == before[:cursor] + "\n" + before[cursor:]:
            start = buf.get_iter_at_offset(cursor)
            end = buf.get_iter_at_offset(cursor + 1)
            if buf.get_text(start, end, True) == "\n":
                buf.delete(start, end)
            return "send"
        return "commit"

    def _on_key(self, widget, event) -> bool:
        key = event.keyval
        if key == Gdk.KEY_Escape:
            self._finish()
            return True
        if key not in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            return False
        in_composer = widget is self.composer or self.composer.has_focus()
        if event.state & Gdk.ModifierType.SHIFT_MASK:
            if in_composer:
                self.composer.get_buffer().insert_at_cursor("\n")
            return True
        if in_composer:
            action = self._enter_action(event)
            if action == "send" and not self.loading:
                self._submit()
            return True
        if self.loading:
            return True
        self._submit()
        return True

    def _submit(self) -> None:
        if self.loading or self.closed:
            return
        prompt = self._composer_text().strip()
        if not prompt:
            return
        command = parse_command(prompt)
        if command is not None:
            self._run_command(command[0], command[1])
            self._set_composer("")
            return
        if not self.cfg.api_key:
            self._set_idle_hint()
            return
        if len(prompt) > self.cfg.max_chars:
            self.hint.set_text(f"超过 {self.cfg.max_chars} 字，未发送。")
            return
        updated, start, end = append_question(self._transcript_text(), prompt)
        self.waiting = (start, end)
        self.loading = True
        self._set_transcript(updated)
        self._set_composer("")
        self.hint.set_text(HINT_WAIT)
        self.messages.append({"role": "user", "content": prompt})
        self.generation += 1
        generation = self.generation
        messages = list(self.messages)
        cfg = self.cfg

        def work() -> None:
            try:
                reply, calls, annotations = complete(cfg, messages)
                shown = plain_reply(reply)
                note = source_note(annotations)
                if note:
                    shown = shown + "\n\n" + note
                payload = ("ok", shown, assistant_message(plain_reply(reply), calls))
            except (DeepSeekError, OSError, RuntimeError) as exc:
                payload = ("err", str(exc))
            except Exception as exc:
                payload = ("err", str(exc) or "请求失败")
            GLib.idle_add(self._on_result, generation, payload)

        threading.Thread(target=work, daemon=True).start()

    def _run_command(self, name: str, argument: str) -> None:
        if name == "clear":
            self.messages.clear()
            self.waiting = None
            self._set_transcript("")
            self._set_idle_hint()
            return
        transcript = self._transcript_text().strip()
        if not transcript:
            self.hint.set_text("没有可保存的内容\n" + HINT_IDLE)
            return
        from pop_ai.deepseek import local_now

        directory = state_dir() / "sessions"
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        path = session_file(directory, argument, local_now())
        path.write_text(transcript + "\n", encoding="utf-8")
        self.hint.set_text(f"已保存到 {path}\n/save 保存会话，/clear 清除会话")

    def _on_result(self, generation: int, payload: tuple) -> bool:
        if self.closed or generation != self.generation or self.waiting is None:
            return False
        start, end = self.waiting
        self.waiting = None
        self.loading = False
        kind = payload[0]
        if kind != "ok":
            if self.messages and self.messages[-1]["role"] == "user":
                self.messages.pop()
            body = payload[1] or "请求失败"
        else:
            body = payload[1]
            self.messages.append(payload[2])
        try:
            updated = finish_turn(self._transcript_text(), start, end, body)
        except ValueError:
            updated = self._transcript_text() + "\n" + plain_reply(body)
        self._set_transcript(updated)
        self._set_idle_hint()
        if not self.composer.has_focus():
            self.composer.grab_focus()
        return False


def open_popup(cfg: Config, initial: str) -> None:
    Popup(cfg, initial).present()


def available() -> bool:
    return bool(os.environ.get("DISPLAY"))
