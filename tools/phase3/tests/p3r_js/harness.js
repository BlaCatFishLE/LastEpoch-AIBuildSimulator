#!/usr/bin/env node
/*
 * P3R behavior acceptance harness (Node, no external dependencies).
 *
 * Executes the *actual* inline JavaScript of a freshly generated player report
 * (source: a new `le.ps1 run` HTML file) inside `node:vm`, with a minimal DOM
 * and a controllable `fetch` adapter.  It never re-implements report business
 * logic: the report's own shared recompute client and model interaction script
 * are the code under test.  The Python unittest suite drives scenarios through
 * the JSON spec file described below and asserts on the produced observations.
 *
 * Usage: node harness.js <spec.json>
 * Output: JSON on stdout (optionally also written to spec.output).
 *
 * Spec shape (all paths absolute or relative to process.cwd()):
 * {
 *   "output": "path/obs.json",
 *   "fetchDefault": "real",
 *   "endpoint": "http://127.0.0.1:18765/recompute",   // optional override
 *   "pages": {
 *     "main": {"html": "run/report.html", "protocol": "file", "bindingsPatch": {...},
 *              "resultKeys": ["single_hit", "hits_per_cast", "scenario"]}
 *   },
 *   "steps": [
 *     {"page": "main", "op": "set_value", "selector": "[data-cond-hits]", "value": "1"},
 *     {"page": "main", "op": "click", "selector": "[data-cond-compute]"},
 *     {"page": "main", "op": "wait_idle"},
 *     {"page": "main", "op": "snapshot", "name": "after"},
 *     {"page": "main", "op": "save_written", "path": "run/written.html"},
 *     {"page": "main", "op": "reload_written"}
 *   ]
 * }
 */
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");
const crypto = require("crypto");

const VOID_ELEMENTS = new Set([
  "area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
  "param", "source", "track", "wbr",
]);
const RAW_TEXT_ELEMENTS = new Set(["script", "style", "textarea", "title"]);

function sha256(text) {
  return crypto.createHash("sha256").update(String(text), "utf8").digest("hex");
}

// ---------------------------------------------------------------------------
// Minimal DOM
// ---------------------------------------------------------------------------

class TextNode {
  constructor(doc, text) {
    this.ownerDocument = doc;
    this.nodeType = 3;
    this.data = String(text);
    this.parentNode = null;
  }
  get textContent() { return this.data; }
  set textContent(value) { this.data = String(value == null ? "" : value); }
  get nodeValue() { return this.data; }
  set nodeValue(value) { this.data = String(value == null ? "" : value); }
  get nextSibling() { return siblingOf(this, 1); }
  get previousSibling() { return siblingOf(this, -1); }
  get parentElement() { return this.parentNode instanceof Element ? this.parentNode : null; }
}

function siblingOf(node, delta) {
  const parent = node.parentNode;
  if (!parent) return null;
  const list = parent.childNodes;
  const index = list.indexOf(node);
  return index < 0 ? null : list[index + delta] || null;
}

class ClassList {
  constructor(element) { this._element = element; }
  _values() {
    const raw = this._element.getAttribute("class") || "";
    return raw.split(/\s+/).filter(Boolean);
  }
  _store(values) { this._element.setAttribute("class", Array.from(new Set(values)).join(" ")); }
  get length() { return this._values().length; }
  get value() { return this._element.getAttribute("class") || ""; }
  item(index) { return this._values()[Number(index)] || null; }
  contains(name) { return this._values().includes(String(name)); }
  add(...names) { const v = this._values(); for (const n of names) if (!v.includes(String(n))) v.push(String(n)); this._store(v); }
  remove(...names) { const drop = new Set(names.map(String)); this._store(this._values().filter((v) => !drop.has(v))); }
  toggle(name, force) {
    const has = this.contains(name);
    const want = force === undefined ? !has : !!force;
    if (want && !has) this.add(name); else if (!want && has) this.remove(name);
    return want;
  }
  replace(oldName, newName) {
    const v = this._values().map((x) => (x === String(oldName) ? String(newName) : x));
    this._store(v);
    return true;
  }
  forEach(cb, thisArg) { this._values().forEach((v) => cb.call(thisArg, v)); }
  toString() { return this.value; }
  [Symbol.iterator]() { return this._values()[Symbol.iterator](); }
}

class Style {
  constructor() { this._props = {}; }
  setProperty(name, value) { this._props[String(name)] = String(value); }
  removeProperty(name) { delete this._props[String(name)]; }
  getPropertyValue(name) { return this._props[String(name)] || ""; }
  get cssText() { return Object.entries(this._props).map(([k, v]) => `${k}: ${v};`).join(" "); }
  set cssText(value) { this._props = {}; String(value || "").split(";").forEach((part) => { const i = part.indexOf(":"); if (i > 0) this.setProperty(part.slice(0, i).trim(), part.slice(i + 1).trim()); }); }
}
Object.defineProperty(Style.prototype, "display", {
  get() { return this.getPropertyValue("display"); },
  set(value) { this.setProperty("display", value); },
});

let NEXT_NODE_ID = 1;
class Element {
  constructor(doc, tagName) {
    this.ownerDocument = doc;
    this.nodeType = 1;
    this.localName = String(tagName || "div").toLowerCase();
    this.tagName = this.localName.toUpperCase();
    this.namespaceURI = "http://www.w3.org/1999/xhtml";
    this._attrs = new Map();
    this.childNodes = [];
    this.parentNode = null;
    this._listeners = new Map();
    this.style = new Style();
    this._value = "";
    this.checked = false;
    this._selectedIndex = 0;
    this.files = null;
    this._nodeId = NEXT_NODE_ID++;
    this._classList = null;
    this._dataset = null;
  }
  get nodeId() { return this._nodeId; }
  get children() { return this.childNodes.filter((n) => n.nodeType === 1); }
  get parentElement() { return this.parentNode instanceof Element ? this.parentNode : null; }
  get firstChild() { return this.childNodes[0] || null; }
  get lastChild() { return this.childNodes[this.childNodes.length - 1] || null; }
  get firstElementChild() { return this.children[0] || null; }
  get lastElementChild() { const c = this.children; return c[c.length - 1] || null; }
  get nextSibling() { return siblingOf(this, 1); }
  get previousSibling() { return siblingOf(this, -1); }
  get nextElementSibling() {
    let n = this.nextSibling;
    while (n && n.nodeType !== 1) n = n.nextSibling;
    return n;
  }
  get previousElementSibling() {
    let n = this.previousSibling;
    while (n && n.nodeType !== 1) n = n.previousSibling;
    return n;
  }
  get childElementCount() { return this.children.length; }
  get id() { return this.getAttribute("id") || ""; }
  set id(value) { this.setAttribute("id", value); }
  get className() { return this.getAttribute("class") || ""; }
  set className(value) { this.setAttribute("class", value); }
  get classList() { return this._classList || (this._classList = new ClassList(this)); }
  get attributes() {
    return Array.from(this._attrs.entries()).map(([name, value]) => ({ name, value, nodeName: name, nodeValue: value }));
  }
  get dataset() {
    if (!this._dataset) {
      const element = this;
      this._dataset = new Proxy({}, {
        get(_t, key) { return element.getAttribute("data-" + kebab(String(key))) || undefined; },
        set(_t, key, value) { element.setAttribute("data-" + kebab(String(key)), value); return true; },
        has(_t, key) { return element.hasAttribute("data-" + kebab(String(key))); },
        deleteProperty(_t, key) { element.removeAttribute("data-" + kebab(String(key))); return true; },
      });
    }
    return this._dataset;
  }
  get hidden() { return this.hasAttribute("hidden"); }
  set hidden(value) { if (value) this.setAttribute("hidden", ""); else this.removeAttribute("hidden"); }
  get disabled() { return this.hasAttribute("disabled"); }
  set disabled(value) { if (value) this.setAttribute("disabled", ""); else this.removeAttribute("disabled"); }
  get type() {
    if (this.localName === "input") return this.getAttribute("type") || "text";
    if (this.localName === "button") return this.getAttribute("type") || "submit";
    if (this.localName === "select") return "select-one";
    return this.getAttribute("type") || "";
  }
  set type(value) { this.setAttribute("type", value); }
  get name() { return this.getAttribute("name") || ""; }
  set name(value) { this.setAttribute("name", value); }
  get href() { return this.getAttribute("href") || ""; }
  set href(value) { this.setAttribute("href", value); }
  get download() { return this.getAttribute("download") || ""; }
  set download(value) { this.setAttribute("download", value); }
  get target() { return this.getAttribute("target") || ""; }
  set target(value) { this.setAttribute("target", value); }
  get options() { return this.children.filter((c) => c.localName === "option"); }
  _optionValue(option) {
    const explicit = option.getAttribute("value");
    return explicit !== null ? String(explicit) : option.textContent;
  }
  get value() {
    if (this.localName === "select") {
      const selected = this.options.filter((o) => o.selected === true || o._selected === true);
      const option = selected.length ? selected[selected.length - 1] : this.options[0];
      return option ? this._optionValue(option) : "";
    }
    if (this.localName === "option") return this._optionValue(this);
    return this._value;
  }
  set value(next) {
    const value = String(next == null ? "" : next);
    if (this.localName === "select") {
      const options = this.options;
      let index = options.findIndex((option) => this._optionValue(option) === value);
      if (index < 0) index = 0;
      options.forEach((option, position) => { option._selected = position === index; });
      this._selectedIndex = index;
      return;
    }
    this._value = value;
  }
  get selectedIndex() {
    if (this.localName !== "select") return this._selectedIndex || 0;
    const options = this.options;
    const index = options.findIndex((option) => option._selected === true || option.selected === true);
    return index >= 0 ? index : 0;
  }
  set selectedIndex(next) {
    const index = Number(next) || 0;
    const options = this.options;
    options.forEach((option, position) => { option._selected = position === index; });
    this._selectedIndex = index;
  }
  get selectedOptions() {
    if (this.localName !== "select") return [];
    const opts = this.options;
    if (this.multiple) return opts.filter((o) => o.selected);
    const selected = opts.filter((o) => o.selected);
    return selected.length ? [selected[selected.length - 1]] : (opts[0] ? [opts[0]] : []);
  }
  get multiple() { return this.hasAttribute("multiple"); }
  get selected() { return this.hasAttribute("selected") || (this.localName === "option" && this._selected === true); }
  set selected(value) {
    if (this.localName !== "option") return;
    this._selected = !!value;
    if (value && this.parentNode && this.parentNode.localName === "select" && !this.parentNode.multiple) {
      for (const sibling of this.parentNode.options) if (sibling !== this) sibling._selected = false;
      this.parentNode.selectedIndex = this.parentNode.options.indexOf(this);
      this.parentNode.value = this.value;
    }
  }
  get textContent() {
    return this.childNodes.map((n) => n.textContent).join("");
  }
  set textContent(value) {
    this.childNodes = [];
    const text = String(value == null ? "" : value);
    if (text) this.appendChild(new TextNode(this.ownerDocument, text));
  }
  get innerText() { return this.textContent; }
  set innerText(value) { this.textContent = value; }
  get innerHTML() { return this.childNodes.map((n) => serializeNode(n)).join(""); }
  set innerHTML(value) {
    this.childNodes = [];
    const fragment = parseFragment(this.ownerDocument, String(value == null ? "" : value));
    for (const child of fragment.childNodes.slice()) this.appendChild(child);
  }
  get outerHTML() { return serializeNode(this); }
  set outerHTML(value) {
    const parent = this.parentNode;
    if (!parent) return;
    const fragment = parseFragment(this.ownerDocument, String(value == null ? "" : value));
    const index = parent.childNodes.indexOf(this);
    parent.childNodes.splice(index, 1, ...fragment.childNodes);
    for (const child of fragment.childNodes) child.parentNode = parent;
  }
  getAttribute(name) {
    const key = String(name || "").toLowerCase();
    return this._attrs.has(key) ? this._attrs.get(key) : null;
  }
  setAttribute(name, value) {
    const key = String(name || "").toLowerCase();
    this._attrs.set(key, String(value == null ? "" : value));
    const doc = this.ownerDocument;
    if (doc && key === "id") doc._idMap.set(String(value), this);
    if (this.localName === "option" && key === "value") { /* value read live */ }
    if (this.localName === "option" && key === "selected") this._selected = true;
  }
  removeAttribute(name) {
    const key = String(name || "").toLowerCase();
    const previous = this._attrs.get(key);
    this._attrs.delete(key);
    if (this.ownerDocument && key === "id" && previous) this.ownerDocument._idMap.delete(previous);
    if (this.localName === "option" && key === "selected") this._selected = false;
  }
  hasAttribute(name) { return this._attrs.has(String(name || "").toLowerCase()); }
  toggleAttribute(name, force) {
    const has = this.hasAttribute(name);
    const want = force === undefined ? !has : !!force;
    if (want && !has) this.setAttribute(name, "");
    if (!want && has) this.removeAttribute(name);
    return want;
  }
  getAttributeNames() { return Array.from(this._attrs.keys()); }
  appendChild(child) {
    if (child && child.nodeType === 11) { for (const nested of child.childNodes.slice()) this.appendChild(nested); return child; }
    if (this.childNodes.length && this.childNodes[this.childNodes.length - 1] === child) return child;
    const existing = this.childNodes.indexOf(child);
    if (existing >= 0) this.childNodes.splice(existing, 1);
    child.parentNode = this;
    this.childNodes.push(child);
    return child;
  }
  insertBefore(child, reference) {
    if (!reference) return this.appendChild(child);
    const index = this.childNodes.indexOf(reference);
    if (index < 0) return this.appendChild(child);
    const existing = this.childNodes.indexOf(child);
    if (existing >= 0) this.childNodes.splice(existing, 1);
    child.parentNode = this;
    this.childNodes.splice(this.childNodes.indexOf(reference), 0, child);
    return child;
  }
  removeChild(child) {
    const index = this.childNodes.indexOf(child);
    if (index < 0) return child;
    this.childNodes.splice(index, 1);
    child.parentNode = null;
    return child;
  }
  replaceChild(newChild, oldChild) {
    const index = this.childNodes.indexOf(oldChild);
    if (index < 0) return oldChild;
    const existing = this.childNodes.indexOf(newChild);
    if (existing >= 0) this.childNodes.splice(existing, 1);
    this.childNodes[this.childNodes.indexOf(oldChild)] = newChild;
    newChild.parentNode = this;
    oldChild.parentNode = null;
    return oldChild;
  }
  remove() {
    if (this.parentNode) this.parentNode.removeChild(this);
  }
  replaceWith(...nodes) {
    const parent = this.parentNode;
    if (!parent) return;
    const index = parent.childNodes.indexOf(this);
    parent.childNodes.splice(index, 1, ...nodes.map((n) => (typeof n === "string" ? new TextNode(this.ownerDocument, n) : n)));
    for (const node of parent.childNodes) node.parentNode = parent;
  }
  before(...nodes) {
    const parent = this.parentNode;
    if (!parent) return;
    const index = parent.childNodes.indexOf(this);
    const items = nodes.map((n) => (typeof n === "string" ? new TextNode(this.ownerDocument, n) : n));
    parent.childNodes.splice(index, 0, ...items);
    for (const node of items) node.parentNode = parent;
  }
  after(...nodes) {
    const parent = this.parentNode;
    if (!parent) return;
    const index = parent.childNodes.indexOf(this);
    const items = nodes.map((n) => (typeof n === "string" ? new TextNode(this.ownerDocument, n) : n));
    parent.childNodes.splice(index + 1, 0, ...items);
    for (const node of items) node.parentNode = parent;
  }
  append(...nodes) { for (const n of nodes) this.appendChild(typeof n === "string" ? new TextNode(this.ownerDocument, n) : n); }
  prepend(...nodes) {
    for (const n of nodes.reverse()) this.insertBefore(typeof n === "string" ? new TextNode(this.ownerDocument, n) : n, this.childNodes[0] || null);
  }
  replaceChildren(...nodes) {
    this.childNodes = [];
    for (const n of nodes) this.appendChild(typeof n === "string" ? new TextNode(this.ownerDocument, n) : n);
  }
  insertAdjacentHTML(position, html) {
    const fragment = parseFragment(this.ownerDocument, String(html == null ? "" : html));
    const items = fragment.childNodes.slice();
    for (const node of items) node.parentNode = null;
    const pos = String(position).toLowerCase();
    if (pos === "beforebegin") { const parent = this.parentNode; if (!parent) return; const index = parent.childNodes.indexOf(this); parent.childNodes.splice(index, 0, ...items); for (const n of items) n.parentNode = parent; }
    else if (pos === "afterbegin") { this.childNodes.splice(0, 0, ...items); for (const n of items) n.parentNode = this; }
    else if (pos === "beforeend") { for (const n of items) this.appendChild(n); }
    else if (pos === "afterend") { const parent = this.parentNode; if (!parent) return; const index = parent.childNodes.indexOf(this); parent.childNodes.splice(index + 1, 0, ...items); for (const n of items) n.parentNode = parent; }
  }
  insertAdjacentElement(position, element) { this.insertAdjacentHTML(position, element.outerHTML); }
  insertAdjacentText(position, text) { this.insertAdjacentHTML(position, escapeHTML(text)); }
  cloneNode(deep) {
    const clone = new Element(this.ownerDocument, this.localName);
    for (const [key, value] of this._attrs) clone._attrs.set(key, value);
    if (deep) for (const child of this.childNodes) clone.appendChild(child.cloneNode ? child.cloneNode(true) : new TextNode(this.ownerDocument, child.textContent));
    return clone;
  }
  matches(selector) { return matchesOne(this, selector, this.ownerDocument); }
  closest(selector) {
    let node = this;
    while (node && node.nodeType === 1) {
      if (matchesOne(node, selector, this.ownerDocument)) return node;
      node = node.parentNode;
    }
    return null;
  }
  contains(node) {
    if (!node) return false;
    if (node === this) return true;
    let current = node;
    while (current) { if (current === this) return true; current = current.parentNode; }
    return false;
  }
  querySelector(selector) {
    const found = queryAll(this, selector, this.ownerDocument, true);
    return found[0] || null;
  }
  querySelectorAll(selector) { return queryAll(this, selector, this.ownerDocument, true); }
  getElementsByTagName(tag) { return this.querySelectorAll(String(tag)); }
  getElementsByClassName(names) { return this.querySelectorAll(String(names).split(/\s+/).filter(Boolean).map((n) => "." + n).join("")); }
  addEventListener(type, handler, options) {
    if (typeof handler !== "function" && (!handler || typeof handler.handleEvent !== "function")) return;
    const key = String(type);
    if (!this._listeners.has(key)) this._listeners.set(key, []);
    this._listeners.get(key).push({ handler, capture: !!(options && options.capture) });
  }
  removeEventListener(type, handler) {
    const key = String(type);
    const list = this._listeners.get(key);
    if (!list) return;
    this._listeners.set(key, list.filter((entry) => entry.handler !== handler));
  }
  dispatchEvent(event) {
    if (!event) return true;
    const type = String(event.type || "");
    event.target = event.target || this;
    if (typeof event.preventDefault !== "function") event.preventDefault = () => { event.defaultPrevented = true; };
    if (typeof event.stopPropagation !== "function") event.stopPropagation = () => { event._stopped = true; };
    let node = this;
    while (node) {
      event.currentTarget = node;
      const list = node._listeners && node._listeners.get(type);
      if (list) {
        for (const entry of list.slice()) {
          try {
            if (typeof entry.handler === "function") entry.handler.call(node, event);
            else if (entry.handler && typeof entry.handler.handleEvent === "function") entry.handler.handleEvent(event);
          } catch (error) {
            this.ownerDocument._page.errors.push({ where: "event:" + type, message: String(error && error.stack || error) });
          }
        }
      }
      if (event._stopped || event.cancelBubble) break;
      node = node.parentNode;
    }
    if (!event._stopped && this.ownerDocument && this.ownerDocument !== this) {
      try { this.ownerDocument.dispatchEvent(event); } catch (error) { this.ownerDocument._page.errors.push({ where: "document-dispatch", message: String(error) }); }
    }
    return !event.defaultPrevented;
  }
  click() {
    if (this.disabled) return;
    if (this.localName === "a" && this.ownerDocument && this.ownerDocument._page) {
      const page = this.ownerDocument._page;
      const href = this.getAttribute("href") || "";
      page.downloads.push({
        href,
        download: this.getAttribute("download") || "",
        content: href.indexOf("blob:") === 0 ? (page.blobStore[href] || null) : null,
        at: Date.now(),
      });
    }
    this.dispatchEvent({ type: "click", target: this, button: 0, bubbles: true, cancelable: true });
  }
  focus() { this.ownerDocument.activeElement = this; this.dispatchEvent({ type: "focus", target: this }); }
  blur() { if (this.ownerDocument.activeElement === this) this.ownerDocument.activeElement = this.ownerDocument.body; this.dispatchEvent({ type: "blur", target: this }); }
  select() {}
  setSelectionRange() {}
  scrollIntoView() {}
  scrollTo() {}
  getBoundingClientRect() { return { x: 0, y: 0, width: 0, height: 0, top: 0, left: 0, right: 0, bottom: 0 }; }
  getClientRects() { return []; }
  get context() { return undefined; }
  get parentElement() {
    return this.parentNode instanceof Element ? this.parentNode : null;
  }
  get offsetWidth() { return 0; }
  get offsetHeight() { return 0; }
  get clientWidth() { return 0; }
  get clientHeight() { return 0; }
  get scrollWidth() { return 0; }
  get scrollHeight() { return 0; }
  get scrollTop() { return 0; }
  set scrollTop(_value) {}
  get scrollLeft() { return 0; }
  set scrollLeft(_value) {}
  getContext() { return makeCanvasContext(); }
  toDataURL() { return "data:image/png;base64,"; }
  getBBox() { return { x: 0, y: 0, width: 0, height: 0 }; }
  getTotalLength() { return 0; }
  setAttributeNS(_ns, name, value) { this.setAttribute(name, value); }
  getAttributeNS(_ns, name) { return this.getAttribute(name); }
}

function kebab(name) {
  return String(name || "").replace(/[A-Z]/g, (m) => "-" + m.toLowerCase());
}

function escapeHTML(value) {
  return String(value == null ? "" : value)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function serializeNode(node) {
  if (!node) return "";
  if (node.nodeType === 3) return rawText(node.data);
  const attrs = Array.from(node._attrs.entries()).map(([key, value]) => (value === "" ? key : `${key}="${escapeAttr(value)}"`)).join(" ");
  const open = `<${node.localName}${attrs ? " " + attrs : ""}>`;
  if (VOID_ELEMENTS.has(node.localName)) return open;
  const raw = RAW_TEXT_ELEMENTS.has(node.localName);
  return open + node.childNodes.map((child) => (raw && child.nodeType === 3 ? child.data : serializeNode(child))).join("") + `</${node.localName}>`;
}
function rawText(text) { return String(text); }
function escapeAttr(value) { return String(value).replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;"); }

function parseFragment(doc, html) {
  const fragment = new Element(doc, "#fragment");
  parseInto(doc, fragment, String(html || ""));
  return fragment;
}

const AUTO_CLOSE = {
  li: ["li"],
  dt: ["dt", "dd"], dd: ["dt", "dd"],
  td: ["td", "th"], th: ["td", "th"],
  tr: ["tr"],
  tbody: ["tbody", "tr", "td", "th"], thead: ["thead", "tr", "td", "th"], tfoot: ["tfoot", "tr", "td", "th"],
  option: ["option"],
  optgroup: ["option", "optgroup"],
};
const BLOCK_START = new Set([
  "address", "article", "aside", "blockquote", "details", "dialog", "div", "dl", "fieldset",
  "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6", "header",
  "hgroup", "hr", "main", "nav", "ol", "p", "pre", "section", "table", "ul",
]);
function autoCloseForStart(stack, tag) {
  const auto = AUTO_CLOSE[tag];
  if (auto) {
    while (stack.length > 1) {
      const top = stack[stack.length - 1].localName;
      if (auto.includes(top)) { stack.pop(); continue; }
      if ((tag === "td" || tag === "th") && (top === "td" || top === "th")) { stack.pop(); continue; }
      break;
    }
  }
  if (tag === "tr") {
    while (stack.length > 1 && ["tr", "td", "th"].includes(stack[stack.length - 1].localName)) stack.pop();
  }
  if (tag === "p" || BLOCK_START.has(tag)) {
    while (stack.length > 1 && stack[stack.length - 1].localName === "p") stack.pop();
  }
}
function parseInto(doc, root, html) {
  const stack = [root];
  const tagRe = /<!--[\s\S]*?-->|<!\[CDATA\[[\s\S]*?\]\]>|<!DOCTYPE[^>]*>|<\/\s*([A-Za-z][\w:-]*)\s*>|<([A-Za-z][\w:-]*)((?:"[^"]*"|'[^']*'|[^>"'])*?)(\/?)>|([^<]+)/g;
  let match;
  while ((match = tagRe.exec(html)) !== null) {
    const current = stack[stack.length - 1];
    if (match[2]) {
      const tag = match[2].toLowerCase();
      const local = tag.indexOf(":") >= 0 ? tag.split(":").pop() : tag;
      autoCloseForStart(stack, local);
      const element = new Element(doc, local);
      if (tag.indexOf(":") >= 0) element.namespaceURI = tag.split(":").shift();
      parseAttributes(element, match[3] || "");
      stack[stack.length - 1].appendChild(element);
      if (!match[4] && !VOID_ELEMENTS.has(local)) {
        if (RAW_TEXT_ELEMENTS.has(local)) {
          const start = tagRe.lastIndex;
          const endRe = new RegExp("</\\s*" + local + "\\s*>", "i");
          const rest = html.slice(start);
          const end = rest.search(endRe);
          const text = end < 0 ? rest : rest.slice(0, end);
          if (text) element.appendChild(new TextNode(doc, text));
          if (end >= 0) {
            const close = rest.slice(end).match(endRe)[0];
            tagRe.lastIndex = start + end + close.length;
          } else {
            tagRe.lastIndex = html.length;
          }
        } else {
          stack.push(element);
        }
      }
    } else if (match[1]) {
      const tag = match[1].toLowerCase();
      let matched = -1;
      for (let index = stack.length - 1; index > 0; index -= 1) {
        if (stack[index].localName === tag) { matched = index; break; }
        if (stack[index].localName === "#fragment" && tag === "body") { matched = index; break; }
      }
      if (matched > 0) stack.length = matched;
    } else if (match[5] !== undefined) {
      if (match[5]) current.appendChild(new TextNode(doc, match[5]));
    }
  }
}

function parseAttributes(element, raw) {
  const re = /([^\s=\/>]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?/g;
  let match;
  while ((match = re.exec(raw || "")) !== null) {
    const name = match[1];
    if (!name || name === "/") continue;
    const value = match[2] !== undefined ? match[2] : match[3] !== undefined ? match[3] : match[4] !== undefined ? match[4] : "";
    element.setAttribute(name, value);
  }
}

// --- selector engine -------------------------------------------------------
function splitSelectorList(selector) {
  const out = [];
  let depth = 0, current = "";
  for (const char of String(selector || "")) {
    if (char === "[" || char === "(") depth += 1;
    if (char === "]" || char === ")") depth -= 1;
    if (char === "," && depth === 0) { out.push(current.trim()); current = ""; continue; }
    current += char;
  }
  if (current.trim()) out.push(current.trim());
  return out.filter(Boolean);
}

const tokenCache = new Map();
function parseSelector(selector) {
  if (tokenCache.has(selector)) return tokenCache.get(selector);
  const list = splitSelectorList(selector).map(parseComplex);
  tokenCache.set(selector, list);
  return list;
}
function parseComplex(part) {
  const tokens = String(part || "").trim().split(/\s+/).filter(Boolean);
  return tokens.map(parseToken);
}
function parseToken(text) {
  const token = { tag: null, id: null, classes: [], attrs: [], pseudos: [], combinator: " " };
  let rest = String(text || "");
  if (rest.startsWith(">")) { token.combinator = ">"; rest = rest.slice(1); }
  const matchTag = rest.match(/^[A-Za-z][\w:-]*/);
  if (matchTag) { token.tag = matchTag[0].toLowerCase(); rest = rest.slice(matchTag[0].length); }
  const partRe = /(#[\w-]+|\.[\w-]+|\[[^\]]+\]|:not\([^)]*\)|:[\w-]+(?:\([^)]*\))?)/g;
  let m;
  while ((m = partRe.exec(rest)) !== null) {
    const chunk = m[0];
    if (chunk.startsWith("#")) token.id = chunk.slice(1);
    else if (chunk.startsWith(".")) token.classes.push(chunk.slice(1));
    else if (chunk.startsWith("[")) token.attrs.push(parseAttrSelector(chunk.slice(1, -1)));
    else token.pseudos.push(chunk);
  }
  return token;
}
function parseAttrSelector(body) {
  const match = String(body).match(/^([^\s~|^$*!=]+)\s*(?:([~|^$*]?=)\s*(?:"([^"]*)"|'([^']*)'|([^\s\]]+)))?$/);
  if (!match) return { name: String(body).trim(), op: null, value: null };
  return {
    name: match[1].toLowerCase(),
    op: match[2] || null,
    value: match[3] !== undefined ? match[3] : match[4] !== undefined ? match[4] : match[5] !== undefined ? match[5] : null,
  };
}
function attrMatches(element, spec) {
  const actual = element.getAttribute(spec.name);
  if (actual === null) return false;
  if (!spec.op) return true;
  const value = spec.value === null ? "" : spec.value;
  switch (spec.op) {
    case "=": return actual === value;
    case "~=": return actual.split(/\s+/).includes(value);
    case "|=": return actual === value || actual.startsWith(value + "-");
    case "^=": return actual.startsWith(value);
    case "$=": return actual.endsWith(value);
    case "*=": return actual.includes(value);
    default: return false;
  }
}
function tokenMatches(element, token) {
  if (!element || element.nodeType !== 1) return false;
  if (token.tag && element.localName !== token.tag) return false;
  if (token.id && element.getAttribute("id") !== token.id) return false;
  for (const cls of token.classes) if (!element.classList.contains(cls)) return false;
  for (const attr of token.attrs) if (!attrMatches(element, attr)) return false;
  for (const pseudo of token.pseudos) {
    if (pseudo === ":first-child") { if (element.previousElementSibling) return false; }
    else if (pseudo === ":last-child") { if (element.nextElementSibling) return false; }
    else if (pseudo === ":checked") { if (!element.checked) return false; }
    else if (pseudo === ":disabled") { if (!element.disabled) return false; }
    else if (pseudo === ":enabled") { if (element.disabled) return false; }
    else if (pseudo.startsWith(":not(")) {
      const inner = pseudo.slice(5, -1).trim();
      if (matchesOne(element, inner, element.ownerDocument)) return false;
    } else if (pseudo.startsWith(":nth-child(")) {
      const raw = pseudo.slice(11, -1).trim();
      const index = element.parentNode ? element.parentNode.children.indexOf(element) + 1 : 0;
      if (raw === "odd" && index % 2 !== 1) return false;
      else if (raw === "even" && index % 2 !== 0) return false;
      else if (/^\d+$/.test(raw) && index !== Number(raw)) return false;
    } else {
      // unsupported pseudo: treat as non-matching to surface selector use in errors
    }
  }
  return true;
}
function matchesComplex(element, tokens) {
  let index = tokens.length - 1;
  if (!tokenMatches(element, tokens[index])) return false;
  let node = element;
  index -= 1;
  while (index >= 0) {
    const token = tokens[index];
    if (token.combinator === ">") {
      node = node.parentElement;
      if (!node || !tokenMatches(node, token)) return false;
    } else {
      let ancestor = node.parentElement;
      let found = null;
      while (ancestor) {
        if (tokenMatches(ancestor, token)) { found = ancestor; break; }
        ancestor = ancestor.parentElement;
      }
      if (!found) return false;
      node = found;
    }
    index -= 1;
  }
  return true;
}
function matchesOne(element, selector, doc) {
  if (!element || element.nodeType !== 1) return false;
  let list;
  try { list = parseSelector(selector); } catch (error) { return false; }
  for (const tokens of list) if (matchesComplex(element, tokens)) return true;
  return false;
}
function queryAll(root, selector, doc, descendantsOnly) {
  const list = [];
  if (!selector) return list;
  let parsed;
  try { parsed = parseSelector(selector); } catch (error) { return list; }
  const visit = (node) => {
    if (node.nodeType === 1) {
      if (!descendantsOnly || node !== root) {
        for (const tokens of parsed) { if (matchesComplex(node, tokens)) { list.push(node); break; } }
      }
      for (const child of node.childNodes) visit(child);
    }
  };
  if (root.nodeType === 11 || root.localName === "#fragment") for (const child of root.childNodes) visit(child);
  else visit(root);
  if (!descendantsOnly) return list;
  const selfMatches = parsed.some((tokens) => matchesComplex(root, tokens));
  if (selfMatches) list.unshift(root);
  return list;
}

// ---------------------------------------------------------------------------
// Document / Page
// ---------------------------------------------------------------------------
class Document {
  constructor(page) {
    this._page = page;
    this._idMap = new Map();
    this.nodeType = 9;
    this.ownerDocument = null;
    this.readyState = "complete";
    this.activeElement = null;
    this._listeners = new Map();
    this._writes = [];
    this._written = null;
    this.title = "";
    this.currentScript = null;
    const html = new Element(this, "html");
    this.documentElement = html;
    this.appendChild = (child) => { /* document.appendChild unsupported */ };
    this.head = new Element(this, "head");
    this.body = new Element(this, "body");
    html.appendChild(this.head);
    html.appendChild(this.body);
  }
  get nodeName() { return "#document"; }
  get childNodes() { return [this.documentElement]; }
  get firstChild() { return this.documentElement; }
  get children() { return [this.documentElement]; }
  createElement(tag) { return new Element(this, tag); }
  createElementNS(ns, tag) { const element = new Element(this, tag); element.namespaceURI = ns || element.namespaceURI; return element; }
  createTextNode(text) { return new TextNode(this, text); }
  createDocumentFragment() { return new Element(this, "#fragment"); }
  getElementById(id) {
    return this._idMap.get(String(id)) || null;
  }
  querySelector(selector) { return queryAll(this.documentElement, selector, this, false)[0] || null; }
  querySelectorAll(selector) { return queryAll(this.documentElement, selector, this, false); }
  getElementsByTagName(tag) { return this.querySelectorAll(String(tag)); }
  getElementsByClassName(names) { return this.querySelectorAll(String(names).split(/\s+/).filter(Boolean).map((n) => "." + n).join("")); }
  addEventListener(type, handler) {
    const key = String(type);
    if (!this._listeners.has(key)) this._listeners.set(key, []);
    this._listeners.get(key).push({ handler });
  }
  removeEventListener(type, handler) {
    const list = this._listeners.get(String(type));
    if (list) this._listeners.set(String(type), list.filter((e) => e.handler !== handler));
  }
  dispatchEvent(event) {
    const type = String(event && event.type || "");
    const list = this._listeners.get(type) || [];
    for (const entry of list.slice()) {
      try { entry.handler.call(this, event); } catch (error) { this._page.errors.push({ where: "document:" + type, message: String(error && error.stack || error) }); }
    }
    return true;
  }
  open() { this._writes = []; this._written = null; }
  write(chunk) { this._writes.push(String(chunk == null ? "" : chunk)); }
  writeln(chunk) { this.write(String(chunk == null ? "" : chunk) + "\n"); }
  close() { this._written = this._writes.join(""); this._page.writtenHtml = this._written; this._page.writeCount += 1; }
  get cookie() { return ""; }
  set cookie(_value) {}
  execCommand() { return false; }
  createRange() { return { selectNodeContents() {}, setStart() {}, setEnd() {}, toString() { return ""; } }; }
}
function makeCanvasContext() {
  const target = {
    canvas: null,
    measureText: () => ({ width: 0 }),
    createLinearGradient: () => ({ addColorStop() {} }),
    createRadialGradient: () => ({ addColorStop() {} }),
    getImageData: () => ({ data: [] }),
    putImageData: () => {},
    setLineDash: () => {},
    save: () => {}, restore: () => {},
  };
  return new Proxy(target, {
    get(obj, key) {
      if (key in obj) return obj[key];
      if (typeof key === "string" && /^[A-Z]/.test(key)) return () => {};
      return obj[key];
    },
    set(obj, key, value) { obj[key] = value; return true; },
  });
}

function makeFileReader(page) {
  return class FileReader {
    constructor() { this.result = null; this.onload = null; this.onerror = null; this.onloadend = null; }
    _finish(text, error) {
      if (error) { this.error = error; if (typeof this.onerror === "function") this.onerror({ target: this }); }
      else { this.result = text; if (typeof this.onload === "function") this.onload({ target: this }); }
      if (typeof this.onloadend === "function") this.onloadend({ target: this });
      page.pendingReaders = Math.max(0, page.pendingReaders - 1);
    }
    readAsText(blob) {
      page.pendingReaders += 1;
      const resolveText = blob && typeof blob.text === "function" ? blob.text() : Promise.resolve(String(blob || ""));
      resolveText.then((text) => setTimeout(() => this._finish(text), 0)).catch((error) => setTimeout(() => this._finish(null, error), 0));
    }
    readAsDataURL(blob) {
      page.pendingReaders += 1;
      const text = "data:application/json;base64," + Buffer.from(String((blob && blob.__content) || "")).toString("base64");
      setTimeout(() => this._finish(text), 0);
    }
    readAsArrayBuffer(blob) {
      page.pendingReaders += 1;
      const data = Buffer.from(String((blob && blob.__content) || ""));
      setTimeout(() => { this.result = data.buffer.slice(data.byteOffset, data.byteOffset + data.byteLength); this._finish(null); }, 0);
    }
    abort() {}
  };
}

function makeFileClass() {
  return class File {
    constructor(parts, name, options) {
      this.name = String(name || "file");
      this.type = String((options && options.type) || "");
      this.__content = (parts || []).map((p) => String(p)).join("");
      this.size = this.__content.length;
      this.lastModified = Date.now();
    }
    text() { return Promise.resolve(this.__content); }
    arrayBuffer() { const buf = Buffer.from(this.__content); return Promise.resolve(buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength)); }
  };
}

function makeDataTransfer() {
  return class DataTransfer {
    constructor() { this._files = []; this.items = { add: (file) => { this._files.push(file); } }; }
    get files() { return this._files; }
  };
}

// ---------------------------------------------------------------------------
// Page construction / script execution
// ---------------------------------------------------------------------------
function makeStorage(seed) {
  const data = new Map(Object.entries(seed || {}));
  return {
    getItem: (key) => (data.has(String(key)) ? data.get(String(key)) : null),
    setItem: (key, value) => { data.set(String(key), String(value)); },
    removeItem: (key) => { data.delete(String(key)); },
    clear: () => data.clear(),
    key: (index) => Array.from(data.keys())[Number(index)] || null,
    get length() { return data.size; },
    _dump: () => Object.fromEntries(data.entries()),
  };
}

function buildPage(state, name, config) {
  const page = {
    name,
    errors: [],
    scripts: [],
    embedded: {},
    fetchCallsAtBuild: state.fetchCalls.length,
    pendingReaders: 0,
    writeCount: 0,
    writtenHtml: null,
    lastWritten: null,
    downloads: [],
    blobStore: {},
    console: [],
    timers: [],
  };
  const html = fs.readFileSync(config.html, "utf8").replace(/^\uFEFF/, "");
  page.htmlPath = config.html;
  page.htmlSha256 = sha256(html);
  const document = new Document(page);
  page.document = document;
  const scriptElements = [];
  const originalCreate = document.createElement.bind(document);
  document.createElement = (tag) => originalCreate(tag);
  // Parse the real document into our DOM.
  const rootEl = new Element(document, "#document-fragment");
  parseInto(document, rootEl, html);
  // Move parsed children into the document's html element.
  for (const child of rootEl.childNodes.slice()) {
    if (child.nodeType === 1 && child.localName === "html") {
      document.documentElement = child;
      child.parentNode = null;
      for (const nested of child.childNodes.slice()) if (nested.localName === "head") document.head = nested;
      for (const nested of child.childNodes.slice()) if (nested.localName === "body") document.body = nested;
      break;
    }
  }
  if (!document.documentElement || document.documentElement.localName !== "html") {
    document.documentElement = originalCreate("html");
  }
  // Normalize form-control defaults the way a browser's parser would.
  collectNodes(document.documentElement).forEach((node) => {
    if (node.localName === "select") {
      const options = node.options;
      if (options.length && !options.some((option) => option._selected === true || option.getAttribute("selected") !== null)) {
        options[0]._selected = true;
        node._selectedIndex = 0;
      }
    }
    if (node.localName === "input") {
      if (node.hasAttribute("value")) node._value = node.getAttribute("value");
      else if (String(node.getAttribute("type") || "") === "checkbox" || String(node.getAttribute("type") || "") === "radio") node._value = "on";
      if (node.hasAttribute("checked")) node.checked = true;
    }
    if (node.localName === "textarea") node._value = node.textContent;
  });
  // Collect embedded JSON + scripts; register ids.
  collectNodes(document.documentElement).forEach((node) => {
    if (node.getAttribute("id")) document._idMap.set(node.getAttribute("id"), node);
    if (node.localName === "script") {
      const type = String(node.getAttribute("type") || "").toLowerCase();
      const id = node.getAttribute("id");
      const body = node.textContent;
      if (type.includes("json")) {
        if (id) { try { page.embedded[id] = JSON.parse(body); } catch (error) { page.errors.push({ where: "embedded:" + id, message: String(error) }); } }
      } else if (body && body.trim()) {
        scriptElements.push({ id: id || "", body, attrs: node._attrs });
      }
    }
  });
  if (!document.body) document.body = originalCreate("body");
  if (!document.head) document.head = originalCreate("head");

  // Bindings override (used for negative client tests; does not alter files).
  if (config.bindingsPatch && page.embedded["le-recalc-bindings"]) {
    page.embedded["le-recalc-bindings"] = deepMerge(page.embedded["le-recalc-bindings"], config.bindingsPatch);
    const node = document._idMap.get("le-recalc-bindings");
    if (node) node.textContent = JSON.stringify(page.embedded["le-recalc-bindings"]);
  }
  // Endpoint override for file:// / explicit-endpoint cases.
  if (config.endpoint && page.embedded["le-recalc-bindings"]) {
    const bindings = page.embedded["le-recalc-bindings"];
    if (bindings && typeof bindings === "object") {
      bindings.recompute_endpoint = String(config.endpoint);
      const node = document._idMap.get("le-recalc-bindings");
      if (node) node.textContent = JSON.stringify(bindings);
    }
  }

  // --- VM context ---------------------------------------------------------
  const locationUrl = new URL(
    config.protocol === "file"
      ? "file:///" + String(config.html).replace(/\\/g, "/").replace(/^\/+/, "")
      : (config.servedUrl || "http://127.0.0.1:1/")
  );
  const window = {
    document,
    location: {
      protocol: String(config.protocol || "file") + ":",
      href: locationUrl.href,
      origin: String(config.protocol || "file") === "file" ? "null" : locationUrl.origin,
      host: locationUrl.host,
      hostname: locationUrl.hostname,
      port: locationUrl.port,
      pathname: locationUrl.pathname,
      search: "",
      hash: "",
      assign() {}, replace() {}, reload() {},
    },
    crypto: { randomUUID: () => crypto.randomUUID(), getRandomValues: (arr) => crypto.getRandomValues(arr) },
    navigator: { userAgent: "p3r-acceptance-harness", language: "zh-CN", clipboard: { writeText: async () => {} } },
    console: { log: (...a) => page.console.push(["log", ...a.map(String)]), warn: (...a) => page.console.push(["warn", ...a.map(String)]), error: (...a) => page.console.push(["error", ...a.map(String)]), info: (...a) => page.console.push(["info", ...a.map(String)]), debug: () => {} },
    setTimeout: (fn, ms, ...args) => { const id = setTimeout(() => { try { fn(...args); } catch (e) { page.errors.push({ where: "timer", message: String(e && e.stack || e) }); } }, Math.min(Number(ms) || 0, 50)); page.timers.push(id); return id; },
    clearTimeout: (id) => clearTimeout(id),
    setInterval: () => 0,
    clearInterval: () => {},
    requestAnimationFrame: (fn) => { const id = setTimeout(() => { try { fn(Date.now()); } catch (e) { page.errors.push({ where: "raf", message: String(e && e.stack || e) }); } }, 0); page.timers.push(id); return id; },
    cancelAnimationFrame: (id) => clearTimeout(id),
    queueMicrotask: (fn) => queueMicrotask(fn),
    fetch: (url, options) => stateFetch(state, page, url, options),
    FileReader: makeFileReader(page),
    File: makeFileClass(),
    Blob: makeBlobClass(),
    DataTransfer: makeDataTransfer(),
    URL: Object.assign(function () {}, {
      createObjectURL: (blob) => {
        const key = "blob:p3r-" + sha256(String((blob && blob.__content) || "")).slice(0, 12);
        page.blobStore[key] = String((blob && blob.__content) || "");
        return key;
      },
      revokeObjectURL: () => {},
    }),
    TextEncoder: class { encode(text) { return new Uint8Array(Buffer.from(String(text), "utf8")); } },
    TextDecoder: class { decode(bytes) { return Buffer.from(bytes || []).toString("utf8"); } },
    atob: (text) => Buffer.from(String(text), "base64").toString("binary"),
    btoa: (text) => Buffer.from(String(text), "binary").toString("base64"),
    performance: { now: () => Date.now() },
    matchMedia: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }),
    getComputedStyle: () => ({ getPropertyValue: () => "", display: "block", visibility: "visible" }),
    alert: () => {}, confirm: () => true, prompt: () => null,
    addEventListener: (type, handler) => { if (!window._listeners) window._listeners = {}; (window._listeners[String(type)] ||= []).push(handler); },
    removeEventListener: () => {},
    dispatchEvent: () => true,
    downloadRecords: [],
    history: { state: null, length: 1, replaceState() {}, pushState() {}, back() {}, forward() {}, go() {} },
    localStorage: makeStorage((config.storageSeed || {}).localStorage || {}),
    sessionStorage: makeStorage((config.storageSeed || {}).sessionStorage || {}),
    innerWidth: 1280,
    innerHeight: 800,
    devicePixelRatio: 1,
    pageXOffset: 0,
    pageYOffset: 0,
    screen: { width: 1280, height: 800 },
    getSelection: () => ({ removeAllRanges() {}, addRange() {}, toString: () => "" }),
    MutationObserver: class { constructor() {} observe() {} disconnect() {} takeRecords() { return []; } },
    IntersectionObserver: class { constructor() {} observe() {} disconnect() {} unobserve() {} },
    ResizeObserver: class { constructor() {} observe() {} disconnect() {} unobserve() {} },
    __p3r: null,
  };
  window.window = window;
  window.self = window;
  window.globalThis = window;
  window.top = window;
  window.parent = window;
  window.HTMLElement = Element;
  window.Element = Element;
  window.Node = { ELEMENT_NODE: 1, TEXT_NODE: 3 };
  window.CSS = { escape: (v) => String(v) };
  window.scrollTo = () => {};
  window.scroll = () => {};
  window.open = () => null;
  window.URLSearchParams = URLSearchParams;
  window.structuredClone = (v) => JSON.parse(JSON.stringify(v));
  window.LeRecalc = undefined;
  document.defaultView = window;
  page.window = window;

  const sandbox = window;
  const context = vm.createContext(sandbox);
  page.context = context;

  // Execute the shared recompute client, then the shared report actions
  // (D10 config envelope/status helper), then the model interaction script(s).
  // Definitions use ``window.X =``; model scripts only reference them.
  const defines = (p, name) => new RegExp("window\\." + name + "\\s*=").test(p.body);
  const recalcScripts = scriptElements.filter((s) => defines(s, "LeRecalc") || s.body.includes("Shared local-recompute client"));
  const actionScripts = scriptElements.filter(
    (s) => !recalcScripts.includes(s) && (defines(s, "ReportActions") || s.body.includes("D10 shared report actions"))
  );
  const shellScripts = scriptElements.filter(
    (s) => !recalcScripts.includes(s) && !actionScripts.includes(s) && !s.body.includes("createController(")
      && (s.body.includes("Shared player-report chrome") || (s.body.includes("data-page-btn") && s.body.includes("page-tab")))
  );
  const modelScripts = scriptElements.filter(
    (s) => !recalcScripts.includes(s) && !actionScripts.includes(s) && !shellScripts.includes(s)
      && s.body.includes("createController(")
  );
  const executed = [];
  const runScript = (entry) => {
    try {
      vm.runInContext(entry.body, context, { filename: (entry.id || "inline") + ".js" });
      executed.push({ id: entry.id, chars: entry.body.length });
      return true;
    } catch (error) {
      page.errors.push({ where: "script:" + (entry.id || "inline"), message: String(error && error.stack || error) });
      return false;
    }
  };
  const order = recalcScripts.concat(actionScripts, shellScripts, modelScripts);
  if (config.runAllScripts) {
    order.push(...scriptElements.filter((s) => !order.includes(s)));
  } else {
    // Execute a script only when it has no controller and contains model-ish
    // hooks? Keep bootstrap out: it only mutates the offline notice.
  }
  for (const entry of order) runScript(entry);
  // Chrome normally fires DOMContentLoaded / load after parsing.
  try { document.dispatchEvent({ type: "DOMContentLoaded", target: document }); } catch (e) { page.errors.push({ where: "domcontentloaded", message: String(e) }); }
  try { window.dispatchEvent && window.dispatchEvent({ type: "load" }); } catch (e) { page.errors.push({ where: "load", message: String(e) }); }
  page.loadFetchCount = state.fetchCalls.length - page.fetchCallsAtBuild;
  page.scriptsExecuted = executed;
  page.hasLeRecalc = !!window.LeRecalc;
  page.hasReportActions = !!window.ReportActions;
  state.pages[name] = page;
  return page;
}

function makeBlobClass() {
  return class Blob {
    constructor(parts, options) { this.__content = (parts || []).map((p) => String(p)).join(""); this.type = String((options && options.type) || ""); this.size = this.__content.length; }
    text() { return Promise.resolve(this.__content); }
    arrayBuffer() { const buf = Buffer.from(this.__content); return Promise.resolve(buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength)); }
  };
}

function deepMerge(base, patch) {
  if (!patch || typeof patch !== "object" || Array.isArray(patch)) return patch === undefined ? base : patch;
  const out = Object.assign({}, base && typeof base === "object" ? base : {});
  for (const [key, value] of Object.entries(patch)) {
    if (value === null) { delete out[key]; continue; }
    if (value && typeof value === "object" && !Array.isArray(value) && typeof out[key] === "object" && !Array.isArray(out[key])) out[key] = deepMerge(out[key], value);
    else out[key] = value;
  }
  return out;
}

function getPath(obj, dotted) {
  let current = obj;
  for (const part of String(dotted).split(".")) {
    if (current == null) return undefined;
    if (Array.isArray(current) && /^\d+$/.test(part)) current = current[Number(part)];
    else current = current[part];
  }
  return current;
}
function setPath(obj, dotted, value) {
  const parts = String(dotted).split(".");
  let current = obj;
  for (let index = 0; index < parts.length - 1; index += 1) {
    const part = parts[index];
    const next = parts[index + 1];
    if (current[part] == null) current[part] = /^\d+$/.test(next) ? [] : {};
    current = current[part];
  }
  current[parts[parts.length - 1]] = value;
}
function deletePath(obj, dotted) {
  const parts = String(dotted).split(".");
  let current = obj;
  for (let index = 0; index < parts.length - 1; index += 1) { current = current && current[parts[index]]; if (current == null) return; }
  if (current && typeof current === "object") delete current[parts[parts.length - 1]];
}

// ---------------------------------------------------------------------------
// Fetch adapter
// ---------------------------------------------------------------------------
let deferredQueue = [];
function makeResponse(text, status) {
  return {
    status,
    ok: status >= 200 && status < 300,
    headers: { get: () => "application/json" },
    text: async () => String(text),
    json: async () => JSON.parse(String(text)),
  };
}
function stateFetch(state, page, url, options) {
  const requestUrl = String(url || "");
  const rawBody = options && options.body ? String(options.body) : "";
  let body = null;
  try { body = rawBody ? JSON.parse(rawBody) : null; } catch (error) { body = rawBody; }
  const call = { page: page.name, url: requestUrl, body, at: Date.now(), mode: state.currentFetch ? state.currentFetch.mode : "real" };
  state.fetchCalls.push(call);
  const behavior = Object.assign({ mode: "real" }, state.currentFetch || {});
  state.currentFetch = null;
  page.pendingFetches = (page.pendingFetches || 0) + 1;
  const finish = (result) => {
    page.pendingFetches = Math.max(0, page.pendingFetches - 1);
    state.fetchResults.push(Object.assign({ page: page.name, url: requestUrl, body, mode: behavior.mode, at: Date.now() }, result));
  };
  return (async () => {
    try {
      if (behavior.mode === "hold") {
        await new Promise((resolve) => { state.holds.push(resolve); page.holds = (page.holds || []); page.holds.push(resolve); });
      }
      if (behavior.mode === "fail") throw new Error(behavior.message || "本机服务连接失败");
      const target = behavior.url || requestUrl;
      const init = {
        method: (options && options.method) || "POST",
        headers: Object.assign({ "Content-Type": "application/json" }, (options && options.headers) || {}),
        body: rawBody || undefined,
      };
      let response;
      try {
        response = await fetch(target, init);
      } catch (error) {
        throw new Error("本机服务连接失败：" + String(error && error.message || error));
      }
      const text = await response.text();
      let finalText = text;
      if (behavior.mode === "mutate" || behavior.mutate) {
        let payload;
        try { payload = JSON.parse(text); } catch (error) { payload = text; }
        if (payload && typeof payload === "object") {
          for (const [dotted, value] of Object.entries(behavior.mutate.set || {})) setPath(payload, dotted, value);
          for (const dotted of behavior.mutate.delete || []) deletePath(payload, dotted);
          if (Object.prototype.hasOwnProperty.call(behavior.mutate, "raw")) payload = behavior.mutate.raw;
          finalText = typeof payload === "string" ? payload : JSON.stringify(payload);
        }
      }
      let status = response.status;
      if (behavior.status) status = behavior.status;
      let payloadSummary = null;
      try {
        const parsed = JSON.parse(finalText);
        if (parsed && typeof parsed === "object") {
          payloadSummary = {
            schema_version: parsed.schema_version,
            request_id: parsed.request_id,
            accepted: parsed.accepted,
            issues: parsed.issues,
            error: parsed.error,
            status: parsed.status,
            html_chars: typeof parsed.html === "string" ? parsed.html.length : null,
          };
        }
      } catch (error) { payloadSummary = { nonJson: true, chars: finalText.length }; }
      const finalResponse = makeResponse(finalText, status);
      finish({ ok: true, status, mutated: !!(behavior.mode === "mutate" || behavior.mutate), payload: payloadSummary });
      return finalResponse;
    } catch (error) {
      finish({ ok: false, error: String(error && error.message || error) });
      throw error;
    }
  })();
}

// ---------------------------------------------------------------------------
// Action interpreter
// ---------------------------------------------------------------------------
function $(page, selector) {
  const node = page.document.querySelector(selector);
  if (!node) throw new Error("selector not found: " + selector);
  return node;
}
function dispatch(page, element, types) {
  for (const type of types) {
    const event = { type, target: element, bubbles: true, cancelable: true };
    if (type === "input" || type === "change") {
      // Event constructors are not needed by the report scripts; plain objects suffice.
    }
    element.dispatchEvent(event);
  }
}
function setElementValue(page, selector, value, checked, dispatchTypes) {
  const element = $(page, selector);
  if (checked !== undefined && checked !== null) element.checked = !!checked;
  if (value !== undefined && value !== null) element.value = String(value);
  dispatch(page, element, dispatchTypes);
  return { selector, tag: element.localName, value: element.value, checked: element.checked };
}
function loadConfig(page, selector, content, filename) {
  const element = $(page, selector);
  const file = new (page.window.File)([content], filename || "scenario.json", { type: "application/json" });
  const transfer = new (page.window.DataTransfer)();
  transfer.items.add(file);
  try { element.files = transfer.files; } catch (error) { element.files = transfer.files; }
  dispatch(page, element, ["change"]);
  return { selector, size: content.length };
}
function snapshot(page, state, name, resultKeys) {
  const controls = [];
  const selector = "[data-cond][data-state],[data-cond-hits],[data-cond-preset],[data-cond-level],[data-cond-target],[data-cond-fire-res],[data-cond-compute],[data-apply-scenario],[data-cond-load],[data-cond-save],[data-cond-reset],[data-cond-live-recompute],[data-scenario-level],[data-scenario-fire-res],[data-state]";
  const seen = new Set();
  for (const node of page.document.querySelectorAll(selector)) {
    if (seen.has(node)) continue;
    seen.add(node);
    const attrs = {};
    for (const key of node.getAttributeNames()) if (key.startsWith("data-") || key === "id" || key === "name" || key === "type" || key === "disabled" || key === "checked" || key === "class") attrs[key] = node.getAttribute(key);
    const chain = [];
    let cur = node;
    while (cur && cur.nodeType === 1 && chain.length < 4) { chain.unshift(cur.localName + (cur.getAttribute("id") ? "#" + cur.getAttribute("id") : "")); cur = cur.parentNode; }
    controls.push({
      selectorTag: node.localName,
      parentChain: chain.join(" > "),
      inPageBuffs: !!node.closest("#page-buffs"),
      id: node.getAttribute("id") || "",
      attrs,
      value: node.localName === "select" || node.localName === "input" ? node.value : undefined,
      checked: node.localName === "input" ? !!node.checked : undefined,
      disabled: !!node.disabled,
      text: node.localName === "select" ? undefined : node.textContent.slice(0, 400),
    });
  }
  const statuses = [];
  for (const node of page.document.querySelectorAll("[data-cond-status],[data-cond-live-status],[data-cond-status-text],[data-recalc-status],[data-cond-import-status],[data-cond-import-error],[data-cond-changed],[data-cond-status-for],[data-scenario-error],[data-status]")) {
    statuses.push({ tag: node.localName, attrs: Object.fromEntries(node.getAttributeNames().map((k) => [k, node.getAttribute(k)])), text: node.textContent.slice(0, 600), hidden: !!node.hidden });
  }
  const result = {};
  const resultKeysToUse = resultKeys || ["single_hit", "dps", "hits_per_cast", "scenario", "baseline_profile", "single_cast", "cast", "loops", "monster_dr", "consistency"];
  for (const id of Object.keys(page.embedded)) {
    const payload = page.embedded[id];
    if (id === "result-data" || id === "hit-result-json") {
      result[id] = {};
      if (payload && typeof payload === "object") for (const key of resultKeysToUse) if (Object.prototype.hasOwnProperty.call(payload, key)) result[id][key] = payload[key];
    } else {
      result[id] = payload;
    }
  }
  const activePageButton = page.document.querySelector("[data-page-btn].is-active,[data-page-btn][aria-selected=\"true\"]");
  const activeWindowButton = page.document.querySelector("[data-window-btn].is-active,[data-window-btn][aria-selected=\"true\"]");
  const activePolicyButton = page.document.querySelector("[data-policy-btn].is-active,[data-policy-btn][aria-selected=\"true\"]");
  const bodyAttrs = {};
  if (page.document.body) {
    for (const key of page.document.body.getAttributeNames()) bodyAttrs[key] = page.document.body.getAttribute(key);
  }
  const uiState = {
    body: bodyAttrs,
    activePage: activePageButton ? activePageButton.getAttribute("data-page-btn") : null,
    activeWindow: activeWindowButton ? activeWindowButton.getAttribute("data-window-btn") : null,
    activePolicy: activePolicyButton ? activePolicyButton.getAttribute("data-policy-btn") : null,
    statusStates: (page.document.querySelectorAll("[data-status-state]") || []).map
      ? Array.from(page.document.querySelectorAll("[data-status-state]")).map((node) => ({
          state: node.getAttribute("data-status-state"),
          text: node.textContent.slice(0, 160),
        }))
      : [],
  };
  return {
    name,
    takenAt: new Date().toISOString(),
    page: page.name,
    uiState,
    htmlSha256: page.htmlSha256,
    controls,
    statuses,
    embedded: result,
    bindings: page.embedded["le-recalc-bindings"] || null,
    capabilities: page.embedded["report-capabilities"] || null,
    writeCount: page.writeCount,
    writtenChars: page.writtenHtml ? page.writtenHtml.length : 0,
    writtenSha256: page.writtenHtml ? sha256(page.writtenHtml) : null,
    requests: JSON.parse(JSON.stringify(state.fetchCalls.filter((call) => call.page === page.name))),
    responses: JSON.parse(JSON.stringify(state.fetchResults.filter((call) => call.page === page.name))),
    errors: page.errors.slice(),
    scriptsExecuted: page.scriptsExecuted,
    hasLeRecalc: page.hasLeRecalc,
    downloads: page.downloads.slice(),
    windowState: { writtenCount: page.writeCount, pendingFetches: page.pendingFetches || 0, pendingReaders: page.pendingReaders || 0 },
  };
}
async function waitIdle(state, timeoutMs) {
  const deadline = Date.now() + (timeoutMs || 15000);
  while (Date.now() < deadline) {
    const pending = Object.values(state.pages).reduce((sum, page) => sum + (page.pendingFetches || 0) + (page.pendingReaders || 0), 0);
    if (pending === 0) {
      await new Promise((resolve) => setTimeout(resolve, 10));
      const again = Object.values(state.pages).reduce((sum, page) => sum + (page.pendingFetches || 0) + (page.pendingReaders || 0), 0);
      if (again === 0) return { ok: true };
    }
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
  return { ok: false, error: "wait_idle timeout" };
}

async function runStep(state, step) {
  const pageName = step.page || "main";
  let page = state.pages[pageName];
  const op = String(step.op || "");
  if (op === "reload_written") {
    const html = step.html !== undefined ? String(step.html) : (page && page.writtenHtml);
    if (!html) throw new Error("reload_written: no written HTML captured");
    const tmp = path.join(state.tmpDir, `reload-${pageName}-${state.reloadIndex++}.html`);
    fs.writeFileSync(tmp, html, "utf8");
    const config = Object.assign({}, (state.pageConfigs && state.pageConfigs[pageName]) || {}, { html: tmp });
    // A browser refresh keeps the tab's sessionStorage/localStorage; carry them
    // so UI-state restoration (page tab / window selection) is observable.
    if (page && page.window) {
      config.storageSeed = {
        localStorage: page.window.localStorage && page.window.localStorage._dump ? page.window.localStorage._dump() : {},
        sessionStorage: page.window.sessionStorage && page.window.sessionStorage._dump ? page.window.sessionStorage._dump() : {},
      };
    }
    if (step.protocol) config.protocol = step.protocol;
    page = buildPage(state, pageName, config);
    return { op, page: pageName, htmlPath: tmp };
  }
  if (!page) {
    if (!state.pageConfigs || !state.pageConfigs[pageName]) throw new Error("unknown page: " + pageName);
    page = buildPage(state, pageName, state.pageConfigs[pageName]);
    if (op === "build_page") return { op: "build_page", page: pageName };
  }
  if (op === "set_value") {
    return Object.assign({ op }, setElementValue(page, step.selector, step.value, step.checked, step.dispatchTypes || (step.dispatch ? [step.dispatch] : ["input", "change"])));
  }
  if (op === "set_checked") {
    return Object.assign({ op }, setElementValue(page, step.selector, step.value, step.checked, step.dispatchTypes || (step.dispatch ? [step.dispatch] : ["input", "change"])));
  }
  if (op === "set_all_checked") {
    const nodes = page.document.querySelectorAll(step.selector);
    for (const node of nodes) { node.checked = !!step.checked; dispatch(page, node, step.dispatchTypes || ["input", "change"]); }
    return { op, selector: step.selector, count: nodes.length, checked: step.checked };
  }
  if (op === "select_value") {
    const element = $(page, step.selector);
    element.value = String(step.value);
    dispatch(page, element, step.dispatchTypes || ["input", "change"]);
    return { op, selector: step.selector, value: element.value };
  }
  if (op === "click") {
    const element = $(page, step.selector);
    element.click();
    return { op, selector: step.selector };
  }
  if (op === "dispatch") {
    const element = $(page, step.selector);
    dispatch(page, element, [step.event || "input"]);
    return { op, selector: step.selector, event: step.event || "input" };
  }
  if (op === "load_config") {
    const content = step.content !== undefined ? String(step.content) : fs.readFileSync(step.contentPath, "utf8");
    return Object.assign({ op }, loadConfig(page, step.selector, content, step.filename));
  }
  if (op === "load_config_raw") {
    const content = fs.readFileSync(step.contentPath, "utf8");
    return Object.assign({ op }, loadConfig(page, step.selector, content, step.filename));
  }
  if (op === "set_fetch") {
    state.currentFetch = { mode: step.mode || "real", mutate: step.mutate, message: step.message, status: step.status, url: step.url };
    return { op, mode: step.mode || "real" };
  }
  if (op === "release_fetch") {
    const resolve = state.holds.shift();
    if (!resolve) throw new Error("release_fetch: no held request");
    resolve();
    return { op, released: true };
  }
  if (op === "wait") { await new Promise((resolve) => setTimeout(resolve, Number(step.ms) || 0)); return { op, ms: Number(step.ms) || 0 }; }
  if (op === "wait_idle") { return Object.assign({ op }, await waitIdle(state, step.timeoutMs)); }
  if (op === "wait_fetch") {
    const wanted = Number(step.count || 1);
    const deadline = Date.now() + Number(step.timeoutMs || 15000);
    while (state.fetchCalls.length < wanted && Date.now() < deadline) {
      await new Promise((resolve) => setTimeout(resolve, 10));
    }
    if (state.fetchCalls.length < wanted) throw new Error("wait_fetch: timeout waiting for " + wanted + " fetch call(s)");
    await new Promise((resolve) => setTimeout(resolve, 20));
    return { op, fetchCalls: state.fetchCalls.length };
  }
  if (op === "snapshot") { return { op, snapshot: snapshot(page, state, step.name || "snapshot", step.resultKeys) }; }
  if (op === "save_written") {
    if (!page.writtenHtml) throw new Error("save_written: no document.write captured");
    fs.writeFileSync(step.path, page.writtenHtml, "utf8");
    return { op, path: step.path, chars: page.writtenHtml.length, sha256: sha256(page.writtenHtml) };
  }
  if (op === "save_last_html") {
    fs.writeFileSync(step.path, fs.readFileSync(page.htmlPath, "utf8"), "utf8");
    return { op, path: step.path };
  }
  if (op === "query") {
    const nodes = page.document.querySelectorAll(step.selector || "*");
    return { op, selector: step.selector, count: nodes.length, first: nodes[0] ? nodes[0].outerHTML.slice(0, 300) : null };
  }
  if (op === "call") {
    let target;
    try {
      target = vm.runInContext(String(step.expression || ""), page.context);
    } catch (error) {
      throw new Error("call: expression failed: " + String(error && error.message || error));
    }
    const args = Array.isArray(step.args) ? step.args : [];
    let result;
    if (typeof target === "function") {
      result = await target.apply(null, args);
    } else {
      result = target;
    }
    let normalized = null;
    try {
      normalized = result === undefined ? null : JSON.parse(JSON.stringify(result));
    } catch (error) {
      normalized = { nonJson: String(result) };
    }
    return { op, expression: step.expression, result: normalized };
  }
  if (op === "read") {
    const element = $(page, step.selector);
    const prop = step.property || "textContent";
    return { op, selector: step.selector, property: prop, value: element[prop] };
  }
  if (op === "note") return { op, note: step.note || "" };
  throw new Error("unknown op: " + op);
}

function collectNodes(root) {
  const out = [];
  const visit = (node) => {
    if (node.nodeType === 1) { out.push(node); for (const child of node.childNodes) visit(child); }
  };
  visit(root);
  return out;
}

// ---------------------------------------------------------------------------
// main
// ---------------------------------------------------------------------------
function sanitizeObservations(value, prefixes) {
  if (typeof value === "string") {
    let out = value;
    for (const prefix of prefixes) {
      if (prefix && out.startsWith(prefix)) {
        out = out.slice(prefix.length).replace(/^[\\/]+/, "");
        break;
      }
    }
    return out;
  }
  if (Array.isArray(value)) return value.map((item) => sanitizeObservations(item, prefixes));
  if (value && typeof value === "object") {
    const out = {};
    for (const [key, item] of Object.entries(value)) out[key] = sanitizeObservations(item, prefixes);
    return out;
  }
  return value;
}

async function main() {
  const specPath = process.argv[2];
  if (!specPath) { console.error("usage: node harness.js <spec.json>"); process.exit(2); }
  const spec = JSON.parse(fs.readFileSync(specPath, "utf8"));
  const tmpDir = spec.tmpDir || path.join(path.dirname(specPath), ".p3r-tmp");
  fs.mkdirSync(tmpDir, { recursive: true });
  const state = {
    fetchCalls: [],
    fetchResults: [],
    holds: [],
    currentFetch: null,
    pages: {},
    pageConfigs: spec.pages || { main: { html: spec.html, protocol: spec.protocol || "file", endpoint: spec.endpoint, bindingsPatch: spec.bindingsPatch } },
    tmpDir,
    reloadIndex: 0,
  };
  const observations = { specPath, startedAt: new Date().toISOString(), errors: [], steps: [], pages: {} };
  try {
    for (const step of spec.steps || []) {
      const result = await runStep(state, step);
      observations.steps.push(result);
    }
    for (const [name, page] of Object.entries(state.pages)) {
      observations.pages[name] = {
        name,
        htmlPath: page.htmlPath,
        htmlSha256: page.htmlSha256,
        loadFetchCount: page.loadFetchCount || 0,
        hasLeRecalc: page.hasLeRecalc,
        hasReportActions: page.hasReportActions,
        scriptsExecuted: page.scriptsExecuted,
        writeCount: page.writeCount,
        downloads: page.downloads,
        writtenChars: page.writtenHtml ? page.writtenHtml.length : 0,
        writtenSha256: page.writtenHtml ? sha256(page.writtenHtml) : null,
        errors: page.errors,
        requests: state.fetchCalls.filter((call) => call.page === name),
        responses: state.fetchResults.filter((call) => call.page === name),
        console: page.console,
      };
    }
    observations.doneAt = new Date().toISOString();
    observations.ok = true;
  } catch (error) {
    observations.ok = false;
    observations.error = String(error && error.stack || error);
    for (const [name, page] of Object.entries(state.pages)) {
      observations.pages[name] = observations.pages[name] || {
        name,
        errors: page.errors,
        requests: state.fetchCalls.filter((call) => call.page === name),
        responses: state.fetchResults.filter((call) => call.page === name),
      };
    }
  }
  const cleaned = sanitizeObservations(observations, [
    process.cwd(),
    process.cwd().replace(/\\/g, "/"),
  ]);
  const text = JSON.stringify(cleaned, null, 2);
  if (spec.output) fs.writeFileSync(spec.output, text + "\n", "utf8");
  process.stdout.write(text + "\n");
  process.exit(observations.ok ? 0 : 1);
}

main().catch((error) => {
  console.error(error && error.stack || error);
  process.exit(3);
});
