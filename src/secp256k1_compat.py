"""Runtime shim: bridge python-bitcointx 1.1.x against libsecp256k1 >= 0.6.

libsecp256k1 0.6 removed two deprecated symbols that bitcointx still tries to
bind at library-load time:

    secp256k1_ec_privkey_tweak_add  ->  secp256k1_ec_seckey_tweak_add
    secp256k1_ec_privkey_negate     ->  secp256k1_ec_seckey_negate

Without this, the FIRST bitcointx EC operation fails to load the library at all
(``_add_function_definitions`` raises on the missing dlsym).

The bot's runtime now performs EC verification: ``PSBTManager.validate_returned``
cryptographically checks each participant's signature when they return a signed
PSBT (so a bogus/trolling signature is rejected at submission instead of only
when the network rejects the broadcast). That makes this a RUNTIME dependency,
which is why the shim lives in ``src/`` and is imported by ``psbt_manager`` and
by ``conftest.py``.

Importing this module applies the patch exactly once (idempotent), and is a
no-op on a libsecp256k1 that still has the old names. Delete this module once
python-bitcointx ships a release that binds the renamed symbols directly.
"""

import bitcointx.core.secp256k1 as _btx_secp

_DEPRECATED_TO_NEW = [
    ("secp256k1_ec_privkey_tweak_add", "secp256k1_ec_seckey_tweak_add"),
    ("secp256k1_ec_privkey_negate", "secp256k1_ec_seckey_negate"),
]

if not getattr(_btx_secp, "_nostrmix_secp_patched", False):
    _orig_add_function_definitions = _btx_secp._add_function_definitions

    def _patched_add_function_definitions(lib):
        """Pre-alias removed names onto the library handle, then run bitcointx's
        own binding code unchanged."""
        for old, new in _DEPRECATED_TO_NEW:
            try:
                getattr(lib, old)
                continue  # deprecated name still resolves -> nothing to do
            except AttributeError:
                pass
            try:
                setattr(lib, old, getattr(lib, new))
            except AttributeError:
                # Neither name exists — let the original binder raise so the
                # failure stays diagnostic.
                pass
        return _orig_add_function_definitions(lib)

    _btx_secp._add_function_definitions = _patched_add_function_definitions
    _btx_secp._nostrmix_secp_patched = True
