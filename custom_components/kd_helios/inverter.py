"""Writing the time-of-use programs to a Deye inverter.

All writes go through the Solarman integration's generic register service: its
Deye definition does not expose the time-of-use block as entities, and its
entities are read-only. Blocks are written one at a time with a pause, because
the inverter's Modbus link is easily overrun by back-to-back writes.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .battery import TOU_REGISTERS, WORK_MODE_REGISTER, TouPlan, WorkMode
from .const import SOLARMAN_WRITE_SERVICE

_LOGGER = logging.getLogger(__name__)

SOLARMAN_DOMAIN = "solarman"
WRITE_PAUSE = 0.3


class InverterWriteError(HomeAssistantError):
    """Raised when the inverter cannot be written."""


def _ensure_write_service(hass: HomeAssistant) -> None:
    """Fail early when Solarman cannot write."""
    if not hass.services.has_service(SOLARMAN_DOMAIN, SOLARMAN_WRITE_SERVICE):
        raise InverterWriteError(
            "The Solarman integration does not expose "
            f"{SOLARMAN_DOMAIN}.{SOLARMAN_WRITE_SERVICE}"
        )


async def async_write_registers(
    hass: HomeAssistant,
    device_id: str,
    registers: dict[int, list[int]],
    *,
    work_mode: WorkMode | int | None = None,
) -> dict[int, list[int]]:
    """Write register blocks, and optionally the work mode, to the inverter."""
    _ensure_write_service(hass)

    written: dict[int, list[int]] = {}
    first = True
    for register, values in registers.items():
        if not first:
            await asyncio.sleep(WRITE_PAUSE)
        first = False
        await hass.services.async_call(
            SOLARMAN_DOMAIN,
            SOLARMAN_WRITE_SERVICE,
            {"device": device_id, "register": register, "values": values},
            blocking=True,
        )
        written[register] = values
        _LOGGER.info(
            "Wrote registers %s..%s with %s",
            register,
            register + len(values) - 1,
            values,
        )

    if work_mode is not None:
        await asyncio.sleep(WRITE_PAUSE)
        await hass.services.async_call(
            SOLARMAN_DOMAIN,
            "write_holding_register",
            {
                "device": device_id,
                "register": WORK_MODE_REGISTER,
                "value": int(work_mode),
            },
            blocking=True,
        )
        _LOGGER.info("Wrote work mode %s", int(work_mode))

    return written


async def async_write_tou(
    hass: HomeAssistant,
    device_id: str,
    tou: TouPlan,
    *,
    work_mode: WorkMode | int | None = None,
) -> dict[str, Any]:
    """Write a full set of six time-of-use programs."""
    registers = tou.registers()
    written = await async_write_registers(
        hass, device_id, registers, work_mode=work_mode
    )
    return {
        "registers": {str(register): values for register, values in written.items()},
        "programs": [
            {
                "start": f"{program.hhmm:04d}",
                "power_w": program.power_w,
                "soc_pct": program.soc_pct,
                "source": int(program.source),
            }
            for program in tou.programs
        ],
    }


def register_order() -> list[int]:
    """Return the register order used for a write, for diagnostics."""
    return [
        TOU_REGISTERS["times"],
        TOU_REGISTERS["power"],
        TOU_REGISTERS["soc"],
        TOU_REGISTERS["sources"],
    ]
