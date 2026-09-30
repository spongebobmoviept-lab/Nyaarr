"""Small unit tests for the release-classification and filtering rules.

Run with:  python -m unittest discover -s tests
(or inside the image:  docker run --rm -v "$PWD/tests:/app/tests" nyaarr python -m unittest discover -s tests)
"""

import os
import tempfile
import unittest

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="nyaarr-test-"))

from app import curation  # noqa: E402
from app.classifier import classify_release, matches_preference  # noqa: E402
from app.config import settings  # noqa: E402
from app.sonarr import SonarrRelease  # noqa: E402


def _release(title: str, guid: str = "g", seeders: int = 10) -> SonarrRelease:
    return SonarrRelease(guid=guid, indexer_id=1, title=title, seeders=seeders, size=0, rejected=False, rejections=[])


class ClassifierTests(unittest.TestCase):
    def test_vostfr_is_foreign_sub(self):
        c = classify_release("[Team] Show - 05 VOSTFR 1080p")
        self.assertEqual(c.kind, "foreign_sub")
        self.assertEqual(c.confidence, "high")
        self.assertFalse(matches_preference(c, "prefer_sub"))

    def test_language_named_subs_are_foreign(self):
        for title in ("Show S01E02 German Sub 1080p", "Show 03 Spanish-Subbed", "Show 04 GerSub", "Show 05 ESP SUB"):
            with self.subTest(title=title):
                self.assertEqual(classify_release(title).kind, "foreign_sub")

    def test_english_marker_wins_over_foreign(self):
        c = classify_release("Show 06 ENG SUB + VOSTFR")
        self.assertEqual(c.kind, "sub")
        self.assertTrue(matches_preference(c, "prefer_sub"))

    def test_bare_subbed_is_unknown(self):
        c = classify_release("Show - 07 Subbed 720p")
        self.assertEqual(c.kind, "unknown")
        self.assertFalse(matches_preference(c, "prefer_sub"))

    def test_known_fansub_group_still_sub(self):
        c = classify_release("[SubsPlease] Show - 08 (1080p)", ["SubsPlease"])
        self.assertEqual(c.kind, "sub")


class OpusSettingTests(unittest.TestCase):
    def setUp(self):
        self._saved = (settings.block_opus_audio, settings.known_fansub_groups, settings.min_seeders)
        settings.known_fansub_groups = ["SubsPlease"]
        settings.min_seeders = 1

    def tearDown(self):
        settings.block_opus_audio, settings.known_fansub_groups, settings.min_seeders = self._saved

    def _pick(self):
        releases = [
            _release("[SubsPlease] Show - 01 (1080p) [Opus 2.0]", guid="opus", seeders=100),
            _release("[SubsPlease] Show - 01 (1080p) [AAC]", guid="aac", seeders=5),
        ]
        return [c["guid"] for c in curation._pick_ranked_candidates(releases, [], "prefer_sub")]

    def test_opus_allowed_by_default(self):
        settings.block_opus_audio = False
        self.assertEqual(self._pick(), ["opus", "aac"])

    def test_opus_blocked_when_enabled(self):
        settings.block_opus_audio = True
        self.assertEqual(self._pick(), ["aac"])

    def test_opus_word_match_only(self):
        self.assertTrue(curation.is_opus_release("Show 01 [OPUS]"))
        self.assertFalse(curation.is_opus_release("Magnum Opusculum 01"))


class NewEpisodeWindowTests(unittest.TestCase):
    def setUp(self):
        self._saved = settings.new_episodes_only_days

    def tearDown(self):
        settings.new_episodes_only_days = self._saved

    def test_default_window(self):
        settings.new_episodes_only_days = 7
        self.assertTrue(curation._within_new_episode_window(0.5))
        self.assertTrue(curation._within_new_episode_window(7))
        self.assertFalse(curation._within_new_episode_window(8))
        self.assertFalse(curation._within_new_episode_window(None))

    def test_zero_means_whole_backlog(self):
        settings.new_episodes_only_days = 0
        self.assertTrue(curation._within_new_episode_window(900))
        self.assertTrue(curation._within_new_episode_window(None))


if __name__ == "__main__":
    unittest.main()
