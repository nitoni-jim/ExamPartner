"""
tests/test_licence_lease.py — the signed lease (contract §6, brief F5, F6).

The four vectors below are copied verbatim from docs/LICENCE_API_CONTRACT.md
§6.4, where the Windows client carries the same four as its own unit tests.
They are the cheapest place to catch a Python/.NET disagreement about the
lease format, so they are tested in BOTH directions here: verify_lease()
accepts or rejects each one, and sign_lease() reproduces the valid lease
byte for byte from the documented payload and the TEST seed.

TEST KEY ONLY. It is derived from a published string; anyone can recompute
it. A test below proves that configuring it as LICENCE_SIGNING_KEY makes
licensing unavailable.
"""
import base64
import json

import pytest

TEST_SEED_B64 = "vaqSxX9ClSwoMECP/J92vJWuEQYqyktbMlFU8YPu+Lg="
TEST_PUBLIC_B64 = "fnYIDrqbBrz13D83z4RbYt9zzmDtbuoZ/4rCCROaEXQ="

PAYLOAD_JSON = (
    '{"account_id":"0d9c8b7a6f5e4d3c2b1a09f8e7d6c5b4","binding_id":"6f1c2e0a9b8d4'
    'c7e5a3f2b1d0c9e8f7a","expires_at":"2026-11-07T09:00:00Z","installation_id":"'
    'a1b2c3d4e5f60718293a4b5c6d7e8f90","issued_at":"2026-10-08T09:00:00Z","kid":0'
    ',"lease_serial":3,"machine_fingerprint_hash":"fd2ec5b521ad183152a5a69ee0acda'
    '6c271abbc60a97da48942225da41214566","seat_pool_id":"5e4d3c2b1a0f9e8d7c6b5a49'
    '38271605","subscription_expires_at":"2027-09-30T23:59:59Z","typ":"ep.licence'
    '.lease","v":1}'
)

VALID = (
    'eyJhY2NvdW50X2lkIjoiMGQ5YzhiN2E2ZjVlNGQzYzJiMWEwOWY4ZTdkNmM1YjQiLCJiaW5kaW5n'
    'X2lkIjoiNmYxYzJlMGE5YjhkNGM3ZTVhM2YyYjFkMGM5ZThmN2EiLCJleHBpcmVzX2F0IjoiMjAy'
    'Ni0xMS0wN1QwOTowMDowMFoiLCJpbnN0YWxsYXRpb25faWQiOiJhMWIyYzNkNGU1ZjYwNzE4Mjkz'
    'YTRiNWM2ZDdlOGY5MCIsImlzc3VlZF9hdCI6IjIwMjYtMTAtMDhUMDk6MDA6MDBaIiwia2lkIjow'
    'LCJsZWFzZV9zZXJpYWwiOjMsIm1hY2hpbmVfZmluZ2VycHJpbnRfaGFzaCI6ImZkMmVjNWI1MjFh'
    'ZDE4MzE1MmE1YTY5ZWUwYWNkYTZjMjcxYWJiYzYwYTk3ZGE0ODk0MjIyNWRhNDEyMTQ1NjYiLCJz'
    'ZWF0X3Bvb2xfaWQiOiI1ZTRkM2MyYjFhMGY5ZThkN2M2YjVhNDkzODI3MTYwNSIsInN1YnNjcmlw'
    'dGlvbl9leHBpcmVzX2F0IjoiMjAyNy0wOS0zMFQyMzo1OTo1OVoiLCJ0eXAiOiJlcC5saWNlbmNl'
    'LmxlYXNlIiwidiI6MX0.ktj3b5Qti8UWLLy1aVDhV9uxgZLY_L1PY4LrAeo1NWe7fTcASSpDfggq'
    'RQy59pgqFsORu3zCi6YmyZBOiA7RDw'
)

TAMPERED = (
    'eyJhY2NvdW50X2lkIjoiMGQ5YzhiN2E2ZjVlNGQzYzJiMWEwOWY4ZTdkNmM1YjQiLCJiaW5kaW5n'
    'X2lkIjoiNmYxYzJlMGE5YjhkNGM3ZTVhM2YyYjFkMGM5ZThmN2EiLCJleHBpcmVzX2F0IjoiMjAy'
    'Ni0xMS0wN1QwOTowMDowMFoiLCJpbnN0YWxsYXRpb25faWQiOiJhMWIyYzNkNGU1ZjYwNzE4Mjkz'
    'YTRiNWM2ZDdlOGY5MCIsImlzc3VlZF9hdCI6IjIwMjYtMTAtMDhUMDk6MDA6MDBaIiwia2lkIjow'
    'LCJsZWFzZV9zZXJpYWwiOjQsIm1hY2hpbmVfZmluZ2VycHJpbnRfaGFzaCI6ImZkMmVjNWI1MjFh'
    'ZDE4MzE1MmE1YTY5ZWUwYWNkYTZjMjcxYWJiYzYwYTk3ZGE0ODk0MjIyNWRhNDEyMTQ1NjYiLCJz'
    'ZWF0X3Bvb2xfaWQiOiI1ZTRkM2MyYjFhMGY5ZThkN2M2YjVhNDkzODI3MTYwNSIsInN1YnNjcmlw'
    'dGlvbl9leHBpcmVzX2F0IjoiMjAyNy0wOS0zMFQyMzo1OTo1OVoiLCJ0eXAiOiJlcC5saWNlbmNl'
    'LmxlYXNlIiwidiI6MX0.ktj3b5Qti8UWLLy1aVDhV9uxgZLY_L1PY4LrAeo1NWe7fTcASSpDfggq'
    'RQy59pgqFsORu3zCi6YmyZBOiA7RDw'
)

WRONG_KEY = (
    'eyJhY2NvdW50X2lkIjoiMGQ5YzhiN2E2ZjVlNGQzYzJiMWEwOWY4ZTdkNmM1YjQiLCJiaW5kaW5n'
    'X2lkIjoiNmYxYzJlMGE5YjhkNGM3ZTVhM2YyYjFkMGM5ZThmN2EiLCJleHBpcmVzX2F0IjoiMjAy'
    'Ni0xMS0wN1QwOTowMDowMFoiLCJpbnN0YWxsYXRpb25faWQiOiJhMWIyYzNkNGU1ZjYwNzE4Mjkz'
    'YTRiNWM2ZDdlOGY5MCIsImlzc3VlZF9hdCI6IjIwMjYtMTAtMDhUMDk6MDA6MDBaIiwia2lkIjow'
    'LCJsZWFzZV9zZXJpYWwiOjMsIm1hY2hpbmVfZmluZ2VycHJpbnRfaGFzaCI6ImZkMmVjNWI1MjFh'
    'ZDE4MzE1MmE1YTY5ZWUwYWNkYTZjMjcxYWJiYzYwYTk3ZGE0ODk0MjIyNWRhNDEyMTQ1NjYiLCJz'
    'ZWF0X3Bvb2xfaWQiOiI1ZTRkM2MyYjFhMGY5ZThkN2M2YjVhNDkzODI3MTYwNSIsInN1YnNjcmlw'
    'dGlvbl9leHBpcmVzX2F0IjoiMjAyNy0wOS0zMFQyMzo1OTo1OVoiLCJ0eXAiOiJlcC5saWNlbmNl'
    'LmxlYXNlIiwidiI6MX0.7BQrUeYISJVVA_rZq1mkCA4E3HYq_w-Zp6Rxve6HFCfHtV8BmMdwnD92'
    '5NS8qH_IAqyw1lYJT7DmSZJU141FDg'
)

PADDED = (
    'eyJhY2NvdW50X2lkIjoiMGQ5YzhiN2E2ZjVlNGQzYzJiMWEwOWY4ZTdkNmM1YjQiLCJiaW5kaW5n'
    'X2lkIjoiNmYxYzJlMGE5YjhkNGM3ZTVhM2YyYjFkMGM5ZThmN2EiLCJleHBpcmVzX2F0IjoiMjAy'
    'Ni0xMS0wN1QwOTowMDowMFoiLCJpbnN0YWxsYXRpb25faWQiOiJhMWIyYzNkNGU1ZjYwNzE4Mjkz'
    'YTRiNWM2ZDdlOGY5MCIsImlzc3VlZF9hdCI6IjIwMjYtMTAtMDhUMDk6MDA6MDBaIiwia2lkIjow'
    'LCJsZWFzZV9zZXJpYWwiOjMsIm1hY2hpbmVfZmluZ2VycHJpbnRfaGFzaCI6ImZkMmVjNWI1MjFh'
    'ZDE4MzE1MmE1YTY5ZWUwYWNkYTZjMjcxYWJiYzYwYTk3ZGE0ODk0MjIyNWRhNDEyMTQ1NjYiLCJz'
    'ZWF0X3Bvb2xfaWQiOiI1ZTRkM2MyYjFhMGY5ZThkN2M2YjVhNDkzODI3MTYwNSIsInN1YnNjcmlw'
    'dGlvbl9leHBpcmVzX2F0IjoiMjAyNy0wOS0zMFQyMzo1OTo1OVoiLCJ0eXAiOiJlcC5saWNlbmNl'
    'LmxlYXNlIiwidiI6MX0=.ktj3b5Qti8UWLLy1aVDhV9uxgZLY_L1PY4LrAeo1NWe7fTcASSpDfgg'
    'qRQy59pgqFsORu3zCi6YmyZBOiA7RDw'
)


def _test_keys():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    private = Ed25519PrivateKey.from_private_bytes(base64.b64decode(TEST_SEED_B64))
    return private, {0: base64.b64decode(TEST_PUBLIC_B64)}


# ---------------------------------------------------------------------------
# Contract §6.4 vectors
# ---------------------------------------------------------------------------

def test_valid_vector_verifies_and_decodes_to_the_documented_payload():
    from services.licence_lease import verify_lease

    _, public = _test_keys()
    assert verify_lease(VALID, public) == json.loads(PAYLOAD_JSON)


def test_sign_lease_reproduces_the_valid_vector_byte_for_byte():
    from services.licence_lease import serialise_payload, sign_lease

    private, _ = _test_keys()
    payload = json.loads(PAYLOAD_JSON)
    assert serialise_payload(payload).decode("ascii") == PAYLOAD_JSON  # §6.3
    assert sign_lease(payload, private) == VALID


@pytest.mark.parametrize("lease", [TAMPERED, WRONG_KEY, PADDED], ids=["tampered", "wrong_key", "padded"])
def test_invalid_vectors_are_rejected(lease):
    from services.licence_lease import LeaseInvalid, verify_lease

    _, public = _test_keys()
    with pytest.raises(LeaseInvalid):
        verify_lease(lease, public)


# ---------------------------------------------------------------------------
# verify_lease() — the §6.1 steps, in order
# ---------------------------------------------------------------------------

def _resign(payload, private):
    from services.licence_lease import sign_lease

    return sign_lease(payload, private)


@pytest.mark.parametrize("lease", [
    "", ".", "abc", "abc.", ".abc", "a.b.c", "ab+c.def", "abc.de/f", "abc=.def", " " + VALID,
])
def test_malformed_structure_is_rejected(lease):
    from services.licence_lease import LeaseInvalid, verify_lease

    _, public = _test_keys()
    with pytest.raises(LeaseInvalid):
        verify_lease(lease, public)


def test_unknown_kid_is_rejected():
    from services.licence_lease import LeaseInvalid, verify_lease

    with pytest.raises(LeaseInvalid):
        verify_lease(VALID, {1: base64.b64decode(TEST_PUBLIC_B64)})


@pytest.mark.parametrize("change", [{"v": 2}, {"typ": "ep.activation"}, {"kid": "0"}])
def test_wrong_version_type_or_kid_type_is_rejected_even_when_signed(change):
    from services.licence_lease import LeaseInvalid, verify_lease

    private, public = _test_keys()
    payload = {**json.loads(PAYLOAD_JSON), **change}
    with pytest.raises(LeaseInvalid):
        verify_lease(_resign(payload, private), public)


def test_signature_covers_the_transmitted_segment_not_reserialised_json():
    """A semantically identical payload serialised differently is a
    different segment, and the original signature must not cover it."""
    from services.licence_lease import LeaseInvalid, verify_lease

    _, public = _test_keys()
    segment, signature = VALID.split(".")
    pretty = json.dumps(json.loads(PAYLOAD_JSON), indent=1).encode()
    other = base64.urlsafe_b64encode(pretty).rstrip(b"=").decode()
    with pytest.raises(LeaseInvalid):
        verify_lease(f"{other}.{signature}", public)


def test_sign_lease_reads_no_configuration(monkeypatch):
    """F5: pure — the key is an argument. Breaking every config value must
    not change the output."""
    import config
    from services.licence_lease import sign_lease

    monkeypatch.setattr(config, "LICENCE_SIGNING_KEY", "garbage")
    monkeypatch.setattr(config, "LICENCE_SIGNING_KID", "garbage")
    private, _ = _test_keys()
    assert sign_lease(json.loads(PAYLOAD_JSON), private) == VALID


# ---------------------------------------------------------------------------
# load_signing_key() — F6
# ---------------------------------------------------------------------------

def _fresh_seed_b64():
    import os

    return base64.b64encode(os.urandom(32)).decode()


def test_a_valid_key_loads():
    from services.licence_lease import load_signing_key

    key, kid = load_signing_key(_fresh_seed_b64(), "1")
    assert kid == 1
    assert len(key.public_key().public_bytes_raw()) == 32


@pytest.mark.parametrize("key_b64, kid, reason", [
    ("", "1", "LICENCE_SIGNING_KEY is not set"),
    ("not base64!!", "1", "not valid base64"),
    (base64.b64encode(b"x" * 31).decode(), "1", "32"),
    (base64.b64encode(b"x" * 64).decode(), "1", "32"),
    (TEST_SEED_B64, "1", "published TEST"),
    ("__fresh__", "", "LICENCE_SIGNING_KID is not set"),
    ("__fresh__", "0", "at least 1"),
    ("__fresh__", "-3", "at least 1"),
    ("__fresh__", "one", "integer"),
    ("__fresh__", "1.5", "integer"),
])
def test_bad_keys_are_refused_with_a_reason(key_b64, kid, reason):
    from services.licence_lease import SigningKeyError, load_signing_key

    if key_b64 == "__fresh__":
        key_b64 = _fresh_seed_b64()
    with pytest.raises(SigningKeyError, match=reason):
        load_signing_key(key_b64, kid)


def test_the_published_test_seed_is_refused_however_it_is_written():
    from services.licence_lease import SigningKeyError, load_signing_key

    for variant in (TEST_SEED_B64, "  " + TEST_SEED_B64 + "\n"):
        with pytest.raises(SigningKeyError, match="published TEST"):
            load_signing_key(variant, "1")
