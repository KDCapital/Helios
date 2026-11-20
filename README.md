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
| **Employer reimbursement** | A fixed amount per kWh, or the dynamic formula `(market price + fixed surcharge) × surcharge percentage`. |
| **ERE certificates** | A fixed `ERE per kWh × price per ERE`, or the ratio derived from the [Joulo integration](https://github.com/boronz/joulo-hacs-integratie). |
| **Honest net cost** | `net cost = cost − reimbursement − ERE value`, and it may go negative. |
| **Survives restarts** | Totals, integration baselines and daily counters are persisted. |
| **Exact price boundaries** | Energy consumed across a tariff change is split at the boundary, not priced at a single rate. |

---

## Installation

### HACS (recommended)

1. HACS → **Integrations** → ⋮ → **Custom repositories**.
2. Add this repository URL, category **Integration**.
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

**Persistence.** Totals, the last source reading, the daily counter and the
Joulo baselines are stored through Home Assistant's storage helper, so a
restart continues where it left off instead of resetting to zero.

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
  consumption. Use a metered energy entity for those.
- **Dynamic contracts only.** Fixed and variable contracts are not modelled in
  this version, and gas is out of scope.

---

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install pytest-homeassistant-custom-component ruff
.venv/bin/python -m pytest        # 94 tests
.venv/bin/ruff check custom_components tests
```

The test suite covers the tariff math, price normalisation against recorded
API payloads, the accounting engine (price boundaries, gaps, meter resets and
source swaps, implausible deltas, reimbursement and ERE), the HTTP client error
paths, both config flows, periodic price polling, persistence across a reload
and entity cleanup when a device loses a feature. Coverage is 96%.

---

## Roadmap: from cost tracker to HEM platform

KD Helios is deliberately split into layers so the cost tracker can grow into
the heating/energy management platform without a rewrite:

- `prices.py` — tariff acquisition and a queryable price timeline
- `tariff.py` — pure money math, no Home Assistant imports
- `accounting.py` — energy integration and per-device accumulators
- `coordinator.py` — orchestration and polling
- `sensor.py` — presentation only

The pure layers already answer the questions a HEM needs ("what does this kWh
cost at this instant?", "what did this device cost over this window?"), and the
accumulators are plain serialisable state, so they can move to a database or an
external service later — behind the same interface, whether that ends up as a
Docker service or stays inside Home Assistant.
