"""Tests for financial display and quote availability."""
import pytest

from dashboard.presentation import (
    build_dashboard_presentation_bundle,
    finite_number,
    format_numeric,
    numeric_tone,
    option_mark,
)


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


def test_build_dashboard_presentation_bundle_expired_retention():
    snapshots = {
        "balances": {
            "status": "expired",
            "synced_at": "2026-11-01T12:00:00Z",
            "success_at": 1793534400.0,
            "age": 1200.0,
            "error": "HTTP 500",
            "generation": 42,
            "data": {
                "balances": {
                    "total_equity": 105000.0,
                    "total_cash": 80000.0,
                    "margin": {"option_buying_power": 50000.0, "option_requirement": 15000.0},
                }
            },
        },
        "positions": {
            "status": "expired",
            "synced_at": "2026-11-01T12:00:00Z",
            "success_at": 1793534400.0,
            "age": 1200.0,
            "error": "HTTP 500",
            "generation": 42,
            "data": {
                "positions": {
                    "position": [
                        {"symbol": "SPXW261120P05800000", "quantity": 1, "cost_basis": 500.0},
                    ]
                }
            },
        },
        "orders": {
            "status": "expired",
            "synced_at": "2026-11-01T12:00:00Z",
            "success_at": 1793534400.0,
            "age": 1200.0,
            "error": "HTTP 500",
            "generation": 42,
            "data": {
                "orders": {
                    "order": [
                        {"id": "1001", "symbol": "SPXW261120P05800000", "side": "buy", "quantity": 1, "status": "open", "price": 5.0},
                    ]
                }
            },
        },
        "quote:SPXW261120P05800000": {
            "status": "ready",
            "synced_at": "2026-11-01T12:15:00Z",
            "success_at": 1793535300.0,
            "age": 10.0,
            "error": None,
            "generation": 45,
            "data": {"bid": 5.2, "ask": 5.4, "last": 5.3},
        },
    }

    bundle = build_dashboard_presentation_bundle(snapshots)

    # 1. Generations, age, errors, provider timestamps are preserved
    assert bundle["generations"]["balances"] == 42
    assert bundle["generations"]["quote:SPXW261120P05800000"] == 45
    assert bundle["ages"]["balances"] == 1200.0
    assert any("HTTP 500" in err for err in bundle["errors"])
    assert bundle["provider_timestamps"]["balances"] == 1793534400.0

    # 2. Historical positions and orders are retained separately, not discarded
    assert len(bundle["positions"]) == 1
    assert bundle["positions"][0]["symbol"] == "SPXW261120P05800000"
    assert bundle["positions"][0]["is_expired"] is True
    assert len(bundle["historical_positions"]) == 1
    assert len(bundle["orders"]) == 1
    assert bundle["orders"][0]["id"] == "1001"
    assert bundle["orders"][0]["is_expired"] is True

    # 3. Expired active totals are excluded (not fabricating active balances or $0.00 P&L)
    assert bundle["equity"] is None
    assert bundle["cash"] is None
    assert bundle["unrealized_pnl"] is None
    assert bundle["historical_balances"]["total_equity"] == 105000.0

    # 4. Valuation coverage is reported accurately
    assert bundle["valuation_coverage"]["total"] == 1
    assert bundle["valuation_coverage"]["valued"] == 1
    assert bundle["valuation_coverage"]["coverage_pct"] == 100.0
