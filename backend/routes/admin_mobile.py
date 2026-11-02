"""
=============================================================================
admin_mobile.py — Admin tarafında mobil uygulama yönetimi
=============================================================================
- App version'ları (ios/android) güncelle
- Feature flags güncelle
- Push notification gönder (FCM/APNs)
- Cihazları listele
=============================================================================
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from datetime import datetime, timezone
from typing import Optional
from pydantic import BaseModel, Field

from .deps import db, require_admin, require_permission
from .push import send_push_to_devices, _admin_user_ids
from activity_audit import record_admin_audit

router = APIRouter(prefix="/admin/mobile", tags=["admin-mobile"])


@router.get("/versions")
async def get_versions(current_user: dict = Depends(require_permission("integrations.view", any_in_area=True))):
    cfg = await db.settings.find_one({"id": "app_versions"}, {"_id": 0})
    return cfg or {"id": "app_versions", "versions": {
        "ios": {"min_version": "1.0.0", "latest_version": "1.0.0", "force_update": False, "store_url": "", "release_notes": ""},
        "android": {"min_version": "1.0.0", "latest_version": "1.0.0", "force_update": False, "store_url": "", "release_notes": ""},
    }}


class VersionUpdateReq(BaseModel):
    versions: dict


@router.post("/versions")
async def update_versions(req: VersionUpdateReq, current_user: dict = Depends(require_permission("integrations.edit"))):
    await db.settings.update_one(
        {"id": "app_versions"},
        {"$set": {"versions": req.versions, "updated_at": datetime.now(timezone.utc).isoformat(),
                  "updated_by": current_user.get("email")}},
        upsert=True,
    )
    return {"success": True}


@router.get("/config")
async def get_app_config(current_user: dict = Depends(require_permission("integrations.view", any_in_area=True))):
    cfg = await db.settings.find_one({"id": "app_config"}, {"_id": 0})
    return cfg or {"id": "app_config"}


# GÜVENLİK (INJ-01, mass assignment): gövde eskiden OLDUĞU GİBİ $set ediliyordu → `id` alanı
# app_config belgesini başka bir ayar belgesine (ör. "iyzico") yeniden anahtarlıyor, rastgele
# alanlar (api_key/api_secret…) yazılabiliyordu. Yalnız panelin düzenlediği bölümler yazılır.
_APP_CONFIG_SECTIONS = ("feature_flags", "branding", "support")


@router.post("/config")
async def update_app_config(payload: dict, current_user: dict = Depends(require_permission("integrations.edit"))):
    upd = {}
    for _k in _APP_CONFIG_SECTIONS:
        if _k not in (payload or {}):
            continue
        _v = payload.get(_k)
        if not isinstance(_v, dict):
            raise HTTPException(status_code=400, detail=f"'{_k}' bir nesne olmalı")
        upd[_k] = _v
    await db.settings.update_one(
        {"id": "app_config"},
        {"$set": {**upd, "updated_at": datetime.now(timezone.utc).isoformat(),
                  "updated_by": current_user.get("email")}},
        upsert=True,
    )
    return {"success": True}


@router.get("/devices")
async def list_devices(platform: Optional[str] = None, limit: int = 100,
                       current_user: dict = Depends(require_permission("integrations.view", any_in_area=True))):
    q = {"is_active": True}
    if platform:
        q["platform"] = platform
    total = await db.user_devices.count_documents(q)
    items = await db.user_devices.find(q, {"_id": 0, "push_token": 0}) \
        .sort("last_seen_at", -1).limit(limit).to_list(limit)
    # platform breakdown
    pipeline = [
        {"$match": {"is_active": True}},
        {"$group": {"_id": "$platform", "count": {"$sum": 1}}},
    ]
    breakdown = {r["_id"]: r["count"] async for r in db.user_devices.aggregate(pipeline)}
    return {"total": total, "items": items, "by_platform": breakdown}


# ---------------------------------------------------------------------------
# PUSH NOTIFICATION SENDER (iOS → APNs, Android → FCM HTTP v1)
# ---------------------------------------------------------------------------

class PushSendReq(BaseModel):
    target: str = Field("all", pattern="^(all|user|device|platform|admins)$")
    target_value: Optional[str] = None  # user_id / device_id / 'ios'/'android' (admins için gerekmez)
    title: str
    body: str
    data: Optional[dict] = None  # custom payload (deep link vb.)
    image_url: Optional[str] = None


@router.post("/push/send")
async def send_push(req: PushSendReq, request: Request,
                    current_user: dict = Depends(require_permission("campaigns.push_send"))):
    """Push notification gönder: iOS cihazlara APNs, Android cihazlara FCM v1. Bir platform için
    yapılandırma yoksa o cihazlar "queued" döner (gönderilmez). Hedef "admins" = yalnız personel cihazları.

    GÜVENLİK (denetim 2026-10): tüm müşterilere toplu bildirim → ayrı yetki
    (campaigns.push_send) + denetim kaydı. Süper admin ('*') geçer.
    """
    if not (req.title or "").strip() or not (req.body or "").strip():
        raise HTTPException(status_code=400, detail="Başlık ve metin zorunlu")
    if len(req.title) > 120 or len(req.body) > 1000:
        raise HTTPException(status_code=400, detail="Başlık en çok 120, metin en çok 1000 karakter olabilir")
    await record_admin_audit(
        db, action="mobile.push_send", entity_type="push", entity_id=req.target,
        current_user=current_user, request=request, source="admin.mobile",
        metadata={"target": req.target, "has_target_value": bool(req.target_value),
                  "title_len": len(req.title), "body_len": len(req.body)})
    # Hedef cihazları topla
    q = {"is_active": True, "push_token": {"$nin": [None, ""]}}
    if req.target == "admins":
        # Yalnız aktif yönetici/personel cihazları (kendi telefonuna test için).
        q["user_id"] = {"$in": await _admin_user_ids()}
    elif req.target == "user" and req.target_value:
        q["user_id"] = req.target_value
    elif req.target == "device" and req.target_value:
        q["device_id"] = req.target_value
    elif req.target == "platform" and req.target_value in ("ios", "android"):
        q["platform"] = req.target_value
    devices = await db.user_devices.find(q, {"_id": 0}).to_list(5000)

    if not devices:
        return {"success": False, "sent": 0, "failed": 0, "message": "Hedef cihaz bulunamadı"}

    # iOS → APNs (doğrudan), Android → FCM v1. (Eski FCM legacy API Haziran 2024'te Google tarafından
    # kapatıldığı için buradaki eski `fcm/send` + FCM_SERVER_KEY yolu KALDIRILDI.)
    result = await send_push_to_devices(devices, req.title, req.body, req.data or {}, req.image_url)
    sent, failed, queued = result["sent"], result["failed"], result["queued"]
    errors = result["errors"]

    await db.push_notifications_log.insert_one({
        "title": req.title,
        "body": req.body,
        "target": req.target,
        "target_value": req.target_value,
        "sent": sent,
        "failed": failed,
        "queued": queued,
        "via": "+".join(result["via"]) or "none",
        "errors": errors[:5],
        "sent_at": datetime.now(timezone.utc).isoformat(),
        "sent_by": current_user.get("email"),
    })

    message = None
    if queued and not sent:
        message = (f"{queued} cihaz için gönderim yapılandırması eksik — bildirim gönderilmedi. "
                   "İlgili anahtarlar tanımlanınca tekrar deneyin.")
    return {"success": bool(sent) or not (queued or failed), "sent": sent, "failed": failed, "queued": queued,
            "total_targets": len(devices), "errors": errors[:5], **({"message": message} if message else {})}
