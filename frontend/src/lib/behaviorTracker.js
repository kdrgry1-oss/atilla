/**
 * Birinci taraf davranış izleyici (Hotjar/Clarity benzeri) — ısı haritası, kaydırma, süre, görüntüleme.
 *
 * GİZLİLİK: YALNIZ çerez onayında "analitik" izni verilmişse gönderir (facette_cookie_consent.analytics).
 * Kişisel veri yok: rastgele sekme-oturum kimliği (sessionStorage), göreli tıklama konumu, öğe etiketi,
 * form alanı içeriği ASLA alınmaz. Panel (/admin), panel önizleme iframe'i ve personel oturumu izlenmez.
 */
import { isStaffToken } from "./staffDataWipe";

const API = `${process.env.REACT_APP_BACKEND_URL}/api`;
const CONSENT_KEY = "facette_cookie_consent";
const FLUSH_MS = 10000;
const MAX_QUEUE = 40;

let queue = [];
let started = false;
let page = { path: "", pt: "other", pid: null, cat: null };
let activeMs = 0;
let lastActive = 0;
let maxDepth = 0;
let sid = "";
let pvSent = false;
let pvTimer = null;

const now = () => Date.now();

function consentOk() {
  try {
    const c = JSON.parse(localStorage.getItem(CONSENT_KEY) || "null");
    return !!(c && c.analytics === true);
  } catch { return false; }
}

function disabled() {
  try {
    if (window.self !== window.top) return true;                   // panel ısı haritası önizlemesi
    if (window.location.pathname.startsWith("/admin")) return true;
    if (isStaffToken(localStorage.getItem("token"))) return true;   // personel vitrinde gezerken sayılmaz
  } catch { return true; }
  return false;
}

function getSid() {
  if (sid) return sid;
  try {
    sid = sessionStorage.getItem("fc_bsid") || "";
    if (!sid) {
      const r = (crypto?.getRandomValues ? Array.from(crypto.getRandomValues(new Uint8Array(12)), (b) => b.toString(16).padStart(2, "0")).join("") : Math.random().toString(36).slice(2) + Math.random().toString(36).slice(2));
      sid = `s${r}`.slice(0, 40);
      sessionStorage.setItem("fc_bsid", sid);
    }
  } catch { sid = `s${Math.random().toString(36).slice(2)}${now()}`; }
  return sid;
}

function defaultType(path) {
  if (path === "/") return "home";
  if (path.startsWith("/arama")) return "search";
  if (path.startsWith("/sepet")) return "cart";
  if (path.startsWith("/odeme")) return "checkout";
  if (path.startsWith("/hesabim") || path.startsWith("/giris")) return "account";
  if (path.startsWith("/kategori/")) return "category";
  return "other";
}

function push(ev) {
  if (disabled()) return;
  queue.push({ ...ev, path: ev.path || page.path, pt: ev.pt || page.pt, w: window.innerWidth });
  if (queue.length >= MAX_QUEUE) flush();
}

function flush(useBeacon = false) {
  if (!queue.length) return;
  const events = queue.splice(0, queue.length);
  if (!consentOk() || disabled()) return;                           // izin yok → at, gönderme
  const body = JSON.stringify({ sid: getSid(), events });
  try {
    if (useBeacon && navigator.sendBeacon) {
      navigator.sendBeacon(`${API}/analytics/collect`, new Blob([body], { type: "application/json" }));
      return;
    }
    fetch(`${API}/analytics/collect`, { method: "POST", headers: { "Content-Type": "application/json" }, body, keepalive: true }).catch(() => {});
  } catch { /* sessiz */ }
}

function markActive() {
  const t = now();
  if (document.visibilityState === "visible" && lastActive && t - lastActive < 30000) activeMs += t - lastActive;
  lastActive = t;
}

function sendTime() {
  markActive();
  if (activeMs > 500) push({ t: "time", ms: Math.round(activeMs), pid: page.pid, cat: page.cat });
  activeMs = 0;
}

let scrollSent = false;
function endPage() {
  // Sayılmamış (pv gönderilmeden terk edilen / yönlendirilen) sayfa süre/kaydırma üretmez.
  if (!page.path || !pvSent) return;
  sendTime();
  if (maxDepth > 0 && !scrollSent) push({ t: "scroll", depth: Math.round(maxDepth), pid: page.pid, cat: page.cat });
  scrollSent = true;
}

function onScroll() {
  markActive();
  const doc = document.documentElement;
  const h = Math.max(doc.scrollHeight, document.body?.scrollHeight || 0);
  const seen = window.scrollY + window.innerHeight;
  if (h > 0) maxDepth = Math.max(maxDepth, Math.min(100, (seen / h) * 100));
}

function label(el) {
  let n = el;
  for (let i = 0; n && i < 4; i++, n = n.parentElement) {
    const tid = n.getAttribute && n.getAttribute("data-testid");
    if (tid) return `${n.tagName.toLowerCase()}[${tid}]`.slice(0, 80);
    if (n.id) return `${n.tagName.toLowerCase()}#${n.id}`.slice(0, 80);
  }
  return (el.tagName || "").toLowerCase();
}

function productIdOf(el) {
  const c = el.closest && el.closest("[data-product-id]");
  return c ? c.getAttribute("data-product-id") : null;
}

function onClick(e) {
  markActive();
  const el = e.target;
  if (!el || el.closest?.("input, textarea, select, [contenteditable='true']")) return;  // form içeriği ASLA
  const a = el.closest?.("a, button, [role='button'], img, [data-product-id]") || el;
  const txt = (a.getAttribute?.("aria-label") || a.innerText || a.alt || "").replace(/\s+/g, " ").trim().slice(0, 40);
  push({ t: "click", x: e.clientX / window.innerWidth, y: e.pageY, sel: label(a), txt,
         pid: productIdOf(el) || page.pid, cat: page.cat });
}


function sendPv() {
  if (pvSent || !page.path) return;
  pvSent = true;
  clearTimeout(pvTimer);
  // Belge yüksekliği içerik yerleştikten sonra ölçülür (ısı haritası y ölçeği için).
  setTimeout(() => {
    const h = Math.max(document.documentElement.scrollHeight, document.body?.scrollHeight || 0);
    push({ t: "pv", pid: page.pid, cat: page.cat, h });
    onScroll();
  }, 600);
}

/** Sayfa değişiminde App çağırır (route path). */
export function trackPageview(path) {
  if (!started) start();
  if (disabled()) return;
  endPage();
  page = { path, pt: defaultType(path), pid: null, cat: null };
  activeMs = 0; lastActive = now(); maxDepth = 0; scrollSent = false; pvSent = false;
  clearTimeout(pvTimer);
  // Ürün/kategori sayfası kendini setPageContext ile tanıtınca pv hemen yazılır; tanıtmayan
  // sayfalar (ana sayfa, sepet, statik) için kısa bekleme sonrası.
  pvTimer = setTimeout(sendPv, page.pt === "other" ? 3500 : 900);
}

/** Ürün/kategori sayfası kendini tanıtır: setPageContext({type:'product', pid}) / ({type:'category', cat}). */
export function setPageContext({ type, pid, cat } = {}) {
  if (type) page.pt = type;
  if (pid) page.pid = String(pid);
  if (cat) page.cat = String(cat);
  if ((type === "product" && pid) || (type === "category" && cat)) sendPv();
}

export function trackAddToCart(pid) {
  if (pid) push({ t: "atc", pid: String(pid) });
}

/** Arama kelimesi — kimliksiz istatistik (oturum/kullanıcı gönderilmez). */
export function trackSearchTerm(q, n, src = "live", pid = null) {
  try {
    if (disabled()) return;
    const term = String(q || "").trim();
    if (term.length < 2) return;
    fetch(`${API}/analytics/search`, { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ q: term.slice(0, 80), n: Number(n) || 0, src, pid }), keepalive: true }).catch(() => {});
  } catch { /* sessiz */ }
}

function start() {
  started = true;
  if (typeof window === "undefined") return;
  window.addEventListener("scroll", onScroll, { passive: true });
  document.addEventListener("click", onClick, true);
  ["mousemove", "keydown", "touchstart"].forEach((ev) => window.addEventListener(ev, markActive, { passive: true }));
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") { sendTime(); flush(true); }
    else lastActive = now();
  });
  window.addEventListener("pagehide", () => { endPage(); flush(true); });
  setInterval(() => flush(), FLUSH_MS);
}
