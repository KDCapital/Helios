/* Render the panel once against a real payload, outside a browser.
 *
 * The element needs a shadow root and a `hass` object only; everything else is
 * plain DOM strings. This lets the test suite prove the render path does not
 * throw and produces the expected sections, without a browser.
 */
const fs = require("fs");

global.HTMLElement = class {
  constructor() {}
  attachShadow() {
    this.shadowRoot = {
      innerHTML: "",
      // A stand-in element so the event wiring runs too.
      getElementById: () => ({ addEventListener: () => {} }),
      // The render wires the range tabs and the chart hover; both are no-ops here.
      querySelector: () => null,
      querySelectorAll: () => [],
    };
    return this.shadowRoot;
  }
};

let Panel;
global.customElements = {
  define: (_name, cls) => {
    Panel = cls;
  },
};
global.window = {
  confirm: () => false,
  setInterval: () => 0,
  clearInterval: () => 0,
};

const [, , source, payloadPath, section, wizardStep] = process.argv;
new Function(fs.readFileSync(source, "utf8"))();

const payload = JSON.parse(fs.readFileSync(payloadPath, "utf8"));
const element = new Panel();
element._hass = {
  config: { currency: payload.entry.currency },
  user: { name: "Jules", is_admin: true },
  states: {},
  devices: {},
  entities: {},
  callWS: async () => ({}),
  callService: async () => undefined,
};
element._data = payload;
if (section === "onboarding") {
  // The panel fetches this over the socket; the harness sets it directly.
  element._setup = { completed: false, step: "welcome", steps: [], skipped: [], device_count: 0 };
  element._sections = [];
  element._wizardStep = Number(wizardStep || 0);
} else if (section) {
  element._section = section;
}
element._render();

process.stdout.write(element.shadowRoot.innerHTML);
