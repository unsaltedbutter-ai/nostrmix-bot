#!/usr/bin/env python3
"""Exercise the friendly mix-name generator (src/mix_names.py).

Generates a batch of names and reports how readable and how unique they are, so
you can decide whether `adjective-noun` names (e.g. warm-cupcake, blue-bear) are
a good replacement for today's random hex mix ids.

Two modes:
  --mode random  pick adjective + noun at random; repeats follow the birthday
                 paradox. The report shows observed vs expected collisions.
  --mode index   map sequential UNIQUE integers k=0,1,2,... to names; distinct
                 k always give distinct names (a numeric suffix kicks in past the
                 word-space size). This is the "always unique" option.

Usage:
    python scripts/mix_names.py --count 1000
    python scripts/mix_names.py --count 50 --mode index
    python scripts/mix_names.py --count 200 --seed 7 --sample 20
"""
from __future__ import annotations

import argparse
import math
import os
import random
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src import mix_names as mn


def _expected_distinct(space: int, n: int) -> float:
    """Expected number of distinct values when drawing n times from `space`."""
    # space * (1 - (1 - 1/space)^n), computed stably.
    return space * (1.0 - math.exp(n * math.log1p(-1.0 / space)))


def _p_any_collision(space: int, n: int) -> float:
    """Approx probability of at least one collision in n draws (birthday)."""
    if n < 2:
        return 0.0
    return 1.0 - math.exp(-n * (n - 1) / (2.0 * space))


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="mix_names.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Generate friendly adjective-noun mix names and report how often they\n"
            "repeat, to gauge them as a replacement for random-hex mix ids.\n\n"
            "  --mode random : random picks; repeats per the birthday paradox.\n"
            "  --mode index  : sequential unique integers -> always-unique names\n"
            "                  (numeric suffix past the word-space size)."
        ),
        epilog=(
            "examples:\n"
            "  python scripts/mix_names.py --count 1000\n"
            "  python scripts/mix_names.py --count 50 --mode index\n"
            "  python scripts/mix_names.py --count 200 --seed 7 --sample 20"
        ),
    )
    ap.add_argument("--count", type=int, default=20,
                    help="how many names to generate (default 20)")
    ap.add_argument("--mode", choices=("random", "index"), default="random",
                    help="random picks vs sequential unique integers (default random)")
    ap.add_argument("--seed", type=int, default=None,
                    help="seed the RNG for reproducible random output")
    ap.add_argument("--start", type=int, default=0,
                    help="(index mode) first integer k to encode (default 0)")
    ap.add_argument("--sep", default="-", help="separator between words (default '-')")
    ap.add_argument("--sample", type=int, default=15,
                    help="how many example names to print (default 15)")
    args = ap.parse_args()

    if args.count < 1:
        print("--count must be >= 1")
        return 2

    space = mn.SPACE
    words = mn.ADJECTIVES + mn.NOUNS
    avg_len = sum(len(w) for w in words) / len(words)
    pct_short = 100 * sum(len(w) <= 5 for w in words) // len(words)
    print("== word space ==")
    print(f"  adjectives: {len(mn.ADJECTIVES)}   nouns: {len(mn.NOUNS)}")
    print(f"  distinct adjective-noun names: {space:,}")
    print(f"  (with one numeric suffix 2..10 that's ~{space*9:,} more)")
    print(f"  word length: avg {avg_len:.1f}, {pct_short}% are <=5 letters "
          f"(short = easier to retype on a phone)")

    names = []
    if args.mode == "random":
        rng = random.Random(args.seed)
        names = [mn.random_name(rng, sep=args.sep) for _ in range(args.count)]
    else:
        names = [mn.name_from_index(args.start + i, sep=args.sep)
                 for i in range(args.count)]

    distinct = len(set(names))
    collisions = len(names) - distinct

    print(f"\n== generated {args.count:,} name(s) [{args.mode} mode] ==")
    print(f"  distinct:   {distinct:,}")
    print(f"  repeats:    {collisions:,}  ({100*collisions/len(names):.2f}% of draws)")

    if args.mode == "random":
        exp_distinct = _expected_distinct(space, args.count)
        print(f"  expected distinct (birthday math): {exp_distinct:,.1f}"
              f"  -> ~{args.count - exp_distinct:,.1f} repeats expected")
        print(f"  P(at least one repeat) at this count: {100*_p_any_collision(space, args.count):.2f}%")
        # A handy reference: the count at which a collision becomes ~50% likely.
        half = int(1.1774 * math.sqrt(space))
        print(f"  rule of thumb: a repeat becomes ~50% likely around {half:,} names.")
    else:
        suffixed = sum(1 for n in names if n.rsplit(args.sep, 1)[-1].isdigit())
        print(f"  guaranteed unique by construction; {suffixed:,} used a numeric suffix"
              f" (k >= {space:,}).")

    # All names are adjective-noun by construction — none are "nonsense" in the
    # grammatical sense. Sanity-check that every token came from the lists.
    adj_set, noun_set = set(mn.ADJECTIVES), set(mn.NOUNS)
    bad = [n for n in names
           if n.split(args.sep)[0] not in adj_set or n.split(args.sep)[1] not in noun_set]
    print(f"  structurally valid adjective-noun: {len(names)-len(bad):,}/{len(names):,}"
          + ("" if not bad else f"   *** {len(bad)} malformed ***"))

    print(f"\n== sample ({min(args.sample, len(names))}) ==")
    for n in names[:args.sample]:
        print(f"  {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
