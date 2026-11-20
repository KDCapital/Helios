"""Feed-in (teruglever) deductions per energy provider.

epexprijzen.nl publishes feed-in data in the provider dataset that ships with
its web application, but **not** through its JSON API: ``/api/v1/providers``
only carries ids and names, and ``/api/v1/prices/{provider}/{interval}``
carries import prices and their components only. The table below is therefore
a snapshot of that provider dataset (see :data:`FEED_IN_SNAPSHOT_DATE`),
converted to the VAT inclusive deduction that providers quote in their
contracts.

It exists only to *suggest* a value. The config flow requires the user to
confirm or correct it, because this data can drift, and a wrong feed-in
deduction would silently distort every euro of export revenue.

Export price model::

    export_price = epex_spot * coefficient - deduction

A negative deduction means the provider pays a bonus on top of the spot price
(for example Zonneplan, which hands back its own purchase fee).
"""

from __future__ import annotations

from typing import Final

#: Date the provider dataset behind this table was read.
FEED_IN_SNAPSHOT_DATE: Final = "2026-11-20"

#: Deduction from the EPEX spot price per provider, in EUR/kWh including VAT.
FEED_IN_DEDUCTION: Final[dict[str, float]] = {
    "anwb-energie": 0.0,
    "budget-energie": 0.0,
    "coolblue-energie": 0.02,
    "delta-energie": 0.0,
    "easyenergy": 0.02178,
    "eneco": 0.01573,
    "energie-vanons": 0.029,
    "energiedirect": 0.0205,
    "energiek": 0.01803,
    "energyzero": 0.02242,
    "engie": 0.01589,
    "essent": 0.02528,
    "frank-energie": 0.01392,
    "frank-energie-slim": -0.01815,
    "greenchoice": 0.02242,
    "groenpand": 0.0242,
    "hegg": 0.02242,
    "innova": -0.00605,
    "liveenergy": 0.0242,
    "mega": 0.01452,
    "nextenergy": 0.0,
    "noord-energie": 0.01815,
    "oxxio": 0.0189,
    "powerpeers": 0.01,
    "pure-energie": 0.01803,
    "quatt-energy": 0.01815,
    "samenom": 0.025,
    "samsam": 0.02242,
    "sunergy": 0.02242,
    "tibber": 0.018,
    "tinqe": 0.015,
    "vandebron": 0.01361,
    "vattenfall": 0.0,
    "zonneplan": -0.02,
}

#: Spot coefficient applied to the feed-in price, where the provider
#: publishes one. Providers that are absent use 1.0.
FEED_IN_SPOT_COEFFICIENT: Final[dict[str, float]] = {}


def feed_in_deduction(provider: str) -> float | None:
    """Return the suggested feed-in deduction for a provider."""
    return FEED_IN_DEDUCTION.get(provider)


def feed_in_spot_coefficient(provider: str) -> float:
    """Return the spot coefficient the provider applies to feed-in."""
    return FEED_IN_SPOT_COEFFICIENT.get(provider, 1.0)
