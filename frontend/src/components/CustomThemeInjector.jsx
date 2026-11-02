/**
 * CustomThemeInjector.jsx — Özel Tema (CSS/JS) enjeksiyonu — GÜVENLİ.
 * ---------------------------------------------------------------------------
 * - CSS ve JS YALNIZCA mağaza (storefront) sayfalarında yüklenir.
 * - /admin altında HİÇBİR ŞEY enjekte edilmez → tenant kodu admin panelini,
 *   JWT token'ını veya oturumunu ASLA etkileyemez (en kritik güvenlik sınırı).
 * - Özel JS ödeme / hesap / giriş-şifre / sipariş-sonucu sayfalarında ÇALIŞTIRILMAZ
 *   (kart, parola ve kişisel veri girilen ekranlar). CSS bu sayfalarda da uygulanır.
 * - SPA'da bir kez çalışmış JS kaldırılamaz: JS bu belgede çalıştıktan sonra hassas
 *   bir sayfaya (veya /admin'e) geçilirse sayfa temiz bir belgeyle yeniden yüklenir.
 * - JS yalnız admin panelinde 'js_enabled' açıksa sunucudan gelir (varsayılan KAPALI).
 * - Savunmacı: hata / boş → hiçbir şey yapmaz.
 */
import { useEffect, useRef } from "react";
import { useLocation } from "react-router-dom";
import axios from "axios";

const API = `${process.env.REACT_APP_BACKEND_URL}/api`;

// Özel JS'in ÇALIŞMAYACAĞI vitrin yolları (tam eşleşme veya alt yol).
const JS_BLOCKED_PATHS = [
  "/odeme", "/odeme-bildirimi", "/hesabim", "/giris", "/sifremi-unuttum", "/sifre-sifirla",
  "/order-success", "/siparis-tamamlandi", "/iade", "/iade-islemleri",
];

const isJsBlockedPath = (p) => JS_BLOCKED_PATHS.some((x) => p === x || p.startsWith(`${x}/`));

const jsHasRun = () => {
  try { return window.__FACETTE_CUSTOM_JS_RAN__ === true; } catch (_) { return false; }
};

export default function CustomThemeInjector() {
  const location = useLocation();
  const themeReq = useRef(null);
  const path = location.pathname || "";
  const isAdmin = path.startsWith("/admin");
  const jsBlocked = isAdmin || isJsBlockedPath(path);

  useEffect(() => {
    if (jsBlocked && jsHasRun()) {
      // Özel JS bu belgede zaten çalıştı; hassas sayfaya temiz belgeyle gir.
      window.location.reload();
      return undefined;
    }
    if (isAdmin) {
      // Admin'e girildiyse enjekte edilmiş tema kodunu KALDIR (güvenlik + admin UI bozulmasın).
      document.getElementById("facette-custom-css")?.remove();
      document.getElementById("facette-custom-js")?.remove();
      themeReq.current = null; // vitrine dönüşte güncel temayı yeniden çek
      return undefined;
    }
    let alive = true;
    if (!themeReq.current) {
      themeReq.current = axios.get(`${API}/custom-theme`).then((r) => r.data || {}).catch(() => ({}));
    }
    themeReq.current.then((data) => {
      if (!alive) return;
      const css = data.custom_css || "";
      const js = data.custom_js || "";
      if (css && !document.getElementById("facette-custom-css")) {
        const st = document.createElement("style");
        st.id = "facette-custom-css";
        st.textContent = css;
        document.head.appendChild(st);
      }
      if (js && !jsBlocked && !jsHasRun() && !document.getElementById("facette-custom-js")) {
        try {
          window.__FACETTE_CUSTOM_JS_RAN__ = true;
          const sc = document.createElement("script");
          sc.id = "facette-custom-js";
          sc.textContent = js;
          document.body.appendChild(sc);
        } catch (_) { /* tema JS hatası mağazayı kırmasın */ }
      }
    });
    return () => { alive = false; };
  }, [isAdmin, jsBlocked]);

  return null;
}
