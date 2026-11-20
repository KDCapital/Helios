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

### 2. Add a device

Open the KD Helios entry and choose **Add device**.

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
| **Import / export power entity** | Optional, feeds the live power sensors. |
| **Net metering (saldering)** | On by default while it is still legal. |
| **Feed-in deduction** | Deducted from the spot price for exported kWh. Prefilled, and you must confirm it. |
| **Supply costs / grid operator costs per month** | Your fixed costs. Both are required; enter 0 if you have none. |

Only one connection can exist per installation.

#### Meter recognition

Picking a meter is friendlier than hunting for four entity ids, so the device is
matched by the entity registry's integration platform and translation key
rather than by entity names. HomeWizard's P1 meter is recognised out of the box;
unknown meters fall back to a device-class and naming heuristic. Detection only
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
| `sensor.<grid>_grid_import_power` / `_grid_export_power` | Live power, when configured |
| `binary_sensor.<grid>_net_metering_active` | Whether saldering still applies |

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
.venv/bin/python -m pytest        # 148 tests
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
Coverage is 96%.

---

## Roadmap: from cost tracker to HEM platform

KD Helios is deliberately split into layers so the cost tracker can grow into
the heating/energy management platform without a rewrite:

- `prices.py` — tariff acquisition and a queryable price timeline
- `tariff.py` — pure money math, no Home Assistant imports
- `accounting.py` — energy integration and per-device accumulators
- `grid.py` — grid import/export and the annual saldering ledger
- `coordinator.py` — orchestration and polling
- `sensor.py` — presentation only

`sources.py` holds the single interpretation of units and unavailable states
shared by devices and the grid. The pure layers already answer the questions a
HEM needs ("what does this kWh
cost at this instant?", "what did this device cost over this window?"), and the
accumulators are plain serialisable state, so they can move to a database or an
external service later — behind the same interface, whether that ends up as a
Docker service or stays inside Home Assistant.
