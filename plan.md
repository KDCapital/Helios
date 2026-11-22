# Plan: multi-brand inverter support

**Status:** proposed — not implemented
**Date:** 2026-11-22
**Scope:** battery charge planning and inverter control
**Related code:** [battery.py](custom_components/kd_helios/battery.py), [inverter.py](custom_components/kd_helios/inverter.py), [coordinator.py](custom_components/kd_helios/coordinator.py), [config_flow.py](custom_components/kd_helios/config_flow.py), [services.py](custom_components/kd_helios/services.py)

## 1. Goal

Let a user pick their inverter in the battery setup and have KD Helios:

1. recognise which integration/device it is,
2. propose a supported driver and model profile,
3. show what it can and cannot do,
4. write a safe charging schedule through that brand's integration.

Read-only support (cost tracking and planning without writes) must work for any brand
from day one. Control support is opt-in and grows driver by driver.

Non-goals for the first iteration:

- a universal register-map editor for end users;
- cloud control APIs (too slow and rate-limited for quarter-hour plans);
- talking to the inverter over a KD Helios-owned Modbus socket while another
  integration owns the same connection.

## 2. Current state (commit e78fc75)

The planner is mostly brand-agnostic, but everything below it is a Deye adapter:

| Location | Deye/Solarman assumption |
|---|---|
| [battery.py](custom_components/kd_helios/battery.py#L39-L54) | `TOU_REGISTERS`, `TOU_SLOTS`, `WORK_MODE_REGISTER`, `ChargeSource`, `WorkMode` |
| `battery.py` | Six combined HHMM slots, power in W, per-slot SOC target and source, `TouPlan.registers()` |
| [inverter.py](custom_components/kd_helios/inverter.py) | Solarman write/read services; verification of four register blocks |
| [coordinator.py](custom_components/kd_helios/coordinator.py#L249-L380) | Control/release, power-cap rewrite and verification through those helpers |
| [services.py](custom_components/kd_helios/services.py#L80-L100) | Refuses to run without the Solarman service |
| [config_flow.py](custom_components/kd_helios/config_flow.py#L500-L580) | Device selector filtered to `solarman`; Deye `restore_work_mode` |
| Tests | Fake Solarman services; four blocks; six slots; Deye work modes |

Consequence: adding a brand currently means editing the planner, the coordinator,
the service, the flow and half the test suite. Phase 1 removes that coupling
before a second brand is added.

## 3. Target architecture

```text
ChargePlan                     brand-agnostic: windows, kWh, kW, SOC
    │
    ▼
Schedule projector             uses InverterCapabilities
    │                          start/end, charge kW, target SOC, source
    ▼
InverterDriver                 transport + model profile
    │                          serialise, apply, read back, restore, cap
    ▼
Vendor HA integration          services / entities, owns the connection
```

Proposed layout:

```text
custom_components/kd_helios/inverters/
  __init__.py       # registry lookup
  base.py           # InverterDriver, Capabilities, Detection, ApplyResult
  schedule.py       # project(plan, settings, capabilities) -> InverterSchedule
  deye.py           # current Solarman/Deye implementation
  solis.py          # first non-Deye driver, added later
```

Layers:

1. **Planner** (`plan_charge`, `should_apply`, fingerprints): no registers, no
   work modes, no vendor names. Stays in `battery.py` or moves to `planner.py`.
2. **Projector** (`schedule.py`): turns a `ChargePlan` into a canonical schedule,
   honouring slot count, SOC support, source support and mode semantics.
3. **Driver** (`inverters/*.py`): knows the transport, registers/entities,
   verification and safe restore path. Thin and testable against a fake service.
4. **Registry**: maps a device to candidate drivers, using domain + manufacturer
   + model + firmware, then a read-only probe.

### Canonical schedule

```python
@dataclass(frozen=True, slots=True)
class InverterSlot:
    start: datetime
    end: datetime
    charge_kw: float
    target_soc_pct: float | None
    source: ChargeSource | None

@dataclass(frozen=True, slots=True)
class InverterSchedule:
    slots: tuple[InverterSlot, ...]
    degraded: tuple[str, ...] = ()   # capabilities that were not available
```

`build_tou_programs`, `build_restore_programs` and `TouPlan.registers()` move to
the Deye driver (or a Deye-specific module). The canonical schedule is what
`AppliedPlan` and the dashboard use, so the panel does not need driver knowledge.

## 4. Driver contract

Keep this small and capability-driven. Do not build a generic register-map
engine; curated drivers are safer and easier to verify.

```python
@dataclass(frozen=True, slots=True)
class Capabilities:
    max_slots: int
    slot_kind: Literal["combined", "charge_discharge_pairs"]
    power_unit: Literal["watt", "amp"]
    per_slot_soc: bool
    per_slot_source: bool
    power_cap: bool
    verified_readback: bool
    needs_battery_voltage: bool
    restore_kind: str

@dataclass(frozen=True, slots=True)
class Detection:
    driver: str
    profile: str
    confidence: Literal["confirmed", "likely", "manual"]
    capabilities: Capabilities
    details: Mapping[str, Any]

class InverterDriver(Protocol):
    key: str
    domains: tuple[str, ...]

    @classmethod
    def detect(cls, hass, device) -> Detection | None: ...

    async def async_probe(self, device_id, options) -> Detection | None: ...
    async def async_apply(self, schedule: InverterSchedule) -> ApplyResult: ...
    async def async_read(self) -> InverterSchedule | None: ...
    async def async_release(self) -> None: ...
    async def async_set_power_cap(self, kw: float) -> None: ...
```

`async_read` is optional: return `None` when the transport cannot read and let
the capability `verified_readback` say so. `async_set_power_cap` is optional:
drivers without it return a visible blocker instead of ignoring the request.

## 5. Config flow

Device-first, then confirmation. This matches the requested UX without letting a
device alone decide how to write registers.

1. **Inverter**
   - Device selector filtered to the supported integration domains *plus* an
     explicit “No inverter / monitor only” choice.
   - Home Assistant's device selector supports multiple domains through
     `filter=[{"integration": ...}, ...]`, not a multi-value `integration=`.
2. **Detected driver**
   - Run a read-only probe (model/firmware registers or entities; no writes).
   - Show driver, model, firmware and a capability summary, for example:
     “Deye + Solarman — 6 slots, power in W, per-slot SOC, verified writes”.
   - Ambiguous: let the user choose from candidates.
   - Unknown/unprobeable: save as **monitoring only**.
3. **Driver options**
   - Only show fields the driver needs: nominal battery voltage for amp-based
     brands, transport service, model/firmware variant.
   - Deye's restore work mode belongs here, not in the generic battery form.
4. **Battery settings**
   - Existing capacity, powers, reserve/target, efficiency, horizon, sensors.
5. **Writes**
   - Still off by default.
   - Offer **dry run** (show the payload) and **test write** (write, read back,
     restore) before automatic control can be enabled.

Reconfigure rules:

- Changing driver or device while control is enabled must be refused until the
  user releases control.
- Releasing/removing the battery must call the **old** driver's restore path
  before the configuration is replaced or deleted.
- After a firmware change is detected, re-probe and mark the applied schedule
  unverified; do not silently keep writing.

## 6. Detection

Registry entries, most specific first:

```text
(transport domain, manufacturer, model pattern, firmware range) -> driver + profile
```

Inputs available from Home Assistant:

- `device.config_entries` / `device.primary_config_entry` → integration domain
- `manufacturer`, `model`, `model_id`, `sw_version`
- identifiers
- a read-only probe for the truth (model/firmware registers or integration
  sensors)

Why domain alone is not enough:

- **Solarman** is a transport used by several inverter brands.
- **Solis Modbus** may expose a generic device model; RHI/S5/S6 differ.
- **SolaX-modbus** supports multiple brands as well.
- Firmware versions change which registers/protocol variants are safe.

Detection result is stored in the battery subentry and shown in diagnostics. It
is a proposal, not authority: the user confirms it.

## 7. Data model and migration

Battery subentry data:

```text
inverter_driver: "deye_solarman" | "solis_modbus" | ...
inverter_profile: "sun-12k" | "s6-eh" | None
inverter_device_id: <existing key, unchanged>
inverter_options: {battery_voltage: 51.2, ...}
restore_work_mode: -> move into driver options (Deye)
monitoring_only: bool (derived: no driver or writes unsupported)
```

Migration:

- Existing entries without `inverter_driver` default to `deye_solarman`.
- Existing integer `restore_work_mode` maps to the Deye driver option; no user
  action.
- Read `BatteryConfig.from_subentry` with the same defaults so old storage keeps
  working even before a migration runs.
- Bump the subentry/version marker only if a real migration is required; a lazy
  default is cheaper and safer for a HACS integration.

## 8. Safety invariants

1. **One writer per inverter.** Use the integration that owns the connection.
   Never open a second Modbus socket from KD Helios.
2. **Serialise and pace writes.** Generalise the current `WRITE_PAUSE`; one
   writer queue per device.
3. **Read back before trust.** If a driver cannot verify, writes must be
   explicitly allowed as “unverified”, never silently trusted.
4. **Restore before switching.** Driver-specific safe state: Deye writes
   generator-only slots + self-use; Solis sets self-use and grid charging off.
5. **Fail closed.** Unknown profile/firmware → monitoring only; writes disabled.
6. **Degrade visibly.** Missing per-slot SOC/source/power cap becomes a blocker
   shown by the existing blocker registry, never a silent approximation.
7. **No free-text register maps** in the main path. If an expert escape hatch is
   added later, it must be dry-run-first and unsupported.
8. **Dry run and test write** are required for every new driver before it can be
   recommended.

## 9. Refactor map

| From | To |
|---|---|
| `battery.TOU_REGISTERS`, `TOU_SLOTS`, `WORK_MODE_REGISTER` | `inverters/deye.py` |
| `battery.TouProgram`, `TouPlan`, `TouPlan.registers()` | Deye driver + canonical `schedule.py` |
| `battery.build_tou_programs`, `build_restore_programs` | Deye driver / projector |
| `battery.ChargeSource`, `WorkMode` | Deye driver (or canonical source enum if genuinely generic) |
| `inverter.py` | `inverters/deye.py`, with transport helpers shared |
| `coordinator` direct calls to `async_write_*` | `driver.async_apply/release/set_power_cap` |
| `services.py` Solarman check | driver lookup + `driver.async_apply` |
| `config_flow` hard-coded `solarman` selector | driver registry + filtered device selector |
| `strings.json` / translations | Driver-neutral copy, driver-specific labels from driver |
| Tests with Solarman fakes | Shared fake transport + driver contract suite |

`should_apply`, plan fingerprints, `AppliedPlan` and the dashboard payload stay
brand-agnostic. `AppliedPlan.programs` becomes the canonical slot list.

## 10. Phases

### Phase 0 — decision spike (1–2 days)

- Pick the first non-Deye target (suggested: one Solis family on local Modbus).
- Confirm the transport integration and its service/entity contract.
- Capture a dry-run payload and a real read-back from hardware if possible.
- Decide whether the first driver supports TOU registers, dispatch periods, or
  both.

**Done when:** one page of register/service facts, one target model, and a clear
yes/no on verified read-back.

### Phase 1 — abstraction, no behaviour change (2–4 days)

- Add `inverters/base.py`, `schedule.py`, `deye.py`.
- Move Deye constants, TouPlan serialisation, restore and work modes.
- Route coordinator/service/flow through the driver.
- Keep existing tests green; add a Deye driver contract test.
- No config/UI changes beyond selecting the default driver internally.

**Done when:** the Deye path is behaviourally identical, and the coordinator no
longer imports Solarman specifics.

### Phase 2 — device-first detection and flow (2–3 days)

- Add supported-domain filter + “monitor only”.
- Read-only probe and detection result.
- Capability summary in the flow; hide unsupported driver options.
- Store driver/profile/options; lazy migration for existing entries.
- Diagnostics expose driver, model, firmware, capabilities, last verification.
- Dry-run and test-write plumbing.

**Done when:** an existing Deye install needs no action, and a Deye device is
detected and confirmed through the new flow.

### Phase 3 — first non-Deye driver (1–2 weeks incl. hardware)

- Implement `inverters/solis.py` (or the chosen brand) with profile,
  capabilities, apply/read/release/cap.
- Project canonical slots onto the brand's slot semantics (current vs power,
  pairs vs combined, SOC support).
- Contract tests + golden payload tests + hardware validation checklist.
- Start in monitoring-only; enable writes only after the test write passes.

**Done when:** the driver can be selected, probed, applied, read back, released
and power-capped on a real inverter, with visible degradation for unsupported
features.

### Phase 4 — more brands (3–8 days each + hardware)

- Each brand is a driver + profile + contract tests.
- Add a supported-device matrix to the docs.
- Track firmware quirks as capability/profile variants.

## 11. Testing strategy

- **Contract suite** parametrised over drivers: apply a sample schedule, assert
  transport payload, read-back, retry, release, cap, and graceful failure.
- **Golden payloads** per profile (registers/entities), so refactors cannot
  silently change what is written.
- **Fake transports** per integration; no hardware in CI.
- **Projector tests** without hardware: slot count, amps conversion, missing SOC
  support, truncation and degradation.
- **Dry-run tests** that assert the exact would-be payload.
- **Hardware checklist** per brand/model/firmware in `docs/`; CI cannot prove the
  inverter, only the adapter.
- Regression test that switching/deleting a battery calls restore first.

## 12. Open questions / decisions

1. Which exact Solis model and transport is the first target?
2. Do we depend on a vendor integration's raw register service, its entities, or
   both? Prefer raw services where they exist; entities are the fallback.
3. Where does battery voltage come from for amp-based brands: a sensor, a
   configured nominal value, or a driver read?
4. Do we support multiple simultaneous drivers/batteries, or keep one battery per
   entry for now? (Current model: one battery.)
5. How much capability degradation is acceptable before writes are disabled?
6. Do we add an expert register-map escape hatch at all?
7. What is the support policy for unknown firmware: block writes, warn, or both?

## 13. Definition of done for the first release

- Existing Deye/Solarman users see no change and need no reconfiguration.
- A supported second brand can be selected by device, probed read-only, and
  confirmed with a capability summary.
- Monitoring-only works for any selected inverter that cannot be probed.
- Writes are off by default, verified where possible, and restorable.
- Unsupported capabilities appear as blockers, not silent approximations.
- Driver contract tests and a hardware validation record exist for the new brand.
- README/docs list the supported devices and what each driver can do.

## 14. References

External research collected while scoping this plan:

- Solis Modbus Home Assistant integration:
  [Pho3niX90/solis_modbus](https://github.com/Pho3niX90/solis_modbus);
  directory listing with raw register read/write actions:
  [Vulpo](https://vulpo.app/integrations/marketplace/pho3nix90-solis-modbus).
- Solis local Modbus control registers (storage mode, TOU, charging):
  [FixJeEnergy](https://fixjeenergy.nl/nieuws/solis-hybride-omvormer-modbus-rtu-tcp-home-assistant),
  [fboundy register notes](https://github.com/fboundy/ha_solis_modbus/issues/1),
  [DIY Solar register thread](https://diysolarforum.com/threads/time-of-use-modbus-registers-on-solis-rhi-3k-48es-solved-success.129329/).
- Solis model variation:
  [S5/S6 register map](https://modbusconnect.com/configurations/solis-s5-s6),
  [per-slot SOC discussion](https://github.com/Pho3niX90/solis_modbus/issues/93).
- SolaX-modbus Solis support and remote dispatch:
  [wills106/homeassistant-solax-modbus](https://github.com/wills106/homeassistant-solax-modbus),
  [remote dispatch docs](https://homeassistant-solax-modbus.readthedocs.io/en/latest/solis-remote-dispatch/).
- Why not Solis Cloud for control:
  [Jumpy07](https://github.com/Jumpy07/Solis---SolisCloud-and-Home-Assistant).
