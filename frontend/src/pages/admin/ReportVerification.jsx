/**
 * ReportVerification.jsx — Rapor Doğrulama (veri havuzu + denetçi).
 *
 * Havuz: 5 Haziran'dan bugüne GÜN × PLATFORM × ÜRÜN sipariş / iptal / iade (adet + tutar).
 * İptal/iade iki tarihle: İŞLEM tarihi (iptal/pusula günü) ve SİPARİŞ tarihi (kohort).
 * Denetçi: 1/7/30/60 gün + 5 Haziran'dan pencerelerinde rapor rakamlarını havuzla karşılaştırır.
 */
import { Fragment, useCallback, useEffect, useState } from "react";
import axios from "axios";
import { toast } from "sonner";
import { RefreshCw, CheckCircle, XCircle } from "lucide-react";

const API = `${process.env.REACT_APP_BACKEND_URL}/api`;
const auth = () => ({ headers: { Authorization: `Bearer ${localStorage.getItem("token")}` } });

const WINDOWS = [["1g", "Bugün"], ["7g", "7 Gün"], ["30g", "30 Gün"], ["60g", "60 Gün"], ["5haz", "5 Haziran'dan"]];
const SOURCES = [["tumu", "Tümü"], ["site", "Site"], ["trendyol", "Trendyol"], ["hepsiburada", "Hepsiburada"], ["amazon", "Amazon"]];
const CH_LABEL = { site: "Site", trendyol: "Trendyol", hepsiburada: "Hepsiburada", amazon: "Amazon", temu: "Temu", n11: "N11", bilinmeyen: "Bilinmeyen" };

const _n = (v) => Number(v || 0);
const fmtNum = (v) => _n(v).toLocaleString("tr-TR");
const fmtMoney = (v) => "₺" + _n(v).toLocaleString("tr-TR", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const fmtVal = (alan, v) => (v == null ? "—" : alan.includes("tutar") ? fmtMoney(v) : fmtNum(v));
const fmtDay = (d) => (d ? String(d).split("-").reverse().join(".") : "—");
const fmtTime = (iso) => { try { return new Date(iso).toLocaleString("tr-TR"); } catch { return "—"; } };

// Havuz satırı → görünen sütunlar (adet + tutar çiftleri)
const COLS = [
  ["Sipariş", "o_units", "o_rev"],
  ["İptal · işlem tarihi", "ce_units", "ce_rev"],
  ["İade · işlem tarihi", "re_units", "re_rev"],
  ["İptal · sipariş tarihi", "cc_units", "cc_rev"],
  ["İade · sipariş tarihi", "rc_units", "rc_rev"],
];
const netOf = (r) => ({ u: _n(r.o_units) - _n(r.cc_units) - _n(r.rc_units), t: _n(r.o_rev) - _n(r.cc_rev) - _n(r.rc_rev) });

function PairCells({ r }) {
  return (
    <>
      {COLS.map(([, u, t]) => (
        <Fragment key={u}>
          <td className="px-3 py-2 text-right tabular-nums">{fmtNum(r[u])}</td>
          <td className="px-3 py-2 text-right tabular-nums text-gray-600">{fmtMoney(r[t])}</td>
        </Fragment>
      ))}
      <td className="px-3 py-2 text-right tabular-nums font-semibold">{fmtNum(netOf(r).u)}</td>
      <td className="px-3 py-2 text-right tabular-nums font-semibold">{fmtMoney(netOf(r).t)}</td>
    </>
  );
}

function PairHead({ first }) {
  return (
    <thead className="bg-gray-50 text-xs text-gray-600">
      <tr>
        <th rowSpan={2} className="px-3 py-2 text-left align-bottom">{first}</th>
        {COLS.map(([l]) => <th key={l} colSpan={2} className="px-3 py-1 text-center border-l">{l}</th>)}
        <th colSpan={2} className="px-3 py-1 text-center border-l">Net (sipariş tarihi)</th>
      </tr>
      <tr>
        {[...COLS, ["net"]].map(([l]) => (
          <Fragment key={l}>
            <th className="px-3 py-1 text-right border-l font-medium">Adet</th>
            <th className="px-3 py-1 text-right font-medium">Tutar</th>
          </Fragment>
        ))}
      </tr>
    </thead>
  );
}

export default function ReportVerification() {
  const [rec, setRec] = useState(null);
  const [sel, setSel] = useState(null);           // seçili denetim hücresi
  const [win, setWin] = useState("30g");
  const [src, setSrc] = useState("tumu");
  const [sum, setSum] = useState(null);
  const [loading, setLoading] = useState(false);
  const [running, setRunning] = useState(false);
  const [tab, setTab] = useState("platform");

  const loadRec = useCallback(async () => {
    try { const { data } = await axios.get(`${API}/admin/report-ledger/reconciliation`, auth()); setRec(data); }
    catch (e) { toast.error(e.response?.data?.detail || "Denetim sonucu alınamadı"); }
  }, []);
  const loadSum = useCallback(async () => {
    setLoading(true);
    try {
      const params = { window: win }; if (src !== "tumu") params.source = src;
      const { data } = await axios.get(`${API}/admin/report-ledger/summary`, { ...auth(), params });
      setSum(data);
    } catch (e) { toast.error(e.response?.data?.detail || "Veri havuzu alınamadı"); }
    finally { setLoading(false); }
  }, [win, src]);

  useEffect(() => { loadRec(); }, [loadRec]);
  useEffect(() => { loadSum(); }, [loadSum]);

  const runNow = async () => {
    setRunning(true);
    const t = toast.loading("Havuz yenileniyor ve raporlar denetleniyor…");
    try {
      const { data } = await axios.post(`${API}/admin/report-ledger/run`, {}, { ...auth(), timeout: 600000 });
      toast.dismiss(t);
      toast.success(data.farkli ? `Denetim bitti: ${data.farkli}/${data.hucre} hücrede fark var` : "Denetim bitti: tüm raporlar havuzla uyumlu");
      await Promise.all([loadRec(), loadSum()]);
    } catch (e) { toast.dismiss(t); toast.error(e.response?.data?.detail || "Denetim çalıştırılamadı"); }
    finally { setRunning(false); }
  };

  const cell = (w, s) => (rec?.cells || []).find((c) => c.pencere === w && c.kaynak === s);
  const T = sum?.totals || {};

  return (
    <div className="space-y-6" data-testid="report-verification">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold">Rapor Doğrulama</h1>
          <p className="mt-1 text-sm text-gray-500">
            Panel siparişlerinden 5 Haziran'dan bugüne gün gün, platform platform ve ürün ürün oluşturulan veri havuzu.
            Raporlar bu havuzla otomatik karşılaştırılır (30 dk'da bir; gece tam yeniden kurulum).
          </p>
        </div>
        <button onClick={runNow} disabled={running}
          className="inline-flex items-center gap-2 h-10 px-4 rounded-md bg-gray-900 text-white text-sm font-medium hover:bg-gray-800 disabled:opacity-60">
          <RefreshCw size={16} className={running ? "animate-spin" : ""} /> Şimdi Denetle
        </button>
      </div>

      {/* DENETİM MATRİSİ */}
      <section className="rounded-lg border bg-white">
        <div className="flex items-center justify-between border-b px-4 py-3">
          <h2 className="font-semibold">Rapor ↔ Havuz Karşılaştırması</h2>
          <span className="text-xs text-gray-500">
            Son denetim: {rec?.at ? fmtTime(rec.at) : "henüz yok"}
            {rec?.hucre ? ` · ${rec.hucre - (rec.farkli || 0)}/${rec.hucre} uyumlu` : ""}
          </span>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="bg-gray-50 text-xs text-gray-600">
              <tr>
                <th className="px-4 py-2 text-left">Dönem</th>
                {SOURCES.map(([k, l]) => <th key={k} className="px-4 py-2 text-center">{l}</th>)}
              </tr>
            </thead>
            <tbody>
              {WINDOWS.map(([w, wl]) => (
                <tr key={w} className="border-t">
                  <td className="px-4 py-2 font-medium">{wl}</td>
                  {SOURCES.map(([s]) => {
                    const c = cell(w, s);
                    const bad = c ? (c.alanlar || []).filter((x) => !x.ok).length : 0;
                    const active = sel && sel.pencere === w && sel.kaynak === s;
                    return (
                      <td key={s} className="px-4 py-2 text-center">
                        {c ? (
                          <button onClick={() => setSel(active ? null : c)}
                            className={`inline-flex items-center justify-center gap-1.5 h-8 min-w-[88px] px-3 rounded-md text-xs font-medium border ${c.ok ? "border-green-200 bg-green-50 text-green-700" : "border-red-200 bg-red-50 text-red-700"} ${active ? "ring-2 ring-offset-1 ring-gray-400" : ""}`}>
                            {c.ok ? <CheckCircle size={14} /> : <XCircle size={14} />}
                            {c.ok ? "Uyumlu" : (c.hata ? "Hata" : `${bad} fark`)}
                          </button>
                        ) : <span className="text-xs text-gray-400">—</span>}
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {sel && (
          <div className="border-t px-4 py-3">
            <div className="mb-2 text-sm font-medium">
              {WINDOWS.find(([w]) => w === sel.pencere)?.[1]} · {SOURCES.find(([s]) => s === sel.kaynak)?.[1]}
              <span className="ml-2 text-xs font-normal text-gray-500">{fmtDay(sel.baslangic)} – {fmtDay(sel.bitis)}{sel.yeniden_hesaplandi ? " · rapor yeniden hesaplatıldı" : ""}</span>
            </div>
            {sel.hata ? <div className="text-sm text-red-600">{sel.hata}</div> : (
              <table className="w-full text-sm">
                <thead className="text-xs text-gray-600"><tr>
                  <th className="py-1 text-left">Rapor alanı</th><th className="py-1 text-right">Raporda</th>
                  <th className="py-1 text-right">Havuzda</th><th className="py-1 text-right">Fark</th>
                </tr></thead>
                <tbody>
                  {(sel.alanlar || []).map((x) => (
                    <tr key={x.alan} className={`border-t ${x.ok ? "" : "bg-red-50"}`}>
                      <td className="py-1.5">{x.alan}</td>
                      <td className="py-1.5 text-right tabular-nums">{fmtVal(x.alan, x.rapor)}</td>
                      <td className="py-1.5 text-right tabular-nums">{fmtVal(x.alan, x.havuz)}</td>
                      <td className={`py-1.5 text-right tabular-nums ${x.ok ? "text-gray-400" : "font-semibold text-red-700"}`}>{x.ok ? "✓" : fmtVal(x.alan, x.fark)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        )}
      </section>

      {/* VERİ HAVUZU */}
      <section className="rounded-lg border bg-white">
        <div className="flex flex-wrap items-center justify-between gap-3 border-b px-4 py-3">
          <h2 className="font-semibold">Veri Havuzu</h2>
          <div className="flex flex-wrap items-center gap-2">
            {WINDOWS.map(([w, l]) => (
              <button key={w} onClick={() => setWin(w)}
                className={`h-8 px-3 rounded-md text-xs font-medium border ${win === w ? "bg-gray-900 text-white border-gray-900" : "bg-white text-gray-700 border-gray-300 hover:bg-gray-50"}`}>{l}</button>
            ))}
            <select value={src} onChange={(e) => setSrc(e.target.value)} className="h-8 rounded-md border border-gray-300 px-2 text-xs">
              {SOURCES.map(([k, l]) => <option key={k} value={k}>{l}</option>)}
            </select>
          </div>
        </div>
        <div className="grid grid-cols-2 gap-3 p-4 md:grid-cols-6">
          {[["Sipariş", T.o_units, T.o_rev], ["İptal · işlem", T.ce_units, T.ce_rev], ["İade · işlem", T.re_units, T.re_rev],
            ["İptal · sipariş t.", T.cc_units, T.cc_rev], ["İade · sipariş t.", T.rc_units, T.rc_rev], ["Net · sipariş t.", netOf(T).u, netOf(T).t]].map(([l, u, t]) => (
            <div key={l} className="rounded-md border p-3">
              <div className="text-xs text-gray-500">{l}</div>
              <div className="mt-1 text-lg font-semibold tabular-nums">{fmtNum(u)} <span className="text-xs font-normal text-gray-500">adet</span></div>
              <div className="text-sm tabular-nums text-gray-700">{fmtMoney(t)}</div>
            </div>
          ))}
        </div>
        <div className="flex gap-1 border-b px-4">
          {[["platform", "Platform"], ["urun", "Ürün"], ["gun", "Gün"]].map(([k, l]) => (
            <button key={k} onClick={() => setTab(k)}
              className={`px-3 py-2 text-sm border-b-2 -mb-px ${tab === k ? "border-orange-500 font-semibold text-orange-600" : "border-transparent text-gray-500 hover:text-gray-800"}`}>{l}</button>
          ))}
          <span className="ml-auto self-center text-xs text-gray-500">
            {sum ? `${fmtDay(sum.start)} – ${fmtDay(sum.end)} · havuz güncellendi: ${sum.built_at ? fmtTime(sum.built_at) : "henüz kurulmadı"}` : ""}
          </span>
        </div>
        <div className="overflow-x-auto">
          {loading ? <div className="p-6 text-center text-sm text-gray-500">Yükleniyor…</div> : (
            <table className="w-full text-sm">
              <PairHead first={tab === "platform" ? "Platform" : tab === "urun" ? "Ürün" : "Gün"} />
              <tbody>
                {(tab === "platform" ? (sum?.by_platform || []) : tab === "urun" ? (sum?.products || []) : (sum?.daily || [])).map((r) => (
                  <tr key={r._id} className="border-t">
                    <td className="px-3 py-2">
                      {tab === "platform" ? (CH_LABEL[r._id] || r._id)
                        : tab === "gun" ? fmtDay(r._id)
                          : (<span className="block max-w-xs truncate" title={r.name}>{r.name || r._id}{r.barcode ? <span className="ml-1.5 text-xs text-gray-400">{r.barcode}</span> : null}</span>)}
                    </td>
                    <PairCells r={r} />
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </section>
    </div>
  );
}
