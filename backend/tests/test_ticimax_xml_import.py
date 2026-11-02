"""Ticimax XMLExport aktarımı: stoklu ürün, kategori ağacı, varyantlar, tüm alanlar, tekrar çalıştırma."""
import asyncio

import mongomock_motor
import pytest

import ticimax_xml_import as tx

XML = """<?xml version="1.0" encoding="utf-8"?>
<Root><Urunler>
 <Urun>
  <UrunKartiID>501</UrunKartiID><UrunAdi>Gümüş Zincir Kolye</UrunAdi>
  <OnYazi>925 ayar</OnYazi><Aciklama><![CDATA[<p>Şık &amp; zarif</p>]]></Aciklama>
  <Marka>Inure</Marka><Tedarikci>Atölye</Tedarikci>
  <KategoriTree>Ana Sayfa > Takı > Kolye</KategoriTree><Kategori>Kolye</Kategori>
  <SEO_SayfaBaslik>Kolye SEO</SEO_SayfaBaslik><GtipKodu>7113</GtipKodu><OzelAlan1>X</OzelAlan1>
  <Resimler><Resim>https://cdn.example.com/a.jpg</Resim><Resim>https://cdn.example.com/b.jpg</Resim></Resimler>
  <TeknikDetaylar><TeknikDetay Tanim="Ayar" Deger="925"/></TeknikDetaylar>
  <UrunSecenek>
   <Secenek><VaryasyonID>9001</VaryasyonID><StokKodu>KLY-45</StokKodu><Barkod>869001</Barkod>
     <StokAdedi>3</StokAdedi><SatisFiyati>1.250,00</SatisFiyati><IndirimliFiyat>999,90</IndirimliFiyat>
     <KDVDahil>true</KDVDahil><KdvOrani>20</KdvOrani><ParaBirimi>TL</ParaBirimi><Desi>1</Desi>
     <EkSecenekOzellik><Ozellik Tanim="Uzunluk" Deger="45 cm">45 cm</Ozellik><Ozellik Tanim="Renk" Deger="Gümüş">Gümüş</Ozellik></EkSecenekOzellik>
   </Secenek>
   <Secenek><VaryasyonID>9002</VaryasyonID><StokKodu>KLY-50</StokKodu><Barkod>869002</Barkod>
     <StokAdedi>0</StokAdedi><SatisFiyati>1250</SatisFiyati><KDVDahil>true</KDVDahil><KdvOrani>20</KdvOrani>
     <EkSecenekOzellik><Ozellik Tanim="Uzunluk" Deger="50 cm">50 cm</Ozellik><Ozellik Tanim="Renk" Deger="Gümüş">Gümüş</Ozellik></EkSecenekOzellik>
   </Secenek>
  </UrunSecenek>
 </Urun>
 <Urun>
  <UrunKartiID>502</UrunKartiID><UrunAdi>Stoksuz Küpe</UrunAdi><KategoriTree>Takı > Küpe</KategoriTree>
  <UrunSecenek><Secenek><VaryasyonID>9100</VaryasyonID><StokAdedi>0</StokAdedi><SatisFiyati>300</SatisFiyati></Secenek></UrunSecenek>
 </Urun>
 <Urun>
  <UrunKartiID>503</UrunKartiID><UrunAdi>Yüzük</UrunAdi><KategoriTree>Takı/Yüzük</KategoriTree>
  <UrunSecenek><Secenek><VaryasyonID>9200</VaryasyonID><StokAdedi>2</StokAdedi><SatisFiyati>500</SatisFiyati>
   <KDVDahil>false</KDVDahil><KdvOrani>20</KdvOrani>
   <EkSecenekOzellik><Ozellik Tanim="Yüzük Ölçüsü" Deger="14">14</Ozellik></EkSecenekOzellik></Secenek></UrunSecenek>
 </Urun>
</Urunler></Root>""".encode("utf-8")


@pytest.fixture
def mdb(monkeypatch):
    db = mongomock_motor.AsyncMongoMockClient()["t"]
    import routes.deps as deps
    monkeypatch.setattr(deps, "db", db)
    return db


def run(c):
    return asyncio.get_event_loop().run_until_complete(c) if False else asyncio.run(c)


def test_import_full(mdb):
    prods = tx.parse_feed(XML)
    assert len(prods) == 3
    st = run(tx.import_products(mdb, prods))
    assert st["created"] == 2 and st["skipped_no_stock"] == 1 and st["errors"] == 0

    p = run(mdb.products.find_one({"urun_karti_id": "501"}))
    assert p["name"] == "Gümüş Zincir Kolye" and p["price"] == 1250.0 and p["sale_price"] == 999.9
    assert p["stock"] == 3 and len(p["variants"]) == 2               # stoksuz varyant da var (0)
    v45 = next(v for v in p["variants"] if v["urun_id"] == "9001")
    assert v45["size"] == "45 cm" and v45["color"] == "Gümüş" and v45["barcode"] == "869001"
    assert v45["stock_code"] == "KLY-45" and v45["stock"] == 3
    assert p["images"] == ["https://cdn.example.com/a.jpg", "https://cdn.example.com/b.jpg"]
    assert p["brand"] == "Inure" and p["short_description"] == "925 ayar" and "Şık & zarif" in p["description"]
    assert p["meta_title"] == "Kolye SEO" and p["gtip_code"] == "7113"
    assert p["ticimax_fields"]["OZELALAN1"] == "X"                      # bilinmeyen alan da saklanır
    assert p["ticimax_xml"]["UrunKartiID"] == "501"                     # ham XML eksiksiz
    assert p["technical_details"] == [{"name": "Ayar", "value": "925"}]
    assert p["breadcrumb"] == "Takı > Kolye" and p["category_name"] == "Kolye"

    cats = {c["name"]: c for c in run(mdb.categories.find({}).to_list(None))}
    assert set(cats) == {"Takı", "Kolye", "Yüzük"}                      # 'Ana Sayfa' atlandı, Küpe stoksuz
    assert cats["Kolye"]["parent_id"] == cats["Takı"]["id"] and not cats["Takı"]["parent_id"]
    assert p["category_id"] == cats["Kolye"]["id"] and set(p["category_ids"]) == {cats["Takı"]["id"], cats["Kolye"]["id"]}

    y = run(mdb.products.find_one({"urun_karti_id": "503"}))
    assert y["price"] == 600.0 and y["variants"][0]["size"] == "14"     # KDV hariç → KDV eklendi
    assert y["category_id"] == cats["Yüzük"]["id"]


def test_reimport_updates_stock_and_keeps_manual_edits(mdb):
    run(tx.import_products(mdb, tx.parse_feed(XML)))
    run(mdb.products.update_one({"urun_karti_id": "501"}, {"$set": {"name": "Benim Adım", "name_by": "admin"}}))
    xml2 = XML.replace(b"<StokAdedi>3</StokAdedi>", b"<StokAdedi>7</StokAdedi>")
    st = run(tx.import_products(mdb, tx.parse_feed(xml2)))
    assert st["created"] == 0 and st["updated"] == 2
    p = run(mdb.products.find_one({"urun_karti_id": "501"}))
    assert p["name"] == "Benim Adım"                                     # elle düzenleme korunur
    assert p["stock"] == 7 and len(p["variants"]) == 2
    assert run(mdb.categories.count_documents({})) == 3                 # kategoriler tekrar oluşmaz
