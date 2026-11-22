/* Render the panel once against a real payload, outside a browser.
 *
 * The element needs a shadow root and a `hass` object only; everything else is
 * plain DOM strings. This lets the test suite prove the render path does not
 * throw and produces the expected cards, without a browser.
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
const registry = new Map();
global.customElements = {
  get: (name) => registry.get(name),
  define: (name, cls) => {
    if (registry.has(name)) {
      throw new Error(`the name "${name}" has already been used`);
    }
    registry.set(name, cls);
    Panel = cls;
  },
};
global.window = {
  confirm: () => false,
  setInterval: () => 0,
  clearInterval: () => 0,
};

const [, , source, payloadPath] = process.argv;
const bundle = fs.readFileSync(source, "utf8");
new Function(bundle)();
// A second load happens in the browser when the panel is re-registered after a
// reload. It must skip define(), not throw "already been used".
new Function(bundle)();

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
element._render();

process.stdout.write(element.shadowRoot.innerHTML);
