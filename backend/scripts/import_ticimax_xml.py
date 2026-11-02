"""Ticimax XMLExport feed'ini içe aktarır (sunucuda, otomatik kurulum ve zamanlanmış görevden çağrılır).

Kullanım:  python scripts/import_ticimax_xml.py [URL] [--dry-run]
URL verilmezse .env içindeki PRODUCT_XML_URL kullanılır. URL log'a yazılmaz (erişim anahtarı içerir).
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))


async def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    url = (args[0] if args else os.environ.get("PRODUCT_XML_URL") or "").strip()
    if not url:
        print("PRODUCT_XML_URL tanımlı değil — ürün aktarımı atlandı.")
        return 0
    from motor.motor_asyncio import AsyncIOMotorClient
    import ticimax_xml_import as tx
    db = AsyncIOMotorClient(os.environ["MONGO_URL"])[os.environ["DB_NAME"]]
    stats = await tx.run_import(db, url, dry_run="--dry-run" in sys.argv)
    print("Ürün aktarımı:", json.dumps(stats, ensure_ascii=False))
    return 0 if stats.get("errors", 0) == 0 or stats.get("created", 0) + stats.get("updated", 0) > 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
