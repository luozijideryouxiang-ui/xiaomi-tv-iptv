"""Bounded IPTV URL checks with observed address-family and decode metrics.

Network traffic is performed by a pinned HTTP connection. HLS playlists and
segments are fetched by this module, then a local FFmpeg process decodes the
bounded sample from stdin; FFmpeg never resolves or connects to the candidate.
"""

from __future__ import annotations

import concurrent.futures
import ipaddress
import os
import queue
import re
import socket
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool
from urllib3.exceptions import NewConnectionError


_FAMILIES = (("ipv4", socket.AF_INET), ("ipv6", socket.AF_INET6))
_FRAME_RE = re.compile(
    r"\bn:\s*(\d+).*?pts_time:\s*([-\d.]+).*?\bs:(\d+)x(\d+)"
)
_BITRATE_RE = re.compile(r"\bbitrate:\s*([\d.]+)\s*kb/s", re.IGNORECASE)
_REDIRECT_CODES = {301, 302, 303, 307, 308}
_FFMPEG_SLOTS = threading.BoundedSemaphore(8)


class ProbeError(Exception):
    """A safe, user-facing probe failure without a source URL."""


def _resolve_host(host: str, timeout: float, allow_private: bool) -> list[str]:
    """Resolve within a bounded wait and reject any non-public DNS answer."""
    try:
        literal = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        literal = None

    if literal is not None:
        addresses = [str(literal)]
    else:
        lowered = host.rstrip(".").lower()
        if lowered == "localhost" or lowered.endswith(".localhost"):
            if not allow_private:
                raise ProbeError("private or loopback host is not allowed")

        result: list[str] = []
        failure: list[BaseException] = []

        def lookup() -> None:
            try:
                rows = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
                for row in rows:
                    address = row[4][0].split("%", 1)[0]
                    if address not in result:
                        result.append(address)
            except BaseException as exc:  # carried to the caller thread
                failure.append(exc)

        thread = threading.Thread(target=lookup, name="iptv-dns", daemon=True)
        thread.start()
        thread.join(max(0.01, timeout))
        if thread.is_alive():
            raise ProbeError("DNS lookup timed out")
        if failure:
            if isinstance(failure[0], socket.gaierror):
                raise ProbeError("DNS lookup failed") from None
            raise ProbeError("DNS lookup failed") from None
        addresses = result

    if not addresses:
        raise ProbeError("DNS returned no addresses")
    if not allow_private:
        for address in addresses:
            try:
                parsed = ipaddress.ip_address(address)
            except ValueError:
                raise ProbeError("DNS returned an invalid address") from None
            if not parsed.is_global:
                raise ProbeError("private or non-public DNS address is not allowed")
    return addresses


def _validate_url(url: str) -> tuple[str, str, int]:
    try:
        parts = urlsplit(url)
        scheme = parts.scheme.lower()
        host = parts.hostname
        port = parts.port
    except (ValueError, UnicodeError):
        raise ProbeError("invalid URL") from None
    if scheme not in ("http", "https") or not host:
        raise ProbeError("only HTTP and HTTPS stream URLs are supported")
    if len(url) > 8192:
        raise ProbeError("URL is too long")
    if port is not None and not (1 <= port <= 65535):
        raise ProbeError("invalid URL port")
    return scheme, host, port or (443 if scheme == "https" else 80)


def _sockaddr_for(ip: str, port: int) -> tuple:
    address = ipaddress.ip_address(ip)
    if address.version == 6:
        return (str(address), port, 0, 0)
    return (str(address), port)


def _family_from_peer(peer: tuple) -> str | None:
    if isinstance(peer, tuple) and len(peer) == 4:
        return "ipv6"
    if isinstance(peer, tuple) and len(peer) == 2:
        return "ipv4"
    return None


class _PinnedConnectionMixin:
    def __init__(self, *args, pinned_ip: str, peer_callback=None, **kwargs):
        self._pinned_ip = pinned_ip
        self._peer_callback = peer_callback
        super().__init__(*args, **kwargs)

    def _new_conn(self):
        try:
            address = ipaddress.ip_address(self._pinned_ip)
            sock = socket.socket(
                socket.AF_INET6 if address.version == 6 else socket.AF_INET,
                socket.SOCK_STREAM,
            )
            timeout = self.timeout
            if timeout is not None and isinstance(timeout, (int, float)):
                sock.settimeout(timeout)
            for level, option, value in self.socket_options or ():
                sock.setsockopt(level, option, value)
            if self.source_address:
                sock.bind(self.source_address)
            sock.connect(_sockaddr_for(self._pinned_ip, self.port))
            if self._peer_callback:
                self._peer_callback(sock.getpeername())
            return sock
        except OSError as exc:
            try:
                sock.close()
            except (UnboundLocalError, AttributeError):
                pass
            raise NewConnectionError(self, "pinned connection failed") from exc


class _PinnedHTTPConnection(_PinnedConnectionMixin, HTTPConnection):
    pass


class _PinnedHTTPSConnection(_PinnedConnectionMixin, HTTPSConnection):
    """HTTPS keeps the original URL host for SNI and certificate checks."""


class _PinnedHTTPConnectionPool(HTTPConnectionPool):
    ConnectionCls = _PinnedHTTPConnection


class _PinnedHTTPSConnectionPool(HTTPSConnectionPool):
    ConnectionCls = _PinnedHTTPSConnection


class _PinnedAdapter(HTTPAdapter):
    def __init__(self, pinned_ip: str):
        super().__init__(max_retries=0, pool_connections=1, pool_maxsize=1)
        self.pinned_ip = pinned_ip
        self.peer_family: str | None = None

    def _connected(self, peer: tuple) -> None:
        self.peer_family = _family_from_peer(peer)

    def get_connection_with_tls_context(self, request, verify, proxies=None, cert=None):
        if proxies:
            raise requests.exceptions.InvalidProxyURL("proxies are disabled for probes")
        scheme, host, port = _validate_url(request.url)
        pool = _PinnedHTTPSConnectionPool if scheme == "https" else _PinnedHTTPConnectionPool
        return pool(
            host=host, port=port, maxsize=1, block=True,
            pinned_ip=self.pinned_ip, peer_callback=self._connected,
        )


@dataclass
class _FetchedResponse:
    response: requests.Response
    session: requests.Session
    family: str | None
    elapsed_ms: float
    network_mode: str = "direct_pinned"

    def close(self) -> None:
        self.response.close()
        self.session.close()


class _FamilyFetcher:
    def __init__(self, family: str | None, initial_ips: list[str], allow_private: bool,
                 deadline: float, connect_timeout: float, read_timeout: float,
                 initial_host: str, network_mode: str = "direct_pinned"):
        self.family = family
        self.network_mode = network_mode
        self.allow_private = allow_private
        self.deadline = deadline
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self._resolved: dict[str, list[str]] = {initial_host: list(initial_ips)}
        self._chosen: dict[str, str] = {}
        self.last_family: str | None = None
        self.last_http_ms: float | None = None

    @staticmethod
    def _has_family(ip: str, family: str) -> bool:
        version = ipaddress.ip_address(ip).version
        return version == (4 if family == "ipv4" else 6)

    def _addresses(self, host: str) -> list[str]:
        if host not in self._resolved:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise ProbeError("probe time limit reached")
            self._resolved[host] = _resolve_host(
                host, min(2.0, remaining), self.allow_private
            )
        if self.family is None:
            return self._resolved[host]
        addresses = [ip for ip in self._resolved[host] if self._has_family(ip, self.family)]
        if not addresses:
            raise ProbeError(f"no {self.family} DNS address; family untested")
        chosen = self._chosen.get(host)
        if chosen in addresses:
            addresses.remove(chosen)
            addresses.insert(0, chosen)
        return addresses

    def get(self, url: str) -> _FetchedResponse:
        if self.network_mode == "system_proxy":
            return self._get_system_proxy(url)
        return self._get_pinned(url)

    def _get_system_proxy(self, url: str) -> _FetchedResponse:
        """Fetch with Requests' configured system proxy; origin family is unknown."""
        current = url
        elapsed_ms = 0.0
        for redirect_count in range(4):
            _scheme, host, _port = _validate_url(current)
            self._addresses(host)  # Keep private and non-public targets blocked.
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise ProbeError("probe time limit reached")
            session = requests.Session()
            session.trust_env = True
            started = time.monotonic()
            try:
                response = session.get(
                    current,
                    headers={"User-Agent": "IPTV-stream-check/1.0", "Accept": "*/*"},
                    stream=True,
                    allow_redirects=False,
                    timeout=(min(self.connect_timeout, remaining),
                             min(self.read_timeout, remaining)),
                )
            except requests.exceptions.Timeout:
                session.close()
                raise ProbeError("HTTP request timed out via system network") from None
            except requests.exceptions.RequestException:
                session.close()
                raise ProbeError("HTTP request failed via system network") from None

            elapsed = (time.monotonic() - started) * 1000
            fetched = _FetchedResponse(response, session, None, elapsed, "system_proxy")
            self.network_mode = "system_proxy"
            self.last_family = None
            self.last_http_ms = elapsed_ms + elapsed
            if response.status_code in _REDIRECT_CODES:
                location = response.headers.get("Location")
                fetched.close()
                if not location or redirect_count >= 3:
                    raise ProbeError("invalid or excessive HTTP redirect")
                current = urljoin(current, location)
                _validate_url(current)
                elapsed_ms += elapsed
                continue
            if response.status_code not in (200, 206):
                status = response.status_code
                fetched.close()
                raise ProbeError(f"HTTP status {status}")
            fetched.elapsed_ms += elapsed_ms
            return fetched
        raise ProbeError("excessive HTTP redirects")

    def _get_pinned(self, url: str) -> _FetchedResponse:
        current = url
        elapsed_ms = 0.0
        for redirect_count in range(4):
            scheme, host, _port = _validate_url(current)
            addresses = self._addresses(host)
            last_error: str | None = None
            fetched = None
            for ip in addresses:
                remaining = self.deadline - time.monotonic()
                if remaining <= 0:
                    raise ProbeError("probe time limit reached")
                session = requests.Session()
                session.trust_env = False
                adapter = _PinnedAdapter(ip)
                session.mount(f"{scheme}://", adapter)
                started = time.monotonic()
                try:
                    response = session.get(
                        current,
                        headers={"User-Agent": "IPTV-stream-check/1.0", "Accept": "*/*"},
                        stream=True,
                        allow_redirects=False,
                        timeout=(min(self.connect_timeout, remaining),
                                 min(self.read_timeout, remaining)),
                    )
                except requests.exceptions.SSLError:
                    session.close()
                    last_error = "TLS handshake failed"
                    break
                except requests.exceptions.Timeout:
                    session.close()
                    last_error = "HTTP request timed out"
                    if time.monotonic() >= self.deadline:
                        break
                    continue
                except requests.exceptions.RequestException:
                    session.close()
                    last_error = "HTTP connection failed"
                    if time.monotonic() >= self.deadline:
                        break
                    continue

                elapsed = (time.monotonic() - started) * 1000
                # Requests may close its response socket immediately when the
                # origin sends Connection: close. The adapter records the peer
                # while the socket is still connected, in _new_conn().
                peer_family = adapter.peer_family or _response_peer_family(response)
                expected_family = "ipv4" if ipaddress.ip_address(ip).version == 4 else "ipv6"
                if peer_family != expected_family or peer_family != self.family:
                    response.close()
                    session.close()
                    last_error = "connected peer family could not be verified"
                    break
                self._chosen[host] = ip
                self.last_family = peer_family
                self.last_http_ms = elapsed_ms + elapsed
                fetched = _FetchedResponse(response, session, peer_family, elapsed)
                break

            if fetched is None:
                raise ProbeError(last_error or "HTTP connection failed")

            response = fetched.response
            if response.status_code in _REDIRECT_CODES:
                location = response.headers.get("Location")
                fetched.close()
                if not location or redirect_count >= 3:
                    raise ProbeError("invalid or excessive HTTP redirect")
                current = urljoin(current, location)
                _validate_url(current)
                elapsed_ms += fetched.elapsed_ms
                continue
            if response.status_code not in (200, 206):
                status = response.status_code
                fetched.close()
                raise ProbeError(f"HTTP status {status}")
            fetched.elapsed_ms += elapsed_ms
            return fetched
        raise ProbeError("excessive HTTP redirects")


def _response_peer_family(response: requests.Response) -> str | None:
    raw = getattr(response, "raw", None)
    candidates = [
        getattr(getattr(raw, "_connection", None), "sock", None),
        getattr(getattr(getattr(getattr(raw, "_fp", None), "fp", None), "raw", None), "_sock", None),
    ]
    for sock in candidates:
        if sock is None:
            continue
        try:
            peer = sock.getpeername()
            return "ipv6" if isinstance(peer, tuple) and len(peer) == 4 else "ipv4"
        except OSError:
            continue
    return None


def _read_limited(fetched: _FetchedResponse, limit: int, deadline: float) -> bytes:
    content_length = fetched.response.headers.get("Content-Length")
    if content_length:
        try:
            if int(content_length) > limit:
                raise ProbeError("playlist exceeds the read limit")
        except ValueError:
            pass
    chunks = bytearray()
    try:
        for chunk in fetched.response.iter_content(chunk_size=16 * 1024):
            if time.monotonic() >= deadline:
                raise ProbeError("probe time limit reached")
            if not chunk:
                continue
            chunks.extend(chunk)
            if len(chunks) > limit:
                raise ProbeError("playlist exceeds the read limit")
    except requests.exceptions.Timeout:
        raise ProbeError("HTTP response timed out") from None
    except requests.exceptions.RequestException:
        raise ProbeError("HTTP response could not be read") from None
    return bytes(chunks)


def _inspect_initial(fetched: _FetchedResponse, deadline: float) -> tuple[bytes, object | None]:
    """Read a bounded HLS playlist, or only a prefix for a media stream."""
    iterator = iter(fetched.response.iter_content(chunk_size=16 * 1024))
    try:
        first = next(iterator, b"")
    except requests.exceptions.Timeout:
        raise ProbeError("HTTP response timed out") from None
    except requests.exceptions.RequestException:
        raise ProbeError("HTTP response could not be read") from None
    if time.monotonic() >= deadline:
        raise ProbeError("probe time limit reached")
    body = bytearray(first or b"")
    if not body:
        raise ProbeError("HTTP response is empty")
    content_type = fetched.response.headers.get("Content-Type", "")
    if body.lstrip().startswith(b"#EXTM3U") or "mpegurl" in content_type.lower():
        if len(body) > 256 * 1024:
            raise ProbeError("playlist exceeds the read limit")
        try:
            for chunk in iterator:
                if time.monotonic() >= deadline:
                    raise ProbeError("probe time limit reached")
                body.extend(chunk)
                if len(body) > 256 * 1024:
                    raise ProbeError("playlist exceeds the read limit")
        except requests.exceptions.Timeout:
            raise ProbeError("HTTP response timed out") from None
        except requests.exceptions.RequestException:
            raise ProbeError("HTTP response could not be read") from None
        return bytes(body), None
    return bytes(body[:16 * 1024]), iterator


def _is_html(body: bytes, content_type: str = "") -> bool:
    content_type = content_type.lower()
    if "text/html" in content_type or "application/xhtml+xml" in content_type:
        return True
    prefix = body[:512].lstrip().lower()
    return prefix.startswith((b"<!doctype html", b"<?xml", b"<html", b"<head", b"<body", b"<form", b"<script", b"<meta")) or b"<html" in prefix


def _is_json_error(body: bytes, content_type: str = "") -> bool:
    if "application/json" in content_type.lower():
        return True
    prefix = body[:256].lstrip()
    return prefix.startswith((b"{", b"["))


def _parse_attributes(value: str) -> dict[str, str]:
    attrs: dict[str, str] = {}
    for match in re.finditer(r'(?:^|,)\s*([A-Z0-9-]+)=((?:"[^"]*")|[^,]*)', value, re.I):
        attrs[match.group(1).upper()] = match.group(2).strip().strip('"')
    return attrs


def _parse_playlist(body: bytes, base_url: str) -> dict:
    if not body.lstrip().startswith(b"#EXTM3U"):
        raise ProbeError("invalid HLS playlist")
    text = body.decode("utf-8", errors="replace")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines or lines[0] != "#EXTM3U":
        raise ProbeError("invalid HLS playlist")

    variants = []
    segments = []
    durations = []
    map_url = None
    pending_duration = None
    for line in lines[1:]:
        if line.startswith(("#EXT-X-KEY:", "#EXT-X-SESSION-KEY:")):
            attrs = _parse_attributes(line.split(":", 1)[1])
            if attrs.get("METHOD", "").upper() != "NONE":
                raise ProbeError("encrypted HLS is unsupported")
        elif line.startswith("#EXT-X-MAP:"):
            attrs = _parse_attributes(line.split(":", 1)[1])
            if attrs.get("URI"):
                map_url = urljoin(base_url, attrs["URI"])
        elif line.startswith("#EXT-X-STREAM-INF:"):
            variants.append({"attrs": _parse_attributes(line.split(":", 1)[1]), "next": None})
        elif line.startswith("#EXTINF:"):
            try:
                pending_duration = max(0.0, float(line.split(":", 1)[1].split(",", 1)[0]))
            except ValueError:
                pending_duration = None
        elif line.startswith("#"):
            continue
        elif variants and variants[-1]["next"] is None:
            variants[-1]["next"] = urljoin(base_url, line)
        else:
            segments.append(urljoin(base_url, line))
            durations.append(pending_duration)
            pending_duration = None

    variants = [variant for variant in variants if variant["next"]]
    if variants:
        def quality(item):
            attrs = item["attrs"]
            try:
                width, height = (int(part) for part in attrs.get("RESOLUTION", "0x0").split("x", 1))
            except ValueError:
                width, height = 0, 0
            try:
                bandwidth = int(attrs.get("AVERAGE-BANDWIDTH") or attrs.get("BANDWIDTH") or 0)
            except ValueError:
                bandwidth = 0
            within_1080p = height <= 1080 and width <= 1920
            return (within_1080p, height * width, bandwidth)

        selected = max(variants, key=quality)
        attrs = selected["attrs"]
        try:
            bandwidth_kbps = float(attrs.get("AVERAGE-BANDWIDTH") or attrs.get("BANDWIDTH")) / 1000
        except (ValueError, TypeError):
            bandwidth_kbps = None
        resolution = None
        match = re.fullmatch(r"(\d+)x(\d+)", attrs.get("RESOLUTION", ""))
        if match:
            resolution = [int(match.group(1)), int(match.group(2))]
        return {
            "kind": "master",
            "variant_url": selected["next"],
            "bandwidth_kbps": bandwidth_kbps,
            "resolution": resolution,
        }
    if not segments:
        raise ProbeError("HLS playlist contains no media segments")
    return {
        "kind": "media",
        "segments": segments,
        "durations": durations,
        "map_url": map_url,
        "bandwidth_kbps": None,
        "resolution": None,
    }


def _reject_hls_segment_paths(urls: list[tuple[str, float | None]], fragments: list[str]) -> None:
    """Reject known access-denied placeholder paths before starting FFmpeg."""
    if not fragments:
        return
    for segment_url, _duration in urls:
        try:
            path = urlsplit(segment_url).path.casefold()
        except (TypeError, ValueError):
            continue
        if any(fragment in path for fragment in fragments):
            raise ProbeError("HLS contains an access-denied placeholder")


def _ffmpeg_path(options: dict) -> str:
    path = options.get("ffmpeg_path")
    if path:
        return os.fspath(path)
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        raise ProbeError("FFmpeg is unavailable") from None


class _FFmpegSampler:
    def __init__(self, options: dict, started_at: float, deadline: float):
        self.started_at = started_at
        self.deadline = deadline
        self.sample_target = options["sample_seconds"]
        self.max_bytes = options["max_sample_bytes"]
        self._state_lock = threading.Lock()
        self._first_frame_ms: float | None = None
        self._resolution: list[int] | None = None
        self._pts: list[float] = []
        self._frame_wall: list[float] = []
        self._bitrate: float | None = None
        self._queue: queue.Queue[bytes | None] = queue.Queue(maxsize=2)
        exe = _ffmpeg_path(options)
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not _FFMPEG_SLOTS.acquire(timeout=max(0.0, remaining)):
            raise ProbeError("FFmpeg concurrency limit reached before probe deadline")
        self._slot_acquired = True
        command = [
            exe, "-hide_banner", "-loglevel", "info", "-re", "-i", "pipe:0",
            "-map", "0:v:0", "-an", "-vf", "showinfo", "-t", str(self.sample_target),
            "-f", "null", "-",
        ]
        try:
            self.process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                shell=False,
                close_fds=True,
            )
        except (OSError, ValueError):
            self._release_slot()
            raise ProbeError("FFmpeg could not be started") from None
        self._writer = threading.Thread(target=self._write_input, name="iptv-ffmpeg-input", daemon=True)
        self._reader = threading.Thread(target=self._read_stderr, name="iptv-ffmpeg-log", daemon=True)
        try:
            self._writer.start()
            self._reader.start()
        except RuntimeError:
            self.process.kill()
            self.process.wait()
            self._release_slot()
            raise ProbeError("FFmpeg monitor could not be started") from None
        self.bytes_fed = 0

    def _release_slot(self) -> None:
        if self._slot_acquired:
            self._slot_acquired = False
            _FFMPEG_SLOTS.release()

    def _write_input(self) -> None:
        try:
            assert self.process.stdin is not None
            while True:
                data = self._queue.get()
                if data is None:
                    break
                self.process.stdin.write(data)
                self.process.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            pass
        finally:
            try:
                if self.process.stdin:
                    self.process.stdin.close()
            except OSError:
                pass

    def _read_stderr(self) -> None:
        assert self.process.stderr is not None
        for raw in iter(self.process.stderr.readline, b""):
            wall = time.monotonic()
            line = raw.decode("utf-8", errors="replace").strip()
            match = _FRAME_RE.search(line)
            if match:
                pts = float(match.group(2))
                width, height = int(match.group(3)), int(match.group(4))
                with self._state_lock:
                    if self._first_frame_ms is None:
                        self._first_frame_ms = max(0.0, (wall - self.started_at) * 1000)
                    if self._resolution is None:
                        self._resolution = [width, height]
                    self._pts.append(pts)
                    self._frame_wall.append(wall)
                continue
            bitrate = _BITRATE_RE.search(line)
            if bitrate:
                with self._state_lock:
                    if self._bitrate is None:
                        self._bitrate = float(bitrate.group(1))

    def feed(self, chunk: bytes, deadline: float) -> bool:
        if self.process.poll() is not None:
            return False
        allowed = self.max_bytes - self.bytes_fed
        if allowed <= 0:
            return False
        data = chunk[:allowed]
        while data:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or self.process.poll() is not None:
                return False
            piece = data[:64 * 1024]
            try:
                self._queue.put(piece, timeout=min(0.2, remaining))
            except queue.Full:
                if time.monotonic() >= deadline:
                    return False
                continue
            data = data[len(piece):]
            self.bytes_fed += len(piece)
        return self.bytes_fed < self.max_bytes

    def finish(self) -> dict:
        try:
            try:
                self._queue.put(None, timeout=max(0.01, min(0.25, self.deadline - time.monotonic())))
            except queue.Full:
                pass
            remaining = self.deadline - time.monotonic()
            try:
                self.process.wait(timeout=max(0.01, remaining))
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
            self._writer.join(timeout=0.5)
            self._reader.join(timeout=1.0)
            if self.process.poll() is None:
                self.process.kill()
                self.process.wait()
            with self._state_lock:
                pts = list(self._pts)
                walls = list(self._frame_wall)
                frames = len(pts)
                sample_seconds = max(0.0, max(pts) - min(pts)) if pts else 0.0
                stalls = 0
                for previous, current in zip(walls, walls[1:]):
                    if current - previous > 1.5:
                        stalls += 1
                return {
                    "first_frame_ms": self._first_frame_ms,
                    "resolution": self._resolution,
                    "bitrate_kbps": self._bitrate,
                    "sample_seconds": sample_seconds,
                    "frames": frames,
                    "stalls": stalls,
                    "decode_error": self.process.returncode not in (0, None),
                }
        finally:
            self._release_slot()


def _check_options(options: dict) -> dict:
    def number(name, default, low, high):
        try:
            value = float(options.get(name, default))
        except (TypeError, ValueError):
            value = default
        return min(high, max(low, value))

    try:
        workers = int(options.get("concurrency", options.get("max_workers", 24)))
    except (TypeError, ValueError):
        workers = 24
    try:
        sample_bytes = int(options.get("max_sample_bytes", 4 * 1024 * 1024))
    except (TypeError, ValueError):
        sample_bytes = 4 * 1024 * 1024
    network_mode = options.get("network_mode", "auto")
    if network_mode not in ("auto", "direct", "system_proxy"):
        network_mode = "auto"
    raw_reject_segment_paths = options.get("reject_segment_paths", ())
    reject_segment_paths = []
    if isinstance(raw_reject_segment_paths, (list, tuple, set)):
        for value in raw_reject_segment_paths:
            if isinstance(value, str):
                fragment = value.strip().casefold()
                if fragment and fragment not in reject_segment_paths:
                    reject_segment_paths.append(fragment)
    return {
        "max_workers": min(24, max(1, workers)),
        "allow_private": options.get("allow_private") is True,
        "connect_timeout": number("connect_timeout", 4.0, 0.2, 4.0),
        "read_timeout": number("http_timeout", options.get("read_timeout", 12.0), 0.2, 12.0),
        # HTTP, decode, and FFmpeg slot waiting share one deadline.
        "probe_timeout": number("stream_timeout", options.get("probe_timeout", 12.0), 1.0, 15.0),
        "sample_seconds": number("sample_seconds", 3.0, 0.5, 3.0),
        "max_sample_bytes": min(8 * 1024 * 1024, max(256 * 1024, sample_bytes)),
        "ffmpeg_path": options.get("ffmpeg_path"),
        "network_mode": network_mode,
        "reject_segment_paths": reject_segment_paths,
    }


def _base_result(candidate: dict, family: str | None, location: str, error: str,
                 basis: str, family_observed: bool = False,
                 family_tested: bool | None = None,
                 network_mode: str = "direct_pinned") -> dict:
    result = dict(candidate)
    if family_tested is None:
        family_tested = family is not None and network_mode == "direct_pinned"
    result.update({
        "ok": False,
        "error": error,
        "family": family,
        "family_tested": family_tested,
        "family_observed": family_observed,
        "network_mode": network_mode,
        "http_ms": None,
        "first_frame_ms": None,
        "resolution": None,
        "bitrate_kbps": None,
        "sample_seconds": 0.0,
        "frames": 0,
        "stalls": 0,
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "location": location,
        "metrics_basis": basis,
    })
    return result


def _probe_family(candidate: dict, family: str | None, initial_ips: list[str],
                  options: dict, location: str) -> dict:
    started = time.monotonic()
    deadline = started + options["probe_timeout"]
    network_mode = options.get("probe_network_mode", "direct_pinned")
    reported_family = None if network_mode == "system_proxy" else family
    result = _base_result(
        candidate, reported_family, location, "",
        ("Configured system proxy used; upstream address family is unverified."
         if network_mode == "system_proxy" else
         "Family labels the requested probe; the connected socket peer is checked before marking it observed."),
        family_tested=(family is not None and network_mode == "direct_pinned"),
        network_mode=network_mode,
    )
    fetcher = _FamilyFetcher(
        family, initial_ips, options["allow_private"], deadline,
        options["connect_timeout"], options["read_timeout"],
        _validate_url(candidate["url"])[1] if isinstance(candidate.get("url"), str) else "",
        network_mode=network_mode,
    )
    sampler = None
    bitrate_basis = None
    hls_seen = False
    first_http_ms = None
    network_start = started
    try:
        url = candidate.get("url")
        if not isinstance(url, str):
            raise ProbeError("candidate URL is missing")
        first = fetcher.get(url)
        first_http_ms = first.elapsed_ms
        if first.family is not None:
            result["family"] = first.family
            result["family_observed"] = True
        try:
            body, remainder = _inspect_initial(first, deadline)
        except Exception:
            first.close()
            raise
        content_type = first.response.headers.get("Content-Type", "")
        first_url = first.response.url
        if _is_html(body, content_type) or _is_json_error(body, content_type):
            first.close()
            raise ProbeError("response is HTML or JSON, not media")
        if body.lstrip().startswith(b"#EXTM3U") or "mpegurl" in content_type.lower():
            hls_seen = True
            first.close()
            playlist = _parse_playlist(body, first_url)
            for _ in range(3):
                if playlist["kind"] != "master":
                    break
                parent_playlist = playlist
                fetched = fetcher.get(playlist["variant_url"])
                try:
                    variant_body = _read_limited(fetched, 256 * 1024, deadline)
                    variant_type = fetched.response.headers.get("Content-Type", "")
                    variant_url = fetched.response.url
                finally:
                    fetched.close()
                if _is_html(variant_body, variant_type) or _is_json_error(variant_body, variant_type):
                    raise ProbeError("HLS variant returned HTML or JSON")
                playlist = _parse_playlist(variant_body, variant_url)
                if parent_playlist.get("bandwidth_kbps") is not None:
                    bitrate_basis = "HLS variant advertised bandwidth"
                    result["bitrate_kbps"] = parent_playlist["bandwidth_kbps"]
                if parent_playlist.get("resolution"):
                    result["resolution"] = parent_playlist["resolution"]
            if playlist["kind"] == "master":
                raise ProbeError("nested HLS master playlist limit reached")
            urls = []
            if playlist.get("map_url"):
                urls.append((playlist["map_url"], None))
            urls.extend(zip(playlist["segments"], playlist["durations"]))
            if not urls:
                raise ProbeError("HLS playlist contains no media segments")
            _reject_hls_segment_paths(urls, options["reject_segment_paths"])
        else:
            # Continue the same bounded response so startup is measured from the
            # original connection and a live transport is not drained into RAM.
            urls = [(first, body, remainder, None)]

        sampler = _FFmpegSampler(options, network_start, deadline)
        bytes_sent = 0
        checked_segment = False
        for item in urls:
            if time.monotonic() >= deadline or sampler.process.poll() is not None:
                break
            if hls_seen:
                segment_url, duration = item
                fetched = fetcher.get(segment_url)
                initial_body = b""
                initial_remainder = None
            else:
                fetched, initial_body, initial_remainder, duration = item
            response_type = fetched.response.headers.get("Content-Type", "")
            segment_bytes = 0
            segment_complete = False
            try:
                iterator = initial_remainder or iter(fetched.response.iter_content(chunk_size=32 * 1024))
                if initial_body:
                    if _is_html(initial_body, response_type) or _is_json_error(initial_body, response_type):
                        raise ProbeError("media segment is HTML or JSON")
                    checked_segment = True
                    initial = initial_body[:options["max_sample_bytes"]]
                    segment_bytes += len(initial)
                    bytes_sent += len(initial)
                    sampler.feed(initial, deadline)
                prefix = bytearray()
                for chunk in iterator:
                    if time.monotonic() >= deadline:
                        raise ProbeError("probe time limit reached")
                    if not chunk:
                        continue
                    if not prefix:
                        prefix.extend(chunk[:512])
                        if _is_html(bytes(prefix), response_type) or _is_json_error(bytes(prefix), response_type):
                            raise ProbeError("media segment is HTML or JSON")
                        checked_segment = True
                    allowance = options["max_sample_bytes"] - bytes_sent
                    if allowance <= 0:
                        break
                    sample = chunk[:allowance]
                    segment_bytes += len(sample)
                    bytes_sent += len(sample)
                    if not sampler.feed(sample, deadline):
                        break
                    if len(sample) < len(chunk):
                        break
                else:
                    segment_complete = True
                if not checked_segment:
                    raise ProbeError("media segment is empty")
            except requests.exceptions.Timeout:
                raise ProbeError("media segment request timed out") from None
            except requests.exceptions.RequestException:
                raise ProbeError("media segment could not be read") from None
            finally:
                fetched.close()
            if (duration and duration > 0 and segment_complete and segment_bytes > 0
                    and result["bitrate_kbps"] is None):
                result["bitrate_kbps"] = segment_bytes * 8 / duration / 1000
                bitrate_basis = "complete media segment bytes divided by EXTINF duration"
            if not sampler.bytes_fed < options["max_sample_bytes"] or sampler.process.poll() is not None:
                break

        if not checked_segment:
            raise ProbeError("no media segment bytes were received")
        decoded = sampler.finish()
        sampler = None
        result.update({key: decoded[key] for key in (
            "first_frame_ms", "resolution", "sample_seconds", "frames", "stalls"
        )})
        if result["bitrate_kbps"] is None:
            result["bitrate_kbps"] = decoded["bitrate_kbps"]
            if decoded["bitrate_kbps"] is not None:
                bitrate_basis = "FFmpeg input metadata"
        result["http_ms"] = first_http_ms
        if decoded["frames"] <= 0 or decoded["resolution"] is None:
            raise ProbeError("FFmpeg did not decode a video frame")
        if decoded["decode_error"]:
            raise ProbeError("FFmpeg reported a media decode error")
        result["ok"] = True
        result["error"] = ""
        if network_mode == "system_proxy":
            result["metrics_basis"] = (
                "HTTP fetched through the configured system network/proxy; upstream address family is unverified; "
                "bounded media bytes decoded by local FFmpeg stdin; "
                f"{decoded['frames']} frames over {decoded['sample_seconds']:.3f}s; stalls count decoded-frame wall gaps over 1.5s. "
                "This short sample does not establish long-term stability."
            )
        else:
            result["metrics_basis"] = (
                "Upstream HTTP peer family observed on pinned sockets; bounded media bytes decoded by local FFmpeg stdin; "
                f"{decoded['frames']} frames over {decoded['sample_seconds']:.3f}s; stalls count decoded-frame wall gaps over 1.5s. "
                "This short sample does not establish long-term stability."
            )
        if bitrate_basis:
            result["metrics_basis"] += f" Bitrate: {bitrate_basis}."
        elif result["bitrate_kbps"] is None:
            result["metrics_basis"] += " Bitrate unavailable from observed metadata."
        if hls_seen:
            result["metrics_basis"] += " HLS playlist and media bytes validated."
        return result
    except ProbeError as exc:
        result["error"] = str(exc)
        if not result["family_observed"] and fetcher.last_family:
            result["family"] = fetcher.last_family
            result["family_observed"] = True
        if not result["family_observed"] and network_mode != "system_proxy":
            result["metrics_basis"] = f"No upstream socket peer was observed; {family} was attempted but remains unverified."
        if first_http_ms is None:
            first_http_ms = fetcher.last_http_ms
        result["http_ms"] = first_http_ms
        return result
    except Exception as exc:
        # Do not return exception text: requests/urllib3 messages can contain credentials.
        result["error"] = f"probe failed ({type(exc).__name__})"
        if not result["family_observed"] and fetcher.last_family:
            result["family"] = fetcher.last_family
            result["family_observed"] = True
        if not result["family_observed"] and network_mode != "system_proxy":
            result["metrics_basis"] = f"No upstream socket peer was observed; {family} was attempted but remains unverified."
        if first_http_ms is None:
            first_http_ms = fetcher.last_http_ms
        result["http_ms"] = first_http_ms
        return result
    finally:
        if sampler is not None:
            sampler.finish()
        result["checked_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")


def _prepare_candidate(candidate: dict, options: dict) -> dict:
    url = candidate.get("url") if isinstance(candidate, dict) else None
    if not isinstance(url, str):
        return {"candidate": candidate if isinstance(candidate, dict) else {}, "error": "candidate URL is missing", "addresses": []}
    try:
        _scheme, host, _port = _validate_url(url)
        addresses = _resolve_host(host, 2.0, options["allow_private"])
        return {"candidate": candidate, "error": None, "addresses": addresses}
    except ProbeError as exc:
        return {"candidate": candidate, "error": str(exc), "addresses": []}


def check_candidates(candidates: list[dict], options: dict, location: str = "local") -> list[dict]:
    """Check each candidate through available IPv4 and IPv6 paths, or the system proxy.

    Defaults cap network work at 24 workers, FFmpeg at 8 processes, and each
    family probe at 12 seconds total. Set
    ``allow_private=True`` only for a deliberately trusted private IPTV source.
    Supported option overrides: ``concurrency`` (or ``max_workers``),
    ``allow_private``, ``connect_timeout``, ``http_timeout``, ``stream_timeout``, ``sample_seconds``,
    ``max_sample_bytes``, ``ffmpeg_path``, and ``network_mode``. ``auto`` (default)
    uses Requests' configured system proxy when one is present; such checks do
    not claim the upstream address family. ``direct`` forces pinned IPv4/IPv6
    probes. ``system_proxy`` explicitly uses Requests' configured network path.
    """
    opts = _check_options(options or {})
    prepared: dict[int, dict] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=opts["max_workers"]) as pool:
        prepare_futures = {
            pool.submit(_prepare_candidate, candidate, opts): index
            for index, candidate in enumerate(candidates)
        }
        for future in concurrent.futures.as_completed(prepare_futures):
            index = prepare_futures[future]
            try:
                prepared[index] = future.result()
            except Exception as exc:
                prepared[index] = {
                    "candidate": candidates[index],
                    "error": f"candidate preparation failed ({type(exc).__name__})",
                    "addresses": [],
                }

        rows: dict[int, list[dict]] = {index: [] for index in range(len(candidates))}
        probe_futures = {}
        for index, item in sorted(prepared.items()):
            candidate = item["candidate"]
            mode = opts["network_mode"]
            if mode == "auto":
                try:
                    # Do not retain, display, or report proxy values: they can
                    # contain credentials. Only whether Requests has one matters.
                    mode = "system_proxy" if bool(requests.utils.get_environ_proxies(candidate["url"])) else "direct_pinned"
                except Exception:
                    mode = "direct_pinned"
            elif mode == "direct":
                mode = "direct_pinned"
            if mode == "system_proxy":
                if item["error"]:
                    rows[index].append(_base_result(
                        candidate, None, location, item["error"],
                        "No upstream family was tested; candidate preparation failed before a request.",
                        family_tested=False, network_mode="system_proxy",
                    ))
                else:
                    probe_opts = dict(opts, probe_network_mode="system_proxy")
                    future = pool.submit(_probe_family, candidate, None, item["addresses"], probe_opts, location)
                    probe_futures[future] = index
                continue

            for family, _af in _FAMILIES:
                addresses = [
                    ip for ip in item["addresses"]
                    if ipaddress.ip_address(ip).version == (4 if family == "ipv4" else 6)
                ]
                if item["error"]:
                    rows[index].append(_base_result(
                        candidate, family, location, item["error"],
                        "No socket connection was made; family was not observed.",
                        family_tested=False,
                    ))
                elif not addresses:
                    rows[index].append(_base_result(
                        candidate, family, location,
                        f"no {family} DNS address; family untested",
                        "DNS returned no address for this family; no socket connection was attempted.",
                        family_tested=False,
                    ))
                else:
                    probe_opts = dict(opts, probe_network_mode="direct_pinned")
                    future = pool.submit(_probe_family, candidate, family, addresses, probe_opts, location)
                    probe_futures[future] = (index, family)

        for future in concurrent.futures.as_completed(probe_futures):
            owner = probe_futures[future]
            try:
                rows[owner if isinstance(owner, int) else owner[0]].append(future.result())
            except Exception as exc:
                if isinstance(owner, int):
                    original, family, failed_mode = candidates[owner], None, "system_proxy"
                else:
                    owner, family = owner
                    original, failed_mode = candidates[owner], "direct_pinned"
                rows[owner].append(_base_result(
                    original, family, location,
                    f"probe failed ({type(exc).__name__})",
                    "Probe raised an unexpected error before metrics were observed.",
                    family_tested=family is not None,
                    network_mode=failed_mode,
                ))
    family_order = {"ipv4": 0, "ipv6": 1, None: 0}
    return [
        row
        for index in range(len(candidates))
        for row in sorted(rows[index], key=lambda value: family_order.get(value.get("family"), 2))
    ]
