"""Rapor önbelleği: sipariş hareketi sonrası bayat-ama-yakın sonuç ANINDA döner, arka planda tazelenir."""
import asyncio

import pytest

from routes import reports as R

pytestmark = pytest.mark.asyncio


async def test_context_stale_while_revalidate(pay_env, monkeypatch):
    calls = {"n": 0}

    async def fake(s, e, source=None):
        calls["n"] += 1
        await asyncio.sleep(0.05)
        return ([calls["n"]], {}, {})

    monkeypatch.setattr(R, "_canonical_report_context_uncached", fake)
    R._CTX_CACHE.clear()
    first = await R._canonical_report_context("2026-10-01", "2026-10-16")
    assert first[0] == [1] and calls["n"] == 1
    again = await R._canonical_report_context("2026-10-01", "2026-10-16")
    assert again[0] == [1] and calls["n"] == 1                 # taze → önbellek
    await R.invalidate_report_cache()                          # sipariş hareketi
    stale = await R._canonical_report_context("2026-10-01", "2026-10-16")
    assert stale[0] == [1]                                     # beklemeden eski sonuç
    await asyncio.sleep(0.2)                                   # arka plan tazeledi
    fresh = await R._canonical_report_context("2026-10-01", "2026-10-16")
    assert fresh[0] == [2] and calls["n"] == 2


async def test_endpoint_cache_and_internal_call_bypass(pay_env):
    calls = {"n": 0}

    @R.report_swr_cache("t_ep")
    async def ep(start_date=None, current_user=None):
        calls["n"] += 1
        return {"rows": [calls["n"]]}

    R._EP_CACHE.clear()
    a = await ep(start_date="2026-10-01", current_user={"id": "x"})
    b = await ep(start_date="2026-10-01", current_user={"id": "y"})
    assert a == b == {"rows": [1]} and calls["n"] == 1
    b["rows"].append(99)                                       # kopya: önbellek bozulmaz
    assert (await ep(start_date="2026-10-01"))["rows"] == [1]
    await ep(start_date=object())                              # basit olmayan param → önbelleksiz
    assert calls["n"] == 2
