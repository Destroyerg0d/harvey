"""Verifier keys that travel in a URL must not reach the terminal or a log."""

import logging

from harvey.main import RedactKeysFilter


def _record(msg, *args):
    return logging.LogRecord("httpx", logging.INFO, __file__, 1, msg, args, None)


def test_a_key_in_a_logged_url_is_redacted():
    record = _record('HTTP Request: %s %s "%s"', "GET",
                     "https://verifier.example.test/v2/validate?email=a%40b.test&api_key=abc123secret",
                     "HTTP/1.1 200 OK")
    assert RedactKeysFilter().filter(record)
    message = record.getMessage()
    assert "abc123secret" not in message
    assert "api_key=<redacted>" in message and "email=a%40b.test" in message


def test_every_key_parameter_name_is_covered():
    for name in ("key", "apikey", "api_key", "token", "access_token"):
        record = _record(f"GET https://x.example.test/?{name}=s3cr3t&mode=power")
        RedactKeysFilter().filter(record)
        assert "s3cr3t" not in record.getMessage() and "mode=power" in record.getMessage()


def test_a_key_in_the_url_path_is_redacted():
    # MyEmailVerifier: /verifier/validate_single/{email}/{key}
    record = _record('HTTP Request: %s %s "%s"', "GET",
                     "https://client.myemailverifier.test/verifier/validate_single/"
                     "a%2Bb@c.example.test/pathsecret123", "HTTP/1.1 200 OK")
    RedactKeysFilter().filter(record)
    message = record.getMessage()
    assert "pathsecret123" not in message
    assert "/validate_single/a%2Bb@c.example.test/<redacted>" in message
    assert message.endswith('"HTTP/1.1 200 OK"')


def test_a_line_without_a_key_is_left_alone():
    record = _record("HTTP Request: GET https://shop.example.test/about \"HTTP/1.1 404\"")
    RedactKeysFilter().filter(record)
    assert record.getMessage() == 'HTTP Request: GET https://shop.example.test/about "HTTP/1.1 404"'


def test_the_filter_is_on_the_httpx_logger():
    assert any(isinstance(f, RedactKeysFilter) for f in logging.getLogger("httpx").filters)
