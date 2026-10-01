import unittest

from scripts.generate import render, select, validate
from scripts.update import retain_history, record_key


class PublicationTests(unittest.TestCase):
    def test_main_only_shows_one_channel_and_backups_are_separate(self):
        records = [
            {"name": "CCTV-1 综合", "url": "https://example.com/a", "ok": True, "family": "ipv4", "first_frame_ms": 500, "stalls": 0, "sample_seconds": 3},
            {"name": "CCTV-1 综合", "url": "https://example.com/b", "ok": True, "family": "ipv6", "first_frame_ms": 900, "stalls": 0, "sample_seconds": 3},
        ]
        selected = select(records, [{"name": "CCTV-1 综合", "group": "央视", "elderly": True}])
        self.assertEqual(validate(render(selected, "https://example.com/epg.xml")), 1)
        self.assertEqual(selected[0]["url"], "https://example.com/a")
        self.assertEqual(len(selected[0]["alternates"]), 1)
        self.assertEqual(len(select(records, [{"name": "CCTV-1 综合"}], "ipv6")), 1)

    def test_failures_retain_then_expire_last_good_stream(self):
        good = {"name": "汕头综合", "url": "https://example.com/live", "family": "ipv4", "ok": True, "checked_at": "2026-10-02T00:00:00Z"}
        names = {"汕头综合"}
        state = retain_history([good], {}, names, 3)
        failure = {**good, "ok": False, "error": "timeout"}
        state = retain_history([failure], state, names, 3)
        self.assertTrue(state[record_key(good)]["ok"])
        self.assertTrue(state[record_key(good)]["retained"])
        state = retain_history([failure], state, names, 3)
        self.assertTrue(state[record_key(good)]["ok"])
        state = retain_history([failure], state, names, 3)
        self.assertFalse(state[record_key(good)]["ok"])

    def test_removed_channel_does_not_persist_in_history(self):
        record = {"name": "购物台", "url": "https://example.com/shop", "family": "ipv4", "ok": True}
        self.assertEqual(retain_history([], {record_key(record): record}, {"CCTV-1 综合"}, 3), {})

    def test_unavailable_family_does_not_expire_previous_stream(self):
        good = {"name": "汕头综合", "url": "https://example.com/live", "family": "ipv6", "ok": True, "checked_at": "today"}
        state = retain_history([good], {}, {good["name"]}, 3)
        untested = {**good, "ok": False, "family_tested": False, "error": "no ipv6 DNS address"}
        for _ in range(4):
            state = retain_history([untested], state, {good["name"]}, 3)
        self.assertEqual(state[record_key(good)]["failures"], 0)

    def test_direct_success_replaces_unverified_proxy_history(self):
        proxy = {"name": "CCTV-1 综合", "url": "https://example.com/live", "family": None, "ok": True, "checked_at": "old"}
        state = retain_history([proxy], {}, {proxy["name"]}, 3)
        direct = {**proxy, "family": "ipv4", "family_observed": True, "checked_at": "new"}
        state = retain_history([direct], state, {proxy["name"]}, 3)
        self.assertNotIn(record_key(proxy), state)
        self.assertIn(record_key(direct), state)

    def test_duplicate_or_incomplete_playlist_is_rejected(self):
        item = {"name": "汕头体育", "url": "https://example.com/live", "group": "汕头"}
        with self.assertRaises(ValueError):
            render([item, item])
        with self.assertRaises(ValueError):
            validate(render([item]).rsplit("\n", 2)[0] + "\n")


if __name__ == "__main__":
    unittest.main()
