/**
 * BehaviorAnalytics.jsx — Ziyaretçi Davranışı (Hotjar/Clarity benzeri, birinci taraf).
 * Sekmeler: Özet · Ürünler · Kategoriler · Aramalar · Sayfa & Süre · Isı Haritası.
 * Veri yalnız analitik çerez izni veren ziyaretçilerden (arama kelimeleri kimliksiz, herkesten).
 */
import { useCallback, useEffect, useRef, useState } from "react";
import axios from "axios";
import { toast } from "sonner";
import { RefreshCw } from "lucide-react";

const API = `${process.env.REACT_APP_BACKEND_URL}/api`;
const auth = () => ({ headers: { Authorization: `Bearer ${localStorage.getItem("token")}` } });
const fmtNum = (v) => Number(v || 0).toLocaleString("tr-TR");
const fmtSec = (s) => {
  const n = Number(s || 0);
  if (n < 60) return `${n.toLocaleString("tr-TR", { maximumFractionDigits: 1 })} sn`;
  return `${Math.floor(n / 60)} dk ${Math.round(n % 60)} sn`;
};
const pct = (a, b) => (b ? `%${(100 * a / b).toLocaleString("tr-TR", { maximumFractionDigits: 1 })}` : "—");
const PT_LABEL = { home: "Ana sayfa", product: "Ürün sayfası", category: "Kategori", search: "Arama", cart: "Sepet",
  checkout: "Ödeme", account: "Hesap/Giriş", other: "Diğer" };
const RANGES = [[1, "Bugün"], [7, "7 Gün"], [30, "30 Gün"], [90, "90 Gün"]];
const TABS = [["ozet", "Özet"], ["urun", "Ürünler"], ["kategori", "Kategoriler"], ["arama", "Aramalar"],
  ["sayfa", "Sayfa & Süre"], ["isi", "Isı Haritası"]];

function Th({ children, right }) {
  return <th className={`px-3 py-2 text-xs font-medium text-gray-600 ${right ? "text-right" : "text-left"}`}>{children}</th>;
}
function Td({ children, right, strong }) {
  return <td className={`px-3 py-2 ${right ? "text-right tabular-nums" : ""} ${strong ? "font-semibold" : ""}`}>{children}</td>;
}
function Empty({ children }) {
  return <div className="p-8 text-center text-sm text-gray-500">{children}</div>;
}

// ── Isı haritası çizimi: yoğunluk (alfa birikimi) → renk paleti (mavi→yeşil→sarı→kırmızı)
function drawHeat(canvas, points, width, height) {
  if (!canvas) return;
  canvas.width = width; canvas.height = height;
  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, width, height);
  if (!points.length) return;
  const r = width < 600 ? 18 : 26;
  const a = Math.max(0.03, Math.min(0.3, 30 / points.length));
  for (const p of points) {
    const x = p.x * width, y = p.y;
    if (y > height) continue;
    const g = ctx.createRadialGradient(x, y, 0, x, y, r);
    g.addColorStop(0, `rgba(0,0,0,${a})`); g.addColorStop(1, "rgba(0,0,0,0)");
    ctx.fillStyle = g; ctx.fillRect(x - r, y - r, 2 * r, 2 * r);
  }
  const img = ctx.getImageData(0, 0, width, height); const d = img.data;
  const stops = [[0, [0, 0, 255]], [0.35, [0, 200, 255]], [0.55, [0, 220, 0]], [0.75, [255, 230, 0]], [1, [255, 0, 0]]];
  let max = 0;
  for (let i = 3; i < d.length; i += 4) if (d[i] > max) max = d[i];
  for (let i = 0; i < d.length; i += 4) {
    const v = d[i + 3] / (max || 1);
    if (!v) continue;
    let c = stops[stops.length - 1][1];
    for (let k = 1; k < stops.length; k++) {
      if (v <= stops[k][0]) {
        const [p0, c0] = stops[k - 1], [p1, c1] = stops[k]; const t = (v - p0) / (p1 - p0);
        c = [0, 1, 2].map((j) => Math.round(c0[j] + (c1[j] - c0[j]) * t)); break;
      }
    }
    // Az yoğun nokta soluk/şeffaf, yoğun bölge belirgin (seyrek tek tıklamalar leke yapmasın).
    d[i] = c[0]; d[i + 1] = c[1]; d[i + 2] = c[2]; d[i + 3] = v < 0.06 ? 0 : Math.round(30 + 200 * Math.sqrt(v));
  }
  ctx.putImageData(img, 0, 0);
}

function Heatmap({ days }) {
  const [pages, setPages] = useState([]);
  const [path, setPath] = useState("/");
  const [device, setDevice] = useState("desktop");
  const [mode, setMode] = useState("click");
  const [data, setData] = useState(null);
  const [frameH, setFrameH] = useState(1200);
  const [boxW, setBoxW] = useState(1000);
  const canvasRef = useRef(null);
  const frameRef = useRef(null);
  const boxRef = useRef(null);
  const devW = device === "mobile" ? 390 : device === "tablet" ? 820 : 1440;

  useEffect(() => {
    axios.get(`${API}/admin/behavior/pages`, { ...auth(), params: { days } })
      .then((r) => { const ps = r.data?.pages || []; setPages(ps); if (ps.length && !ps.find((p) => p.path === path)) setPath(ps[0].path); })
      .catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [days]);
  useEffect(() => {
    if (!path) return;
    axios.get(`${API}/admin/behavior/heatmap`, { ...auth(), params: { path, device, days } })
      .then((r) => { setData(r.data); if (r.data?.doc_height) setFrameH(r.data.doc_height); })
      .catch((e) => toast.error(e.response?.data?.detail || "Isı haritası alınamadı"));
  }, [path, device, days]);
  useEffect(() => {
    const ro = new ResizeObserver(() => setBoxW(boxRef.current?.clientWidth || 1000));
    if (boxRef.current) ro.observe(boxRef.current);
    return () => ro.disconnect();
  }, []);
  useEffect(() => {
    if (mode === "click") drawHeat(canvasRef.current, data?.clicks || [], devW, frameH);
    else if (canvasRef.current) { const c = canvasRef.current; c.width = devW; c.height = frameH; c.getContext("2d").clearRect(0, 0, devW, frameH); }
  }, [data, devW, frameH, mode]);

  const onFrameLoad = () => {
    try {
      const doc = frameRef.current?.contentDocument;
      const h = Math.max(doc?.documentElement?.scrollHeight || 0, doc?.body?.scrollHeight || 0);
      if (h > 300) setFrameH(h);
    } catch { /* farklı köken: kayıtlı yükseklik kullanılır */ }
  };
  const scale = Math.min(1, (boxW - 2) / devW);
  const reach = data?.scroll_reach || [];

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <select value={path} onChange={(e) => setPath(e.target.value)} className="h-9 min-w-[280px] rounded-md border border-gray-300 px-2 text-sm">
          {!pages.find((p) => p.path === path) && <option value={path}>{path}</option>}
          {pages.map((p) => <option key={p.path} value={p.path}>{p.path} · {fmtNum(p.views)} görüntüleme</option>)}
        </select>
        {[["desktop", "Masaüstü"], ["tablet", "Tablet"], ["mobile", "Mobil"]].map(([k, l]) => (
          <button key={k} onClick={() => setDevice(k)}
            className={`h-9 px-3 rounded-md text-sm border ${device === k ? "bg-gray-900 text-white border-gray-900" : "bg-white border-gray-300 hover:bg-gray-50"}`}>{l}</button>
        ))}
        <span className="mx-1 h-6 w-px bg-gray-200" />
        {[["click", "Tıklama haritası"], ["scroll", "Kaydırma haritası"]].map(([k, l]) => (
          <button key={k} onClick={() => setMode(k)}
            className={`h-9 px-3 rounded-md text-sm border ${mode === k ? "bg-orange-500 text-white border-orange-500" : "bg-white border-gray-300 hover:bg-gray-50"}`}>{l}</button>
        ))}
        <span className="ml-auto text-xs text-gray-500">
          {data ? `${fmtNum(data.views)} görüntüleme · ${fmtNum((data.clicks || []).length)} tıklama` : ""}
        </span>
      </div>
      <div className="grid gap-4 lg:grid-cols-[1fr_260px]">
        <div ref={boxRef} className="overflow-auto rounded-lg border bg-gray-100" style={{ maxHeight: 900 }}>
          <div style={{ width: devW * scale, height: frameH * scale, position: "relative" }}>
            <div style={{ width: devW, height: frameH, transform: `scale(${scale})`, transformOrigin: "0 0", position: "absolute", left: 0, top: 0 }}>
              <iframe ref={frameRef} title="Sayfa önizleme" src={path} onLoad={onFrameLoad} scrolling="no"
                style={{ width: devW, height: frameH, border: 0, pointerEvents: "none", background: "#fff" }} />
              <canvas ref={canvasRef} style={{ position: "absolute", left: 0, top: 0, width: devW, height: frameH, pointerEvents: "none" }} />
              {mode === "scroll" && reach.map((r) => (
                <div key={r.depth} style={{ position: "absolute", left: 0, right: 0, top: (r.depth / 100) * frameH,
                  height: frameH / 10, background: `rgba(239,68,68,${0.05 + 0.5 * (1 - r.pct / 100)})` }}>
                  <span className="absolute left-3 top-1 rounded bg-black/70 px-2 py-0.5 text-white" style={{ fontSize: 14 / scale > 40 ? 40 : 14 / scale }}>
                    %{r.depth} derinliğe ulaşan: %{r.pct}
                  </span>
                </div>
              ))}
            </div>
          </div>
        </div>
        <div className="space-y-4">
          <div className="rounded-lg border bg-white">
            <div className="border-b px-3 py-2 text-sm font-semibold">En çok tıklanan öğeler</div>
            {(data?.top_elements || []).length === 0 ? <Empty>Henüz tıklama yok</Empty> : (
              <ul className="divide-y text-sm">
                {data.top_elements.slice(0, 15).map((e, i) => (
                  <li key={i} className="flex items-center justify-between gap-2 px-3 py-1.5">
                    <span className="truncate" title={e.sel}>{e.txt || e.sel || "—"}</span>
                    <span className="tabular-nums text-gray-600">{fmtNum(e.n)}</span>
                  </li>
                ))}
              </ul>
            )}
          </div>
          <div className="rounded-lg border bg-white">
            <div className="border-b px-3 py-2 text-sm font-semibold">Kaydırma derinliği</div>
            <ul className="divide-y text-sm">
              {reach.map((r) => (
                <li key={r.depth} className="flex items-center gap-2 px-3 py-1.5">
                  <span className="w-12 text-gray-600">%{r.depth}</span>
                  <span className="h-2 flex-1 rounded bg-gray-100"><span className="block h-2 rounded bg-orange-500" style={{ width: `${r.pct}%` }} /></span>
                  <span className="w-12 text-right tabular-nums">%{r.pct}</span>
                </li>
              ))}
            </ul>
          </div>
        </div>
      </div>
    </div>
  );
}

export default function BehaviorAnalytics() {
  const [days, setDays] = useState(7);
  const [tab, setTab] = useState("ozet");
  const [d, setD] = useState(null);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try { const r = await axios.get(`${API}/admin/behavior/summary`, { ...auth(), params: { days } }); setD(r.data); }
    catch (e) { toast.error(e.response?.data?.detail || "Davranış verisi alınamadı"); }
    finally { setLoading(false); }
  }, [days]);
  useEffect(() => { load(); }, [load]);

  const T = d?.totals || {};
  const noData = d && !T.pageviews && !T.searches;

  return (
    <div className="space-y-6" data-testid="behavior-analytics">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold">Davranış Analizi</h1>
          <p className="mt-1 text-sm text-gray-500">
            Hangi ürün/kategoriye tıklanıyor, nerede ne kadar vakit geçiriliyor, ne aranıyor. Davranış verisi yalnız
            analitik çerez izni veren ziyaretçilerden; arama kelimeleri kimliksiz olarak tüm aramalardan toplanır.
          </p>
        </div>
        <div className="flex items-center gap-2">
          {RANGES.map(([n, l]) => (
            <button key={n} onClick={() => setDays(n)}
              className={`h-9 px-3 rounded-md text-sm border ${days === n ? "bg-gray-900 text-white border-gray-900" : "bg-white border-gray-300 hover:bg-gray-50"}`}>{l}</button>
          ))}
          <button onClick={load} className="inline-flex h-9 items-center gap-1.5 rounded-md border border-gray-300 bg-white px-3 text-sm hover:bg-gray-50">
            <RefreshCw size={14} className={loading ? "animate-spin" : ""} /> Yenile
          </button>
        </div>
      </div>

      <div className="grid grid-cols-2 gap-3 md:grid-cols-5">
        {[["Oturum", fmtNum(T.sessions)], ["Sayfa görüntüleme", fmtNum(T.pageviews)], ["Tıklama", fmtNum(T.clicks)],
          ["Ort. oturum süresi", fmtSec(T.avg_session_sec)], ["Arama", fmtNum(T.searches)]].map(([l, v]) => (
          <div key={l} className="rounded-lg border bg-white p-4">
            <div className="text-xs text-gray-500">{l}</div>
            <div className="mt-1 text-xl font-semibold tabular-nums">{v}</div>
          </div>
        ))}
      </div>

      {noData && (
        <div className="rounded-lg border border-amber-200 bg-amber-50 p-4 text-sm text-amber-800">
          Bu dönem için henüz veri yok. Toplama yeni başladı; ziyaretçiler geldikçe raporlar dolacak.
        </div>
      )}

      <div className="flex gap-1 overflow-x-auto border-b">
        {TABS.map(([k, l]) => (
          <button key={k} onClick={() => setTab(k)}
            className={`whitespace-nowrap px-3 py-2 text-sm border-b-2 -mb-px ${tab === k ? "border-orange-500 font-semibold text-orange-600" : "border-transparent text-gray-500 hover:text-gray-800"}`}>{l}</button>
        ))}
      </div>

      {tab === "ozet" && d && (
        <div className="grid gap-4 lg:grid-cols-2">
          <div className="rounded-lg border bg-white">
            <div className="border-b px-4 py-2 font-semibold">En çok görüntülenen 10 ürün</div>
            <table className="w-full text-sm"><tbody>
              {(d.products || []).slice(0, 10).map((p) => (
                <tr key={p.pid} className="border-t"><Td>{p.name}</Td><Td right>{fmtNum(p.views)}</Td></tr>
              ))}
            </tbody></table>
          </div>
          <div className="rounded-lg border bg-white">
            <div className="border-b px-4 py-2 font-semibold">En çok aranan 10 kelime</div>
            <table className="w-full text-sm"><tbody>
              {(d.searches || []).slice(0, 10).map((s) => (
                <tr key={s.term} className="border-t"><Td>{s.term}</Td><Td right>{fmtNum(s.count)}</Td></tr>
              ))}
            </tbody></table>
          </div>
          <div className="rounded-lg border bg-white">
            <div className="border-b px-4 py-2 font-semibold">En çok gezilen 10 kategori</div>
            <table className="w-full text-sm"><tbody>
              {(d.categories || []).slice(0, 10).map((c) => (
                <tr key={c.cat} className="border-t"><Td>{c.cat}</Td><Td right>{fmtNum(c.views)}</Td></tr>
              ))}
            </tbody></table>
          </div>
          <div className="rounded-lg border bg-white">
            <div className="border-b px-4 py-2 font-semibold">Cihaz dağılımı</div>
            <table className="w-full text-sm"><tbody>
              {Object.entries(T.devices || {}).map(([k, v]) => (
                <tr key={k} className="border-t"><Td>{{ desktop: "Masaüstü", mobile: "Mobil", tablet: "Tablet" }[k] || k}</Td>
                  <Td right>{fmtNum(v)} · {pct(v, T.pageviews)}</Td></tr>
              ))}
            </tbody></table>
          </div>
        </div>
      )}

      {tab === "urun" && d && (
        <div className="overflow-x-auto rounded-lg border bg-white">
          {(d.products || []).length === 0 ? <Empty>Henüz ürün görüntüleme verisi yok</Empty> : (
            <table className="w-full text-sm">
              <thead className="bg-gray-50"><tr>
                <Th>Ürün</Th><Th right>Görüntüleme</Th><Th right>Tekil oturum</Th><Th right>Listede tıklanma</Th>
                <Th right>Ort. süre</Th><Th right>Sepete ekleme</Th><Th right>Sepet oranı</Th><Th right>Satış (site)</Th><Th right>Satışa dönüşüm</Th>
              </tr></thead>
              <tbody>
                {d.products.map((p) => (
                  <tr key={p.pid} className="border-t">
                    <Td><span className="inline-flex items-center gap-2">
                      {p.image ? <img src={p.image} alt="" className="h-9 w-7 rounded object-cover" /> : <span className="h-9 w-7 rounded bg-gray-100" />}
                      <span className="max-w-xs truncate" title={p.name}>{p.name}</span></span></Td>
                    <Td right strong>{fmtNum(p.views)}</Td><Td right>{fmtNum(p.sessions)}</Td><Td right>{fmtNum(p.list_clicks)}</Td>
                    <Td right>{fmtSec(p.avg_sec)}</Td><Td right>{fmtNum(p.atc)}</Td><Td right>{pct(p.atc, p.views)}</Td>
                    <Td right>{fmtNum(p.bought)}</Td><Td right>{pct(p.bought, p.sessions)}</Td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}

      {tab === "kategori" && d && (
        <div className="overflow-x-auto rounded-lg border bg-white">
          {(d.categories || []).length === 0 ? <Empty>Henüz kategori verisi yok</Empty> : (
            <table className="w-full text-sm">
              <thead className="bg-gray-50"><tr><Th>Kategori</Th><Th right>Görüntüleme</Th><Th right>Tekil oturum</Th><Th right>Ort. süre</Th></tr></thead>
              <tbody>{d.categories.map((c) => (
                <tr key={c.cat} className="border-t"><Td>{c.cat}</Td><Td right strong>{fmtNum(c.views)}</Td>
                  <Td right>{fmtNum(c.sessions)}</Td><Td right>{fmtSec(c.avg_sec)}</Td></tr>
              ))}</tbody>
            </table>
          )}
        </div>
      )}

      {tab === "arama" && d && (
        <div className="grid gap-4 lg:grid-cols-[2fr_1fr]">
          <div className="overflow-x-auto rounded-lg border bg-white">
            {(d.searches || []).length === 0 ? <Empty>Henüz arama kaydı yok</Empty> : (
              <table className="w-full text-sm">
                <thead className="bg-gray-50"><tr><Th>Aranan kelime</Th><Th right>Arama</Th><Th right>Ort. sonuç</Th>
                  <Th right>Sonuçsuz</Th><Th right>Ürüne tıklanan</Th></tr></thead>
                <tbody>{d.searches.map((s) => (
                  <tr key={s.term} className="border-t"><Td>{s.term}</Td><Td right strong>{fmtNum(s.count)}</Td>
                    <Td right>{s.avg_results.toLocaleString("tr-TR")}</Td>
                    <Td right>{s.zero ? <span className="text-red-600">{fmtNum(s.zero)}</span> : "0"}</Td>
                    <Td right>{fmtNum(s.clicked)}</Td></tr>
                ))}</tbody>
              </table>
            )}
          </div>
          <div className="rounded-lg border bg-white">
            <div className="border-b px-4 py-2 font-semibold">Sonuç bulunamayan aramalar</div>
            <p className="px-4 pt-2 text-xs text-gray-500">Müşterinin aradığı ama sitede karşılığı olmayan kelimeler — ürün adı/etiket fırsatı.</p>
            {(d.zero_result_searches || []).length === 0 ? <Empty>Yok</Empty> : (
              <ul className="divide-y px-1 text-sm">
                {d.zero_result_searches.map((s) => (
                  <li key={s.term} className="flex items-center justify-between px-3 py-1.5"><span>{s.term}</span><span className="tabular-nums text-gray-600">{fmtNum(s.count)}</span></li>
                ))}
              </ul>
            )}
          </div>
        </div>
      )}

      {tab === "sayfa" && d && (
        <div className="overflow-x-auto rounded-lg border bg-white">
          {(d.page_types || []).length === 0 ? <Empty>Henüz veri yok</Empty> : (
            <table className="w-full text-sm">
              <thead className="bg-gray-50"><tr><Th>Sayfa türü</Th><Th right>Görüntüleme</Th><Th right>Toplam süre</Th>
                <Th right>Ort. süre</Th><Th right>Ort. kaydırma</Th></tr></thead>
              <tbody>{d.page_types.map((p) => (
                <tr key={p.pt} className="border-t"><Td>{PT_LABEL[p.pt] || p.pt}</Td><Td right>{fmtNum(p.views)}</Td>
                  <Td right strong>{fmtNum(p.total_min)} dk</Td><Td right>{fmtSec(p.avg_sec)}</Td>
                  <Td right>{p.avg_scroll == null ? "—" : `%${p.avg_scroll}`}</Td></tr>
              ))}</tbody>
            </table>
          )}
        </div>
      )}

      {tab === "isi" && <Heatmap days={days} />}
    </div>
  );
}
