"""A small TOML reader for the subset this tool writes.

Supported: sections, comments, bools, ints, floats, quoted strings,
triple-quoted strings, and arrays of those values. No inline tables,
dates, or dotted keys.
"""

from __future__ import annotations

import re


class TomlError(ValueError):
    pass


def loads(text: str) -> dict[str, dict]:
    data: dict[str, dict] = {}
    section: dict | None = None
    i = 0
    length = len(text)

    def skip_ws_and_comments(pos: int) -> int:
        while pos < length:
            char = text[pos]
            if char in " \t\r\n":
                pos += 1
                continue
            if char == "#":
                newline = text.find("\n", pos)
                pos = length if newline < 0 else newline + 1
                continue
            break
        return pos

    def parse_string(pos: int) -> tuple[str, int]:
        if text.startswith('"""', pos):
            pos += 3
            if pos < length and text[pos] == "\n":
                pos += 1
            end = text.find('"""', pos)
            if end < 0:
                raise TomlError("unterminated triple-quoted string")
            return text[pos:end], end + 3
        if text[pos] != '"':
            raise TomlError("expected a string")
        pos += 1
        chars: list[str] = []
        while pos < length:
            char = text[pos]
            if char == '"':
                return "".join(chars), pos + 1
            if char == "\\":
                if pos + 1 >= length:
                    raise TomlError("unterminated escape")
                escaped = text[pos + 1]
                mapping = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", '"': '"'}
                if escaped not in mapping:
                    raise TomlError(f"unsupported escape \\{escaped}")
                chars.append(mapping[escaped])
                pos += 2
                continue
            if char == "\n":
                raise TomlError("newline in a basic string")
            chars.append(char)
            pos += 1
        raise TomlError("unterminated string")

    def parse_value(pos: int) -> tuple[object, int]:
        pos = skip_ws_and_comments(pos)
        if pos >= length:
            raise TomlError("expected a value")
        if text[pos] == '"':
            return parse_string(pos)
        if text[pos] == "[":
            pos += 1
            items: list[object] = []
            while True:
                pos = skip_ws_and_comments(pos)
                if pos < length and text[pos] == "]":
                    return items, pos + 1
                item, pos = parse_value(pos)
                items.append(item)
                pos = skip_ws_and_comments(pos)
                if pos < length and text[pos] == ",":
                    pos += 1
                    continue
                if pos < length and text[pos] == "]":
                    continue
                raise TomlError("expected ',' or ']' in array")
        if text.startswith("true", pos) and not _ident_continues(text, pos + 4):
            return True, pos + 4
        if text.startswith("false", pos) and not _ident_continues(text, pos + 5):
            return False, pos + 5
        match = re.match(r"-?\d+(?:\.\d+)?", text[pos:])
        if not match:
            raise TomlError(f"unsupported value near {text[pos:pos + 16]!r}")
        raw = match.group(0)
        pos += len(raw)
        if "." in raw:
            return float(raw), pos
        return int(raw), pos

    while i < length:
        i = skip_ws_and_comments(i)
        if i >= length:
            break
        if text.startswith("[[", i):
            raise TomlError("arrays of tables are not supported")
        if text[i] == "[":
            end = text.find("]", i)
            if end < 0:
                raise TomlError("unterminated section")
            name = text[i + 1:end].strip()
            if not name:
                raise TomlError("empty section name")
            section = data.setdefault(name, {})
            i = end + 1
            continue
        match = re.match(r"[A-Za-z0-9_-]+", text[i:])
        if not match:
            raise TomlError(f"expected a key near {text[i:i + 16]!r}")
        key = match.group(0)
        i += len(key)
        while i < length and text[i] in " \t":
            i += 1
        if i >= length or text[i] != "=":
            raise TomlError(f"expected '=' after {key}")
        i += 1
        value, i = parse_value(i)
        if section is None:
            raise TomlError(f"key {key} is outside a section")
        section[key] = value
        while i < length and text[i] in " \t\r":
            i += 1
        if i < length and text[i] == "#":
            newline = text.find("\n", i)
            i = length if newline < 0 else newline + 1
        elif i < length and text[i] == "\n":
            i += 1
        elif i < length:
            raise TomlError(f"unexpected trailing text near {text[i:i + 16]!r}")
    return data


def _ident_continues(text: str, pos: int) -> bool:
    return pos < len(text) and (text[pos].isalnum() or text[pos] == "_")
