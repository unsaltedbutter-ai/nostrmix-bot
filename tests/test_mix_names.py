"""Tests for the friendly mix-name generator (src/mix_names.py)."""

import os
import sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import random
from src import mix_names as mn


class TestWordLists:
    def test_lowercase_single_token_no_dups(self):
        for word in mn.ADJECTIVES + mn.NOUNS:
            assert word == word.lower(), f"{word!r} not lowercase"
            assert word.isalpha(), f"{word!r} has non-letters"
        assert len(mn.ADJECTIVES) == len(set(mn.ADJECTIVES)), "duplicate adjective"
        assert len(mn.NOUNS) == len(set(mn.NOUNS)), "duplicate noun"

    def test_disjoint(self):
        # A word in both buckets would let random_name produce e.g. "rose-rose".
        assert set(mn.ADJECTIVES).isdisjoint(set(mn.NOUNS))

    def test_short_and_common(self):
        # Memorability: every word is at least 3 letters, and most are short.
        for w in mn.ADJECTIVES + mn.NOUNS:
            assert 3 <= len(w) <= 8, f"{w!r} is an awkward length"
        all_words = mn.ADJECTIVES + mn.NOUNS
        short = sum(1 for w in all_words if len(w) <= 5)
        assert short / len(all_words) >= 0.6, "too many long words for easy typing"

    def test_no_offensive_words(self):
        # Exact-match denylist (NOT substring — "glass" contains "ass" etc.).
        banned = {
            "sex", "ass", "shit", "damn", "crap", "piss", "cock", "dick",
            "fuck", "tit", "cum", "anus", "butt", "turd",
        }
        offenders = [w for w in mn.ADJECTIVES + mn.NOUNS if w in banned]
        assert offenders == [], f"offensive words present: {offenders}"

    def test_no_unfortunate_combinations(self):
        # Some color+object pairs are NSFW slang even though each word is clean.
        # Guard the known ones so a future word addition can't resurrect them.
        bad_pairs = {("blue", "waffle"), ("cream", "pie")}
        adj, noun = set(mn.ADJECTIVES), set(mn.NOUNS)
        present = [f"{a}-{n}" for a, n in bad_pairs if a in adj and n in noun]
        assert present == [], f"unfortunate name(s) possible: {present}"


class TestGenerators:
    def test_random_name_structure(self):
        rng = random.Random(0)
        for _ in range(200):
            name = mn.random_name(rng)
            a, n = name.split("-")
            assert a in mn.ADJECTIVES and n in mn.NOUNS

    def test_name_from_index_is_collision_free(self):
        # Every distinct k maps to a distinct name, including past SPACE where a
        # numeric suffix kicks in.
        names = [mn.name_from_index(k) for k in range(mn.SPACE + 500)]
        assert len(set(names)) == len(names)

    def test_name_from_index_negative_rejected(self):
        import pytest
        with pytest.raises(ValueError):
            mn.name_from_index(-1)
