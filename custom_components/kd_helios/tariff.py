"""Pure tariff calculations for KD Helios.

Everything in this module is a side effect free function on plain numbers, so
the money math can be unit tested and reasoned about without Home Assistant.

Sign convention: ``cost`` and ``reimbursement`` are positive amounts of money.
A device's net cost is ``cost - reimbursement - ere_value`` and may become
negative, for example when an employer reimburses more than the home tariff.
"""

from __future__ import annotations

from .const import MarketPriceBasis, ReimbursementMode

VAT_RATE = 0.21


def all_in_from_components(
    spot: float,
    spot_coefficient: float,
    provider_charge: float,
    energy_tax: float,
    vat_rate: float = VAT_RATE,
) -> float:
    """Reconstruct the all-in price from epexprijzen.nl components.

    The API reports ``provider_charge`` and ``energy_tax`` including VAT while
    the spot price is quoted excluding VAT, which makes the consumer price::

        spot * coefficient * (1 + VAT) + provider_charge + energy_tax
    """
    return spot * spot_coefficient * (1 + vat_rate) + provider_charge + energy_tax


def reimbursement_rate(
    mode: ReimbursementMode,
    *,
    fixed_rate: float = 0.0,
    surcharge: float = 0.0,
    percent: float = 100.0,
    basis: MarketPriceBasis = MarketPriceBasis.SPOT,
    spot: float | None = None,
    all_in: float | None = None,
) -> float | None:
    """Return the employer reimbursement in EUR/kWh for one price point.

    ``fixed`` reimbursement pays :attr:`fixed_rate` regardless of the tariff.

    ``dynamic`` reimbursement follows the user's formula::

        (market price + fixed surcharge) * surcharge percentage

    where ``percent`` is expressed as a percentage (``100`` means factor 1.0,
    ``105`` adds five percent). With ``percent=100`` this reduces to the
    additive form ``market + surcharge``.

    Returns ``None`` when the required market price is unavailable.
    """
    if mode is ReimbursementMode.NONE:
        return None
    if mode is ReimbursementMode.FIXED:
        return fixed_rate

    market = spot if basis is MarketPriceBasis.SPOT else all_in
    if market is None:
        return None
    return (market + surcharge) * (percent / 100.0)


def ere_value_per_kwh(ere_per_kwh: float, ere_price: float) -> float:
    """Return the ERE certificate value of one kWh in EUR."""
    return ere_per_kwh * ere_price


def ere_ratio(
    credits: float,
    energy_kwh: float,
    previous_credits: float,
    previous_energy_kwh: float,
    minimum_window_kwh: float,
) -> float | None:
    """Derive ERE credits per kWh from two cumulative readings.

    Returns ``None`` until enough energy has been observed for the ratio to be
    meaningful, or when the counters went backwards.
    """
    delta_credits = credits - previous_credits
    delta_energy = energy_kwh - previous_energy_kwh
    if delta_energy < minimum_window_kwh:
        return None
    if delta_credits < 0:
        return None
    return delta_credits / delta_energy
