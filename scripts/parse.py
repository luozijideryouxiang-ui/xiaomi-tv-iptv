"""Parse M3U and simple TXT IPTV playlist bodies into channel candidates."""

from __future__ import annotations

import re
from urllib.parse import urlsplit


MAX_PLAYLIST_ENTRIES = 50_000
_ATTRIBUTE = re.compile(
    r"([\w-]+)\s*=\s*(?:\"((?:\\.|[^\"])*)\"|'((?:\\.|[^'])*)'|(\S+))"
)
_TEST_NAME = re.compile(r"(?:^|[\s._-])(?:test|testing)(?:$|[\s._-])|测试", re.I)


def parse_playlist(text: str, source: str, priority: int = 100) -> list[dict]:
    """Parse playlist rows, keeping only named HTTP(S) stream candidates."""
    if not isinstance(text, str) or not text:
        return []

    candidates: list[dict] = []
    pending: dict | None = None
    current_group: str | None = None

    for raw_line in text.splitlines():
        line = raw_line.strip().lstrip("\ufeff").strip()
        if not line:
            continue

        if line.upper().startswith("#EXTINF:"):
            pending = _parse_extinf(line[len("#EXTINF:") :])
            continue

        if pending is not None:
            if line.startswith("#"):
                continue
            candidate_url = _clean_url(line)
            if candidate_url:
                candidate = _candidate(
                    name=pending.get("name") or pending.get("tvg_name") or "",
                    url=candidate_url,
                    source=source,
                    priority=priority,
                    group=pending.get("group"),
                    tvg_id=pending.get("tvg_id"),
                    tvg_name=pending.get("tvg_name"),
                    logo=pending.get("logo"),
                )
                if candidate is not None:
                    candidates.append(candidate)
                    if len(candidates) >= MAX_PLAYLIST_ENTRIES:
                        break
            pending = None
            continue

        # M3U comments and directives are metadata; only EXTINF rows create candidates.
        if line.startswith(("#", "//")):
            continue
        if "," not in line:
            continue

        name, value = line.split(",", 1)
        name = name.strip()
        value = value.strip()
        if value.lower() == "#genre#":
            current_group = name or None
            continue

        candidate_url = _clean_url(value)
        if not candidate_url:
            continue
        candidate = _candidate(
            name=name,
            url=candidate_url,
            source=source,
            priority=priority,
            group=current_group,
        )
        if candidate is not None:
            candidates.append(candidate)
            if len(candidates) >= MAX_PLAYLIST_ENTRIES:
                break

    return candidates


def _parse_extinf(payload: str) -> dict:
    comma = _first_unquoted_comma(payload)
    if comma < 0:
        return {}
    metadata = payload[:comma]
    title = payload[comma + 1 :].strip()
    attributes: dict[str, str] = {}
    for match in _ATTRIBUTE.finditer(metadata):
        key = match.group(1).lower()
        value = next((part for part in match.groups()[1:] if part is not None), "")
        attributes[key] = value.replace(r"\"", '"').replace(r"\'", "'")
    return {
        "name": title,
        "tvg_id": attributes.get("tvg-id"),
        "tvg_name": attributes.get("tvg-name"),
        "logo": attributes.get("tvg-logo"),
        "group": attributes.get("group-title"),
    }


def _first_unquoted_comma(value: str) -> int:
    quote: str | None = None
    escaped = False
    for index, char in enumerate(value):
        if escaped:
            escaped = False
        elif char == "\\" and quote is not None:
            escaped = True
        elif quote is not None and char == quote:
            quote = None
        elif quote is None and char in {"'", '"'}:
            quote = char
        elif quote is None and char == ",":
            return index
    return -1


def _clean_url(value: str) -> str | None:
    value = value.strip().strip("\"'")
    if not value or any(char.isspace() or ord(char) < 32 for char in value):
        return None
    try:
        parts = urlsplit(value)
        if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
            return None
        # Accessing .port also catches malformed ports while retaining URL credentials.
        _ = parts.port
    except ValueError:
        return None
    return value


def _candidate(
    *,
    name: str,
    url: str,
    source: str,
    priority: int,
    group: str | None = None,
    tvg_id: str | None = None,
    tvg_name: str | None = None,
    logo: str | None = None,
) -> dict | None:
    name = name.strip()
    if not name or _TEST_NAME.search(name):
        return None
    try:
        priority = int(priority)
    except (TypeError, ValueError):
        priority = 100
    result = {
        "name": name,
        "url": url,
        "source": source,
        "priority": priority,
        "tvg_id": tvg_id,
        "tvg_name": tvg_name,
        "logo": logo,
    }
    if group:
        result["group"] = group.strip()
    return result
