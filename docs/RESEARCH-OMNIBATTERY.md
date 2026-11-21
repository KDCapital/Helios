# Omnibattery — what we can learn from it

A focused review of [ffunes/Omnibattery](https://github.com/ffunes/Omnibattery),
read at **v1.6.1** (`2e7156f`, 2026-11-17). The checkout used for this review
lives in `.research/vendor/omnibattery` (gitignored); refresh it with:

```bash
git clone --depth 1 https://github.com/ffunes/Omnibattery.git \
  .research/vendor/omnibattery
```

This document records what their charge-control layer does that ours does not,
which of those ideas actually fit KD Helios, and which ones we should
deliberately *not* copy. It is an input to the roadmap, not a roadmap change.

---

## 1. Two products, one shared decision layer

They are not the same product, and the difference matters for what is worth
copying.

| | Omnibattery | KD Helios |
|---|---|---|
| Core job | Keep grid exchange at zero and move energy by tariff | Price energy truthfully, then plan when to buy |
| Control style | **Closed loop**: PD controller on a live grid sensor, 1–2 s cadence | **Open loop**: write six TOU slots, let the inverter run them |
| Hardware | 8 battery brands behind its own drivers (Modbus/MQTT/HTTP) | One inverter (Deye) through the Solarman integration |
| Price model | Consumer tariff per provider; arbitrage + predictive charging | All-in provider price, reimbursement, ERE, saldering |
| Selling point | "Your battery does the right thing every second" | "Your energy costs what it actually costs" |
| Size | 87.8k LOC in `custom_components/`, ~3.4k test functions | 8.0k LOC, 254 tests |
| Runtime deps | `pymodbus` only | none (Solarman is an HA integration) |

The overlap is the **decision layer**: when is buying energy worth it, and what
stops us. That is where the transferable material is. Their real-time control
loop is a different bet from ours and mostly does not apply (see §6).

Scale is a signal by itself. They have been in the field long enough that three
thousand tests and a changelog full of specific issue numbers encode edge cases
we have not met yet. Reading that changelog is the cheapest way to inherit them
(§7).

---

## 2. How their control cycle is built

Five layers, in order of increasing specificity. The layering is the lesson.

### 2.1 A semantic driver contract, not a register map

`drivers/base.py` exposes exactly two hardware operations: *read telemetry* and
*deliver a signed net power* (+charge / −discharge). No register address or MQTT
topic appears in the contract, so a Modbus battery and an MQTT battery sit behind
the same interface.

Brand differences are declared once, as data, in `DriverCapabilities`: whether the
hardware enforces its own SOC cut-offs, whether it has a force mode, whether
telemetry is pushed or polled, the power envelope, the minimum reliable operating
power, the actuation and readback latency, and whether a setpoint readback can be
trusted. Callers branch on capabilities rather than on model strings.

For us this is over-engineering: we have one inverter and Solarman already owns
the transport. But the *shape* of the idea — describe what the device can do as
data, and keep protocol detail out of the decision code — is already how our
`TouProgram`/`WorkMode` enums work, and it is worth keeping that way.

### 2.2 Effective power is a chain of caps that can only lower

`_battery_power_limit` (`__init__.py:3090`) composes the charge ceiling:

```
base envelope
  → driver dynamic limit   (PV headroom, BMS taper, "cannot accept charge now")
  → top-of-charge taper    (200 W above 3.48 V cell)
  → temperature derate     (linear 40→50 °C, floor 40 %)
  → time-slot ceiling      (per-slot power override)
```

Every stage obeys the same contract: **take a limit, return a limit that is never
higher**, and **pass it through unchanged when its input is missing**. A sensor
gap cannot throttle charging; a hot battery cannot raise a slot ceiling.

That is a better shape than our single `charge_power_limit()` in `battery.py`,
which today hard-codes two bounds (EV priority, import cap) in one function. A
list of monotone caps would let us add fuse headroom, SOC headroom taper,
temperature and per-slot ceilings without touching callers or rewriting tests.

### 2.3 Every decision is attributable: the blocker registry

This is the single most transferable idea in the repo.

They keep four registries — global/per-battery × charge/discharge — of reasons a
direction is currently blocked (`__init__.py:3712`–`3841`):

```python
self.set_charge_block("charge_delay_setpoint", "waiting_for_solar", {"soc": 41})
self.set_charge_block("temperature", "derated", {"temp_c": 47, "factor": 0.3})
self.remove_charge_block("price_discharge")
```

Each record carries a `source`, a machine-readable `reason`, free-form `details`,
and a `since` timestamp preserved across updates. Subsystems register and clear
their own blocks; nothing computes "the reason" centrally. The result is exposed
as sensor attributes (`charge_blockers`, `battery_charge_blockers`), in
diagnostics, and in a compact one-line log format.

Two details make it work:

- **Sources are namespaced per subsystem**, so two blockers can coexist without
  overwriting each other, and the user sees *all* of them, not the first.
- **The registry is the only place the question "why not?" is answered.** The
  control loop asks `is_charge_blocked(...)`, the sensor reads the registry; no
  second, drifting explanation exists.

We have the seed of this: `ChargePlan.reason` with `"full"`, `"no_prices"`,
`"no_spread"`, `"no_soc"`. It is a single string, it only covers "no windows",
and it is overwritten every tick. Our roadmap's M5 goal — *"you can answer 'why is
the battery doing this?' from the panel alone"* — is exactly what this registry
delivers, and it is much cheaper to build in from the start than to retrofit.

### 2.4 Writes are confirmed, skipped when redundant, and tracked when ignored

`_set_battery_power` (`__init__.py:7330`) is worth reading in full. Its guards:

1. **Skip if unchanged** — compare the command against what the device reports
   (`net_power_from_data`) and do not write when it already matches. Protects a
   slow bus from being hammered by a plan that recomputes every second.
2. **Read back and ACK** — a write is not success until the device echoes it.
   `SetpointResult` carries `confirmed`, `exact`, and a `failure_reason`, so
   "accepted" and "applied" are distinguishable.
3. **Latency-aware expectations** — per-driver `actuator_latency_s`,
   `readback_latency_s`, `engage_grace_s`, and a `setpoint_confirm_reliable` flag
   for devices whose report lags the write. Confirmation windows are per device,
   not a global constant.
4. **Non-delivery detection** — `NonResponsiveTracker` excludes a battery that
   accepts commands but delivers nothing after 3 cycles, retries it after a flat
   5-minute cooldown, and raises a **Repair** issue if it stays dead for 30
   minutes. The cooldown never grows: recovering fast matters more than punishing
   a flaky unit.
5. **Manual ownership is checked before every automatic write**, and a battery
   the user took over is never silently reclaimed.

Our equivalent is `should_apply()` in `battery.py`, which is a good start on (1)
but has nothing for (2)–(5). We call the Solarman service `blocking=True` and log
what we sent. We never read back, we have no repair issue, and a partial write
(times written, power register fails) leaves the inverter running a combination
that was never planned. That is the most concrete reliability gap this review
found.

### 2.5 Time slots: validated at configure time, not patched at runtime

Up to 8 slots, per battery, with independent *allow charge* / *allow discharge*
ticks, optional SOC and power overrides, and a manual mode that forces one exact
power. Rules enforced in the config flow:

- the end must be later than the start on the same day (no midnight crossing),
- two slots for the same battery scope may not overlap (`_slots_overlap`,
  `config_flow.py:999`),
- a manual slot needs a power override and exactly one direction.

Because overlaps are rejected at entry, the planner never has to decide which of
two contradictory slots wins. We take the opposite approach — we merge windows
and drop whatever does not fit in six programs (`TouPlan.truncated`) — which is
correct for a six-register device but means a truncated plan is a normal,
silent outcome. Their model suggests we should at least *name* what was dropped
in the same place we explain everything else.

Note also their 1.6.1 change: **no charging and discharging inside the same
window**. Energy bought and spent inside one slot is a no-op that still costs
round-trip losses. Our `_apply_hold` already refuses to rewrite GRID slots inside
the car's window, but the general case (a GRID window overlapping a hold or a
discharge window) is not explicitly decided anywhere.

---

## 3. Solar-aware charge delay — the best policy idea in the repo

`control/charge_delay.py:489` (`_should_delay_charge`). The rule is one line:

```text
net_solar_for_battery = remaining_solar − remaining_consumption
release charging only when net_solar < energy_needed × 1.3
```

The value is in the failure semantics around that line, all of which come from
real incidents:

- **Fail-open, but not fail-fast.** No forecast sensor configured → unlock
  immediately (deliberate fail-safe). A *configured* sensor reading
  unavailable/unknown → hold the delay for a 5-minute grace period, then unlock.
- **A transient unlock must never latch.** Some unlock reasons
  (`no_forecast`, `zero_forecast`) are marked transient and stay re-evaluable;
  only a genuine forecast decision latches "charging allowed" for the rest of
  the local day. Without this, one sensor blip at midnight silently disabled the
  feature until tomorrow.
- **A provisional 0 kWh is not "no sun".** Providers publish the new day late;
  a scalar zero before 01:00 local is held as "not published yet" rather than
  treated as an empty sky (#457).
- **A deadband (0.5 kWh) on the grid-needed decision**, so a near-balanced day
  does not flip every cycle.
- **A 30 % cushion** on the energy comparison, i.e. optimistic forecasts are
  assumed, not trusted.
- **Re-evaluate only when the forecast moves** by more than 0.05 kWh or its
  source changes — the equivalent of our `should_apply` cost control.
- **An optional SOC setpoint before the delay**: let the battery reach 50 %
  first, then wait for the sun. Re-arms only after SOC falls 3 points below it.

Their docs publish the state machine verbatim (`Disabled`, `Charging to
setpoint`, `Waiting for forecast`, `Charging allowed`, `Delayed`), which is the
blocker-registry idea applied to a single feature.

We have no solar forecast at all today, so the *policy* is out of reach until M2.
The *shape* is not: a hold decision with named states, an explicit
transient/decided distinction, and fail-open behaviour is exactly what our EV
window hold and M3 planner will need. Worth designing in before the forecast
arrives rather than after.

---

## 4. Battery health stewardship — a gap in our roadmap

Omnibattery treats the battery as hardware with a maintenance schedule. We treat
it as a tank with a state of charge. Three features, all LFP-specific:

### 4.1 Weekly full charge

Once a week (configurable day), temporarily raise the target to 100 %, charge,
confirm completion, then restore the original limit. Completion is deliberately
strict: not "one reading above 3.60 V", but SOC 100 % *or* a confirmed BMS cutoff
(command to charge, delivered power ≤ 10 W, inverter in Standby for 5 consecutive
control cycles, and at least 10 s since the first refusal sample). The debounce
exists because a single sample of "not charging" is also what a bus glitch looks
like.

Why it matters: LFP cells only balance near the top of the knee, and a battery
held at 80 % never gets there. This is the difference between a battery that
still has its capacity in five years and one that drifts.

### 4.2 Top-of-charge taper

Above a cell voltage of 3.48 V, drop the charge ceiling to a flat 200 W and keep
it there; exit only below 3.44 V (hysteresis). Above 3.60 V the battery is
considered full and charging pauses; if SOC still reads below 99 % at that
voltage, they allow **one** further 200 W attempt after the cell relaxes to
3.57 V, as a best-effort BMS coulomb-counter recalibration.

This is the physically correct way to finish a charge. Our planner's target SOC
is a number; it has no idea that the last 2 % must be taken slowly.

### 4.3 Cell balance monitor

Measures max−min cell voltage **after** a full charge and a 60 s rest, stores up
to 52 weekly readings, and reports deltas as `green` (<200 mV), `yellow`
(200–229), `orange` (230–249), `red` (≥250). The docs explain why the reading is
only meaningful at the top (the LFP plateau is ~1 mV per % SOC, so mid-charge
deltas are noise), and they are careful to say one red reading is not a diagnosis
— the trend is.

Feasibility for us: the Deye register map in `.research/solarman/` exposes
**Battery Temperature**, SOC, voltage, current and a battery status word — but no
per-cell voltages. So §4.1 and §4.2 are implementable through the registers we
already write; §4.3 depends on what the battery's BMS publishes to HA (often it
does, via a separate integration). The monitor should be optional on
availability, never a hard dependency.

---

## 5. Smaller ideas worth keeping on the list

| Idea | Where | Why it fits us |
|---|---|---|
| **Temperature derate** | `control/temperature_limit.py` | Deye exposes battery temperature. Linear ramp 40→50 °C, floor 40 %, no latch, fail-open on missing data. ~60 lines, one test file. |
| **Charge ordering by conversion loss** | `control/charge_order.py` | On scarce sun, fill the DC-coupled battery first because fewer kWh are lost; latch the "scarce" verdict with hysteresis so a wandering forecast does not reorder the fleet every cycle. Directly relevant once the car and the battery compete for one fuse. |
| **Plan-invalidation fingerprint** | `__init__.py:3134` | A tuple of every *setting* the plan reads. `test_replan_inputs.py` sorts every setting into "re-plan on change" or "read live", so a moved knob can never leave a stale schedule in force. We have `should_apply` (plan vs applied) but no equivalent for "the inputs moved". |
| **Repairs integration** | `tracking/non_responsive_tracker.py` | A dead battery raised only a log line for a day; now it raises a Repair. We have `ISSUE_NO_DEVICES` already, so the pattern is established. |
| **Notify with the reason** | `pricing/engine.py` | Their notifications say *why* ("waiting for prices", "cheaper to import"), not just what. Cheap, and it is the same blocker data. |
| **Fail-safe degradation ladder** | throughout | Every optional input has a defined behaviour when missing, and it is documented next to the code. Sensor gap → passthrough, not stop. |

---

## 6. What we should not copy

- **The PD control loop.** It is the heart of their product and it needs a live
  grid sensor and a 1–2 s actuation path. Our inverter is reached through
  Solarman's service calls with a 0.3 s pause between register blocks; a loop at
  that latency would ring. Our open-loop TOU plan is the right control strategy
  for this hardware — they also offer a Time Slot mode for exactly that reason.
- **Their file layout.** A 12.6k-line `__init__.py` containing a 1.8k-line
  `_run_control_cycle`, and manager classes that read and write the controller's
  private attributes by reference. Their own docstrings call this
  out as debt repeatedly ("the runtime *state* stays on the controller by
  reference"). Our invariant 1 (the core is pure) is better; do not import their
  coupling.
- **Multi-brand drivers.** Out of scope per our §1. We consume entities and one
  inverter.
- **8 slots, per-battery.** We have six hardware programs. Their slot model solves
  a problem we do not have.
- **Their pricing engine.** 8.3k lines, 7 price providers, quarter-hour
  resolution, export bonuses. Ours is already more honest about reimbursement,
  ERE and saldering — this is the one area where we are ahead.

---

## 7. Field evidence from the changelog

These are bugs a charge controller hits once it runs unattended, with issue
numbers. Several map directly onto gaps already listed in our `ROADMAP.md` §8:

| Their incident | Our exposure |
|---|---|
| Night charge came up short when the night was cheaper to import than the day (#662) | Our planner buys per-slot by spread; it does not book the house load *after* the window. Same class of bug. |
| Charging and discharging inside one window — energy bought and spent with only losses (#1.6.1) | `_apply_hold` handles the EV case only. |
| Predictive charging booked too few slots for batteries with unequal room | Not applicable today (one battery), will be when EV and battery share a fuse. |
| Horizon dropped after a failed forecast fetch (#817) | `prices.py` carries prices forward 36 h; a fetch failure is not yet distinguishable from an empty market in the plan's `reason`. |
| Overfilled a battery with a low discharge limit | Our planner caps charge by `max_charge_kw` and capacity, not by what the inverter can later discharge. |
| A 20× solar/profile speedup, because a re-plan froze HA for ~6 s on arm64 | Our invariant 2 (blocking work off the loop) already covers the mechanism; the lesson is that the *planner* itself became the blocking work. Our benchmark (0.3 ms/plan) is the right guard — keep it. |
| Re-plan after any setting it reads, once settings are stable 3 s | Debounced invalidation; see §5. |

---

## 8. What was taken, and what was left

Everything below was implemented in the same increment as this review, so the
document doubles as the record of what changed and where.

| Idea | Status | Where |
|---|---|---|
| Blocker registry with sources, details and `since` | **Done** | `decisions.py`, `battery.py`, `coordinator.py`, `sensor.battery_status`, panel **Why** list |
| Charge ceiling as a chain of monotone caps | **Done** | `caps.py`, `battery.charge_power_caps()`, `coordinator._async_update_power_cap()` |
| Verified writes, retry, Repair on mismatch | **Done** | `inverter.async_write_tou_verified()`, `coordinator._write_failed()` |
| "In force but not delivering" detection + Repair | **Done** | `coordinator._check_not_delivering()` |
| Thermal charge derate | **Done** | `caps.TemperatureCap`, opt-in, needs a temperature entity |
| Weekly full charge | **Done** | `maintenance.WeeklyFullCharge`, day-of-week, target raised and restored |
| Solar-aware charge hold | **Done, as a credit** | `forecast.solar_credit()` — buys the shortfall rather than holding outright |
| Plan invalidation when a setting moves | **Done** | `battery.settings_fingerprint()`, `should_apply(force=...)` |
| Stale prices named, not silently reused | **Done** | `PriceCoordinator.stale` / `last_error`, `plan.prices_stale` |
| Drain-aware valuation (the "night charge came up short" incident) | **Done** | `ChargeWindow.drain_kwh`, `_expected_drain()` |
| No buying and spending in one window | **Done** | `plan_charge(suppressed=...)`, `hold_conflict` blocker |
| Overfill versus a slow discharge | **Done** | `max_discharge_kw`, per-peak budget in `plan_charge()` |
| Cell-balance monitor (max/min cell voltage, 52-week history) | **Not taken** | No per-cell registers on the Deye; the BMS does not publish them through Solarman. Needs a separate BMS integration before it can mean anything. |
| Top-of-charge taper (200 W above 3.48 V/cell) | **Not taken** | Same reason: we cannot see a cell voltage, and a battery-level voltage would be a guess. |

Two deliberate differences from their implementation:

- **The solar hold is a credit, not a suppress flag.** Omnibattery decides
  "hold or release"; we subtract the forecast surplus from what the plan buys,
  so a partial deficit still gets a (smaller) window in the cheapest hour. It
  needs a learned house profile: without one, nothing is credited and the state
  says `no_consumption_forecast`, because crediting sun against an unknown load
  is how a battery ends up empty at dusk.
- **No latch on the solar decision.** Their delay latches "released" for the rest
  of the day; ours is recomputed from the live forecast every tick with a
  deadband. There is no manual reset to remember, and a worsened forecast simply
  restores the purchase.

The remaining gap is hardware, not design: a cell-balance monitor needs cell
voltages, and the Deye map in `.research/solarman/` exposes only pack voltage,
current, SOC, temperature and a status word. If a BMS integration is added to
the same Home Assistant, the weekly full charge already creates the comparable
top-of-charge condition a monitor would need.
