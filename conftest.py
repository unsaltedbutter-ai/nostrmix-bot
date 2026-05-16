"""Project-root pytest conftest.

Single purpose: bridge python-bitcointx 1.1.5 against libsecp256k1 >= 0.6,
which removed two deprecated symbols bitcointx still tries to bind at
library-load time:

    secp256k1_ec_privkey_tweak_add  ->  secp256k1_ec_seckey_tweak_add
    secp256k1_ec_privkey_negate     ->  secp256k1_ec_seckey_negate

Without this hook, any test that constructs a CKey / CPubKey raises
``ImportError: secp256k1 library not found`` even when the library is
installed, because bitcointx's ``_add_function_definitions`` fails on the
first missing dlsym.

The bot's *runtime* doesn't need bitcointx's signing path (participants
sign their own inputs in their wallets), so this patch lives in conftest.py
rather than src/ - it's strictly a test-fixture concern.

Once python-bitcointx ships a release that uses the renamed symbols,
this file can be deleted.
"""

import ctypes
import bitcointx.core.secp256k1 as _btx_secp

_DEPRECATED_TO_NEW = [
    ("secp256k1_ec_privkey_tweak_add", "secp256k1_ec_seckey_tweak_add"),
    ("secp256k1_ec_privkey_negate", "secp256k1_ec_seckey_negate"),
]


_orig_add_function_definitions = _btx_secp._add_function_definitions


def _patched_add_function_definitions(lib):
    """Pre-alias removed names onto the library handle, then let bitcointx's
    own binding code run as if nothing happened. Touching only this hook
    avoids re-implementing the path-discovery / context-creation logic of
    the full loader."""
    for old, new in _DEPRECATED_TO_NEW:
        try:
            getattr(lib, old)
            continue  # deprecated name still resolves -> nothing to do
        except AttributeError:
            pass
        try:
            setattr(lib, old, getattr(lib, new))
        except AttributeError:
            # Neither name exists - let the original binder raise so the
            # failure mode stays diagnostic.
            pass
    return _orig_add_function_definitions(lib)


_btx_secp._add_function_definitions = _patched_add_function_definitions
