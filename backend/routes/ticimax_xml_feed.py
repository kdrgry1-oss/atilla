"""Panel: Ticimax XMLExport ürün aktarımını başlat / son sonucu gör."""
import asyncio
import os

from fastapi import APIRouter, Body, Depends, HTTPException, Request

from .deps import db, logger, require_permission

router = APIRouter()
_running = {"task": None}


@router.post("/ticimax-xml-import")
async def start_ticimax_xml_import(payload: dict = Body(default={}),
                                   current_user: dict = Depends(require_permission("integrations.xml_export"))):
    import ticimax_xml_import as tx
    src = await tx.get_source(db)
    url = (payload.get("url") or src.get("url") or os.environ.get("PRODUCT_XML_URL") or "").strip()
    if not url:
        raise HTTPException(400, "XML adresi gerekli (url veya PRODUCT_XML_URL)")
    if not url.lower().startswith(("http://", "https://")):
        raise HTTPException(400, "XML adresi http(s) ile başlamalı")
    t = _running.get("task")
    if t is not None and not t.done():
        return {"started": False, "message": "Aktarım zaten çalışıyor"}
    dry = bool(payload.get("dry_run"))
    include_no_stock = bool(payload["include_no_stock"]) if "include_no_stock" in payload \
        else bool(src.get("include_no_stock"))
    if payload.get("url") and payload.get("save_source") and not dry:
        # Kalıcı kaynak: zamanlanmış senkron (cron) da bundan sonra bu adresi kullanır.
        await tx.set_source(db, url, include_no_stock)

    async def _run():
        try:
            st = await tx.run_import(db, url, dry_run=dry, include_no_stock=include_no_stock)
            logger.info(f"[ticimax-xml] aktarım bitti: {st}")
            if dry:
                await db.settings.update_one(
                    {"id": "ticimax_xml_import_dry"},
                    {"$set": {"id": "ticimax_xml_import_dry", **{k: v for k, v in st.items() if k != "structure"}}},
                    upsert=True)
        except Exception as e:
            logger.error(f"[ticimax-xml] aktarım hatası: {type(e).__name__}: {e}")
            await db.settings.update_one({"id": "ticimax_xml_import_last"},
                                         {"$set": {"id": "ticimax_xml_import_last", "error": f"{type(e).__name__}"}},
                                         upsert=True)
    _running["task"] = asyncio.create_task(_run())
    return {"started": True}


@router.get("/ticimax-xml-import")
async def ticimax_xml_import_status(current_user: dict = Depends(require_permission("integrations.xml_export"))):
    last = await db.settings.find_one({"id": "ticimax_xml_import_last"}, {"_id": 0}) or {}
    t = _running.get("task")
    dry = await db.settings.find_one({"id": "ticimax_xml_import_dry"}, {"_id": 0}) or {}
    return {"running": bool(t is not None and not t.done()), "last": last, "last_dry_run": dry}


RESET_CONFIRM = "TUM URUNLERI SIL"


@router.post("/catalog-reset")
async def catalog_reset(request: Request, payload: dict = Body(default={}),
                        current_user: dict = Depends(require_permission("products.delete"))):
    """TÜM ürünleri KALICI siler (geri alınamaz) — kataloğu sıfırdan kurmak için.
    Onay metni zorunlu; kategoriler, siparişler ve ayarlar silinmez."""
    if (payload.get("confirm") or "") != RESET_CONFIRM:
        raise HTTPException(400, f'Onay gerekli: confirm="{RESET_CONFIRM}"')
    t = _running.get("task")
    if t is not None and not t.done():
        raise HTTPException(409, "Ürün aktarımı çalışıyor — bitince tekrar deneyin")
    total = await db.products.count_documents({})
    if payload.get("dry_run"):
        return {"dry_run": True, "products": total}
    res = await db.products.delete_many({})
    await db.trendyol_excel_unmatched.delete_many({})
    try:
        from activity_audit import record_admin_audit
        await record_admin_audit(db, action="catalog.reset", entity_type="product", entity_id="*",
                                 before={"products": total}, after={"products": 0},
                                 current_user=current_user, request=request, source="catalog_reset")
    except Exception as e:
        logger.warning(f"[catalog-reset] denetim kaydı yazılamadı: {type(e).__name__}")
    logger.warning(f"[catalog-reset] {res.deleted_count} ürün kalıcı silindi")
    return {"deleted": res.deleted_count}
