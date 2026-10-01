import unittest

from scripts.normalize import normalize_candidates
from scripts.parse import parse_playlist


CHANNELS = [
    {"name": "CCTV-5", "group": "央视频道", "elderly": True, "epg_names": ["CCTV5"]},
    {"name": "CCTV-17", "group": "央视频道", "elderly": False, "epg_names": []},
    {"name": "汕头", "group": "地方频道", "elderly": True, "epg_names": ["汕头台"]},
]


class ParseNormalizeTests(unittest.TestCase):
    def test_mixed_m3u_and_txt_keep_whitelisted_channels_and_merge_duplicates(self):
        m3u = '''\
#EXTM3U
#EXTVLCOPT:http-user-agent=ignored
#EXTINF:-1 tvg-id="cctv5" tvg-name="中央五套,体育" tvg-logo="https://logos.invalid/5.png" group-title="中央,体育",CCTV-5 HD,体育
https://user:pass@example.invalid/cctv5.m3u8
#EXTINF:-1 tvg-id=CCTV17 group-title="央视频道",CCTV-17
https://example.invalid/cctv17.m3u8
#EXTINF:-1 group-title="地方频道",汕头HD
https://example.invalid/shantou.m3u8
#EXTINF:-1,TEST
https://example.invalid/test.m3u8
#EXTINF:-1,购物频道
rtsp://example.invalid/shopping
'''
        txt = '''\
# This is a comment
地方频道,#genre#
汕头高清,https://example.invalid/shantou.m3u8
中央十七套,https://example.invalid/cctv17.m3u8
购物频道高清,https://example.invalid/shopping.m3u8
错误行,udp://example.invalid/ignored
'''

        candidates = parse_playlist(m3u, source="m3u-source", priority=20)
        candidates += parse_playlist(txt, source="txt-source", priority=10)

        self.assertEqual(len(candidates), 6)
        cctv5 = next(candidate for candidate in candidates if candidate["name"].startswith("CCTV-5"))
        self.assertEqual(cctv5["name"], "CCTV-5 HD,体育")
        self.assertEqual(cctv5["tvg_name"], "中央五套,体育")
        self.assertEqual(cctv5["group"], "中央,体育")
        self.assertEqual(cctv5["url"], "https://user:pass@example.invalid/cctv5.m3u8")

        normalized = normalize_candidates(
            candidates,
            CHANNELS,
            {"CCTV-5": ["中央五套"], "CCTV-17": ["中央十七套"]},
        )
        self.assertEqual({channel["name"] for channel in normalized}, {"CCTV-5", "CCTV-17", "汕头"})
        by_name = {channel["name"]: channel for channel in normalized}
        self.assertEqual(by_name["CCTV-5"]["group"], "央视频道")
        self.assertTrue(by_name["CCTV-5"]["elderly"])
        self.assertEqual(by_name["CCTV-5"]["logo"], "https://logos.invalid/5.png")
        self.assertEqual(by_name["CCTV-17"]["sources"], ["txt-source", "m3u-source"])
        self.assertEqual(by_name["汕头"]["sources"], ["txt-source", "m3u-source"])
        self.assertEqual(by_name["汕头"]["group"], "地方频道")
        self.assertEqual(len(normalized), 3)

    def test_unknown_names_do_not_match_by_substring(self):
        candidates = [
            {"name": "购物频道 CCTV-5 特价", "url": "https://example.invalid/shop", "source": "s"},
            {"name": "CCTV5", "url": "https://example.invalid/cctv5", "source": "s"},
        ]
        result = normalize_candidates(candidates, CHANNELS, {})
        self.assertEqual([item["name"] for item in result], ["CCTV-5"])

        fullwidth_hd = normalize_candidates(
            [{"name": "ＣＣＴＶ－５（高清）", "url": "https://example.invalid/fullwidth", "source": "s"}],
            CHANNELS,
            {},
        )
        self.assertEqual([item["name"] for item in fullwidth_hd], ["CCTV-5"])

        tvg_match = normalize_candidates(
            [{"name": "Provider label", "tvg_name": "CCTV5", "url": "https://example.invalid/tvg", "source": "s"}],
            CHANNELS,
            {},
        )
        self.assertEqual([item["name"] for item in tvg_match], ["CCTV-5"])

    def test_cctv_quality_and_tvg_id_suffixes_preserve_plus_identity(self):
        channels = [
            {"name": "CCTV-1 综合", "group": "央视", "elderly": True},
            {"name": "CCTV-5 体育", "group": "央视", "elderly": True},
            {"name": "CCTV-5+ 体育赛事", "group": "央视", "elderly": True},
            {"name": "广东新闻", "group": "广东", "elderly": True},
        ]
        candidates = [
            {"name": "CCTV-1 (720p)", "url": "https://example.invalid/cctv1-title"},
            {"name": "Provider label", "tvg_id": "CCTV1.cn@SD", "url": "https://example.invalid/cctv1-id"},
            {"name": "CCTV-5 (1080p)", "url": "https://example.invalid/cctv5"},
            {"name": "CCTV-5+ (720p)", "url": "https://example.invalid/cctv5plus"},
            {"name": "广东新闻[1920x1080]", "url": "https://example.invalid/guangdong-news"},
            {
                "name": "购物频道 CCTV-5 特价",
                "tvg_id": "shopping.cn@SD",
                "url": "https://example.invalid/shopping",
            },
        ]

        normalized = normalize_candidates(candidates, channels, {})

        by_url = {item["url"].rsplit("/", 1)[-1]: item["name"] for item in normalized}
        self.assertEqual(
            by_url,
            {
                "cctv1-title": "CCTV-1 综合",
                "cctv1-id": "CCTV-1 综合",
                "cctv5": "CCTV-5 体育",
                "cctv5plus": "CCTV-5+ 体育赛事",
                "guangdong-news": "广东新闻",
            },
        )


if __name__ == "__main__":
    unittest.main()
