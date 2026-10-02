import pytest

from twitch_radio.admin import passwords


def test_hash_round_trip() -> None:
    stored = passwords.hash_password("correct horse")
    assert passwords.is_password_hash(stored)
    assert passwords.verify_password("correct horse", stored)
    assert not passwords.verify_password("wrong", stored)


def test_hashes_are_salted() -> None:
    assert passwords.hash_password("same") != passwords.hash_password("same")


def test_plain_text_password_still_verifies() -> None:
    assert passwords.verify_password("hunter2", "hunter2")
    assert not passwords.verify_password("hunter3", "hunter2")


def test_overlong_candidate_is_rejected() -> None:
    assert not passwords.verify_password("x" * (passwords.MAX_PASSWORD_LENGTH + 1), "x")


def test_unicode_password() -> None:
    stored = passwords.hash_password("pässwörd✓")
    assert passwords.verify_password("pässwörd✓", stored)


@pytest.mark.parametrize(
    "bad",
    [
        "scrypt:15:8:1:onlyfour",
        "scrypt:99:8:1:AAAA:BBBB",
        "scrypt:15:99:1:AAAA:BBBB",
        "scrypt:15:8:99:AAAA:BBBB",
        "scrypt:15:8:1::BBBB",
        "scrypt:x:8:1:AAAA:BBBB",
    ],
)
def test_malformed_hashes_are_flagged_and_never_verify(bad: str) -> None:
    assert passwords.validate_stored_password(bad) is not None
    assert not passwords.verify_password("anything", bad)


def test_valid_hash_and_plain_text_pass_validation() -> None:
    assert passwords.validate_stored_password(passwords.hash_password("x")) is None
    assert passwords.validate_stored_password("plain") is None
