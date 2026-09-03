"""Encryption for credentials stored at rest.

What this protects against, stated plainly, because encryption that nobody
can describe the threat model for tends to be encryption that does not
help: the database file is a single portable artifact. It gets backed up,
synced, copied to a new machine, and attached to a bug report. A Google
OAuth refresh token sitting in it in the clear is a standing grant to read
somebody's coursework and write to their calendar, valid until revoked, for
anyone who ever touches a copy. Encrypting it moves that grant behind a key
that lives in the environment rather than in the file, so a copy of the
database alone is not enough.

What it does not protect against: an attacker who already has both the
database and the process environment. Nothing at this layer can, and
pretending otherwise would be worse than being clear about it.

The construction is AES-256-GCM with a random 96-bit nonce per encryption.
Authenticated, so tampering is detected rather than silently decrypting to
garbage. Associated data binds each ciphertext to the user and service it
belongs to - so a row copied from one user to another fails to decrypt
rather than handing the second user the first one's Google access. That is
a real attack against a table an attacker can write but not read, and it
costs one extra argument to close.

A leading version byte makes the scheme identifiable, so a future
algorithm or key rotation can be introduced without a schema change and
without guessing how an existing blob was produced.
"""

from __future__ import annotations

import base64
import os
import secrets

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

KEY_ENV_VAR = "RAGRA_CREDENTIAL_KEY"

# Version 1: AES-256-GCM, 12-byte nonce, ciphertext with appended tag.
_VERSION = 1
_NONCE_BYTES = 12
_KEY_BYTES = 32


class CredentialKeyMissing(RuntimeError):
    """No usable encryption key is configured.

    Raised rather than falling back to storing plaintext. A silent
    downgrade is the worst outcome available here: everything keeps
    working, so nobody notices, and the tokens are in the clear anyway.
    """


class CredentialDecryptionError(RuntimeError):
    """A stored credential could not be decrypted.

    Deliberately does not distinguish a wrong key from a tampered blob from
    a mismatched owner. All three mean the same thing to a caller - do not
    use this - and the difference is only useful to someone probing.
    """


def generate_key() -> str:
    """A fresh key, in the form the environment variable expects.

    Exposed so the setup path can mint one rather than inviting somebody to
    invent their own - a hand-typed passphrase in this variable would look
    identical to a real key and be enormously weaker.
    """
    return base64.urlsafe_b64encode(secrets.token_bytes(_KEY_BYTES)).decode("ascii")


def load_key(env: dict[str, str] | None = None) -> bytes:
    """Read and validate the configured key.

    The length check is not pedantry: a truncated or mistyped value would
    otherwise fail much later, at decryption time, on data that has already
    been written with it.
    """
    source = os.environ if env is None else env
    raw = (source.get(KEY_ENV_VAR) or "").strip()
    if not raw:
        raise CredentialKeyMissing(
            f"{KEY_ENV_VAR} is not set; credentials cannot be stored or read. "
            "Generate one with: ragra generate-credential-key"
        )
    try:
        key = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    except (ValueError, TypeError) as exc:
        raise CredentialKeyMissing(f"{KEY_ENV_VAR} is not valid base64") from exc
    if len(key) != _KEY_BYTES:
        raise CredentialKeyMissing(
            f"{KEY_ENV_VAR} must decode to {_KEY_BYTES} bytes, got {len(key)}"
        )
    return key


def is_configured(env: dict[str, str] | None = None) -> bool:
    """Whether encryption is usable, without raising. For status output and
    for deciding whether a feature is available at all."""
    try:
        load_key(env)
    except CredentialKeyMissing:
        return False
    return True


def _associated_data(*, user_id: int, service: str) -> bytes:
    """The binding between a ciphertext and where it is allowed to live."""
    return f"ragra:v{_VERSION}:user={user_id}:service={service}".encode("utf-8")


def encrypt(plaintext: str, *, user_id: int, service: str, key: bytes | None = None) -> bytes:
    """Encrypt a credential for one user and one service."""
    aes = AESGCM(key if key is not None else load_key())
    nonce = secrets.token_bytes(_NONCE_BYTES)
    sealed = aes.encrypt(
        nonce,
        plaintext.encode("utf-8"),
        _associated_data(user_id=user_id, service=service),
    )
    return bytes([_VERSION]) + nonce + sealed


def decrypt(blob: bytes, *, user_id: int, service: str, key: bytes | None = None) -> str:
    """Decrypt a credential, verifying it belongs where it was found."""
    if not blob or blob[0] != _VERSION:
        raise CredentialDecryptionError("stored credential is not in a recognised format")

    nonce = blob[1 : 1 + _NONCE_BYTES]
    sealed = blob[1 + _NONCE_BYTES :]
    aes = AESGCM(key if key is not None else load_key())
    try:
        plaintext = aes.decrypt(
            nonce, sealed, _associated_data(user_id=user_id, service=service)
        )
    except InvalidTag as exc:
        raise CredentialDecryptionError("stored credential could not be decrypted") from exc
    return plaintext.decode("utf-8")
