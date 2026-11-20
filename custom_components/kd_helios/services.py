"""Services for KD Helios.

Only one action so far: writing the planned time-of-use programs to a Deye
inverter. It is gated twice on purpose, because writing time-of-use registers
changes how a real battery behaves:

* the battery configuration must have writes explicitly enabled, and
* the call must pass ``confirm: true``.

Writes go through the Solarman integration's generic register service, since
Solarman's Deye definition does not expose the time-of-use block as entities.
"""

from __future__ import annotations

import asyncio
import logging

import voluptuous as vol
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv

from .const import DOMAIN, SERVICE_APPLY_TOU, SOLARMAN_WRITE_SERVICE

_LOGGER = logging.getLogger(__name__)

ATTR_ENTRY_ID = "entry_id"
ATTR_CONFIRM = "confirm"

#: Pause between register writes: the inverter's Modbus link is easily overrun
#: when several blocks are written back to back.
WRITE_PAUSE = 0.3

APPLY_TOU_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ENTRY_ID): cv.string,
        vol.Required(ATTR_CONFIRM): cv.boolean,
    }
)


def async_setup_services(hass: HomeAssistant) -> None:
    """Register the KD Helios actions."""

    async def async_apply_tou(call: ServiceCall) -> ServiceResponse:
        """Write the currently planned time-of-use programs to the inverter."""
        entry_id = call.data[ATTR_ENTRY_ID]
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is None or entry.domain != DOMAIN:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="unknown_entry",
            )

        engine = entry.runtime_data.engine
        config = engine.battery_config
        if config is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="no_battery",
            )
        if not config.allow_writes:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="writes_disabled",
            )
        if not call.data[ATTR_CONFIRM]:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="confirm_required",
            )
        if not config.inverter_device_id:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="no_inverter_device",
            )
        if engine.tou_plan is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="no_plan",
            )
        if not hass.services.has_service("solarman", SOLARMAN_WRITE_SERVICE):
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="solarman_unavailable",
            )

        registers = engine.tou_plan.registers()
        for index, (register, values) in enumerate(registers.items()):
            if index:
                await asyncio.sleep(WRITE_PAUSE)
            await hass.services.async_call(
                "solarman",
                SOLARMAN_WRITE_SERVICE,
                {
                    "device": config.inverter_device_id,
                    "register": register,
                    "values": values,
                },
                blocking=True,
            )
            _LOGGER.info(
                "Wrote time-of-use registers %s..%s with %s",
                register,
                register + len(values) - 1,
                values,
            )

        return {
            "entry_id": entry_id,
            "registers": {
                str(register): values for register, values in registers.items()
            },
            "programs": [
                {
                    "start": f"{program.hhmm:04d}",
                    "power_w": program.power_w,
                    "soc_pct": program.soc_pct,
                    "source": int(program.source),
                }
                for program in engine.tou_plan.programs
            ],
        }

    hass.services.async_register(
        DOMAIN,
        SERVICE_APPLY_TOU,
        async_apply_tou,
        schema=APPLY_TOU_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
