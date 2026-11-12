"""Panel: Trendyol "Ürünleriniz" Excel'i ile sistem ürünlerini eşleştir (barkod + kategori)."""
from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import Response

from .deps import db, logger, require_permission

router = APIRouter()
_MAX = 30 * 1024 * 1024
_bg = {"task": None}


@router.post("/trendyol-excel-match")
async def trendyol_excel_match(file: UploadFile = File(...), dry_run: bool = Query(False),
                               create_missing: bool = Query(False), background: bool = Query(False),
                               current_user: dict = Depends(require_permission("products.marketplace_sync"))):
    name = (file.filename or "").lower()
    if not name.endswith((".xlsx", ".xls")):
        raise HTTPException(400, "Trendyol'dan indirilen .xlsx dosyasını yükleyin")
    content = await file.read(_MAX + 1)
    if len(content) > _MAX:
        raise HTTPException(413, "Dosya çok büyük (en fazla 30 MB)")
    import trendyol_excel_match as tm
    try:
        tm.save_last_upload(content)   # sunucuda (600) saklanır: kurulum tanısı ve yeniden çalıştırma için
    except Exception as e:
        logger.warning(f"[trendyol-excel] dosya saklanamadı: {type(e).__name__}")
    if background:
        # Uzun iş (on binlerce satır + ürün oluşturma): arka planda çalışır, sonuç GET ile okunur.
        t = _bg.get("task")
        if t is not None and not t.done():
            return {"started": False, "message": "Eşleştirme zaten çalışıyor"}

        async def _run():
            key = "trendyol_excel_match_dry" if dry_run else "trendyol_excel_match_last"
            try:
                rep = await tm.run_match(db, content, dry_run=dry_run, create_missing=create_missing)
                if dry_run:
                    await db.settings.update_one({"id": key}, {"$set": {"id": key, **rep}}, upsert=True)
            except Exception as e:
                logger.error(f"[trendyol-excel] arka plan eşleştirme hatası: {type(e).__name__}: {e}")
                await db.settings.update_one({"id": key}, {"$set": {"id": key, "error": type(e).__name__}},
                                             upsert=True)
        import asyncio
        _bg["task"] = asyncio.create_task(_run())
        return {"started": True}
    try:
        report = await tm.run_match(db, content, dry_run=dry_run, create_missing=create_missing)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        logger.error(f"[trendyol-excel] eşleştirme hatası: {type(e).__name__}: {e}")
        raise HTTPException(500, "Eşleştirme sırasında hata oluştu")
    return report


@router.get("/trendyol-excel-match")
async def trendyol_excel_match_last(current_user: dict = Depends(require_permission("integrations.view"))):
    last = await db.settings.find_one({"id": "trendyol_excel_match_last"}, {"_id": 0}) or {}
    t = _bg.get("task")
    if t is not None or last.get("error"):
        dry = await db.settings.find_one({"id": "trendyol_excel_match_dry"}, {"_id": 0}) or {}
        return {**last, "running": bool(t is not None and not t.done()), "last_dry_run": dry}
    return last


@router.get("/trendyol-excel-match/unmatched.xlsx")
async def trendyol_excel_unmatched(current_user: dict = Depends(require_permission("integrations.view"))):
    # ACİL ANAHTAR: freeze_exports açıkken toplu veri dışa aktarımı yapılmaz (423).
    from security_switches import require_switch_off as _sw_off, emit_security_event as _sec_emit
    await _sw_off("freeze_exports")
    import trendyol_excel_match as tm
    data = await tm.unmatched_xlsx(db)
    await _sec_emit("trendyol_excel_unmatched_export", current_user, {"export": "trendyol-eslesmeyenler.xlsx"})
    return Response(content=data,
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": 'attachment; filename="trendyol-eslesmeyenler.xlsx"'})
