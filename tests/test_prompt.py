import unittest

from pop_ai.config import load_config
from pop_ai.deepseek import assistant_message, local_now, reminder_text, request_body, with_latest_reminder
from datetime import datetime

from pop_ai.prompt import (
    SEPARATOR,
    append_question,
    finish_turn,
    initial_text,
    parse_command,
    plain_reply,
    session_filename,
    source_note,
)
from pathlib import Path

from pop_ai.shortcuts import Conflict, conflict_message, display_hotkey, same_accelerator, without_accelerator
from pop_ai.windows import classify_surface
from pop_ai.x11util import (
    accept_copied_text,
    highlighted_text,
    is_unselected_line_copy,
    live_terminal_text,
    owner_is_within,
    primary_is_selection,
)


EDITABLE = ["code", "gedit", "gnome-terminal"]
READONLY = ["evince", "xterm"]


class CommandTests(unittest.TestCase):
    def test_only_exact_commands_are_recognized(self):
        self.assertEqual(parse_command("  /clear  "), ("clear", ""))
        self.assertEqual(parse_command("/save"), ("save", ""))
        self.assertEqual(parse_command("/save  会议 "), ("save", "会议"))
        self.assertIsNone(parse_command("/save请帮我"))
        self.assertIsNone(parse_command("请解释 /save"))

    def test_session_name_is_a_safe_file(self):
        moment = datetime(2026, 11, 23, 10, 30, 0)
        self.assertEqual(session_filename("", moment), "session-20261123-103000.txt")
        self.assertEqual(session_filename("../会议 记录", moment), "会议_记录-20261123-103000.txt")


class PromptTests(unittest.TestCase):
    def test_selection_is_prefixed(self):
        self.assertEqual(initial_text("  selected  ", "请解释"), "请解释：“selected”")
        self.assertEqual(initial_text("a\nb", "请解释"), "请解释：“a\nb”")

    def test_blank_selection_stays_empty(self):
        self.assertEqual(initial_text(None, "请解释"), "")
        self.assertEqual(initial_text(" \n\t", "请解释"), "")

    def test_empty_prefix_keeps_selection(self):
        self.assertEqual(initial_text("selected", "  "), "selected")

    def test_turns_append(self):
        question = initial_text("selected", "请解释")
        waiting, start, end = append_question("", question)
        self.assertEqual(waiting[start:end], "处理中…")
        replied = finish_turn(waiting, start, end, "因为甲")
        self.assertEqual(
            replied,
            "[me]\n" + question + "\n" + SEPARATOR + "\n[deepseek]\n因为甲",
        )

        waiting, start, end = append_question(replied, "再说细一点")
        replied = finish_turn(waiting, start, end, "**细节**\n- 一项")
        self.assertEqual(
            replied,
            "[me]\n"
            + question
            + "\n"
            + SEPARATOR
            + "\n[deepseek]\n因为甲\n"
            + SEPARATOR
            + "\n[me]\n再说细一点\n"
            + SEPARATOR
            + "\n[deepseek]\n细节\n· 一项",
        )

    def test_plain_reply_strips_markdown(self):
        raw = "**粗体** and __也粗__\n- 一项\n## 标题\n```\ncode\n```"
        self.assertEqual(plain_reply(raw), "粗体 and 也粗\n· 一项\n标题\ncode")


class ShortcutTests(unittest.TestCase):
    def test_super_p_is_removed_without_touching_other_keys(self):
        self.assertTrue(same_accelerator("<Super>p", "<Super>P"))
        self.assertFalse(same_accelerator("<Super>Page_Up", "<Super>p"))
        self.assertEqual(
            without_accelerator(["<Super>p", "XF86Display"], "<Super>p"),
            ["XF86Display"],
        )

    def test_conflict_message_offers_config_edit(self):
        conflict = Conflict("org.gnome.mutter.keybindings", "switch-monitor", "array", ["XF86Display"])
        text = conflict_message("<Super>p", [conflict], Path("/work/pop-ai/config.toml"))
        self.assertEqual(display_hotkey("<Super>p"), "Super+P")
        self.assertIn("切换显示器", text)
        self.assertIn("/work/pop-ai/config.toml", text)
        self.assertIn("[hotkeys] ask", text)


class SelectionTests(unittest.TestCase):
    def test_editor_line_copy_is_not_a_selection(self):
        self.assertTrue(is_unselected_line_copy("当前这一行\n", ""))
        self.assertTrue(is_unselected_line_copy("当前这一行\n", "别处留下的选区"))

    def test_matching_primary_is_a_selection(self):
        self.assertFalse(is_unselected_line_copy("当前这一行\n", "当前这一行"))
        self.assertFalse(is_unselected_line_copy("划选的词", ""))
        self.assertFalse(is_unselected_line_copy("第一行\n第二行\n", ""))

    def test_clipboard_helper_is_outside_the_focused_window(self):
        root = 1
        focused = 10
        parents = {12: 11, 11: focused, focused: root, 20: root}
        self.assertTrue(owner_is_within(12, focused, parents, root))
        self.assertTrue(owner_is_within(focused, focused, parents, root))
        self.assertFalse(owner_is_within(20, focused, parents, root))
        self.assertFalse(owner_is_within(0, focused, parents, root))

    def test_clipboard_left_in_primary_is_not_a_selection(self):
        self.assertFalse(primary_is_selection("剪贴板", "剪贴板", True, False))
        self.assertFalse(primary_is_selection("剪贴板\n", "剪贴板", True, False))
        self.assertFalse(primary_is_selection("  \n", "剪贴板", True, True))

    def test_highlighted_text_is_a_selection(self):
        self.assertTrue(primary_is_selection("划选的句子", "剪贴板", True, False))
        self.assertTrue(primary_is_selection("剪贴板", "剪贴板", True, True))
        self.assertTrue(primary_is_selection("划选的句子", "", False, False))

    def test_untouched_clipboard_is_not_a_copy(self):
        self.assertFalse(accept_copied_text("剪贴板", "剪贴板", True, False))
        self.assertTrue(accept_copied_text("剪贴板", "剪贴板", True, True))
        self.assertTrue(accept_copied_text("划选的句子", "剪贴板", True, False))

    def test_terminal_without_highlight_is_empty(self):
        self.assertIsNone(highlighted_text(0, "上次选中的文字"))
        self.assertIsNone(highlighted_text(1, "  \n"))
        self.assertEqual(highlighted_text(1, "当前高亮"), "当前高亮")

    def test_confirmed_highlight_can_come_from_primary(self):
        self.assertEqual(live_terminal_text("划选的句子", "别的", False), "划选的句子")
        self.assertEqual(live_terminal_text("", "划选的句子", True), "划选的句子")
        self.assertIsNone(live_terminal_text("", "剪贴板", False))
        self.assertIsNone(live_terminal_text(" \n", " \n", True))


class WindowTests(unittest.TestCase):
    def test_editable_window_copies_with_keyboard(self):
        self.assertEqual(classify_surface("code", "Code", EDITABLE, READONLY), "editable")

    def test_terminal_never_receives_ctrl_c(self):
        self.assertEqual(
            classify_surface("gnome-terminal", "Gnome-terminal", EDITABLE, READONLY),
            "terminal",
        )
        self.assertEqual(
            classify_surface("gnome-terminal-server", "Gnome-terminal", EDITABLE, READONLY),
            "terminal",
        )

    def test_unknown_window_reads_primary(self):
        self.assertEqual(classify_surface("evince", "Evince", EDITABLE, READONLY), "readonly")
        self.assertEqual(classify_surface("other", "Other", EDITABLE, READONLY), "readonly")
        self.assertEqual(classify_surface("", "", EDITABLE, READONLY), "readonly")


class RequestTests(unittest.TestCase):
    def test_history_is_plain_text(self):
        body = request_body(
            "deepseek-flash",
            False,
            "Answer directly.",
            [
                {"role": "user", "content": "请解释\nfoo"},
                {"role": "assistant", "content": "foo 的意思"},
                {"role": "user", "content": "再说细一点"},
            ],
            web_search=True,
        )
        self.assertNotIn("response_format", body)
        self.assertNotIn("tool_choice", body)
        self.assertEqual(body["thinking"], {"type": "disabled"})
        self.assertEqual(
            body["tools"],
            [{"type": "deepseek_web_search"}, {"type": "deepseek_read_url"}],
        )
        self.assertEqual(body["messages"][0]["role"], "system")
        self.assertEqual(body["messages"][-1]["content"], "再说细一点")
        self.assertEqual(len(body["messages"]), 4)
        quiet = request_body("deepseek-flash", False, "Answer directly.", [], web_search=False)
        self.assertNotIn("tools", quiet)

    def test_assistant_turn_keeps_server_tool_calls(self):
        calls = [{"id": "srvtoolu_1", "type": "deepseek_web_search"}]
        message = assistant_message("明天多云。", calls)
        self.assertEqual(message["server_tool_calls"], calls)
        self.assertNotIn("server_tool_calls", assistant_message("没有搜索。", None))

    def test_reminder_uses_the_local_clock(self):
        moment = local_now()
        date, weekday, location, platform, language = reminder_text(moment).split(",", 4)
        self.assertEqual(date, moment.date().isoformat())
        self.assertEqual(weekday, "星期" + "一二三四五六日"[moment.weekday()])
        self.assertEqual(location, "")
        self.assertEqual(platform, "Web")
        self.assertEqual(language, "中文")
        messages = [
            {"role": "user", "content": "第一问"},
            {"role": "assistant", "content": "第一答"},
            {"role": "user", "content": "未来三天天气"},
        ]
        updated = with_latest_reminder(messages, moment)
        self.assertEqual(updated[-2]["role"], "latest_reminder")
        self.assertEqual(updated[-2]["content"], reminder_text(moment))
        self.assertEqual(updated[-1]["content"], "未来三天天气")
        self.assertEqual(updated[0]["role"], "user")

    def test_source_note_lists_citations(self):
        note = source_note([
            {"type": "url_citation", "url_citation": {"url": "https://example.com/a", "title": "甲"}},
            {"type": "url_citation", "url_citation": {"url": "https://example.com/a", "title": "甲"}},
            {"type": "url_citation", "url_citation": {"url": "https://example.com/b", "title": ""}},
        ])
        self.assertEqual(note, "来源：\n1. 甲 https://example.com/a\n2. https://example.com/b")

    def test_bundled_config(self):
        cfg = load_config()
        self.assertEqual(cfg.prefix, "请解释")
        self.assertEqual(cfg.hotkey, "<Super>p")
        self.assertEqual(cfg.model, "deepseek-flash")
        self.assertFalse(cfg.thinking)
        self.assertTrue(cfg.web_search)
        self.assertIn("cursor", [item.lower() for item in cfg.editable_wm_class])
        self.assertIn("gnome-terminal", [item.lower() for item in cfg.readonly_wm_class])
        self.assertNotIn("{", cfg.system)
        self.assertIn("plain text", cfg.system.lower())
        self.assertEqual(cfg.width, 1014)
        self.assertEqual(cfg.height, 702)
