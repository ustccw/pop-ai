"""DeepSeek chat completions. The reply is plain text, not JSON."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime

from pop_ai.config import Config


class DeepSeekError(RuntimeError):
    pass


SEARCH_NOTE = (
    "When a question needs current facts, search the web and answer from the results. "
    "Open a link in the question when its contents matter. "
    "Do not claim that you cannot access the internet. "
    "The latest_reminder before the user message is today's date in the user's timezone. "
    "Count today and the next few days from that date, not from memory."
)

WEEKDAYS = "一二三四五六日"

WEB_TOOLS = (
    {"type": "deepseek_web_search"},
    {"type": "deepseek_read_url"},
)


def request_body(
    model: str,
    thinking: bool,
    system: str,
    messages: list[dict[str, str]],
    web_search: bool = False,
) -> dict:
    body = {
        "model": model,
        "messages": [{"role": "system", "content": system}, *messages],
        "stream": False,
        "thinking": {"type": "enabled" if thinking else "disabled"},
    }
    if web_search:
        body["tools"] = [dict(item) for item in WEB_TOOLS]
    return body


def local_now() -> datetime:
    """Current time in the machine timezone."""
    return datetime.now().astimezone()


def reminder_text(moment: datetime) -> str:
    """Date line DeepSeek expects: date, weekday, place, platform, language.

    Place is left empty. The clock comes from the machine timezone.
    """
    local = moment.astimezone()
    weekday = "星期" + WEEKDAYS[local.weekday()]
    return f"{local.date().isoformat()},{weekday},,Web,中文"


def with_latest_reminder(messages: list[dict], moment: datetime | None = None) -> list[dict]:
    """Put today's date immediately before the latest user message."""
    if not messages:
        return list(messages)
    last_user = -1
    for index, message in enumerate(messages):
        if message.get("role") == "user":
            last_user = index
    if last_user < 0:
        return list(messages)
    reminder = {
        "role": "latest_reminder",
        "content": reminder_text(moment or local_now()),
    }
    updated = list(messages)
    updated.insert(last_user, reminder)
    return updated


def assistant_message(content: str, server_tool_calls: list | None) -> dict[str, str | list]:
    """The assistant turn to send back. Tool calls must be echoed unchanged."""
    message: dict[str, str | list] = {"role": "assistant", "content": content}
    if server_tool_calls:
        message["server_tool_calls"] = server_tool_calls
    return message


def complete(cfg: Config, messages: list[dict[str, str]]) -> tuple[str, list | None, list | None]:
    if not cfg.api_key:
        raise DeepSeekError(f"把 DeepSeek 的 API key 写进 {cfg.api_key_path}")
    if not messages:
        raise DeepSeekError("没有可发送的内容")
    system = cfg.system
    if cfg.web_search:
        system = system + "\n\n" + SEARCH_NOTE
    body = request_body(
        cfg.model,
        cfg.thinking,
        system,
        with_latest_reminder(messages),
        cfg.web_search,
    )
    request = urllib.request.Request(
        cfg.base_url + "/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + cfg.api_key,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=cfg.timeout_sec) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace").strip().replace("\n", " ")
        if len(detail) > 240:
            detail = detail[:240]
        raise DeepSeekError(f"DeepSeek API 调用失败：{exc.code} {detail}") from exc
    except urllib.error.URLError as exc:
        raise DeepSeekError(f"DeepSeek API 调用失败：{exc.reason}") from exc
    except (json.JSONDecodeError, TimeoutError, OSError) as exc:
        raise DeepSeekError(f"DeepSeek API 调用失败：{exc}") from exc
    try:
        message = payload["choices"][0]["message"]
        content = message["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise DeepSeekError("DeepSeek 没有返回文本") from exc
    if not isinstance(content, str) or not content.strip():
        raise DeepSeekError("DeepSeek 返回了空内容")
    calls = message.get("server_tool_calls")
    annotations = message.get("annotations")
    if not isinstance(calls, list):
        calls = None
    if not isinstance(annotations, list):
        annotations = None
    return content.strip(), calls, annotations
