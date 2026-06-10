"""Friendly mix names — simple `adjective-object` phrases like `big-apple`.

Optimised for a human who reads a name in a Nostr note, goes off to gather their
inputs for a few minutes, then has to retype it from memory in a DM — often on a
phone keyboard. So the words are chosen to be **short and very common**:

- adjectives are simple everyday descriptors: the basic colors (red, blue,
  green…) plus plain words like big, little, soft, hard, wet, dry, open, smooth,
- nouns are everyday **objects** — fruit, cutlery, household things, gadgets
  (apple, spoon, lamp, phone…), favouring 3-5 letters or longer-but-common words.

The only rules are common, short, and inoffensive (not constrained to BIP-39 —
that pulled in rare words like "gosling").

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

# Everyday objects — fruit/veg, cutlery, household items, gadgets, clothes, treats.
# Short and common; longer words are kept only when very familiar.
_NOUNS = [
    "adapter", "apple", "apron", "bagel", "ball", "balloon", "banana", "basket",
    "battery", "bean", "bed", "beet", "bell", "belt", "bench", "berry", "bike",
    "biscuit", "blanket", "blender", "book", "boot", "bottle", "bowl", "box",
    "bread", "brick", "broom", "brush", "bucket", "butter", "button", "cabbage",
    "cabinet", "cable", "cake", "camera", "candle", "candy", "cap", "carpet",
    "carrot", "cashew", "celery", "cereal", "chair", "charger", "cheese",
    "cherry", "chip", "clip", "clock", "coat", "coconut", "coin", "collar",
    "comb", "console", "cookie", "cord", "cork", "corn", "couch", "cracker",
    "crayon", "cup", "cupcake", "curtain", "desk", "dice", "dish", "disk",
    "domino", "donut", "door", "drawer", "dress", "drill", "drive", "drum",
    "egg", "fan", "faucet", "fence", "fig", "flag", "fork", "frame", "fridge",
    "garlic", "gate", "ginger", "glove", "glue", "grape", "grater", "hammer",
    "hanger", "hat", "headset", "honey", "hoodie", "hook", "hose", "jacket",
    "jam", "jar", "jeans", "jelly", "kale", "kettle", "key", "keyboard", "kite",
    "knife", "ladder", "ladle", "lamp", "lantern", "laptop", "leek", "lemon",
    "mango", "marble", "mat", "match", "melon", "mirror", "mitten", "mixer",
    "modem", "monitor", "mop", "mouse", "muffin", "mug", "nail", "napkin",
    "noodle", "onion", "oven", "pan", "pancake", "pants", "pea", "peach",
    "peanut", "pear", "pen", "pepper", "phone", "pillow", "pipe", "pitcher",
    "pizza", "plate", "plug", "plum", "pocket", "popcorn", "pot", "potato",
    "pretzel", "printer", "pudding", "pumpkin", "puzzle", "radio", "radish",
    "raisin", "remote", "rice", "ring", "robe", "robot", "router", "rug",
    "ruler", "sandal", "satoshi", "saucer", "saw", "scarf", "scooter", "screen",
    "sensor", "shelf", "shirt", "shoe", "shorts", "sink", "skirt", "slipper",
    "soap", "sock", "socket", "sofa", "soup", "spatula", "speaker", "sponge",
    "spoon", "squash", "stamp", "stool", "stove", "straw", "sugar", "sweater",
    "switch", "syrup", "table", "tablet", "tape", "teapot", "thermos", "tie",
    "tile", "toast", "toaster", "tomato", "tongs", "top", "torch", "towel",
    "tray", "tub", "turnip", "vacuum", "vase", "vest", "wagon", "walnut",
    "watch", "webcam", "whisk", "window", "wrench", "zipper",
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
