"""Whitelist IPTV candidates against the configured channel catalogue."""

from __future__ import annotations

import re
import unicodedata
from typing import Any


_QUALITY_WORDS = r"(?:UHD|HD|4K|SD|超高清|高清|标清)"
_RESOLUTION = r"(?:\d{3,4}[pi]|\d{3,4}x\d{3,4})"
# These suffixes are metadata only when they are at the end of a title. In
# particular, '+' is deliberately absent from the separators so CCTV-5+ stays
# distinct from CCTV-5.
_QUALITY_SUFFIX = re.compile(
    rf"[\s\[\](){{}}._:/\\-]*(?:{_QUALITY_WORDS})[\s\[\](){{}}.,!?;:\\-]*$",
    re.I,
)
_RESOLUTION_SUFFIX = re.compile(
    rf"(?:\s*\(\s*{_RESOLUTION}\s*\)|\s*\[\s*{_RESOLUTION}\s*\]|\s+{_RESOLUTION})\s*$",
    re.I,
)
_CCTV_SUFFIXES = (
    "中文国际", "财经", "综艺", "体育赛事", "体育", "奥林匹克", "国防军事", "國防軍事",
    "军事", "农业农村", "农业", "科教", "戏曲", "社会与法", "新闻", "少儿", "音乐",
    "纪录", "法治", "电视剧", "综合",
)
_CCTV_SUFFIX_PATTERN = "|".join(_CCTV_SUFFIXES)
_CCTV_CANONICAL = re.compile(
    rf"^cctv0*(\d{{1,2}})(\+)?(?:{_CCTV_SUFFIX_PATTERN})?$"
)
_CCTV_NAME = re.compile(
    rf"^cctv0*(\d{{1,2}})(\+|plus)?(?:{_CCTV_SUFFIX_PATTERN})?$"
)
_CHINESE_NUMBERS = {
    1: "一", 2: "二", 3: "三", 4: "四", 5: "五", 6: "六", 7: "七", 8: "八", 9: "九",
    10: "十", 11: "十一", 12: "十二", 13: "十三", 14: "十四", 15: "十五", 16: "十六", 17: "十七",
}
def normalize_candidates(candidates: list[dict], channels: list[dict], aliases: dict) -> list[dict]:
    """Keep only exact canonical/alias matches and merge duplicate canonical streams.

    ``aliases`` is the contents of the aliases mapping itself, keyed by canonical
    channel name. Unknown names never pass through, even if they share a substring
    with a known channel.
    """
    channel_by_name: dict[str, dict] = {}
    canonical_by_token: dict[str, str] = {}
    for channel in channels:
        if not isinstance(channel, dict):
            continue
        name = channel.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        canonical = name.strip()
        channel_by_name[canonical] = channel
        token = _name_token(canonical)
        if token:
            canonical_by_token[token] = canonical

    if not channel_by_name:
        return []

    name_to_canonical = dict(canonical_by_token)
    for canonical, channel in channel_by_name.items():
        for synonym in _as_names(channel.get("epg_names")):
            _add_alias(name_to_canonical, synonym, canonical)

    if isinstance(aliases, dict):
        for alias_canonical, synonyms in aliases.items():
            canonical = canonical_by_token.get(_name_token(alias_canonical)) if isinstance(alias_canonical, str) else None
            if canonical is None:
                continue
            for synonym in _as_names(synonyms):
                _add_alias(name_to_canonical, synonym, canonical)

    cctv_number_to_canonical: dict[tuple[int, bool], str] = {}
    for canonical in channel_by_name:
        match = _CCTV_CANONICAL.fullmatch(_name_token(canonical))
        if match:
            number = int(match.group(1))
            is_plus = bool(match.group(2))
            cctv_number_to_canonical[(number, is_plus)] = canonical
            _add_cctv_fallback_aliases(name_to_canonical, canonical, number, is_plus)

    # Preserve preferred sources by considering lower numeric priority first.
    ordered = sorted(
        (candidate for candidate in candidates if isinstance(candidate, dict)),
        key=lambda candidate: _priority(candidate.get("priority", 100)),
    )
    normalized: list[dict] = []
    index_by_key: dict[tuple[str, str], int] = {}
    for candidate in ordered:
        canonical = _resolve_candidate(candidate, name_to_canonical, cctv_number_to_canonical)
        if canonical is None:
            continue
        url = candidate.get("url")
        if not isinstance(url, str) or not url.strip():
            continue
        channel = channel_by_name[canonical]
        key = (canonical, _url_key(url))
        existing_index = index_by_key.get(key)
        if existing_index is not None:
            existing = normalized[existing_index]
            _merge_sources(existing, candidate)
            _merge_source_metadata(existing, candidate)
            continue

        result = dict(candidate)
        result["name"] = canonical
        result["url"] = url.strip()
        result["group"] = channel.get("group", "")
        if "elderly" in channel:
            result["elderly"] = channel["elderly"]
        if "epg_names" in channel:
            result["epg_names"] = channel["epg_names"]
        if channel.get("logo"):
            result["logo"] = channel["logo"]
        result["priority"] = _priority(candidate.get("priority", 100))
        result["sources"] = _sources_for(candidate)
        result["source_metadata"] = _source_metadata_for(candidate)
        index_by_key[key] = len(normalized)
        normalized.append(result)

    return normalized


def _name_token(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    value = unicodedata.normalize("NFKC", value).strip().casefold()
    value = re.sub(
        r"(^|[,\s])(?:UHD|HD|4K|SD|超高清|高清|标清)(?=$|[,\s])",
        r"\1 ",
        value,
        flags=re.I,
    )
    while value:
        without_suffix = _RESOLUTION_SUFFIX.sub("", value).strip()
        without_suffix = _QUALITY_SUFFIX.sub("", without_suffix).strip()
        if without_suffix == value:
            break
        value = without_suffix
    return "".join(
        char for char in value
        if char == "+" or (
            not unicodedata.category(char).startswith(("P", "S", "Z"))
            and not char.isspace()
        )
    )


def _tvg_id_token(value: Any) -> str:
    """Normalize the bounded country and quality suffixes used in TVG IDs."""
    if not isinstance(value, str):
        return ""
    value = unicodedata.normalize("NFKC", value).strip()
    value = re.sub(
        r"@(?:SD|HD|UHD|FHD|4K|\d{3,4}[pi])$",
        "",
        value,
        flags=re.I,
    )
    value = re.sub(r"\.(?:cn|china)$", "", value, flags=re.I)
    return _name_token(value)


def _as_names(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple, set)):
        return [item for item in value if isinstance(item, str) and item.strip()]
    return []


def _add_alias(index: dict[str, str], synonym: str, canonical: str) -> None:
    token = _name_token(synonym)
    if not token:
        return
    # Do not let an ambiguous alias redirect one configured channel to another.
    previous = index.get(token)
    if previous is None or previous == canonical:
        index[token] = canonical
    else:
        index.pop(token, None)


def _add_cctv_fallback_aliases(
    index: dict[str, str], canonical: str, number: int, is_plus: bool
) -> None:
    chinese_number = _CHINESE_NUMBERS.get(number)
    if is_plus:
        # A plus channel must never claim the ordinary numeric CCTV alias.
        synonyms = [
            f"CCTV{number}+", f"CCTV-{number}+", f"CCTV {number}+",
            f"CCTV{number} Plus", f"CCTV-{number} Plus",
        ]
    else:
        synonyms = [
            f"CCTV{number}", f"CCTV-{number}", f"中央{number}套",
            f"中央电视台{number}套", f"央视{number}套",
        ]
        if chinese_number:
            synonyms.extend((
                f"中央{chinese_number}套", f"中央电视台{chinese_number}套",
                f"央视{chinese_number}套",
            ))
    for suffix in _CCTV_SUFFIXES:
        synonyms.append(f"CCTV{number}{'+' if is_plus else ''}{suffix}")
    for synonym in synonyms:
        _add_alias(index, synonym, canonical)


def _resolve_candidate(
    candidate: dict,
    name_to_canonical: dict[str, str],
    cctv_number_to_canonical: dict[tuple[int, bool], str],
) -> str | None:
    name = candidate.get("name")
    if isinstance(name, str) and name.strip():
        token = _name_token(name)
        exact = name_to_canonical.get(token)
        if exact:
            return exact
        # The family rule is anchored and limited to known CCTV channel suffixes.
        match = _CCTV_NAME.fullmatch(token)
        if match:
            canonical = cctv_number_to_canonical.get(
                (int(match.group(1)), bool(match.group(2)))
            )
            if canonical:
                return canonical

    # TVG metadata may carry the configured channel name when a source's display
    # title is a provider-specific label. It still must match an exact whitelist alias.
    for key in ("tvg_name", "tvg_id"):
        value = candidate.get(key)
        token = _tvg_id_token(value) if key == "tvg_id" else _name_token(value)
        if token in name_to_canonical:
            return name_to_canonical[token]
    return None


def _priority(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 100


def _url_key(value: str) -> str:
    # Fragments are client-side and do not identify a different stream endpoint.
    from urllib.parse import urlsplit, urlunsplit

    try:
        parts = urlsplit(value.strip())
        host = (parts.hostname or "").lower()
        if ":" in host:
            host = f"[{host}]"
        if parts.port is not None:
            host += f":{parts.port}"
        if parts.username is not None:
            user = parts.username
            password = f":{parts.password}" if parts.password is not None else ""
            host = f"{user}{password}@{host}"
        return urlunsplit((parts.scheme.lower(), host, parts.path, parts.query, ""))
    except ValueError:
        return value.strip()


def _sources_for(candidate: dict) -> list[Any]:
    values = []
    existing = candidate.get("sources")
    if isinstance(existing, list):
        values.extend(existing)
    source = candidate.get("source")
    if source is not None:
        values.append(source)
    return _unique(values)


def _source_metadata_for(candidate: dict) -> list[dict]:
    keys = (
        "source", "source_id", "source_name", "priority", "project_url", "rights_note",
        "format", "download_status", "used_cache", "cache_age_seconds",
    )
    metadata = {key: candidate[key] for key in keys if key in candidate}
    return [metadata] if metadata else []


def _merge_sources(existing: dict, candidate: dict) -> None:
    existing["sources"] = _unique(existing.get("sources", []) + _sources_for(candidate))


def _merge_source_metadata(existing: dict, candidate: dict) -> None:
    for item in _source_metadata_for(candidate):
        if item not in existing["source_metadata"]:
            existing["source_metadata"].append(item)


def _unique(values: list[Any]) -> list[Any]:
    result = []
    for value in values:
        if value is not None and value not in result:
            result.append(value)
    return result
