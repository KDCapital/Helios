"""Writing the time-of-use programs to a Deye inverter.

All writes go through the Solarman integration's generic register services: its
Deye definition does not expose the time-of-use block as entities, and its
entities are read-only. Blocks are written one at a time with a pause, because
the inverter's Modbus link is easily overrun by back-to-back writes.

A write that returns without an exception is not proof that the inverter took
it: the link can drop a block, and a half-applied schedule is worse than the
old one because it mixes new times with old powers. When Solarman exposes its
read service, every block is therefore read back and compared, and the whole
set is written again once before the caller is told it failed.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr

from .battery import (
    GRID_CHARGE_REGISTER,
    TOU_ENABLE_ALL_DAYS,
    TOU_ENABLE_REGISTER,
    TOU_REGISTERS,
    TOU_SLOTS,
    WORK_MODE_REGISTER,
    TouPlan,
    WorkMode,
)
from .const import SOLARMAN_WRITE_SERVICE, WRITE_VERIFY_RETRIES

_LOGGER = logging.getLogger(__name__)

SOLARMAN_DOMAIN = "solarman"
SOLARMAN_READ_SERVICE = "read_multiple_holding_registers"
WRITE_PAUSE = 0.3


class InverterWriteError(HomeAssistantError):
    """Raised when the inverter cannot be written, or did not take the write."""


class InverterNotReadyError(InverterWriteError):
    """Raised while the integration that owns the inverter is not loaded.

    Home Assistant sets up custom integrations concurrently, so a write can
    run before Solarman has finished its own setup. Its config entry then has
    no ``runtime_data`` yet, and calling one of its services raises an
    ``AttributeError`` from inside Solarman. That is a startup ordering issue,
    not a refused write, so the caller retries quietly instead of raising a
    Repair.
    """


def _owning_entry(hass: HomeAssistant, device_id: str) -> ConfigEntry | None:
    """Return the config entry that owns a device."""
    device = dr.async_get(hass).async_get(device_id)
    if device is None or not device.config_entry_id:
        return None
    return hass.config_entries.async_get_entry(device.config_entry_id)


def _inverter_is_ready(hass: HomeAssistant, device_id: str) -> bool | None:
    """Return whether the inverter's integration is loaded.

    ``None`` means there is no device registry entry to judge by; the service
    call is then left to report the problem itself.
    """
    entry = _owning_entry(hass, device_id)
    if entry is None:
        return None
    return entry.domain == SOLARMAN_DOMAIN and entry.state is ConfigEntryState.LOADED


def _ensure_write_service(hass: HomeAssistant, device_id: str) -> None:
    """Fail early when Solarman cannot write for this inverter."""
    entry = _owning_entry(hass, device_id)
    if entry is not None and entry.domain != SOLARMAN_DOMAIN:
        raise InverterWriteError(
            "The selected inverter device does not belong to the Solarman "
            f"integration but to {entry.domain}"
        )
    if entry is not None and entry.state is not ConfigEntryState.LOADED:
        # Checked before the service table: Solarman registers its services
        # before it finishes loading the entry, and calling one in that window
        # is exactly what raises the runtime-data error.
        raise InverterNotReadyError(
            "The Solarman integration is not loaded yet "
            f"({entry.state.value}) for this inverter"
        )
    if not hass.services.has_service(SOLARMAN_DOMAIN, SOLARMAN_WRITE_SERVICE):
        raise InverterWriteError(
            "The Solarman integration does not expose "
            f"{SOLARMAN_DOMAIN}.{SOLARMAN_WRITE_SERVICE}"
        )


def _raise_if_not_ready(err: AttributeError) -> None:
    """Translate Solarman's missing-runtime-data error into a retryable one."""
    if "runtime_data" not in str(err):
        raise err
    raise InverterNotReadyError(
        "The Solarman integration has not finished setting up this inverter"
    ) from err


async def async_read_registers(
    hass: HomeAssistant,
    device_id: str,
    register: int,
    count: int,
) -> dict[int, int] | None:
    """Read a block of holding registers, or None when reading is not available.

    ``None`` is not a failure: older Solarman builds expose only the write
    services, and a missing read must degrade verification, never block control.
    """
    if not hass.services.has_service(SOLARMAN_DOMAIN, SOLARMAN_READ_SERVICE):
        return None
    if _inverter_is_ready(hass, device_id) is False:
        return None
    try:
        response = await hass.services.async_call(
            SOLARMAN_DOMAIN,
            SOLARMAN_READ_SERVICE,
            {"device": device_id, "register": register, "count": count},
            blocking=True,
            return_response=True,
        )
    except Exception as err:  # noqa: BLE001 - a read failure must not stop control
        _LOGGER.warning(
            "Could not read back registers %s..%s: %s",
            register,
            register + count - 1,
            err,
        )
        return None
    if not isinstance(response, Mapping):
        return None
    values: dict[int, int] = {}
    for key, value in response.items():
        try:
            values[int(key)] = int(value)
        except (TypeError, ValueError):
            continue
    return values or None


async def async_read_tou(
    hass: HomeAssistant,
    device_id: str,
) -> dict[int, list[int]] | None:
    """Return the schedule the inverter actually holds, or None if unreadable."""
    blocks: dict[int, list[int]] = {}
    for register in TOU_REGISTERS.values():
        block = await async_read_registers(hass, device_id, register, TOU_SLOTS)
        if block is None:
            return None
        blocks[register] = [
            block.get(register + offset, 0) for offset in range(TOU_SLOTS)
        ]
    return blocks


async def _verify_registers(
    hass: HomeAssistant,
    device_id: str,
    registers: dict[int, list[int]],
) -> dict[int, dict[str, int | None]] | None:
    """Return register mismatches, or None when the read is not available."""
    mismatches: dict[int, dict[str, int | None]] = {}
    checked = False
    for register, expected in registers.items():
        actual_block = await async_read_registers(
            hass, device_id, register, len(expected)
        )
        if actual_block is None:
            return None
        checked = True
        for offset, value in enumerate(expected):
            actual = actual_block.get(register + offset)
            # A register the device did not answer for is as bad as a wrong
            # one: the schedule cannot be confirmed either way.
            if actual != value:
                mismatches[register + offset] = {
                    "expected": value,
                    "actual": actual,
                }
    if not checked:
        return None
    return mismatches


async def async_write_registers(
    hass: HomeAssistant,
    device_id: str,
    registers: dict[int, list[int]],
    *,
    work_mode: WorkMode | int | None = None,
) -> dict[int, list[int]]:
    """Write register blocks, and optionally the work mode, to the inverter."""
    _ensure_write_service(hass, device_id)

    written: dict[int, list[int]] = {}
    first = True
    for register, values in registers.items():
        if not first:
            await asyncio.sleep(WRITE_PAUSE)
        first = False
        try:
            await hass.services.async_call(
                SOLARMAN_DOMAIN,
                SOLARMAN_WRITE_SERVICE,
                {"device": device_id, "register": register, "values": values},
                blocking=True,
            )
        except AttributeError as err:
            _raise_if_not_ready(err)
        written[register] = values
        _LOGGER.info(
            "Wrote registers %s..%s with %s",
            register,
            register + len(values) - 1,
            values,
        )

    if work_mode is not None:
        await asyncio.sleep(WRITE_PAUSE)
        try:
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
        except AttributeError as err:
            _raise_if_not_ready(err)
        _LOGGER.info("Wrote work mode %s", int(work_mode))

    return written


def _tou_registers(tou: TouPlan, *, enable_charge: bool) -> dict[int, list[int]]:
    """Return the TOU blocks plus the master switches they need.

    The four time/power/SOC/source blocks are ignored by the inverter when
    grid charging or the weekly TOU schedule is off. Those masters are written
    last, after the programs, so the inverter never applies a half-written
    schedule.
    """
    registers = tou.registers()
    registers[GRID_CHARGE_REGISTER] = [1 if enable_charge else 0]
    registers[TOU_ENABLE_REGISTER] = [TOU_ENABLE_ALL_DAYS if enable_charge else 0]
    return registers


async def async_write_tou(
    hass: HomeAssistant,
    device_id: str,
    tou: TouPlan,
    *,
    work_mode: WorkMode | int | None = None,
    enable_charge: bool = True,
) -> dict[str, Any]:
    """Write a full set of six time-of-use programs."""
    registers = _tou_registers(tou, enable_charge=enable_charge)
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


async def async_write_tou_verified(
    hass: HomeAssistant,
    device_id: str,
    tou: TouPlan,
    *,
    work_mode: WorkMode | int | None = None,
    retries: int = WRITE_VERIFY_RETRIES,
    enable_charge: bool = True,
) -> dict[str, Any]:
    """Write the schedule and prove the inverter took it.

    Returns ``verified`` True when every register read back as written, False
    when a mismatch survived the retry, and None when the transport cannot read
    at all (the write is then accepted on trust, as it was before).
    """
    registers = _tou_registers(tou, enable_charge=enable_charge)
    attempts = max(1, retries + 1)
    mismatches: dict[int, dict[str, int | None]] = {}

    for attempt in range(1, attempts + 1):
        await async_write_registers(hass, device_id, registers, work_mode=work_mode)
        read_back = await _verify_registers(hass, device_id, registers)
        if read_back is None:
            _LOGGER.debug(
                "Solarman cannot read registers; accepting the write on trust"
            )
            return {
                "verified": None,
                "attempts": attempt,
                "mismatches": {},
            }
        if not read_back:
            return {"verified": True, "attempts": attempt, "mismatches": {}}
        mismatches = read_back
        _LOGGER.warning(
            "Inverter did not take the schedule (attempt %s/%s): %s",
            attempt,
            attempts,
            read_back,
        )

    raise InverterWriteError(
        f"The inverter did not accept the schedule after {attempts} attempts: "
        f"{mismatches}"
    )


def register_order() -> list[int]:
    """Return the register order used for a write, for diagnostics."""
    return [
        TOU_REGISTERS["times"],
        TOU_REGISTERS["power"],
        TOU_REGISTERS["soc"],
        TOU_REGISTERS["sources"],
        GRID_CHARGE_REGISTER,
        TOU_ENABLE_REGISTER,
    ]
