import { useEffect, useRef, useState } from "react";
import axios from "axios";
import { Eye, Info } from "lucide-react";
import ProductCard from "../ProductCard";

const API = `${process.env.REACT_APP_BACKEND_URL}/api`;

/**
 * Kampanya ÖNİZLEME — formdaki (henüz kaydedilmemiş) kampanyanın kapsamdaki ürün
 * kartlarında NASIL görüneceğini vitrinin GERÇEK ürün kartıyla gösterir.
 * Sunucu, vitrinle aynı rozet/indirim fonksiyonlarını kullanır (POST /campaigns/preview).
 */
export default function CampaignPreview({ form, editingId, columns = "grid-cols-2", limit = 24 }) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState("");
  const seq = useRef(0);

  useEffect(() => {
    const my = ++seq.current;
    const t = setTimeout(async () => {
      setLoading(true); setErr("");
      try {
        const r = await axios.post(`${API}/campaigns/preview`, { ...form, id: editingId || undefined, limit });
        if (my === seq.current) setData(r.data);
      } catch (e) {
        if (my === seq.current) setErr(e.response?.data?.detail || "Önizleme alınamadı");
      } finally {
        if (my === seq.current) setLoading(false);
      }
    }, 450);
    return () => clearTimeout(t);
  }, [form, editingId, limit]);

  const items = data?.items || [];
  const applied = items.filter((x) => x.applies).length;

  return (
    <div className="space-y-3" data-testid="campaign-preview">
      <div className="flex items-center justify-between gap-3">
        <div className="inline-flex items-center gap-2 text-sm font-semibold text-gray-800">
          <Eye size={16} /> Ürün kartı önizlemesi
        </div>
        <div className="text-xs text-gray-500 tabular-nums">
          {loading ? "Hesaplanıyor…" : data ? `${data.total} ürün kapsamda · ${data.shown} gösteriliyor · ${applied} üründe indirim görünür` : ""}
        </div>
      </div>
      {err && <div className="text-xs text-red-600">{err}</div>}
      {(data?.notes || []).length > 0 && (
        <div className="rounded-lg border border-amber-200 bg-amber-50 p-2.5 space-y-1">
          {data.notes.map((n, i) => (
            <div key={i} className="flex items-start gap-1.5 text-[11px] text-amber-800">
              <Info size={12} className="mt-0.5 shrink-0" /> <span>{n}</span>
            </div>
          ))}
        </div>
      )}
      {data && items.length === 0 && !loading && (
        <div className="text-xs text-gray-400 py-8 text-center">Kapsamda ürün yok.</div>
      )}
      <div className={`grid ${columns} gap-4`}>
        {items.map((it) => (
          <div key={it.card.id} className="space-y-1.5">
            <div className="pointer-events-none select-none">
              <ProductCard product={it.card} />
            </div>
            {!it.applies && (
              <div className="text-[11px] font-medium text-gray-400">{it.reason || "Kartta indirim görünmez"}</div>
            )}
            {it.state && (
              <div className="text-[10px] text-amber-700">{it.state} — önizlemede aktif ve stokta gösteriliyor</div>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}
