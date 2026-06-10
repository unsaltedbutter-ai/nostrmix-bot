"""Friendly mix names — simple `color-object` phrases like `silver-cupcake`.

Optimised for a human who reads a name in a Nostr note, goes off to gather their
inputs for a few minutes, then has to retype it from memory in a DM — often on a
phone keyboard. So the words are chosen to be **short and very common**:

- adjectives are everyday **colors** (visual + easy to spell: red, blue, gold…),
- nouns are everyday **objects** — fruit, cutlery, household things, gadgets
  (apple, spoon, lamp, phone…), favouring 3-5 letters or longer-but-common words.

Not constrained to the BIP-39 list (that pulled in rare words like "gosling"); the
only rules are common, short, and inoffensive.

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

# Everyday colors — short, common, visual, easy to spell (no turquoise/chartreuse).
_ADJECTIVES = [
    "amber", "aqua", "beige", "black", "blue", "bronze", "brown", "coral",
    "copper", "cream", "crimson", "cyan", "gold", "gray", "green", "indigo",
    "ivory", "jade", "lime", "maroon", "mint", "navy", "olive", "orange",
    "pink", "purple", "red", "rose", "ruby", "rust", "scarlet", "silver",
    "sky", "tan", "teal", "violet", "white", "yellow",
]

# Everyday objects — fruit, cutlery, household items, gadgets, clothes, treats.
# Short and common; a few 6-8 letter words are kept only when very familiar.
_NOUNS = [
    "apple", "bagel", "ball", "banana", "basket", "battery", "bean", "bed",
    "bell", "belt", "bench", "berry", "blanket", "book", "boot", "bottle",
    "bowl", "box", "bread", "broom", "brush", "bucket", "button", "cabbage",
    "cable", "cake", "camera", "candle", "candy", "cap", "carrot", "chair",
    "charger", "cherry", "chip", "clip", "clock", "coat", "coconut", "coin",
    "comb", "cookie", "cord", "cork", "corn", "couch", "crayon", "cup",
    "cupcake", "desk", "dish", "donut", "door", "drum", "egg", "fan", "fig",
    "flag", "fork", "garlic", "glove", "grape", "hat", "honey", "jar", "key",
    "keyboard", "kettle", "kite", "knife", "lamp", "laptop", "lemon", "mango",
    "mat", "melon", "mirror", "mitten", "monitor", "mouse", "muffin", "mug",
    "onion", "pan", "pancake", "peach", "pear", "pen", "pepper", "phone",
    "pillow", "pizza", "plate", "plug", "plum", "pot", "potato", "printer",
    "pumpkin", "radio", "remote", "rice", "ring", "robe", "router", "rug",
    "ruler", "satoshi", "scarf", "screen", "shelf", "shirt", "shoe", "sink",
    "soap", "sock", "sofa", "soup", "speaker", "spoon", "sponge", "stamp",
    "stool", "straw", "sugar", "table", "taco", "tablet", "tie", "toast",
    "tomato", "towel", "tray", "tub", "vase", "vest", "watch", "window",
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
