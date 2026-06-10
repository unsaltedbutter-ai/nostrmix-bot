"""Friendly mix names — simple `adjective-object` phrases like `big-apple`.

Optimised for a human who reads a name in a Nostr note, goes off to gather their
inputs for a few minutes, then has to retype it from memory in a DM — often on a
phone keyboard. So the words are chosen to be **short and very common**:

- adjectives are simple everyday descriptors: the basic colors (red, blue,
  green…) plus plain words like big, little, soft, hard, wet, dry, open, smooth,
- nouns are everyday **things** — objects, food, animals, people — every one
  chosen to be **3 or 4 letters** and very common (bag, dog, lamp, mug…).

The only rules are common, short, and inoffensive (not constrained to BIP-39 —
that pulled in rare words like "gosling"). The noun list is kept to 3-4 letter
words on purpose: there are easily enough common ones to make the name space
large (79 adjectives × ~250 nouns ≈ 19k pairs), so collisions among the ≤10
live mixes stay well under 1% without ever reaching for a longer word.

Two ways to get a name:

- ``random_name()`` picks a color and an object at random. The mixer only needs a
  name to be unique among *currently live* mixes (1-10 at a time; finished/failed
  mixes are destroyed), so a clash is checked at creation and simply retried.
- ``name_from_index(k)`` maps a UNIQUE integer ``k`` to a name deterministically
  (distinct ``k`` -> distinct name; a numeric suffix past ``SPACE``).

Names are joined with ``sep`` (default ``-``) so they stay a single token — the
DM protocol's ``/join <mix_name>`` reads one whitespace-delimited word.
"""
from __future__ import annotations

import math as _math
import random as _random
from typing import Optional

# Simple, common adjectives: the basic colors plus everyday descriptors
# (size, texture, state, temperature, speed). Child-simple and quick to type.
_ADJECTIVES = [
    "bent", "big", "black", "blue", "bold", "bright", "broken", "brown",
    "bumpy", "clean", "cold", "cool", "dark", "dry", "easy", "empty", "extra",
    "fancy", "fast", "flat", "fluffy", "free", "fresh", "full", "fuzzy", "good",
    "gray", "green", "handy", "hard", "hot", "huge", "icy", "jumbo", "juicy",
    "large", "light", "little", "long", "loose", "mega", "mini", "neat", "new",
    "nice", "odd", "old", "open", "orange", "plain", "purple", "quick", "red",
    "rough", "round", "safe", "salty", "sharp", "shiny", "short", "simple",
    "slow", "small", "smooth", "soft", "sour", "spare", "spicy", "super",
    "sweet", "tall", "tasty", "tight", "tiny", "warm", "wet", "white", "wide",
    "yellow",
]

# Everyday nouns — objects, food, animals, people. Every word is 3-4 letters and
# very common, so a name is quick to read and retype from memory on a phone.
# (No 5+ letter words: there are more than enough short common nouns to keep the
# space large, so we never need a "webcam" where "web" does the same job.)
_NOUNS = [
    "ant", "ape", "bag", "ball", "bar", "bat", "bean", "bed", "bee", "bell",
    "belt", "bike", "bird", "bit", "boat", "bone", "book", "boot", "bot",
    "bowl", "box", "bug", "bulb", "bun", "bus", "cab", "cake", "can", "cane",
    "cap", "car", "card", "cart", "case", "cat", "cell", "chip", "clip", "coat",
    "coin", "comb", "cone", "cord", "corn", "cot", "cow", "crab", "cub", "cube",
    "cup", "dad", "dart", "desk", "dice", "dish", "disk", "dock", "dog", "doll",
    "door", "drum", "duck", "ear", "egg", "elf", "eye", "fan", "fig", "fish",
    "flag", "fork", "fox", "frog", "gal", "gate", "gear", "gem", "gift", "glue",
    "goat", "gold", "gum", "guy", "ham", "hand", "hat", "hen", "hill", "hook",
    "horn", "hose", "hut", "ice", "ink", "iron", "jam", "jar", "jet", "jug",
    "kale", "key", "kid", "kite", "knee", "knob", "lab", "lake", "lamb", "lamp",
    "leaf", "leek", "leg", "lens", "lid", "lime", "line", "lion", "lip", "lock",
    "log", "mail", "map", "mask", "mat", "meat", "milk", "mint", "mom", "moon",
    "mop", "moth", "mug", "mule", "nail", "neck", "nest", "net", "note", "nut",
    "oak", "oar", "oil", "owl", "pad", "palm", "pan", "paw", "pea", "pear",
    "pen", "pet", "pie", "pig", "pill", "pin", "pine", "pipe", "plum", "pole",
    "pond", "pony", "pool", "pot", "pub", "pump", "pup", "raft", "rag", "rail",
    "rake", "ram", "ramp", "rat", "rice", "ring", "road", "robe", "rock", "rod",
    "rope", "rose", "rug", "sack", "sail", "salt", "sand", "saw", "sea", "seal",
    "seat", "shed", "ship", "shoe", "sign", "silk", "sink", "soap", "sock",
    "sofa", "son", "soup", "spy", "star", "step", "suit", "sun", "swan", "tag",
    "tank", "tap", "tape", "tea", "tent", "tie", "tile", "tin", "toad", "toe",
    "tool", "top", "toy", "tray", "tree", "tub", "tube", "tuna", "van", "vase",
    "vest", "vine", "wall", "wand", "wasp", "wave", "web", "well", "wig", "wine",
    "wing", "wire", "wolf", "wood", "wool", "worm", "yak", "yarn", "yolk", "zip",
]


def _dedup(seq):
    """Order-preserving de-duplication."""
    return list(dict.fromkeys(seq))


ADJECTIVES = _dedup(_ADJECTIVES)
NOUNS = _dedup(_NOUNS)
SPACE = len(ADJECTIVES) * len(NOUNS)  # distinct color-object pairs


def _coprime_near(target: int, n: int) -> int:
    """Smallest odd number >= target that is coprime to n (so `*c mod n` is a
    bijection on [0, n))."""
    c = max(1, target) | 1
    while _math.gcd(c, n) != 1:
        c += 2
    return c


# Multiplying the index by a constant coprime to SPACE, mod SPACE, is a bijection
# on [0, SPACE) — collision-free but scatters CONSECUTIVE indices across the
# whole space (so k=0,1,2,... don't share a noun many times in a row).
_SCRAMBLE = _coprime_near(int(SPACE * 0.6180339887), SPACE) if SPACE > 1 else 1


def random_name(rng: Optional[_random.Random] = None, sep: str = "-") -> str:
    """A random ``color<sep>object`` name. Check uniqueness at the call site
    (against currently-live mixes) and retry on the rare collision."""
    r = rng or _random
    return f"{r.choice(ADJECTIVES)}{sep}{r.choice(NOUNS)}"


def name_from_index(k: int, sep: str = "-") -> str:
    """Deterministic, collision-free mapping of a UNIQUE integer ``k`` to a name.

    For ``0 <= k < SPACE`` every value yields a distinct color-object pair. For
    ``k >= SPACE`` a numeric suffix keeps it unique (``...-2``, ``...-3``, ...).
    """
    if k < 0:
        raise ValueError("k must be non-negative")
    idx = (k % SPACE) * _SCRAMBLE % SPACE
    a = ADJECTIVES[idx % len(ADJECTIVES)]
    n = NOUNS[idx // len(ADJECTIVES)]
    name = f"{a}{sep}{n}"
    cycle = k // SPACE
    return name if cycle == 0 else f"{name}{sep}{cycle + 1}"
