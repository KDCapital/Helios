# KD Helios

**What your energy actually costs — including the devices that someone else pays for.**

KD Helios is a Home Assistant integration that prices the energy of individual
devices against the real, live tariff of your own Dutch energy provider. It
exists because the common "dynamic price × consumption" helpers break down the
moment money flows in more than one direction:

- an **EV charged at home but paid for by your employer** earns money back,
- **ERE certificates** add value to every kWh you charge,
- and a cheap night tariff can turn a device's net cost **negative**.

KD Helios tracks cost, reimbursement and certificate value separately, so the
net number is truthful instead of an average that hides what is going on.

Prices come from [epexprijzen.nl](https://epexprijzen.nl), which publishes
all-in consumer prices per provider, including VAT, energy tax and the provider
charge.

---

## Features

| | |
|---|---|
| **Real provider tariffs** | Pick your own provider from the 34 that epexprijzen.nl publishes, per quarter hour or per hour. |
| **Per-device metering** | Attach an existing power (W) or energy (kWh) entity to a device and get energy, cost, cost today and current price. |
| **Employer reimbursement** | A fixed amount per kWh, or the dynamic formula `(market price + fixed surcharge) × surcharge percentage`, settled per quarter hour or per hour independently of your own contract. |
| **ERE certificates** | A fixed `ERE per kWh × price per ERE`, or the ratio derived from the [Joulo integration](https://github.com/boronz/joulo-hacs-integratie). |
| **Honest net cost** | `net cost = cost − reimbursement − ERE value`, and it may go negative. |
| **Grid connection** | Import and export metered at the real tariff, with an annual **saldering** ledger and your fixed monthly costs. |
| **Battery charging** | Works out when charging a home battery is most profitable, and can program the six time-of-use slots of a Deye inverter through Solarman. |
| **Survives restarts** | Totals, integration baselines and daily counters are persisted. |
| **Exact price boundaries** | Energy consumed across a tariff change is split at the boundary, not priced at a single rate. |

---

## Installation

### HACS (recommended)

1. HACS → **Integrations** → ⋮ → **Custom repositories**.
2. Add `https://github.com/KDCapital/Helios`, category **Integration**.
3. Install **KD Helios** and restart Home Assistant.

### Manual

Copy `custom_components/kd_helios` into your Home Assistant `config/custom_components`
directory and restart.

Requires Home Assistant **2026.10** or newer (the integration uses config
subentries).

---

## Configuration

### 1. Add the integration

**Settings → Devices & services → Add integration → KD Helios.**

Choose your energy provider as listed on epexprijzen.nl, and whether your
contract settles **per quarter hour** (most dynamic contracts) or **per hour**.

The provider and interval can be changed later through **Reconfigure**; devices
and their totals are kept.

### 2. Add a device or an EV charger

Open the KD Helios entry and pick the entry point that matches what you are
metering:

- **Add device** (in Dutch: *Apparaat toevoegen*) — an appliance or a circuit.
  Name, power entity, energy entity. Done. It is **never** asked about employer
  reimbursement or ERE certificates, because neither applies to a heat pump.
- **Add EV charger** (in Dutch: *Laadpaal toevoegen*) — the same fields, then
  reimbursement and ERE in two more steps.

Both appear under the same **Add** menu on the integration page, and they are
separate subentry types (`device` and `ev_charger`), so the list of devices and
the edit forms stay short and specific.

#### Add device

| Field | Meaning |
|---|---|
| **Name** | Shown in Home Assistant, for example `EV charger`. |
| **Device type** | `Appliance or circuit`, or `EV charger` to unlock reimbursement and ERE. |
| **Power entity** | A sensor in W or kW. Integrated over time. |
| **Energy entity** | A cumulative sensor in kWh. Preferred, because it is exact. |

If both are attached, the energy entity is authoritative and the power entity
only feeds the live `Power` sensor. Mixing the two mid-flight would double
count, so KD Helios never switches between them — and pointing a device at a
different meter rebases the counters instead of billing the difference between
the old and the new reading.

#### Add EV charger

The following two sections are part of the charger workflow only.

### 3. Employer reimbursement (EV chargers)

Pick how your employer pays for charging.

**Fixed** — a set amount per kWh:

```text
reimbursement = kWh × fixed rate
```

**Dynamic** — your formula, with the surcharge applied first and the percentage
as a multiplier:

```text
reimbursement_per_kwh = (market price + fixed surcharge) × (percentage / 100)
```

With `percentage = 100` this is simply `market price + surcharge`. Use `105` to
add a five percent margin. Amounts accept four decimal places.

The **market price** can be either:

- **EPEX spot** — the raw day-ahead price, excluding VAT and energy tax. This is
  what a company tariff is usually based on.
- **All-in price** — what you pay at home, including VAT and energy tax. Use
  this if your employer reimburses you at the same rate you pay.

**Interval** — the reimbursement has its own price interval, independent of your
contract: **per quarter hour** (default) or **per hour**. Dynamic tariffs do not
always line up: an employer or charging platform may settle hourly while your
own contract settles per quarter. An hourly reimbursement must use the hour
average, not the quarter hour price at the moment the car happened to charge, so
set it to *per hour* and the four quarters of each hour are averaged for the
reimbursement while your cost keeps following the contract.

### 4. ERE certificates

**Fixed** — `ERE per kWh × price per ERE`, for example `0.25 × €4.00 = €1.00/kWh`.

**Joulo** — derive the ratio from the cumulative counters of the
[Joulo integration](https://github.com/boronz/joulo-hacs-integratie):

| Field | Entity |
|---|---|
| Joulo ERE credits | `sensor.joulo_total_ere_credits` |
| Joulo total kWh | `sensor.joulo_total_all_kwh` |
| Live ERE price (optional) | `sensor.joulo_actuele_ere_marktprijs` |

The ratio is `ΔERE / ΔkWh` between two readings of those counters. It is only
trusted after 1 kWh has passed, and the configured fixed ratio is used as a
fallback until then. Because the ratio is account wide, treating it as per
device is correct as long as the device *is* the charger those EREs come from.

### 5. Add the grid connection

Devices tell you what *they* cost; the grid connection tells you what you
actually **pay the energy company**. Add it once, from the same **Add device**
menu (**Grid connection** / *Netwerkaansluiting*). It is three short steps:

1. **Meter** — pick your meter device, for example a **HomeWizard Wi-Fi P1
   meter**. KD Helios recognises the meter and looks up its counters. You may
   also skip this and choose the counters yourself.
2. **Meters** — the counters it found are filled in for you to check and
   correct. For a HomeWizard P1 meter that is the combined *Energy import* and
   *Energy export* sensor, not the per-tariff T1/T2 registers.
3. **Feed-in and fixed costs** — confirm the feed-in deduction and enter your
   monthly fixed costs.

| Field | Meaning |
|---|---|
| **Meter device** | Optional. Detection is best effort; you always confirm the result on the next screen. |
| **Import energy entity** | Cumulative kWh taken from the grid. Required. |
| **Export energy entity** | Cumulative kWh returned. Optional. |
| **Import / export power entity** | Optional separate power sensors, in W. |
| **Signed grid power entity** | Optional single sensor where positive imports and negative exports, as a P1 meter reports it. |
| **Net metering (saldering)** | On by default while it is still legal. |
| **Feed-in deduction** | Deducted from the spot price for exported kWh. Prefilled, and you must confirm it. |
| **Supply costs / grid operator costs per month** | Your fixed costs. Both are required; enter 0 if you have none. |

Only one connection can exist per installation.

#### Meter recognition

Picking a meter is friendlier than hunting for four entity ids, so the device is
matched by the entity registry's integration platform and translation key
rather than by entity names. HomeWizard's P1 meter is recognised out of the box;
unknown meters fall back to a device-class and naming heuristic.

Detection is checked against the integration's own published metadata by
`tests/test_homewizard_alignment.py`, which parses HomeWizard's sensor
descriptions and fails if a counter is renamed, disabled by default, or stops
being cumulative kWh. It also refuses to guess: if the combined counter is
unavailable it suggests **nothing** rather than falling back to a T1/T2 register
that would meter only part of the flow.

A P1 meter reports live power as **one signed value**: positive when importing,
negative when exporting. That sensor has no translation key on HomeWizard, so it
is recognised as the only power sensor without one. Selecting it fills a single
*signed* power field, from which the import and export power sensors are derived
(and a net power sensor shows the signed value). Choosing the same sensor in
both the import and the export field is treated the same way. A negative reading
on a dedicated import or export sensor is clamped away as a sign error. Detection only
ever *suggests* values — guessing wrong would silently meter the wrong counter,
so the next step always shows what was found and what will be used.

#### Saldering

While net metering applies, exported kWh are **not** paid at the feed-in price:
they are netted 1:1 against the kWh you import, so an exported kWh is worth the
*import tariff* of that moment. That is why the toggle exists and why it is on
by default.

`saldering` legally ends on **1 January 2027**. From that instant the integration
ignores the toggle and settles export at the feed-in price, whatever the stored
preference says — `binary_sensor.…_net_metering_active` shows which mode is live
and carries `saldering_ends_on`.

Netting is capped by what you import: if you export more than you consume in a
year, the surplus is valued at the feed-in price instead. Both valuations are
accumulated and mixed by volume, so the year total stays truthful.

#### The feed-in deduction is confirmed, not assumed

epexprijzen.nl publishes feed-in data in the provider dataset of its web
application but **not** through its JSON API — `/api/v1/providers` only carries
ids and names, and the price endpoints carry import prices only. So the
integration ships a dated snapshot of that dataset
([feedin.py](custom_components/kd_helios/feedin.py)) to *suggest* a value.

Because that snapshot can drift, the flow requires you to confirm the amount,
and you can always overwrite it. The stored value records whether it came from
epexprijzen or from you:

| Provider examples | Deduction |
|---|---|
| Zonneplan | −€0.02000 (they pay a bonus) |
| Frank Energie Slim | −€0.01815 |
| VandeBron | +€0.01361 |
| Essent | +€0.02528 |

A negative deduction is a bonus on top of the spot price, which is exactly how
Zonneplan returns its purchase fee.

#### Fixed costs

`supply_cost_monthly` and `grid_fee_monthly` are accrued per second and spread
over the days of the month, so the yearly total grows smoothly instead of
jumping on the first of the month. They are **not** prefilled from provider
data: only you know your contract.

### 6. Add the home battery (optional)

Tells you **when** to buy energy for the battery, using the same tariff the rest
of the integration already tracks: charge in the cheap hours, and let the
battery discharge into the expensive ones.

| Field | Meaning |
|---|---|
| **Battery SOC sensor** | State of charge, for example the Solarman battery SOC sensor. |
| **Battery capacity / max charge power** | Real kWh and kW, used to spread a window over several slots. |
| **Reserve / target SOC** | Never discharge below the reserve; never buy above the target. |
| **Round-trip efficiency** | A stored kWh returns this fraction, so a spread has to beat the losses. |
| **Planning horizon** | How far ahead to look for a cheaper hour to charge from and a dearer one to discharge into. |
| **Inverter device** | The Solarman inverter, needed before writes can be enabled. |
| **Allow writing to the inverter** | Off by default. |

`Planned charge`, `Planned saving`, `Applied plan saving` and
`Next charge start` appear on the battery device.

#### Proposed versus applied

The plan is recomputed on every tick, so it moves as prices and the state of
charge change. That is the **proposal**. The moment you write it, the exact
plan and the programs derived from it are stored as the **applied plan**:

- `Applied plan saving` is the saving the plan in force was predicted to
  deliver, with `applied_at`, the state of charge at the time, the windows and
  the six programs as attributes.
- `Planned charge` reports whether the proposal still matches what is in force
  (`applied_matches_proposal`), so drift is visible instead of silent.
- Both survive a restart, and both appear in diagnostics.

Without that snapshot, the number that justified writing to the inverter would
be overwritten a minute later and you could never check what it was.

#### Reading the market, not just the price list

Each candidate quarter hour is valued against the **highest price that comes
after it** within the horizon, times the round-trip efficiency. Slots whose
spread does not cover the losses are not bought at all — with a flat tariff the
plan is simply "do nothing", which is the honest answer.

Six slots is a hard limit of the inverter, so at most three windows survive
(each needs an idle slot after it). If the plan wants more, it says so through
`tou_truncated` instead of quietly dropping the difference.

#### Deye time-of-use mapping

The six programs are written to the holding registers documented in Deye's
three-phase energy-storage protocol for the **SG04LP3/SG05LP3 family**:

| Block | Registers | Value |
|---|---|---|
| TOU Time 1–6 | 148–153 | start, `HHMM` |
| TOU Power 1–6 | 154–159 | watts |
| TOU SOC 1–6 | 166–171 | percent |
| TOU Charge Source 1–6 | 172–177 | 0 off, 1 grid, 2 generator, 3 both |

A charging slot starts at the window start with source **grid** and the target
SOC. It is followed by an idle slot at the window's end with the reserve SOC,
which is what lets the battery discharge into the expensive hours.

#### Two modes

| Mode | Name | What it does |
|---|---|---|
| `self_consumption` | **Self-consumption** / *Eigen verbruik* | Charges in the cheap hours and keeps that energy for the house. Work mode: zero export to load. |
| `arbitrage` | **Saldering arbitrage** / *Salderingsarbitrage* | Buys in the cheapest hour, **holds** the charge, then returns it to the grid in the most expensive hour. Work mode: selling first. |

Arbitrage only makes sense while saldering still applies, so it is built around
three guards:

- **Netting headroom is a hard cap.** Export beyond your own annual consumption
  only earns the feed-in price, which turns a profit into a loss. The plan caps
  the trade at `imported − exported` for the current year, straight from the grid
  ledger, so it shrinks by itself as the year fills up.
- **The trade only runs with a positive spread** — `η × sell price − buy price` —
  and says "no spread" instead of cycling the battery for nothing.
- **It ends with saldering.** Nothing special is needed: from 1 January 2027 the
  feed-in price is far below import, so the spread disappears and the planner
  stops trading on its own.

The inverter encoding reflects the intent: charge slot at target SOC, an extra
**hold slot at target SOC** at the end of charging (so the energy is not leaked
into the house early), then a **sell slot at the reserve SOC** where the battery
discharges to the grid.

Because providers differ, whether saldering actually nets grid-charged battery
energy is a setting. Turn it off and arbitrage will not run — Zonneplan nets it
through 2026; check yours.

#### Automatic control, and releasing it safely

`switch.<battery>_inverter_control` is the runtime toggle. Turn it on and KD
Helios writes the current plan immediately and keeps it in sync from then on;
turn it off and it hands the inverter back.

Two things make the automatic mode safe:

- **It only rewrites when the schedule actually changed** — when the next charge
  window moves by more than five minutes, when the target SOC changes, or when
  there is suddenly nothing worth buying. It never rewrites because the window's
  *end* drifted while the battery charged, and never more than once every ten
  minutes, so the Modbus link is not hammered.
- **Releasing control never leaves grid charging behind.** All six slots are
  rewritten with the **generator** as their only source, so the inverter cannot
  silently keep buying power, and the work mode is written back to
  self-consumption. Which work mode that is, is a setting: *Selling first*,
  *Zero export to load* (default) or *Zero export to CT*.

The switch is only available when writes were enabled in the battery settings
and an inverter device is selected — the config permission is the master gate,
the switch is the runtime one.

#### Why writing needs two confirmations

Solarman is the right tool for *reading* this inverter, but two limits matter:

- its Deye definitions do not expose the time-of-use block as entities, so there
  is nothing to bind to, and
- its entities are read-only.

It does expose generic register services, so KD Helios writes through
`solarman.write_multiple_holding_registers`, in four blocks with a short pause
between them. Because that changes how a real battery behaves, the action
refuses to run unless writes are enabled in the battery settings **and** the
call passes `confirm: true`:

```yaml
action: kd_helios.apply_tou_programs
data:
  entry_id: 01ARZ3NDEKTSV4RRFFQ69G5FAV
  confirm: true
```

It returns the programs and registers it wrote, so the result is auditable
rather than a fire-and-forget write.

---

## Sidebar panel

KD Helios adds a **Helios** entry to the sidebar, registered the same way other
integrations add a panel (`panel_custom` plus a static module). It is one plain
custom element — no build step, no external imports — served from
`/kd_helios/kd-helios-panel.js`, so it works offline and survives Home Assistant
upgrades.

The layout follows the Helios dashboard: a greeting and live tariff in the
header, four KPI tiles, a price chart with the planned charging windows, then
cards for the EV charger, the device breakdown, the battery and the tariff.

### What is real, and what is deliberately absent

The dashboard shows **only values the integration actually has**. Three things
from the original design are not there, and are not faked:

| Missing | Why |
|---|---|
| Solar generation | KD Helios has no PV entity in its model; it tracks consumption, grid and battery. |
| Car state of charge | That needs the vehicle integration, not the charger. |
| "vs yesterday" trends | The ledger stores the year and today, not a history, so a trend would be invented. |

Instead, every tile carries real context: the grid cost shows the year to date,
the battery tile says whether the saving is from the *applied* plan or only a
proposal, the EV tile shows what has been reimbursed, and the grid balance
splits import from export.

### Today

The grid ledger now keeps today's import, export, credit and fixed costs
alongside the yearly totals, and devices already tracked their own daily
energy and cost. That is what the KPI tiles and the device breakdown read, so
"today" means today and not "since installation".

### How it is built

Everything comes from one WebSocket command, `kd_helios/dashboard`, built by
`build_dashboard()` — the same function the command calls, so the payload is
tested without a browser. A Node harness renders the element against the real
payload and asserts the sections appear, which catches a broken render path that
Python tests cannot reach.

The panel refreshes every minute, needs the frontend (it registers on setup
when the frontend is loaded, and otherwise waits for startup), and its only
action — writing the programs — asks for confirmation first.

---

## Worked example

A home EV charger on Zonneplan, midday on 20 November 2026, 10 kWh charged.

| Component | Calculation | Result |
|---|---|---|
| All-in home tariff | from epexprijzen.nl | €0.381683 /kWh |
| EPEX spot price | from epexprijzen.nl | €0.2073 /kWh |
| Reimbursement (dynamic, spot, +€0.025, 110%) | (0.2073 + 0.025) × 1.1 | €0.25553 /kWh |
| ERE (0.25 ERE/kWh at €4.00) | 0.25 × 4.00 | €1.00 /kWh |

For 10 kWh:

```text
cost          = 10 × 0.381683 = €3.81683
reimbursement = 10 × 0.25553  = €2.5553
ERE value     = 10 × 1.00     = €10.00
net cost      = 3.81683 − 2.5553 − 10.00 = −€8.73847   (you come out ahead)
```

A plain cost tracker would have reported €3.82 and missed the entire story.

---

## Entities

### Tariff (updated every minute from cached prices)

| Entity | Description |
|---|---|
| `sensor.kd_helios_current_price` | All-in price now, EUR/kWh |
| `sensor.kd_helios_next_price` | All-in price for the next slot |
| `sensor.kd_helios_epex_spot_price` | Raw EPEX spot, excluding VAT and energy tax |
| `sensor.kd_helios_average_price_today` | Time weighted average for the local day |
| `sensor.kd_helios_energy_tax` | Energy tax component (diagnostic) |
| `sensor.kd_helios_provider_charge` | Provider charge component (diagnostic) |

### Per device

| Entity | Description |
|---|---|
| `sensor.<device>_energy` | Metered energy, kWh |
| `sensor.<device>_cost` | Total cost, € |
| `sensor.<device>_cost_today` | Cost since local midnight, € |
| `sensor.<device>_current_price` | Tariff applying right now |
| `sensor.<device>_power` | Live power, when a power entity is attached |
| `sensor.<device>_reimbursement` | Total reimbursement, when configured |
| `sensor.<device>_reimbursement_rate` | Current reimbursement per kWh |
| `sensor.<device>_ere_value` | Total ERE value, when configured |
| `sensor.<device>_ere_credits` | Metered ERE credits |
| `sensor.<device>_net_cost` | `cost − reimbursement − ERE value`, when either applies |

### Grid connection (year to date)

| Entity | Description |
|---|---|
| `sensor.<grid>_grid_import_energy` | kWh imported this year |
| `sensor.<grid>_grid_export_energy` | kWh exported this year |
| `sensor.<grid>_grid_import_cost` | What the imported kWh cost |
| `sensor.<grid>_grid_export_credit` | What the exported kWh earned back |
| `sensor.<grid>_fixed_costs` | Accrued supply and grid operator costs |
| `sensor.<grid>_grid_cost` | `import cost − export credit + fixed costs` |
| `sensor.<grid>_feed_in_price` | Feed-in price right now |
| `sensor.<grid>_grid_import_power` / `_grid_export_power` | Live power, split by direction |
| `sensor.<grid>_grid_net_power` | Signed net power, when a signed sensor is configured |
| `binary_sensor.<grid>_net_metering_active` | Whether saldering still applies |

### Home battery

| Entity | Description |
|---|---|
| `sensor.<battery>_planned_charge` | kWh the plan wants to buy, with the windows and programs as attributes |
| `sensor.<battery>_planned_saving` | Expected profit of the proposal, in euros |
| `sensor.<battery>_applied_saving` | Expected profit of the plan actually written, with its windows and programs |
| `sensor.<battery>_next_charge_start` | When the next planned window begins |
| `switch.<battery>_inverter_control` | Enables or disables automatic control; releasing restores self-consumption and generator-only charging |

The `grid_cost` sensor exposes `billing_year`, `saldering_active`,
`netted_kwh`, `open_export_kwh`, `export_deduction`, `fixed_monthly` and the
accounting health counters as attributes.

The `cost` sensor also exposes `source_entity`, `source_available`,
`unpriced_energy_kwh`, `untracked_gaps` and `rejected_deltas` as attributes, so
accounting health is visible without adding diagnostic entities.

---

## How it works

**Prices.** The tariff is re-fetched every ten minutes and the sensors are
recomputed every minute from the cached series, so `current price` flips at the
quarter hour boundary without an extra API call. Two endpoints are used. The provider endpoint returns all-in prices
with `energy_tax`, `provider_charge` and `spot_coefficient`; the spot endpoint
returns raw day-ahead prices at quarter hour resolution. Both are normalised
onto a single 15 minute UTC grid, so an hourly contract still resolves the
quarter hour spot price used by your reimbursement formula.

Verified identity, which the test suite checks against every recorded price:

```text
all_in = spot × spot_coefficient × 1.21 + provider_charge + energy_tax
```

**Accounting.** Every minute each device is sampled. A cumulative energy meter
gives the delta directly; a power sensor is integrated trapezoidally over the
elapsed window. The resulting kWh are then split across the price boundaries
they actually spanned.

**Clock discipline.** The accounting clock only advances on a *successful*
reading. If a source entity goes unavailable, or no tariff is available, the
window stays open and the energy is priced over its true duration once both are
back. A window longer than 10 minutes is treated as downtime: Home Assistant
cannot know the prices that applied, so instead of inventing them the period is
dropped, logged and counted in `untracked_gaps`.

Two further guards keep the numbers honest. Changing a device's source entity
re-baselines instead of billing `new reading − old reading`. And any single
window implying more than 500 kW is rejected as a sensor glitch (a meter that
reported 0 while offline and then recovered, for example) and counted in
`rejected_deltas`.

Energy that was metered but had no tariff is not thrown away silently: it is
added to `unpriced_energy_kwh`, which is persisted and exposed on the `cost`
sensor.

**Grid connection.** Import and export are two independent counters with their
own baselines, gap handling and plausibility guards. Imported kWh are priced at
the all-in tariff; exported kWh are accrued **twice**, once at the import tariff
(for saldering) and once at the feed-in price (for after 2026). The active mode
picks the mix:

```text
ratio  = min(1, imported_kwh / exported_kwh)
credit = saldered_credit × ratio + feedin_credit × (1 − ratio)
total  = imported_cost − credit + fixed_cost
```

The ledger is per calendar year and resets on 1 January.

**Persistence.** Totals, the last source reading, the daily counter, the Joulo
baselines and the grid ledger are stored through Home Assistant's storage
helper, so a restart continues where it left off instead of resetting to zero.

---

## Known limitations

- **Downtime is not billed.** Energy consumed while Home Assistant was off is
  not priced, because the tariff that applied cannot be reconstructed.
- **Negative prices flow through every formula.** With dynamic reimbursement,
  `(market + surcharge) × percentage` can be negative, which means you may owe
  money for charging. That is the honest result; the fixed-rate mode is the way
  to avoid it.
- **The Joulo ratio is account wide.** It is only meaningful when the tracked
  device is the charger that earns those EREs.
- **epexprijzen.nl is an unofficial, free API.** If it fails, the last known
  prices are carried forward for up to 36 hours, then energy becomes
  "unpriced" rather than wrongly priced (and shows up in
  `unpriced_energy_kwh`).
- **Exporting devices are not netted.** A power-based device that exports
  (solar, V2G) never produces negative deltas, so its energy total only counts
  consumption. Use a metered energy entity for those. The grid connection does
  handle export, because it has a dedicated export counter.
- **Saldering prorates by volume.** When a year exports more than it imports,
  the split between netted and feed-in kWh is an average rather than an exact
  per-kWh match. It is exact whenever you import more than you export.
- **The feed-in deduction is a snapshot.** It comes from epexprijzen.nl's
  provider dataset, not from their API, so it is suggested and confirmed rather
  than fetched live. Re-check it when your contract changes.
- **Fixed costs are manual.** They are not read from any source; provider
  monthly fees are published only in signup flows.
- **Automatic control is coarse by design.** It reacts to the charge window
  moving, not to every price tick, and it waits ten minutes between writes. A
  plan that changes only in energy (the window shrinking as the battery fills)
  is deliberately not rewritten.
- **Dynamic contracts only.** Fixed and variable contracts are not modelled in
  this version, and gas is out of scope.
- **A reload during an API outage** (for example while adding a device) puts the
  entry in retry until epexprijzen.nl answers again, because prices are not
  cached across restarts. Home Assistant retries automatically.

---

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install pytest-homeassistant-custom-component ruff
.venv/bin/python -m pytest        # 221 tests
.venv/bin/ruff check custom_components tests
```

The test suite covers the tariff math, price normalisation against recorded
API payloads, the accounting engine (price boundaries, gaps, meter resets and
source swaps, implausible deltas, reimbursement and ERE), the HTTP client error
paths, both config flows, periodic price polling, persistence across a reload,
grid import/export with both saldering modes, the 2027 cut-over, hourly versus
quarterly reimbursement of the same energy, P1 meter recognition, both grid
flows end to end, and entity cleanup when a device loses a feature. There is
also a guard that no translation contains an unresolved ``%key`` reference.
The battery planner is covered separately: which hours it buys, how capacity and
charge power cap the plan, when it declines to cycle at all, and the exact
register payload written to the inverter, plus that the applied plan and its
predicted saving survive a restart. The panel payload is covered by rendering
`build_dashboard()` directly, which is the same data the browser receives. There
is also a test that a device-typed entry created before the split keeps its
reimbursement when edited through the short form, and that automatic control
writes once per real change, never on every tick, and that releasing it writes
generator-only slots plus the configured work mode. The panel is rendered in
Node against a real payload, and again for a tariff-only install, so a broken
render path fails the suite. Coverage is 94%.

---

## Roadmap: from cost tracker to HEM platform

KD Helios is deliberately split into layers so the cost tracker can grow into
the heating/energy management platform without a rewrite:

- `prices.py` — tariff acquisition and a queryable price timeline
- `tariff.py` — pure money math, no Home Assistant imports
- `accounting.py` — energy integration and per-device accumulators
- `grid.py` — grid import/export and the annual saldering ledger
- `battery.py` — the charge optimiser and the Deye time-of-use mapping
- `websocket.py` — the one payload the panel renders
- `frontend/kd-helios-panel.js` — the sidebar panel itself
- `coordinator.py` — orchestration and polling
- `sensor.py` — presentation only

`sources.py` holds the single interpretation of units and unavailable states
shared by devices and the grid. The pure layers already answer the questions a
HEM needs ("what does this kWh
cost at this instant?", "what did this device cost over this window?"), and the
accumulators are plain serialisable state, so they can move to a database or an
external service later — behind the same interface, whether that ends up as a
Docker service or stays inside Home Assistant.
