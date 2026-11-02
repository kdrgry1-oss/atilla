import routes.products as rp


def test_tier_badge_hidden_when_product_campaign_blocks_it(monkeypatch):
    camp = {"id": "c20", "value": 20, "categories": ["ws"], "title": "%20",
            "not_combinable_with": ["t5000", "t10000"]}
    tiers = [{"id": "t5000", "type": "fixed", "value": 500, "min_cart_total": 5000},
             {"id": "t10000", "type": "fixed", "value": 1000, "min_cart_total": 10000}]
    monkeypatch.setitem(rp._CAMP_BADGE_CACHE, "tiers", tiers)
    monkeypatch.setitem(rp._CAMP_BADGE_CACHE, "multi", [])
    ws = rp._apply_campaign_badge({"id": "a", "price": 3000, "category_ids": ["ws"]}, [camp])
    assert ws["campaign_discount_percent"] == 20 and ws["tier_badge"] == ""
    other = rp._apply_campaign_badge({"id": "b", "price": 3000, "category_ids": ["x"]}, [camp])
    assert other["tier_badge"]  # kapsam dışı ürün: sepet-tutarı ibaresi görünür
    # engel yoksa ikisi birlikte görünür
    camp2 = {**camp, "not_combinable_with": []}
    both = rp._apply_campaign_badge({"id": "a", "price": 3000, "category_ids": ["ws"]}, [camp2])
    assert both["tier_badge"]
