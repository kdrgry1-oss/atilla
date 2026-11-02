/**
 * Panelin YENİ SÜRÜMÜ yayınlandığında açık sekmeyi haberdar eder.
 * Personel paneli saatlerce/günlerce açık tutuyor → düzeltmeler (ör. iade arama yarışı) sekme
 * yenilenene kadar ulaşmıyordu. /version.json 3 dk'da bir (ve sekmeye dönünce) kontrol edilir;
 * sürüm değiştiyse üstte "Yenile" şeridi çıkar, bir sonraki SAYFA GEÇİŞİNDE kendiliğinden yenilenir
 * (form yazılırken sayfa zorla yenilenmez).
 */
import { useEffect, useRef, useState } from "react";
import { useLocation } from "react-router-dom";

const CURRENT = String(process.env.REACT_APP_BUILD_SHA || "").toLowerCase();
const KNOWN = /^[0-9a-f]{7,40}$/.test(CURRENT);

export default function AdminUpdateNotice() {
  const [stale, setStale] = useState(false);
  const loc = useLocation();
  const first = useRef(true);

  useEffect(() => {
    if (!KNOWN) return undefined;
    let alive = true;
    const check = async () => {
      try {
        const r = await fetch(`/version.json?ts=${Date.now()}`, { cache: "no-store" });
        if (!r.ok) return;
        const j = await r.json();
        const sha = String(j?.sha || "").toLowerCase();
        if (alive && /^[0-9a-f]{7,40}$/.test(sha) && sha !== CURRENT) setStale(true);
      } catch { /* ağ hatası: sessiz */ }
    };
    check();
    const t = setInterval(check, 3 * 60 * 1000);
    const onVis = () => { if (document.visibilityState === "visible") check(); };
    document.addEventListener("visibilitychange", onVis);
    return () => { alive = false; clearInterval(t); document.removeEventListener("visibilitychange", onVis); };
  }, []);

  // Eski sürüm + sayfa değişti → güvenli an: yeni sürümü yükle.
  useEffect(() => {
    if (first.current) { first.current = false; return; }
    if (stale) window.location.reload();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loc.pathname]);

  if (!stale) return null;
  return (
    <div className="fixed inset-x-0 top-0 z-[1000] flex items-center justify-center gap-3 bg-amber-500 px-4 py-2 text-sm font-medium text-white shadow"
         data-testid="admin-update-notice">
      <span>Panelin yeni sürümü yayınlandı — düzeltmelerin geçerli olması için sayfayı yenileyin.</span>
      <button onClick={() => window.location.reload()}
        className="h-7 rounded-md bg-white px-3 text-xs font-semibold text-amber-700 hover:bg-amber-50">
        Şimdi Yenile
      </button>
    </div>
  );
}
