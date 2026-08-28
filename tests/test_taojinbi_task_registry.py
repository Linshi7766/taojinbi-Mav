import unittest

from taojinbi_mav.tasks import TaskProfile
from taojinbi_mav.tasks.registry import (
    profile_for_row,
    profile_for_title,
    registered_profiles,
)


class TaskProfileInvariantTests(unittest.TestCase):
    def test_profile_requires_exactly_one_title_match_mode(self):
        with self.assertRaises(ValueError):
            TaskProfile(
                key="invalid",
                strategy="feed_browse",
                title_prefix=None,
                exact_title=None,
                description_required=True,
                allow_dynamic_total=False,
                rotating_title=False,
            )

    def test_profile_rejects_two_title_match_modes_when_one_is_empty(self):
        with self.assertRaises(ValueError):
            TaskProfile(
                key="invalid",
                strategy="feed_browse",
                title_prefix="搜一搜",
                exact_title="",
                description_required=True,
                allow_dynamic_total=False,
                rotating_title=False,
            )

    def test_profile_rejects_empty_title_prefix(self):
        with self.assertRaises(ValueError):
            TaskProfile(
                key="invalid",
                strategy="feed_browse",
                title_prefix="",
                exact_title=None,
                description_required=True,
                allow_dynamic_total=False,
                rotating_title=False,
            )

    def test_profile_rejects_empty_exact_title(self):
        with self.assertRaises(ValueError):
            TaskProfile(
                key="invalid",
                strategy="feed_browse",
                title_prefix=None,
                exact_title="",
                description_required=True,
                allow_dynamic_total=False,
                rotating_title=False,
            )


class SearchTaskProfileTests(unittest.TestCase):
    def test_search_is_the_first_registered_profile(self):
        self.assertEqual(registered_profiles()[0].key, "search")

    def test_search_matches_prefix_without_description(self):
        profile = profile_for_row("搜一搜你心仪的宝贝", "")
        self.assertIsNotNone(profile)
        self.assertEqual(profile.key, "search")
        self.assertEqual(profile.strategy, "search_discovery_browse")
        self.assertFalse(profile.description_required)
        self.assertFalse(profile.allow_dynamic_total)
        self.assertFalse(profile.rotating_title)

    def test_search_identity_does_not_merge_different_titles(self):
        profile = profile_for_title("搜一搜你心仪的宝贝")
        self.assertTrue(
            profile.same_identity(
                "搜一搜你心仪的宝贝",
                "搜一搜你心仪的宝贝",
            )
        )
        self.assertFalse(
            profile.same_identity(
                "搜一搜你心仪的宝贝",
                "搜一搜其他任务",
            )
        )

    def test_unregistered_tasks_fail_closed(self):
        self.assertIsNone(profile_for_title("拍立淘逛感兴趣的宝贝"))


class HashtagTaskProfileTests(unittest.TestCase):
    def test_hashtag_is_registered_after_search(self):
        self.assertEqual(
            tuple(profile.key for profile in registered_profiles()[:2]),
            ("search", "hashtag"),
        )

    def test_hashtag_accepts_missing_description_and_uses_feed(self):
        profile = profile_for_row("看看#斯维诗鱼油", "")
        self.assertIsNotNone(profile)
        self.assertEqual(profile.strategy, "feed_browse")
        self.assertTrue(profile.allow_dynamic_total)
        self.assertTrue(profile.rotating_title)

    def test_hashtag_merges_rotating_topic_titles_only(self):
        profile = profile_for_title("看看#斯维诗鱼油")
        self.assertTrue(
            profile.same_identity("看看#斯维诗鱼油", "看看#鱼油生发")
        )
        self.assertFalse(
            profile.same_identity("看看#斯维诗鱼油", "看看你感兴趣的宝贝")
        )


class FeaturedGoodsTaskProfileTests(unittest.TestCase):
    def test_final_registry_contains_exactly_three_profiles(self):
        self.assertEqual(
            tuple(profile.key for profile in registered_profiles()),
            ("search", "hashtag", "featured_goods"),
        )

    def test_featured_goods_requires_exact_title_and_browse_description(self):
        self.assertIsNone(profile_for_row("发现精选好物", ""))
        profile = profile_for_row("发现精选好物", "浏览 +50")
        self.assertIsNotNone(profile)
        self.assertEqual(profile.strategy, "feed_browse")
        self.assertTrue(profile.description_required)
        self.assertFalse(profile.allow_dynamic_total)
        self.assertFalse(profile.rotating_title)
        self.assertIsNone(profile_for_row("发现精选好物推荐", "浏览"))

    def test_removed_families_remain_unregistered(self):
        for title in (
            "拍立淘逛感兴趣的宝贝",
            "酒店超抵日至高5%",
            "去省钱卡领红包",
            "淘金币充话费可抵钱",
            "好物沉浸看",
            "逛逛金币加抵好货",
            "逛好店赚一大波金币",
        ):
            with self.subTest(title=title):
                self.assertIsNone(profile_for_title(title))


class SafeLabelTests(unittest.TestCase):
    def test_safe_label_redacts_rotating_titles(self):
        self.assertEqual(
            profile_for_title("搜一搜你心仪的宝贝").safe_label(), "搜一搜…"
        )
        self.assertEqual(
            profile_for_title("看看#斯维诗鱼油").safe_label(), "看看#…"
        )
        self.assertEqual(
            profile_for_title("发现精选好物").safe_label(), "发现精选好物"
        )


if __name__ == "__main__":
    unittest.main()
