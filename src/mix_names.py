"""Friendly mix names — simple `adjective-noun` phrases like `warm-cupcake`.

Two ways to get a name:

- ``random_name()`` picks an adjective and a noun at random. Readable, but a
  finite space means repeats follow the birthday paradox (see scripts/mix_names.py).
- ``name_from_index(k)`` maps a UNIQUE integer ``k`` (e.g. a row counter, or a
  date+time+rowid value) to a name deterministically. Distinct ``k`` always give
  distinct names — for the first ``SPACE`` values via a unique adjective-noun
  pair, and beyond that with a numeric suffix (``warm-cupcake-2``). This is the
  way to GUARANTEE uniqueness.

Names are joined with ``sep`` (default ``-``) so they stay a single token — the
DM protocol's ``/join <mix_name>`` reads one whitespace-delimited word, so a
space ("warm cupcake") would only parse as "warm". Use the hyphenated form as
the id; show a space form only for display if you like.
"""
from __future__ import annotations

import math as _math
import random as _random
from typing import Optional

# Curated, friendly, mostly-concrete words. Kept lowercase and single-token.
# Duplicates (and any adjective that also appears as a noun) are harmless — the
# two lists are sampled independently — but we de-dup each list below so the
# combinatorial space count is honest.
_ADJECTIVES = [
    "amber", "arctic", "ashen", "autumn", "azure", "blue", "bold", "brave",
    "breezy", "bright", "bronze", "bubbly", "calm", "cheery", "clever", "cloudy",
    "cobalt", "copper", "cosmic", "cozy", "crimson", "crisp", "curious", "dapper",
    "dewy", "dreamy", "dusky", "dusty", "eager", "earnest", "electric", "emerald",
    "fancy", "fearless", "feisty", "fiery", "fizzy", "fluffy", "foamy", "frosty",
    "gentle", "giddy", "glossy", "golden", "graceful", "grassy", "hearty", "hidden",
    "humble", "icy", "indigo", "ivory", "jade", "jolly", "jovial", "keen",
    "kindly", "lazy", "leafy", "lively", "lone", "lucky", "lunar", "mellow",
    "merry", "minty", "misty", "modest", "mossy", "nimble", "noble", "ochre",
    "olive", "opal", "peachy", "pearly", "peppy", "placid", "playful", "plucky",
    "plump", "polar", "quaint", "quick", "quiet", "rapid", "rosy", "ruby",
    "rugged", "rustic", "salty", "sandy", "scarlet", "shady", "sharp", "shiny",
    "silent", "silken", "silly", "silver", "sleepy", "slender", "smoky", "snowy",
    "snug", "solar", "spry", "starry", "steady", "stellar", "sturdy", "sunny",
    "sunlit", "swift", "tame", "tawny", "teal", "tender", "timid", "tiny",
    "toasty", "tranquil", "velvet", "vivid", "warm", "wily", "windy", "wise",
    "witty", "woolly", "zany", "zesty", "zippy", "amiable", "balmy", "chipper",
    "dainty", "frisky", "genial", "hushed", "jaunty", "mirthful", "nifty", "spirited",
]

_NOUNS = [
    "acorn", "almond", "anchor", "antler", "apple", "apricot", "aspen", "badger",
    "bagel", "bamboo", "basil", "beacon", "bean", "bear", "beaver", "beetle",
    "berry", "biscuit", "bison", "blossom", "bluebird", "boulder", "bramble", "breeze",
    "brook", "buffalo", "bunny", "burrow", "cabin", "cactus", "candle", "canyon",
    "cardinal", "cedar", "cherry", "chestnut", "chipmunk", "clover", "comet", "cookie",
    "cottage", "cove", "crane", "cricket", "crocus", "cupcake", "daisy", "dandelion",
    "delta", "dewdrop", "donut", "dove", "dragon", "dune", "eagle", "ember",
    "fawn", "fern", "ferry", "finch", "fjord", "flamingo", "flint", "fox",
    "gander", "garden", "geode", "ginger", "glade", "goose", "gopher", "gosling",
    "grotto", "harbor", "hare", "heron", "hickory", "hollow", "honey", "hornet",
    "iris", "ivy", "jasmine", "jay", "kettle", "kingfisher", "koala", "lagoon",
    "lantern", "lark", "lemon", "lichen", "lilac", "lily", "lobster", "loon",
    "lotus", "lynx", "magpie", "mango", "maple", "marmot", "meadow", "melon",
    "meteor", "mitten", "moose", "moss", "muffin", "mushroom", "narwhal", "nest",
    "oak", "ocelot", "orchard", "otter", "owl", "panda", "pansy", "peach",
    "pebble", "pelican", "penguin", "petal", "pheasant", "pigeon", "pinecone", "piper",
    "plum", "pond", "poppy", "prairie", "puffin", "pumpkin", "quail", "rabbit",
    "raccoon", "radish", "raven", "reef", "ridge", "river", "robin", "salmon",
    "sapling", "seal", "sparrow", "spruce", "squirrel", "starling", "stream", "sunflower",
    "swallow", "sycamore", "tadpole", "teapot", "thicket", "thistle", "toad", "truffle",
    "tulip", "turtle", "vine", "violet", "walnut", "walrus", "waterfall", "willow",
    "wombat", "wren", "yarrow",
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
# across the whole space (otherwise k=0,1,2,... would share a noun 144x in a row,
# leaking creation order and looking repetitive). ~golden-ratio start spreads well.
_SCRAMBLE = _coprime_near(int(SPACE * 0.6180339887), SPACE) if SPACE > 1 else 1


def random_name(rng: Optional[_random.Random] = None, sep: str = "-") -> str:
    """A random ``adjective<sep>noun`` name. Repeats per the birthday paradox."""
    r = rng or _random
    return f"{r.choice(ADJECTIVES)}{sep}{r.choice(NOUNS)}"


def name_from_index(k: int, sep: str = "-") -> str:
    """Deterministic, collision-free mapping of a UNIQUE integer ``k`` to a name.

    For ``0 <= k < SPACE`` every value yields a distinct adjective-noun pair. For
    ``k >= SPACE`` a numeric suffix keeps it unique (``...-2``, ``...-3``, ...).
    Feed it any source of unique integers (a counter, rowid, time-based id) to
    guarantee unique mix names.
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
