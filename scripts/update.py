"""Fetch, verify, retain last-good channels, and publish deterministic M3U files."""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from urllib.parse import parse_qsl, urlsplit, urlunsplit
import xml.etree.ElementTree as ET

import requests
import yaml

from .check import check_candidates
from .fetch import fetch_sources
from .generate import rank, render, select, validate
from .normalize import normalize_candidates
from .parse import parse_playlist


def load_yaml(path):
    with Path(path).open(encoding="utf-8") as stream:
        return yaml.safe_load(stream) or {}


def atomic_write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as temp:
        temp.write(text)
        name = temp.name
    os.replace(name, path)


def record_key(record):
    return hashlib.sha256("\0".join(str(record.get(key, "")) for key in ("name", "url", "family")).encode()).hexdigest()


def retain_history(checks, previous, names, threshold):
    """Only consecutive observed failures count; unchecked families are not failures."""
    state = {key: value for key, value in previous.items() if value.get("name") in names}
    directly_verified = {(item["name"], item["url"]) for item in checks
                         if item.get("ok") and item.get("family_observed")}
    state = {key: value for key, value in state.items()
             if value.get("family") is not None or (value["name"], value["url"]) not in directly_verified}
    for current in checks:
        if not current.get("ok") and current.get("family_tested") is False and current.get("network_mode") != "system_proxy":
            continue
        key = record_key(current)
        old = state.get(key, {})
        if current.get("ok"):
            state[key] = {**current, "failures": 0, "last_ok_at": current.get("checked_at"), "retained": False}
        else:
            failures = old.get("failures", 0) + 1
            if old.get("last_ok_at") and failures < threshold:
                state[key] = {**old, "failures": failures, "retained": True, "latest_error": current.get("error", "failed")}
            else:
                state[key] = {**current, "failures": failures, "last_ok_at": old.get("last_ok_at"), "retained": False}
    # History no longer advertised by any upstream is probed by the caller too.
    return state


def redact_url(url):
    parts = urlsplit(url)
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    if parts.port:
        host += f":{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, "[redacted]" if parts.query else "", ""))


def shortlist(items, previous, cap):
    """Give backup projects a fair chance instead of testing only the biggest source."""
    successes = {item["url"] for item in previous.values() if item.get("last_ok_at")}
    ordered = sorted(items, key=lambda item: (item["url"] not in successes, item.get("priority", 100), item["url"]))
    chosen = [item for item in ordered if item["url"] in successes][:cap]
    seen = {item["url"] for item in chosen}
    buckets = defaultdict(list)
    for item in ordered:
        if item["url"] not in seen:
            buckets[item.get("source", "unknown")].append(item)
    while len(chosen) < cap and any(buckets.values()):
        for bucket in buckets.values():
            if bucket and len(chosen) < cap:
                item = bucket.pop(0)
                if item["url"] not in seen:
                    chosen.append(item)
                    seen.add(item["url"])
    return chosen


def fetch_epg(config, channels, aliases, cache_dir):
    errors = []
    for url in config.get("urls", []):
        try:
            with requests.get(url, timeout=(4, 15), stream=True) as response:
                response.raise_for_status()
                body = bytearray()
                for block in response.iter_content(65536):
                    body.extend(block)
                    if len(body) > 40_000_000:
                        raise ValueError("EPG exceeds 40 MB limit")
            if b"<!DOCTYPE" in body or b"<!ENTITY" in body:
                raise ValueError("EPG entity declarations are not supported")
            root = ET.fromstring(body)
            if root.tag != "tv":
                raise ValueError("Not XMLTV")
            mapping = {}
            for item in root.findall("channel"):
                for label in [item.get("id", "")] + [node.text or "" for node in item.findall("display-name")]:
                    normalized = normalize_candidates([{"name": label, "url": "https://example.com/epg", "source": "epg", "priority": 0}], channels, aliases)
                    if normalized:
                        mapping.setdefault(normalized[0]["name"], item.get("id", ""))
            ids = set(mapping.values())
            for node in list(root):
                if node.tag == "channel" and node.get("id") not in ids:
                    root.remove(node)
                elif node.tag == "programme" and node.get("channel") not in ids:
                    root.remove(node)
            xml = ET.tostring(root, encoding="unicode", xml_declaration=True)
            atomic_write(cache_dir / "epg.xml", xml)
            return url, mapping, xml, errors
        except Exception as exc:
            errors.append({"url": redact_url(url), "error": type(exc).__name__})
    cached = cache_dir / "epg.xml"
    return (config.get("urls") or [""])[0], {}, cached.read_text(encoding="utf-8") if cached.exists() else None, errors


def run(args):
    root = Path(args.root).resolve()
    settings = load_yaml(root / "config/sources.yaml")
    channel_config = load_yaml(root / "config/channels.yaml")
    aliases = load_yaml(root / "config/aliases.yaml").get("aliases", {})
    channels = [{**item, "order": index} for index, item in enumerate(channel_config["channels"])]
    names = {item["name"] for item in channels}
    if len(names) != len(channels):
        raise ValueError("Duplicate configured channel names")
    options = settings.get("checking", {})
    cache = root / ".cache/iptv"
    output = root / "output"
    cache.mkdir(parents=True, exist_ok=True)
    output.mkdir(parents=True, exist_ok=True)
    state_path = output / "check_state.json"
    previous = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    enabled_sources = [source for source in settings["sources"] if source.get("enabled", True)]
    bodies, fetch_errors = fetch_sources(enabled_sources, cache, options.get("connect_timeout", 4), options.get("http_timeout", 12))
    raw = []
    for source in bodies:
        metadata = {key: source[key] for key in (
            "project_url", "rights_note", "format", "download_status", "used_cache", "cache_age_seconds"
        ) if key in source}
        raw.extend({**item, **metadata} for item in parse_playlist(
            source["body"], source["id"], source.get("priority", 100)
        ))
    raw.extend({**item, "priority": item.get("priority", 5)} for item in settings.get("candidates", []))
    normalized = normalize_candidates(raw, channels, aliases)
    previous_urls = {(item.get("name"), item.get("url")) for item in normalized}
    for old in previous.values():
        if old.get("name") in names and old.get("last_ok_at") and (old["name"], old["url"]) not in previous_urls:
            normalized.append(old)
            previous_urls.add((old["name"], old["url"]))
    grouped = defaultdict(list)
    for item in normalized:
        parts = urlsplit(item["url"])
        query_keys = {key.lower() for key, _ in parse_qsl(parts.query)}
        if parts.username or parts.password or query_keys & {"username", "password", "passwd"}:
            continue
        grouped[item["name"]].append(item)
    candidates = []
    cap = max(1, min(options.get("max_candidates_per_channel", 12), 30))
    for items in grouped.values():
        # Previous successes first, then trusted upstream priority; actual final
        # ordering still follows measured stability and startup time.
        candidates.extend(shortlist(items, previous, cap))
    print(f"Parsed {len(raw)} entries; checking {len(candidates)} deduplicated candidates for {len(grouped)} channels", flush=True)
    checks = check_candidates(candidates, options, location=args.location)
    state = retain_history(checks, previous, names, options.get("failure_threshold", 3))
    live = list(state.values())
    main = select(live, channels)
    epg_url, epg_map, epg_xml, epg_errors = fetch_epg(settings.get("epg", {}), channels, aliases, cache)
    if epg_xml and settings.get("epg", {}).get("published_url"):
        epg_url = settings["epg"]["published_url"]
    for collection in (channels, main):
        for item in collection:
            item["epg_id"] = epg_map.get(item["name"], (item.get("epg_names") or [item.get("tvg_id", "")])[0])
    previous_main = output / "iptv.m3u"
    old_count = validate(previous_main.read_text(encoding="utf-8")) if previous_main.exists() else 0
    degraded = not main or (old_count and len(main) < old_count * 0.6)
    payloads = {}
    if not degraded:
        variants = {
            "iptv.m3u": main, "main.m3u": main, "iptv_all.m3u": main,
            "iptv_ipv4.m3u": select(live, channels, "ipv4"),
            "iptv_ipv6.m3u": select(live, channels, "ipv6"),
            "elderly.m3u": [item for item in main if item.get("elderly")],
            "backup.m3u": [{**item, **item["alternates"][0]} for item in main if item.get("alternates")],
            "backup2.m3u": [{**item, **item["alternates"][1]} for item in main if len(item.get("alternates", [])) > 1],
        }
        for filename, items in variants.items():
            limit = channel_config.get("elderly_max", 60) if filename == "elderly.m3u" else channel_config.get("max_channels", 100)
            text = render(items, epg_url, limit)
            count = validate(text)
            # Preserve a last-good family playlist during a transient family outage.
            path = output / filename
            if not count and path.exists() and validate(path.read_text(encoding="utf-8")):
                continue
            payloads[path] = text
    report = {
        "updated_at": datetime.now(timezone.utc).isoformat(), "location": args.location,
        "status": "preserved_previous" if degraded and old_count else "failed" if degraded else "updated",
        "sources_downloaded_or_cached": len(bodies), "fetch_errors": fetch_errors,
        "parsed_entries": len(raw), "candidate_count": len(candidates),
        "currently_successful_checks": sum(bool(item.get("ok")) for item in checks),
        "published_channel_count": old_count if degraded else len(main),
        "elderly_channel_count": sum(bool(item.get("elderly")) for item in main),
        "missing_channels": [item["name"] for item in channels if item["name"] not in {x["name"] for x in main}],
        "retained_channels": sorted({item["name"] for item in main if item.get("retained")}),
        "epg_errors": epg_errors,
        "measurement_limits": "Short samples do not establish long-term uptime. Runner network is not the TV household network. Untested metrics remain null.",
        "checks": [{**item, "url": redact_url(item["url"])} for item in checks],
    }
    # All playlists are rendered and validated before replacing any published file.
    for path, text in payloads.items():
        atomic_write(path, text)
    if epg_xml:
        atomic_write(output / "epg.xml", epg_xml)
    atomic_write(output / "epg.json", json.dumps({"url": epg_url, "mapping": epg_map}, ensure_ascii=False, indent=2) + "\n")
    atomic_write(state_path, json.dumps(state, ensure_ascii=False, indent=2) + "\n")
    atomic_write(output / "report.json", json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({key: report[key] for key in ("status", "published_channel_count", "elderly_channel_count", "missing_channels")}, ensure_ascii=False), flush=True)
    return 1 if degraded and not old_count else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=Path(__file__).resolve().parents[1])
    parser.add_argument("--location", default="local")
    args = parser.parse_args()
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
