/**
 * =============================================================================
 * MarketplaceHub.jsx — Çok Pazaryerli Yönetim Ekranı
 * =============================================================================
 *
 * AMAÇ:
 *   Ticimax Marketplace v2 panelindeki sol pazaryeri listesi + orta ayar
 *   alanının ayna versiyonu. Admin, desteklenen tüm pazaryerleri arasından
 *   birini seçer, o pazaryerinin:
 *     - API credentials (Supplier ID, Key/Secret, vs.)
 *     - Transfer kuralları (fiyat türü, komisyon, barkod ayarı, sipariş
 *       durum güncellensin mi, iade aktarımı, vb.)
 *     - Otomatik güncelleme periyotları
 *   bilgilerini tek bir ekranda yönetir.
 *
 * BAĞLANTILI BACKEND:
 *   GET  /api/marketplace-hub/marketplaces
 *   GET  /api/marketplace-hub/marketplaces/{key}/schema
 *   GET  /api/marketplace-hub/accounts/{key}
 *   POST /api/marketplace-hub/accounts/{key}
 *
 * NOT: Transfer rules'ın bir kısmı tüm pazaryerlerinde ortaktır; backend
 *      COMMON_TRANSFER_RULES tanımında yönetilir — yeni bir pazaryeri
 *      eklediğinde burada sıfır kod değişikliği olur.
 * =============================================================================
 */
import { useEffect, useMemo, useState } from "react";
import axios from "axios";
import { toast } from "sonner";
import { Link } from "react-router-dom";
import { Save, ExternalLink, Search, Power, Zap, Eye, EyeOff, Cable, SlidersHorizontal } from "lucide-react";
import StockPriceUpdatePanel from "../../components/admin/StockPriceUpdatePanel";

const API = `${process.env.REACT_APP_BACKEND_URL}/api`;

function FieldRenderer({ field, value, onChange }) {
  const [show, setShow] = useState(false);
  if (field.type === "switch") {
    return (
      <label className="flex items-center gap-2 cursor-pointer" data-testid={`mp-field-${field.key}`}>
        <input
          type="checkbox"
          checked={!!value}
          onChange={(e) => onChange(e.target.checked)}
          className="w-4 h-4 accent-stone-800"
        />
        <span className="text-sm text-gray-700">{field.label}</span>
        {field.help && <span className="text-[10px] text-gray-400">({field.help})</span>}
      </label>
    );
  }
  if (field.type === "select") {
    return (
      <div>
        <label className="block text-xs font-semibold text-gray-600 mb-1">
          {field.label} {field.required && <span className="text-[#B0413A]">*</span>}
        </label>
        <select
          value={value ?? field.default ?? ""}
          onChange={(e) => onChange(e.target.value)}
          className="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm bg-white"
          data-testid={`mp-field-${field.key}`}
        >
          <option value="">Seçiniz...</option>
          {(field.options || []).map((o) => (
            <option key={o.value} value={o.value}>{o.label}</option>
          ))}
        </select>
      </div>
    );
  }
  const isPwd = field.type === "password";
  return (
    <div>
      <label className="block text-xs font-semibold text-gray-600 mb-1">
        {field.label} {field.required && <span className="text-[#B0413A]">*</span>}
      </label>
      <div className="relative">
        <input
          type={isPwd && !show ? "password" : field.type === "number" ? "number" : "text"}
          value={value ?? field.default ?? ""}
          onChange={(e) => onChange(field.type === "number" ? (e.target.value === "" ? "" : parseFloat(e.target.value)) : e.target.value)}
          placeholder={field.placeholder || ""}
          className="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm bg-white pr-9"
          data-testid={`mp-field-${field.key}`}
        />
        {isPwd && (
          <button type="button" onClick={() => setShow((s) => !s)}
                  className="absolute right-2 top-1/2 -translate-y-1/2 text-gray-400 hover:text-gray-700" tabIndex={-1}>
            {show ? <EyeOff size={16} /> : <Eye size={16} />}
          </button>
        )}
      </div>
      {field.help && <p className="text-[11px] text-gray-400 mt-1">{field.help}</p>}
    </div>
  );
}

export default function MarketplaceHub() {
  const [marketplaces, setMarketplaces] = useState([]);
  const [active, setActive] = useState(null);
  const [schema, setSchema] = useState(null);
  const [account, setAccount] = useState(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [search, setSearch] = useState("");
  // Trendyol operatif ayarları (db.settings id=trendyol — push & scheduler BUNU okur)
  const [tyOp, setTyOp] = useState(null);
  const [tyOpSaving, setTyOpSaving] = useState(false);
  const [tyBusy, setTyBusy] = useState("");
  // Hepsiburada operatif (db.settings id=hepsiburada — status + client bunu okur)
  const [hbOp, setHbOp] = useState(null);
  const [hbOpSaving, setHbOpSaving] = useState(false);
  const [hbTesting, setHbTesting] = useState(false);

  // İlk yükleme: pazaryeri listesi
  useEffect(() => {
    const token = localStorage.getItem("token");
    axios.get(`${API}/marketplace-hub/marketplaces`, { headers: { Authorization: `Bearer ${token}` } })
      .then((res) => {
        const list = res.data?.marketplaces || [];
        setMarketplaces(list);
        if (list.length > 0) setActive(list[0].key);
      })
      .catch(() => toast.error("Pazaryeri listesi alınamadı"))
      .finally(() => setLoading(false));
  }, []);

  // Pazaryeri değişince schema + account'u çek
  useEffect(() => {
    if (!active) return;
    const token = localStorage.getItem("token");
    const h = { headers: { Authorization: `Bearer ${token}` } };
    setAccount(null); setSchema(null);
    Promise.all([
      axios.get(`${API}/marketplace-hub/marketplaces/${active}/schema`, h),
      axios.get(`${API}/marketplace-hub/accounts/${active}`, h),
    ]).then(([sRes, aRes]) => {
      setSchema(sRes.data);
      setAccount(aRes.data);
    }).catch(() => toast.error("Ayarlar yüklenemedi"));
  }, [active]);

  // Trendyol seçiliyse operatif ayarları ayrı store'dan (db.settings) çek
  useEffect(() => {
    if (active !== "trendyol") { setTyOp(null); return; }
    const token = localStorage.getItem("token");
    axios.get(`${API}/integrations/trendyol/settings?t=${Date.now()}`,
      { headers: { Authorization: `Bearer ${token}` } })
      .then((r) => setTyOp(r.data || {}))
      .catch(() => setTyOp({}));
  }, [active]);

  // Hepsiburada seçiliyse operatif ayarları db.settings'ten çek
  useEffect(() => {
    if (active !== "hepsiburada") { setHbOp(null); return; }
    const token = localStorage.getItem("token");
    axios.get(`${API}/integrations/hepsiburada/settings?t=${Date.now()}`,
      { headers: { Authorization: `Bearer ${token}` } })
      .then((r) => setHbOp(r.data || {}))
      .catch(() => setHbOp({}));
  }, [active]);

  const filtered = useMemo(() => {
    if (!search) return marketplaces;
    const s = search.toLocaleLowerCase("tr");
    return marketplaces.filter((m) => m.name.toLocaleLowerCase("tr").includes(s));
  }, [marketplaces, search]);

  const updateCred = (k, v) => setAccount((p) => ({ ...p, credentials: { ...(p?.credentials || {}), [k]: v } }));
  const updateRule = (k, v) => setAccount((p) => ({ ...p, transfer_rules: { ...(p?.transfer_rules || {}), [k]: v } }));
  const updateSync = (k, v) => setAccount((p) => ({ ...p, auto_sync: { ...(p?.auto_sync || {}), [k]: v } }));

  const save = async () => {
    if (!account) return;
    setSaving(true);
    try {
      const token = localStorage.getItem("token");
      const res = await axios.post(`${API}/marketplace-hub/accounts/${active}`, account,
        { headers: { Authorization: `Bearer ${token}` } });
      toast.success(res.data?.message || "Kaydedildi");
    } catch (err) {
      toast.error("Kayıt başarısız: " + (err.response?.data?.detail || err.message));
    } finally {
      setSaving(false);
    }
  };

  // ----- Trendyol operatif: alan güncelle / kaydet / işlem tetikle -----
  const tyUpd = (k, v) => setTyOp((p) => ({ ...(p || {}), [k]: v }));

  const saveTyOp = async () => {
    setTyOpSaving(true);
    try {
      const token = localStorage.getItem("token");
      await axios.post(`${API}/integrations/trendyol/settings`, tyOp,
        { headers: { Authorization: `Bearer ${token}` } });
      toast.success("Trendyol ayarları kaydedildi (push & scheduler bunu kullanır)");
    } catch (err) {
      toast.error("Kayıt başarısız: " + (err.response?.data?.detail || err.message));
    } finally { setTyOpSaving(false); }
  };

  const runTyOp = async (label, url, busyKey) => {
    setTyBusy(busyKey);
    try {
      const token = localStorage.getItem("token");
      const r = await axios.post(`${API}${url}`, null,
        { headers: { Authorization: `Bearer ${token}` } });
      toast.success(r.data?.message || `${label} tamam`);
    } catch (err) {
      toast.error(`${label} hatası: ` + (err.response?.data?.detail || err.message));
    } finally { setTyBusy(""); }
  };

  // ----- n11 ürün aktarma (önizle → eksik zorunlu özellikleri seç → aktar) -----
  const [n11P, setN11P] = useState({ code: "", category_id: "", category_label: "", shipment_template: "",
    preparing_day: 2, attributes: {} });
  const [n11CatQ, setN11CatQ] = useState("");
  const [n11Cats, setN11Cats] = useState([]);
  const [n11Res, setN11Res] = useState(null);
  const [n11Busy, setN11Busy] = useState(false);
  const searchN11Cats = async (q) => {
    setN11CatQ(q);
    if (q.trim().length < 2) { setN11Cats([]); return; }
    try {
      const r = await axios.get(`${API}/integrations/n11/categories?q=${encodeURIComponent(q)}&limit=30`,
        { headers: { Authorization: `Bearer ${localStorage.getItem("token")}` } });
      setN11Cats(r.data?.categories || []);
    } catch { setN11Cats([]); }
  };
  const pushN11 = async (dryRun) => {
    setN11Busy(true);
    try {
      const r = await axios.post(`${API}/integrations/n11/products/push`, { ...n11P, dry_run: dryRun },
        { headers: { Authorization: `Bearer ${localStorage.getItem("token")}` } });
      setN11Res(r.data);
      if (!dryRun) (r.data?.success ? toast.success : toast.error)(r.data?.message || "Tamam");
    } catch (err) {
      toast.error(err.response?.data?.detail || err.message);
    } finally { setN11Busy(false); }
  };

  // ----- n11 işlemleri (kimlik bu ekrandaki API Bilgileri'nden okunur) -----
  const runN11 = async (label, url, busyKey) => {
    setTyBusy(busyKey);
    try {
      const token = localStorage.getItem("token");
      const r = await axios.post(`${API}${url}`, null,
        { headers: { Authorization: `Bearer ${token}` } });
      if (r.data?.success === false) toast.error(r.data?.message || `${label} başarısız`);
      else toast.success(r.data?.message || `${label} tamam`);
    } catch (err) {
      toast.error(`${label} hatası: ` + (err.response?.data?.detail || err.message));
    } finally { setTyBusy(""); }
  };

  // ----- Hepsiburada operatif: güncelle / kaydet / test -----
  const hbUpd = (k, v) => setHbOp((p) => ({ ...(p || {}), [k]: v }));

  const saveHbOp = async () => {
    setHbOpSaving(true);
    try {
      const token = localStorage.getItem("token");
      await axios.post(`${API}/integrations/hepsiburada/settings`, hbOp,
        { headers: { Authorization: `Bearer ${token}` } });
      toast.success("Hepsiburada ayarları kaydedildi");
    } catch (err) {
      toast.error("Kayıt başarısız: " + (err.response?.data?.detail || err.message));
    } finally { setHbOpSaving(false); }
  };

  const testHb = async () => {
    setHbTesting(true);
    try {
      const token = localStorage.getItem("token");
      const r = await axios.post(`${API}/integrations/hepsiburada/test-connection`, {},
        { headers: { Authorization: `Bearer ${token}` } });
      if (r.data?.success) toast.success(r.data?.message || "Bağlantı başarılı");
      else toast.error(r.data?.message || "Bağlantı başarısız");
    } catch (err) {
      toast.error("Test başarısız: " + (err.response?.data?.detail || err.message));
    } finally { setHbTesting(false); }
  };

  if (loading) return <div className="py-10 text-center text-sm text-gray-500">Yükleniyor…</div>;

  return (
    <div data-testid="marketplace-hub-page">
      {/* Başlık */}
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-2xl font-bold">Pazaryerleri Yönetimi</h1>
          <p className="text-sm text-gray-500 mt-1">
            Tüm e-ticaret pazaryerlerinin API bilgileri, aktarım kuralları ve otomatik senkron ayarları tek merkezden.
          </p>
        </div>
        <button
          onClick={save}
          disabled={saving || !account}
          className="flex items-center gap-1 bg-black text-white px-4 py-2 rounded-lg text-sm hover:bg-gray-800 disabled:opacity-50"
          data-testid="marketplace-save-btn"
        >
          <Save size={14} />
          {saving ? "Kaydediliyor..." : "Kaydet"}
        </button>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-12 gap-6">
        {/* SOL: PAZARYERİ LİSTESİ */}
        <div className="lg:col-span-3 bg-white border rounded-xl overflow-hidden">
          <div className="p-3 border-b bg-gray-50">
            <div className="relative">
              <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400" />
              <input
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder="Ara..."
                className="w-full border border-gray-200 rounded-lg pl-8 pr-3 py-1.5 text-sm outline-none"
                data-testid="marketplace-search-input"
              />
            </div>
          </div>
          <div className="max-h-[720px] overflow-y-auto">
            {filtered.map((m) => {
              const isActive = m.key === active;
              return (
                <button
                  key={m.key}
                  onClick={() => setActive(m.key)}
                  className={`w-full flex items-center gap-3 px-4 py-3 text-left border-b transition-colors ${
                    isActive ? "bg-stone-100 border-l-4 border-l-stone-900" : "hover:bg-stone-50"
                  }`}
                  data-testid={`marketplace-row-${m.key}`}
                >
                  <div
                    className="w-8 h-8 rounded-full flex items-center justify-center text-white text-[10px] font-black shrink-0"
                    style={{ backgroundColor: m.color || "#333" }}
                  >
                    {m.name.slice(0, 2).toUpperCase()}
                  </div>
                  <div className="flex-1 min-w-0">
                    <div className="font-semibold text-sm text-gray-900 truncate">{m.name}</div>
                    <div className="text-[11px] text-gray-500 truncate">{(m.features || []).length} özellik</div>
                  </div>
                </button>
              );
            })}
          </div>
        </div>

        {/* SAĞ: AKTIF PAZARYERİ AYARLARI */}
        <div className="lg:col-span-9">
          {!schema || !account ? (
            <div className="bg-white border rounded-xl p-10 text-center text-sm text-gray-400">
              Soldan bir pazaryeri seçin.
            </div>
          ) : (
            <>
              {/* Header kart */}
              <div className="bg-white border rounded-xl p-5 mb-4">
                <div className="flex items-start justify-between">
                  <div>
                    <div className="flex items-center gap-3">
                      <div
                        className="w-10 h-10 rounded-full flex items-center justify-center text-white text-xs font-black"
                        style={{ backgroundColor: schema.color || "#333" }}
                      >
                        {schema.name.slice(0, 2).toUpperCase()}
                      </div>
                      <div>
                        <h2 className="text-xl font-bold">{schema.name}</h2>
                        <p className="text-xs text-gray-500">{schema.description}</p>
                      </div>
                    </div>
                    <div className="flex flex-wrap gap-1 mt-3">
                      {(schema.features || []).map((f) => (
                        <span key={f} className="text-[10px] bg-gray-100 text-gray-700 px-2 py-0.5 rounded-full font-medium">
                          {f}
                        </span>
                      ))}
                    </div>
                  </div>
                  <div className="flex flex-col items-end gap-2">
                    {schema.website && (
                      <a href={schema.website} target="_blank" rel="noopener noreferrer"
                         className="text-xs text-gray-500 hover:text-black flex items-center gap-1">
                        {schema.website.replace(/^https?:\/\//, "").split("/")[0]} <ExternalLink size={12} />
                      </a>
                    )}
                    <label className="flex items-center gap-2 cursor-pointer" data-testid="mp-enabled-switch">
                      <input
                        type="checkbox"
                        checked={!!account.enabled}
                        onChange={(e) => setAccount((p) => ({ ...p, enabled: e.target.checked }))}
                        className="w-4 h-4 accent-green-600"
                      />
                      <span className={`text-sm font-semibold ${account.enabled ? "text-[#3F7A52]" : "text-gray-400"}`}>
                        <Power size={12} className="inline mr-1" />
                        {account.enabled ? "Aktif" : "Pasif"}
                      </span>
                    </label>
                  </div>
                </div>
                {/* Quick Links: Detaylı sayfalar */}
                <div className="mt-4 pt-4 border-t flex flex-wrap gap-2">
                  {!["trendyol", "hepsiburada"].includes(active) && (
                  <Link
                    to={active.startsWith("amazon") ? "/admin/amazon?tab=mapping" : "/admin/entegrasyonlar"}
                    data-testid="quick-link-integrations"
                    className="inline-flex items-center gap-1.5 text-xs bg-gray-100 hover:bg-gray-200 text-gray-800 px-3 py-1.5 rounded-lg font-medium"
                  >
                    <Cable size={13} /> Aktarım İşlemleri (Ürün Gönder / Sipariş Al)
                  </Link>
                  )}
                  <Link
                    to="/admin/urunler?aktar=barkod"
                    data-testid="quick-link-barcode-push"
                    className="inline-flex items-center gap-1.5 text-xs bg-gray-100 hover:bg-gray-200 text-gray-800 px-3 py-1.5 rounded-lg font-medium"
                  >
                    <Zap size={13} /> Barkod / Stok Kodu ile Aktar
                  </Link>
                  <Link
                    to="/admin/marka-eslestir"
                    data-testid="quick-link-brand-map"
                    className="inline-flex items-center gap-1.5 text-xs bg-gray-100 hover:bg-gray-200 text-gray-800 px-3 py-1.5 rounded-lg font-medium"
                  >
                    <SlidersHorizontal size={13} /> Marka Eşleştirme
                  </Link>
                  <Link
                    to={active.startsWith("amazon") ? "/admin/amazon?tab=mapping" : "/admin/kategori-eslestir"}
                    data-testid="quick-link-category-map"
                    className="inline-flex items-center gap-1.5 text-xs bg-gray-100 hover:bg-gray-200 text-gray-800 px-3 py-1.5 rounded-lg font-medium"
                  >
                    <SlidersHorizontal size={13} /> Kategori Eşleştirme
                  </Link>
                  <Link
                    to={`/admin/entegrasyon-loglari?marketplace=${active}`}
                    data-testid="quick-link-logs"
                    className="inline-flex items-center gap-1.5 text-xs bg-gray-100 hover:bg-gray-200 text-gray-800 px-3 py-1.5 rounded-lg font-medium"
                  >
                    <ExternalLink size={13} /> Bu Pazaryerinin Logları
                  </Link>
                </div>
              </div>

              {/* STOK / FİYAT GÜNCELLE — hub içinde doğrudan (Faz T2: barkod/stok kodu ile hedefli) */}
              <StockPriceUpdatePanel
                marketplace={active}
                auth={{ headers: { Authorization: `Bearer ${localStorage.getItem("token")}` } }}
              />

              {/* ===== TRENDYOL OPERATİF AYARLAR (db.settings — push & scheduler bunu okur) ===== */}
              {active === "trendyol" && (
                <div className="bg-white border rounded-xl p-5 mb-4">
                  <div className="flex items-center justify-between mb-1">
                    <h3 className="text-sm font-black text-gray-900 uppercase tracking-wider">
                      Trendyol API & Varsayılanları
                    </h3>
                    <button
                      onClick={saveTyOp}
                      disabled={tyOpSaving || !tyOp}
                      className="flex items-center gap-1 bg-stone-900 text-white px-3 py-1.5 rounded-lg text-xs hover:bg-stone-800 disabled:opacity-50"
                      data-testid="ty-op-save"
                    >
                      <Save size={13} /> {tyOpSaving ? "Kaydediliyor..." : "Kaydet"}
                    </button>
                  </div>
                  <p className="text-[11px] text-stone-500 mb-4">
                    Ürün gönderimi ve otomatik senkron <strong>bu</strong> bilgileri kullanır. (Operatif kaynak)
                  </p>
                  {!tyOp ? (
                    <div className="text-sm text-gray-400 py-4">Yükleniyor…</div>
                  ) : (
                    <>
                      <div className="grid md:grid-cols-2 gap-4">
                        <div>
                          <label className="block text-xs font-semibold text-gray-600 mb-1">
                            Tedarikçi No (Supplier ID) <span className="text-[#B0413A]">*</span>
                          </label>
                          <input value={tyOp.supplier_id ?? ""} onChange={(e) => tyUpd("supplier_id", e.target.value)}
                                 placeholder="157840"
                                 className="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm" data-testid="ty-op-supplier" />
                        </div>
                        <div>
                          <label className="block text-xs font-semibold text-gray-600 mb-1">Ortam</label>
                          <select value={tyOp.mode ?? "prod"} onChange={(e) => tyUpd("mode", e.target.value)}
                                  className="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm bg-white" data-testid="ty-op-mode">
                            <option value="prod">Canlı</option>
                            <option value="test">Test</option>
                          </select>
                        </div>
                        <div>
                          <label className="block text-xs font-semibold text-gray-600 mb-1">
                            API Key <span className="text-[#B0413A]">*</span>
                          </label>
                          <input value={tyOp.api_key ?? ""} onChange={(e) => tyUpd("api_key", e.target.value)}
                                 className="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm" data-testid="ty-op-key" />
                        </div>
                        <div>
                          <label className="block text-xs font-semibold text-gray-600 mb-1">
                            API Secret <span className="text-[#B0413A]">*</span>
                          </label>
                          <input type="password" value={tyOp.api_secret ?? ""} onChange={(e) => tyUpd("api_secret", e.target.value)}
                                 className="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm" data-testid="ty-op-secret" />
                        </div>
                      </div>
                      <div className="mt-4 pt-4 border-t">
                        <p className="text-xs font-semibold text-stone-600 mb-3">Gönderim Varsayılanları</p>
                        <div className="grid md:grid-cols-3 gap-4">
                          <div>
                            <label className="block text-xs font-semibold text-gray-600 mb-1">Varsayılan Marka ID</label>
                            <input value={tyOp.default_brand_id ?? ""} onChange={(e) => tyUpd("default_brand_id", e.target.value)}
                                   className="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm" />
                          </div>
                          <div>
                            <label className="block text-xs font-semibold text-gray-600 mb-1">Varsayılan Kargo Firma ID</label>
                            <input value={tyOp.default_cargo_company_id ?? ""} onChange={(e) => tyUpd("default_cargo_company_id", e.target.value)}
                                   className="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm" />
                          </div>
                          <div>
                            <label className="block text-xs font-semibold text-gray-600 mb-1">Varsayılan KDV (%)</label>
                            <input type="number" value={tyOp.default_vat_rate ?? ""}
                                   onChange={(e) => tyUpd("default_vat_rate", e.target.value === "" ? "" : parseFloat(e.target.value))}
                                   className="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm" />
                          </div>
                        </div>
                      </div>
                    </>
                  )}
                </div>
              )}

              {/* ===== TRENDYOL İŞLEMLERİ ===== */}
              {active === "trendyol" && (
                <div className="bg-white border rounded-xl p-5 mb-4">
                  <h3 className="text-sm font-black text-gray-900 uppercase tracking-wider mb-1">İşlemler</h3>
                  <p className="text-[11px] text-stone-500 mb-4">Manuel tetikleme. (Fiyat/stok için üstteki panel)</p>
                  <div className="flex flex-wrap gap-2">
                    {[
                      { label: "Ürün Gönder", url: "/integrations/trendyol/products/sync", key: "push" },
                      { label: "Sipariş Al", url: "/integrations/trendyol/orders/import", key: "orders" },
                      { label: "Kategori İndir", url: "/integrations/trendyol/categories/sync", key: "cats" },
                      { label: "Marka İndir", url: "/integrations/trendyol/brands/sync", key: "brands" },
                    ].map((op) => (
                      <button
                        key={op.key}
                        onClick={() => runTyOp(op.label, op.url, op.key)}
                        disabled={!!tyBusy}
                        className="inline-flex items-center gap-1.5 text-xs border border-stone-300 hover:bg-stone-100 text-stone-800 px-3 py-2 rounded-lg font-medium disabled:opacity-50"
                        data-testid={`ty-op-${op.key}`}
                      >
                        <Cable size={13} /> {tyBusy === op.key ? "Çalışıyor..." : op.label}
                      </button>
                    ))}
                  </div>
                </div>
              )}

              {/* ===== HEPSİBURADA OPERATİF AYARLAR (db.settings — status & client bunu okur) ===== */}
              {active === "hepsiburada" && (
                <div className="bg-white border rounded-xl p-5 mb-4">
                  <div className="flex items-center justify-between mb-1">
                    <h3 className="text-sm font-black text-gray-900 uppercase tracking-wider">
                      Hepsiburada API Ayarları
                    </h3>
                    <div className="flex gap-2">
                      <button onClick={testHb} disabled={hbTesting || !hbOp}
                        className="flex items-center gap-1 border border-stone-300 hover:bg-stone-100 text-stone-800 px-3 py-1.5 rounded-lg text-xs disabled:opacity-50"
                        data-testid="hb-op-test">
                        <Cable size={13} /> {hbTesting ? "Test ediliyor..." : "Bağlantı Test Et"}
                      </button>
                      <button onClick={saveHbOp} disabled={hbOpSaving || !hbOp}
                        className="flex items-center gap-1 bg-stone-900 text-white px-3 py-1.5 rounded-lg text-xs hover:bg-stone-800 disabled:opacity-50"
                        data-testid="hb-op-save">
                        <Save size={13} /> {hbOpSaving ? "Kaydediliyor..." : "Kaydet"}
                      </button>
                    </div>
                  </div>
                  <p className="text-[11px] text-stone-500 mb-4">
                    Sipariş/ürün işlemleri ve durum rozeti <strong>bu</strong> bilgileri kullanır. (Operatif kaynak)
                  </p>
                  {!hbOp ? (
                    <div className="text-sm text-gray-400 py-4">Yükleniyor…</div>
                  ) : (
                    <>
                      <div className="grid md:grid-cols-2 gap-4">
                        <div>
                          <label className="block text-xs font-semibold text-gray-600 mb-1">
                            Merchant ID <span className="text-[#B0413A]">*</span>
                          </label>
                          <input value={hbOp.merchant_id ?? ""} onChange={(e) => hbUpd("merchant_id", e.target.value)}
                                 placeholder="e88383f6-478d-..."
                                 className="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm" data-testid="hb-op-merchant" />
                        </div>
                        <div>
                          <label className="block text-xs font-semibold text-gray-600 mb-1">Ortam</label>
                          <select value={hbOp.mode ?? "production"} onChange={(e) => hbUpd("mode", e.target.value)}
                                  className="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm bg-white" data-testid="hb-op-mode">
                            <option value="production">Production (Canlı)</option>
                            <option value="sandbox">Sandbox (SIT Test)</option>
                          </select>
                          {(hbOp.mode ?? "production") === "sandbox" && (
                            <p className="mt-1 text-[11px] text-red-600 font-semibold">
                              ⚠️ Sandbox seçili — ürün/stok/fiyat gönderimleri TEST ortamına gider, gerçek mağazada görünmez.
                            </p>
                          )}
                        </div>
                        <div>
                          <label className="block text-xs font-semibold text-gray-600 mb-1">
                            Secret Key <span className="text-[#B0413A]">*</span>
                          </label>
                          <input type="password" value={hbOp.secret_key ?? ""} onChange={(e) => hbUpd("secret_key", e.target.value)}
                                 placeholder={hbOp.secret_key === "********" ? "********" : "Hepsiburada API parolası"}
                                 className="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm" data-testid="hb-op-secret" />
                        </div>
                        <div>
                          <label className="block text-xs font-semibold text-gray-600 mb-1">
                            Developer Username <span className="text-[#B0413A]">*</span>
                          </label>
                          <input value={hbOp.dev_username ?? ""} onChange={(e) => hbUpd("dev_username", e.target.value)}
                                 placeholder="ceyjewelry_dev"
                                 className="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm" data-testid="hb-op-dev" />
                        </div>
                      </div>
                      <div className="mt-4 pt-4 border-t flex items-center gap-6">
                        <label className="flex items-center gap-2 cursor-pointer" data-testid="hb-op-active">
                          <input type="checkbox" checked={!!hbOp.is_active}
                                 onChange={(e) => hbUpd("is_active", e.target.checked)}
                                 className="w-4 h-4 accent-green-600" />
                          <span className={`text-sm font-semibold ${hbOp.is_active ? "text-[#3F7A52]" : "text-gray-400"}`}>
                            Entegrasyon Aktif
                          </span>
                        </label>
                      </div>
                    </>
                  )}
                </div>
              )}

              {/* API CREDENTIALS — operatif store'lu pazaryerlerinde gizli (operatif ayarlar yukarıda) */}
              {!["trendyol", "hepsiburada"].includes(active) && (
              <div className="bg-white border rounded-xl p-5 mb-4">
                <h3 className="text-sm font-black text-gray-900 uppercase tracking-wider mb-4">
                  {schema.name} API Bilgileriniz
                </h3>
                <div className="grid md:grid-cols-2 gap-4">
                  {(schema.credential_fields || []).map((f) => (
                    <FieldRenderer
                      key={f.key}
                      field={f}
                      value={account.credentials?.[f.key]}
                      onChange={(v) => updateCred(f.key, v)}
                    />
                  ))}
                </div>
              </div>
              )}

              {active === "n11" && (
              <div className="bg-white border rounded-xl p-5 mb-4" data-testid="n11-actions">
                <h3 className="text-sm font-black text-gray-900 uppercase tracking-wider mb-1">N11 İşlemleri</h3>
                <p className="text-xs text-gray-500 mb-4">
                  Önce API bilgilerini girip kaydedin ve entegrasyonu aktif edin. Siparişler panele Hepsiburada ile aynı
                  kurallarla düşer (stok bir kez düşülür, n11'de iptal olursa stok geri eklenir). Stok gönderimi yalnız
                  n11'de zaten listeli ve Facette ile eşleşen ürünlerin adedini günceller; fiyata dokunmaz.
                </p>
                <div className="flex flex-wrap items-center gap-2">
                  {[
                    ["Bağlantı testi", "/integrations/n11/test-connection", "n11-test", "Bağlantıyı Test Et"],
                    ["Sipariş çekme", "/integrations/n11/orders/import?days=15", "n11-orders", "Siparişleri Çek (son 15 gün)"],
                    ["Stok gönderimi", "/integrations/n11/stock/sync", "n11-stock", "Stokları n11'e Gönder"],
                    ["Kategori güncelleme", "/integrations/n11/categories/sync", "n11-cats", "Kategori Ağacını Güncelle"],
                  ].map(([label, url, key, text]) => (
                    <button key={key} type="button" onClick={() => runN11(label, url, key)} disabled={!!tyBusy}
                      data-testid={`${key}-btn`}
                      className="inline-flex items-center gap-1.5 h-9 px-3 rounded-lg border border-gray-300 bg-white text-xs font-semibold text-gray-800 hover:bg-gray-50 disabled:opacity-50">
                      <Zap size={13} /> {tyBusy === key ? "Çalışıyor…" : text}
                    </button>
                  ))}
                </div>

                <div className="mt-5 pt-4 border-t" data-testid="n11-push">
                  <h4 className="text-xs font-black text-gray-900 uppercase tracking-wider mb-1">Ürün Aktar</h4>
                  <p className="text-xs text-gray-500 mb-3">
                    Barkod veya stok koduyla ürünü seçin, n11 kategorisini arayıp seçin. Önce "Önizle" ile fiyatı ve
                    eksik zorunlu özellikleri görün; tamamlayınca "n11'e Aktar". (Kategori listesi boşsa önce
                    "Kategori Ağacını Güncelle".)
                  </p>
                  <div className="grid md:grid-cols-2 gap-3">
                    <label className="block">
                      <span className="text-xs text-gray-600">Barkod / Stok Kodu</span>
                      <input value={n11P.code} onChange={(e) => setN11P({ ...n11P, code: e.target.value })}
                        className="mt-1 w-full h-9 border border-gray-300 rounded-lg px-3 text-sm" data-testid="n11-push-code" />
                    </label>
                    <label className="block relative">
                      <span className="text-xs text-gray-600">n11 Kategorisi {n11P.category_id ? `· ${n11P.category_id}` : ""}</span>
                      <input value={n11P.category_label || n11CatQ}
                        onChange={(e) => { setN11P({ ...n11P, category_id: "", category_label: "" }); searchN11Cats(e.target.value); }}
                        placeholder="Ara: gömlek, elbise…"
                        className="mt-1 w-full h-9 border border-gray-300 rounded-lg px-3 text-sm" data-testid="n11-push-cat" />
                      {!n11P.category_id && n11Cats.length > 0 && (
                        <div className="absolute z-20 left-0 right-0 mt-1 max-h-60 overflow-auto bg-white border border-gray-200 rounded-lg shadow">
                          {n11Cats.map((c) => (
                            <button key={c.id} type="button"
                              onClick={() => { setN11P({ ...n11P, category_id: c.id, category_label: c.path, attributes: {} }); setN11Cats([]); }}
                              className="block w-full text-left px-3 py-1.5 text-xs text-gray-800 hover:bg-gray-50">{c.path}</button>
                          ))}
                        </div>
                      )}
                    </label>
                    <label className="block">
                      <span className="text-xs text-gray-600">Teslimat Şablonu Adı (n11 Mağazam)</span>
                      <input value={n11P.shipment_template} onChange={(e) => setN11P({ ...n11P, shipment_template: e.target.value })}
                        className="mt-1 w-full h-9 border border-gray-300 rounded-lg px-3 text-sm" />
                    </label>
                    <label className="block">
                      <span className="text-xs text-gray-600">Hazırlanma Süresi (gün)</span>
                      <input type="number" min={1} max={30} value={n11P.preparing_day}
                        onChange={(e) => setN11P({ ...n11P, preparing_day: e.target.value })}
                        className="mt-1 w-full h-9 border border-gray-300 rounded-lg px-3 text-sm" />
                    </label>
                  </div>

                  {n11Res && (n11Res.missing || []).length > 0 && (
                    <div className="grid md:grid-cols-2 gap-3 mt-3">
                      {n11Res.missing.map((m) => (
                        <label key={m.id} className="block">
                          <span className="text-xs text-red-700">Zorunlu: {m.name}</span>
                          {(m.values || []).length > 0 ? (
                            <select value={n11P.attributes[m.id] || ""}
                              onChange={(e) => setN11P({ ...n11P, attributes: { ...n11P.attributes, [m.id]: e.target.value } })}
                              className="mt-1 w-full h-9 border border-red-300 rounded-lg px-2 text-sm bg-white">
                              <option value="">Seçin…</option>
                              {m.values.map((v) => <option key={v.id} value={v.id}>{v.value}</option>)}
                            </select>
                          ) : (
                            <input value={n11P.attributes[m.id] || ""}
                              onChange={(e) => setN11P({ ...n11P, attributes: { ...n11P.attributes, [m.id]: e.target.value } })}
                              className="mt-1 w-full h-9 border border-red-300 rounded-lg px-3 text-sm" />
                          )}
                        </label>
                      ))}
                    </div>
                  )}

                  {n11Res && (
                    <div className={`mt-3 text-xs rounded-lg border px-3 py-2 ${n11Res.success || n11Res.ready ? "bg-green-50 border-green-200 text-green-800" : "bg-amber-50 border-amber-200 text-amber-800"}`}
                      data-testid="n11-push-result">
                      <div className="font-semibold">{n11Res.message}</div>
                      {n11Res.product && (
                        <div className="mt-1 text-gray-700">
                          {n11Res.product.name} · {n11Res.sku_count} varyant · Liste {n11Res.list_price} TL · Satış {n11Res.sale_price} TL
                        </div>
                      )}
                      {(n11Res.results || []).filter((x) => String(x.status).toUpperCase() !== "SUCCESS").map((x) => (
                        <div key={x.stockCode} className="mt-1 text-red-700">{x.stockCode}: {(x.reasons || []).join("; ")}</div>
                      ))}
                    </div>
                  )}

                  <div className="flex items-center gap-2 mt-3">
                    <button type="button" onClick={() => pushN11(true)} disabled={n11Busy || !n11P.code}
                      className="inline-flex items-center gap-1.5 h-9 px-3 rounded-lg border border-gray-300 bg-white text-xs font-semibold text-gray-800 hover:bg-gray-50 disabled:opacity-50">
                      <Eye size={13} /> Önizle
                    </button>
                    <button type="button" onClick={() => pushN11(false)} disabled={n11Busy || !n11P.code || !n11P.category_id}
                      data-testid="n11-push-btn"
                      className="inline-flex items-center gap-1.5 h-9 px-3 rounded-lg bg-black text-white text-xs font-semibold hover:bg-gray-800 disabled:opacity-50">
                      <Zap size={13} /> {n11Busy ? "Çalışıyor…" : "n11'e Aktar"}
                    </button>
                  </div>
                </div>
              </div>
              )}

              {/* TRANSFER RULES — yalnız tanımlı alan varsa */}
              {(schema.transfer_rule_fields || []).length > 0 && (
              <div className="bg-white border rounded-xl p-5 mb-4">
                <h3 className="text-sm font-black text-gray-900 uppercase tracking-wider mb-4">
                  Aktarım Kuralları
                </h3>
                <div className="grid md:grid-cols-2 gap-4">
                  {(schema.transfer_rule_fields || []).map((f) => (
                    <FieldRenderer
                      key={f.key}
                      field={f}
                      value={account.transfer_rules?.[f.key]}
                      onChange={(v) => updateRule(f.key, v)}
                    />
                  ))}
                </div>
              </div>
              )}

              {/* AUTO-SYNC */}
              <div className="bg-white border rounded-xl p-5">
                <h3 className="text-sm font-black text-gray-900 uppercase tracking-wider mb-4 flex items-center gap-2">
                  <Zap size={14} /> Otomatik Güncelleme
                </h3>
                <div className="grid md:grid-cols-2 gap-6">
                  <div className="border rounded-lg p-4">
                    <label className="flex items-center gap-2 cursor-pointer mb-3" data-testid="auto-products-switch">
                      <input
                        type="checkbox"
                        checked={!!account.auto_sync?.products_enabled}
                        onChange={(e) => updateSync("products_enabled", e.target.checked)}
                        className="w-4 h-4 accent-stone-800"
                      />
                      <span className="font-semibold text-sm">Ürünler Otomatik Güncellensin</span>
                    </label>
                    <label className="block text-xs text-gray-600 mb-1">Periyot (dakika)</label>
                    <input
                      type="number"
                      value={account.auto_sync?.products_interval_min ?? 3}
                      onChange={(e) => updateSync("products_interval_min", parseInt(e.target.value) || 3)}
                      className="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm"
                      data-testid="auto-products-interval"
                    />
                    <p className="text-[11px] text-gray-400 mt-2">
                      Pazaryerine fiyat/stok güncellemesi bu sıklıkla gönderilir.
                    </p>
                  </div>
                  <div className="border rounded-lg p-4">
                    <label className="flex items-center gap-2 cursor-pointer mb-3" data-testid="auto-orders-switch">
                      <input
                        type="checkbox"
                        checked={!!account.auto_sync?.orders_enabled}
                        onChange={(e) => updateSync("orders_enabled", e.target.checked)}
                        className="w-4 h-4 accent-stone-800"
                      />
                      <span className="font-semibold text-sm">Siparişler Otomatik Çekilsin</span>
                    </label>
                    <div className="grid grid-cols-2 gap-2">
                      <div>
                        <label className="block text-xs text-gray-600 mb-1">Periyot (dk)</label>
                        <input
                          type="number"
                          value={account.auto_sync?.orders_interval_min ?? 5}
                          onChange={(e) => updateSync("orders_interval_min", parseInt(e.target.value) || 5)}
                          className="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm"
                          data-testid="auto-orders-interval"
                        />
                      </div>
                      <div>
                        <label className="block text-xs text-gray-600 mb-1">Geriye Dönük (saat)</label>
                        <input
                          type="number"
                          value={account.auto_sync?.orders_lookback_hours ?? 100}
                          onChange={(e) => updateSync("orders_lookback_hours", parseInt(e.target.value) || 100)}
                          className="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm"
                        />
                      </div>
                    </div>
                    <p className="text-[11px] text-gray-400 mt-2">
                      Pazaryerinden siparişler bu sıklıkla alınır, son N saatlik.
                    </p>
                  </div>
                </div>
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
