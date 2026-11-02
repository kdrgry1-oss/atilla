"""iOS push — doğrudan APNs gönderimi (routes.push).

Capacitor iOS'ta HAM APNs token'ı verir; FCM v1 bunu kabul etmez. Bu testler:
  • APNs isteğinin biçimi (ES256 JWT + kid, apns-topic, payload) ve yanıt işleme (200 / 410 / 400),
  • .p8 anahtarının farklı biçimlerde (PEM, \\n kaçışlı tek satır, başlıksız base64) kabulü,
  • send_push_to_admins'in token'ları platforma göre ayırması (iOS → APNs, Android → FCM v1) ve
    müşteri cihazlarına yönetici bildirimi GİTMEMESİ,
  • bildirim yapılandırması yoksa gönderimin sessizce atlanması.
Ağa hiçbir şey çıkmaz: APNs çağrıları respx ile yakalanır.
"""
from __future__ import annotations


import jwt
import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from httpx import Response

import routes.push as push_mod
from routes import deps

REAL_SEND = push_mod.send_push_to_admins


def _p8() -> str:
    key = ec.generate_private_key(ec.SECP256R1())
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode()


@pytest.fixture
def apns_cfg():
    pem = _p8()
    return {"key_id": "KEY1234567", "team_id": "TEAM123456", "key": push_mod._normalize_p8(pem),
            "topic": "tr.com.atilla.admin", "env": "production"}


def test_normalize_p8_accepts_pem_escaped_and_bare_base64():
    pem = _p8().strip()
    body = "".join(l for l in pem.splitlines() if "-----" not in l)
    for raw in (pem, pem.replace("\n", "\\n"), body):
        out = push_mod._normalize_p8(raw)
        assert out.startswith("-----BEGIN PRIVATE KEY-----") and out.endswith("-----END PRIVATE KEY-----")
        # anahtar gerçekten okunabilir (ES256 imzalanabilir)
        jwt.encode({"iss": "T"}, out, algorithm="ES256", headers={"kid": "K"})
    assert push_mod._normalize_p8("") == ""


@pytest.mark.asyncio
@respx.mock
async def test_apns_send_request_shape_and_outcomes(apns_cfg):
    ok = respx.post("https://api.push.apple.com/3/device/tok-ok").mock(return_value=Response(200))
    respx.post("https://api.push.apple.com/3/device/tok-gone").mock(
        return_value=Response(410, json={"reason": "Unregistered"}))
    respx.post("https://api.push.apple.com/3/device/tok-bad").mock(
        return_value=Response(400, json={"reason": "BadDeviceToken"}))
    sent, stale, errors = await push_mod._apns_send(
        apns_cfg, ["tok-ok", "tok-gone", "tok-bad"], "Başlık", "Metin", {"type": "new_order", "order_id": "o1"})
    assert sent == 1
    # yalnız 410 bayat sayılır; 400 BadDeviceToken (ortam uyuşmazlığı olabilir) pasife ALINMAZ
    assert stale == ["tok-gone"]
    assert any("410" in e and "Unregistered" in e for e in errors)
    assert any("400" in e and "BadDeviceToken" in e for e in errors)

    req = ok.calls.last.request
    assert req.headers["apns-topic"] == "tr.com.atilla.admin"
    assert req.headers["apns-push-type"] == "alert"
    tok = req.headers["authorization"].split(" ", 1)[1]
    hdr = jwt.get_unverified_header(tok)
    assert hdr["alg"] == "ES256" and hdr["kid"] == "KEY1234567"
    assert jwt.decode(tok, options={"verify_signature": False})["iss"] == "TEAM123456"
    import json
    payload = json.loads(req.content)
    assert payload["aps"]["alert"] == {"title": "Başlık", "body": "Metin"}
    assert payload["aps"]["sound"] == "default"
    assert payload["type"] == "new_order" and payload["order_id"] == "o1"


@pytest.mark.asyncio
@respx.mock
async def test_apns_data_cannot_override_aps_and_sandbox_host(apns_cfg):
    apns_cfg = {**apns_cfg, "env": "sandbox"}
    r = respx.post("https://api.sandbox.push.apple.com/3/device/t1").mock(return_value=Response(200))
    sent, _, _ = await push_mod._apns_send(apns_cfg, ["t1"], "a", "b", {"aps": {"evil": 1}, "k": "v"})
    assert sent == 1
    import json
    p = json.loads(r.calls.last.request.content)
    assert "evil" not in p["aps"] and p["k"] == "v"


@pytest.mark.asyncio
async def test_apns_config_from_env_and_missing(monkeypatch, pay_env):
    for k in ("APNS_KEY_ID", "APNS_TEAM_ID", "APNS_KEY", "APNS_TOPIC", "APNS_ENV"):
        monkeypatch.delenv(k, raising=False)
    assert await push_mod._apns_config() is None
    monkeypatch.setenv("APNS_KEY_ID", "K1")
    monkeypatch.setenv("APNS_TEAM_ID", "T1")
    monkeypatch.setenv("APNS_KEY", _p8().replace("\n", "\\n"))
    cfg = await push_mod._apns_config()
    assert cfg["topic"] == "tr.com.atilla.admin" and cfg["env"] == "production"
    monkeypatch.setenv("APNS_ENV", "bogus")
    assert (await push_mod._apns_config())["env"] == "production"


@pytest.fixture
def routed(pay_env, monkeypatch):
    fcm, apns = [], []

    async def _fcm_cfg():
        return "kv-project", '{"type": "service_account"}'

    async def _fcm_send(project, sa, tokens, title, body, data):
        fcm.append({"tokens": list(tokens), "title": title})
        return len(tokens), []

    async def _apns_cfg():
        return {"key_id": "K", "team_id": "T", "key": "x", "topic": "tr.com.atilla.admin", "env": "production"}

    async def _apns_send(cfg, tokens, title, body, data):
        apns.append({"tokens": list(tokens), "title": title, "data": dict(data)})
        return len(tokens), [], []

    monkeypatch.setattr(push_mod, "send_push_to_admins", REAL_SEND)
    monkeypatch.setattr(push_mod, "_fcm_config", _fcm_cfg)
    monkeypatch.setattr(push_mod, "_fcm_send", _fcm_send)
    monkeypatch.setattr(push_mod, "_apns_config", _apns_cfg)
    monkeypatch.setattr(push_mod, "_apns_send", _apns_send)
    return fcm, apns


async def _seed():
    await deps.db.users.insert_one({"id": "adm-1", "email": "boss@example.test", "is_admin": True,
                                    "is_super_admin": True, "is_active": True})
    await deps.db.users.insert_one({"id": "cust-9", "email": "c9@example.test", "is_admin": False})
    await deps.db.user_devices.insert_many([
        {"user_id": "adm-1", "device_id": "d-ios", "platform": "ios", "push_token": "tok-ios", "is_active": True},
        {"user_id": "adm-1", "device_id": "d-and", "platform": "android", "push_token": "tok-and", "is_active": True},
        {"user_id": "cust-9", "device_id": "d-c-ios", "platform": "ios", "push_token": "tok-cust-ios",
         "is_active": True},
    ])


@pytest.mark.asyncio
async def test_admin_push_routes_by_platform_and_skips_customers(routed):
    fcm, apns = routed
    await _seed()
    n = await push_mod.send_push_to_admins("x", "y", {"type": "test"})
    assert n == 2
    assert [m["tokens"] for m in fcm] == [["tok-and"]]
    assert [m["tokens"] for m in apns] == [["tok-ios"]]
    log = await deps.db.push_notifications_log.find_one({"kind": "test"}, {"_id": 0})
    assert log["via"] == "fcm_v1+apns" and log["sent"] == 2 and log["targets"] == 2


@pytest.mark.asyncio
async def test_admin_push_ios_only_works_without_fcm_config(routed, monkeypatch):
    fcm, apns = routed

    async def _none():
        return None, None
    monkeypatch.setattr(push_mod, "_fcm_config", _none)
    await _seed()
    n = await push_mod.send_push_to_admins("x", "y", {"type": "test"})
    assert n == 1 and not fcm and apns[0]["tokens"] == ["tok-ios"]


@pytest.mark.asyncio
async def test_admin_push_skipped_silently_when_nothing_configured(routed, monkeypatch):
    fcm, apns = routed

    async def _none_fcm():
        return None, None

    async def _none_apns():
        return None
    monkeypatch.setattr(push_mod, "_fcm_config", _none_fcm)
    monkeypatch.setattr(push_mod, "_apns_config", _none_apns)
    await _seed()
    assert await push_mod.send_push_to_admins("x", "y", {"type": "test"}) == 0
    assert not fcm and not apns


@pytest.mark.asyncio
async def test_send_push_to_devices_queues_unconfigured_platform(routed, monkeypatch):
    fcm, apns = routed

    async def _none_apns():
        return None
    monkeypatch.setattr(push_mod, "_apns_config", _none_apns)
    devices = [{"platform": "ios", "push_token": "a"}, {"platform": "android", "push_token": "b"}]
    res = await push_mod.send_push_to_devices(devices, "t", "b", {"k": "v"})
    assert res["sent"] == 1 and res["queued"] == 1 and res["failed"] == 0
    assert fcm[0]["tokens"] == ["b"] and not apns
    assert any("APNs" in e for e in res["errors"])


def test_push_status_reports_missing_config_and_last_attempt(kv_client, kv_users, kv_db, monkeypatch):
    """Panel 'Bildirim Durumu': APNs yoksa ve cihaz yoksa nedenini gösterir (sır yok)."""
    from conftest import kv_ip, kv_run
    for k in ("APNS_KEY_ID", "APNS_TEAM_ID", "APNS_KEY"):
        monkeypatch.delenv(k, raising=False)
    sa = kv_users.staff(super_admin=True)
    kv_run(push_mod.send_push_to_admins("Yeni", "W1 · 1 ürün", {"type": "new_order"}))
    r = kv_client.get("/api/push/status", headers={**kv_users.hdr(sa), **kv_ip()})
    assert r.status_code == 200, r.text
    js = r.json()
    assert js["apns_configured"] is False
    assert js["last_attempt"]["kind"] == "new_order" and "cihaz" in js["last_attempt"]["result"]
    st = kv_users.staff(perms=["orders.view"])
    assert kv_client.get("/api/push/status", headers={**kv_users.hdr(st), **kv_ip()}).status_code == 403
