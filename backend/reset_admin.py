import asyncio
from motor.motor_asyncio import AsyncIOMotorClient
import bcrypt
import os
import pathlib

# Auto-detect MongoDB connection (same logic as deps.py)
MONGO_URL = os.environ.get('MONGO_URL')
if not MONGO_URL:
    sock = pathlib.Path('/tmp/mongodb-27017.sock')
    if sock.exists():
        MONGO_URL = 'mongodb://%2Ftmp%2Fmongodb-27017.sock'
    else:
        MONGO_URL = 'mongodb://127.0.0.1:27017'

db_name = os.environ.get('DB_NAME', 'test_database')

async def reset():
    client = AsyncIOMotorClient(MONGO_URL)
    db = client[db_name]
    
    # GÜVENLİK: sabit "admin123" varsayılanı kaldırıldı. ADMIN_RESET_PASSWORD env
    # zorunlu; verilmezse betik hiçbir şey yapmadan durur (zayıf parola dayatılmaz).
    new_password = os.environ.get("ADMIN_RESET_PASSWORD", "").strip()
    if len(new_password) < 10:
        print("HATA: ADMIN_RESET_PASSWORD ortam değişkeni (>=10 karakter) gerekli. İşlem iptal.")
        return
    hashed = bcrypt.hashpw(new_password.encode(), bcrypt.gensalt()).decode()
    
    from datetime import datetime as _dt, timezone as _tz
    import uuid as _uuid
    _now_iso = _dt.now(_tz.utc).isoformat()
    # GÜVENLİK (denetim 2026-10, #5): parola sıfırlanınca token_version artırılır → hesabın
    # TÜM açık oturumları (çalınmış olabilecek token'lar dâhil) geçersiz olur.
    result = await db.users.update_one(
        {"email": "admin@facette.com"},
        {"$set": {"password": hashed, "password_changed_at": _now_iso,
                  "password_updated_at": _now_iso},
         "$inc": {"token_version": 1}},
        upsert=False
    )
    # Denetim izi (auth_audit_logs — deps.write_audit_log ile aynı şema). Best-effort.
    try:
        _u = await db.users.find_one({"email": "admin@facette.com"}, {"_id": 0, "id": 1})
        await db.auth_audit_logs.insert_one({
            "id": str(_uuid.uuid4()),
            "event": "admin_password_reset_script",
            "user_id": (_u or {}).get("id"),
            "email": "admin@facette.com",
            "ip": None,
            "user_agent": "reset_admin.py",
            "success": True,
            "meta": {"created": result.matched_count == 0,
                     "host": (os.environ.get("HOSTNAME") or "")[:64]},
            "created_at": _now_iso,
        })
    except Exception as _ae:
        print(f"UYARI: denetim kaydı yazılamadı ({type(_ae).__name__})")
    
    if result.matched_count == 0:
        # Admin user doesn't exist, create it
        import uuid
        from datetime import datetime, timezone
        await db.users.insert_one({
            "id": str(uuid.uuid4()),
            "email": "admin@facette.com",
            "password": hashed,
            "first_name": "Admin",
            "last_name": "User",
            "is_admin": True,
            "is_active": True,
            "created_at": datetime.now(timezone.utc).isoformat()
        })
        print("Admin user CREATED: admin@facette.com (parola ADMIN_RESET_PASSWORD ile ayarlandı)")
    else:
        print(f"Admin password RESET. Matched: {result.matched_count}, Modified: {result.modified_count}")
        print("Login: admin@facette.com (parola ADMIN_RESET_PASSWORD ile ayarlandı)")

if __name__ == "__main__":
    asyncio.run(reset())
