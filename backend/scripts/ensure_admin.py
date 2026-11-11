"""Her kurulumda çalışır: GitHub Secrets'taki ADMIN_EMAIL / ADMIN_PASSWORD ile yönetici hesabını garanti eder.

- Bu e-postayla hesap yoksa → süper yönetici olarak oluşturulur (ilk girişte şifre değişikliği istenir).
- Hesap varsa ve GitHub'daki ADMIN_PASSWORD SON UYGULANANDAN FARKLIYSA → şifre ona sıfırlanır
  (tüm açık oturumlar kapanır, kilit kaldırılır). Panelden değiştirdiğiniz şifreye, secret aynı
  kaldığı sürece DOKUNULMAZ.
Şifre ya da özeti log'a yazılmaz.
"""
import asyncio
import re
import os
import pathlib
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

APPLIED = pathlib.Path(os.environ.get("ADMIN_APPLIED_FILE", "/opt/atilla/.admin_secret_applied"))
MIN_LEN = 10


async def main() -> int:
    import bcrypt
    import uuid
    from motor.motor_asyncio import AsyncIOMotorClient
    email = (os.environ.get("ADMIN_INITIAL_EMAIL") or "").strip().lower()
    pw = (os.environ.get("ADMIN_INITIAL_PASSWORD") or "").strip()
    if not email or not pw:
        print("Yönetici: ADMIN_EMAIL / ADMIN_PASSWORD secret'ı yok — atlandı.")
        return 0
    if len(pw) < MIN_LEN:
        print(f"!! Yönetici: ADMIN_PASSWORD en az {MIN_LEN} karakter olmalı — hesap oluşturulmadı/sıfırlanmadı.")
        return 0
    db = AsyncIOMotorClient(os.environ["MONGO_URL"])[os.environ["DB_NAME"]]
    now = datetime.now(timezone.utc).isoformat()
    hashed = bcrypt.hashpw(pw.encode(), bcrypt.gensalt(rounds=12)).decode()
    user = await db.users.find_one({"email": email}, {"_id": 0, "id": 1})
    applied = APPLIED.read_text().strip() if APPLIED.exists() else ""
    secret_changed = not (applied and bcrypt.checkpw(pw.encode(), applied.encode()))
    if user is None:
        await db.users.insert_one({
            "id": str(uuid.uuid4()), "email": email, "password": hashed,
            "first_name": "Yönetici", "last_name": "", "is_admin": True, "is_super_admin": True,
            "is_active": True, "must_change_password": True, "created_at": now,
        })
        print("✔ Yönetici hesabı oluşturuldu (giriş: ADMIN_EMAIL / ADMIN_PASSWORD).")
    elif secret_changed:
        await db.users.update_one({"email": email}, {
            "$set": {"password": hashed, "password_changed_at": now, "password_updated_at": now,
                     "must_change_password": True, "is_active": True, "is_admin": True, "is_super_admin": True},
            "$unset": {"locked_until": ""}, "$inc": {"token_version": 1}})
        await db.login_ip_failures.delete_many({"key": {"$regex": "^" + re.escape(email)}})
        print("✔ Yönetici şifresi GitHub'daki ADMIN_PASSWORD değerine sıfırlandı.")
    else:
        print("Yönetici hesabı mevcut (şifre değişmedi).")
    if user is None or secret_changed:
        APPLIED.write_text(bcrypt.hashpw(pw.encode(), bcrypt.gensalt(rounds=12)).decode())
        os.chmod(APPLIED, 0o600)
        try:
            await db.auth_audit_logs.insert_one({"id": str(uuid.uuid4()), "event": "admin_ensure_on_deploy",
                                                 "email": email, "success": True, "created_at": now,
                                                 "meta": {"created": user is None}})
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
