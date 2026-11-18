/* 用極簡 DOM 模擬跑一遍 tem-widget.js，確認卡片能正常渲染。
 * 用法： node tools/widget_smoke_test.js data/exports/tem-widget.js
 */
const fs = require("fs");

const target = process.argv[2] || "data/exports/tem-widget.js";
const nodes = [];

function mkEl(tag) {
  const el = {
    tagName: tag,
    children: [],
    attributes: {},
    style: {},
    innerHTML: "",
    textContent: "",
    classList: { add() {}, remove() {}, toggle() {} },
    setAttribute(k, v) { el.attributes[k] = v; },
    getAttribute(k) { return Object.prototype.hasOwnProperty.call(el.attributes, k) ? el.attributes[k] : null; },
    appendChild(c) { el.children.push(c); return c; },
    remove() {},
    _ls: {},
    addEventListener(type, fn) { (el._ls[type] = el._ls[type] || []).push(fn); },
    fire(type, ev) { (el._ls[type] || []).forEach((fn) => fn(ev || { stopPropagation() {} })); },
    contains() { return false; },
    querySelector() { return null; },
    querySelectorAll() { return []; },
    getBoundingClientRect() { return { top: 0, left: 0, right: 120, bottom: 44, width: 120, height: 44 }; },
  };
  nodes.push(el);
  return el;
}

const root = mkEl("div");
let rootHtml = "";
Object.defineProperty(root, "innerHTML", {
  get() { return rootHtml; },
  set(v) { rootHtml = v; },
});
let cardCache = [];
let cardCacheN = -1;
const fakeCards = () => {
  const n = (rootHtml.match(/class="temx-card"/g) || []).length;
  if (n !== cardCacheN) {
    cardCacheN = n;
    cardCache = Array.from({ length: n }, (_, i) => {
      const c = mkEl("div");
      c.attributes["data-i"] = String(i);
      return c;
    });
  }
  return cardCache;
};
root.querySelectorAll = (sel) => (sel === ".temx-card" ? fakeCards() : []);
global.document = {
  currentScript: null,
  readyState: "complete",
  head: mkEl("head"),
  body: mkEl("body"),
  getElementById() { return null; },
  createElement: mkEl,
  querySelector(sel) { return sel === "#tem-widget" ? root : null; },
  addEventListener() {},
  querySelectorAll() { return []; },
};
global.window = { innerWidth: 1400, innerHeight: 900, addEventListener() {}, open() {} };
global.fetch = () => Promise.reject(new Error("offline"));
global.setTimeout = (fn) => { try { fn(); } catch (e) { console.error("setTimeout cb error:", e.message); } return 0; };
global.clearTimeout = () => {};
global.parseInt = parseInt;

eval(fs.readFileSync(target, "utf8"));

// 觸發第一張卡片的 mouseenter，檢查懸浮卡（K 線）是否正常產生
const firstCard = fakeCards()[0];
if (firstCard) firstCard.fire("mouseenter");
const popEl = global.document.body.children.find((c) => String(c.className).indexOf("temx-pop") >= 0);
const popHtml = popEl ? popEl.innerHTML : "";
const rects = (popHtml.match(/<rect /g) || []).length;
const lines = (popHtml.match(/<line /g) || []).length;
const hasRanges = popHtml.indexOf("ranges") >= 0;

const out = root.innerHTML;
const cards = (out.match(/class="temx-card"/g) || []).length;
const imgs = (out.match(/<img /g) || []).length;
const sparks = (out.match(/<polyline/g) || []).length;
const price = out.match(/temx-price[^>]*>([^<]+)</);

console.log("目标文件       :", target, `(${(fs.statSync(target).size / 1024).toFixed(1)} KB)`);
console.log("渲染 HTML 长度 :", out.length);
console.log("卡片数         :", cards);
console.log("含头像 img     :", imgs);
console.log("含迷你走势     :", sparks);
console.log("样式已注入     :", global.document.head.children.length > 0);
console.log("首个卡片价格   :", price ? price[1] : "(无)");
console.log("--- 首张卡片片段 ---");
console.log(out.slice(0, 380));
console.log("--- 悬浮卡（K 线）---");
console.log("弹出层已生成   :", !!popEl);
console.log("K 线蜡烛实体   :", rects, "根 | 影线", lines, "条");
console.log("区间切换按钮   :", hasRanges);
console.log("悬浮卡片段     :", popHtml.slice(0, 300).replace(/\s+/g, " "));

if (!cards) { console.error("\nFAIL: 没有渲染出任何卡片"); process.exit(1); }
if (!popEl || rects < 5) { console.error("\nFAIL: 悬浮 K 线图没有正常产生"); process.exit(1); }
console.log("\nPASS");
