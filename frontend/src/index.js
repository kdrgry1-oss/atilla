// build: 2026-08-09c — root ErrorBoundary (render çökmesini yakalar + beacon + chunk-hatası oto-kurtarma)
import React from "react";
import ReactDOM from "react-dom/client";
import "@/index.css";
import App from "@/App";

// GÖRSEL YEDEĞİ: R2 görsel deposundan (CDN) bir görsel açılmazsa (ör. Cloudflare 403 — depo
// herkese açık erişimi kesilmiş), aynı nesneyi backend'in R2 vekilinden (/api/upload/r2/...)
// bir kez yeniden dener. Tek dinleyici tüm <img>'leri kapsar; döngü koruması data-r2fb.
(function installImageFallback() {
  try {
    var api = String(process.env.REACT_APP_BACKEND_URL || "").replace(/\/+$/, "");
    if (!api) return;
    window.addEventListener("error", function (ev) {
      var el = ev && ev.target;
      if (!el || el.tagName !== "IMG" || el.getAttribute("data-r2fb")) return;
      var src = el.currentSrc || el.src || "";
      var m = src.match(/^https?:\/\/[^/]+\/((?:uploads|products|pagedesign|brand)\/[^?#]+\.(?:webp|jpe?g|png|gif|avif))/i);
      if (!m || src.indexOf(api) === 0) return;
      el.setAttribute("data-r2fb", "1");
      if (el.srcset) el.removeAttribute("srcset");
      el.src = api + "/api/upload/r2/" + m[1];
    }, true);
  } catch (e) { /* yoksay */ }
})();

// Kök hata sınırı: React render sırasında bir bileşen çökerse TÜM uygulama unmount olup
// #root boşalır (beyaz ekran → self-heal reload → "site açılmıyor"). Burada yakalayıp:
//  1) Gerçek sebebi (mesaj + bileşen yığını) telemetriye gönderiyoruz (window.__fctBeacon),
//  2) Chunk yükleme hatasıysa (deploy sonrası bayat cache) bir kez cache-bust reload → oto-kurtarma,
//  3) Aksi halde beyaz ekran yerine kibar "tekrar dene" ekranı gösteriyoruz.
class RootErrorBoundary extends React.Component {
  constructor(props) {
    super(props);
    this.state = { crashed: false };
  }
  static getDerivedStateFromError() {
    return { crashed: true };
  }
  componentDidCatch(error, info) {
    var msg = "";
    try { msg = (error && (error.stack || error.message)) ? String(error.stack || error.message) : String(error); } catch (e) {}
    var stack = "";
    try { stack = (info && info.componentStack) ? String(info.componentStack) : ""; } catch (e) {}
    try {
      if (window.__fctBeacon) {
        window.__fctBeacon("error", { message: msg.slice(0, 500), source: "react_boundary:" + stack.slice(0, 160) });
      }
    } catch (e) {}
    // Chunk/lazy-import yükleme hatası → genelde yeni deploy'dan sonra tarayıcı bayat chunk ister.
    // Bir kez cache-bust reload ile taze index+chunk çek (döngü guard: sessionStorage).
    var isChunk = /Loading chunk|ChunkLoadError|dynamically imported module|Failed to fetch dynamically|Importing a module script failed/i.test(msg);
    if (isChunk) {
      try {
        if (!sessionStorage.getItem("__chunkReload")) {
          sessionStorage.setItem("__chunkReload", "1");
          var u = location.pathname + (location.search ? location.search + "&" : "?") + "_c=" + Date.now() + location.hash;
          location.replace(u);
          return;
        }
      } catch (e) {}
    }
  }
  render() {
    if (this.state.crashed) {
      return (
        <div style={{ minHeight: "70vh", display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", gap: 14, padding: 24, textAlign: "center", fontFamily: "system-ui,-apple-system,Segoe UI,Arial,sans-serif" }}>
          <div style={{ fontSize: 18, fontWeight: 700, color: "#111" }}>Bir sorun oluştu</div>
          <div style={{ fontSize: 14, color: "#666", maxWidth: 360, lineHeight: 1.5 }}>
            Sayfa yüklenirken beklenmedik bir hata oldu. Lütfen tekrar deneyin.
          </div>
          <button
            onClick={() => { try { sessionStorage.removeItem("__chunkReload"); } catch (e) {} location.replace(location.pathname); }}
            style={{ padding: "10px 22px", background: "#111", color: "#fff", border: "none", borderRadius: 2, fontSize: 13, cursor: "pointer" }}
          >
            Tekrar dene
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}

const root = ReactDOM.createRoot(document.getElementById("root"));
root.render(
  <RootErrorBoundary>
    <App />
  </RootErrorBoundary>,
);
