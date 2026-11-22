/* KD Helios sidebar panel.
 *
 * A plain custom element: no build step and no runtime imports, so it works
 * offline and survives Home Assistant upgrades. Everything it renders comes
 * from the kd_helios/dashboard WebSocket command.
 *
 * Only real values are shown. The design follows the Helios dashboard, but
 * there is deliberately no solar generation, no car state of charge and no
 * day-over-day trend: the integration does not track those, and inventing them
 * would make a cost dashboard lie.
 */

// Where configuration lives now that the panel no longer edits it.
const CONFIG_HINT = "Settings → Devices & services → KD Helios";

// Every condition the backend can report, in the words the panel uses. One
// dictionary so a new reason cannot appear as a bare code on the dashboard.
const BLOCKER_LABELS = {
  no_soc: "No battery SOC reading",
  no_prices: "No prices available",
  prices_stale: "Prices are from the last successful fetch",
  full: "Battery is at its target",
  no_spread: "No profitable spread in the horizon",
  waiting_for_solar: "Waiting for solar to cover the charge",
  partly_solar: "Solar covers part of the charge",
  hold_conflict: "The car's window blocks the cheap hours",
  slots_truncated: "The plan does not fit the six inverter slots",
  ev_charging: "The car is charging",
  import_cap: "The connection is at its limit",
  temperature: "The battery is hot",
  control_off: "Inverter control is switched off",
  writes_disabled: "Inverter writes are disabled",
  no_inverter: "No inverter selected",
  inverter_not_ready: "Waiting for the inverter integration to load",
  write_failed: "The inverter refused the schedule",
  manual_charge: "Test charge running",
  not_delivering: "The battery is not delivering",
  weekly_full_charge: "Weekly full charge for cell balancing",
  no_forecast: "No solar forecast available",
  no_consumption_forecast: "The house load is not learned yet",
};

const STYLE = `
  :host {
    display: block;
    background: var(--primary-background-color);
    color: var(--primary-text-color);
    min-height: 100vh;
    padding: 20px 24px 40px;
    box-sizing: border-box;
    font-family: var(--paper-font-body1_-_font-family, system-ui, sans-serif);
    --helios-accent: #4f8cff;
    --helios-good: var(--success-color, #34c759);
    --helios-warn: var(--warning-color, #f5a623);
    --helios-card: var(--card-background-color, #16181d);
    --helios-line: var(--divider-color, #2a2d34);
  }
  * { box-sizing: border-box; }
  .wrap { max-width: 1240px; margin: 0 auto; display: flex; flex-direction: column; gap: 18px; }

  .topbar { display: flex; align-items: center; gap: 12px; }
  .brand { display: flex; align-items: center; gap: 10px; }
  .mark {
    width: 34px; height: 34px; border-radius: 10px; display: grid; place-items: center;
    background: color-mix(in srgb, var(--helios-accent) 22%, transparent);
    color: var(--helios-accent); font-size: 18px;
  }
  .brand .name { font-size: 19px; font-weight: 650; letter-spacing: -0.01em; }
  .eyebrow { font-size: 10px; letter-spacing: .14em; text-transform: uppercase; color: var(--secondary-text-color); }
  .spacer { flex: 1; }
  .chip {
    display: inline-flex; align-items: center; gap: 6px;
    font-size: 12px; padding: 5px 10px; border-radius: 999px;
    background: var(--secondary-background-color); color: var(--secondary-text-color);
    border: 1px solid var(--helios-line); white-space: nowrap;
  }
  .chip .dot { width: 6px; height: 6px; border-radius: 50%; background: var(--helios-good); }
  .chip.warn .dot { background: var(--helios-warn); }
  .chip.off .dot { background: var(--secondary-text-color); }
  button {
    font: inherit; font-size: 12px; padding: 7px 12px; border-radius: 9px; cursor: pointer;
    background: transparent; color: var(--primary-text-color);
    border: 1px solid var(--helios-line);
  }
  button.primary { background: var(--helios-accent); border-color: transparent; color: #fff; }
  button:disabled { opacity: .45; cursor: default; }

  .hero { display: flex; align-items: flex-end; gap: 16px; flex-wrap: wrap; margin-top: 6px; }
  .hero h1 { margin: 4px 0 2px; font-size: 30px; font-weight: 680; letter-spacing: -0.02em; }
  .hero p { margin: 0; color: var(--secondary-text-color); font-size: 13px; }
  .hero .meta { margin-left: auto; display: flex; gap: 14px; align-items: center; font-size: 12px; color: var(--secondary-text-color); }

  .kpis { display: grid; gap: 14px; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); }
  .card {
    background: var(--helios-card); border: 1px solid var(--helios-line);
    border-radius: 14px; padding: 16px 18px;
  }
  .kpi { display: flex; flex-direction: column; gap: 8px; min-height: 132px; }
  .kpi .head { display: flex; align-items: center; gap: 8px; }
  .kpi .tile {
    width: 30px; height: 30px; border-radius: 9px; display: grid; place-items: center;
    background: var(--secondary-background-color); color: var(--secondary-text-color); font-size: 15px;
  }
  .kpi .label { font-size: 12.5px; color: var(--secondary-text-color); }
  .kpi .value { font-size: 29px; font-weight: 660; letter-spacing: -0.02em; line-height: 1.05; margin-top: auto; }
  .kpi .value small { font-size: 14px; font-weight: 500; color: var(--secondary-text-color); margin-left: 4px; }
  .kpi .pill {
    align-self: flex-start; font-size: 11.5px; padding: 3px 9px; border-radius: 999px;
    background: var(--secondary-background-color); color: var(--secondary-text-color);
    border: 1px solid var(--helios-line);
  }
  .kpi .pill.good { color: var(--helios-good); }

  .cols { display: grid; gap: 14px; }
  .cols.two { grid-template-columns: repeat(auto-fit, minmax(400px, 1fr)); }
  .card h2 { margin: 0; font-size: 14.5px; font-weight: 620; }
  .card .sub { margin: 2px 0 0; font-size: 12px; color: var(--secondary-text-color); }
  .card .head { display: flex; align-items: flex-start; gap: 10px; }
  .card .head .spacer { flex: 1; }
  .legend { display: flex; gap: 12px; flex-wrap: wrap; font-size: 11.5px; color: var(--secondary-text-color); }
  .legend span { display: inline-flex; align-items: center; gap: 5px; }
  .swatch { width: 8px; height: 8px; border-radius: 3px; }
  .swatch.line { height: 2px; width: 14px; border-radius: 2px; }

  .chartwrap { position: relative; margin-top: 14px; height: 220px; }
  .chartwrap svg { position: absolute; inset: 0; width: 100%; height: 100%; overflow: visible; }
  .chartwrap .gridline { position: absolute; left: 0; right: 0; border-top: 1px dashed var(--helios-line); }
  .chartwrap .ylabel { position: absolute; right: 4px; transform: translateY(-50%); font-size: 10.5px; color: var(--secondary-text-color); }
  .chartwrap .win { position: absolute; top: 0; bottom: 0; background: var(--helios-accent); opacity: .13; border-radius: 3px; }
  .chartwrap .now { position: absolute; top: 0; bottom: 0; width: 1px; background: var(--primary-text-color); opacity: .35; }
  .axis { position: relative; height: 18px; margin-top: 6px; font-size: 10.5px; color: var(--secondary-text-color); }
  .axis span { position: absolute; transform: translateX(-50%); white-space: nowrap; }

  .rows { display: flex; flex-direction: column; gap: 0; }
  .row {
    display: flex; align-items: center; gap: 10px; padding: 9px 0;
    border-bottom: 1px solid var(--helios-line); font-size: 13px;
  }
  .row:last-child { border-bottom: none; }
  .row .grow { flex: 1; min-width: 0; }
  .row .name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .row .tag { font-size: 10.5px; color: var(--secondary-text-color); }
  .row .num { font-variant-numeric: tabular-nums; }

  .bars { display: flex; flex-direction: column; gap: 12px; margin-top: 12px; }
  .bar-row .top { display: flex; align-items: baseline; gap: 8px; font-size: 12.5px; margin-bottom: 5px; }
  .bar-row .top .num { margin-left: auto; font-variant-numeric: tabular-nums; color: var(--secondary-text-color); }
  .track { height: 6px; border-radius: 999px; background: var(--secondary-background-color); overflow: hidden; }
  .fill { height: 100%; border-radius: 999px; }

  .ring { display: flex; align-items: center; gap: 18px; }
  .ring .dial {
    width: 92px; height: 92px; border-radius: 50%; display: grid; place-items: center;
    background: conic-gradient(var(--helios-accent) var(--pct), var(--secondary-background-color) 0);
  }
  .ring .dial .inner {
    width: 74px; height: 74px; border-radius: 50%; background: var(--helios-card);
    display: grid; place-items: center; font-size: 19px; font-weight: 650;
  }
  .metrics { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px 18px; flex: 1; }
  .metric .label { font-size: 11.5px; color: var(--secondary-text-color); }
  .metric .value { font-size: 17px; font-weight: 600; font-variant-numeric: tabular-nums; }
  .metric .value.good { color: var(--helios-good); }

  .muted { color: var(--secondary-text-color); font-size: 12.5px; margin: 8px 0 0; }
  .error { color: var(--error-color); }
  .actions { display: flex; align-items: center; gap: 10px; margin-top: 14px; flex-wrap: wrap; }
  .ranges { display: inline-flex; gap: 2px; padding: 2px; border-radius: 999px;
    background: var(--secondary-background-color); border: 1px solid var(--helios-line); }
  .ranges button { border: none; background: transparent; padding: 5px 11px; border-radius: 999px; }
  .ranges button.on { background: var(--helios-accent); color: #fff; }
  .chartwrap .bands { position: absolute; inset: 0; display: flex; }
  .chartwrap .bands span { flex: 1; }
  .tip { position: absolute; pointer-events: none; padding: 7px 10px; border-radius: 9px;
    background: var(--helios-card); border: 1px solid var(--helios-line); font-size: 11.5px;
    box-shadow: 0 6px 20px rgba(0,0,0,.35); white-space: nowrap; z-index: 3;
    font-variant-numeric: tabular-nums; }
  .tip b { font-weight: 600; }
  .tip .row { display: flex; gap: 10px; justify-content: space-between; }
  .tip .muted { margin: 0; font-size: 10.5px; }
`;


class KdHeliosPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._data = null;
    this._error = null;
    this._started = false;
    this._busy = false;
    this._synced = null;
    this._range = "today";
    this._history = null;
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._started) {
      this._started = true;
      this._load();
      this._timer = window.setInterval(() => this._load(), 60000);
    }
  }

  disconnectedCallback() {
    window.clearInterval(this._timer);
  }

  async _load() {
    if (!this._hass) return;
    try {
      this._data = await this._hass.callWS({ type: "kd_helios/dashboard" });
      this._history = await this._loadHistory();
      this._error = null;
      this._synced = new Date();
    } catch (err) {
      this._error = err && err.message ? err.message : String(err);
    }
    this._render();
  }

  async _loadHistory() {
    if (this._range === "today") return null;
    try {
      return await this._hass.callWS({
        type: "kd_helios/history",
        range: this._range,
      });
    } catch (err) {
      // History is a nicety; the live numbers must still render.
      return { available: false, error: String(err) };
    }
  }

  async _setRange(range) {
    if (this._range === range) return;
    this._range = range;
    this._history = await this._loadHistory();
    this._render();
  }

  async _chargeNow() {
    if (!this._data) return;
    const ok = window.confirm(
      "Charge from the grid for 15 minutes? This buys energy at the current price."
    );
    if (!ok) return;
    this._busy = true;
    this._render();
    try {
      await this._hass.callService("kd_helios", "charge_now", {
        entry_id: this._data.entry.entry_id,
        confirm: true,
        minutes: 15,
      });
      await this._load();
    } catch (err) {
      this._error = err && err.message ? err.message : String(err);
    } finally {
      this._busy = false;
      this._render();
    }
  }

  async _apply() {
    if (!this._data) return;
    const ok = window.confirm(
      "Write the planned charge windows to the inverter? This changes how the battery behaves."
    );
    if (!ok) return;
    this._busy = true;
    this._render();
    try {
      await this._hass.callService("kd_helios", "apply_tou_programs", {
        entry_id: this._data.entry.entry_id,
        confirm: true,
      });
      await this._load();
    } catch (err) {
      this._error = err && err.message ? err.message : String(err);
    } finally {
      this._busy = false;
      this._render();
    }
  }

  // -- formatting ---------------------------------------------------------
  _currency() {
    return (this._hass && this._hass.config && this._hass.config.currency) || "EUR";
  }

  _money(value, digits = 2) {
    if (value === null || value === undefined) return "—";
    const text = new Intl.NumberFormat(undefined, {
      minimumFractionDigits: digits,
      maximumFractionDigits: digits,
    }).format(value);
    return this._currency() === "EUR" ? `€${text}` : `${text} ${this._currency()}`;
  }

  _price(value) {
    if (value === null || value === undefined) return "—";
    return new Intl.NumberFormat(undefined, {
      minimumFractionDigits: 3,
      maximumFractionDigits: 3,
    }).format(value);
  }

  _number(value, digits = 1) {
    if (value === null || value === undefined) return "—";
    return new Intl.NumberFormat(undefined, {
      minimumFractionDigits: digits,
      maximumFractionDigits: digits,
    }).format(value);
  }

  _time(iso, withDate = false) {
    const date = new Date(iso);
    return date.toLocaleString(
      undefined,
      withDate
        ? { weekday: "short", hour: "2-digit", minute: "2-digit" }
        : { hour: "2-digit", minute: "2-digit" }
    );
  }

  _date(iso) {
    return new Date(iso).toLocaleDateString(undefined, {
      month: "short",
      day: "numeric",
      year: "numeric",
    });
  }

  _greeting() {
    const hour = new Date().getHours();
    if (hour < 12) return "Good morning";
    if (hour < 18) return "Good afternoon";
    return "Good evening";
  }

  _esc(text) {
    return String(text).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  }

  // -- pieces -------------------------------------------------------------
  _rangeTabs() {
    const labels = { today: "Today", "7d": "7D", month: "Month", year: "Year" };
    return Object.entries(labels)
      .map(
        ([key, text]) =>
          `<button data-range="${key}" class="${this._range === key ? "on" : ""}">${text}</button>`
      )
      .join("");
  }

  _rangeLabel() {
    return { today: "today", "7d": "the last 7 days", month: "this month", year: "this year" }[
      this._range
    ];
  }

  _kpi({ icon, label, value, unit, sub, tone }) {
    return `
      <div class="card kpi">
        <div class="head"><span class="tile">${icon}</span><span class="label">${label}</span></div>
        <div class="value">${value}${unit ? `<small>${unit}</small>` : ""}</div>
        ${sub ? `<span class="pill ${tone || ""}">${sub}</span>` : ""}
      </div>`;
  }

  _chart(prices, windows, nowIso) {
    const points = prices.points || [];
    if (!points.length) {
      return `<p class="muted">No prices published for this period yet.</p>`;
    }
    const values = points.map((point) => point.price);
    const min = Math.min(...values);
    const max = Math.max(...values);
    const span = max - min || 1;
    const pad = span * 0.12;
    const low = min - pad;
    const high = max + pad;
    const y = (price) => 100 - ((price - low) / (high - low)) * 100;

    const step = points.length > 1 ? 100 / (points.length - 1) : 0;
    const coords = points.map((point, index) => `${(index * step).toFixed(3)},${y(point.price).toFixed(3)}`);
    const line = `M${coords.join(" L")}`;
    const area = `${line} L100,100 L0,100 Z`;

    const average = prices.average_today;
    const avgLine =
      average !== null && average >= low && average <= high
        ? `<line x1="0" y1="${y(average).toFixed(2)}" x2="100" y2="${y(average).toFixed(2)}"
             stroke="var(--secondary-text-color)" stroke-width="1" stroke-dasharray="3 4"
             vector-effect="non-scaling-stroke" opacity=".55" />`
        : "";

    const start = new Date(points[0].t).getTime();
    const end = new Date(points[points.length - 1].t).getTime() + 15 * 60000;
    const total = end - start || 1;
    const overlays = (windows || [])
      .map((window) => {
        const from = new Date(window.start).getTime();
        const to = new Date(window.end).getTime();
        const left = ((from - start) / total) * 100;
        const width = ((to - from) / total) * 100;
        if (width <= 0 || left > 100) return "";
        return `<div class="win" style="left:${left}%;width:${width}%"></div>`;
      })
      .join("");

    const now = new Date(nowIso).getTime();
    const marker =
      now >= start && now <= end
        ? `<div class="now" style="left:${(((now - start) / total) * 100).toFixed(3)}%"></div>`
        : "";

    // Top of the chart is the highest price, so the labels descend.
    const gridlines = [0, 50, 100]
      .map(
        (pct, index) => `
        <div class="gridline" style="top:${pct}%"></div>
        <span class="ylabel" style="top:${pct}%">${this._price(
          index === 0 ? high - pad : index === 1 ? (low + high) / 2 : low + pad
        )}</span>`
      )
      .join("");

    const bands = points
      .map((_point, index) => `<span data-i="${index}"></span>`)
      .join("");

    const ticks = [];
    for (let hour = 0; hour <= 24; hour += 6) {
      ticks.push(
        `<span style="left:${((hour * 3600000) / total) * 100}%">${this._time(
          new Date(start + hour * 3600000).toISOString()
        )}</span>`
      );
    }

    return `
      <div class="chartwrap">
        <svg viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true">
          <defs>
            <linearGradient id="helios-fill" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stop-color="var(--helios-accent)" stop-opacity="0.45" />
              <stop offset="100%" stop-color="var(--helios-accent)" stop-opacity="0.02" />
            </linearGradient>
          </defs>
          <path d="${area}" fill="url(#helios-fill)" />
          <path d="${line}" fill="none" stroke="var(--helios-accent)" stroke-width="2"
                stroke-linejoin="round" stroke-linecap="round" vector-effect="non-scaling-stroke" />
          ${avgLine}
        </svg>
        ${gridlines}
        ${overlays}
        ${marker}
        <div class="bands">${bands}</div>
        <div class="tip" hidden></div>
      </div>
      <div class="axis">${ticks.join("")}</div>`;
  }

  _evCard(ev, summary) {
    if (!ev.length) {
      return `<div class="card"><div class="head"><div><h2>EV charging</h2>
        <p class="sub">No EV charger configured</p></div></div>
        <p class="muted">Add an EV charger under ${CONFIG_HINT} to track charging, reimbursement and ERE value.</p></div>`;
    }
    const rows = ev
      .map(
        (device) => `
        <div class="row">
          <span class="grow"><span class="name">${this._esc(device.name)}</span>
            ${device.power !== null ? `<div class="tag">${this._number(device.power, 0)} W now</div>` : ""}</span>
          <span class="num">${this._money(device.cost_today)}</span>
        </div>`
      )
      .join("");

    // How much of the charging cost has been reimbursed so far.
    const reimbursed = summary.ev_reimbursed || 0;
    const costToDate = reimbursed - (summary.ev_balance || 0);
    const share = costToDate > 0 ? Math.max(0, Math.min(100, (reimbursed / costToDate) * 100)) : 0;

    return `
      <div class="card">
        <div class="head">
          <div><h2>EV charging &amp; reimbursement</h2>
          <p class="sub">Today, and reimbursement to date</p></div>
          <span class="spacer"></span>
          ${ev.some((device) => device.power > 100) ? '<span class="chip"><span class="dot"></span>Charging</span>' : ""}
        </div>
        <div class="ring" style="margin-top:14px">
          <div class="dial" style="--pct:${share}%">
            <div class="inner">${this._money(summary.ev_cost_today)}</div>
          </div>
          <div class="metrics">
            <div class="metric"><div class="label">Reimbursed to date</div>
              <div class="value good">${this._money(summary.ev_reimbursed)}</div></div>
            <div class="metric"><div class="label">Balance to date</div>
              <div class="value ${summary.ev_balance >= 0 ? "good" : ""}">${this._money(summary.ev_balance)}</div></div>
          </div>
        </div>
        <div class="rows" style="margin-top:14px">${rows}</div>
      </div>`;
  }

  _deviceCard(devices) {
    if (!devices.length) {
      return `<div class="card"><div class="head"><div><h2>Device cost breakdown</h2>
        <p class="sub">Nothing tracked yet</p></div></div>
        <p class="muted">Add a device under ${CONFIG_HINT} to see where the money goes.</p></div>`;
    }
    const today = devices
      .map((device) => ({ ...device, spend: Math.max(0, device.cost_today || 0) }))
      .sort((a, b) => b.spend - a.spend);
    const highest = Math.max(...today.map((device) => device.spend), 0.0001);
    const palette = ["#4f8cff", "#f5a623", "#a78bfa", "#34c759", "#ff6b6b", "#22d3ee"];

    const rows = today
      .map((device, index) => {
        const width = Math.max(2, (device.spend / highest) * 100);
        const colour = palette[index % palette.length];
        return `
          <div class="bar-row">
            <div class="top">
              <span>${this._esc(device.name)}</span>
              <span class="num">${this._number(device.energy_kwh_today, 1)} kWh · ${this._money(device.cost_today)}</span>
            </div>
            <div class="track"><div class="fill" style="width:${width}%;background:${colour}"></div></div>
          </div>`;
      })
      .join("");

    return `
      <div class="card">
        <div class="head"><div><h2>Device cost breakdown</h2>
          <p class="sub">${this._rangeLabel()}, per tracked device</p></div></div>
        <div class="bars">${rows}</div>
      </div>`;
  }

  _blockers(blockers) {
    if (!blockers || !blockers.length) {
      return "";
    }
    const rows = blockers
      .map((blocker) => {
        const label = BLOCKER_LABELS[blocker.reason] || blocker.reason;
        const details = blocker.details || {};
        const facts = Object.entries(details)
          .filter(([, value]) => value !== null && value !== undefined && value !== "")
          .map(([key, value]) => `${key.replace(/_/g, " ")} ${value}`)
          .join(" · ");
        return `<div class="row"><span class="grow">${this._esc(label)}</span>
          <span class="num muted">${this._esc(facts)}</span></div>`;
      })
      .join("");
    return `<div class="rows" style="margin-top:10px">
      <div class="row"><span class="grow"><strong>Why</strong></span></div>
      ${rows}</div>`;
  }

  _reasonsCard(battery) {
    const blockers = (battery && battery.blockers) || [];
    const verified = battery ? battery.write_verified : null;
    const rows = blockers
      .map(
        (blocker) => `
        <div class="row">
          <span class="grow"><span class="name">${this._esc(blocker.reason)}</span>
            <div class="tag">${this._esc(blocker.source)}${
              blocker.since ? ` · since ${this._time(blocker.since)}` : ""
            }${this._details(blocker.details)}</div></span>
        </div>`
      )
      .join("");

    return `<div class="card">
      <div class="head"><div><h2>Why the battery is doing this</h2>
        <p class="sub">Every bound that is holding right now, and the numbers behind it</p></div>
        <span class="spacer"></span>
        <span class="chip ${verified === false ? "warn" : ""}"><span class="dot"></span>${
          verified === false ? "write unverified" : verified ? "write verified" : "no write yet"
        }</span>
      </div>
      ${rows ? `<div class="rows" style="margin-top:8px">${rows}</div>` : `<p class="muted">Nothing is holding the battery back.</p>`}
    </div>`;
  }

  _details(details) {
    if (!details || typeof details !== "object") return "";
    const parts = Object.entries(details)
      .filter(([, value]) => value !== null && value !== undefined)
      .slice(0, 3)
      .map(([key, value]) => `${key} ${typeof value === "number" ? Math.round(value * 100) / 100 : value}`);
    return parts.length ? ` · ${parts.join(" · ")}` : "";
  }

  _batteryCard(battery, summary) {
    if (!battery) {
      return `<div class="card"><div class="head"><div><h2>Home battery</h2>
        <p class="sub">No battery configured</p></div></div>
        <p class="muted">Add a home battery under ${CONFIG_HINT} to plan charging against the tariff.</p></div>`;
    }
    const plan = battery.plan;
    const applied = battery.applied;
    const soc = battery.soc_pct === null ? 0 : Math.max(0, Math.min(100, battery.soc_pct));
    const saving = summary.battery_saving_planned;
    const next = plan && plan.next_window_start ? plan.next_window_start : null;

    const canWrite = battery.allow_writes && battery.has_inverter;
    return `
      <div class="card">
        <div class="head">
          <div><h2>Battery &amp; plan</h2>
          <p class="sub">${battery.capacity_kwh} kWh · max ${battery.max_charge_kw} kW</p></div>
          <span class="spacer"></span>
          <span class="chip ${battery.control_enabled ? "" : "off"}">
            <span class="dot"></span>${battery.control_enabled ? "Control on" : "Control off"}</span>
        </div>
        <div class="ring" style="margin-top:14px">
          <div class="dial" style="--pct:${soc}%"><div class="inner">${this._number(soc, 0)}%</div></div>
          <div class="metrics">
            <div class="metric"><div class="label">Planned saving</div>
              <div class="value good">${this._money(saving)}</div></div>
            <div class="metric"><div class="label">Next window</div>
              <div class="value">${next ? this._time(next, true) : "—"}</div></div>
            <div class="metric"><div class="label">Planned charge</div>
              <div class="value">${plan ? this._number(plan.energy_kwh, 1) : "—"} kWh</div></div>
            <div class="metric"><div class="label">Applied</div>
              <div class="value">${applied ? this._time(applied.applied_at, true) : "—"}</div></div>
          </div>
        </div>
        ${this._blockers(battery.blockers)}
        <div class="actions">
          <button class="primary" id="apply" ${canWrite && !this._busy ? "" : "disabled"}>Write now</button>
          <button id="charge-now" ${
            canWrite && battery.control_enabled && !this._busy ? "" : "disabled"
          }>Charge 15 min</button>
          <span class="muted" style="margin:0">${
            canWrite
              ? "Automatic control does this for you when enabled."
              : "Enable inverter writes in the battery settings to control it."
          }</span>
        </div>
      </div>`;
  }

  _tariffCard(prices, grid) {
    const cheap = prices.min;
    const expensive = prices.max;
    return `
      <div class="card">
        <div class="head"><div><h2>Tariff</h2>
          <p class="sub">${this._esc(this._data.entry.provider)} · ${this._esc(this._data.entry.interval)}</p></div>
          <span class="spacer"></span>
          <span class="chip"><span class="dot"></span>${this._currency()}/kWh</span>
        </div>
        <div class="metrics" style="margin-top:14px">
          <div class="metric"><div class="label">Now</div>
            <div class="value">${this._money(prices.current, 3)}</div></div>
          <div class="metric"><div class="label">Spot</div>
            <div class="value">${this._money(prices.spot, 3)}</div></div>
          <div class="metric"><div class="label">Cheapest next 24 h</div>
            <div class="value good">${this._money(cheap, 3)}</div></div>
          <div class="metric"><div class="label">Most expensive</div>
            <div class="value">${this._money(expensive, 3)}</div></div>
        </div>
        <div class="rows" style="margin-top:8px">
          <div class="row"><span class="grow">Energy tax</span>
            <span class="num">${this._money(prices.energy_tax, 4)}</span></div>
          <div class="row"><span class="grow">Provider charge</span>
            <span class="num">${this._money(prices.provider_charge, 4)}</span></div>
          ${
            grid
              ? `<div class="row"><span class="grow">Fixed costs</span>
                   <span class="num">${this._money(grid.fixed_monthly)} / month</span></div>
                 <div class="row"><span class="grow">Feed-in price</span>
                   <span class="num">${this._money(grid.export_price, 3)}</span></div>
                 <div class="row"><span class="grow">Saldering</span>
                   <span class="num">${grid.saldering_active ? "Active" : "Off"}</span></div>`
              : ""
          }
        </div>
      </div>`;
  }

  _gridCard(grid) {
    if (!grid) {
      return `<div class="card"><div class="head"><div><h2>Grid connection</h2>
        <p class="sub">Not configured</p></div></div>
        <p class="muted">Add a grid connection under ${CONFIG_HINT} to see what you pay the energy company.</p></div>`;
    }
    const rows = [
      ["Imported today", `${this._number(grid.imported_kwh_today, 2)} kWh`],
      ["Imported cost today", this._money(grid.imported_cost_today)],
      ["Exported today", `${this._number(grid.exported_kwh_today, 2)} kWh`],
      ["Export credit today", this._money(grid.export_credit_today)],
      ["Fixed costs today", this._money(grid.fixed_cost_today)],
      ["Year to date", this._money(grid.total_cost)],
    ]
      .map(
        ([label, value]) =>
          `<div class="row"><span class="grow">${label}</span><span class="num">${value}</span></div>`
      )
      .join("");
    return `
      <div class="card">
        <div class="head"><div><h2>Grid connection</h2>
          <p class="sub">Billing year ${grid.billing_year}</p></div>
          <span class="spacer"></span>
          <span class="chip ${grid.saldering_active ? "" : "warn"}"><span class="dot"></span>${
            grid.saldering_active ? "Saldering" : "Feed-in"
          }</span>
        </div>
        <div class="rows" style="margin-top:8px">${rows}</div>
      </div>`;
  }

  _wireChart() {
    const wrap = this.shadowRoot.querySelector(".chartwrap");
    if (!wrap || !this._data) return;
    const points = (this._data.prices && this._data.prices.points) || [];
    if (!points.length) return;
    const tip = wrap.querySelector(".tip");
    const bands = wrap.querySelectorAll(".bands span");

    const show = (index, event) => {
      const point = points[index];
      if (!point) return;
      const price = this._money(point.price, 3);
      tip.innerHTML = `
        <div><b>${this._time(point.t)}</b></div>
        <div class="row"><span>All-in</span><b>${price}</b></div>`;
      tip.hidden = false;
      const box = wrap.getBoundingClientRect();
      const left = Math.min(Math.max(event.clientX - box.left + 12, 4), box.width - 120);
      tip.style.left = `${left}px`;
      tip.style.top = `${Math.max(4, event.clientY - box.top - 46)}px`;
    };

    bands.forEach((band) => {
      band.addEventListener("mousemove", (event) =>
        show(Number(band.dataset.i), event)
      );
      band.addEventListener("mouseleave", () => {
        tip.hidden = true;
      });
    });
  }

  _render() {
    if (!this.shadowRoot) return;
    if (!this._data) {
      this.shadowRoot.innerHTML = `
        <style>${STYLE}</style>
        <div class="wrap">
          <div class="topbar"><div class="brand"><span class="mark">⚡</span>
            <div><div class="name">Helios</div><div class="eyebrow">KD Helios</div></div></div></div>
          <p class="${this._error ? "error" : "muted"}">${
            this._error ? this._esc(this._error) : "Loading…"
          }</p>
        </div>`;
      return;
    }

    const { entry, prices, grid, battery, devices, summary } = this._data;
    const history = this._history && this._history.available ? this._history : null;

    // With a range selected, every number comes from the statistics of that
    // range; without one, from the live daily counters.
    const view = history
      ? history.devices.map((row) => {
          const live = devices.find((item) => item.name === row.name);
          return {
            name: row.name,
            kind: live ? live.kind : "device",
            cost: row.cost,
            energy: row.energy_kwh,
            power: live ? live.power : null,
            cost_today: row.cost,
            energy_kwh_today: row.energy_kwh,
          };
        })
      : devices;

    const ev = view.filter((device) => device.kind === "ev_charger");
    const others = view.filter((device) => device.kind !== "ev_charger");
    const gridCost = history
      ? history.grid
        ? history.grid.cost
        : null
      : summary.grid_cost_today;
    const gridIn = history
      ? history.grid
        ? history.grid.import_kwh
        : null
      : summary.grid_import_today;
    const gridOut = history
      ? history.grid
        ? history.grid.export_kwh
        : null
      : summary.grid_export_today;
    const gridBalance = gridIn === null || gridOut === null ? null : gridIn - gridOut;
    const devicesCost = history
      ? view.reduce((total, device) => total + (device.cost || 0), 0)
      : summary.devices_cost_today;
    const windows = battery && battery.plan ? battery.plan.windows : [];
    const user = this._hass && this._hass.user ? this._hass.user.name : entry.title;
    const control = battery
      ? battery.control_enabled
        ? `<span class="chip"><span class="dot"></span>Control on</span>`
        : `<span class="chip off"><span class="dot"></span>Control off</span>`
      : "";

    const kpis = [
      this._kpi({
        icon: "⚡",
        label: "Net electricity cost",
        value: this._money(gridCost),
        sub: history
          ? `${this._rangeLabel()}`
          : summary.grid_cost_year !== null
          ? `Year to date ${this._money(summary.grid_cost_year)}`
          : "No grid connection",
        tone: "",
      }),
      this._kpi({
        icon: "🔋",
        label: "Battery plan saving",
        value: this._money(
          summary.battery_saving_applied !== null
            ? summary.battery_saving_applied
            : summary.battery_saving_planned
        ),
        sub:
          summary.battery_saving_applied !== null
            ? "Applied plan · predicted"
            : summary.battery_saving_planned !== null
            ? "Proposal · not applied"
            : "No battery configured",
        tone: "good",
      }),
      this._kpi({
        icon: "🔌",
        label: "EV charging balance",
        value: this._money(summary.ev_balance),
        sub: history
          ? `${this._rangeLabel()} · live balance ${this._money(summary.ev_balance)}`
          : summary.ev_reimbursed !== null
          ? `${this._money(summary.ev_reimbursed)} reimbursed`
          : "No EV charger",
        tone: summary.ev_balance >= 0 ? "good" : "",
      }),
      this._kpi({
        icon: "🏠",
        label: "Net grid balance",
        value: this._number(gridBalance, 1),
        unit: "kWh",
        sub:
          gridIn !== null
            ? `${this._number(gridIn, 1)} in · ${this._number(gridOut, 1)} out`
            : "No grid connection",
        tone: "",
      }),
    ].join("");

    this.shadowRoot.innerHTML = `
      <style>${STYLE}</style>
      <div class="wrap">
        <div class="topbar">
          <div class="brand"><span class="mark">⚡</span>
            <div><div class="name">Helios</div><div class="eyebrow">KD Helios</div></div>
          </div>
          <span class="spacer"></span>
          <span class="chip"><span class="dot"></span>Live tariff ${this._money(prices.current, 3)}/kWh</span>
          ${control}
          ${this._error ? `<span class="chip warn"><span class="dot"></span>${this._esc(this._error)}</span>` : ""}
          <div class="ranges">${this._rangeTabs()}</div>
          <button id="refresh">Refresh</button>
        </div>

        <div class="hero">
          <div>
            <div class="eyebrow">Energy overview</div>
            <h1>${this._greeting()}, ${this._esc(user)}</h1>
            <p>Here is how your home is performing today.</p>
          </div>
          <div class="meta">
            <span class="chip"><span class="dot"></span>Live monitoring</span>
            <span>${this._date(this._data.now)}</span>
          </div>
        </div>

        <div class="kpis">${kpis}</div>

        <div class="card">
          <div class="head">
            <div><h2>Price &amp; planned charging</h2>
              <p class="sub">All-in tariff for the next 24 hours, with the windows KD Helios wants</p></div>
            <span class="spacer"></span>
            <div class="legend">
              <span><i class="swatch line" style="background:var(--helios-accent)"></i>All-in price</span>
              <span><i class="swatch line" style="background:var(--secondary-text-color)"></i>Average today</span>
              <span><i class="swatch" style="background:color-mix(in srgb, var(--helios-accent) 30%, transparent)"></i>Planned charge</span>
            </div>
          </div>
          ${this._chart(prices, windows, this._data.now)}
        </div>

        <div class="cols two">
          ${this._evCard(ev, summary)}
          ${this._deviceCard(others)}
        </div>

        <div class="cols two">
          ${this._batteryCard(battery, summary)}
          ${this._tariffCard(prices, grid)}
        </div>

        <div class="cols two">
          ${this._reasonsCard(battery)}
        </div>

        <div class="cols two">
          ${this._gridCard(grid)}
          ${this._kpi({ icon: "🧾", label: history ? "Devices in range" : "Devices today", value: this._money(devicesCost), sub: `${devices.length} tracked device${devices.length === 1 ? "" : "s"}` })}
        </div>
      </div>`;

    this.shadowRoot.querySelectorAll("[data-range]").forEach((button) => {
      button.addEventListener("click", () => this._setRange(button.dataset.range));
    });
    this._wireChart();

    const apply = this.shadowRoot.getElementById("apply");
    if (apply) apply.addEventListener("click", () => this._apply());
    const chargeNow = this.shadowRoot.getElementById("charge-now");
    if (chargeNow) chargeNow.addEventListener("click", () => this._chargeNow());
    const refresh = this.shadowRoot.getElementById("refresh");
    if (refresh) refresh.addEventListener("click", () => this._load());
  }
}

if (!customElements.get("kd-helios-panel")) {
  customElements.define("kd-helios-panel", KdHeliosPanel);
}
