"""Panel: Ticimax XMLExport ürün aktarımını başlat / son sonucu gör."""
import asyncio
import os

from fastapi import APIRouter, Body, Depends, HTTPException

from .deps import db, logger, require_permission

router = APIRouter()
_running = {"task": None}


@router.post("/ticimax-xml-import")
async def start_ticimax_xml_import(payload: dict = Body(default={}),
                                   current_user: dict = Depends(require_permission("integrations.xml_export"))):
    url = (payload.get("url") or os.environ.get("PRODUCT_XML_URL") or "").strip()
    if not url:
        raise HTTPException(400, "XML adresi gerekli (url veya PRODUCT_XML_URL)")
    t = _running.get("task")
    if t is not None and not t.done():
        return {"started": False, "message": "Aktarım zaten çalışıyor"}
    import ticimax_xml_import as tx

    async def _run():
        try:
            st = await tx.run_import(db, url, dry_run=bool(payload.get("dry_run")))
            logger.info(f"[ticimax-xml] aktarım bitti: {st}")
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
    return {"running": bool(t is not None and not t.done()), "last": last}
