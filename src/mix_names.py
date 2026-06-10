"""Friendly mix names — simple `adjective-noun` phrases like `brave-otter`.

Every word is drawn from the **BIP-39 English wordlist** (the 2048 words used for
Bitcoin seed phrases), hand-sorted into adjective and noun buckets. BIP-39 words
are short, common, and unambiguous (each is unique in its first four letters),
which makes the names easy to read and type.

Two ways to get a name:

- ``random_name()`` picks an adjective and a noun at random. The mixer only needs
  a name to be unique among *currently live* mixes (finished/failed mixes are
  destroyed), so collisions are checked at creation time and simply retried — a
  finite space is fine. See scripts/mix_names.py for the collision numbers.
- ``name_from_index(k)`` maps a UNIQUE integer ``k`` to a name deterministically
  (distinct ``k`` -> distinct name; a numeric suffix past ``SPACE``). Useful if
  you ever want guaranteed-unique-forever names from a counter.

Names are joined with ``sep`` (default ``-``) so they stay a single token — the
DM protocol's ``/join <mix_name>`` reads one whitespace-delimited word, so a
space ("brave otter") would only parse as "brave".
"""
from __future__ import annotations

import math as _math
import random as _random
from typing import Optional

# All words below are members of the BIP-39 English wordlist. Adjectives and
# nouns are disjoint (a word in both bucket would allow "velvet-velvet").
_ADJECTIVES = [
    "able", "absurd", "acoustic", "ancient", "arctic", "awesome", "bitter",
    "brave", "bright", "brisk", "brown", "calm", "clever", "cool", "crazy",
    "crisp", "curious", "cute", "dizzy", "eager", "early", "easy", "elegant",
    "fancy", "flat", "fresh", "funny", "gentle", "giant", "glad", "good",
    "great", "green", "happy", "harsh", "heavy", "huge", "humble", "hungry",
    "keen", "kind", "large", "lazy", "light", "little", "lonely", "loud",
    "loyal", "lucky", "lunar", "mad", "major", "merry", "narrow", "noble",
    "polar", "pretty", "proud", "quick", "rapid", "rare", "raw", "rich",
    "royal", "rural", "sad", "short", "shy", "silent", "silly", "slim", "slow",
    "small", "smart", "smooth", "soft", "solid", "sunny", "super", "sweet",
    "swift", "tiny", "true", "useful", "vast", "warm", "wide", "wild", "wise",
    "young",
]

_NOUNS = [
    "anchor", "animal", "antenna", "antique", "apple", "april", "arch", "arrow",
    "artwork", "autumn", "avocado", "badge", "banana", "basket", "beach", "bean",
    "beauty", "bench", "bicycle", "bird", "blossom", "boat", "bone", "book",
    "bridge", "bubble", "cabbage", "cabin", "cactus", "cake", "camera", "camp",
    "candy", "cannon", "canoe", "canyon", "carpet", "castle", "cat", "cattle",
    "cave", "cherry", "chicken", "city", "cloth", "cloud", "clown", "coast",
    "coconut", "coffee", "coral", "cotton", "couch", "country", "cradle", "crane",
    "cricket", "crystal", "cup", "deer", "desert", "diamond", "dolphin", "donkey",
    "door", "dove", "dragon", "drum", "duck", "eagle", "egg", "elephant", "engine",
    "envelope", "fabric", "fence", "fiber", "field", "finger", "fish", "flag",
    "flame", "flower", "foam", "forest", "fox", "frog", "garden", "ghost",
    "giraffe", "glass", "glove", "goat", "guitar", "hammer", "hamster", "harbor",
    "hawk", "hill", "honey", "horse", "ice", "island", "ivory", "jacket", "jaguar",
    "jar", "jelly", "kangaroo", "kitten", "lake", "lamp", "lawn", "leaf", "lemon",
    "leopard", "lion", "lizard", "lobster", "maple", "marble", "meadow", "monkey",
    "moon", "mountain", "mouse", "muffin", "mushroom", "nest", "ocean", "olive",
    "onion", "orange", "orbit", "orchard", "ostrich", "oyster", "palace", "panda",
    "parrot", "peanut", "pepper", "piano", "pigeon", "pizza", "planet", "pond",
    "pony", "potato", "puppy", "rabbit", "raccoon", "radio", "raven", "ribbon",
    "river", "rocket", "rose", "salmon", "sand", "shell", "ship", "shrimp",
    "snake", "soda", "spider", "spoon", "squirrel", "sugar", "sunset", "tiger",
    "toast", "tomato", "tornado", "tortoise", "tower", "town", "train", "tree",
    "trumpet", "turkey", "turtle", "umbrella", "valley", "velvet", "village",
    "violin", "volcano", "walnut", "wasp", "whale", "wheat", "wolf", "zebra",
]


def _dedup(seq):
    """Order-preserving de-duplication."""
    return list(dict.fromkeys(seq))


ADJECTIVES = _dedup(_ADJECTIVES)
NOUNS = _dedup(_NOUNS)
SPACE = len(ADJECTIVES) * len(NOUNS)  # distinct adjective-noun pairs


def _coprime_near(target: int, n: int) -> int:
    """Smallest odd number >= target that is coprime to n (so `*c mod n` is a
    bijection on [0, n))."""
    c = max(1, target) | 1
    while _math.gcd(c, n) != 1:
        c += 2
    return c


# Multiplying the index by a constant coprime to SPACE, mod SPACE, is a bijection
# on [0, SPACE) — so it stays collision-free but scatters CONSECUTIVE indices
# across the whole space (otherwise k=0,1,2,... would share a noun many times in
# a row, leaking creation order). ~golden-ratio start spreads well.
_SCRAMBLE = _coprime_near(int(SPACE * 0.6180339887), SPACE) if SPACE > 1 else 1


def random_name(rng: Optional[_random.Random] = None, sep: str = "-") -> str:
    """A random ``adjective<sep>noun`` name. Check uniqueness at the call site
    (against currently-live mixes) and retry on the rare collision."""
    r = rng or _random
    return f"{r.choice(ADJECTIVES)}{sep}{r.choice(NOUNS)}"


def name_from_index(k: int, sep: str = "-") -> str:
    """Deterministic, collision-free mapping of a UNIQUE integer ``k`` to a name.

    For ``0 <= k < SPACE`` every value yields a distinct adjective-noun pair. For
    ``k >= SPACE`` a numeric suffix keeps it unique (``...-2``, ``...-3``, ...).
    """
    if k < 0:
        raise ValueError("k must be non-negative")
    # Scramble within the current cycle so consecutive k look varied while every
    # distinct k still maps to a distinct (pair, cycle) -> a distinct name.
    idx = (k % SPACE) * _SCRAMBLE % SPACE
    a = ADJECTIVES[idx % len(ADJECTIVES)]
    n = NOUNS[idx // len(ADJECTIVES)]
    name = f"{a}{sep}{n}"
    cycle = k // SPACE
    return name if cycle == 0 else f"{name}{sep}{cycle + 1}"
