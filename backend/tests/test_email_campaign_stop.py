import asyncio
from mongomock_motor import AsyncMongoMockClient


def test_stop_running_campaigns_once(monkeypatch):
    import routes.email_marketing as em
    from datetime import datetime, timezone
    db = AsyncMongoMockClient()["t"]
    monkeypatch.setattr(em, "db", db)
    calls = []

    async def fake_suspend(cfg, cid):
        calls.append(cid)
        return {"success": True}

    async def fake_cfg(_db):
        return {"api_key": "x"}
    monkeypatch.setattr(em, "brevo_suspend_campaign", fake_suspend)
    monkeypatch.setattr(em, "get_brevo_config", fake_cfg)
    now = datetime.now(timezone.utc).isoformat()

    async def run():
        await db.email_campaigns.insert_many([
            {"id": "s", "provider": "ses", "status": "sending", "created_at": now},
            {"id": "b", "provider": "brevo", "status": "submitted", "external_campaign_id": 77, "created_at": now},
            {"id": "old", "provider": "ses", "status": "sent", "created_at": now},
        ])
        await em.stop_running_campaigns_once()
        assert (await db.email_campaigns.find_one({"id": "s"}))["status"] == "stopped"
        assert (await db.email_campaigns.find_one({"id": "b"}))["status"] == "stopped"
        assert (await db.email_campaigns.find_one({"id": "old"}))["status"] == "sent"
        assert calls == [77]
        # SES döngüsü durdurulmuş kampanyayı yeniden başlatmaz
    asyncio.run(run())


def test_resume_stopped_ses_campaign(monkeypatch):
    import routes.email_marketing as em
    db = AsyncMongoMockClient()["t3"]
    monkeypatch.setattr(em, "db", db)
    ran = []

    async def fake_provider():
        return "ses", {"x": 1}

    async def fake_run(cid):
        ran.append(cid)
    monkeypatch.setattr(em, "_selected_provider", fake_provider)
    monkeypatch.setattr(em, "_run_campaign", fake_run)

    async def fake_missing(cid):
        c = await db.email_campaigns.find_one({"id": cid}, {"_id": 0})
        if c.get("status") in ("queued", "sending", "syncing"):
            raise em.HTTPException(status_code=400, detail="x")
        return {"campaign": c, "chain": [cid, "r1"], "received": 1900, "audience": 2146,
                "missing": [{"email": "z@z.z"}]}
    monkeypatch.setattr(em, "_missing_for", fake_missing)

    async def run():
        await db.email_campaigns.insert_one({"id": "s", "provider": "ses", "status": "stopped", "cursor": "m@x.com", "html": "<p>x</p>"})
        r = await em.resume_campaign("s", {"email": "a@b.c"})
        await asyncio.sleep(0)
        assert r["mode"] == "resume"
        c = await db.email_campaigns.find_one({"id": "s"})
        assert c["status"] == "queued" and c["cursor"] == "" and c["exclude_campaign_ids"] == ["r1"]
        assert ran == ["s"]
        await db.email_campaigns.insert_one({"id": "done", "status": "sent", "provider": "ses"})
        try:
            await em.resume_campaign("done", {"email": "a@b.c"})
            assert False
        except em.HTTPException as e:
            assert e.status_code == 400
    asyncio.run(run())


def test_merge_resend_rows_once(monkeypatch):
    import routes.email_marketing as em
    db = AsyncMongoMockClient()["t4"]
    monkeypatch.setattr(em, "db", db)

    async def run():
        await db.email_campaigns.insert_many([
            {"id": "x", "provider": "ses", "status": "stopped", "sent": 1900, "failed": 0, "total": 2146},
            {"id": "r", "provider": "ses", "status": "sent", "sent": 243, "failed": 0, "resend_of": "x"},
        ])
        await em.merge_resend_rows_once()
        x = await db.email_campaigns.find_one({"id": "x"})
        assert x["sent"] == 2143 and x["status"] == "sent" and x["merged_ids"] == ["r"]
        lst = await em.list_campaigns(50, {})
        assert [c["id"] for c in lst["campaigns"]] == ["x"]
    asyncio.run(run())
