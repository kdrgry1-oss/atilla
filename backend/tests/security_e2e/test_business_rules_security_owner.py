"""Güvenlik kuralları (security.*): açık süper admin bayrağı OLMAYAN ama tam yetkili ('*') rol
sahibi ya da panel sahibi kaydedebilir; sıradan personel kaydedemez."""
from conftest import kv_ip, kv_run


def test_full_role_admin_can_set_alert_emails(kv_client, kv_users, kv_db):
    kv_run(kv_db.roles.insert_one({"id": "r-full", "name": "Süper Admin", "permissions": ["*"]}))
    u = kv_users.staff(perms=["settings.edit", "settings.view"])
    kv_run(kv_db.users.update_one({"id": u["id"]}, {"$set": {"role_id": "r-full", "is_super_admin": False}}))
    r = kv_client.put("/api/admin/business-rules", json={"values": {"security.alert_emails": "a@example.com"}},
                      headers={**kv_users.hdr({**u, "role_id": "r-full", "is_super_admin": False}), **kv_ip()})
    assert r.status_code == 200, r.text
    st = kv_users.staff(perms=["settings.edit", "settings.view"])
    r = kv_client.put("/api/admin/business-rules", json={"values": {"security.alert_emails": "b@example.com"}},
                      headers={**kv_users.hdr(st), **kv_ip()})
    assert r.status_code == 403
