"""Render small, deterministic playlists and refuse duplicate channel entries."""
from __future__ import annotations

import math
import re
from pathlib import Path
from urllib.parse import urlsplit

GROUPS = ["央视", "卫视", "广东", "汕头", "地方", "其他"]


def rank(record: dict) -> tuple:
    """Short observation only: retained observations rank after current successes."""
    first = record.get("first_frame_ms")
    height = (record.get("resolution") or [0, 0])[1]
    quality = 0 if height == 1080 else 1 if height >= 720 else 2
    return (
        bool(record.get("retained")),
        record.get("stalls", 0),
        -min(record.get("sample_seconds", 0), 3),
        first if isinstance(first, (int, float)) else math.inf,
        quality,
        -(record.get("bitrate_kbps") or 0),
        record.get("priority", 100),
        record["url"],
    )


def select(records: list[dict], channels: list[dict], family: str | None = None) -> list[dict]:
    grouped: dict[str, list[dict]] = {}
    for item in records:
        if item.get("ok") and (family is None or item.get("family") == family):
            grouped.setdefault(item["name"], []).append(item)
    selected = []
    for channel in channels:
        unique = {}
        for item in sorted(grouped.get(channel["name"], []), key=rank):
            unique.setdefault(item["url"], item)
        choices = list(unique.values())[:3]
        if choices:
            selected.append({**choices[0], **channel, "alternates": choices[1:]})
    return selected


def _sort(channel: dict) -> tuple:
    group = channel.get("group", "其他")
    match = re.match(r"CCTV-(\d+)(\+)?", channel["name"])
    number = (int(match[1]), bool(match[2])) if match else (99, False)
    return (GROUPS.index(group) if group in GROUPS else 99, number, channel.get("order", 999), channel["name"])


def _attribute(value: object) -> str:
    # Quoted EXTINF attributes do not have a universal backslash escape syntax.
    return str(value or "").replace('"', "'").replace("\r", " ").replace("\n", " ")


def render(channels: list[dict], epg_url: str = "", limit: int = 100) -> str:
    channels = sorted(channels, key=_sort)[:limit]
    if len({item["name"] for item in channels}) != len(channels):
        raise ValueError("Duplicate canonical channel names")
    header = '#EXTM3U' + (f' x-tvg-url="{_attribute(epg_url)}"' if epg_url else "")
    lines = [header]
    for item in channels:
        url = item["url"]
        if urlsplit(url).scheme not in {"http", "https"} or "\n" in url or "\r" in url:
            raise ValueError("Invalid stream URL")
        logo = item.get("logo", "")
        if logo and urlsplit(logo).scheme not in {"http", "https"}:
            logo = ""
        attrs = {
            "tvg-id": item.get("epg_id", item.get("tvg_id", "")),
            "tvg-name": item["name"],
            "tvg-logo": logo,
            "group-title": item.get("group", "其他"),
        }
        lines.append('#EXTINF:-1 ' + " ".join(f'{key}="{_attribute(value)}"' for key, value in attrs.items()) + ',' + item["name"])
        lines.append(url)
    return "\n".join(lines) + "\n"


def validate(text: str) -> int:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines or not lines[0].startswith("#EXTM3U"):
        raise ValueError("Missing M3U header")
    names = []
    expect_url = False
    for line in lines[1:]:
        if line.startswith("#EXTINF:"):
            if expect_url:
                raise ValueError("Channel missing stream URL")
            names.append(line.rsplit(",", 1)[-1])
            for key in ("tvg-id", "tvg-name", "tvg-logo", "group-title"):
                if f'{key}="' not in line:
                    raise ValueError(f"Missing {key}")
            expect_url = True
        elif not line.startswith("#"):
            if not expect_url or urlsplit(line).scheme not in {"http", "https"}:
                raise ValueError("Unexpected/invalid stream URL")
            expect_url = False
    if expect_url or len(names) != len(set(names)):
        raise ValueError("Incomplete or duplicate playlist")
    return len(names)
