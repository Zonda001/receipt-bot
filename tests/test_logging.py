import logging

from receipt_bot.__main__ import RedactingFormatter


def test_secrets_are_redacted_including_tracebacks():
    token = "123456:SECRET-TOKEN-abc"
    fmt = RedactingFormatter([token, ""])
    try:
        raise RuntimeError(f"502, url='https://api.telegram.org/file/bot{token}/photos/a.jpg'")
    except RuntimeError:
        record = logging.LogRecord("x", logging.ERROR, __file__, 1, "download %s", (token,), exc_info=True)
        import sys
        record.exc_info = sys.exc_info()
    out = fmt.format(record)
    assert token not in out and "***" in out
