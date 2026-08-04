from cloudhelm.security import (
    digest_token,
    hash_secret,
    new_opaque_token,
    safe_relative_path,
    verify_secret,
)


def test_agent_secret_hash_roundtrip():
    encoded = hash_secret("a strong agent token")
    assert encoded != "a strong agent token"
    assert verify_secret("a strong agent token", encoded)
    assert not verify_secret("wrong token", encoded)


def test_opaque_tokens_are_random_and_only_digests_are_stored():
    first = new_opaque_token()
    second = new_opaque_token()
    assert first != second
    assert len(digest_token(first)) == 64
    assert first not in digest_token(first)


def test_oauth_next_path_must_stay_on_this_site():
    assert safe_relative_path("/nodes?id=1") == "/nodes?id=1"
    assert safe_relative_path("https://evil.example") == "/"
    assert safe_relative_path("//evil.example") == "/"
    assert safe_relative_path("/%2f%2fevil.example") == "/"
    assert safe_relative_path("/\\evil.example") == "/"
