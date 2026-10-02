import socket
import queue
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from scripts import check


class CheckTests(unittest.TestCase):
    def test_decoder_backpressure_does_not_drop_media_bytes(self):
        delivered = []
        attempts = 0

        def put(piece, timeout):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise queue.Full
            delivered.append(piece)

        sampler = object.__new__(check._FFmpegSampler)
        sampler.process = SimpleNamespace(poll=lambda: None)
        sampler.max_bytes = 300000
        sampler.bytes_fed = 0
        sampler._queue = SimpleNamespace(put=put)
        media = b"a" * 65536 + b"b" * 65536
        self.assertTrue(sampler.feed(media, time.monotonic() + 1))
        self.assertEqual(b"".join(delivered), media)

    def test_private_dns_answer_is_rejected_before_connect(self):
        rows = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))]
        with mock.patch.object(check.socket, "getaddrinfo", return_value=rows):
            with self.assertRaisesRegex(check.ProbeError, "non-public"):
                check._resolve_host("public-looking.example", 1.0, False)

    def test_family_requires_observed_socket_peer(self):
        class FailingFetcher:
            def __init__(self, *args, **kwargs):
                self.last_family = None
                self.last_http_ms = None

            def get(self, _url):
                raise check.ProbeError("HTTP connection failed")

        candidate = {"name": "demo", "url": "https://example.com/live.m3u8"}
        options = check._check_options({"probe_timeout": 1})
        with mock.patch.object(check, "_FamilyFetcher", FailingFetcher):
            result = check._probe_family(
                candidate, "ipv6", ["2606:2800:220:1:248:1893:25c8:1946"], options, "local"
            )
        self.assertFalse(result["ok"])
        self.assertFalse(result["family_observed"])
        self.assertEqual(result["family"], "ipv6")  # attempted family, explicitly unobserved

    def test_html_payload_is_rejected_without_starting_ffmpeg(self):
        class FakeResponse:
            headers = {"Content-Type": "text/html; charset=utf-8"}
            url = "https://example.com/live"

            def iter_content(self, chunk_size):
                yield b"<html><body>login</body></html>"

            def close(self):
                pass

        class FakeFetched:
            response = FakeResponse()
            family = "ipv4"
            elapsed_ms = 2.0

            def close(self):
                self.response.close()

        class FakeFetcher:
            last_family = "ipv4"
            last_http_ms = 2.0

            def __init__(self, *args, **kwargs):
                pass

            def get(self, _url):
                return FakeFetched()

        candidate = {"name": "demo", "url": "http://example.com/live"}
        options = check._check_options({})
        with mock.patch.object(check, "_FamilyFetcher", FakeFetcher), mock.patch.object(
            check, "_FFmpegSampler", side_effect=AssertionError("must not decode HTML")
        ):
            result = check._probe_family(candidate, "ipv4", ["93.184.216.34"], options, "local")
        self.assertFalse(result["ok"])
        self.assertIn("HTML", result["error"])
        self.assertTrue(result["family_observed"])

    def test_forbidden_hls_segment_is_rejected_before_starting_ffmpeg(self):
        class FakeResponse:
            headers = {"Content-Type": "application/vnd.apple.mpegurl"}
            url = "https://stream.example/live/index.m3u8"

            def iter_content(self, chunk_size):
                yield (
                    b"#EXTM3U\n#EXTINF:4.0,\n"
                    b"../../FORBID/1e547e6b/128.ts?expires=1\n"
                )

            def close(self):
                pass

        class FakeFetched:
            response = FakeResponse()
            family = "ipv4"
            elapsed_ms = 2.0

            def close(self):
                self.response.close()

        class FakeFetcher:
            gets = 0

            def __init__(self, *args, **kwargs):
                pass

            def get(self, _url):
                self.gets += 1
                return FakeFetched()

        candidate = {"name": "demo", "url": "https://stream.example/live/index.m3u8"}
        options = check._check_options({"reject_segment_paths": ["/forbid/"]})
        with mock.patch.object(check, "_FamilyFetcher", FakeFetcher), mock.patch.object(
            check, "_FFmpegSampler", side_effect=AssertionError("must not decode forbidden HLS")
        ):
            result = check._probe_family(candidate, "ipv4", ["93.184.216.34"], options, "local")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "HLS contains an access-denied placeholder")

    def test_normal_baicheng_hls_segment_path_is_not_rejected(self):
        urls = [("https://stream2.jlntv.cn/baicheng1_sd/128.ts?x=1", 4.0)]
        check._reject_hls_segment_paths(urls, ["/forbid/"])

    def test_encrypted_hls_is_rejected(self):
        playlist = (
            b"#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI=\"key.bin\"\n"
            b"#EXTINF:4.0,\nsegment.ts\n"
        )
        with self.assertRaisesRegex(check.ProbeError, "encrypted HLS"):
            check._parse_playlist(playlist, "https://example.com/live.m3u8")

    def test_peer_family_comes_from_socket_address_shape(self):
        class FakeSocket:
            def getpeername(self):
                return ("2001:4860:4860::8888", 443, 0, 0)

        response = SimpleNamespace(
            raw=SimpleNamespace(_connection=SimpleNamespace(sock=FakeSocket()))
        )
        self.assertEqual(check._response_peer_family(response), "ipv6")

    def test_pinned_transport_uses_ip_but_keeps_original_hostname(self):
        created = []

        class FakeSocket:
            def __init__(self, family, socktype):
                self.family = family
                self.connected_to = None
                created.append(self)

            def settimeout(self, _timeout):
                pass

            def setsockopt(self, *_args):
                pass

            def connect(self, address):
                self.connected_to = address

            def getpeername(self):
                return (self.connected_to[0], self.connected_to[1], 0, 0)

            def close(self):
                pass

        observed = []
        with mock.patch.object(check.socket, "socket", FakeSocket):
            pool = check._PinnedHTTPSConnectionPool(
                "cdn.example", 443, pinned_ip="2001:4860:4860::8888", timeout=1.0,
                peer_callback=lambda peer: observed.append(check._family_from_peer(peer)),
            )
            connection = pool._new_conn()
            sock = connection._new_conn()
        self.assertEqual(connection.host, "cdn.example")
        self.assertEqual(sock.family, socket.AF_INET6)
        self.assertEqual(sock.connected_to[0], "2001:4860:4860::8888")
        self.assertEqual(observed, ["ipv6"])

    def test_config_timeout_and_concurrency_names_are_honored(self):
        options = check._check_options({
            "concurrency": 30,
            "connect_timeout": 4,
            "http_timeout": 12,
            "stream_timeout": 12,
        })
        self.assertEqual(options["max_workers"], 24)
        self.assertEqual(options["connect_timeout"], 4)
        self.assertEqual(options["read_timeout"], 12)
        self.assertEqual(options["probe_timeout"], 12)

    def test_candidate_checks_respect_worker_limit(self):
        guard = threading.Lock()
        active = 0
        peak = 0

        def prepare(candidate, _options):
            return {
                "candidate": candidate,
                "error": None,
                "addresses": ["93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"],
            }

        def probe(candidate, family, _ips, _options, location):
            nonlocal active, peak
            with guard:
                active += 1
                peak = max(peak, active)
            time.sleep(0.02)
            with guard:
                active -= 1
            return check._base_result(candidate, family, location, "", "mocked")

        candidates = [{"url": f"https://example.com/{index}"} for index in range(4)]
        with mock.patch.object(check, "_prepare_candidate", side_effect=prepare), mock.patch.object(
            check, "_probe_family", side_effect=probe
        ), mock.patch.object(check.requests.utils, "get_environ_proxies", return_value={}):
            results = check.check_candidates(
                candidates, {"max_workers": 2, "network_mode": "direct"}
            )
        self.assertEqual(len(results), 8)
        self.assertLessEqual(peak, 2)

    def test_auto_system_proxy_probes_once_without_claiming_upstream_family(self):
        candidate = {"name": "demo", "url": "https://example.com/live.m3u8"}
        probe_args = []

        def prepare(value, _options):
            return {"candidate": value, "error": None, "addresses": ["93.184.216.34"]}

        def probe(value, family, addresses, options, location):
            probe_args.append((family, addresses, options["probe_network_mode"]))
            return check._base_result(
                value, None, location, "", "mock proxy decode", family_tested=False,
                network_mode="system_proxy",
            )

        # Keep proxy details opaque: user/password must never appear in result rows.
        fake_proxy = "http://user:secret@127.0.0.1:7897"
        with mock.patch.object(check, "_prepare_candidate", side_effect=prepare), mock.patch.object(
            check, "_probe_family", side_effect=probe
        ), mock.patch.object(
            check.requests.utils, "get_environ_proxies", return_value={"https": fake_proxy}
        ):
            rows = check.check_candidates([candidate], {"network_mode": "auto"})

        self.assertEqual(len(rows), 1)
        self.assertEqual(probe_args, [(None, ["93.184.216.34"], "system_proxy")])
        self.assertIsNone(rows[0]["family"])
        self.assertFalse(rows[0]["family_tested"])
        self.assertFalse(rows[0]["family_observed"])
        self.assertEqual(rows[0]["network_mode"], "system_proxy")
        self.assertNotIn("secret", repr(rows[0]))

    def test_missing_dns_family_is_explicitly_untested(self):
        candidate = {"name": "demo", "url": "https://example.com/live.m3u8"}

        def prepare(value, _options):
            return {"candidate": value, "error": None, "addresses": ["93.184.216.34"]}

        def probe(value, family, _addresses, _options, location):
            return check._base_result(value, family, location, "mocked failure", "mocked")

        with mock.patch.object(check, "_prepare_candidate", side_effect=prepare), mock.patch.object(
            check, "_probe_family", side_effect=probe
        ), mock.patch.object(check.requests.utils, "get_environ_proxies", return_value={}):
            rows = check.check_candidates([candidate], {"network_mode": "direct"})

        ipv6 = next(row for row in rows if row["family"] == "ipv6")
        self.assertFalse(ipv6["family_tested"])
        self.assertFalse(ipv6["family_observed"])

    def test_master_playlist_exposes_selected_variant_metrics(self):
        body = (
            b"#EXTM3U\n"
            b"#EXT-X-STREAM-INF:BANDWIDTH=900000,RESOLUTION=1280x720\n720.m3u8\n"
            b"#EXT-X-STREAM-INF:BANDWIDTH=2500000,RESOLUTION=1920x1080\n1080.m3u8\n"
        )
        parsed = check._parse_playlist(body, "https://example.com/master.m3u8")
        self.assertEqual(parsed["variant_url"], "https://example.com/1080.m3u8")
        self.assertEqual(parsed["resolution"], [1920, 1080])
        self.assertEqual(parsed["bandwidth_kbps"], 2500)


if __name__ == "__main__":
    unittest.main()
