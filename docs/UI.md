# KD Helios — UI and installation design

> **Status (22 November 2026): configuration has moved out of the panel.**
> The panel-first decision was reversed: setup and every setting now go through
> Home Assistant's own config flows (**Settings → Devices & services → KD
> Helios**), because shipping a working product came before rebuilding the
> panel's forms. The panel keeps the read-only dashboard, the history ranges and
> the *Write now* action. Sections below that describe the wizard, inline forms,
> panel-side subentry management and the panel contract editor are design
> history, not current behaviour.

The panel is the product. The integration page is a signpost. Everything a user
configures, sees and controls lives in **KD Helios** in the sidebar; the config
flows, subentries and entity platforms are implementation detail.

Decisions locked in by the owner:

- **Name:** KD Helios.
- **Wizard and free navigation both allowed.** The wizard is the default path,
  but the dashboard is reachable at any time and can be finished later.
- **Admins only.** The panel is registered with `require_admin=True`. If viewing
  for other household members is wanted later, it gets its own read-only panel
  rather than weakening this one.
- **EVCC is required for the car.** The user installs
  [ha-evcc](https://github.com/marq24/ha-evcc) and Helios reads its entities. The
  wizard detects it and blocks the car step with install instructions when it is
  missing.

---

## 1. Principles

1. **One place.** If a setting exists, it is editable in the panel. No "go to the
   integration page" detours.
2. **Save as you go.** Every wizard step and every form writes immediately, so
   abandoning halfway loses nothing.
3. **Detect, then confirm.** Never ask for an entity that can be found. Show what
   was found and let the user correct it.
4. **Explain consequences.** Anything that writes to hardware says what it will
   write, in one sentence, before it is enabled.
5. **The panel tells you why.** Every plan, price and control state has a reason
   attached, visible without opening logs.

## 2. Information architecture

```
KD Helios
├── Overview      today at a glance, the 24 h plan, live state
├── Devices       the tracked loads and what they cost
├── Car           EVCC schedule, reimbursement, ERE
├── Battery       capacity, powers, plan, control switch
├── Grid          metering, saldering, fixed costs, connection
├── History       ranges: today / 7 days / month / year
└── Settings      writes, safety, audit log, data, about
```

Rules:

- **Overview** is the landing page and stays read-only except for the control
  switch.
- Each of Devices / Car / Battery / Grid is a **list plus a detail form**, with
  `Add` in the section header. One entity per row, click to edit.
- **History** reuses the range selector that already exists.
- **Settings** holds the consequential things: write permission, dry-run, audit
  log, profile coverage, diagnostics export.

## 3. Screens

### Overview

Existing dashboard, plus three additions:

| Block | Content |
|---|---|
| Setup progress | Shown only while incomplete: "Setup 4 of 8 · Continue" |
| Plan timeline | The next 24 h: planned windows, expected cost, and *why* |
| Control state | On/off in the header, with the last error if one exists |

The plan timeline is the piece that makes the product feel intelligent:
`02:00–04:00 · charge 8 kWh · €0.61 · cheapest window before your 07:30 departure`.

### Devices

List: name, kind, today's kWh and cost, power now. `Add device` opens an inline
form (name, power entity, energy entity) — the same fields as the flow today,
just without the flow.

Empty state: "Nothing tracked yet. Add a device to see where the money goes."

### Car

Reads EVCC entities. Shows: SOC, connected state, planned window, session energy,
reimbursement and ERE settings.

Requires EVCC. If `evcc` is not in `hass.config.components`, the section shows the
install instructions and a link instead of a form — it cannot be configured
manually, because the schedule is the whole point.

### Battery

Capacity, max charge, reserve and target SOC, efficiency, invoice device,
connection (phases/amps/margin), control switch and the applied plan. The raw TOU
programs stay in the sensor attributes and diagnostics rather than in the card.

Below the plan, a **Why** list renders the blocker registry: one row per
condition that shaped the plan (`The car is charging`, `The connection is at its
limit`, `Waiting for solar to cover the charge`, `Inverter control is switched
off`), with the numbers behind it. The same rows appear in English only, like the
rest of the panel; the sensor attributes keep the machine codes for automations.

The `Write now` action stays, but it is secondary: automatic control is the
product.

### Grid

Metering entities, tariff (saldering, feed-in source), fixed costs, billing year,
and the live import/export with the fuse headroom shown.

### History

Ranges drive every number, as today. New: a per-day cost bar chart and the
forecast-versus-actual comparison once M2 lands.

### Settings

| Group | Contents |
|---|---|
| Control | write permission, dry-run, priority (EV/battery), import cap |
| Safety | "Test write" button, last error, watchdog state |
| Audit | every actuation with time, target, value and reason |
| Data | profile coverage, reset counters, export diagnostics |
| About | version, update notes, link to the roadmap |

## 4. Onboarding wizard

Eight steps, each saving immediately. Progress is stored (a `setup_progress` key
in the entry's options or our store) and shown on Overview until complete.

| # | Step | Required | Notes |
|---|---|---|---|
| 1 | Welcome | — | Three lines, one button. No form. |
| 2 | Contract | yes | Provider searchable and grouped dynamic/fixed, plus interval. |
| 3 | Connection | yes | Phases, fuse amps, margin. Explains the fuse cap. |
| 4 | Detect | — | Scans for HomeWizard P1, Solarman inverter, EVCC. One click per hit, with the mapped entities shown for confirmation. |
| 5 | Battery | no | Inverter device, capacity, powers, reserve/target. |
| 6 | Grid | no | Metering, saldering, fixed costs. Pre-filled by step 4. |
| 7 | Car | no | EVCC entities, pack size, departures. Blocked without EVCC. |
| 8 | Control | no | Enabling automatic control, with dry-run offered as the safe option. |

Wizard behaviour:

- **Resumable.** Closing the panel keeps the position.
- **Skippable.** `Skip for now` on 5–8; the relevant section shows an empty state
  that leads back here.
- **Never blocking.** The dashboard is always reachable; the progress banner is
  the only nag.
- **One click from a device.** `config_panel_domain` gives every Helios device a
  **Configure** button that opens the matching section directly.

## 5. WebSocket command surface

All panel traffic goes through our own commands. Only the first two rows are
implemented; the rest were removed together with the panel's configuration
surface (see the status note at the top) and are kept here as the old plan.

| Command | Purpose |
|---|---|
| `kd_helios/dashboard` | Everything the Overview renders (exists) |
| `kd_helios/history` | Range totals from statistics (exists) |
| `kd_helios/setup` | Wizard state: read and advance |
| `kd_helios/detect` | Scan for known meters, inverters and EVCC entities |
| `kd_helios/subentries` | List configurable subentries with their current data |
| `kd_helios/subentry/create` | Create a device, EV, battery or grid subentry |
| `kd_helios/subentry/update` | Update one subentry's data or title |
| `kd_helios/subentry/delete` | Remove a subentry |
| `kd_helios/control` | Take or release inverter control (wraps the switch) |
| `kd_helios/test_write` | Write one register and read it back |
| `kd_helios/audit` | Recent actuations with reasons |
| `kd_helios/profile/reset` | Clear the learned profile |

Response conventions: `{"ok": true, "data": ...}` or an error with a `code`
(`unauthorized`, `invalid`, `not_found`, `busy`) and a human message. Validation
lives server side, in one place, shared by the wizard and the section forms.

Creating, updating or deleting a subentry fires the entry's update listener and
reloads the integration, which is what makes new entities appear. The panel shows
a busy state for the reload and then refreshes the dashboard.

## 6. Storage and migration

**Subentries stay the storage layer.** `config_subentry_id` is what binds entities
to devices and statistics, so moving config elsewhere would break history and the
device page. The panel writes subentries through the public APIs:

- `hass.config_entries.async_add_subentry(entry, ConfigSubentry(...))`
- `hass.config_entries.async_update_subentry(entry, subentry_id, data=..., title=...)`
- `hass.config_entries.async_remove_subentry(entry, subentry_id)`

**Existing installations are untouched.** Current subentries keep their ids,
entities and statistics. The flows stop being the editing path:

- `async_step_user` creates the single entry with defaults, then aborts with
  `panel` and a `description_placeholders.url` pointing at `/helios`.
- `async_step_reconfigure` aborts with `panel` as well.
- The abort reason and its translation say plainly: *"Managed in the KD Helios
  panel."*
- A persistent notification with a link is created on first setup.

**Single entry.** `unique_id` remains the provider, so a second entry is refused;
the panel edits the existing one.

## 7. Design system

Reuse Home Assistant's CSS variables so light and dark both work, with our own
accent. The panel already does this; the redesign keeps it.

| Token | Value |
|---|---|
| Card | `--card-background-color`, radius 14 px, 1 px divider border |
| Spacing | 4 / 8 / 12 / 16 / 24 |
| Type | 30 px KPI, 14.5 px card title, 12.5 px body, 11 px labels (uppercase, tracked) |
| Pills | 999 px radius, secondary background, one accent variant for good states |
| Accent | `#4f8cff`, good `--success-color`, warn `--warning-color` |

Components to add: `card`, `kpi`, `pill`, `stepper`, `field` (label, control, help,
error), `list-row`, `toast`, `modal`, `empty-state`, `audit-row`. Plain functions
returning HTML strings, matching the current panel's style — no build step.

**Mobile:** tables become stacked list rows; the KPI grid goes single column below
520 px; the wizard is one step per screen; the plan timeline scrolls horizontally.

**Accessibility:** every control has a label, focus is visible, the wizard is
keyboard navigable, and colour is never the only signal (pills carry text).

**Languages:** Dutch and English, all strings in the integration's translations;
no `%key:` placeholders in the panel (there is a test for this).

## 8. Build order

| Phase | Scope | Sessions |
|---|---|---|
| A | Panel shell: navigation, sections, list-and-form editing for Devices/Car/Battery/Grid via WS commands; flows become signposts | 3–4 |
| B | Wizard: eight steps, resumable, progress banner, EVCC detection and blocking | 2–3 |
| C | Detection: HomeWizard, Solarman, EVCC entity mapping with confirmation | 2 |
| D | Settings: control, dry-run, audit log, test write, diagnostics | 2 |
| E | Polish: mobile pass, empty states, toasts, accessibility | 2–3 |

Phase A is the load-bearing one: once subentry editing works from the panel, every
later screen is composition rather than plumbing.

## 9. Acceptance criteria

- Installing the integration creates one entry and shows a notification linking to
  the panel; the integration page offers nothing but that signpost.
- A fresh install can be completed end to end without ever opening a config flow
  dialog.
- Every field currently in a flow is editable in the panel, and the two stay in
  sync because they share the same server-side validation.
- Removing a device in the panel removes its entities; history of other devices is
  untouched.
- Non-admin users do not see the panel at all.
- The car section refuses to configure without EVCC and says why, with a link.
- Nothing in the panel can enable hardware writes without the explicit control
  step and, once enabled, every write appears in the audit log.

## 9b. Phase A: what is built (historical)

The configuration surface below was removed on 22 November 2026; see the status
note at the top.

- `management.py` holds the whole configuration surface: validation, create,
  update, delete, the wizard's position and integration detection.
- Six WebSocket commands expose it (`kd_helios/setup`, `setup/advance`,
  `sections`, `subentry/create|update|delete`), and every mutating one refuses
  non-admins.
- The panel has a section bar (Overview / Devices / Car / Battery / Grid /
  History / Settings), lists each subentry with its stored fields, removes one
  with a confirmation, and adds a device from an inline form with an entity
  datalist.
- The config flows now abort with a `panel` signpost that says where the
  settings live.

- Every section now edits inline through one field table (`FIELDS`), so a field
  cannot exist in a section form and be missing from the wizard: devices, cars,
  battery and grid all create, edit and remove from the panel.
- The wizard renders as a five-screen flow (welcome, connection, grid, car,
  control) that reuses those same forms, saves progress after every step and can
  be skipped; an unfinished setup shows a banner on the Overview.
- A render test proves the wizard's first screen comes out of the real payload.

- The contract is editable in Settings (`kd_helios/contract`), so provider and
  interval no longer need a config flow.
- Detection (`detect_entities`) offers entity ids for grid, battery and car by
  platform and sensor name; a new form prefills them and says so. Detection
  never forces a value: the user confirms.

Still phase A backlog: the control switch inside Settings and the audit log.

## 10. Open questions

1. **Provider list** — 34 providers today; should the wizard offer a search box
   only, or a shortlist of dynamic-tariff providers plus "other"?
2. **Audit retention** — keep the last N actuations (say 500) or a time window?
3. **Setup progress storage** — entry options or our own store? Options keep it
   with the entry and survive reinstall; the store avoids touching options.
4. **Delete confirmation** — type the device name, or a plain confirm? Typing is
   safer but annoying for a heat pump added by mistake.
