import pytest

from scraper.main import _REPORT_CRON, _cron_fields


def test_cron_fields_accepts_standard_five_field_cron():
    assert _cron_fields("0 3 * * *") == ("0", "3", "*", "*", "*")


def test_cron_fields_falls_back_for_invalid_cron():
    # The daily report is the only cron left, so its expression is the fallback.
    assert _cron_fields("not enough fields") == tuple(_REPORT_CRON.split())


def test_cron_fields_rejects_invalid_fallback():
    with pytest.raises(ValueError):
        _cron_fields("bad", fallback="also bad")
