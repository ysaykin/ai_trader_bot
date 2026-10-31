"""Tests for financial display and quote availability."""
import pytest

from dashboard.presentation import finite_number, format_numeric, numeric_tone, option_mark


@pytest.mark.parametrize("value", [None, True, float("nan"), float("inf")])
def test_invalid_values_are_unavailable(value):
    assert finite_number(value) is None
    assert format_numeric(value, currency=True) == "N/A"
    assert numeric_tone(value) == "neutral"


def test_signed_currency_and_negative_zero():
    assert format_numeric(-123.45, currency=True) == "-$123.45"
    assert format_numeric(-0.001, currency=True) == "$0.00"
    assert numeric_tone(-1) == "negative"


def test_option_marks_do_not_invent_values():
    assert option_mark(None, None, None) is None
    assert option_mark(float("nan"), 1, None) is None
    assert option_mark(0, 0, None) == 0
    assert option_mark(2, 1, 0) == 0
