import pytest

from app.ingest.validator import PayloadError, parse_temperature, validate_topic


def parse(payload: bytes) -> float:
    return parse_temperature(payload, plausible_min=-60.0, plausible_max=60.0)


def test_parses_plain_float():
    assert parse(b"4.25") == 4.25


def test_parses_negative_and_whitespace():
    assert parse(b" -18.5\n") == -18.5


def test_rejects_non_numeric():
    with pytest.raises(PayloadError):
        parse(b"DROP TABLE measurement")


def test_rejects_nan_and_inf():
    for bad in (b"nan", b"inf", b"-inf"):
        with pytest.raises(PayloadError):
            parse(bad)


def test_rejects_implausible_values():
    with pytest.raises(PayloadError):
        parse(b"999")
    with pytest.raises(PayloadError):
        parse(b"-273")


def test_rejects_oversized_payload():
    with pytest.raises(PayloadError):
        parse(b"1" * 1000)


def test_rejects_non_utf8():
    with pytest.raises(PayloadError):
        parse(b"\xff\xfe")


def test_topic_length_limits():
    assert validate_topic("fridge/sensor1") == "fridge/sensor1"
    with pytest.raises(PayloadError):
        validate_topic("")
    with pytest.raises(PayloadError):
        validate_topic("x" * 501)
