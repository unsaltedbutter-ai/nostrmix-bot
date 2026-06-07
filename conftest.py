"""Project-root pytest conftest.

The libsecp256k1 >= 0.6 symbol-rename shim lives in
``src/secp256k1_compat.py`` (the bot's runtime now does EC verification of
returned signatures, so it's a runtime concern, not just a test one). Importing
it here applies the patch before any test loads bitcointx's EC library. The shim
is idempotent, so importing it both here and from ``src/`` is safe.
"""

import src.secp256k1_compat  # noqa: F401  — applies the libsecp256k1 shim
