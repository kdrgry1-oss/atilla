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
    from motor.motor_asyncio import AsyncIOMotorClient
    import ticimax_xml_import as tx
    db = AsyncIOMotorClient(os.environ["MONGO_URL"])[os.environ["DB_NAME"]]
    # Panelden seçilmiş kalıcı kaynak (settings.ticimax_xml_source) .env'deki adresin önüne geçer.
    src = {} if args else await tx.get_source(db)
    url = (args[0] if args else src.get("url") or os.environ.get("PRODUCT_XML_URL") or "").strip()
    if not url:
        print("PRODUCT_XML_URL tanımlı değil — ürün aktarımı atlandı.")
        return 0
    stats = await tx.run_import(db, url, dry_run="--dry-run" in sys.argv, describe="--describe" in sys.argv,
                                include_no_stock=bool(src.get("include_no_stock")) or "--all" in sys.argv)
    st = dict(stats)
    struct = st.pop("structure", None)
    samples = st.pop("feed_samples", None)
    print("Ürün aktarımı:", json.dumps(st, ensure_ascii=False))
    if struct:
        print("XML yapısı (ürün bulunamadı):")
        for k, v in struct.items():
            if k != "paths":
                print(f"  {k}: {v}")
        for line in struct.get("paths", []):
            print("   ", line)
    if samples:
        print(f"Feed: {samples.get('items')} kayıt, {samples.get('groups')} ürün grubu")
        for i, row in enumerate(samples.get("samples", []), 1):
            print(f"  Örnek kayıt {i}:")
            for line in row:
                print("    ", line)
        for k, vals in (samples.get("distinct") or {}).items():
            print(f"  {k}: " + " ; ".join(f"{v!r} x{c}" for v, c in vals))
    return 0 if stats.get("errors", 0) == 0 or stats.get("created", 0) + stats.get("updated", 0) > 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
