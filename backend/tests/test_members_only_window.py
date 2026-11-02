from datetime import datetime, timezone, timedelta
from routes.products import category_members_only_active


def test_members_only_window():
    now = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)
    assert category_members_only_active({"members_only": True}, now)
    assert not category_members_only_active({"members_only": False, "members_only_end": "2026-12-01T00:00"}, now)
    # bitiş (TR saati, naive) gelecekte → üyelere özel
    assert category_members_only_active({"members_only": True, "members_only_end": "2026-11-04T23:59"}, now)
    # bitiş geçti → herkese açık
    assert not category_members_only_active({"members_only": True, "members_only_end": "2026-11-03T14:59"}, now)
    assert category_members_only_active({"members_only": True, "members_only_end": "2026-11-03T15:01"}, now)
    # başlangıç öncesi de üyelere özel (sızıntı yok)
    assert category_members_only_active({"members_only": True, "members_only_start": "2026-11-10T00:00",
                                         "members_only_end": "2026-11-12T00:00"}, now)
