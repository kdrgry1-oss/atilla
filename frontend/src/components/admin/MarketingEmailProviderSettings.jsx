import { useState, useEffect } from "react";
import axios from "axios";
import { toast } from "sonner";
import { Save, CheckCircle2 } from "lucide-react";

const API = `${process.env.REACT_APP_BACKEND_URL}/api`;
const h = () => ({ Authorization: `Bearer ${localStorage.getItem("token")}` });

/** Pazarlama e-postası gönderici ayarları (Brevo birincil + Amazon SES yedek).
 *  E-posta Pazarlama sayfasından Genel Ayarlar → E-posta'ya taşındı. */
export default function MarketingEmailProviderSettings() {
  const [cfg, setCfg] = useState({
    provider: "brevo", fallback_enabled: true,
    brevo_enabled: false, brevo_api_key: "", brevo_from_email: "", brevo_from_name: "", brevo_reply_to: "", brevo_list_id: "", brevo_webhook_secret: "",
    ses_enabled: false, ses_region: "", ses_access_key: "", ses_secret_key: "", ses_from_email: "", ses_from_name: "", ses_reply_to: "", ses_configuration_set: "",
  });
  const [allowed, setAllowed] = useState(null);
  const [busy, setBusy] = useState("");

  const load = async () => {
    try {
      const r = await axios.get(`${API}/admin/email-marketing/settings`, { headers: h() });
      setCfg((p) => ({ ...p, ...(r.data || {}) }));
      setAllowed(true);
    } catch (e) { setAllowed(e.response?.status === 403 ? false : true); }
  };
  useEffect(() => { load(); }, []);

  const saveCfg = async () => {
    setBusy("save");
    try {
      await axios.put(`${API}/admin/email-marketing/settings`, cfg, { headers: h() });
      toast.success("Pazarlama e-postası sağlayıcı ayarları kaydedildi");
      load();
    } catch (e) { toast.error(e.response?.data?.detail || "Kaydedilemedi"); }
    finally { setBusy(""); }
  };

  const validateProvider = async () => {
    setBusy("validate");
    try {
      const r = await axios.post(`${API}/admin/email-marketing/validate-provider`, {}, { headers: h() });
      toast.success(`${String(r.data?.provider || "Sağlayıcı").toUpperCase()} bağlantısı doğrulandı`);
    } catch (e) { toast.error(e.response?.data?.detail || "Bağlantı doğrulanamadı"); }
    finally { setBusy(""); }
  };

  const field = (label, key, type = "text", ph = "") => (
    <label className="block">
      <span className="text-xs font-medium text-gray-600">{label}</span>
      <input type={type} value={cfg[key] || ""} placeholder={ph}
        onChange={(e) => setCfg((p) => ({ ...p, [key]: e.target.value }))}
        className="mt-1 w-full border rounded-lg px-3 py-2 text-sm" />
    </label>
  );

  if (allowed === null) return null;
  if (allowed === false) return <p className="text-sm text-gray-500">Pazarlama e-postası sağlayıcı ayarları için e-posta ayarları yetkisi gerekir.</p>;

  return (
    <div className="bg-white border rounded-xl p-4 space-y-4" data-testid="marketing-provider-settings">
        <div className="flex items-center justify-between">
          <div><h2 className="font-semibold text-sm">Pazarlama E-postası — Gönderim Sağlayıcısı</h2><p className="text-[11px] text-gray-500">E-posta Pazarlama kampanyaları bu ayarla gönderilir. Test gönderimi E-posta Pazarlama sayfasındadır.</p></div>
          <select value={cfg.provider || "brevo"} onChange={(e) => setCfg((p) => ({ ...p, provider: e.target.value }))}
            className="border rounded-lg px-3 py-2 text-sm font-medium">
            <option value="brevo">Brevo (birincil)</option>
            <option value="ses">Amazon SES (yedek / manuel)</option>
          </select>
        </div>
        <div className="rounded-xl border border-blue-200 bg-blue-50/40 p-4 space-y-3">
          <div className="flex items-center justify-between">
            <div>
              <div className="text-sm font-semibold">Brevo Marketing</div>
              <div className="text-[11px] text-gray-500">Kişiler yerel KVKK/İYS rızasına göre Brevo listesiyle eşitlenir; kampanya Brevo Marketing API üzerinden gönderilir.</div>
            </div>
            <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={!!cfg.brevo_enabled} onChange={(e) => setCfg((p) => ({ ...p, brevo_enabled: e.target.checked }))} className="accent-black" /> Aktif</label>
          </div>
          <div className="grid md:grid-cols-2 gap-3">
            {field("Brevo API anahtarı", "brevo_api_key", "password", "xkeysib-...")}
            {field("Brevo kişi listesi ID", "brevo_list_id", "number", "12")}
            {field("Doğrulanmış gönderen adresi", "brevo_from_email", "email", "bulten@facette.com.tr")}
            {field("Gönderen adı", "brevo_from_name", "text", "Facette")}
            {field("Yanıt adresi", "brevo_reply_to", "email", "info@facette.com.tr")}
            {field("Webhook güvenlik anahtarı", "brevo_webhook_secret", "password", "uzun-rastgele-bir-anahtar")}
          </div>
          <p className="text-[11px] text-gray-500">Brevo webhook adresi: <code>/api/email-marketing/brevo-webhook?key=GÜVENLİK_ANAHTARI</code> · unsubscribe, spam ve hard bounce olaylarını seçin.</p>
        </div>
        <details className="rounded-xl border p-4" open={cfg.provider === "ses"}>
          <summary className="cursor-pointer text-sm font-semibold">Amazon SES yedek ayarları</summary>
          <div className="mt-3 space-y-3">
            <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={!!cfg.ses_enabled} onChange={(e) => setCfg((p) => ({ ...p, ses_enabled: e.target.checked }))} className="accent-black" /> SES aktif</label>
            <div className="grid md:grid-cols-2 gap-3">
              {field("AWS Bölgesi", "ses_region", "text", "eu-west-1")}
              {field("Doğrulanmış gönderen", "ses_from_email", "email", "bulten@facette.com.tr")}
              {field("IAM Access Key ID", "ses_access_key")}
              {field("IAM Secret Access Key", "ses_secret_key", "password", "••••••")}
              {field("Gönderen adı", "ses_from_name", "text", "Facette")}
              {field("Yanıt adresi", "ses_reply_to", "email", "info@facette.com.tr")}
              {field("Configuration Set", "ses_configuration_set", "text", "opsiyonel")}
            </div>
          </div>
        </details>
        <label className="flex items-center gap-2 text-sm cursor-pointer">
          <input type="checkbox" checked={!!cfg.fallback_enabled} onChange={(e) => setCfg((p) => ({ ...p, fallback_enabled: e.target.checked }))} className="accent-black" />
          Birincil sağlayıcı yapılandırılmamışsa diğer hazır sağlayıcıyı yedek olarak kullan
        </label>
        <div className="flex flex-wrap items-center gap-3 pt-2">
          <button onClick={saveCfg} disabled={busy === "save"} className="inline-flex items-center gap-2 bg-black text-white px-4 py-2 rounded-lg text-sm font-semibold hover:bg-gray-800 disabled:opacity-50">
            <Save size={15} /> Kaydet
          </button>
          <button onClick={validateProvider} disabled={busy === "validate"} className="inline-flex items-center gap-2 border px-4 py-2 rounded-lg text-sm font-semibold hover:bg-gray-50 disabled:opacity-50">
            <CheckCircle2 size={15} /> Bağlantıyı doğrula
          </button>
        </div>
        <p className="text-[11px] text-gray-400 leading-relaxed">
          İşlemsel e-postalar (sipariş/şifre) Zoho ZeptoMail'den gitmeye devam eder. Brevo pazarlama için birincildir;
          Amazon SES ayarları silinmez ve gerektiğinde yedek olarak seçilebilir.
        </p>
      </div>
  );
}
