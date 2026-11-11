/**
 * TrendyolExcelMatch.jsx
 *
 * Trendyol satıcı panelinden indirilen "Ürünleriniz" Excel'ini yükler; sistemdeki varyantlarla
 * (Tedarikçi Stok Kodu + beden → ürün adı + beden) eşleştirip Trendyol barkodunu varyanta yazar,
 * kategori eşleştirmesini (sistem kategorisi → Trendyol kategorisi) çıkarır.
 * Endpoint: POST /api/integrations/trendyol-excel-match (multipart, ?dry_run=)
 *           GET  /api/integrations/trendyol-excel-match            (son sonuç)
 *           GET  /api/integrations/trendyol-excel-match/unmatched.xlsx
 */
import { useEffect, useState } from "react";
import axios from "axios";
import { toast } from "sonner";
import { Upload, FileSpreadsheet, Download, CheckCircle2, AlertTriangle, Tags } from "lucide-react";

const API = `${process.env.REACT_APP_BACKEND_URL}/api`;
const auth = () => ({ headers: { Authorization: `Bearer ${localStorage.getItem("token")}` } });

const fmt = (n) => (typeof n === "number" ? n.toLocaleString("tr-TR") : "—");

function Stat({ label, value, tone = "gray" }) {
  const tones = {
    gray: "text-gray-900",
    green: "text-green-700",
    amber: "text-amber-700",
    red: "text-red-700",
  };
  return (
    <div className="border rounded p-3 bg-white">
      <div className="text-xs text-gray-500">{label}</div>
      <div className={`mt-1 text-xl font-semibold tabular-nums text-right ${tones[tone]}`}>{fmt(value)}</div>
    </div>
  );
}

export default function TrendyolExcelMatch() {
  const [file, setFile] = useState(null);
  const [busy, setBusy] = useState(false);
  const [report, setReport] = useState(null);

  useEffect(() => {
    axios
      .get(`${API}/integrations/trendyol-excel-match`, auth())
      .then((r) => r.data && r.data.stats && setReport(r.data))
      .catch(() => {});
  }, []);

  const run = async (dryRun) => {
    if (!file) {
      toast.error("Önce Trendyol'dan indirdiğiniz Excel dosyasını seçin");
      return;
    }
    const fd = new FormData();
    fd.append("file", file);
    setBusy(true);
    try {
      const r = await axios.post(
        `${API}/integrations/trendyol-excel-match?dry_run=${dryRun ? "true" : "false"}`,
        fd,
        { ...auth(), timeout: 300000 }
      );
      setReport(r.data);
      toast.success(dryRun ? "Ön izleme hazır (kaydedilmedi)" : "Eşleştirme kaydedildi");
    } catch (e) {
      toast.error(e?.response?.data?.detail || "Eşleştirme başarısız");
    } finally {
      setBusy(false);
    }
  };

  const downloadUnmatched = async () => {
    try {
      const r = await axios.get(`${API}/integrations/trendyol-excel-match/unmatched.xlsx`, {
        ...auth(),
        responseType: "blob",
      });
      const url = URL.createObjectURL(r.data);
      const a = document.createElement("a");
      a.href = url;
      a.download = "trendyol-eslesmeyenler.xlsx";
      a.click();
      URL.revokeObjectURL(url);
    } catch {
      toast.error("Liste indirilemedi");
    }
  };

  const st = report?.stats || {};

  return (
    <div className="p-6 space-y-6" data-testid="trendyol-excel-match">
      <div>
        <h1 className="text-2xl font-semibold inline-flex items-center gap-2">
          <FileSpreadsheet className="w-6 h-6" /> Trendyol Excel Eşleştirme
        </h1>
        <p className="mt-1 text-sm text-gray-600">
          Trendyol satıcı panelinden indirdiğiniz <b>Ürünleriniz</b> Excel'ini yükleyin. Her varyant, Tedarikçi Stok
          Kodu + beden (yoksa ürün adı + beden) ile eşleştirilir ve Trendyol barkodu varyanta yazılır; böylece stok ve
          fiyat güncellemeleri doğru Trendyol ilanına gider. Elle girdiğiniz barkod ve kategori eşleştirmeleri korunur.
        </p>
      </div>

      <div className="border rounded p-4 bg-white flex flex-wrap items-center gap-3">
        <label className="inline-flex items-center gap-2 px-3 py-2 border rounded cursor-pointer hover:bg-gray-50 text-sm">
          <Upload className="w-4 h-4" />
          <span>{file ? file.name : "Excel dosyası seç (.xlsx)"}</span>
          <input
            type="file"
            accept=".xlsx,.xls"
            className="hidden"
            onChange={(e) => setFile(e.target.files?.[0] || null)}
            data-testid="ty-excel-file"
          />
        </label>
        <button
          type="button"
          disabled={busy}
          onClick={() => run(true)}
          className="inline-flex items-center gap-2 px-3 py-2 border rounded text-sm hover:bg-gray-50 disabled:opacity-50"
        >
          Ön izleme (kaydetmeden)
        </button>
        <button
          type="button"
          disabled={busy}
          onClick={() => run(false)}
          className="inline-flex items-center gap-2 px-3 py-2 rounded text-sm bg-black text-white hover:bg-gray-800 disabled:opacity-50"
          data-testid="ty-excel-run"
        >
          <CheckCircle2 className="w-4 h-4" /> {busy ? "Eşleştiriliyor…" : "Eşleştir ve kaydet"}
        </button>
      </div>

      {report && (
        <>
          <div className="text-xs text-gray-500">
            {report.dry_run ? "Ön izleme" : "Son kayıtlı eşleştirme"} ·{" "}
            {report.finished_at ? new Date(report.finished_at).toLocaleString("tr-TR") : ""}
          </div>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
            <Stat label="Sistem varyantı" value={st.variants} />
            <Stat label="Eşleşen varyant" value={st.matched_variants} tone="green" />
            <Stat label="Eşleşen ürün" value={st.matched_products} tone="green" />
            <Stat label="Eşleşmeyen varyant" value={st.unmatched_variants || 0} tone="amber" />
            <Stat label="Excel satırı" value={st.excel_rows} />
            <Stat label="Excel'de eşleşmeyen (aktif)" value={st.excel_unmatched_active} tone="amber" />
            <Stat label="Excel'de eşleşmeyen (arşiv/ihlal)" value={st.excel_unmatched_archived_or_flagged} />
            <Stat label="Mevcut barkodla çakışan" value={st.conflict_existing_barcode || 0} tone="red" />
          </div>

          <div className="border rounded p-4 bg-white text-sm space-y-1">
            <div>
              Eşleşme yolu: stok kodu + beden <b className="tabular-nums">{fmt(st["by_kod+beden"] || 0)}</b> · stok kodu{" "}
              <b className="tabular-nums">{fmt(st.by_kod || 0)}</b> · ürün adı + beden{" "}
              <b className="tabular-nums">{fmt(st["by_ad+beden"] || 0)}</b>
            </div>
            {report.suggested_markup_pct != null && (
              <div>
                Trendyol fiyatları site fiyatından ortalama <b>%{report.suggested_markup_pct}</b> farklı. Trendyol'a
                giden fiyatın aynı kalması için Trendyol ayarlarındaki varsayılan zammı bu değere getirebilirsiniz.
              </div>
            )}
            {(st.conflict_existing_barcode || 0) > 0 && (
              <div className="inline-flex items-center gap-2 text-red-700">
                <AlertTriangle className="w-4 h-4" /> Bazı varyantlarda farklı bir barkod zaten kayıtlı; üzerine
                yazılmadı (ürün kartında "trendyol_barcode_candidate" olarak duruyor).
              </div>
            )}
            <div className="pt-2">
              <button
                type="button"
                onClick={downloadUnmatched}
                className="inline-flex items-center gap-2 px-3 py-1.5 border rounded text-sm hover:bg-gray-50"
              >
                <Download className="w-4 h-4" /> Eşleşmeyen Trendyol ilanlarını indir (.xlsx)
              </button>
            </div>
          </div>

          {Array.isArray(report.categories) && report.categories.length > 0 && (
            <div className="border rounded bg-white overflow-x-auto">
              <div className="px-4 py-3 border-b font-medium inline-flex items-center gap-2">
                <Tags className="w-4 h-4" /> Kategori eşleştirmesi
              </div>
              <table className="w-full text-sm">
                <thead className="bg-gray-50 text-left">
                  <tr>
                    <th className="p-2">Sistem kategorisi</th>
                    <th className="p-2">Trendyol kategorisi</th>
                    <th className="p-2 text-right">Pay</th>
                    <th className="p-2">Diğer adaylar</th>
                    <th className="p-2">Durum</th>
                  </tr>
                </thead>
                <tbody>
                  {report.categories.map((c) => (
                    <tr key={c.category_id} className="border-t">
                      <td className="p-2">{c.category}</td>
                      <td className="p-2">
                        {c.trendyol_category}
                        {c.trendyol_category_id ? (
                          <span className="ml-2 text-xs text-gray-500 tabular-nums">#{c.trendyol_category_id}</span>
                        ) : null}
                      </td>
                      <td className="p-2 text-right tabular-nums">%{c.share_pct}</td>
                      <td className="p-2 text-gray-600">{(c.others || []).join(", ") || "—"}</td>
                      <td className="p-2">
                        {c.kept_existing ? (
                          <span className="text-gray-600">Elle girilmiş korundu</span>
                        ) : (
                          <span className="text-green-700">Atandı</span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}
    </div>
  );
}
