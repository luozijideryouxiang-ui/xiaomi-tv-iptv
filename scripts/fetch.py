"""Bounded, cached retrieval of IPTV playlist sources."""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests


MAX_RESPONSE_BYTES = 20 * 1024 * 1024
MAX_REDIRECTS = 5
MAX_CONCURRENT_SOURCES = 4
_SENSITIVE_QUERY_KEY = re.compile(r"(?:token|auth|key|signature|password|passwd|secret|credential|session)", re.I)


def redact_url(value: str) -> str:
    """Return a report-safe URL with userinfo and common secret query values hidden."""
    try:
        parts = urlsplit(value)
        hostname = parts.hostname or ""
        if ":" in hostname and not hostname.startswith("["):
            hostname = f"[{hostname}]"
        host = hostname
        if parts.port is not None:
            host += f":{parts.port}"
        query = urlencode(
            [(key, "[redacted]" if _SENSITIVE_QUERY_KEY.search(key) else item)
             for key, item in parse_qsl(parts.query, keep_blank_values=True)]
        )
        return urlunsplit((parts.scheme, host, parts.path, query, parts.fragment))
    except (TypeError, ValueError):
        return "[invalid URL]"


def fetch_sources(
    sources: list[dict],
    cache_dir: Path,
    connect_timeout: float = 4,
    http_timeout: float = 12,
) -> tuple[list[dict], list[dict]]:
    """Fetch configured playlists, falling back to each source's last good cache.

    The returned records preserve the source body for parsing and carry simple
    download/cache status. Errors identify sources by configured id only, so a
    credential-bearing URL cannot leak through an exception string.
    """
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)

    def fetch_one(source: dict) -> tuple[dict | None, dict | None]:
        if not isinstance(source, dict):
            return None, {"source": "unknown", "error": "source configuration must be an object", "used_cache": False}

        source_id = _source_id(source)
        cache_path = cache_dir / (hashlib.sha256(source_id.encode("utf-8")).hexdigest() + ".utf8")
        cached_body = _read_cache(cache_path)
        try:
            url = source.get("url")
            if not isinstance(url, str) or not _http_url(url):
                raise _FetchError("source URL must use HTTP or HTTPS")

            body = _download(url, connect_timeout, http_timeout)
            if not _plausible_playlist(body):
                raise _FetchError("response is not a recognizable IPTV playlist")
            _write_cache_atomic(cache_path, body)
            record = _make_record(source, source_id, body, cache_path, used_cache=False)
            return record, None
        except Exception as exc:  # One bad source must not prevent other sources from loading.
            error = _safe_error(exc)
            if cached_body is not None and _plausible_playlist(cached_body):
                record = _make_record(source, source_id, cached_body, cache_path, used_cache=True)
                return record, {"source": source_id, "error": error, "used_cache": True}
            return None, {"source": source_id, "error": error, "used_cache": False}

    with ThreadPoolExecutor(max_workers=min(MAX_CONCURRENT_SOURCES, max(1, len(sources)))) as pool:
        results = list(pool.map(fetch_one, sources))

    records = [record for record, _ in results if record is not None]
    errors = [error for _, error in results if error is not None]
    return records, errors


class _FetchError(Exception):
    pass


def _source_id(source: dict[str, Any]) -> str:
    value = source.get("id")
    if value is not None and str(value).strip():
        return str(value).strip()
    # Stable fallback that does not expose the URL or any credentials.
    url = source.get("url")
    fingerprint = hashlib.sha256(str(url or "missing-url").encode("utf-8")).hexdigest()[:12]
    return f"source-{fingerprint}"


def _http_url(value: str) -> bool:
    try:
        parts = urlsplit(value.strip())
        return parts.scheme.lower() in {"http", "https"} and bool(parts.hostname)
    except ValueError:
        return False


def _download(url: str, connect_timeout: float, http_timeout: float) -> str:
    with requests.Session() as session:
        session.max_redirects = MAX_REDIRECTS
        with session.get(
            url,
            timeout=(connect_timeout, http_timeout),
            allow_redirects=True,
            stream=True,
        ) as response:
            if not _http_url(response.url):
                raise _FetchError("redirect target must use HTTP or HTTPS")
            if response.status_code >= 400:
                raise _FetchError(f"HTTP request returned status {response.status_code}")
            content_length = response.headers.get("Content-Length")
            if content_length:
                try:
                    if int(content_length) > MAX_RESPONSE_BYTES:
                        raise _FetchError("response exceeds the 20 MB limit")
                except ValueError:
                    pass

            chunks: list[bytes] = []
            total = 0
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > MAX_RESPONSE_BYTES:
                    raise _FetchError("response exceeds the 20 MB limit")
                chunks.append(chunk)
            raw = b"".join(chunks)

            try:
                return raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                # IPTV lists commonly carry Simplified Chinese in a legacy encoding.
                encodings = [response.encoding, response.apparent_encoding]
                for encoding in encodings:
                    if encoding and encoding.lower().replace("_", "-") not in {"utf-8", "utf8"}:
                        try:
                            return raw.decode(encoding).lstrip("\ufeff")
                        except (LookupError, UnicodeDecodeError):
                            continue
                raise _FetchError("response is not valid UTF-8 text")


def _plausible_playlist(body: str) -> bool:
    stripped = body.lstrip("\ufeff\r\n\t ")
    if not stripped or "\x00" in stripped:
        return False
    start = stripped[:1024].lower()
    if re.search(r"(?:<!doctype\s+html|<html\b|<head\b|<body\b)", start):
        return False

    lines = [line.strip().lstrip("\ufeff") for line in stripped.splitlines()]
    waiting_for_url = False
    has_m3u_item = False
    for line in lines:
        if not line:
            continue
        if line.upper().startswith("#EXTINF:"):
            waiting_for_url = True
            continue
        if line.startswith("#"):
            continue
        if waiting_for_url:
            if _http_url(line):
                has_m3u_item = True
                break
            waiting_for_url = False
    if has_m3u_item:
        return True

    # TXT playlists use "name,url" rows and optional "group,#genre#" headers.
    for line in lines:
        if not line or line.startswith(("#", "//")) or "," not in line:
            continue
        name, value = line.split(",", 1)
        value = value.strip().strip("\"'")
        if name.strip() and value.lower() != "#genre#" and _http_url(value):
            return True
    return False


def _read_cache(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return None


def _write_cache_atomic(path: Path, body: str) -> None:
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def _make_record(source: dict, source_id: str, body: str, cache_path: Path, used_cache: bool) -> dict:
    try:
        cache_age_seconds = max(0, int(time.time() - cache_path.stat().st_mtime))
    except OSError:
        cache_age_seconds = None
    record = {
        "id": source_id,
        "body": body,
        "priority": _priority(source.get("priority", 100)),
        "download_status": "cached" if used_cache else "downloaded",
        "used_cache": used_cache,
        "cache_age_seconds": cache_age_seconds,
    }
    for key in ("name", "project_url", "rights_note", "format"):
        value = source.get(key)
        if value is not None:
            record[key] = redact_url(value) if key == "project_url" and isinstance(value, str) else value
    return record


def _priority(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 100


def _safe_error(exc: Exception) -> str:
    if isinstance(exc, _FetchError):
        return str(exc)
    if isinstance(exc, requests.TooManyRedirects):
        return "request exceeded the redirect limit"
    if isinstance(exc, requests.Timeout):
        return "request timed out"
    if isinstance(exc, requests.RequestException):
        return f"HTTP request failed ({type(exc).__name__})"
    if isinstance(exc, OSError):
        return "cache read or write failed"
    return f"fetch failed ({type(exc).__name__})"
