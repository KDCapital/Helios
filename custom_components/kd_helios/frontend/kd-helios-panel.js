/* KD Helios sidebar panel.
 *
 * A plain custom element: no build step and no runtime imports, so it works
 * offline and survives Home Assistant upgrades. Everything it renders comes
 * from the kd_helios/dashboard WebSocket command.
 */

const SOURCE_LABELS = { 0: "Off", 1: "Grid", 2: "Generator", 3: "Grid + gen" };

const STYLE = `
  :host {
    display: block;
    background: var(--primary-background-color);
    color: var(--primary-text-color);
    min-height: 100vh;
    padding: 16px;
    box-sizing: border-box;
    font-family: var(--paper-font-body1_-_font-family, system-ui, sans-serif);
  }
  .wrap { max-width: 1400px; margin: 0 auto; display: flex; flex-direction: column; gap: 16px; }
  header { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
  header h1 { font-size: 22px; font-weight: 600; margin: 0; display: flex; align-items: center; gap: 8px; }
  .chip {
    font-size: 12px; padding: 3px 10px; border-radius: 999px;
    background: var(--secondary-background-color); color: var(--secondary-text-color);
    border: 1px solid var(--divider-color);
  }
  .chip.on { background: color-mix(in srgb, var(--success-color) 18%, transparent); color: var(--success-color); border-color: transparent; }
  .chip.off { background: color-mix(in srgb, var(--warning-color) 18%, transparent); color: var(--warning-color); border-color: transparent; }
  .spacer { flex: 1; }
  button {
    font: inherit; font-size: 13px; padding: 7px 14px; border-radius: 8px; cursor: pointer;
    background: var(--primary-color); color: var(--text-primary-color, #fff); border: none;
  }
  button.secondary { background: transparent; color: var(--primary-color); border: 1px solid var(--divider-color); }
  button:disabled { opacity: .5; cursor: default; }
  .grid { display: grid; gap: 16px; }
  .cols-2 { grid-template-columns: repeat(auto-fit, minmax(360px, 1fr)); }
  .cols-4 { grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); }
  .card {
    background: var(--card-background-color); border-radius: 12px; padding: 16px;
    border: 1px solid var(--divider-color); box-shadow: none;
  }
  .card h2 { margin: 0 0 12px; font-size: 15px; font-weight: 600; color: var(--primary-text-color); }
  .hero { display: flex; align-items: baseline; gap: 14px; flex-wrap: wrap; }
  .hero .big { font-size: 38px; font-weight: 700; letter-spacing: -.5px; }
  .hero .unit { font-size: 14px; color: var(--secondary-text-color); }
  .meta { display: flex; gap: 18px; flex-wrap: wrap; margin-top: 10px; font-size: 13px; color: var(--secondary-text-color); }
  .meta b { color: var(--primary-text-color); font-weight: 600; }
  .chart { position: relative; height: 150px; display: flex; align-items: flex-end; gap: 1px; margin-top: 14px; }
  .bar { flex: 1; min-width: 1px; border-radius: 2px 2px 0 0; opacity: .9; }
  .window { position: absolute; top: 0; bottom: 0; background: var(--primary-color); opacity: .16; border-radius: 3px; }
  .now { position: absolute; top: 0; bottom: 0; width: 2px; background: var(--primary-color); }
  .axis { position: relative; height: 18px; margin-top: 4px; font-size: 11px; color: var(--secondary-text-color); }
  .axis span { position: absolute; transform: translateX(-50%); white-space: nowrap; }
  .stat { display: flex; flex-direction: column; gap: 4px; }
  .stat .label { font-size: 12px; color: var(--secondary-text-color); }
  .stat .value { font-size: 20px; font-weight: 600; }
  .stat .value.neg { color: var(--success-color); }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th, td { text-align: left; padding: 7px 8px; border-bottom: 1px solid var(--divider-color); }
  th { color: var(--secondary-text-color); font-weight: 500; font-size: 12px; text-transform: uppercase; letter-spacing: .03em; }
  td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
  tr:last-child td { border-bottom: none; }
  .muted { color: var(--secondary-text-color); font-size: 13px; }
  .badge-off { color: var(--warning-color); }
  .error { color: var(--error-color); }
  .row-actions { display: flex; gap: 8px; align-items: center; margin-top: 12px; flex-wrap: wrap; }
`;

class KdHeliosPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._data = null;
    this._error = null;
    this._started = false;
    this._busy = false;
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
      this._error = null;
    } catch (err) {
      this._error = err && err.message ? err.message : String(err);
    }
    this._render();
  }

  async _apply() {
    if (!this._data || !this._data.battery) return;
    const applied = window.confirm(
      "Write the planned charge windows to the inverter? This changes how the battery behaves."
    );
    if (!applied) return;
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

  _money(value) {
    if (value === null || value === undefined) return "—";
    const currency = (this._hass && this._hass.config && this._hass.config.currency) || "EUR";
    const symbol = currency === "EUR" ? "€" : "";
    const text = new Intl.NumberFormat(undefined, {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    }).format(value);
    return symbol ? `${symbol} ${text}` : `${text} ${currency}`;
  }

  _price(value) {
    if (value === null || value === undefined) return "—";
    return new Intl.NumberFormat(undefined, {
      minimumFractionDigits: 4,
      maximumFractionDigits: 4,
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
    const options = withDate
      ? { weekday: "short", hour: "2-digit", minute: "2-digit" }
      : { hour: "2-digit", minute: "2-digit" };
    return date.toLocaleString(undefined, options);
  }

  _esc(text) {
    return String(text).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  }

  _chart(priceData, windows, nowIso) {
    const points = priceData.points || [];
    if (!points.length) return '<p class="muted">No prices published yet.</p>';
    const values = points.map((p) => p.price);
    const min = Math.min(...values);
    const max = Math.max(...values);
    const span = max - min || 1;
    const start = new Date(points[0].t).getTime();
    const end = new Date(points[points.length - 1].t).getTime() + 15 * 60000;
    const total = end - start || 1;

    const bars = points
      .map((point) => {
        const ratio = (point.price - min) / span;
        const hue = 150 - 150 * ratio;
        const height = 12 + 88 * ratio;
        const label = `${this._time(point.t, true)} · ${this._price(point.price)}`;
        return `<div class="bar" title="${label}" style="height:${height}%;background:hsl(${hue} 70% 45%)"></div>`;
      })
      .join("");

    const overlays = (windows || [])
      .map((window) => {
        const from = new Date(window.start).getTime();
        const to = new Date(window.end).getTime();
        const left = ((from - start) / total) * 100;
        const width = ((to - from) / total) * 100;
        if (width <= 0 || left > 100) return "";
        return `<div class="window" style="left:${left}%;width:${width}%"></div>`;
      })
      .join("");

    const now = new Date(nowIso).getTime();
    let marker = "";
    if (now >= start && now <= end) {
      const left = ((now - start) / total) * 100;
      marker = `<div class="now" style="left:${left}%"></div>`;
    }

    const ticks = [];
    for (let hour = 0; hour <= 24; hour += 6) {
      ticks.push(
        `<span style="left:${(hour / 24) * 100}%">${this._time(
          new Date(start + hour * 3600000).toISOString()
        )}</span>`
      );
    }

    return `
      <div class="chart">${overlays}${bars}${marker}</div>
      <div class="axis">${ticks.join("")}</div>`;
  }

  _gridCard(grid) {
    if (!grid) {
      return `<div class="card"><h2>Grid connection</h2>
        <p class="muted">No grid connection configured. Add one to see what you pay the energy company.</p></div>`;
    }
    const saldering = grid.saldering_active
      ? '<span class="chip on">Saldering active</span>'
      : grid.saldering_enabled
      ? '<span class="chip off">Saldering off</span>'
      : '<span class="chip">Feed-in price</span>';
    return `
      <div class="card">
        <h2>Grid connection · ${grid.billing_year} ${saldering}</h2>
        <div class="grid cols-4">
          <div class="stat"><span class="label">Imported</span>
            <span class="value">${this._number(grid.imported_kwh, 1)} kWh</span></div>
          <div class="stat"><span class="label">Imported cost</span>
            <span class="value">${this._money(grid.imported_cost)}</span></div>
          <div class="stat"><span class="label">Exported</span>
            <span class="value">${this._number(grid.exported_kwh, 1)} kWh</span></div>
          <div class="stat"><span class="label">Export credit</span>
            <span class="value neg">− ${this._money(grid.export_credit)}</span></div>
          <div class="stat"><span class="label">Fixed costs</span>
            <span class="value">${this._money(grid.fixed_cost)}</span></div>
          <div class="stat"><span class="label">Net grid cost</span>
            <span class="value">${this._money(grid.total_cost)}</span></div>
          <div class="stat"><span class="label">Feed-in price</span>
            <span class="value">${this._price(grid.export_price)}</span></div>
          <div class="stat"><span class="label">Netted</span>
            <span class="value">${this._number(grid.netted_kwh, 1)} kWh</span></div>
        </div>
        <p class="muted">${this._money(grid.fixed_monthly)} per month in supply and grid operator costs.</p>
      </div>`;
  }

  _batteryCard(battery) {
    if (!battery) {
      return `<div class="card"><h2>Home battery</h2>
        <p class="muted">No battery configured. Add one to have KD Helios plan its charging.</p></div>`;
    }
    const plan = battery.plan;
    const applied = battery.applied;
    const windows = plan ? plan.windows : [];
    const programs = battery.programs || [];

    const windowRows = windows.length
      ? windows
          .map(
            (w) =>
              `<tr><td>${this._time(w.start, true)}</td><td>${this._time(w.end)}</td>
               <td class="num">${this._number(w.energy_kwh, 1)} kWh</td>
               <td class="num">${this._price(w.average_price)}</td>
               <td class="num">${this._money(w.expected_profit)}</td></tr>`
          )
          .join("")
      : `<tr><td colspan="5" class="muted">${
          plan ? this._reason(plan.reason) : "No plan yet."
        }</td></tr>`;

    const programRows = programs
      .map(
        (p) =>
          `<tr><td>${p.start}</td><td class="num">${p.power_w} W</td>
           <td class="num">${p.soc_pct}%</td>
           <td>${SOURCE_LABELS[p.source] || p.source}</td></tr>`
      )
      .join("");

    const canApply = battery.allow_writes && battery.has_inverter && plan;
    return `
      <div class="card">
        <h2>Home battery</h2>
        <div class="grid cols-4">
          <div class="stat"><span class="label">State of charge</span>
            <span class="value">${this._number(battery.soc_pct, 0)}%</span></div>
          <div class="stat"><span class="label">Planned charge</span>
            <span class="value">${plan ? this._number(plan.energy_kwh, 1) : "—"} kWh</span></div>
          <div class="stat"><span class="label">Planned saving</span>
            <span class="value neg">${plan ? this._money(plan.expected_saving) : "—"}</span></div>
          <div class="stat"><span class="label">Applied plan saving</span>
            <span class="value neg">${applied ? this._money(applied.expected_saving) : "—"}</span></div>
        </div>
        <p class="muted">
          ${battery.capacity_kwh} kWh · max ${battery.max_charge_kw} kW ·
          reserve ${battery.reserve_soc_pct}% · target ${battery.target_soc_pct}% ·
          efficiency ${battery.efficiency_pct}%
          ${applied ? `· applied ${this._time(applied.applied_at, true)}` : "· nothing applied yet"}
        </p>
        <table>
          <thead><tr><th>Window start</th><th>End</th><th class="num">Energy</th>
            <th class="num">Avg price</th><th class="num">Profit</th></tr></thead>
          <tbody>${windowRows}</tbody>
        </table>
        ${
          programRows
            ? `<h2 style="margin-top:16px">Time-of-use programs${battery.truncated ? " (truncated)" : ""}</h2>
               <table><thead><tr><th>Start</th><th class="num">Power</th>
                 <th class="num">SOC</th><th>Source</th></tr></thead>
                 <tbody>${programRows}</tbody></table>`
            : ""
        }
        <div class="row-actions">
          <button id="apply" ${canApply && !this._busy ? "" : "disabled"}>Write programs to inverter</button>
          <span class="muted">${
            battery.allow_writes
              ? battery.has_inverter
                ? "Requires confirmation."
                : "Select an inverter device in the battery settings first."
              : "Writes are disabled in the battery settings."
          }</span>
        </div>
      </div>`;
  }

  _reason(reason) {
    switch (reason) {
      case "planned":
        return "Planned.";
      case "no_spread":
        return "No profitable spread right now, so nothing is bought.";
      case "full":
        return "Battery is already at the target.";
      case "no_soc":
        return "Waiting for a state of charge reading.";
      case "no_prices":
        return "No prices available.";
      default:
        return reason;
    }
  }

  _devicesCard(devices) {
    if (!devices.length) {
      return `<div class="card"><h2>Devices</h2>
        <p class="muted">No devices tracked yet. Add one from the integration page.</p></div>`;
    }
    const rows = devices
      .map(
        (d) => `<tr>
          <td>${this._esc(d.name)}${
            d.kind === "ev_charger" ? ' <span class="chip">EV</span>' : ""
          }</td>
          <td class="num">${this._number(d.energy_kwh, 1)}</td>
          <td class="num">${this._money(d.cost)}</td>
          <td class="num">${this._money(d.cost_today)}</td>
          <td class="num">${d.has_offsets ? this._money(d.net_cost) : "—"}</td>
        </tr>`
      )
      .join("");
    return `
      <div class="card">
        <h2>Devices</h2>
        <table>
          <thead><tr><th>Device</th><th class="num">Energy (kWh)</th>
            <th class="num">Cost</th><th class="num">Today</th><th class="num">Net</th></tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>`;
  }

  _render() {
    if (!this.shadowRoot) return;
    if (!this._data) {
      this.shadowRoot.innerHTML = `
        <style>${STYLE}</style>
        <div class="wrap"><h1>Helios</h1>
        <p class="${this._error ? "error" : "muted"}">${
          this._error ? this._esc(this._error) : "Loading…"
        }</p></div>`;
      return;
    }

    const { entry, prices, grid, battery, devices } = this._data;
    const windows = battery && battery.plan ? battery.plan.windows : [];
    const current = prices.current;
    const currency = entry.currency === "EUR" ? "€" : entry.currency;

    const html = `
      <style>${STYLE}</style>
      <div class="wrap">
        <header>
          <h1>⚡ Helios</h1>
          <span class="chip">${this._esc(entry.provider)} · ${this._esc(entry.interval)}</span>
          ${this._error ? `<span class="chip off">${this._esc(this._error)}</span>` : ""}
          <span class="spacer"></span>
          <button class="secondary" id="refresh">Refresh</button>
        </header>

        <div class="card">
          <div class="hero">
            <span class="big">${this._price(current)}</span>
            <span class="unit">${currency}/kWh now</span>
            <span class="spacer"></span>
            <span class="chip">${prices.min !== null ? `${this._price(prices.min)} – ${this._price(prices.max)} next 24 h` : ""}</span>
          </div>
          <div class="meta">
            <span>Spot <b>${this._price(prices.spot)}</b></span>
            <span>Next <b>${this._price(prices.next)}</b></span>
            <span>Average today <b>${this._price(prices.average_today)}</b></span>
            <span>Energy tax <b>${this._price(prices.energy_tax)}</b></span>
            <span>Provider charge <b>${this._price(prices.provider_charge)}</b></span>
          </div>
          ${this._chart(prices, windows, this._data.now)}
        </div>

        <div class="grid cols-2">
          ${this._gridCard(grid)}
          ${this._batteryCard(battery)}
        </div>

        ${this._devicesCard(devices)}
      </div>`;

    this.shadowRoot.innerHTML = html;

    const apply = this.shadowRoot.getElementById("apply");
    if (apply) apply.addEventListener("click", () => this._apply());
    const refresh = this.shadowRoot.getElementById("refresh");
    if (refresh) refresh.addEventListener("click", () => this._load());
  }
}

customElements.define("kd-helios-panel", KdHeliosPanel);
