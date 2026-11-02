"""n11: sipariş paketleri panele tek sipariş olarak aktarılır (stok bir kez düşer), n11'de iptal
olunca panel siparişi iptal + stok bir kez iade; stok senkronu yalnız n11'de listeli eşleşen ürünler."""
from conftest import kv_ip, kv_run


class FakeN11:
    def __init__(self, pkgs, listed=None):
        self.pkgs, self.listed, self.sent = pkgs, listed or [], []

    async def shipment_packages(self, page=0, **kw):
        return {"content": self.pkgs if page == 0 else [], "totalPages": 1}

    async def product_query(self, page=0, size=250, **kw):
        return {"content": self.listed if page == 0 else [], "last": True, "totalElements": len(self.listed)}

    async def price_stock_update(self, skus):
        self.sent.extend(skus)
        return {"id": 1, "status": "IN_QUEUE"}


def _pkg(status="Created"):
    return {"id": 9001, "orderNumber": "205000111", "shipmentPackageStatus": status,
            "customerfullName": "Ayse Deneme", "customerEmail": "ayse@example.com",
            "shippingAddress": {"fullName": "Ayse Deneme", "city": "İstanbul", "district": "Kadıköy",
                                "address": "Test sk 1", "gsm": "5550000000"},
            "billingAddress": {"fullName": "Ayse Deneme", "invoiceType": 1},
            "packageHistories": [{"createdDate": 1792000000000, "status": "Created"}],
            "cargoTrackingNumber": "CMP123",
            "lines": [{"orderLineId": 77, "stockCode": "BAR-1", "barcode": "BAR-1", "productName": "Gömlek",
                       "quantity": 2, "price": 500, "sellerDiscount": 50, "sellerCouponDiscount": 0,
                       "sellerInvoiceAmount": 950, "variantAttributes": [{"name": "Beden", "value": "M"}]}]}


def _seed(kv_db):
    kv_run(kv_db.products.insert_one({"id": "p-n11", "name": "Gömlek", "barcode": "BAR-1", "stock": 10,
                                      "variants": [{"id": "v1", "barcode": "BAR-1", "size": "M", "stock": 10}]}))


def test_import_then_cancel_restocks_once(kv_db):
    from routes.integrations_n11 import run_n11_order_import
    _seed(kv_db)
    out = kv_run(run_n11_order_import(days=2, client=FakeN11([_pkg()])))
    assert out["imported"] == 1
    o = kv_run(kv_db.orders.find_one({"platform": "n11", "order_number": "205000111"}))
    assert o["status"] == "confirmed" and o["payment_status"] == "paid" and o["total"] == 950
    assert o["items"][0]["matched"] is True and o["items"][0]["size"] == "M"
    kv_run(run_n11_order_import(days=2, client=FakeN11([_pkg()])))   # tekrar: çift düşüm yok
    assert kv_run(kv_db.orders.count_documents({"platform": "n11"})) == 1
    assert kv_run(kv_db.stock_movements.count_documents({"order_id": o["id"], "type": "order_imported"})) == 1
    out = kv_run(run_n11_order_import(days=2, client=FakeN11([_pkg("Cancelled")])))
    assert out["cancelled"] == 1
    assert kv_run(kv_db.orders.find_one({"id": o["id"]}))["status"] == "cancelled"
    out = kv_run(run_n11_order_import(days=2, client=FakeN11([_pkg("Cancelled")])))
    assert out["cancelled"] == 0


def test_cancelled_unknown_order_not_created(kv_db):
    from routes.integrations_n11 import run_n11_order_import
    kv_run(run_n11_order_import(days=2, client=FakeN11([_pkg("Cancelled")])))
    assert kv_run(kv_db.orders.count_documents({"platform": "n11"})) == 0


def test_stock_sync_only_matched_and_changed(kv_db):
    from routes.integrations_n11 import run_n11_stock_sync
    _seed(kv_db)
    fake = FakeN11([], listed=[{"stockCode": "BAR-1", "quantity": 3}, {"stockCode": "YOK-9", "quantity": 5}])
    out = kv_run(run_n11_stock_sync(client=fake))
    assert fake.sent == [{"stockCode": "BAR-1", "quantity": 10}]
    assert out["unmatched"] == 1


def test_endpoints_require_permission_and_config(kv_client, kv_users):
    st = kv_users.staff(perms=["orders.view"])
    assert kv_client.post("/api/integrations/n11/orders/import", headers={**kv_users.hdr(st), **kv_ip()}).status_code == 403
    sa = kv_users.staff(super_admin=True)
    r = kv_client.post("/api/integrations/n11/test-connection", headers={**kv_users.hdr(sa), **kv_ip()})
    assert r.status_code == 400 and "N11" in r.json()["detail"], r.text


def test_product_push_preview_missing_then_push(kv_client, kv_users, kv_db, monkeypatch):
    import routes.integrations_n11 as n11
    kv_run(kv_db.marketplace_accounts.insert_one({"key": "n11", "enabled": True,
        "credentials": {"api_key": "k", "api_secret": "s"},
        "transfer_rules": {"price_field": "price", "sale_price_field": "sale_price", "commission_type": "none"}}))
    kv_run(kv_db.products.insert_one({"id": "p-push", "name": "Keten Gömlek Uzun Kollu", "brand": "FACETTE",
        "price": 1000, "sale_price": 800, "vat_rate": 10, "description": "<p>Keten gömlek açıklaması uzun</p>",
        "images": ["https://cdn.example.com/a.jpg"],
        "variants": [{"barcode": "8680000000011", "size": "M", "color": "Beyaz", "stock": 4}]}))
    cat = {"id": 1001, "name": "Gömlek", "categoryAttributes": [
        {"attributeId": 1, "attributeName": "Marka", "isMandatory": True, "isCustomValue": True, "attributeValues": []},
        {"attributeId": 50, "attributeName": "Renk", "isMandatory": True, "isVariant": True,
         "attributeValues": [{"id": 501, "value": "Beyaz"}]},
        {"attributeId": 60, "attributeName": "Beden", "isMandatory": True, "attributeValues": [{"id": 601, "value": "M"}]},
        {"attributeId": 70, "attributeName": "Kumaş", "isMandatory": True, "isCustomValue": False,
         "attributeValues": [{"id": 701, "value": "Keten"}]}]}
    sent = {}

    class C:
        async def get_category_attributes(self, cid):
            return cat

        async def create_products(self, skus):
            sent["skus"] = skus
            return {"id": 9, "status": "IN_QUEUE"}

        async def task_details(self, tid):
            return {"skus": {"content": [{"itemCode": "8680000000011", "status": "SUCCESS", "reasons": []}]}}

    async def _cl(require_enabled=True):
        return C()
    monkeypatch.setattr(n11, "_get_n11_client", _cl)
    import asyncio
    async def _nosleep(*a, **k):
        return None
    monkeypatch.setattr(asyncio, "sleep", _nosleep)
    sa = kv_users.staff(super_admin=True)
    h = {**kv_users.hdr(sa), **kv_ip()}
    body = {"code": "8680000000011", "category_id": 1001, "shipment_template": "Standart"}
    r = kv_client.post("/api/integrations/n11/products/push", json=body, headers=h)
    js = r.json()
    assert js["ready"] is False and [m["name"] for m in js["missing"]] == ["Kumaş"]
    assert js["list_price"] == 1000 and js["sale_price"] == 800
    r = kv_client.post("/api/integrations/n11/products/push", json={**body, "attributes": {"70": "701"}}, headers=h)
    js = r.json()
    assert js["success"] is True, js
    a = {x["id"]: x for x in sent["skus"][0]["attributes"]}
    assert a[1]["customValue"] == "FACETTE" and a[50]["valueId"] == 501 and a[60]["valueId"] == 601 and a[70]["valueId"] == 701
    assert sent["skus"][0]["stockCode"] == "8680000000011" and sent["skus"][0]["quantity"] == 4
