const EXPLICIT_SELECTOR = [
  "[data-facette-contact-fab]",
  "[data-testid='whatsapp-fab']",
  "[data-whatsapp-fab]",
  ".whatsapp-float",
  ".whatsapp-floating",
  ".whatsapp-button",
  "#whatsapp-button",
  "iframe[src*='whatsapp']",
].join(",");

function isWhatsAppLink(node) {
  const href = String(node?.getAttribute?.("href") || "").toLowerCase();
  return href.includes("wa.me/") || href.includes("api.whatsapp.com/") || href.includes("web.whatsapp.com/");
}

function isFloating(node) {
  if (!node || typeof window === "undefined") return false;
  let current = node;
  for (let depth = 0; current && depth < 4; depth += 1, current = current.parentElement) {
    const position = window.getComputedStyle(current).position;
    if (position === "fixed" || position === "sticky") return true;
  }
  return false;
}

export function findWhatsAppFabs(root = document) {
  const explicit = [...root.querySelectorAll(EXPLICIT_SELECTOR)];
  const floatingLinks = [...root.querySelectorAll("a[href]")].filter((node) => isWhatsAppLink(node) && isFloating(node));
  return [...new Set([...explicit, ...floatingLinks])];
}

function restore(node) {
  if (node.getAttribute("data-facette-wa-suppressed") !== "true") return;
  const previous = node.getAttribute("data-facette-wa-display");
  if (previous) node.style.display = previous;
  else node.style.removeProperty("display");
  node.removeAttribute("data-facette-wa-display");
  node.removeAttribute("data-facette-wa-suppressed");
  node.removeAttribute("aria-hidden");
}

export function enforceSingleWhatsAppFab(root = document, preferred = null) {
  const candidates = findWhatsAppFabs(root);
  const winner = preferred && candidates.includes(preferred) ? preferred : candidates[0] || null;
  candidates.forEach((node) => {
    if (node === winner) {
      restore(node);
      return;
    }
    if (node.getAttribute("data-facette-wa-suppressed") !== "true") {
      if (node.style.display) node.setAttribute("data-facette-wa-display", node.style.display);
      node.style.setProperty("display", "none", "important");
      node.setAttribute("data-facette-wa-suppressed", "true");
      node.setAttribute("aria-hidden", "true");
    }
  });
  return { winner, total: candidates.length, suppressed: Math.max(0, candidates.length - (winner ? 1 : 0)) };
}

export function restoreSuppressedWhatsAppFabs(root = document) {
  root.querySelectorAll("[data-facette-wa-suppressed='true']").forEach(restore);
}
