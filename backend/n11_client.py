"""
n11 Satıcı API istemcisi (REST: kategori / ürün / fiyat-stok / sipariş paketleri).

Kaynak: developer.n11.com (REST). Kimlik: her istekte `appkey` + `appsecret` başlıkları.
Sandbox YOK — tek adres https://api.n11.com. Ürün yazımları asenkron "task" döner
(task-details ile sonuç sorgulanır). Sipariş listesi tarih penceresini sessizce 15 güne kırpar.
n11 hızlı ardışık isteklerde IP'yi geçici askıya alıyor → istekler arası küçük bekleme var.
"""
import asyncio
import logging
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

N11_BASE_URL = "https://api.n11.com"
_MIN_GAP_S = 0.35  # ardışık istekler arası (IP askıya alma koruması)


class N11Error(Exception):
    def __init__(self, status: int, message: str, body: Any = None):
        super().__init__(message)
        self.status = status
        self.body = body


class N11Client:
    def __init__(self, app_key: str, app_secret: str, integrator: str = "", base_url: str = N11_BASE_URL,
                 timeout: float = 30.0):
        self.app_key = (app_key or "").strip()
        self.app_secret = (app_secret or "").strip()
        self.integrator = (integrator or "").strip()
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._lock = asyncio.Lock()
        self._last = 0.0

    def _headers(self) -> Dict[str, str]:
        return {"appkey": self.app_key, "appsecret": self.app_secret,
                "Content-Type": "application/json", "Accept": "application/json"}

    async def _request(self, method: str, path: str, *, params: Optional[Dict] = None,
                       json_body: Any = None) -> Any:
        async with self._lock:
            loop = asyncio.get_event_loop()
            wait = _MIN_GAP_S - (loop.time() - self._last)
            if wait > 0:
                await asyncio.sleep(wait)
            async with httpx.AsyncClient(timeout=self.timeout) as cl:
                r = await cl.request(method, f"{self.base_url}{path}", headers=self._headers(),
                                     params=params, json=json_body)
            self._last = loop.time()
        try:
            body = r.json()
        except Exception:
            body = r.text
        if r.status_code >= 400:
            msg = ""
            if isinstance(body, dict):
                msg = (body.get("errorMessage") or body.get("message") or body.get("errorCode")
                       or "; ".join(str((e or {}).get("reason") or e) for e in (body.get("errors") or [])))
            raise N11Error(r.status_code, str(msg or body)[:500] or f"HTTP {r.status_code}", body)
        return body

    # ---- Kategori ----
    async def get_categories(self) -> List[Dict]:
        body = await self._request("GET", "/cdn/categories")
        if not isinstance(body, dict):
            return []
        return body.get("categories") or []

    async def get_category_attributes(self, category_id: int) -> Dict:
        return await self._request("GET", f"/cdn/category/{int(category_id)}/attribute")

    # ---- Ürün ----
    async def product_query(self, *, page: int = 0, size: int = 250, stock_code: str = None,
                            product_main_id: str = None, status: str = None) -> Dict:
        params: Dict[str, Any] = {"page": page, "size": max(1, min(int(size), 250))}
        if stock_code:
            params["stockCode"] = stock_code
        if product_main_id:
            params["productMainId"] = product_main_id
        if status:
            params["productStatus"] = status
        return await self._request("GET", "/ms/product-query", params=params)

    async def _task(self, kind: str, skus: List[Dict]) -> Dict:
        # ÜYELERE ÖZEL (erken erişim) ürünler n11'e gönderilmez
        from marketplace_block import refresh_blocked, filter_items, skipped_response
        await refresh_blocked()
        skus, _mo_skip = filter_items(list(skus or []), f"n11.{kind}")
        if not skus:
            return skipped_response(_mo_skip) if _mo_skip else {}
        if len(skus) > 1000:
            raise ValueError("n11 tek görevde en çok 1000 SKU kabul eder")
        return await self._request("POST", f"/ms/product/tasks/{kind}",
                                   json_body={"payload": {"integrator": self.integrator, "skus": skus}})

    async def create_products(self, skus: List[Dict]) -> Dict:
        return await self._task("product-create", skus)

    async def update_products(self, skus: List[Dict]) -> Dict:
        return await self._task("product-update", skus)

    async def price_stock_update(self, skus: List[Dict]) -> Dict:
        return await self._task("price-stock-update", skus)

    async def task_details(self, task_id, page: int = 0, size: int = 1000) -> Dict:
        return await self._request("POST", "/ms/product/task-details/page-query",
                                   json_body={"taskId": task_id, "pageable": {"page": page, "size": size}})

    # ---- Sipariş ----
    async def shipment_packages(self, *, start_ms: int = None, end_ms: int = None, page: int = 0,
                                size: int = 100, status: str = None, order_number: str = None,
                                by_last_modified: bool = True) -> Dict:
        params: Dict[str, Any] = {"page": page, "size": max(1, min(int(size), 100)),
                                  "orderByDirection": "ASC"}
        if start_ms:
            params["startDate"] = int(start_ms)
        if end_ms:
            params["endDate"] = int(end_ms)
        if status:
            params["status"] = status
        if order_number:
            params["orderNumber"] = order_number
        if by_last_modified:
            params["orderByField"] = "true"
        return await self._request("GET", "/rest/delivery/v1/shipmentPackages", params=params)

    async def approve_lines(self, line_ids: List) -> Dict:
        return await self._request("PUT", "/rest/order/v1/update", json_body={
            "lines": [{"lineId": int(x)} for x in line_ids], "status": "Picking"})
