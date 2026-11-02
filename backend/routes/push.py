"""
push.py — MODERN FCM v1 push gönderici (yeni-sipariş vb. OTOMATİK bildirimler).
============================================================================
NOT: Cihaz KAYDI zaten routes/mobile.py `/app/devices/register` ile db.user_devices'a
yapılıyor; BURADA TEKRAR ETMEYİZ. admin_mobile.py'deki mevcut gönderici LEGACY FCM
(`fcm.googleapis.com/fcm/send` + `key=...`) kullanıyor — o API Google tarafından
Haziran 2024'te KAPATILDI. Bu modül modern **FCM HTTP v1** ile gönderir.

Yapılandırma (birini seç):
  • env  FCM_PROJECT_ID + FCM_SERVICE_ACCOUNT (service account JSON, tek satır)
  • ya da db.settings id="push": { fcm_project_id, fcm_service_account }
Yapılandırma YOKSA gönderim SESSİZCE atlanır → sipariş akışı ASLA bozulmaz.
"""
import os
import re
import time
from fastapi import APIRouter, Depends
from datetime import datetime, timezone

from .deps import db, logger, require_admin, require_permission

router = APIRouter(prefix="/push", tags=["push"])


# ── ADM-15: PUSH İÇERİĞİNDE KİŞİSEL VERİ YOK ─────────────────────────────────────────────
# Push başlığı/gövdesi kilit ekranında görünür ve Google (FCM) / Apple (APNs) üzerinden geçer.
# Çağıran hangi metni verirse versin, gönderimden ÖNCE tek noktada süzülür:
#   • tür bazlı ŞABLON: sipariş/WhatsApp gibi müşteriye ait bildirimler sabit, PII'siz metinle
#     yeniden kurulur (müşteri adı / tutar / mesaj içeriği / telefon gitmez — ayrıntı panelde);
#   • genel süzgeç: e-posta, telefon, IBAN, TCKN/VKN biçimli değerler maskelenir;
#   • data alanında kişisel veri anahtarları (phone/email/name/address…) atılır.
_PII_DATA_KEYS = {"phone", "email", "name", "customer", "customer_name", "first_name", "last_name",
                  "full_name", "address", "tckn", "tax_number", "iban", "message", "body", "text",
                  # KVKK: adres/telefon taşıyabilen diğer anahtarlar
                  "adres", "shipping_address", "billing_address", "address_line", "gsm", "mobile",
                  "telefon", "customer_phone", "city_address", "street"}
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_IBAN_RE = re.compile(r"\bTR\s?\d{2}(?:\s?\d{4}){5}\s?\d{2}\b", re.I)
_PHONE_RE = re.compile(r"(?<![\w])(?:\+?90[\s-]?)?\(?0?5\d{2}\)?[\s-]?\d{3}[\s-]?\d{2}[\s-]?\d{2}(?!\d)")
_LONG_DIGITS_RE = re.compile(r"(?<![\w])\d{10,11}(?!\d)")   # TCKN (11) / VKN (10) / çıplak telefon
_ITEMS_RE = re.compile(r"(\d+)\s*ürün")


def _scrub_text(v: str, keep: tuple = ()) -> str:
    """PII biçimli değerleri maskeler; `keep` içindeki değerler (sipariş no — pazaryeri sipariş
    numaraları 10-11 haneli olabilir) korunur."""
    s = str(v or "")
    holders = {}
    for i, k in enumerate(x for x in keep if x):
        ph = f"\x00{i}\x00"
        if k in s:
            s = s.replace(k, ph)
            holders[ph] = k
    s = _EMAIL_RE.sub("[e-posta]", s)
    s = _IBAN_RE.sub("[IBAN]", s)
    s = _PHONE_RE.sub("[telefon]", s)
    # KVKK: serbest metindeki adres kalıbı (Mah./Sok./Cad. …) ve kalan TR telefon biçimleri
    from security.contact_pii import scrub_contact_text as _sct
    s = _sct(s)
    s = _LONG_DIGITS_RE.sub("[numara]", s)
    for ph, k in holders.items():
        s = s.replace(ph, k)
    return s


def sanitize_admin_push(title: str, body: str, data: dict = None) -> tuple:
    """(title, body, data) — gönderilecek PII'siz içerik. Saf fonksiyon (test edilir)."""
    d = {str(k): str(v) for k, v in (data or {}).items() if str(k).lower() not in _PII_DATA_KEYS}
    kind = d.get("type") or ""
    onum = str(d.get("order_number") or "").strip()
    if kind == "new_order":
        _m = _ITEMS_RE.search(str(body or ""))
        title = "🛍️ Yeni Sipariş" + (" (ödendi)" if "ödendi" in str(title or "") else "")
        body = f"Yeni sipariş {onum}".strip() + (f" · {_m.group(1)} ürün" if _m else "")
    elif kind == "whatsapp_handoff":
        title = "📞 Yanıt bekleyen müşteri"
        body = "WhatsApp'ta yanıt bekleyen bir müşteri var — ayrıntı panelde."
    keep = tuple(x for x in (onum, str(d.get("order_id") or "")) if x)
    return (_scrub_text(title, keep)[:120], _scrub_text(body, keep)[:300],
            {k: (v if k in ("order_id", "order_number", "type") else _scrub_text(v, keep))[:120] for k, v in d.items()})


async def _fcm_config():
    """FCM v1 için (project_id, service_account_info) döndürür; yoksa (None, None)."""
    project = os.environ.get("FCM_PROJECT_ID", "").strip()
    sa = os.environ.get("FCM_SERVICE_ACCOUNT", "").strip()
    if not project or not sa:
        cfg = await db.settings.find_one({"id": "push"}, {"_id": 0}) or {}
        project = project or str(cfg.get("fcm_project_id") or "").strip()
        sa = sa or cfg.get("fcm_service_account")
    return (project or None), (sa or None)


def _log_entry(data: dict, sent: int, targets: int, via: str = "fcm_v1") -> dict:
    """push_notifications_log satırı — PII'siz: tür + sipariş no + sayaçlar."""
    d = data or {}
    return {
        "kind": str(d.get("type") or "generic")[:40],
        "order_number": str(d.get("order_number") or "")[:40],
        "sent": int(sent or 0), "targets": int(targets or 0), "via": via,
        "sent_at": datetime.now(timezone.utc).isoformat(),
    }


# ── iOS: DOĞRUDAN APNs (Apple Push) ──────────────────────────────────────────────────────────
# Capacitor PushNotifications iOS'ta HAM APNs cihaz token'ı verir (Firebase SDK kurulu değil).
# FCM v1 bu token'ı kabul etmez → iPhone'lara Apple'a doğrudan, token-tabanlı (.p8) kimlik
# doğrulamayla HTTP/2 üzerinden gönderilir. Android token'ları FCM v1 ile gitmeye devam eder.
#
# Yapılandırma (birini seç; ikisi de yoksa iOS gönderimi SESSİZCE atlanır):
#   • env  APNS_KEY_ID + APNS_TEAM_ID + APNS_KEY (.p8 dosyasının içeriği; satır sonları \n olabilir)
#          [APNS_TOPIC (varsayılan tr.com.atilla.admin), APNS_ENV=production|sandbox (varsayılan production)]
#   • ya da db.settings id="push": { apns_key_id, apns_team_id, apns_key, apns_topic, apns_env }
# TestFlight ve App Store build'leri PRODUCTION APNs kullanır (Xcode'dan cihaza kurulan debug → sandbox).
_APNS_HOSTS = {"production": "https://api.push.apple.com", "sandbox": "https://api.sandbox.push.apple.com"}
_DEFAULT_APNS_TOPIC = "tr.com.atilla.admin"
_apns_jwt_cache: dict = {"sig": None, "token": None, "at": 0.0}


def _normalize_p8(raw: str) -> str:
    """.p8 içeriğini PEM'e getirir: '\\n' kaçışlı tek satır ya da başlıksız base64 de kabul edilir."""
    s = str(raw or "").strip().replace("\\n", "\n")
    if not s:
        return ""
    _label = "PRIVATE " + "KEY"  # PEM etiketi (tek literal değil: sır tarayıcısı başlığı anahtar sanmasın)
    if f"BEGIN {_label}" not in s:
        body = "".join(s.split())
        s = (f"-----BEGIN {_label}-----\n" + "\n".join(body[i:i + 64] for i in range(0, len(body), 64))
             + f"\n-----END {_label}-----")
    return s


async def _apns_config():
    """APNs yapılandırması (dict) ya da yoksa None."""
    key_id = os.environ.get("APNS_KEY_ID", "").strip()
    team_id = os.environ.get("APNS_TEAM_ID", "").strip()
    key = os.environ.get("APNS_KEY", "").strip()
    topic = os.environ.get("APNS_TOPIC", "").strip()
    env = os.environ.get("APNS_ENV", "").strip().lower()
    if not (key_id and team_id and key):
        cfg = await db.settings.find_one({"id": "push"}, {"_id": 0}) or {}
        key_id = key_id or str(cfg.get("apns_key_id") or "").strip()
        team_id = team_id or str(cfg.get("apns_team_id") or "").strip()
        key = key or str(cfg.get("apns_key") or "").strip()
        topic = topic or str(cfg.get("apns_topic") or "").strip()
        env = env or str(cfg.get("apns_env") or "").strip().lower()
    if not (key_id and team_id and key):
        return None
    return {"key_id": key_id, "team_id": team_id, "key": _normalize_p8(key),
            "topic": topic or _DEFAULT_APNS_TOPIC,
            "env": env if env in _APNS_HOSTS else "production"}


def _apns_bearer(cfg: dict) -> str:
    """ES256 JWT. Apple: token en geç 60 dk'da yenilenmeli, 20 dk'dan sık YENİLENMEMELİ → 40 dk önbellek."""
    import jwt as _jwt
    now = time.time()
    sig = (cfg["key_id"], cfg["team_id"], hash(cfg["key"]))
    c = _apns_jwt_cache
    if c["token"] and c["sig"] == sig and now - c["at"] < 40 * 60:
        return c["token"]
    tok = _jwt.encode({"iss": cfg["team_id"], "iat": int(now)}, cfg["key"], algorithm="ES256",
                      headers={"kid": cfg["key_id"]})
    c.update({"sig": sig, "token": tok, "at": now})
    return tok


async def _apns_send(cfg: dict, tokens: list, title: str, body: str, data: dict) -> tuple:
    """iOS cihazlarına APNs ile gönderim. Dönüş: (gönderilen, bayat_tokenlar, hata_özetleri).
    Yalnız HTTP 410 (Unregistered = uygulama silinmiş) token'ı bayat sayılır; 400 BadDeviceToken
    ortam (sandbox/production) uyuşmazlığından da gelebileceği için token'lar PASİFE ALINMAZ."""
    import httpx
    aps_payload = {"aps": {"alert": {"title": title, "body": body}, "sound": "default", "badge": 1}}
    for k, v in (data or {}).items():
        if k != "aps":
            aps_payload[str(k)] = v
    headers = {"authorization": f"bearer {_apns_bearer(cfg)}", "apns-topic": cfg["topic"],
               "apns-push-type": "alert", "apns-priority": "10"}
    host = _APNS_HOSTS[cfg["env"]]
    sent, stale, errors = 0, [], []
    async with httpx.AsyncClient(http2=True, timeout=15) as c:
        for t in tokens:
            try:
                r = await c.post(f"{host}/3/device/{t}", headers=headers, json=aps_payload)
                if r.status_code == 200:
                    sent += 1
                    continue
                try:
                    reason = (r.json() or {}).get("reason") or ""
                except Exception:
                    reason = ""
                errors.append(f"APNs {r.status_code} {reason}".strip())
                if r.status_code == 410:
                    stale.append(t)
            except Exception as e:
                errors.append(f"APNs bağlantı hatası: {type(e).__name__}")
    return sent, stale, errors


async def _admin_user_ids() -> list:
    """Yönetici bildirimi alabilecek AKTİF personel kullanıcı id'leri (require_admin ile aynı ölçüt).
    Denetim 2026-10 #7: pasif bırakılmış ya da is_admin bayrağı kalmış eski müşteri hesapları dışarıda."""
    return [u["id"] async for u in db.users.find(
        {"is_admin": True, "is_active": {"$ne": False}, "role": {"$ne": "customer"},
         "$or": [{"is_super_admin": True},
                 {"role_id": {"$exists": True, "$nin": [None, ""]}}]},
        {"_id": 0, "id": 1}) if u.get("id")]


async def send_push_to_devices(devices: list, title: str, body: str, data: dict = None,
                               image_url: str = None) -> dict:
    """Verilen cihaz kayıtlarına (push_token + platform) platformuna göre push gönderir:
    iOS → APNs, diğerleri (Android) → FCM v1. İçerik OLDUĞU GİBİ gider (PII süzgeci YOK —
    yalnız yöneticinin elle yazdığı bildirimler için; otomatik bildirimler send_push_to_admins'ten geçer).
    Yapılandırması olmayan platform için cihazlar `queued` sayılır. Asla exception fırlatmaz."""
    res = {"sent": 0, "failed": 0, "queued": 0, "stale": [], "errors": [], "via": []}
    ios = [d for d in devices if d.get("push_token") and str(d.get("platform") or "").lower() == "ios"]
    others = [d for d in devices if d.get("push_token") and d not in ios]
    _data = {str(k): str(v) for k, v in (data or {}).items()}
    if ios:
        try:
            cfg = await _apns_config()
            if not cfg:
                res["queued"] += len(ios)
                res["errors"].append("iOS: APNs yapılandırılmamış (APNS_KEY_ID/APNS_TEAM_ID/APNS_KEY)")
            else:
                toks = list(dict.fromkeys(d["push_token"] for d in ios))
                s, st, errs = await _apns_send(cfg, toks, title, body, _data)
                res["sent"] += s
                res["failed"] += max(len(toks) - s, 0)
                res["stale"] += st
                res["errors"] += errs[:5]
                res["via"].append("apns")
        except Exception as e:
            res["failed"] += len(ios)
            res["errors"].append(f"iOS gönderim hatası: {type(e).__name__}")
            logger.warning(f"push: APNs gönderim hatası ({e})")
    if others:
        try:
            project, sa = await _fcm_config()
            if not (project and sa):
                res["queued"] += len(others)
                res["errors"].append("Android: FCM v1 yapılandırılmamış (FCM_PROJECT_ID/FCM_SERVICE_ACCOUNT)")
            else:
                toks = list(dict.fromkeys(d["push_token"] for d in others))
                if image_url:
                    s, st = await _fcm_send(project, sa, toks, title, body, _data, image_url=image_url)
                else:
                    s, st = await _fcm_send(project, sa, toks, title, body, _data)
                res["sent"] += s
                res["failed"] += max(len(toks) - s, 0)
                res["stale"] += st
                res["via"].append("fcm_v1")
        except Exception as e:
            res["failed"] += len(others)
            res["errors"].append(f"Android gönderim hatası: {type(e).__name__}")
            logger.warning(f"push: FCM v1 gönderim hatası ({e})")
    if res["stale"]:
        try:
            await db.user_devices.update_many({"push_token": {"$in": res["stale"]}}, {"$set": {"is_active": False}})
        except Exception:
            pass
    return res


async def _push_diag(kind: str, result: str, **extra) -> None:
    """Son yönetici-bildirim denemesinin sonucu (PII'siz) — panel 'Bildirim Durumu' bunu gösterir."""
    try:
        await db.integration_state.update_one(
            {"id": "push_last_attempt"},
            {"$set": {"kind": str(kind or "generic")[:40], "result": result, **extra,
                      "at": datetime.now(timezone.utc).isoformat()}}, upsert=True)
    except Exception:
        pass


async def send_push_to_admins(title: str, body: str, data: dict = None) -> int:
    """Kayıtlı TÜM aktif admin cihazlarına (db.user_devices) modern FCM v1 ile push.
    Best-effort: yapılandırma/hata yoksa 0 döner, ASLA exception fırlatmaz.
    İçerik gönderimden önce `sanitize_admin_push` ile kişisel veriden arındırılır (ADM-15)."""
    try:
        title, body, data = sanitize_admin_push(title, body, data)
        # GÜVENLİK: "admin" bildirimi (sipariş PII içerebilir) YALNIZCA admin
        # kullanıcıların cihazlarına gitmeli. user_devices müşteri cihazlarını da
        # içerdiğinden, önce admin user_id kümesini çıkar ve cihazları ona göre filtrele.
        _kind = str((data or {}).get("type") or "generic")
        admin_ids = await _admin_user_ids()
        if not admin_ids:
            await _push_diag(_kind, "Yönetici hesabı bulunamadı")
            return 0
        devices = await db.user_devices.find(
            {"is_active": True, "push_token": {"$nin": [None, ""]}, "user_id": {"$in": admin_ids}},
            {"_id": 0, "push_token": 1, "device_id": 1, "platform": 1}).to_list(5000)
        tokens = list(dict.fromkeys(d["push_token"] for d in devices if d.get("push_token")))
        _plat = {}
        for _d in devices:
            _plat.setdefault(_d.get("push_token"), str(_d.get("platform") or "").lower())
        # SHOP10: hesap değişimi — aynı cihaz/push token'ı personel çıkışından SONRA başka (müşteri)
        # hesabıyla kaydedildiyse token artık o hesabındır → yönetici bildirimi GÖNDERİLMEZ.
        # Token başına EN SON kayıt (last_seen_at) belirleyicidir.
        if tokens:
            _latest: dict = {}
            async for _d in db.user_devices.find({"push_token": {"$in": tokens}},
                                                 {"_id": 0, "push_token": 1, "user_id": 1, "last_seen_at": 1,
                                                  "is_active": 1}):
                _cur = _latest.get(_d.get("push_token"))
                if _cur is None or str(_d.get("last_seen_at") or "") > str(_cur.get("last_seen_at") or ""):
                    _latest[_d.get("push_token")] = _d
            _admins = set(admin_ids)
            tokens = [t for t in tokens if (_latest.get(t) or {}).get("user_id") in _admins
                      and (_latest.get(t) or {}).get("is_active") is not False]
        if not tokens:
            await _push_diag(_kind, "Kayıtlı yönetici cihazı yok (uygulamada panel hesabıyla giriş + bildirim izni gerekli)")
            return 0
        _data = {str(k): str(v) for k, v in (data or {}).items()}
        # Platform ayrımı: iOS ham APNs token'ı FCM v1'e gitmez → APNs; diğerleri (Android) → FCM v1.
        ios_tokens = [t for t in tokens if _plat.get(t) == "ios"]
        fcm_tokens = [t for t in tokens if _plat.get(t) != "ios"]
        sent, stale, used, _notes = 0, [], [], []
        if fcm_tokens:
            project, sa = await _fcm_config()
            if project and sa:
                _s, _st = await _fcm_send(project, sa, fcm_tokens, title, body, _data)
                sent += _s
                stale += list(_st or [])
                used.append("fcm_v1")
            else:
                logger.info("push: FCM v1 yapılandırılmamış — Android gönderimi atlandı")
                _notes.append("Android: FCM yapılandırılmamış")
        if ios_tokens:
            apns = await _apns_config()
            if apns:
                _s, _st, _errs = await _apns_send(apns, ios_tokens, title, body, _data)
                sent += _s
                stale += list(_st or [])
                used.append("apns")
                for _e in _errs[:3]:
                    logger.warning(f"push: {_e}")
                _notes += _errs[:3]
            else:
                logger.info("push: APNs yapılandırılmamış — iOS gönderimi atlandı")
                _notes.append("iOS: APNs yapılandırılmamış (Railway APNS_KEY_ID / APNS_TEAM_ID / APNS_KEY)")
        await _push_diag(_kind, "Gönderildi" if sent else "Gönderilemedi", sent=sent,
                         ios_devices=len(ios_tokens), android_devices=len(fcm_tokens), notes=_notes[:5])
        if not used:
            return 0
        if stale:
            try:
                await db.user_devices.update_many(
                    {"push_token": {"$in": stale}}, {"$set": {"is_active": False}})
            except Exception:
                pass
        try:
            # GÜVENLİK (ADM-15): log'a başlık/gövde METNİ yazılmaz (müşteri adı, ürün, tutar gibi PII
            # içerebilir) — yalnız tür + sipariş no (+ sayaçlar). İçerik gerekiyorsa sipariş panelden açılır.
            await db.push_notifications_log.insert_one(
                _log_entry(_data, sent, len(tokens), via="+".join(used)))
        except Exception:
            pass
        return sent
    except ModuleNotFoundError as e:
        logger.warning(f"push: FCM bağımlılığı eksik ({e})")
        return 0
    except Exception as e:
        logger.warning(f"push: FCM v1 gönderim hatası ({e})")
        return 0


async def _fcm_send(project: str, sa, tokens: list, title: str, body: str, data: dict,
                    image_url: str = None) -> tuple:
    """FCM v1 gönderimi (her cihaz için ayrı mesaj). Dönüş: (gönderilen, bayat_tokenlar)."""
    import json as _json
    import httpx
    from google.oauth2 import service_account as _gsa
    from google.auth.transport.requests import Request as _GRequest

    info = _json.loads(sa) if isinstance(sa, str) else sa
    creds = _gsa.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/firebase.messaging"])
    creds.refresh(_GRequest())
    access_token = creds.token
    url = f"https://fcm.googleapis.com/v1/projects/{project}/messages:send"
    sent, stale = 0, []
    async with httpx.AsyncClient(timeout=15) as c:
        for t in tokens:
            msg = {"message": {
                "token": t,
                "notification": {"title": title, "body": body, **({"image": image_url} if image_url else {})},
                "data": data,
                "apns": {"payload": {"aps": {"sound": "default", "badge": 1}}},
                "android": {"priority": "high", "notification": {"sound": "default"}},
            }}
            try:
                r = await c.post(url, headers={"Authorization": f"Bearer {access_token}"}, json=msg)
                if r.status_code == 200:
                    sent += 1
                elif r.status_code in (404, 410):
                    stale.append(t)
            except Exception:
                pass
    return sent, stale


@router.get("/status")
async def push_status(current_user: dict = Depends(require_permission("integrations.view", any_in_area=True))):
    """Bildirim tanısı (sır göstermez): APNs/FCM yapılandırması, yönetici cihaz sayıları, son deneme."""
    apns = await _apns_config()
    project, sa = await _fcm_config()
    admin_ids = await _admin_user_ids()
    counts = {}
    async for d in db.user_devices.find({"is_active": True, "push_token": {"$nin": [None, ""]},
                                         "user_id": {"$in": admin_ids}}, {"_id": 0, "platform": 1}):
        k = str(d.get("platform") or "bilinmiyor").lower()
        counts[k] = counts.get(k, 0) + 1
    last = await db.integration_state.find_one({"id": "push_last_attempt"}, {"_id": 0, "id": 0}) or {}
    return {"apns_configured": bool(apns), "apns_env": (apns or {}).get("env"),
            "apns_topic": (apns or {}).get("topic"), "fcm_configured": bool(project and sa),
            "admin_devices": counts, "last_attempt": last}


@router.post("/test")
async def push_test(current_user: dict = Depends(require_permission("settings.edit"))):
    """Yönetici: tüm kayıtlı admin cihazlarına test bildirimi (iOS → APNs, Android → FCM v1)."""
    n = await send_push_to_admins("Test Bildirimi",
                                  "Atilla admin push çalışıyor ✅",
                                  {"type": "test"})
    return {"success": True, "sent": n}
