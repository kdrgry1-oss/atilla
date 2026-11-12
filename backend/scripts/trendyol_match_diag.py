"""Kurulumda çalışır: panelden son yüklenen Trendyol Excel'i ile KAYDETMEDEN (dry-run) eşleştirme yapar ve
özet + eşleşmeyen varyant örnekleri + fiyat örneklerini yazdırır. Barkod/fiyat dışında kişisel veri içermez."""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))


async def main() -> int:
    import trendyol_excel_match as tm
    path = tm.last_upload_path()
    if not path.exists():
        print("Trendyol Excel'i henüz panelden yüklenmedi — tanı atlandı.")
        return 0
    from motor.motor_asyncio import AsyncIOMotorClient
    db = AsyncIOMotorClient(os.environ["MONGO_URL"])[os.environ["DB_NAME"]]
    r = await tm.run_match(db, path.read_bytes(), dry_run=True)
    print("Trendyol eşleştirme ön izleme:", json.dumps(r["stats"], ensure_ascii=False))
    print("Fiyat oranı (TY/site) medyan:", r.get("price_ratio_median"))
    print("Fiyat örnekleri:")
    for x in r.get("price_sample", [])[:20]:
        print(f"   {x['name'][:50]:50} | {str(x['size'] or '-'):8} | site {x['site_price']:>9} | TY {x['trendyol_price']:>9} | {x['matched_by']}")
    print("Eşleşmeyen sistem varyantları (örnek):")
    for x in r.get("unmatched_variant_sample", [])[:60]:
        print(f"   {str(x['name'])[:45]:45} | kod {str(x['stock_code']):16} | beden {str(x['size'] or '-'):10} "
              f"| var {x['variant_count']} | TY bedenleri {','.join(x['trendyol_sizes_for_code']) or 'YOK'}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
