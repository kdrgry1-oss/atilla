"""
=============================================================================
integrations_dogan.py — Doğan e-Dönüşüm entegrasyonu (extracted)
=============================================================================
Iter35 refactor adımı: integrations.py'den Doğan kısmı (4 endpoint, ~80 satır)
ayrı modüle taşındı. iyzico ve temu modülleri ile aynı pattern.

Endpoints:
- GET  /api/integrations/dogan/settings
- POST /api/integrations/dogan/settings
- POST /api/integrations/dogan/test-connection
- POST /api/integrations/dogan/check-user
=============================================================================
"""
from fastapi import APIRouter, Depends, HTTPException, Request

from .deps import db, require_admin, require_permission
from activity_audit import record_admin_audit

router = APIRouter(tags=["Integrations - Doğan"])


@router.get("/dogan/settings")
async def get_dogan_settings(current_user: dict = Depends(require_permission("integrations.view", any_in_area=True))):
    """Get Doğan e-Dönüşüm settings"""
    settings = await db.settings.find_one({"id": "dogan_edonusum"}, {"_id": 0})
    if not settings:
        return {"id": "dogan_edonusum", "enabled": False, "username": "", "password": "", "is_test": True}
    # Mask password — A3: ilk-3-karakter sızıntısı (password_masked) kaldırıldı, yalnız var/yok bilgisi.
    if settings.get("password"):
        settings["password"] = "********"
        settings["has_password"] = True
    return settings


# GÜVENLİK (denetim 2026-10): yalnız kodun GERÇEKTEN okuduğu ayar anahtarları yazılabilir
# (eskiden gövdenin tamamı $set ile yazılıyordu → keyfi alan enjeksiyonu). Yeni bir Doğan
# ayarı eklenirse buraya da eklenmeli.
_DOGAN_ALLOWED_KEYS = {
    "enabled", "username", "password", "is_test",
    "vkn", "supplier_name", "supplier_district", "supplier_city", "supplier_street",
    "supplier_tax_office", "supplier_phone", "supplier_email", "supplier_website",
    "earchive_prefix", "einvoice_prefix",
    "earchive_start_year", "earchive_start_number", "einvoice_start_year", "einvoice_start_number",
    "einvoice_profile", "sender_alias", "sender_unit_alias",
    "earsiv_link_template", "einvoice_link_template",
}
_DOGAN_BOOL_KEYS = {"enabled", "is_test"}


@router.post("/dogan/settings")
async def save_dogan_settings(payload: dict, request: Request,
                              current_user: dict = Depends(require_permission("integrations.dogan_edonusum"))):
    """Save Doğan e-Dönüşüm settings (yetki: integrations.dogan_edonusum; alan beyaz listesi; denetim kaydı)."""
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Geçersiz istek gövdesi")
    # Beyaz liste dışı alanlar YAZILMAZ (GET yanıtını geri gönderen istemci kırılmasın diye
    # 400 yerine sessizce atlanır; adları denetim kaydına düşer).
    unknown = sorted(str(k)[:60] for k in payload.keys() if k not in _DOGAN_ALLOWED_KEYS and k not in ("id", "has_password"))
    clean = {}
    for k in _DOGAN_ALLOWED_KEYS:
        if k not in payload:
            continue
        v = payload.get(k)
        if k in _DOGAN_BOOL_KEYS:
            clean[k] = bool(v)
        elif v is None or isinstance(v, (str, int, float)) and not isinstance(v, bool):
            clean[k] = (v.strip()[:500] if isinstance(v, str) else v)
        else:
            raise HTTPException(status_code=400, detail=f"Geçersiz değer: {k}")
    existing = await db.settings.find_one({"id": "dogan_edonusum"}, {"_id": 0}) or {}
    # Required alan validasyonu — enabled=True ise username/password zorunlu
    if clean.get("enabled"):
        username = clean.get("username") or existing.get("username")
        password = clean.get("password")
        if password in (None, "", "********"):
            password = existing.get("password")
        if not username or not password:
            raise HTTPException(
                status_code=400,
                detail="Doğan e-Dönüşüm aktif etmek için username ve password zorunludur"
            )
    clean["id"] = "dogan_edonusum"
    # Maskeli değer gelirse mevcut password'ü koru
    credential_rotated = False
    if clean.get("password") in (None, "", "********"):
        clean.pop("password", None)
    else:
        credential_rotated = True
        # GÜVENLİK: yeni parolayı at-rest ŞİFRELE (DoganClient okurken çözülür).
        try:
            from security.crypto import encrypt as _enc_secret
            clean["password"] = _enc_secret(clean["password"])
        except Exception:
            pass
    await db.settings.update_one({"id": "dogan_edonusum"}, {"$set": clean}, upsert=True)
    # Denetim: parola ASLA yazılmaz (yalnız 'değişti mi' bilgisi).
    _before = {k: existing.get(k) for k in clean if k not in ("password", "id")}
    _after = {k: clean.get(k) for k in clean if k not in ("password", "id")}
    await record_admin_audit(
        db, action="integrations.dogan_settings_update", entity_type="settings", entity_id="dogan_edonusum",
        before=_before, after=_after, current_user=current_user, request=request,
        source="admin.integrations", metadata={"credential_rotated": credential_rotated, "ignored_keys": unknown[:20]})
    return {"success": True, "message": "Doğan e-Dönüşüm ayarları kaydedildi"}


@router.post("/dogan/test-connection")
async def test_dogan_connection(current_user: dict = Depends(require_permission("integrations.view", any_in_area=True))):
    """Test connection to Doğan e-Dönüşüm"""
    from fastapi.concurrency import run_in_threadpool
    settings = await db.settings.find_one({"id": "dogan_edonusum"}, {"_id": 0})
    if not settings or not settings.get("username"):
        raise HTTPException(status_code=400, detail="Doğan e-Dönüşüm ayarları eksik")

    from dogan_client import DoganClient
    client = DoganClient(
        username=settings["username"],
        password=settings["password"],
        is_test=settings.get("is_test", True)
    )
    # AĞ TEŞHİSİ (canlı olay 2026-10-15: "login failed … connect timeout"): giriş denemesinden ÖNCE
    # Doğan sunucusunun adı çözülüyor mu, 443'e bağlanılabiliyor mu ve sunucumuzun DIŞ IP'si ne —
    # Doğan IP izni / adres değişikliği / kesinti ayrımı panelden görülsün. Sır/kimlik döndürülmez.
    from urllib.parse import urlparse
    auth_host = urlparse(client.auth_wsdl).hostname or ""

    def _probe(host: str) -> dict:
        import socket
        out = {"host": host, "dns": None, "tcp_443": False, "error": "", "per_ip": []}
        try:
            infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
            out["dns"] = sorted({ai[4][0] for ai in infos})[:6]
        except Exception as e:
            out["error"] = f"DNS çözülemedi: {str(e)[:120]}"
            return out
        # Her adresi (IPv4 + IPv6) AYRI dene — hangi hedef adres/aile engelleniyor görülsün.
        seen = set()
        for fam, stype, proto, _cn, sa in infos:
            if sa[0] in seen:
                continue
            seen.add(sa[0])
            r = {"ip": sa[0], "v": 6 if fam == socket.AF_INET6 else 4, "ok": False, "err": ""}
            try:
                with socket.socket(fam, stype, proto) as sk:
                    sk.settimeout(8)
                    sk.connect(sa)
                    r["ok"] = True
            except Exception as e:
                r["err"] = str(e)[:60]
            out["per_ip"].append(r)
        out["tcp_443"] = any(r["ok"] for r in out["per_ip"])
        if not out["tcp_443"]:
            out["error"] = "443 bağlantısı kurulamadı: " + "; ".join(
                f"{r['ip']} → {r['err'] or 'başarısız'}" for r in out["per_ip"])[:240]
        return out

    from dogan_client import dogan_proxy_url, dogan_proxy_display, dogan_proxy_env_state
    _px = dogan_proxy_url()
    if _px:
        # SABİT-IP ARACI SUNUCU üzerinden: doğrudan soket denemesi anlamsız (çıkış aracıdan).
        # Aracı → Doğan tünelini dene; hata türüne göre NEREDE koptuğu söylenir. Şifre gösterilmez.
        _pd = dogan_proxy_display()

        def _probe_proxy() -> dict:
            import requests
            try:
                with requests.get(client.auth_wsdl, proxies={"https": _px, "http": _px},
                                  timeout=(10, 20), stream=True) as r:
                    return {"ok": True, "status": r.status_code}
            except requests.exceptions.ProxyError as e:
                m = str(e)
                if "407" in m:
                    why = "aracı sunucu şifreyi kabul etmedi (DOGAN_PROXY_URL şifresini kontrol edin)"
                elif "403" in m:
                    why = "aracı sunucu bu adrese izin vermiyor (kurulum betiğindeki Doğan adresi)"
                elif "502" in m or "503" in m:
                    why = ("aracı sunucu Doğan'a BAĞLANAMADI — Doğan güvenlik duvarında aracı sunucunun "
                           "IP'si izinli değil ya da Doğan kesintide")
                else:
                    why = ("aracı sunucuya ulaşılamadı — sunucu kapalı ya da port güvenlik duvarında "
                           "kapalı")
                return {"ok": False, "error": f"{why} ({m[:120]})"}
            except requests.exceptions.ConnectTimeout as e:
                return {"ok": False, "error": f"aracı sunucuya bağlantı zaman aşımı — port kapalı olabilir ({str(e)[:100]})"}
            except Exception as e:
                return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:160]}"}

        pr = await run_in_threadpool(_probe_proxy)
        network = {"auth_host": auth_host, "mode": "TEST" if settings.get("is_test", True) else "CANLI",
                   "proxy": _pd, "proxy_probe": pr}
        if not pr.get("ok"):
            return {"success": False,
                    "message": f"Doğan'a sabit-IP aracı sunucu ({_pd}) üzerinden ULAŞILAMADI — {pr.get('error')}",
                    "network": network}
        result = await run_in_threadpool(client.test_connection)
        result["network"] = network
        if result.get("success"):
            result["message"] = (f"{result.get('message', 'Bağlantı başarılı')} ({network['mode']} · "
                                 f"sabit-IP aracı {_pd})")
        return result

    hosts = [auth_host] + [h for h in ("connector.doganedonusum.com", "efaturatest.doganedonusum.com",
                                       "www.doganedonusum.com") if h != auth_host]
    probes = [await run_in_threadpool(_probe, h) for h in hosts]
    egress_ip = ""
    egress_seen: list = []
    egress_v6 = ""
    try:
        import httpx
        async with httpx.AsyncClient(timeout=8) as _hc:
            # Railway yüksek erişilebilirlikte çıkış IP'si bağlantı başına değişebilir → 3 ölçüm.
            for _ in range(3):
                try:
                    _ip = (await _hc.get("https://api.ipify.org")).text.strip()[:45]
                    if _ip and _ip not in egress_seen:
                        egress_seen.append(_ip)
                except Exception:
                    pass
            try:
                _ip6 = (await _hc.get("https://api64.ipify.org")).text.strip()[:45]
                egress_v6 = _ip6 if ":" in _ip6 else ""
            except Exception:
                pass
    except Exception:
        pass
    egress_ip = ", ".join(egress_seen)
    network = {"auth_host": auth_host, "mode": "TEST" if settings.get("is_test", True) else "CANLI",
               "egress_ip": egress_ip, "egress_ipv6": egress_v6, "probes": probes}

    if probes and not probes[0]["tcp_443"]:
        return {"success": False,
                "message": (f"Doğan sunucusuna ({auth_host}) ULAŞILAMIYOR — {probes[0]['error']}. "
                            f"Bağlanılan Doğan adres(ler)i: {', '.join(probes[0].get('dns') or []) or '?'}. "
                            f"Sunucumuzun çıkış IP'si: {egress_ip or 'alınamadı'}"
                            f"{' (IPv6 çıkış: ' + egress_v6 + ')' if egress_v6 else ''}. Doğan'a bu bilgileri "
                            "iletin: kaynak IP'nin, bu hedef adres(ler)in 443 portu için güvenlik duvarında "
                            "izinli olup olmadığını kontrol etsinler. "
                            + {"yok": "[Sabit-IP aracı: DOGAN_PROXY_URL bu sunucuda TANIMLI DEĞİL — Railway'de "
                                      "backend servisinin Variables'ına eklenip deploy edilmeli.]",
                               "gecersiz": "[Sabit-IP aracı: DOGAN_PROXY_URL girilmiş ama GEÇERSİZ — "
                                           "http://kullanici:sifre@sunucu:port biçiminde olmalı.]"}.get(
                                   dogan_proxy_env_state(), "")),
                "network": network}
    # Sync SOAP çağrısı event loop'u bloklamasın diye threadpool'da çalıştır
    result = await run_in_threadpool(client.test_connection)
    result["network"] = network
    if result.get("success"):
        result["message"] = f"{result.get('message', 'Bağlantı başarılı')} ({network['mode']} · çıkış IP {egress_ip or '?'})"
    return result


@router.post("/dogan/check-user")
async def check_dogan_user(payload: dict, current_user: dict = Depends(require_permission("orders.view"))):
    """Check if a VKN is registered for e-Fatura"""
    vkn = payload.get("vkn", "")
    if not vkn:
        raise HTTPException(status_code=400, detail="VKN gerekli")

    settings = await db.settings.find_one({"id": "dogan_edonusum"}, {"_id": 0})
    if not settings or not settings.get("username"):
        raise HTTPException(status_code=400, detail="Doğan e-Dönüşüm ayarları eksik")

    from dogan_client import DoganClient
    client = DoganClient(
        username=settings["username"],
        password=settings["password"],
        is_test=settings.get("is_test", True)
    )
    result = client.check_user(vkn)
    return result
