"""Panel: Trendyol "Ürünleriniz" Excel'i ile sistem ürünlerini eşleştir (barkod + kategori)."""
from fastapi import APIRouter, Body, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import Response

from .deps import db, logger, require_permission

router = APIRouter()
_MAX = 30 * 1024 * 1024
_bg = {"task": None}


@router.post("/trendyol-excel-match")
async def trendyol_excel_match(file: UploadFile = File(...), dry_run: bool = Query(False),
                               create_missing: bool = Query(False), background: bool = Query(False),
                               skip_archived: bool = Query(False), rebuild_created: bool = Query(False),
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
                rep = await tm.run_match(db, content, dry_run=dry_run, create_missing=create_missing,
                                         skip_archived=skip_archived, rebuild_created=rebuild_created)
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
        report = await tm.run_match(db, content, dry_run=dry_run, create_missing=create_missing,
                                         skip_archived=skip_archived, rebuild_created=rebuild_created)
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


@router.post("/trendyol-excel-prune-archived")
async def trendyol_excel_prune_archived(payload: dict = Body(default={}),
                                        current_user: dict = Depends(require_permission("products.delete"))):
    """Excel'den oluşturulan ürünlerde Trendyol'da arşivli ilanların varyantlarını/ürünlerini KALICI siler."""
    dry = bool(payload.get("dry_run", True))
    if not dry and (payload.get("confirm") or "") != "ARSIVLILERI SIL":
        raise HTTPException(400, 'Onay gerekli: confirm="ARSIVLILERI SIL"')
    import trendyol_excel_match as tm
    res = await tm.prune_archived(db, dry_run=dry)
    if not dry:
        logger.warning(f"[trendyol-excel] arşivli ilanlar silindi: {res}")
    return res


@router.post("/catalog-fixups")
async def catalog_fixups(payload: dict = Body(default={}),
                         current_user: dict = Depends(require_permission("products.delete"))):
    """Toplu katalog düzeltmesi: tüm ürünlerde marka adı ve/veya Excel'den oluşturulan ürünlerde
    panel fiyatı = Trendyol ilan fiyatı ÷ bölen. Varsayılan dry_run=true."""
    dry = bool(payload.get("dry_run", True))
    brand = (payload.get("brand") or "").strip() or None
    try:
        div = float(payload["excel_price_divisor"]) if payload.get("excel_price_divisor") else None
    except (TypeError, ValueError):
        raise HTTPException(400, "excel_price_divisor sayı olmalı")
    if div is not None and not (1 <= div <= 20):
        raise HTTPException(400, "excel_price_divisor 1 ile 20 arasında olmalı")
    zero = bool(payload.get("excel_zero_stock"))
    if not brand and not div and not zero:
        raise HTTPException(400, "brand, excel_price_divisor ve/veya excel_zero_stock gerekli")
    import trendyol_excel_match as tm
    res = await tm.catalog_fixups(db, brand=brand, excel_price_divisor=div, excel_zero_stock=zero, dry_run=dry)
    if not dry:
        logger.warning(f"[catalog-fixups] uygulandı: brand={brand!r} divisor={div} "
                       f"{ {k: v for k, v in res.items() if k != 'excel_price_sample'} }")
    return res


@router.get("/trendyol-diagnose")
async def trendyol_diagnose(brand: str = Query("", max_length=80),
                            current_user: dict = Depends(require_permission("integrations.view"))):
    """Salt-okunur tanı: API bilgileri geçerli mi, mağazada kaç ürün var, marka adına göre Trendyol marka ID'si."""
    from routes.integrations_trendyol import get_trendyol_config
    from trendyol_client import TrendyolClient
    cfg = await get_trendyol_config()
    if not cfg or not cfg.get("api_key"):
        raise HTTPException(400, "Trendyol API bilgileri girilmemiş")
    client = TrendyolClient(supplier_id=cfg["supplier_id"], api_key=cfg["api_key"],
                            api_secret=cfg["api_secret"], mode=cfg.get("mode", "live"))
    out: dict = {"mode": cfg.get("mode"), "supplier_id": cfg.get("supplier_id")}
    try:
        data = await client._async_get(f"/product/sellers/{cfg['supplier_id']}/products", params={"size": 3, "page": 0})
        content = data.get("content") or []
        out["auth_ok"] = True
        out["seller_product_count"] = data.get("totalElements")
        out["seller_sample"] = [{"barcode": c.get("barcode"), "title": c.get("title"), "brand": c.get("brand"),
                                 "brandId": c.get("brandId"), "productMainId": c.get("productMainId")} for c in content]
    except Exception as e:
        out["auth_ok"] = False
        code = getattr(getattr(e, "response", None), "status_code", None)
        out["auth_error"] = f"{type(e).__name__}" + (f" HTTP {code}" if code else "")
    if brand.strip():
        try:
            res = await client._async_get("/product/brands/by-name", params={"name": brand.strip()})
            out["brands"] = res if isinstance(res, list) else (res.get("brands") or res)
        except Exception as e:
            code = getattr(getattr(e, "response", None), "status_code", None)
            out["brands_error"] = f"{type(e).__name__}" + (f" HTTP {code}" if code else "")
    return out


@router.post("/catalog-set-attributes")
async def catalog_set_attributes(payload: dict = Body(default={}),
                                 current_user: dict = Depends(require_permission("products.delete"))):
    """Toplu ürün özelliği yazımı (boşluk doldurma): items=[{id, attrs:{Ad: Değer}}].
    Üründe aynı adda DOLU bir özellik varsa dokunulmaz (elle girilen korunur)."""
    items = payload.get("items") or []
    if not isinstance(items, list) or not items or len(items) > 5000:
        raise HTTPException(400, "items: 1-5000 kayıt")
    import trendyol_excel_match as tm
    from datetime import datetime, timezone
    from pymongo import UpdateOne
    ids = [str(i.get("id")) for i in items if isinstance(i, dict) and i.get("id")]
    cur = {p["id"]: p async for p in db.products.find({"id": {"$in": ids}}, {"_id": 0, "id": 1, "attributes": 1})}
    now = datetime.now(timezone.utc).isoformat()
    ops, changed, skipped = [], 0, 0
    for it in items:
        p = cur.get(str((it or {}).get("id")))
        attrs = (it or {}).get("attrs") or {}
        if not p or not isinstance(attrs, dict) or not isinstance(p.get("attributes"), (list, type(None))):
            skipped += 1
            continue
        new = p.get("attributes") or []
        before = len(new)
        for name, value in attrs.items():
            if str(name).strip() and str(value).strip():
                new = tm._set_attr(new, str(name).strip()[:80], str(value).strip()[:200])
        if new != (p.get("attributes") or []) or len(new) != before:
            ops.append(UpdateOne({"id": p["id"]}, {"$set": {"attributes": new, "updated_at": now}}))
            changed += 1
    for i in range(0, len(ops), 500):
        await db.products.bulk_write(ops[i:i + 500], ordered=False)
    return {"changed": changed, "skipped": skipped, "total": len(items)}


@router.post("/ticimax-identity-apply")
async def ticimax_identity_apply(file: UploadFile = File(...), dry_run: bool = Query(True),
                                 current_user: dict = Depends(require_permission("products.delete"))):
    """Ticimax URUNID kimliğiyle katalog onarımı (gzip'li JSON kayıt listesi). Bkz. ticimax_identity.py."""
    import gzip
    import json
    raw = await file.read(_MAX + 1)
    if len(raw) > _MAX:
        raise HTTPException(413, "Dosya çok büyük")
    try:
        if raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
        records = json.loads(raw.decode("utf-8"))
        assert isinstance(records, list) and records and isinstance(records[0], dict)
    except Exception:
        raise HTTPException(400, "Geçersiz dosya (gzip'li JSON kayıt listesi bekleniyor)")
    if len(records) > 200000:
        raise HTTPException(400, "Çok fazla kayıt")
    import ticimax_identity as ti
    res = await ti.apply_identity(db, records, dry_run=dry_run)
    if not dry_run:
        logger.warning(f"[ticimax-identity] uygulandı: {res}")
    return res


@router.post("/hepsiburada-listings-apply")
async def hepsiburada_listings_apply(file: UploadFile = File(...), dry_run: bool = Query(True),
                                     current_user: dict = Depends(require_permission("products.delete"))):
    """Hepsiburada "Listelerim" ilanlarını URUNID ile varyantlara bağlar (gzip'li JSON kayıt listesi)."""
    import gzip
    import json
    raw = await file.read(_MAX + 1)
    if len(raw) > _MAX:
        raise HTTPException(413, "Dosya çok büyük")
    try:
        if raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
        records = json.loads(raw.decode("utf-8"))
        assert isinstance(records, list) and records and isinstance(records[0], dict)
    except Exception:
        raise HTTPException(400, "Geçersiz dosya (gzip'li JSON kayıt listesi bekleniyor)")
    if len(records) > 200000:
        raise HTTPException(400, "Çok fazla kayıt")
    import ticimax_identity as ti
    res = await ti.apply_hb_listings(db, records, dry_run=dry_run)
    if not dry_run:
        logger.warning(f"[hb-listings] uygulandı: {res}")
    return res
