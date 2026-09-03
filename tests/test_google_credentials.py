"""Per-user Google credentials, encrypted at rest.

The properties asserted here are the ones the feature exists for: the token
is not readable in the database file, a copy of the database alone is not
enough to use it, a row moved between accounts does not grant access, and
nothing fails open when the key is missing.

The fixture payload below is deliberately shaped like a serialised
credential but filled with obviously-synthetic markers rather than
realistic-looking token strings. A test fixture that looks like a real
credential is a test fixture that gets flagged by secret scanners forever
after, and one that someone eventually mistakes for a live leak.
"""

from __future__ import annotations

import base64
import json

import pytest

from ragra import crypto
from ragra.adapters import google_credentials as gc
from ragra.db import repo
from tests.support import make_user, owner_id

ACCESS_MARKER = "SYNTHETIC-ACCESS-VALUE-FOR-TESTS"
REFRESH_MARKER = "SYNTHETIC-REFRESH-VALUE-FOR-TESTS"

TOKEN_PAYLOAD = json.dumps(
    {
        "token": ACCESS_MARKER,
        "refresh_token": REFRESH_MARKER,
        "token_uri": "https://oauth2.googleapis.com/token",
        "client_id": "example-client.apps.googleusercontent.com",
        "client_secret": "SYNTHETIC-CLIENT-SECRET-FOR-TESTS",
        "scopes": [
            "https://www.googleapis.com/auth/classroom.courses.readonly",
            "https://www.googleapis.com/auth/classroom.coursework.me.readonly",
        ],
    }
)


@pytest.fixture
def key() -> bytes:
    return crypto.load_key({crypto.KEY_ENV_VAR: crypto.generate_key()})


@pytest.fixture
def alice(conn) -> int:
    return owner_id(conn)


@pytest.fixture
def bea(conn) -> int:
    return make_user(conn, google_sub="credentials-second")


# ---------------------------------------------------------------------------
# The key itself
# ---------------------------------------------------------------------------


def test_a_generated_key_is_usable_and_full_length():
    key = crypto.load_key({crypto.KEY_ENV_VAR: crypto.generate_key()})
    assert len(key) == 32


def test_two_generated_keys_differ():
    assert crypto.generate_key() != crypto.generate_key()


@pytest.mark.parametrize(
    "value",
    ["", "   ", "not-base64!!!", base64.urlsafe_b64encode(b"too short").decode()],
    ids=["empty", "blank", "not-base64", "wrong-length"],
)
def test_an_unusable_key_is_rejected_up_front(value):
    """A truncated or mistyped key must fail now, not later at decryption
    time on data that has already been written with it."""
    with pytest.raises(crypto.CredentialKeyMissing):
        crypto.load_key({crypto.KEY_ENV_VAR: value})


def test_is_configured_reports_without_raising():
    assert crypto.is_configured({crypto.KEY_ENV_VAR: crypto.generate_key()}) is True
    assert crypto.is_configured({}) is False


# ---------------------------------------------------------------------------
# Encryption
# ---------------------------------------------------------------------------


def test_the_ciphertext_does_not_contain_the_token(key, alice):
    blob = crypto.encrypt(TOKEN_PAYLOAD, user_id=alice, service=gc.CLASSROOM, key=key)
    assert REFRESH_MARKER.encode() not in blob
    assert b"refresh_token" not in blob


def test_a_round_trip_returns_exactly_what_went_in(key, alice):
    blob = crypto.encrypt(TOKEN_PAYLOAD, user_id=alice, service=gc.CLASSROOM, key=key)
    assert crypto.decrypt(blob, user_id=alice, service=gc.CLASSROOM, key=key) == TOKEN_PAYLOAD


def test_encrypting_twice_produces_different_ciphertext(key, alice):
    """A fresh nonce each time. Identical ciphertext for identical input
    would leak that a credential was unchanged between two backups."""
    first = crypto.encrypt(TOKEN_PAYLOAD, user_id=alice, service=gc.CLASSROOM, key=key)
    second = crypto.encrypt(TOKEN_PAYLOAD, user_id=alice, service=gc.CLASSROOM, key=key)
    assert first != second


def test_a_different_key_cannot_decrypt(alice):
    """The property that makes a stolen database file insufficient."""
    written_with = crypto.load_key({crypto.KEY_ENV_VAR: crypto.generate_key()})
    attacker = crypto.load_key({crypto.KEY_ENV_VAR: crypto.generate_key()})
    blob = crypto.encrypt(TOKEN_PAYLOAD, user_id=alice, service=gc.CLASSROOM, key=written_with)

    with pytest.raises(crypto.CredentialDecryptionError):
        crypto.decrypt(blob, user_id=alice, service=gc.CLASSROOM, key=attacker)


def test_tampering_is_detected_rather_than_silently_decrypting(key, alice):
    blob = bytearray(crypto.encrypt(TOKEN_PAYLOAD, user_id=alice, service=gc.CLASSROOM, key=key))
    blob[-1] ^= 0x01

    with pytest.raises(crypto.CredentialDecryptionError):
        crypto.decrypt(bytes(blob), user_id=alice, service=gc.CLASSROOM, key=key)


def test_a_credential_cannot_be_moved_to_another_user(key, alice, bea):
    """The attack this closes: someone who can write the database but not
    read the key copies a ciphertext into their own row, expecting to
    inherit the victim's Google access. The associated data binding makes
    the copy undecryptable."""
    blob = crypto.encrypt(TOKEN_PAYLOAD, user_id=alice, service=gc.CLASSROOM, key=key)

    with pytest.raises(crypto.CredentialDecryptionError):
        crypto.decrypt(blob, user_id=bea, service=gc.CLASSROOM, key=key)


def test_a_credential_cannot_be_moved_to_another_service(key, alice):
    """Same binding, other axis: a Classroom grant must not be usable as a
    Calendar one, which would silently widen what Ragra can do."""
    blob = crypto.encrypt(TOKEN_PAYLOAD, user_id=alice, service=gc.CLASSROOM, key=key)

    with pytest.raises(crypto.CredentialDecryptionError):
        crypto.decrypt(blob, user_id=alice, service=gc.CALENDAR, key=key)


def test_an_unrecognised_blob_is_rejected(key, alice):
    with pytest.raises(crypto.CredentialDecryptionError):
        crypto.decrypt(b"", user_id=alice, service=gc.CLASSROOM, key=key)
    with pytest.raises(crypto.CredentialDecryptionError):
        crypto.decrypt(b"\xff garbage", user_id=alice, service=gc.CLASSROOM, key=key)


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


def test_the_stored_row_holds_no_readable_token(conn, key, alice):
    """Checked against the actual stored bytes rather than the API, because
    the risk is what ends up in the file - a backup, a sync, a copy."""
    gc.store(conn, user_id=alice, service=gc.CLASSROOM, payload=TOKEN_PAYLOAD, key=key)

    row = repo.get_google_credentials(conn, user_id=alice, service=gc.CLASSROOM)
    assert REFRESH_MARKER.encode() not in bytes(row["ciphertext"])
    assert ACCESS_MARKER.encode() not in bytes(row["ciphertext"])


def test_the_whole_database_file_contains_no_token(conn, tmp_path, key, alice):
    """The stronger version: scan the file on disk, not just the column.
    An index, a journal page, or a stray copy would show up here."""
    gc.store(conn, user_id=alice, service=gc.CLASSROOM, payload=TOKEN_PAYLOAD, key=key)
    conn.commit()
    conn.execute("VACUUM")

    db_bytes = (tmp_path / "test.db").read_bytes()
    assert REFRESH_MARKER.encode() not in db_bytes
    assert ACCESS_MARKER.encode() not in db_bytes


def test_stored_credentials_round_trip(conn, key, alice):
    gc.store(conn, user_id=alice, service=gc.CLASSROOM, payload=TOKEN_PAYLOAD, key=key)
    assert gc.load(conn, user_id=alice, service=gc.CLASSROOM, key=key) == TOKEN_PAYLOAD


def test_storing_again_replaces_rather_than_duplicates(conn, key, alice):
    """Re-authorization has to overwrite: two rows for one (user, service)
    would make "which credential is current?" unanswerable."""
    gc.store(conn, user_id=alice, service=gc.CLASSROOM, payload=TOKEN_PAYLOAD, key=key)
    refreshed = json.dumps({"token": "SYNTHETIC-SECOND-VALUE", "scopes": ["a"]})
    gc.store(conn, user_id=alice, service=gc.CLASSROOM, payload=refreshed, key=key)

    count = conn.execute(
        "SELECT COUNT(*) AS c FROM google_credentials WHERE user_id = ?", (alice,)
    ).fetchone()["c"]
    assert count == 1
    assert gc.load(conn, user_id=alice, service=gc.CLASSROOM, key=key) == refreshed


def test_scopes_are_readable_without_the_key(conn, key, alice):
    """So a status command works on a machine that deliberately does not
    hold the key."""
    gc.store(conn, user_id=alice, service=gc.CLASSROOM, payload=TOKEN_PAYLOAD, key=key)

    scopes = repo.google_credential_scopes(conn, user_id=alice, service=gc.CLASSROOM)
    assert "classroom.courses.readonly" in scopes


def test_one_users_credentials_are_invisible_to_another(conn, key, alice, bea):
    gc.store(conn, user_id=alice, service=gc.CLASSROOM, payload=TOKEN_PAYLOAD, key=key)

    assert gc.has_credentials(conn, user_id=alice, service=gc.CLASSROOM) is True
    assert gc.has_credentials(conn, user_id=bea, service=gc.CLASSROOM) is False
    with pytest.raises(gc.CredentialsNotStored):
        gc.load(conn, user_id=bea, service=gc.CLASSROOM, key=key)


def test_forgetting_removes_only_the_named_service(conn, key, alice):
    gc.store(conn, user_id=alice, service=gc.CLASSROOM, payload=TOKEN_PAYLOAD, key=key)
    gc.store(conn, user_id=alice, service=gc.CALENDAR, payload=TOKEN_PAYLOAD, key=key)

    assert gc.forget(conn, user_id=alice, service=gc.CLASSROOM) == 1

    assert gc.has_credentials(conn, user_id=alice, service=gc.CLASSROOM) is False
    assert gc.has_credentials(conn, user_id=alice, service=gc.CALENDAR) is True


def test_forgetting_everything_disconnects_the_account(conn, key, alice, bea):
    gc.store(conn, user_id=alice, service=gc.CLASSROOM, payload=TOKEN_PAYLOAD, key=key)
    gc.store(conn, user_id=alice, service=gc.CALENDAR, payload=TOKEN_PAYLOAD, key=key)
    gc.store(conn, user_id=bea, service=gc.CLASSROOM, payload=TOKEN_PAYLOAD, key=key)

    assert gc.forget(conn, user_id=alice) == 2
    assert gc.has_credentials(conn, user_id=bea, service=gc.CLASSROOM) is True


def test_deleting_a_user_destroys_their_google_authorization(conn, key, alice, bea):
    """Otherwise a deleted account leaves a live grant behind - the worst
    kind of leftover, because nothing in the product shows it any more."""
    gc.store(conn, user_id=bea, service=gc.CLASSROOM, payload=TOKEN_PAYLOAD, key=key)
    gc.store(conn, user_id=alice, service=gc.CLASSROOM, payload=TOKEN_PAYLOAD, key=key)

    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("DELETE FROM users WHERE id = ?", (bea,))
    conn.commit()

    assert gc.has_credentials(conn, user_id=bea, service=gc.CLASSROOM) is False
    assert gc.has_credentials(conn, user_id=alice, service=gc.CLASSROOM) is True


def test_an_unknown_service_is_refused(conn, key, alice):
    with pytest.raises(ValueError):
        gc.store(conn, user_id=alice, service="gmail", payload=TOKEN_PAYLOAD, key=key)


def test_storing_without_a_key_fails_instead_of_writing_plaintext(conn, alice, monkeypatch):
    """The failure that matters most. A fallback to plaintext would keep
    everything working, so nobody would notice, and the tokens would be in
    the clear anyway."""
    monkeypatch.delenv(crypto.KEY_ENV_VAR, raising=False)

    with pytest.raises(crypto.CredentialKeyMissing):
        gc.store(conn, user_id=alice, service=gc.CLASSROOM, payload=TOKEN_PAYLOAD)

    assert repo.get_google_credentials(conn, user_id=alice, service=gc.CLASSROOM) is None


# ---------------------------------------------------------------------------
# Importing an existing on-disk token
# ---------------------------------------------------------------------------


def test_an_existing_token_file_can_be_adopted(conn, tmp_path, key, alice):
    """The migration path from file-based storage. Forcing a re-consent
    because the storage layout changed would be a self-inflicted wound."""
    token_file = tmp_path / "classroom-token.json"
    token_file.write_text(TOKEN_PAYLOAD, encoding="utf-8")

    assert gc.import_from_file(
        conn, user_id=alice, service=gc.CLASSROOM, path=token_file, key=key
    ) is True
    assert gc.load(conn, user_id=alice, service=gc.CLASSROOM, key=key) == TOKEN_PAYLOAD


def test_importing_leaves_the_source_file_in_place(conn, tmp_path, key, alice):
    """Destroying the only copy of a working authorization as a side effect
    of a storage change would be an unpleasant surprise."""
    token_file = tmp_path / "classroom-token.json"
    token_file.write_text(TOKEN_PAYLOAD, encoding="utf-8")

    gc.import_from_file(conn, user_id=alice, service=gc.CLASSROOM, path=token_file, key=key)

    assert token_file.exists()


@pytest.mark.parametrize("contents", [None, "", "   "], ids=["missing", "empty", "blank"])
def test_importing_nothing_is_a_safe_no_op(conn, tmp_path, key, alice, contents):
    """So setup can call this unconditionally."""
    token_file = tmp_path / "classroom-token.json"
    if contents is not None:
        token_file.write_text(contents, encoding="utf-8")

    assert gc.import_from_file(
        conn, user_id=alice, service=gc.CLASSROOM, path=token_file, key=key
    ) is False
    assert gc.has_credentials(conn, user_id=alice, service=gc.CLASSROOM) is False


# ---------------------------------------------------------------------------
# Status output
# ---------------------------------------------------------------------------


def test_status_never_prints_anything_secret(conn, key, alice, monkeypatch):
    """This output is written to be pasted into a support conversation."""
    encoded_key = base64.urlsafe_b64encode(key).decode()
    monkeypatch.setenv(crypto.KEY_ENV_VAR, encoded_key)
    gc.store(conn, user_id=alice, service=gc.CLASSROOM, payload=TOKEN_PAYLOAD, key=key)

    text = " ".join(f"{k}: {v}" for k, v in gc.status(conn, user_id=alice).items())

    assert REFRESH_MARKER not in text
    assert ACCESS_MARKER not in text
    assert "SYNTHETIC-CLIENT-SECRET-FOR-TESTS" not in text
    assert encoded_key not in text
    assert "classroom.courses.readonly" in text


def test_status_reports_an_unauthorized_service_plainly(conn, alice):
    assert gc.status(conn, user_id=alice)[gc.CALENDAR] == "not authorized"
