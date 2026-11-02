"""Amazon sevk irsaliyesi (Seller Central 'packing slip', TR düzeni) çözümleyici — sentetik metin."""
from routes.amazon_spapi import _parse_amazon_packing_slip

SLIP = """Teslimat adresi:
ayse deneme yilmaz
ornek mah test sokak
ornek sitesi
A blok no:1/2 daire:3
kat:1
Ornek Mh.,  İstanbul,
Esenyurt,  34510
Sipariş Tarihi:
Kargo Yöntemi:
Alıcı Adı:
Satıcı Adı:
20 Eki 2026 Pzt
Standart
ayse
Ornek Satici
Alıcı:
ayse deneme yilmaz
ornek mah test sokak ornek
sitesi
A blok no:1/2 daire:3 kat:1
Ornek Mh.,  İstanbul,  Esenyurt,  34510
Phone :  5550001122
Sipariş No.: 171-1234567-7654321
Amazon Pazar Yeri'nde Ornek Satici isimli satıcıdan alışveriş yaptığınız için teşekkür ederiz.
"""


def test_parses_recipient_block_not_buyer_account_name():
    d = _parse_amazon_packing_slip(SLIP)
    assert d["order_number"] == "171-1234567-7654321"
    assert d["name"] == "ayse deneme yilmaz"
    assert d["city"] == "İstanbul" and d["district"] == "Esenyurt" and d["postal_code"] == "34510"
    assert d["phone"] == "5550001122"
    assert d["address"].startswith("ornek mah test sokak")
