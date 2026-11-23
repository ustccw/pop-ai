"""Build the text that is shown and sent from the popup."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

WAITING = "处理中…"
SEPARATOR = "─" * 36
USER_MARK = "[me]"
MODEL_MARK = "[deepseek]"


def parse_command(text: str) -> tuple[str, str] | None:
    """Return a slash command typed alone in the input. Other text is a question."""
    stripped = text.strip()
    if stripped == "/clear":
        return ("clear", "")
    if stripped == "/save":
        return ("save", "")
    if stripped.startswith("/save "):
        return ("save", stripped[6:].strip())
    return None


def session_filename(name: str, moment: datetime) -> str:
    """A readable file name. The local date and time are always included."""
    stamp = moment.strftime("%Y%m%d-%H%M%S")
    cleaned = re.sub(r"[^\w.\-]+", "_", name.strip(), flags=re.UNICODE).strip("._")
    if not cleaned or cleaned in {".", ".."}:
        return f"session-{stamp}.txt"
    return f"{cleaned[:80]}-{stamp}.txt"


def session_file(directory: Path, name: str, moment: datetime) -> Path:
    return directory / session_filename(name, moment)


def initial_text(selection: str | None, prefix: str) -> str:
    """Prefix a real selection. Blank or missing text stays an empty draft."""
    if selection is None:
        return ""
    body = selection.strip()
    if not body:
        return ""
    label = prefix.strip()
    if not label:
        return body
    return f"{label}：“{body}”"


def source_note(annotations: list | None) -> str:
    """Plain list of cited pages. Empty when the reply has no citations."""
    if not annotations:
        return ""
    lines: list[str] = []
    seen: set[str] = set()
    for item in annotations:
        if not isinstance(item, dict):
            continue
        cite = item.get("url_citation")
        if not isinstance(cite, dict):
            continue
        url = cite.get("url")
        if not isinstance(url, str) or not url or url in seen:
            continue
        seen.add(url)
        title = cite.get("title")
        if isinstance(title, str) and title.strip() and title.strip() != url:
            lines.append(f"{len(lines) + 1}. {title.strip()} {url}")
        else:
            lines.append(f"{len(lines) + 1}. {url}")
    if not lines:
        return ""
    return "来源：\n" + "\n".join(lines)


def plain_reply(text: str) -> str:
    """Drop Markdown markers. Lists become numbered dots, headings become lines."""
    lines: list[str] = []
    for raw in text.strip().splitlines():
        line = raw.rstrip()
        if line.strip().startswith("```"):
            continue
        line = re.sub(r"^#{1,6}\s*", "", line)
        line = re.sub(r"^(\s*)[-*+]\s+", r"\1· ", line)
        line = line.replace("**", "").replace("__", "")
        lines.append(line)
    return "\n".join(lines).strip()


def append_question(transcript: str, question: str) -> tuple[str, int, int]:
    """Append a question, a rule, and the waiting line.

    A later question is also separated from the previous reply by a rule.
    The returned offsets cover the waiting line.
    """
    question = question.strip()
    if transcript.strip():
        base = transcript.rstrip("\n") + "\n" + SEPARATOR + "\n"
    else:
        base = ""
    head = base + USER_MARK + "\n" + question + "\n" + SEPARATOR + "\n" + MODEL_MARK + "\n"
    updated = head + WAITING
    return updated, len(head), len(updated)


def finish_turn(text: str, start: int, end: int, body: str) -> str:
    """Replace the waiting line with the plain reply."""
    if text[start:end] != WAITING:
        raise ValueError("waiting marker missing")
    return text[:start] + plain_reply(body) + text[end:]
