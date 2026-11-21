# KD Helios — project plan

A whole-home energy management system that lives **inside Home Assistant**.
Home Assistant bridges the devices; this integration does the thinking: price
modelling, accounting, planning, control and optimisation.

This document is the plan we work from. It records the architecture we agreed
on, the milestones in order, what "done" means for each, and the risks that
decide whether we stay on this path.

---

## 1. Scope

**In scope**

- Energy accounting: import, export, fixed costs, per-device cost, EV
  reimbursement and ERE value, saldering.
- Prediction: house consumption profile, solar yield, price forecast.
- Planning and optimisation: battery, EV charging, deferrable consumers.
- Control: inverter time-of-use programs, EV charger schedule, consumer
  switching — all with dry-run, interlocks and an audit trail.
- Presentation: dashboard, ranges, history, explanations of *why* a decision was
  made.

**Out of scope for v1**

- Replacing Home Assistant as the device hub. We consume entities, we do not
  build Zigbee/Z-Wave drivers.
- Multi-tenant or cloud service. Single household.
- Selling energy commercially. Saldering arbitrage was evaluated and dropped
  (see the decision log).

## 2. Architectural invariants

These are the rules that keep the project maintainable. Later milestones must
honour them.

1. **The core is pure.** Price, tariff, planning and optimisation logic takes
   data in and returns a result, with no Home Assistant imports and no I/O.
   Everything in that layer is unit tested without a runtime.
2. **Blocking work goes to an executor.** Anything above ~50 ms runs through
   `hass.async_add_executor_job`, never inline on the event loop.
3. **Two gates for anything that writes.** A configuration permission plus an
   explicit action: `allow_writes` and the control switch for the inverter,
   `enabled` and `dry_run` for consumers.
4. **Fail safe, not fail active.** If the integration stops, devices keep their
   last commanded state. Inverter keeps its TOU plan; relays keep their state;
   the heat pump keeps its own thermostat.
5. **Every increment ends green.** Tests, lint and formatting pass before a
   commit. A commit explains *why*, not just what.
6. **Backwards compatible.** Existing subentries, entity ids and statistics keep
   working across upgrades. No renames that break history.
7. **Both languages.** Every user-facing string exists in English and Dutch,
   with `strings.json` identical to `translations/en.json`.

## 3. Where we are (v0.1, milestone M0)

Shipped and tested (235 tests):

- Provider catalogue, price fetching, all-in tariff model, feed-in deductions.
- Device accounting with source-change rebasing and implausibility guards.
- EV chargers with reimbursement (fixed/dynamic) and ERE value.
- Grid connection: import/export, saldering, fixed costs, daily and yearly views.
- Battery planning, TOU programs, automatic control with a switch, safe release.
- Connection capacity (phases/amps/margin) and the fuse-aware charge cap.
- The car's cheap window and the hold policy inside it.
- Dashboard with ranges, history from long-term statistics, chart hover.

Known gaps, tracked in §8.

## 4. Milestones

Estimates are in **working sessions**. A session is one focused increment: code,
tests, docs, green. One session ≈ 200–400 lines plus tests, which is the pace
this project has actually run at.

### M1 — Data foundation and benchmarks
*2–3 sessions*

**Why**: every prediction is only as good as the history behind it, and we have
none yet. Nothing else can be tuned until this exists.

- Store an hourly consumption profile per device and per house (rolling 90 days).
- Backfill strategy for days before the integration existed: meter counters,
  provider exports, or an explicit "history starts here" marker.
- Benchmark test: synthetic year of 15-minute prices and load, planner timing
  printed and asserted under a ceiling.
- Coverage check: which house loads are unmetered, surfaced as a warning.

**Done when**: the profile is persisted across restarts, the benchmark reports a
number on the target hardware, and an unmetered-load warning appears for a house
where the sum of devices is far below grid import.

### M2 — Prediction
*3–4 sessions*

**Why**: the optimiser needs to know what is coming, not just what is happening.

- Consumption forecast: hour-of-week profile with quantiles, day-ahead.
- Solar forecast: Solcast or Forecast.Solar entity, normalised to kWh per slot.
- Price forecast: next-day EPEX prices (we already fetch a forward series).
- Forecast quality report: predicted vs actual for the last 7 days, visible in
  diagnostics and on the panel.

**Done when**: a day-ahead curve exists for load, solar and price, and its error
is measurable rather than assumed.

### M3 — Optimiser v1
*4–5 sessions*

**Why**: this is the HEMS. Until now we pick cheap slots; from here we plan
against forecasts and constraints.

- Decision model: battery SOC, EV energy target, deferrable load run-hours.
- Objective: cost minimisation with terminal SOC value, not just immediate price.
- Constraints: fuse cap, inverter power, EV departure, consumer deadlines.
- Solver choice gate: DP vs LP. Start DP (no dependency), measure, escalate only
  if the numbers say so.
- Explainability: every plan carries *why* — the binding constraint and the
  alternative it rejected. Shown on the panel.

**Done when**: a plan over 48 h with battery, EV and one deferrable load is
produced in under a second, respects all constraints, and can explain a decision.

### M4 — Consumer control
*4–6 sessions*

**Why**: controlling consumers is where a HEMS earns its keep — and where it can
do damage.

- Capability model: `on_off`, `modulating`, `deferrable(hours, deadline)`,
  `thermal(min/max, inertia)`. Not "just a switch".
- Adapters: switch/light entities, climate entities, heat pump over Modbus.
- **Dry-run first**: decisions logged with intent, nothing actuated, compared
  against reality.
- Safety: interlocks, minimum run/rest times, manual override that wins, and an
  audit log of every actuation.

**Done when**: a deferrable load runs in the cheapest window, and turning off
dry-run changes only the actuation, not the decisions.

### M5 — Dashboard v2
*4–6 sessions*

- Settings surface for connection, battery, car and consumers (today some fields
  only exist in the config flow).
- Forecast charts: predicted vs actual, with the error visible.
- Plan timeline: what will happen and why, not just what is happening.
- Audit log view: every write, when, and the reason.
- Mobile pass: the panel is dense; it needs a small-screen layout.

**Done when**: you can answer "why is the battery doing this?" from the panel
alone, without reading logs.

### M6 — Reliability
*2–3 sessions*

- Watchdog: control loop heartbeat, alert when it stops.
- Fail-safe verification: kill the process mid-cycle, confirm devices keep state.
- Recovery: restart with a stale plan must not repeat or skip a cycle.
- Alerting: device unreachable, plan impossible, fuse cap exceeded.
- Backup/restore of accumulated totals.

**Done when**: pulling the plug in the middle of a charge cycle leaves the system
in a known state, and the alert tells you what happened.

### M7 — Accounting completeness
*2–3 sessions*

- Solar accounting: self-consumed vs exported vs battery-cycled.
- Savings baseline: what the bill would have been without any control, so
  "savings" is measured rather than claimed.
- Year-to-date report: cost, credit, fixed, reimbursement, ERE.

**Done when**: the dashboard can state a savings number and show the baseline it
was measured against.

### M8 — Packaging and release
*2–3 sessions*

- HACS release: versioning, changelog, upgrade path, validated manifest.
- Docs: install, configuration, troubleshooting, architecture.
- Optional compute sidecar (add-on) only if M3's measurements demand it.

**Done when**: a second household can install it from HACS and reach a working
dashboard without asking us anything.

## 5. Timeline

Assumes a steady pace of **2 sessions per week** and hardware testing running in
parallel with the next milestone's design.

| Milestone | Sessions | Calendar |
|---|---|---|
| M1 Data foundation | 2–3 | weeks 1–2 |
| M2 Prediction | 3–4 | weeks 2–4 |
| M3 Optimiser v1 | 4–5 | weeks 4–6 |
| M4 Consumer control | 4–6 | weeks 6–9 |
| M5 Dashboard v2 | 4–6 | weeks 9–12 |
| M6 Reliability | 2–3 | weeks 12–13 |
| M7 Accounting | 2–3 | weeks 13–15 |
| M8 Release | 2–3 | weeks 15–16 |

**Total: 23–33 sessions, roughly 16 weeks at two sessions a week.** At one
session a week, call it 8 months. The estimate is dominated by hardware
verification, which does not compress: a heat pump in January behaves nothing
like one in April, so M4's real validation takes a heating season regardless of
how fast the code lands.

Sessions are not the unit that slips; **the season is**. If M4 lands in spring,
budget one winter of observation before trusting it unattended.

## 6. Working method

- One milestone at a time, in order. No starting M4 while M3 is unproven.
- Each session: implement → tests → lint/format → docs → commit with rationale.
- Each milestone ends with a **hardware verification checklist** in this file,
  ticked off or explicitly deferred.
- Numbers beat opinions: benchmark before optimising, measure forecast error
  before tuning the model.
- Every write to a device ships behind dry-run first.

## 7. Decision gates

Points where we stop and re-decide rather than drift.

1. **After M2**: is the forecast error small enough to optimise against, or do we
   need more/better sensors first?
2. **After M3**: DP or LP? Measure the real plan time; escalate only if needed.
3. **After M4**: does the optimiser fit the event loop, or does it move to a
   sidecar container? The rule: if a plan exceeds ~1 s, it moves out.
4. **Before M8**: HACS-only, or an add-on as well? Only if there is a demonstrated
   need (compute, or a UI that outgrows ingress).

## 8. Known gaps and verification backlog

Things built but not yet proven on live hardware. Each must be ticked or fixed.

- [ ] Reactive charge cap writes register 154 to 0 while the car charges.
- [ ] Releasing control writes generator-only slots and the configured work mode.
- [ ] The hold slot inside the car's window actually keeps the car off the battery.
- [ ] EVCC entity mapping verified against a real ha-evcc install.
- [ ] Deye external CT used as the grid power source for the fuse cap.
- [ ] Inverter oscillation (±10 kW ringing) behaviour under a load step.
- [ ] Saldering edge cases: export beyond consumption, year boundary.
- [ ] Panel on mobile.

## 9. Decision log

- **Saldering arbitrage: built, then removed.** Research confirmed it is
  profitable through 2026, but a few weeks of extra cycles do not pay for battery
  health. The netting headroom stays tracked in the grid ledger if it is ever
  worth revisiting.
- **The car's window is scheduled, not inferred.** Reacting to measured car power
  caused exactly the behaviour we wanted to avoid; the window comes from the
  departure time and the energy the car needs.
- **EV priority is a policy setting, not a hard rule.** `EV first` is the default
  because a car can out-drain a battery in hours; `Battery first` remains
  possible.
- **Writes are always double-gated.** Config permission plus an explicit action.
- **Home Assistant stays the device bridge.** We do not rebuild device drivers.
- **The optimiser is a pure function.** It can move to another process without a
  rewrite when measurements say it should.
