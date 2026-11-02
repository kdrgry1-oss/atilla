"""Cloudflare R2 (S3-compatible) object storage helper.

Görseller artık MongoDB base64 yerine R2'ye yüklenir ve doğrudan R2 public
CDN URL'i üzerinden sunulur. Bu hem DB yükünü azaltır hem de yüklemeyi hızlandırır.
"""
import os
import logging

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger("r2")

_client = None


def is_enabled() -> bool:
    """R2 yapılandırılmış mı?

    R2_PUBLIC_URL ŞART: yoksa public_url() relatif/bozuk bir URL üretir
    (ör. "/uploads/x.jpg") ve görsel hiçbir yerde açılmaz. Eksikse R2'yi KAPALI
    say → güvenilir DB/disk fallback'ine düş (görsel her zaman servis edilir).
    """
    return bool(
        os.environ.get("R2_ENDPOINT")
        and os.environ.get("R2_ACCESS_KEY_ID")
        and os.environ.get("R2_SECRET_ACCESS_KEY")
        and os.environ.get("R2_BUCKET")
        and (os.environ.get("R2_PUBLIC_URL") or "").strip().lower().startswith("http")
    )


def _get_client():
    global _client
    if _client is None:
        _client = boto3.client(
            "s3",
            endpoint_url=os.environ["R2_ENDPOINT"],
            aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
            aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
            region_name="auto",
            # boto3 ≥1.36 varsayılan olarak her isteğe CRC32 sağlama başlığı ekler; Cloudflare R2 bazı
            # isteklerde bunu reddedebiliyor (Cloudflare önerisi: yalnız gerektiğinde).
            config=Config(signature_version="s3v4", retries={"max_attempts": 3, "mode": "standard"},
                          request_checksum_calculation="when_required",
                          response_checksum_validation="when_required"),
        )
    return _client


def public_url(key: str) -> str:
    """Bir nesne anahtarı (key) için public CDN URL'i döndürür."""
    base = os.environ.get("R2_PUBLIC_URL", "").rstrip("/")
    return f"{base}/{key.lstrip('/')}"


def put_object(key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
    """Veriyi R2'ye yükler, public URL döndürür."""
    client = _get_client()
    client.put_object(
        Bucket=os.environ["R2_BUCKET"],
        Key=key.lstrip("/"),
        Body=data,
        ContentType=content_type or "application/octet-stream",
        CacheControl="public, max-age=31536000, immutable",
    )
    return public_url(key)


def get_object(key: str):
    """R2'den nesneyi indirir → (bytes, content_type). Yoksa (None, None)."""
    client = _get_client()
    try:
        resp = client.get_object(Bucket=os.environ["R2_BUCKET"], Key=key.lstrip("/"))
        LAST_GET_ERROR["code"] = ""
        return resp["Body"].read(), resp.get("ContentType", "application/octet-stream")
    except ClientError as e:
        LAST_GET_ERROR["code"] = _safe_code(((e.response or {}).get("Error") or {}).get("Code") or "ClientError")
        return None, None
    except BotoCoreError as e:
        LAST_GET_ERROR["code"] = _safe_code(type(e).__name__)
        return None, None


# Son okuma hatasının KODU (ör. AccessDenied, NoSuchKey, NoSuchBucket) — teşhis için; sır içermez.
LAST_GET_ERROR = {"code": ""}


def _safe_code(c) -> str:
    import re as _re
    return (_re.sub(r"[^A-Za-z0-9]", "", str(c or "")) or "Error")[:40]


def health_check() -> dict:
    """R2 bağlantısını doğrular (head_bucket)."""
    try:
        _get_client().head_bucket(Bucket=os.environ["R2_BUCKET"])
        return {"ok": True, "bucket": os.environ["R2_BUCKET"]}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


def explain_error(e: Exception) -> str:
    """R2 hatasını panelde anlaşılır Türkçe nedene çevirir (sır değeri İÇERMEZ)."""
    code = ""
    try:
        code = (getattr(e, "response", None) or {}).get("Error", {}).get("Code", "") or ""
    except Exception:
        code = ""
    txt = f"{code} {e}".lower()
    if "invalidaccesskeyid" in txt or "signaturedoesnotmatch" in txt or "unauthorized" in txt:
        return f"R2 anahtarı geçersiz ({code or 'imza'}): Railway'deki R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY eşleşmiyor veya silinmiş."
    if "accessdenied" in txt or "forbidden" in txt or "403" in txt:
        return f"R2 anahtarının bu depoya YAZMA izni yok ({code or '403'}): Cloudflare'de anahtara 'Object Read & Write' + doğru bucket izni verilmeli."
    if "nosuchbucket" in txt or "404" in txt:
        return f"R2 deposu bulunamadı ({code or '404'}): Railway'deki R2_BUCKET adı Cloudflare'deki depo adıyla aynı olmalı."
    if "checksum" in txt or "notimplemented" in txt or "invalidargument" in txt:
        return f"R2 isteği reddetti ({code}): istemci/sağlama ayarı uyumsuz."
    if "could not connect" in txt or "endpoint" in txt or "timed out" in txt:
        return "R2 adresine bağlanılamadı: Railway'deki R2_ENDPOINT değerini kontrol edin."
    return f"R2 hatası: {code or type(e).__name__}"


# ---------------------------------------------------------------------------
# GİZLİ ARŞİV DEPOSU (R2_ARCHIVE_BUCKET) — dekont / fatura gibi kişisel/finansal belgeler.
# Herkese açık CDN deposundan AYRI, public erişimi KAPALI bir bucket olmalıdır; dosyalar yalnız
# backend üzerinden (yetkili panel isteğiyle) okunur. Tanımlı değilse arşivleme hiç çalışmaz.
# ---------------------------------------------------------------------------
def archive_bucket() -> str:
    return (os.environ.get("R2_ARCHIVE_BUCKET") or "").strip()


def archive_enabled() -> bool:
    b = archive_bucket()
    return bool(b and os.environ.get("R2_ENDPOINT") and os.environ.get("R2_ACCESS_KEY_ID")
                and os.environ.get("R2_SECRET_ACCESS_KEY") and b != (os.environ.get("R2_BUCKET") or "").strip())


def archive_put(key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
    _get_client().put_object(Bucket=archive_bucket(), Key=key.lstrip("/"), Body=data,
                             ContentType=content_type or "application/octet-stream")


def archive_get(key: str):
    """Arşivden (bytes, content_type); yoksa (None, None)."""
    try:
        resp = _get_client().get_object(Bucket=archive_bucket(), Key=key.lstrip("/"))
        return resp["Body"].read(), resp.get("ContentType", "application/octet-stream")
    except (BotoCoreError, ClientError):
        return None, None


def archive_health() -> dict:
    try:
        _get_client().head_bucket(Bucket=archive_bucket())
        return {"ok": True, "bucket": archive_bucket()}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:200]}
