"""
n11 pazaryeri entegrasyonu — kimlik Pazaryeri Hub'dan (marketplace_accounts key="n11").

Uçlar (prefix /integrations):
  GET  /n11/status              → yapılandırma durumu (sır döndürmez)
  POST /n11/test-connection     → canlı salt-okunur deneme (ürün listesi 1 kayıt)
  POST /n11/categories/sync     → kategori ağacını db.n11_categories'e yazar
  GET  /n11/categories          → yaprak kategoriler (arama)
  GET  /n11/categories/{id}/attributes
  GET  /n11/products            → n11'deki satıcı ürünleri (sayfalı)
  POST /n11/stock/sync          → n11'deki ürünlerin STOĞUNU Facette stoğuyla günceller (fiyat DEĞİL)
  POST /n11/orders/import       → sipariş paketlerini panele aktarır (Hepsiburada ile aynı kurallar)

Sipariş kuralları (HB/Trendyol ile aynı): bir n11 sipariş no = panelde tek sipariş; ilk
aktarımda stok idempotent düşülür; n11'de tamamen iptal olunca panel siparişi iptal + stok
bir kez iade edilir. Panelde elle iptal/iade edilmiş sipariş senkronla geri açılmaz.
"""
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from .deps import db, logger, generate_id, require_permission
from .integrations_common import (
    _decrement_stock_for_imported_order,
    _facette_match_for_codes,
    _facette_product_image,
    log_integration_event,
)

router = APIRouter(tags=["Integrations-N11"])

_CLOSED = ("cancelled", "returned", "refunded", "cancel_refunded")
_N11_STATUS = {
    "created": "confirmed", "picking": "confirmed", "unpacked": "confirmed",
    "shipped": "shipped", "delivered": "delivered",
    "cancelled": "cancelled", "unsupplied": "cancelled",
}
_STATUS_RANK = {"confirmed": 1, "shipped": 2, "delivered": 3}


# ---------------------------------------------------------------- yapılandırma
async def _n11_account() -> dict:
    return await db.marketplace_accounts.find_one({"key": "n11"}, {"_id": 0}) or {}


async def _get_n11_client(require_enabled: bool = True):
    acc = await _n11_account()
    if not acc:
        raise HTTPException(status_code=400, detail="N11 hesabı tanımlı değil. Pazaryeri Hub → N11 ekranından API bilgilerini girin.")
    if require_enabled and not acc.get("enabled"):
        raise HTTPException(status_code=400, detail="N11 entegrasyonu pasif. Pazaryeri Hub → N11 ekranından aktif edin.")
    creds = acc.get("credentials") or {}
    key = str(creds.get("api_key") or "").strip()
    secret = str(creds.get("api_secret") or "").strip()
    try:
        from security.crypto import decrypt as _dec
        secret = _dec(secret) or secret
    except Exception:
        pass
    # Kopyala-yapıştırda gelen boşluk/tırnak/görünmez karakterleri temizle.
    _junk = " \t\r\n\"'\u200b\ufeff"
    key, secret = (key or "").strip(_junk), (secret or "").strip(_junk)
    if not key or not secret:
        raise HTTPException(status_code=400, detail="N11 API Key / API Secret eksik.")
    integrator = str(creds.get("integrator") or "").strip()
    if not integrator:
        try:
            from company import get_company
            c = await get_company(db)
            integrator = c.get("store_name") or c.get("company_name") or ""
        except Exception:
            integrator = ""
    from n11_client import N11Client
    return N11Client(key, secret, integrator=integrator or "Magaza")


def _err_text(e: Exception) -> str:
    st = getattr(e, "status", None)
    if st == 401 or "authenticationFailed" in str(e):
        return "n11 kimlik doğrulaması başarısız — API Key / Secret'ı kontrol edin."
    return str(e)[:300]


@router.get("/n11/status")
async def n11_status(current_user: dict = Depends(require_permission("integrations.view", any_in_area=True))):
    acc = await _n11_account()
    creds = acc.get("credentials") or {}
    last = await db.integration_state.find_one({"id": "n11_sync"}, {"_id": 0}) or {}
    return {"configured": bool(creds.get("api_key") and creds.get("api_secret")),
            "enabled": bool(acc.get("enabled")), "last_order_sync": last.get("orders_at"),
            "last_stock_sync": last.get("stock_at"), "last_result": last.get("last_result")}


@router.post("/n11/test-connection")
async def n11_test_connection(current_user: dict = Depends(require_permission("integrations.n11"))):
    client = await _get_n11_client(require_enabled=False)
    # Tanı: üç farklı n11 servis ailesi ayrı ayrı denenir (ürün /ms, sipariş /rest). Sır gösterilmez.
    probes, total = [], None
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    for label, fn in (("Ürün servisi", lambda: client.product_query(page=0, size=1)),
                      ("Sipariş servisi", lambda: client.shipment_packages(start_ms=now_ms - 86400000,
                                                                           end_ms=now_ms, size=1))):
        try:
            res = await fn()
            if label == "Ürün servisi" and isinstance(res, dict):
                total = res.get("totalElements")
            probes.append({"service": label, "ok": True})
        except Exception as e:
            body = getattr(e, "body", None)
            code = ""
            if isinstance(body, dict):
                code = str(body.get("errorCode") or body.get("name") or body.get("@type") or "")
            probes.append({"service": label, "ok": False, "http": getattr(e, "status", None),
                           "error": f"{code} {str(e)[:200]}".strip()})
    ok = all(p["ok"] for p in probes)
    if ok:
        return {"success": True, "probes": probes,
                "message": f"n11 bağlantısı başarılı. n11'deki ürün sayısı: {total if total is not None else '?'}"}
    detail = " | ".join(f"{p['service']}: " + ("OK" if p["ok"] else f"HTTP {p.get('http')} {p.get('error')}")
                        for p in probes)
    hint = ""
    if any(p.get("http") in (401, 403) or "authentication" in str(p.get("error", "")).lower()
           for p in probes if not p["ok"]):
        hint = (" — n11 anahtarı reddetti. API Key ile Secret'ın AYNI API hesabına ait olduğundan, Secret'ın "
                "e-postadaki en güncel değer olduğundan ve Kaydet'e basıldığından emin olun.")
    return {"success": False, "probes": probes,
            "message": f"n11 bağlantısı başarısız{hint} Ayrıntı: {detail} "
                       f"(anahtar uzunluğu {len(client.app_key)}, secret uzunluğu {len(client.app_secret)})"}


# ---------------------------------------------------------------- kategoriler
def _flatten(nodes, parent_path="", out=None):
    out = [] if out is None else out
    for n in nodes or []:
        name = str(n.get("name") or "")
        path = f"{parent_path} > {name}" if parent_path else name
        subs = n.get("subCategories") or []
        out.append({"id": n.get("id"), "parent_id": n.get("parentId"), "name": name, "path": path,
                    "leaf": not subs})
        _flatten(subs, path, out)
    return out


@router.post("/n11/categories/sync")
async def n11_categories_sync(current_user: dict = Depends(require_permission("integrations.n11"))):
    client = await _get_n11_client(require_enabled=False)
    try:
        tree = await client.get_categories()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"n11 kategori ağacı alınamadı: {_err_text(e)}")
    flat = [c for c in _flatten(tree) if c.get("id") is not None]
    now = datetime.now(timezone.utc).isoformat()
    if flat:
        await db.n11_categories.delete_many({})
        await db.n11_categories.insert_many([{**c, "synced_at": now} for c in flat])
    return {"success": True, "count": len(flat), "leaf_count": sum(1 for c in flat if c["leaf"]),
            "message": f"{len(flat)} n11 kategorisi güncellendi."}


@router.get("/n11/categories")
async def n11_categories(q: str = Query("", max_length=80), limit: int = Query(50, ge=1, le=500),
                         current_user: dict = Depends(require_permission("integrations.view", any_in_area=True))):
    import re
    flt: Dict[str, Any] = {"leaf": True}
    if q.strip():
        flt["path"] = {"$regex": re.escape(q.strip()), "$options": "i"}
    rows = await db.n11_categories.find(flt, {"_id": 0}).limit(limit).to_list(limit)
    return {"categories": rows}


@router.get("/n11/categories/{category_id}/attributes")
async def n11_category_attributes(category_id: int,
                                  current_user: dict = Depends(require_permission("integrations.view", any_in_area=True))):
    client = await _get_n11_client(require_enabled=False)
    try:
        return await client.get_category_attributes(category_id)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"n11 kategori özellikleri alınamadı: {_err_text(e)}")


# ---------------------------------------------------------------- ürünler / stok
@router.get("/n11/products")
async def n11_products(page: int = Query(0, ge=0), size: int = Query(50, ge=1, le=250),
                       stock_code: str = Query("", max_length=255),
                       current_user: dict = Depends(require_permission("integrations.view", any_in_area=True))):
    client = await _get_n11_client(require_enabled=False)
    try:
        return await client.product_query(page=page, size=size, stock_code=stock_code or None)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"n11 ürün listesi alınamadı: {_err_text(e)}")


async def _all_n11_products(client, max_pages: int = 200) -> List[dict]:
    out = []
    for page in range(max_pages):
        res = await client.product_query(page=page, size=250)
        content = (res.get("content") or []) if isinstance(res, dict) else []
        if not content:
            break
        out.extend(content)
        if (res or {}).get("last") is True:
            break
    return out


def _variant_stock(prod: dict, code: str) -> Optional[int]:
    for v in prod.get("variants") or []:
        if code and code in (str(v.get("barcode") or ""), str(v.get("stock_code") or "")):
            try:
                return max(0, int(v.get("stock") or 0))
            except Exception:
                return 0
    if code and code in (str(prod.get("barcode") or ""), str(prod.get("stock_code") or "")):
        try:
            return max(0, int(prod.get("stock") or 0))
        except Exception:
            return 0
    return None


async def run_n11_stock_sync(client=None) -> dict:
    """n11'de LİSTELİ ürünlerin stoğunu Facette stoğuna eşitler (yalnız quantity; fiyat dokunulmaz).
    Eşleşmeyen n11 ürünü atlanır (sıfırlanmaz). Değişmeyen stok gönderilmez."""
    client = client or await _get_n11_client()
    listed = await _all_n11_products(client)
    skus, unmatched = [], 0
    for p in listed:
        sc = str(p.get("stockCode") or "").strip()
        if not sc:
            continue
        m = await _facette_match_for_codes([sc, p.get("barcode")])
        if not m:
            unmatched += 1
            continue
        prod, code, _how = m
        qty = _variant_stock(prod, str(code))
        if qty is None:
            unmatched += 1
            continue
        try:
            cur = int(p.get("quantity") or 0)
        except Exception:
            cur = -1
        if qty != cur:
            skus.append({"stockCode": sc, "quantity": min(qty, 999999)})
    tasks = []
    for i in range(0, len(skus), 1000):
        res = await client.price_stock_update(skus[i:i + 1000])
        tasks.append((res or {}).get("id"))
    out = {"listed": len(listed), "updated": len(skus), "unmatched": unmatched, "task_ids": tasks}
    await db.integration_state.update_one(
        {"id": "n11_sync"}, {"$set": {"stock_at": datetime.now(timezone.utc).isoformat(),
                                      "last_stock_result": out}}, upsert=True)
    return out


@router.post("/n11/stock/sync")
async def n11_stock_sync(current_user: dict = Depends(require_permission("integrations.n11"))):
    try:
        out = await run_n11_stock_sync()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"n11 stok senkronu başarısız: {_err_text(e)}")
    msg = f"n11'de {out['listed']} ürün tarandı, {out['updated']} ürünün stoğu güncellendi"
    if out["unmatched"]:
        msg += f", {out['unmatched']} ürün Facette ile eşleşmedi"
    return {"success": True, **out, "message": msg + "."}


# ---------------------------------------------------------------- siparişler
def _f(v, d=0.0) -> float:
    try:
        return round(float(v), 2)
    except Exception:
        return d


def _ms_iso(v) -> Optional[str]:
    try:
        return datetime.fromtimestamp(int(v) / 1000, tz=timezone.utc).isoformat()
    except Exception:
        return None


def map_n11_order(order_number: str, packages: List[dict]) -> dict:
    """Aynı n11 sipariş no'lu paketleri tek panel siparişine çevirir (bölünen 'Unpacked' üst paket hariç)."""
    active = [p for p in packages if str(p.get("shipmentPackageStatus") or "").strip().lower() != "unpacked"] or packages
    statuses = [_N11_STATUS.get(str(p.get("shipmentPackageStatus") or "").strip().lower(), "confirmed") for p in active]
    live = [s for s in statuses if s != "cancelled"]
    status = "cancelled" if not live else min(live, key=lambda s: _STATUS_RANK.get(s, 1))
    items, subtotal, discount, seen = [], 0.0, 0.0, set()
    for p in active:
        p_cancel = _N11_STATUS.get(str(p.get("shipmentPackageStatus") or "").strip().lower()) == "cancelled"
        for ln in p.get("lines") or []:
            lid = str(ln.get("orderLineId") or "")
            if lid and lid in seen:
                continue
            seen.add(lid)
            if p_cancel and live:
                continue  # kısmen iptal edilen paket kalemleri aktif siparişe girmez
            qty = int(ln.get("quantity") or 1) or 1
            unit = _f(ln.get("price"))
            sd = _f(ln.get("sellerDiscount")) + _f(ln.get("sellerCouponDiscount"))
            inv = ln.get("sellerInvoiceAmount")
            line_total = _f(inv) if inv not in (None, "") else round(unit * qty - sd, 2)
            size = color = ""
            for va in ln.get("variantAttributes") or []:
                nm = str((va or {}).get("name") or "").strip().lower()
                if nm == "beden":
                    size = (va or {}).get("value") or size
                elif nm == "renk":
                    color = (va or {}).get("value") or color
            items.append({
                "product_id": ln.get("stockCode") or ln.get("barcode") or "",
                "marketplace_sku": ln.get("stockCode") or "",
                "n11_product_id": str(ln.get("productId") or ""),
                "n11_order_line_id": lid,
                "product_name": ln.get("productName") or "", "name": ln.get("productName") or "",
                "quantity": qty, "unit_price": unit, "price": unit,
                "discount_amount": round(sd, 2), "line_total": line_total,
                "barcode": ln.get("barcode") or ln.get("stockCode") or "",
                "stock_code": ln.get("stockCode") or "",
                "size": size, "color": color, "currency": "TRY",
                "vat_rate": ln.get("vatRate"), "commission_rate": ln.get("commissionRate"),
            })
            subtotal += unit * qty
            discount += sd
    p0 = active[0] if active else {}
    ship = p0.get("shippingAddress") or {}
    bill = p0.get("billingAddress") or {}
    full = str(ship.get("fullName") or p0.get("customerfullName") or "n11 Müşterisi").strip()
    parts = full.split(" ", 1)
    bfull = str(bill.get("fullName") or full).strip().split(" ", 1)
    total = round(sum(i["line_total"] for i in items), 2) if items else _f(p0.get("totalAmount"))
    doc = {
        "order_number": str(order_number), "platform": "n11", "marketplace": "n11",
        "n11_order_number": str(order_number),
        "n11_package_ids": [str(p.get("id")) for p in packages if p.get("id") is not None],
        "user_id": None, "items": items,
        "shipping_address": {
            "first_name": parts[0] if parts else "n11", "last_name": parts[1] if len(parts) > 1 else "Müşterisi",
            "phone": ship.get("gsm") or "", "email": p0.get("customerEmail") or "",
            "address": ship.get("address") or "", "city": ship.get("city") or "",
            "district": ship.get("district") or "", "neighborhood": ship.get("neighborhood") or "",
            "postal_code": ship.get("postalCode") or "", "country": "TR",
        },
        "billing_address": {
            "first_name": bfull[0] if bfull else "", "last_name": bfull[1] if len(bfull) > 1 else "",
            "phone": bill.get("gsm") or "", "address": bill.get("address") or "",
            "city": bill.get("city") or "", "district": bill.get("district") or "", "country": "TR",
            "tax_number": bill.get("taxId") or p0.get("taxId") or "",
            "tax_office": bill.get("taxHouse") or p0.get("taxOffice") or "",
            "invoice_type": "corporate" if str(bill.get("invoiceType") or "") == "2" else "individual",
        },
        "subtotal": round(subtotal, 2), "shipping_cost": 0, "discount_amount": round(discount, 2),
        "total": total, "payment_method": "marketplace", "payment_status": "paid", "status": status,
        "marketplace_status": ",".join(sorted({str(p.get("shipmentPackageStatus") or "") for p in packages})),
        "cargo_tracking_number": str(p0.get("cargoTrackingNumber") or ""),
        "cargo_provider_name": p0.get("cargoProviderName") or "",
        "cargo_tracking_link": p0.get("cargoTrackingLink") or "",
        "n11_agreed_delivery_at": _ms_iso(p0.get("agreedDeliveryDate")),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    from security.pii import seal_doc_tax_ids as _pii_seal
    return _pii_seal(doc)


async def _n11_enrich_items(order: dict) -> dict:
    for it in order.get("items") or []:
        m = await _facette_match_for_codes([it.get("stock_code"), it.get("barcode")])
        if not m:
            it["matched"] = False
            continue
        prod, code, how = m
        vbar = ""
        for v in prod.get("variants") or []:
            if code in (v.get("barcode"), v.get("stock_code")):
                vbar = v.get("barcode") or ""
                it["size"] = it.get("size") or v.get("size") or ""
                it["color"] = it.get("color") or v.get("color") or ""
                break
        it["product_id"] = prod.get("id")
        it["facette_product_id"] = prod.get("id")
        it["barcode"] = vbar or prod.get("barcode") or it.get("barcode")
        img = _facette_product_image(prod)
        if img:
            it["image"] = img
        it["matched"] = True
        it["match_method"] = how
    return order


def _order_created_at(packages: List[dict]) -> str:
    ts = []
    for p in packages:
        for h in p.get("packageHistories") or []:
            try:
                ts.append(int(h.get("createdDate")))
            except Exception:
                pass
    if ts:
        return datetime.fromtimestamp(min(ts) / 1000, tz=timezone.utc).isoformat()
    return datetime.now(timezone.utc).isoformat()


async def _fetch_packages(client, start: datetime, end: datetime) -> List[dict]:
    """[start,end] aralığını n11'in 15 günlük pencere sınırına göre dilimleyip tüm sayfaları çeker."""
    out, cur = [], start
    while cur < end:
        w_end = min(end, cur + timedelta(days=14, hours=23))
        for page in range(500):
            res = await client.shipment_packages(start_ms=int(cur.timestamp() * 1000),
                                                 end_ms=int(w_end.timestamp() * 1000), page=page, size=100)
            content = (res.get("content") or []) if isinstance(res, dict) else []
            out.extend(content)
            tp = (res or {}).get("totalPages") if isinstance(res, dict) else None
            if not content or (isinstance(tp, int) and page + 1 >= tp):
                break
        cur = w_end
    return out


async def run_n11_order_import(days: int = 3, client=None) -> dict:
    client = client or await _get_n11_client()
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=max(1, min(int(days), 60)))
    pkgs = await _fetch_packages(client, start, end)
    grouped: Dict[str, List[dict]] = {}
    for p in pkgs:
        on = str(p.get("orderNumber") or "").strip()
        if on:
            grouped.setdefault(on, []).append(p)
    imported = updated = cancelled = 0
    from .orders import _restock_order_once, stamp_cancelled_at, _log_order_event
    for on, plist in grouped.items():
        # Aynı paket birden çok pencerede gelebilir → paket id'ye göre tekilleştir (son hali kalsın).
        uniq = {}
        for p in plist:
            uniq[str(p.get("id") or id(p))] = p
        plist = list(uniq.values())
        data = await _n11_enrich_items(map_n11_order(on, plist))
        ex = await db.orders.find_one({"order_number": on, "platform": "n11"}, {"_id": 0})
        if not ex:
            if data["status"] == "cancelled" or not data["items"]:
                continue  # panele hiç girmemiş ve n11'de iptal: kaydetme
            data["id"] = generate_id()
            data["created_at"] = _order_created_at(plist)
            await db.orders.insert_one(dict(data))
            imported += 1
            try:
                await _decrement_stock_for_imported_order(data, "n11")
            except Exception as e:
                logger.warning(f"[n11] stok düşümü başarısız ({on}): {e}")
            continue
        if ex.get("status") in _CLOSED:
            continue  # panelde kapanmış sipariş senkronla geri açılmaz
        if data["status"] == "cancelled":
            r = await db.orders.update_one(
                {"id": ex["id"], "status": {"$nin": list(_CLOSED)}},
                {"$set": {"status": "cancelled", "cancel_source": "n11_sync",
                          "marketplace_status": data["marketplace_status"],
                          "updated_at": datetime.now(timezone.utc).isoformat()}})
            if r.modified_count:
                cancelled += 1
                try:
                    await stamp_cancelled_at(order_id=ex["id"], order_number=on, source="n11_sync")
                except Exception:
                    pass
                try:
                    await _restock_order_once(ex, "order_cancelled")
                except Exception as e:
                    logger.warning(f"[n11] iptal stok iadesi başarısız ({on}): {e}")
                try:
                    await _log_order_event(ex["id"], "status", "n11'de iptal edildi (otomatik)", None,
                                           {"from": ex.get("status"), "to": "cancelled"})
                except Exception:
                    pass
            continue
        upd = {k: data[k] for k in ("marketplace_status", "n11_package_ids", "updated_at",
                                    "n11_agreed_delivery_at")}
        for ck in ("cargo_tracking_number", "cargo_provider_name", "cargo_tracking_link"):
            if data.get(ck):
                upd[ck] = data[ck]
        # Durum yalnız İLERİ gider (panelde elle ilerletilmiş durum geri alınmaz).
        if _STATUS_RANK.get(data["status"], 0) > _STATUS_RANK.get(ex.get("status"), 0):
            upd["status"] = data["status"]
        q_old = sum(int(i.get("quantity") or 1) for i in ex.get("items") or [])
        q_new = sum(int(i.get("quantity") or 1) for i in data.get("items") or [])
        if 0 < q_new < q_old and not ex.get("partial_cancelled"):
            upd["partial_cancelled"] = True
            upd["partial_cancel_at"] = datetime.now(timezone.utc).isoformat()
        await db.orders.update_one({"id": ex["id"]}, {"$set": upd})
        updated += 1
    out = {"packages": len(pkgs), "orders": len(grouped), "imported": imported, "updated": updated,
           "cancelled": cancelled}
    await db.integration_state.update_one(
        {"id": "n11_sync"}, {"$set": {"orders_at": datetime.now(timezone.utc).isoformat(), "last_result": out}},
        upsert=True)
    try:
        await log_integration_event("n11", "order_import", "orders", "", "success",
                                    f"{imported} yeni, {updated} güncel, {cancelled} iptal")
    except Exception:
        pass
    return out


@router.post("/n11/orders/import")
async def n11_orders_import(days: int = Query(3, ge=1, le=60),
                            current_user: dict = Depends(require_permission("integrations.n11"))):
    try:
        out = await run_n11_order_import(days=days)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"n11 siparişleri alınamadı: {_err_text(e)}")
    return {"success": True, **out,
            "message": f"n11: {out['imported']} yeni sipariş aktarıldı, {out['updated']} güncellendi, "
                       f"{out['cancelled']} iptal işlendi."}


async def n11_scheduled_sync() -> None:
    """Zamanlayıcı: Hub'da 'Otomatik Güncelleme' açıksa siparişleri (ve istenirse stoğu) senkronlar."""
    acc = await _n11_account()
    if not acc.get("enabled"):
        return
    auto = acc.get("auto_sync") or {}
    if not (auto.get("orders_enabled") or auto.get("products_enabled")):
        return
    try:
        client = await _get_n11_client()
    except HTTPException:
        return
    if auto.get("orders_enabled"):
        try:
            await run_n11_order_import(days=2, client=client)
        except Exception as e:
            logger.warning(f"[n11] otomatik sipariş senkronu: {e}")
    if auto.get("products_enabled"):
        try:
            await run_n11_stock_sync(client=client)
        except Exception as e:
            logger.warning(f"[n11] otomatik stok senkronu: {e}")


# ---------------------------------------------------------------- ürün aktarma (2. aşama)
def _norm(v) -> str:
    return " ".join(str(v or "").strip().lower().replace("ı", "i").replace("İ", "i").split())


def _n11_prices(product: dict, rules: dict) -> tuple:
    """(listPrice, salePrice) — Hub 'Aktarım Kuralları' (fiyat alanı + komisyon) ile."""
    def _num(k):
        try:
            return float(product.get(k) or 0)
        except Exception:
            return 0.0
    base = _num(rules.get("price_field") or "price") or _num("price")
    sale = _num(rules.get("sale_price_field") or "sale_price")
    if not (0 < sale < base):
        sale = base
    ctype = str(rules.get("commission_type") or "none")
    try:
        cval = float(rules.get("commission_value") or 0)
    except Exception:
        cval = 0.0

    def _apply(x):
        if ctype == "percent":
            return x * (1 + cval / 100.0)
        if ctype == "amount":
            return x + cval
        return x
    lp, sp = round(_apply(base), 2), round(_apply(sale), 2)
    return lp, min(sp, lp)


def _clean_desc(html_or_text: str, name: str) -> str:
    import re as _re
    s = str(html_or_text or "").strip()
    if len(_re.sub(r"<[^>]+>", "", s).strip()) < 20:
        s = f"<p>{name}</p>"
    return s


def _image_urls(product: dict) -> List[str]:
    out = []
    for im in product.get("images") or []:
        u = im.get("url") if isinstance(im, dict) else im
        if isinstance(u, str) and u.startswith("https://"):
            out.append(u)
    return out[:8]


def _pick_value(attr: dict, wanted: str):
    """Kategori özelliğinin değer listesinden ada göre valueId; yoksa (izinliyse) customValue."""
    w = _norm(wanted)
    if not w:
        return None
    for v in attr.get("attributeValues") or []:
        if _norm(v.get("value")) == w:
            return {"id": attr["attributeId"], "valueId": v.get("id"), "customValue": None}
    if attr.get("isCustomValue"):
        return {"id": attr["attributeId"], "valueId": None, "customValue": str(wanted)[:255]}
    return None


def _product_attr_sources(product: dict) -> Dict[str, str]:
    src = {}
    for d in (product.get("technical_details") or {}, product.get("trendyol_attributes") or {},
              product.get("hepsiburada_attributes") or {}):
        if isinstance(d, dict):
            for k, v in d.items():
                if isinstance(v, (str, int, float)) and str(v).strip():
                    src.setdefault(_norm(k), str(v))
    return src


async def _resolve_push_product(code: str) -> dict:
    code = (code or "").strip()
    prod = await db.products.find_one({"id": code}, {"_id": 0}) if code else None
    if not prod and code:
        m = await _facette_match_for_codes([code])
        prod = m[0] if m else None
    if not prod:
        raise HTTPException(status_code=404, detail="Ürün bulunamadı (barkod / stok kodu / ürün id).")
    return prod


def _build_n11_skus(product: dict, cat_attrs: List[dict], rules: dict, shipment_template: str,
                    preparing_day: int, overrides: Dict[str, Any]) -> tuple:
    """(skus, missing) — missing: doldurulamayan ZORUNLU kategori özellikleri."""
    from .integrations_common import _resolve_stock_code
    lp, sp = _n11_prices(product, rules)
    src = _product_attr_sources(product)
    main_id = _resolve_stock_code(product) or str(product.get("id"))
    imgs = _image_urls(product)
    title = str(product.get("name") or "").strip()
    if len(title) < 15:
        title = f"{product.get('brand') or ''} {title}".strip()
    try:
        vat = int(round(float(product.get("vat_rate") if product.get("vat_rate") is not None else 10)))
    except Exception:
        vat = 10
    if vat not in (0, 1, 10, 20):
        vat = 10
    variants = [v for v in (product.get("variants") or []) if (v.get("barcode") or v.get("stock_code"))]
    if not variants:
        variants = [{"barcode": product.get("barcode"), "stock_code": product.get("stock_code"),
                     "stock": product.get("stock"), "size": "", "color": ""}]
    skus, missing = [], {}
    for v in variants:
        attrs = []
        for a in cat_attrs:
            aid, an = a.get("attributeId"), _norm(a.get("attributeName"))
            ov = overrides.get(str(aid))
            val = None
            if ov not in (None, ""):
                ovs = str(ov)
                if ovs.isdigit() and any(str(x.get("id")) == ovs for x in a.get("attributeValues") or []):
                    val = {"id": aid, "valueId": int(ovs), "customValue": None}
                else:
                    val = _pick_value(a, ovs) or {"id": aid, "valueId": None, "customValue": ovs[:255]}
            elif aid == 1 or an == "marka":
                val = _pick_value(a, product.get("brand") or "") or \
                    {"id": aid, "valueId": None, "customValue": str(product.get("brand") or "")[:255] or None}
            elif an in ("renk", "color") and (v.get("color") or src.get("renk")):
                val = _pick_value(a, v.get("color") or src.get("renk"))
            elif an in ("beden", "numara", "size") and v.get("size"):
                val = _pick_value(a, v.get("size"))
            elif an in src:
                val = _pick_value(a, src[an])
            if val and (val.get("valueId") is not None or val.get("customValue")):
                attrs.append(val)
            elif a.get("isMandatory"):
                missing[str(aid)] = {"id": aid, "name": a.get("attributeName"),
                                     "custom_allowed": bool(a.get("isCustomValue")),
                                     "values": [{"id": x.get("id"), "value": x.get("value")}
                                                for x in (a.get("attributeValues") or [])][:300]}
        bc = str(v.get("barcode") or "").strip()
        sc = bc or str(v.get("stock_code") or "").strip()
        try:
            qty = max(0, min(int(v.get("stock") or 0), 999999))
        except Exception:
            qty = 0
        sku = {
            "title": title[:200], "description": _clean_desc(product.get("description"), title),
            "categoryId": None, "currencyType": "TL", "productMainId": main_id,
            "preparingDay": int(preparing_day), "shipmentTemplate": shipment_template,
            "stockCode": sc, "catalogId": None,
            "barcode": bc if (bc.isdigit() and 8 <= len(bc) <= 14) else None,
            "quantity": qty, "images": [{"url": u, "order": i + 1} for i, u in enumerate(imgs)],
            "attributes": attrs, "salePrice": sp, "listPrice": lp, "vatRate": vat,
        }
        skus.append(sku)
    return skus, list(missing.values())


@router.post("/n11/products/push")
async def n11_product_push(payload: dict, current_user: dict = Depends(require_permission("integrations.n11"))):
    """Tek ürünü (tüm varyantlarıyla) n11'e yükler. dry_run=True → yalnız önizleme + eksik özellikler.
    payload: {code, category_id, shipment_template, preparing_day=2, attributes:{attrId: valueId|metin}, dry_run}"""
    client = await _get_n11_client()
    product = await _resolve_push_product(str(payload.get("code") or ""))
    try:
        cat_id = int(payload.get("category_id") or product.get("n11_category_id") or 0)
    except Exception:
        cat_id = 0
    if not cat_id:
        raise HTTPException(status_code=400, detail="n11 kategorisi seçin (yaprak kategori).")
    acc = await _n11_account()
    tmpl = str(payload.get("shipment_template") or acc.get("n11_shipment_template") or "").strip()
    if not tmpl:
        raise HTTPException(status_code=400, detail="n11 teslimat şablonu adı gerekli (Mağazam → Hesabım → Teslimat Bilgileri).")
    try:
        prep = max(1, min(int(payload.get("preparing_day") or 2), 30))
    except Exception:
        prep = 2
    try:
        cat = await client.get_category_attributes(cat_id)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"n11 kategori özellikleri alınamadı: {_err_text(e)}")
    cat_attrs = (cat or {}).get("categoryAttributes") or []
    overrides = {**(product.get("n11_attributes") or {}), **{str(k): v for k, v in (payload.get("attributes") or {}).items()}}
    skus, missing = _build_n11_skus(product, cat_attrs, acc.get("transfer_rules") or {}, tmpl, prep, overrides)
    for s in skus:
        s["categoryId"] = cat_id
    problems = []
    if not _image_urls(product):
        problems.append("Ürünün https görseli yok.")
    if any(not s["stockCode"] for s in skus):
        problems.append("Barkodu/stok kodu olmayan varyant var.")
    if any(s["salePrice"] <= 0 for s in skus):
        problems.append("Fiyat 0 — ürün fiyatını kontrol edin.")
    preview = {"product": {"id": product.get("id"), "name": product.get("name")},
               "category_id": cat_id, "category_name": (cat or {}).get("name"),
               "sku_count": len(skus), "list_price": skus[0]["listPrice"] if skus else None,
               "sale_price": skus[0]["salePrice"] if skus else None,
               "variants": [{"stockCode": s["stockCode"], "quantity": s["quantity"]} for s in skus],
               "missing": missing, "problems": problems}
    if payload.get("dry_run") or missing or problems:
        return {"success": False if (missing or problems) else True, "ready": not (missing or problems),
                **preview,
                "message": ("Önizleme hazır — aktarabilirsiniz." if not (missing or problems)
                            else "Eksik bilgi var: " + "; ".join(problems + [f"zorunlu özellik '{m['name']}'" for m in missing]))}
    # Aktar
    await db.marketplace_accounts.update_one({"key": "n11"}, {"$set": {"n11_shipment_template": tmpl}})
    await db.products.update_one({"id": product["id"]}, {"$set": {
        "n11_category_id": cat_id, "n11_attributes": {str(k): v for k, v in (payload.get("attributes") or {}).items()}
        or product.get("n11_attributes") or {}}})
    try:
        res = await client.create_products(skus)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"n11 ürün yükleme reddedildi: {_err_text(e)}")
    task_id = (res or {}).get("id")
    status = (res or {}).get("status")
    results = []
    if task_id and status != "REJECT":
        import asyncio as _aio
        for _ in range(8):
            await _aio.sleep(2.5)
            try:
                det = await client.task_details(task_id)
            except Exception:
                continue
            rows = [r for r in ((det or {}).get("skus") or {}).get("content") or [] if r.get("itemCode")]
            if rows and all(str(r.get("status") or "").upper() in ("SUCCESS", "FAIL") for r in rows):
                results = [{"stockCode": r.get("itemCode"), "status": r.get("status"),
                            "reasons": r.get("reasons") or []} for r in rows]
                break
    await db.products.update_one({"id": product["id"]}, {"$set": {
        "n11_task_id": task_id, "n11_pushed_at": datetime.now(timezone.utc).isoformat()}})
    ok = [r for r in results if str(r["status"]).upper() == "SUCCESS"]
    fail = [r for r in results if str(r["status"]).upper() != "SUCCESS"]
    if status == "REJECT":
        msg = "n11 görevi reddetti: " + "; ".join(map(str, (res or {}).get("reasons") or []))
    elif not results:
        msg = f"n11 yüklemeyi kuyruğa aldı (görev {task_id}). Sonuç birkaç dakika içinde n11 Mağazam'da görünür."
    else:
        msg = f"{len(ok)} varyant yüklendi, {len(fail)} hata." + (
            " Hata: " + "; ".join(str(x) for x in (fail[0].get("reasons") or [])[:2]) if fail else "")
    await log_integration_event("n11", "product_push", "product", str(product.get("id")),
                                "success" if not fail and status != "REJECT" else "error", msg[:300])
    return {"success": status != "REJECT" and not fail, **preview, "task_id": task_id,
            "task_status": status, "results": results, "message": msg}
