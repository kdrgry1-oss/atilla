/**
 * CustomerSegments.jsx — RFM Müşteri Segmentasyonu
 *
 * Recency / Frequency / Monetary puanlarına göre müşteri grupları.
 * Pazarlama ekibi segment bazlı kupon, SMS, e-posta kampanyaları üretir.
 *
 * Backend: /api/analytics-extra/rfm?lookback_days=365
 */
import { useEffect, useMemo, useState } from "react";
import axios from "axios";
import { toast } from "sonner";
import { Users, Trophy, HeartPulse, Rocket, AlertOctagon, UserMinus, Search, Download, Mail, X, Send, ArrowUp, ArrowDown, ArrowUpDown } from "lucide-react";
import { sanitizeHtml } from "../../lib/sanitizeHtml";

const API = `${process.env.REACT_APP_BACKEND_URL}/api`;

const SEGMENT_META = {
  "VIP / Şampiyon": { cls: "bg-purple-100 text-purple-700 border-purple-200", icon: Trophy },
  "Sadık Müşteri": { cls: "bg-green-100 text-green-700 border-green-200", icon: HeartPulse },
  "Yeni Müşteri": { cls: "bg-blue-100 text-blue-700 border-blue-200", icon: Rocket },
  "Potansiyel Sadık": { cls: "bg-teal-100 text-teal-700 border-teal-200", icon: HeartPulse },
  "Risk Altında (Kayıp Uyarısı)": { cls: "bg-orange-100 text-orange-700 border-orange-200", icon: AlertOctagon },
  "Dikkat Edilmeli": { cls: "bg-yellow-100 text-yellow-800 border-yellow-200", icon: AlertOctagon },
  "Kaybedilen": { cls: "bg-red-100 text-red-700 border-red-200", icon: UserMinus },
  "Hibernasyon": { cls: "bg-gray-100 text-gray-600 border-gray-200", icon: UserMinus },
  "Standart": { cls: "bg-gray-50 text-gray-600 border-gray-200", icon: Users },
};

// Sıralanabilir sütunlar: [anahtar, başlık, değer okuyucu, ipucu]
const COLUMNS = [
  ["segment", "Segment", (i) => i.segment || ""],
  ["email", "Email", (i) => i.email || ""],
  ["name", "Ad", (i) => i.name || ""],
  ["r", "R", (i) => i.r, "Yakınlık puanı (5 = en yeni)"],
  ["f", "F", (i) => i.f, "Sıklık puanı (5 = en sık)"],
  ["m", "M", (i) => i.m, "Harcama puanı (5 = en yüksek)"],
  ["order_count", "Sipariş", (i) => i.order_count],
  ["total_spent", "Net Harcama", (i) => i.total_spent || 0, "NET: iptal/tam iade/ödenmemiş siparişler sayılmaz, kısmi iade tutarı düşülmüştür"],
  ["returns_deducted", "İade Düşülen", (i) => i.returns_deducted || 0, "Sayılan siparişlerden düşülen onaylı iade tutarı"],
  ["recency_days", "Son Sipariş", (i) => -(i.recency_days ?? 99999), "Son siparişten bu yana geçen gün"],
];
const PAGE = 200;

export default function CustomerSegments() {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [lookback, setLookback] = useState(365);
  const [search, setSearch] = useState("");
  const [segmentFilter, setSegmentFilter] = useState("");
  const [campaignOpen, setCampaignOpen] = useState(false);
  const [sortKey, setSortKey] = useState("");          // "" = sunucu sırası (M, F, yakınlık)
  const [sortDir, setSortDir] = useState("desc");
  const [minOrders, setMinOrders] = useState("");
  const [minSpent, setMinSpent] = useState("");
  const [maxDays, setMaxDays] = useState("");
  const [shown, setShown] = useState(PAGE);

  const token = useMemo(() => localStorage.getItem("token"), []);
  const auth = { headers: { Authorization: `Bearer ${token}` } };

  const load = async () => {
    setLoading(true);
    try {
      const r = await axios.get(`${API}/analytics-extra/rfm?lookback_days=${lookback}`, auth);
      setData(r.data);
    } catch { toast.error("Yüklenemedi"); }
    finally { setLoading(false); }
  };
  useEffect(() => { load(); /* eslint-disable-next-line */ }, [lookback]);

  const filtered = useMemo(() => {
    if (!data) return [];
    let items = data.items;
    if (segmentFilter) items = items.filter((i) => i.segment === segmentFilter);
    if (search) {
      const s = search.toLocaleLowerCase("tr").trim();
      items = items.filter((i) =>
        (i.email || "").toLocaleLowerCase("tr").includes(s) ||
        (i.name || "").toLocaleLowerCase("tr").includes(s)
      );
    }
    if (minOrders !== "") items = items.filter((i) => (i.order_count || 0) >= Number(minOrders));
    if (minSpent !== "") items = items.filter((i) => (i.total_spent || 0) >= Number(minSpent));
    if (maxDays !== "") items = items.filter((i) => (i.recency_days ?? 99999) <= Number(maxDays));
    if (sortKey) {
      const get = COLUMNS.find((c) => c[0] === sortKey)[2];
      const dir = sortDir === "asc" ? 1 : -1;
      items = [...items].sort((a, b) => {
        const x = get(a), y = get(b);
        const c = typeof x === "string" ? x.localeCompare(y, "tr") : x - y;
        return c * dir;
      });
    }
    return items;
  }, [data, segmentFilter, search, minOrders, minSpent, maxDays, sortKey, sortDir]);

  useEffect(() => { setShown(PAGE); }, [segmentFilter, search, minOrders, minSpent, maxDays, sortKey, sortDir, lookback]);

  const toggleSort = (key) => {
    if (sortKey !== key) { setSortKey(key); setSortDir(["email", "name", "segment"].includes(key) ? "asc" : "desc"); }
    else if (sortDir === "desc") setSortDir("asc");
    else if (sortDir === "asc" && !["email", "name", "segment"].includes(key)) { setSortKey(""); setSortDir("desc"); }
    else setSortDir("desc");
  };
  const anyFilter = !!(segmentFilter || search || minOrders !== "" || minSpent !== "" || maxDays !== "");
  const clearFilters = () => { setSegmentFilter(""); setSearch(""); setMinOrders(""); setMinSpent(""); setMaxDays(""); };

  const exportCsv = () => {
    if (!filtered.length) return;
    const header = ["Email", "Ad", "Telefon", "Segment", "R", "F", "M", "RFM", "Sipariş", "Net Harcama", "İade Düşülen", "Son Sipariş", "Recency (gün)"];
    const rows = filtered.map((i) => [i.email, i.name || "", i.phone || "", i.segment, i.r, i.f, i.m, i.rfm, i.order_count, i.total_spent, i.returns_deducted ?? 0, i.last_order, i.recency_days]);
    const csv = [header, ...rows].map((r) => r.map((c) => `"${String(c ?? "").replace(/"/g, '""')}"`).join(",")).join("\n");
    const blob = new Blob(["\uFEFF" + csv], { type: "text/csv;charset=utf-8;" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `musteri-segmentleri-${Date.now()}.csv`;
    a.click();
    URL.revokeObjectURL(a.href);
  };

  return (
    <div data-testid="customer-segments-page">
      <div className="flex items-center justify-between mb-4">
        <div>
          <h1 className="text-2xl font-bold flex items-center gap-2">
            <Users size={20} /> Müşteri Segmentasyonu (RFM)
          </h1>
          <p className="text-sm text-gray-500 mt-1">
            Recency (yakınlık) · Frequency (sıklık) · Monetary (ciro) skorlarına göre müşteriler segmentlere ayrılır.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <select value={lookback} onChange={(e) => setLookback(parseInt(e.target.value))}
            className="border border-gray-200 rounded-lg px-2 py-1.5 text-sm bg-white"
            data-testid="rfm-lookback">
            <option value={90}>Son 90 gün</option>
            <option value={180}>Son 180 gün</option>
            <option value={365}>Son 1 yıl</option>
            <option value={730}>Son 2 yıl</option>
          </select>
          <button onClick={exportCsv} disabled={!filtered.length}
            className="flex items-center gap-1 px-3 py-2 border border-gray-200 rounded-lg text-sm hover:bg-gray-50 disabled:opacity-50">
            <Download size={14} /> Excel'e Aktar
          </button>
          <button onClick={() => setCampaignOpen(true)} disabled={!filtered.length}
            data-testid="open-campaign-btn"
            className="flex items-center gap-1 px-3 py-2 bg-black hover:bg-gray-800 text-white rounded-lg text-sm disabled:opacity-50">
            <Mail size={14} /> Kampanya Gönder ({filtered.length})
          </button>
        </div>
      </div>

      {/* Segment özet kartları */}
      {data && (
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mb-4" data-testid="rfm-segment-cards">
          {Object.entries(data.segments).map(([seg, cnt]) => {
            const meta = SEGMENT_META[seg] || SEGMENT_META["Standart"];
            const Icon = meta.icon;
            const active = segmentFilter === seg;
            return (
              <button key={seg}
                onClick={() => setSegmentFilter(active ? "" : seg)}
                className={`text-left border rounded-xl p-3 transition-all ${meta.cls} ${active ? "ring-2 ring-black" : ""}`}
                data-testid={`rfm-segment-${seg}`}>
                <div className="flex items-center gap-2 mb-1">
                  <Icon size={14} />
                  <span className="text-xs font-bold uppercase tracking-wider">{seg}</span>
                </div>
                <div className="text-2xl font-black">{cnt}</div>
                <div className="text-[11px]">müşteri</div>
              </button>
            );
          })}
        </div>
      )}

      {/* Arama + filtreler */}
      <div className="flex flex-wrap items-center gap-2 mb-3" data-testid="rfm-filters">
        <div className="relative w-72">
          <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400" />
          <input value={search} onChange={(e) => setSearch(e.target.value)}
            placeholder="Ad, soyad veya e-posta ara..."
            className="w-full h-9 border border-gray-200 rounded-lg pl-8 pr-3 text-sm"
            data-testid="rfm-search" />
        </div>
        <select value={segmentFilter} onChange={(e) => setSegmentFilter(e.target.value)}
          className="h-9 border border-gray-200 rounded-lg px-2 text-sm bg-white" data-testid="rfm-segment-select">
          <option value="">Tüm segmentler</option>
          {Object.keys(data?.segments || {}).map((sg) => <option key={sg} value={sg}>{sg}</option>)}
        </select>
        <input type="number" min="0" value={minOrders} onChange={(e) => setMinOrders(e.target.value)}
          placeholder="Sipariş ≥" className="h-9 w-28 border border-gray-200 rounded-lg px-3 text-sm" data-testid="rfm-min-orders" />
        <input type="number" min="0" value={minSpent} onChange={(e) => setMinSpent(e.target.value)}
          placeholder="Harcama ≥ ₺" className="h-9 w-32 border border-gray-200 rounded-lg px-3 text-sm" data-testid="rfm-min-spent" />
        <input type="number" min="0" value={maxDays} onChange={(e) => setMaxDays(e.target.value)}
          placeholder="Son sipariş ≤ gün" className="h-9 w-40 border border-gray-200 rounded-lg px-3 text-sm" data-testid="rfm-max-days" />
        {anyFilter && (
          <button onClick={clearFilters} className="h-9 px-3 text-xs text-gray-600 border border-gray-200 rounded-lg hover:bg-gray-50">
            Filtreleri Temizle
          </button>
        )}
        <span className="text-xs text-gray-500 ml-auto">
          {filtered.length} / {data?.total || 0} müşteri
        </span>
      </div>

      <div className="bg-white border rounded-xl shadow-sm overflow-hidden">
        <table className="admin-table admin-table-compact">
          <thead>
            <tr>
              {COLUMNS.map(([key, label, , tip]) => {
                const active = sortKey === key;
                const Icon = !active ? ArrowUpDown : sortDir === "asc" ? ArrowUp : ArrowDown;
                return (
                  <th key={key} title={tip}>
                    <button type="button" onClick={() => toggleSort(key)}
                      className={`inline-flex items-center gap-1 uppercase hover:text-black ${active ? "text-black" : ""}`}
                      data-testid={`rfm-sort-${key}`}>
                      {label}
                      <Icon size={12} className={active ? "" : "opacity-40"} />
                    </button>
                  </th>
                );
              })}
            </tr>
          </thead>
          <tbody>
            {loading ? (
              <tr><td colSpan={10} className="text-center py-8 text-gray-400">Yükleniyor...</td></tr>
            ) : filtered.length === 0 ? (
              <tr><td colSpan={10} className="text-center py-10 text-gray-400">Kayıt yok</td></tr>
            ) : (
              filtered.slice(0, shown).map((i, idx) => {
                const meta = SEGMENT_META[i.segment] || SEGMENT_META["Standart"];
                return (
                  <tr key={idx}>
                    <td>
                      <span className={`inline-flex items-center text-[10px] font-bold px-2 py-0.5 rounded-full border ${meta.cls}`}>
                        {i.segment}
                      </span>
                    </td>
                    <td className="text-xs font-mono text-gray-700">{i.email}</td>
                    <td className="text-sm">{i.name || <span className="text-gray-400">—</span>}</td>
                    <td><span className="font-mono text-xs font-bold">{i.r}</span></td>
                    <td><span className="font-mono text-xs font-bold">{i.f}</span></td>
                    <td><span className="font-mono text-xs font-bold">{i.m}</span></td>
                    <td className="text-sm font-semibold">{i.order_count}</td>
                    <td className="text-sm font-bold text-green-700">{i.total_spent?.toFixed(2)} ₺</td>
                    <td className="text-xs text-red-600">{i.returns_deducted > 0 ? `−${i.returns_deducted.toFixed(2)} ₺` : "—"}</td>
                    <td className="text-[11px] text-gray-500">
                      {i.last_order ? `${new Date(i.last_order).toLocaleDateString("tr-TR")} (${i.recency_days}g)` : "-"}
                    </td>
                  </tr>
                );
              })
            )}
          </tbody>
        </table>
        {!loading && filtered.length > shown && (
          <div className="flex items-center justify-center gap-3 py-3 border-t text-sm">
            <span className="text-gray-500">{shown} / {filtered.length} gösteriliyor</span>
            <button onClick={() => setShown(shown + PAGE)} className="px-3 py-1.5 border border-gray-200 rounded-lg hover:bg-gray-50">
              {PAGE} daha göster
            </button>
            <button onClick={() => setShown(filtered.length)} className="px-3 py-1.5 border border-gray-200 rounded-lg hover:bg-gray-50">
              Tümünü göster
            </button>
          </div>
        )}
      </div>

      {/* Kampanya Gönder Modal */}
      {campaignOpen && (
        <CampaignModal
          recipients={filtered.map((i) => i.email).filter(Boolean)}
          segmentLabel={segmentFilter || "Tüm Filtrelenenler"}
          onClose={() => setCampaignOpen(false)}
        />
      )}
    </div>
  );
}

/* ───────────────────────── CampaignModal ───────────────────────── */

function CampaignModal({ recipients, segmentLabel, onClose }) {
  const [subject, setSubject] = useState("");
  const [html, setHtml] = useState(
    `<div style="font-family:Mulish,sans-serif;max-width:600px;margin:0 auto;background:#fff;padding:32px;color:#000;">\n  <h1 style="font-size:22px;font-weight:300;letter-spacing:0.05em;margin-bottom:16px;">Merhaba,</h1>\n  <p style="font-size:14px;line-height:1.6;color:#333;">\n    Sizin için özel bir teklifimiz var...\n  </p>\n  <a href="https://self-host-test.preview.emergentagent.com" style="display:inline-block;background:#000;color:#fff;padding:14px 32px;text-decoration:none;font-size:11px;letter-spacing:0.2em;text-transform:uppercase;margin-top:16px;">Alışverişe Başla</a>\n</div>`
  );
  const [sending, setSending] = useState(false);
  const [result, setResult] = useState(null);
  const token = localStorage.getItem("token");

  const send = async () => {
    if (!subject.trim() || !html.trim()) {
      toast.error("Konu ve HTML içerik zorunlu");
      return;
    }
    if (!recipients.length) {
      toast.error("Alıcı yok");
      return;
    }
    setSending(true);
    try {
      const r = await axios.post(`${API}/admin/email/send-to-emails`,
        { emails: recipients, subject: subject.trim(), html: html.trim(), segment_label: segmentLabel },
        { headers: { Authorization: `Bearer ${token}` } }
      );
      setResult(r.data.result);
      if (r.data.result?.success > 0) {
        toast.success(`${r.data.result.success} e-posta başarıyla gönderildi`);
      } else if (r.data.result?.failed > 0) {
        toast.error(`Hepsi başarısız (${r.data.result.errors?.[0] || "Bilinmiyor"})`);
      }
    } catch (e) {
      toast.error(e.response?.data?.detail || "Gönderim başarısız");
    } finally {
      setSending(false);
    }
  };

  return (
    <div className="fixed inset-0 bg-black/50 z-50 flex items-center justify-center p-4" data-testid="campaign-modal">
      <div className="bg-white rounded-xl max-w-3xl w-full max-h-[90vh] overflow-y-auto">
        <div className="flex items-center justify-between p-5 border-b">
          <h3 className="text-lg font-bold flex items-center gap-2">
            <Mail size={18} /> Kampanya Gönder
            <span className="text-sm font-normal text-gray-500">→ {segmentLabel}</span>
          </h3>
          <button onClick={onClose} className="p-1 hover:bg-gray-100 rounded">
            <X size={18} />
          </button>
        </div>
        <div className="p-5 space-y-4">
          <div className="bg-blue-50 border border-blue-200 rounded-lg p-3 text-sm">
            <strong>{recipients.length}</strong> alıcıya gönderilecek
            <span className="text-xs text-blue-700 block mt-1">
              {recipients.slice(0, 3).join(", ")}{recipients.length > 3 ? `, +${recipients.length - 3} daha` : ""}
            </span>
          </div>

          <div>
            <label className="block text-xs font-semibold uppercase tracking-wider text-gray-600 mb-1">E-posta Konusu</label>
            <input value={subject} onChange={(e) => setSubject(e.target.value)}
              placeholder="Örn: VIP müşterilerimize özel %20 indirim"
              className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:border-black"
              data-testid="campaign-subject" />
          </div>

          <div>
            <div className="flex items-center justify-between mb-1">
              <label className="text-xs font-semibold uppercase tracking-wider text-gray-600">HTML İçerik</label>
              <span className="text-[10px] text-gray-400">Mulish font + minimal stil önerilir</span>
            </div>
            <textarea value={html} onChange={(e) => setHtml(e.target.value)}
              rows={10}
              className="w-full border border-gray-300 rounded-lg px-3 py-2 text-xs font-mono focus:outline-none focus:border-black"
              data-testid="campaign-html" />
          </div>

          {/* Önizleme */}
          <details className="border border-gray-200 rounded-lg">
            <summary className="px-3 py-2 cursor-pointer text-sm font-medium hover:bg-gray-50">📧 Önizleme</summary>
            <div className="p-3 bg-gray-50 border-t" dangerouslySetInnerHTML={{ __html: sanitizeHtml(html, { allowStyle: true }) }} />
          </details>

          {result && (
            <div className={`rounded-lg p-3 text-sm border ${result.success > 0 ? "bg-green-50 border-green-200 text-green-800" : "bg-red-50 border-red-200 text-red-800"}`}>
              ✅ Başarılı: <strong>{result.success}</strong> &nbsp;·&nbsp;
              ❌ Başarısız: <strong>{result.failed}</strong>
              {result.errors?.length > 0 && (
                <ul className="text-xs mt-2 space-y-0.5">
                  {result.errors.map((e, i) => <li key={i}>• {e}</li>)}
                </ul>
              )}
            </div>
          )}
        </div>
        <div className="flex items-center justify-end gap-2 p-5 border-t bg-gray-50">
          <button onClick={onClose} className="px-4 py-2 border border-gray-300 rounded-lg text-sm hover:bg-white">
            Kapat
          </button>
          <button onClick={send} disabled={sending || !!result}
            data-testid="campaign-send-btn"
            className="flex items-center gap-2 px-5 py-2 bg-black hover:bg-gray-800 text-white rounded-lg text-sm disabled:opacity-50">
            <Send size={14} />
            {sending ? "Gönderiliyor..." : result ? "Gönderildi" : `${recipients.length} Kişiye Gönder`}
          </button>
        </div>
      </div>
    </div>
  );
}
