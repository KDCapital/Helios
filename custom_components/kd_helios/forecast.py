"""What the sun is expected to deliver, and the charge it makes unnecessary.

Buying from the grid in the morning for energy the sun would have delivered in
the afternoon is the classic solar-battery mistake: the energy is bought at the
cheap rate, but it would have been free. KD Helios therefore discounts the
energy it plans to buy by the surplus the forecast still expects, so the plan
buys the shortfall rather than the whole target.

The forecast is trusted only partly: the credit is divided by a cushion before
it is used, so an optimistic forecast shrinks the purchase without erasing it.
Losing that cushion costs a little grid energy; trusting it and being wrong
leaves the battery empty at the evening peak.

Everything here is side-effect free.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .decisions import SOURCE_SOLAR, Blocker

#: How much of the forecast is trusted. A forecast is divided by this before it
#: is credited, so 1.3 means "buy for the deficit even if the sun is expected to
#: cover 130 percent of it".
DEFAULT_CUSHION = 1.3

#: A credit smaller than this is noise, not a saving worth changing the plan for.
DEFAULT_DEADBAND_KWH = 0.5

#: Credit states, exposed to the user.
STATE_DISABLED = "disabled"
STATE_NO_FORECAST = "no_forecast"
STATE_CHARGING_ALLOWED = "charging_allowed"
STATE_PARTLY_SOLAR = "partly_solar"
STATE_WAITING_FOR_SOLAR = "waiting_for_solar"
STATE_NO_CONSUMPTION = "no_consumption_forecast"


@dataclass(frozen=True, slots=True)
class SolarCredit:
    """How much of the energy needed today the sun is expected to deliver."""

    credit_kwh: float = 0.0
    state: str = STATE_DISABLED
    forecast_kwh: float | None = None
    consumption_kwh: float | None = None
    blocker: Blocker | None = field(default=None)

    @property
    def active(self) -> bool:
        """Return True when the forecast reduced what the plan buys."""
        return self.credit_kwh > 0.0


def solar_credit(
    forecast_kwh: float | None,
    consumption_kwh: float | None,
    needed_kwh: float,
    *,
    cushion: float = DEFAULT_CUSHION,
    deadband_kwh: float = DEFAULT_DEADBAND_KWH,
) -> SolarCredit:
    """Return the energy the sun is expected to cover, and the state it implies.

    ``forecast_kwh`` is the production still expected today; ``consumption_kwh``
    is what the house will still use. Their difference is what could reach the
    battery, and only that part of ``needed_kwh`` is credited.
    """
    if forecast_kwh is None:
        return SolarCredit(
            state=STATE_NO_FORECAST,
            blocker=Blocker(SOURCE_SOLAR, "no_forecast"),
        )

    if consumption_kwh is None:
        # Without an expected load there is no surplus to speak of. Crediting
        # the whole forecast against an unknown house would empty the battery
        # at dusk on the first sunny day, so the hold waits for a learned
        # profile instead of guessing.
        return SolarCredit(
            forecast_kwh=forecast_kwh,
            state=STATE_NO_CONSUMPTION,
            blocker=Blocker(SOURCE_SOLAR, "no_consumption_forecast"),
        )

    surplus = max(0.0, float(forecast_kwh) - max(0.0, float(consumption_kwh or 0.0)))
    if surplus <= 0.0 or needed_kwh <= 0.0:
        return SolarCredit(
            forecast_kwh=forecast_kwh,
            consumption_kwh=consumption_kwh,
            state=STATE_CHARGING_ALLOWED,
        )

    cushion = max(1.0, cushion)
    credit = min(float(needed_kwh), surplus / cushion)
    if credit < deadband_kwh:
        return SolarCredit(
            forecast_kwh=forecast_kwh,
            consumption_kwh=consumption_kwh,
            state=STATE_CHARGING_ALLOWED,
        )

    state = STATE_WAITING_FOR_SOLAR if credit >= needed_kwh else STATE_PARTLY_SOLAR
    return SolarCredit(
        credit_kwh=round(credit, 3),
        state=state,
        forecast_kwh=forecast_kwh,
        consumption_kwh=consumption_kwh,
        blocker=Blocker(
            SOURCE_SOLAR,
            state,
            {
                "forecast_kwh": round(float(forecast_kwh), 2),
                "consumption_kwh": (
                    None if consumption_kwh is None else round(consumption_kwh, 2)
                ),
                "credit_kwh": round(credit, 2),
            },
        ),
    )


def remaining_consumption_kwh(
    profile: object,
    now: datetime,
    until: datetime,
) -> float | None:
    """Return the house energy the profile expects between two instants.

    ``None`` means the profile cannot say, which is not the same as zero: the
    caller must then fall back to not crediting any solar, because crediting sun
    against an unknown load is how a battery ends up empty at dusk.
    """
    energy = getattr(profile, "energy_kwh", None)
    if not callable(energy):
        return None
    try:
        value = energy(now, until)
    except Exception:  # noqa: BLE001 - a broken profile must not stop planning
        return None
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return None
