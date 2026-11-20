"""Unit tests for the pure tariff math."""

from __future__ import annotations

import pytest

from custom_components.kd_helios.const import MarketPriceBasis, ReimbursementMode
from custom_components.kd_helios.tariff import (
    all_in_from_components,
    ere_ratio,
    ere_value_per_kwh,
    reimbursement_rate,
)


def test_all_in_matches_the_api_sample() -> None:
    """The documented API components reconstruct the published price."""
    # Recorded from epexprijzen.nl for Zonneplan, 2026-11-20T00:00+01:00.
    assert all_in_from_components(
        spot=0.1632, spot_coefficient=1.0, provider_charge=0.02, energy_tax=0.11085
    ) == pytest.approx(0.328322)


def test_all_in_applies_coefficient_to_spot_only() -> None:
    """A spot coefficient scales the market price, not the fixed components."""
    plain = all_in_from_components(0.10, 1.0, 0.02, 0.11)
    scaled = all_in_from_components(0.10, 1.06, 0.02, 0.11)
    assert scaled - plain == pytest.approx(0.10 * 0.06 * 1.21)


def test_no_reimbursement_returns_none() -> None:
    """Reimbursement is absent unless configured."""
    assert reimbursement_rate(ReimbursementMode.NONE, spot=0.10) is None


def test_fixed_reimbursement_ignores_the_market() -> None:
    """A fixed rate pays the same amount whatever the market does."""
    assert reimbursement_rate(
        ReimbursementMode.FIXED, fixed_rate=0.35, spot=-0.20
    ) == pytest.approx(0.35)


def test_dynamic_reimbursement_spot_basis() -> None:
    """With 100 percent the formula reduces to market plus surcharge."""
    assert reimbursement_rate(
        ReimbursementMode.DYNAMIC,
        surcharge=0.02,
        percent=100.0,
        basis=MarketPriceBasis.SPOT,
        spot=0.10,
        all_in=0.30,
    ) == pytest.approx(0.12)


def test_dynamic_reimbursement_applies_percentage_after_surcharge() -> None:
    """The percentage scales the surcharged market price."""
    assert reimbursement_rate(
        ReimbursementMode.DYNAMIC,
        surcharge=0.02,
        percent=110.0,
        basis=MarketPriceBasis.SPOT,
        spot=0.10,
    ) == pytest.approx((0.10 + 0.02) * 1.10)


def test_dynamic_reimbursement_can_use_all_in_price() -> None:
    """The all-in basis reimburses what the household actually pays."""
    assert reimbursement_rate(
        ReimbursementMode.DYNAMIC,
        surcharge=0.0,
        percent=100.0,
        basis=MarketPriceBasis.ALL_IN,
        spot=0.10,
        all_in=0.28,
    ) == pytest.approx(0.28)


def test_dynamic_reimbursement_without_market_is_none() -> None:
    """A missing market price must not silently pay out zero."""
    assert (
        reimbursement_rate(
            ReimbursementMode.DYNAMIC,
            surcharge=0.02,
            basis=MarketPriceBasis.SPOT,
            spot=None,
        )
        is None
    )


def test_negative_market_price_reduces_reimbursement() -> None:
    """Negative prices flow through the formula unchanged."""
    assert reimbursement_rate(
        ReimbursementMode.DYNAMIC,
        surcharge=0.01,
        basis=MarketPriceBasis.SPOT,
        spot=-0.05,
    ) == pytest.approx(-0.04)


def test_ere_value_per_kwh() -> None:
    """ERE value is the ratio times the certificate price."""
    assert ere_value_per_kwh(0.25, 4.0) == pytest.approx(1.0)


def test_ere_ratio_needs_a_minimum_window() -> None:
    """Too little energy means no trustworthy ratio yet."""
    assert ere_ratio(101.0, 1000.5, 100.0, 1000.0, 1.0) is None


def test_ere_ratio_is_computed_beyond_the_window() -> None:
    """Enough energy yields credits per kWh."""
    assert ere_ratio(120.0, 1100.0, 100.0, 1000.0, 1.0) == pytest.approx(0.2)


def test_ere_ratio_rejects_backwards_counters() -> None:
    """A counter reset must not produce a negative ratio."""
    assert ere_ratio(90.0, 1100.0, 100.0, 1000.0, 1.0) is None
