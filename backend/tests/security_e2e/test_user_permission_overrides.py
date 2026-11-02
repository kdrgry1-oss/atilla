"""Kişiye özel yetki (e-posta/kullanıcı başına): rol + perm_grant − perm_deny.
Yalnız süper admin belirler; '*' ve katalog dışı anahtar reddedilir; kendi yetkisini değiştiremez."""
from conftest import kv_ip


def _me(client, users, u):
    r = client.get("/api/admin/me/permissions", headers={**users.hdr(u), **kv_ip()})
    assert r.status_code == 200, r.text
    return r.json()


def test_grant_and_deny_change_effective_permissions(kv_client, kv_users):
    sa = kv_users.staff(super_admin=True)
    st = kv_users.staff(perms=["orders.view", "products.view"])
    r = kv_client.put(f"/api/admin/users/{st['id']}",
                      json={"perm_grant": ["reports.sales"], "perm_deny": ["products.view"]},
                      headers={**kv_users.hdr(sa), **kv_ip()})
    assert r.status_code == 200, r.text
    me = _me(kv_client, kv_users, st)
    assert "reports.sales" in me["permissions"] and "orders.view" in me["permissions"]
    assert "products.view" not in me["permissions"]
    # Eklenen yetki gerçek uçta geçerli, çıkarılan yetki reddedilir.
    assert kv_client.get("/api/admin/report-ledger/reconciliation",
                         headers={**kv_users.hdr(st), **kv_ip()}).status_code == 200
    # Personel dizini kişiye özel alanları gösterir (panel düzenleme ekranı için).
    lst = kv_client.get("/api/admin/users", headers={**kv_users.hdr(sa), **kv_ip()}).json()["users"]
    row = next(x for x in lst if x["id"] == st["id"])
    assert row["perm_grant"] == ["reports.sales"] and row["perm_deny"] == ["products.view"]


def test_star_unknown_key_and_non_super_rejected(kv_client, kv_users):
    sa = kv_users.staff(super_admin=True)
    st = kv_users.staff(perms=["orders.view"])
    h = {**kv_users.hdr(sa), **kv_ip()}
    assert kv_client.put(f"/api/admin/users/{st['id']}", json={"perm_grant": ["*"]}, headers=h).status_code == 400
    assert kv_client.put(f"/api/admin/users/{st['id']}", json={"perm_grant": ["yok.boyle"]}, headers=h).status_code == 400
    # Süper admin olmayan personel (rol yönetimi yetkisi olsa bile) kişiye özel yetki veremez.
    mgr = kv_users.staff(perms=["settings.view", "settings.edit", "orders.view"])
    r = kv_client.put(f"/api/admin/users/{st['id']}", json={"perm_grant": ["reports.sales"]},
                      headers={**kv_users.hdr(mgr), **kv_ip()})
    assert r.status_code == 403
    assert "reports.sales" not in _me(kv_client, kv_users, st)["permissions"]


def test_cannot_change_own_overrides_and_super_admin_only_deny(kv_client, kv_users, kv_db):
    sa = kv_users.staff(super_admin=True)
    sa2 = kv_users.staff(super_admin=True)
    h = {**kv_users.hdr(sa), **kv_ip()}
    assert kv_client.put(f"/api/admin/users/{sa['id']}", json={"perm_deny": ["orders.view"]}, headers=h).status_code == 400
    # Süper admine yetki EKLENMEZ, yalnız kaldırılır.
    assert kv_client.put(f"/api/admin/users/{sa2['id']}", json={"perm_grant": ["orders.view"]}, headers=h).status_code == 400
    r = kv_client.put(f"/api/admin/users/{sa2['id']}", json={"perm_deny": ["campaigns.influencers_delete"]}, headers=h)
    assert r.status_code == 200, r.text
    me = _me(kv_client, kv_users, {**sa2, "perm_deny": ["campaigns.influencers_delete"]})
    assert me["permissions"] == ["*"] and "denied" not in me   # kişi kendi kısıtını görmez
    from conftest import kv_run
    kv_run(kv_db.influencers.insert_one({"id": "inf-1", "name": "Test"}))
    h2 = {**kv_users.hdr(sa2), **kv_ip()}
    assert kv_client.delete("/api/influencers/inf-1", headers=h2).status_code == 403
    assert kv_client.delete("/api/influencers/inf-1", headers=h).status_code == 200


def test_influencer_delete_needs_own_permission_and_one_time_grant(kv_client, kv_users, kv_db):
    from conftest import kv_run
    from routes.admin_rbac import grant_influencer_delete_once
    st = kv_users.staff(perms=["campaigns.influencers"])
    kv_run(kv_db.influencers.insert_one({"id": "inf-2", "name": "Test"}))
    h = {**kv_users.hdr(st), **kv_ip()}
    assert kv_client.delete("/api/influencers/inf-2", headers=h).status_code == 403
    assert kv_run(grant_influencer_delete_once())["modified"] >= 1
    assert kv_run(grant_influencer_delete_once())["skipped"] is True   # bir kez
    assert kv_client.delete("/api/influencers/inf-2", headers={**kv_users.hdr(st), **kv_ip()}).status_code == 200


def test_deny_all_role_permissions_blocks_panel(kv_client, kv_users):
    sa = kv_users.staff(super_admin=True)
    st = kv_users.staff(perms=["orders.view"])
    assert kv_client.put(f"/api/admin/users/{st['id']}", json={"perm_deny": ["orders.view"]},
                         headers={**kv_users.hdr(sa), **kv_ip()}).status_code == 200
    r = kv_client.get("/api/admin/me/permissions", headers={**kv_users.hdr(st), **kv_ip()})
    assert r.status_code == 403


def test_panel_owner_only_can_manage_overrides_and_other_super_admins(kv_client, kv_users):
    from conftest import kv_run
    from routes.admin_rbac import set_panel_owner_once
    owner = kv_users.staff(super_admin=True, email="sahip@example.com")
    asu = kv_users.staff(super_admin=True)
    st = kv_users.staff(perms=["orders.view", "campaigns.influencers"])
    assert kv_run(set_panel_owner_once("sahip@example.com"))["skipped"] is False
    assert kv_run(set_panel_owner_once("baska@example.com"))["skipped"] is True   # sahip bir kez
    ha = {**kv_users.hdr(asu), **kv_ip()}
    ho = {**kv_users.hdr(owner), **kv_ip()}
    # Süper admin (sahip değil): kişiye özel yetki DEĞİŞTİREMEZ — kendisininkini de, başkasınınkini de.
    assert kv_client.put(f"/api/admin/users/{st['id']}", json={"perm_deny": ["orders.view"]}, headers=ha).status_code == 403
    # Sahip asu'dan influencer silmeyi kaldırır; asu bunu geri açamaz, sahibin hesabına dokunamaz.
    assert kv_client.put(f"/api/admin/users/{asu['id']}", json={"perm_deny": ["campaigns.influencers_delete"]},
                         headers=ho).status_code == 200
    assert kv_client.put(f"/api/admin/users/{owner['id']}", json={"is_active": False}, headers=ha).status_code == 403
    assert kv_client.put(f"/api/admin/users/{owner['id']}", json={"perm_deny": ["orders.view"]}, headers=ha).status_code == 403
    assert kv_client.delete(f"/api/admin/users/{owner['id']}", headers=ha).status_code == 403
    # Panel sahibi kimliği me/permissions ve personel dizininde görünür.
    me = kv_client.get("/api/admin/me/permissions", headers=ho).json()
    assert me["is_owner"] is True and me["role"] == "Panel Sahibi"
    me_a = kv_client.get("/api/admin/me/permissions", headers={**kv_users.hdr(asu), **kv_ip()}).json()
    assert me_a["is_owner"] is False and "denied" not in me_a
    # Sahip olmayan admin personel dizininde kişiye özel farkları GÖRMEZ; sahip görür.
    rows_a = kv_client.get("/api/admin/users", headers={**kv_users.hdr(asu), **kv_ip()}).json()["users"]
    assert all("perm_deny" not in u and "perm_grant" not in u for u in rows_a)
    rows_o = kv_client.get("/api/admin/users", headers=ho).json()["users"]
    assert next(u for u in rows_o if u["id"] == asu["id"])["perm_deny"] == ["campaigns.influencers_delete"]
    lst = kv_client.get("/api/admin/users", headers=ho).json()
    assert lst["owner_defined"] is True
    assert [u["email"] for u in lst["users"] if u["is_owner"]] == ["sahip@example.com"]
    # Sıradan personeldeki normal düzenleme (ad) süper admin için açık kalır.
    assert kv_client.put(f"/api/admin/users/{st['id']}", json={"first_name": "Yeni"}, headers=ha).status_code == 200


def test_panel_owner_deletes_super_admin_without_second_approval(kv_client, kv_users, kv_db):
    from conftest import kv_run
    from routes.admin_rbac import set_panel_owner_once
    owner = kv_users.staff(super_admin=True, email="sahip2@example.com")
    sa2 = kv_users.staff(super_admin=True)
    sa3 = kv_users.staff(super_admin=True)
    kv_run(set_panel_owner_once("sahip2@example.com"))
    # Sahip olmayan süper admin başka süper admini silemez.
    assert kv_client.delete(f"/api/admin/users/{sa3['id']}", headers={**kv_users.hdr(sa2), **kv_ip()}).status_code == 403
    # Sahip: ≥2 aktif süper admin olsa da onay beklemeden siler.
    r = kv_client.delete(f"/api/admin/users/{sa3['id']}", headers={**kv_users.hdr(owner), **kv_ip()})
    assert r.status_code == 200 and r.json().get("success") is True, r.text
    assert kv_run(kv_db.users.find_one({"id": sa3["id"]})) is None
