"""Lider-retry, argümanlı işlerin argümanlarını korumalı (TypeError: missing 'asama' regresyonu)."""
import asyncio

import scheduler


class _FakeSched:
    def __init__(self):
        self.jobs = []

    def add_job(self, fn, trigger, **kw):
        self.jobs.append((fn, kw))


def test_lead_retry_preserves_args_and_kwargs(monkeypatch):
    calls = []

    async def job(asama, extra=None):
        calls.append((asama, extra))

    async def not_leader():
        return False

    fake = _FakeSched()
    monkeypatch.setattr(scheduler, "_acquire_or_renew_leadership", not_leader)
    monkeypatch.setattr(scheduler, "_scheduler", fake)
    w = scheduler._lead(job)
    asyncio.run(w("kapat", extra=1))
    assert calls == []
    fn, kw = fake.jobs[0]
    assert kw["args"] == ["kapat"]
    assert kw["kwargs"] == {"extra": 1, "_lead_retry": True}
    assert kw["id"].endswith("__kapat")

    async def leader():
        return True

    monkeypatch.setattr(scheduler, "_acquire_or_renew_leadership", leader)
    asyncio.run(fn(*kw["args"], **kw["kwargs"]))
    assert calls == [("kapat", 1)]
