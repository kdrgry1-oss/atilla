"""
TEK SEFERLİK SİTE FİYAT GÜNCELLEMESİ — ATILLA_KARLILIK_ANALIZI_v18.xlsx / "Site Karlılık"
=============================================================================
Kaynak: "YENİ" sütunları — Etikete Yazılacak → `price`, Müşterinin Ödediği → `sale_price`
(ikisi eşitse indirimli fiyat YOK → sale_price=None). Yalnız dosyada olan ve sitede
stok kodu + ad ile BİREBİR eşleşen 290 ürün.

YALNIZ SİTE FİYATI değişir. Pazaryeri fiyatları KAYMASIN diye (kullanıcı: "sadece web
sitesi"): Trendyol/Amazon tabanı `member_price_1 || price`, HB (manuel fiyat gönderimi)
tabanı varyantta `member_price_1 || varyant fiyatı`. `member_price_1` boş olan ürün/varyanta
ESKİ taban yazılır → pazaryeri fiyatı bugünküyle aynı kalır. Stok, ad, görsel, kategori,
kampanya vb. HİÇBİR alana dokunulmaz.

Varyantta eski (Ticimax) fiyat alanı doluysa aynı yeni fiyatla güncellenir (Meta katalog
feed'i ve piksel olayları beden fiyatını buradan okur — site fiyatıyla aynı olsun).
Sonuç özeti (eski/yeni) db.settings `fiyat_guncelleme_v18` belgesine yazılır.
"""
from datetime import datetime, timezone

FLAG = "fiyat_guncelleme_v18"

# (ürün id, stok kodu [doğrulama], yeni etiket fiyatı, yeni indirimli fiyat | None)
PLAN = [
    ('7591', 'FCFW274696', 3300, None),
    ('c74849f1-3d55-4a4d-8128-db66928e494d', 'FCSS0400010', 2766, 1936),
    ('4402', 'FCFW3200001', 1741, 1436),
    ('26daef79-81eb-49b9-9d38-89a74fc2e05d', 'FCFW3200001', 1741, 1436),
    ('f68ab6e9-aa85-4a5c-bbb6-3ea9e9dd00a3', 'FCFW3200001', 1861, 1535),
    ('5436', 'FCFW3200002', 1806, 1535),
    ('8618', 'FCFW3200002', 1861, 1535),
    ('3205', 'FCFW3200002', 1861, 1535),
    ('75d3fcb7-87ea-4bd6-a4e4-9344a2b7e8c4', 'FCSS3600001', 1000, 700),
    ('84576e99-1318-4800-913f-ff55dec75beb', 'FCSS3600002', 1000, 700),
    ('4333', 'FCFW165372', 1724, None),
    ('2792', 'FCFW165372', 1724, None),
    ('5813', 'FCFW165372', 1724, None),
    ('714b367b-cea2-4c2e-8ccb-c14105b41428', 'FCSS0100003', 1941, 1650),
    ('40581f23-d07b-46bb-9df5-764fa47fcb8e', 'FCSS0100003', 1941, 1650),
    ('42467d59-6ca0-42dd-9f2d-c8821a15892b', 'FCSS0300001', 2451, 1716),
    ('8cc8a449-da70-48fa-8742-e9fb1943c229', 'FCSS0300001', 2451, 1716),
    ('2415', 'FCSS0100002', 1608, None),
    ('2118', 'FCSS0100002', 1608, None),
    ('2190', 'FCFW0100006', 2019, 1716),
    ('3ce158ee-6485-4fe8-8c37-081dd7b11064', 'FCFW0100006', 2019, 1716),
    ('a8dad308-3ed5-44ce-bcb4-cb0ffdc3e7dd', 'FCFW0100006', 1716, None),
    ('4732', 'FCFW0100001', 896, None),
    ('2525', 'FCFW0100001', 896, None),
    ('6646', 'FCFW0100001', 896, None),
    ('3176', 'FCFW0100001', 896, None),
    ('9785', 'FCFW0100003', 1505, None),
    ('5852', 'FCFW0100003', 1505, None),
    ('6109', 'FCFW0100003', 1505, None),
    ('5273', 'FCFW0100003', 1505, None),
    ('787efe51-5faf-44bc-b79f-2b342e256a90', 'FCSS0200001', 704, None),
    ('8805', 'FCSS0200001', 704, None),
    ('52cf670d-bf34-4d27-bb8e-8f8c8d99e368', 'FCSS0200001', 704, None),
    ('ef8e4607-d4a8-4614-a0eb-412e880d01a7', 'FCSS0200001', 704, None),
    ('4c38ffd1-3a59-4ecf-a3fd-c56c2fc372b8', 'FCSS0200001', 704, None),
    ('1b78f2d0-7823-4a3e-96a4-46c974301334', 'FCSS0200002', 765, None),
    ('5763', 'FCSS0200002', 765, None),
    ('2820', 'FCSS0200002', 765, None),
    ('5259', 'FCSS0200003', 765, None),
    ('8089', 'FCSS0200003', 765, None),
    ('6729', 'FCSS0200003', 765, None),
    ('8831a281-86b6-4476-bded-435a69443837', 'FCSS0200005', 1584, 1234),
    ('8251', 'FCFW463522', 3564, None),
    ('5842', 'FCFW463522', 3564, None),
    ('1917', 'FCFW118909', 3168, None),
    ('ac3477d2-eac1-450d-b4dd-7524797751fc', 'FCSS0400004', 2588, 2200),
    ('4081', 'FCFW395169', 2728, None),
    ('6513', 'FCFW395169', 2728, None),
    ('8057', 'FCFW140904', 2983, None),
    ('5739', 'FCFW0400002', 2824, 2400),
    ('2950', 'FCFW0400002', 2824, 2400),
    ('5130', 'FCFW0400008', 3247, 2760),
    ('9660', 'FCFW0400008', 3247, 2760),
    ('6907', 'FCFW0400008', 3247, 2760),
    ('9681', 'FCFW0400006', 2805, None),
    ('5499', 'FCFW0400006', 2805, None),
    ('2887', 'FCSS0400007', 2678, None),
    ('9452', 'FCSS0400005', 3106, 2640),
    ('125f38d4-4e82-400e-b342-acb99a5f52dd', 'FCSS0400013', 3733, 2613),
    ('7bad5e10-4675-4514-8e9d-7696fc21f329', 'FCSS0400013', 3733, 2613),
    ('900d0969-9c34-4bad-921c-99b277275557', 'FCSS0400012', 2574, None),
    ('7722', 'FCFW891462', 4772, 3025),
    ('8702', 'FCFW891462', 4772, 3025),
    ('d9b1585b-9920-4928-a045-b391cb289fa8', 'FCSS0400003', 3106, 2640),
    ('0b5e1d88-8e4e-4a64-af54-0469439139d7', 'FCSS0400003', 3840, 2640),
    ('50421cff-3b64-44bf-9ab8-c068ebfeeb89', 'FCFW0400010', 3116, None),
    ('3217', 'FCFW0400007', 3844, 3267),
    ('90ea7a46-dc42-4e22-aca3-821bc6829257', 'FCFW0400007', 3844, 3267),
    ('2913', 'FCSS0400008', 2599, None),
    ('eb8cf37f-c307-4c6d-98d9-9a85300909a7', 'FCSS0400008', 2599, None),
    ('b1e5228d-954b-402e-adc3-bc362db49073', 'FCSS0400002', 3080, None),
    ('1669', 'FCSS0400002', 3080, None),
    ('4540', 'FCFW0400005', 2431, None),
    ('6697', 'FCFW0400005', 2431, None),
    ('60bb204b-952c-4188-86a2-dff6b70f340b', 'FCFW0400005', 2431, None),
    ('3165bb07-9a4e-4f4b-8883-647068cd5593', 'FCSS0400009', 3106, 2640),
    ('a6ae9ca6-3277-4345-9b00-efaa00fc9928', 'FCSS0400009', 3106, 2640),
    ('8604', 'FCFW920068', 2517, None),
    ('8680', 'FCFW920068', 2517, None),
    ('9007', 'FCFW814344', 6387, 3861),
    ('9095', 'FCFW814344', 6387, 3861),
    ('2939', 'FCFW0400009', 2871, None),
    ('2607', 'FCFW0400003', 2341, None),
    ('8980', 'FCFW0400003', 2341, None),
    ('6347', 'FCSS248000', 3337, 2002),
    ('1452', 'FCSS248000', 3337, 2002),
    ('a4118f8b-a902-49f1-a187-64052286c434', 'FCSS0600002', 1925, None),
    ('5d584b9d-5304-4c40-93e7-a3cae61d0cb7', 'FCSS0600002', 2750, 1925),
    ('2644', 'FCFW631530', 2728, None),
    ('9589', 'FCFW113844', 2525, None),
    ('a79f5517-14eb-42c8-9d81-0725624a36f2', 'FCSS0600010', 2750, 1925),
    ('f391078e-528e-4765-abea-a993934da8fb', 'FCSS0600010', 2750, 1925),
    ('1430', 'FCSS648936', 3221, 2255),
    ('8280', 'FCFW0600003', 1669, 1419),
    ('3265cb44-ed10-436a-bb4a-416ef386d3e5', 'FCSS0600004', 3420, 2394),
    ('1450', 'FCSS0600012', 2829, 1980),
    ('3525', 'FCSS0600012', 2829, 1980),
    ('cacb6669-b72b-492f-8a29-50835768eb50', 'FCSS0600006', 2079, 1455),
    ('1600', 'FCFW0600007', 1568, None),
    ('4489', 'FCFW0600007', 1568, None),
    ('9952', 'FCFW0600006', 1864, 1584),
    ('9200', 'FCFW0600006', 1864, 1584),
    ('6212', 'FCFW0600006', 1864, 1584),
    ('b84c567a-ba34-495d-be32-4e6d59daca8e', 'FCFW0600005', 2278, 1936),
    ('ea68d258-e3b2-405d-9be5-1749d1313363', 'FCFW0600005', 1936, None),
    ('6b860124-8bd8-40b0-8d2c-8eb2603c2120', '2025140002', 2011, 1408),
    ('5210', 'FCFW832487', 1452, None),
    ('7155', 'FCFW832487', 1452, None),
    ('4941', 'FCFW179183', 1188, None),
    ('5483', 'FCFW0700001', 990, None),
    ('6941', 'FCFW0700001', 990, None),
    ('4235', 'FCFW0700001', 990, None),
    ('c16675eb-b481-444b-b6d9-ae8a787bce2c', '2025140002', 2011, 1408),
    ('e88aba49-9598-4391-a729-d54d250ab1fb', 'FCSS0700005', 1569, None),
    ('8111', 'FCFW0700003', 1242, 1056),
    ('3a5cee77-0ad9-4b5b-b703-f78cc6dcad9a', 'FCSS0700007', 1910, 1337),
    ('a37dc2ee-044d-4b06-b887-9346bb43c35b', 'FCSS0700007', 1910, 1337),
    ('5dae4c77-295c-44c9-9d53-8e75de565c86', 'FCSS0700006', 1964, 1375),
    ('b8f484f1-8f00-4dcb-8d63-297148961dc7', 'FCSS0700006', 1964, 1375),
    ('3865', 'FCSS0700004', 1242, 1056),
    ('9b01c7de-5138-4e9c-9a95-4d8b821f6f7a', 'FCSS0700002', 1697, 1188),
    ('e6cccee8-13ee-437a-8b5e-86acde095a3a', 'FCSS0700002', 1697, 1188),
    ('5469', 'FCSS0700002', 1697, 1188),
    ('6825', 'FCSS0700003', 1980, 1683),
    ('bd753f1f-88cd-48fb-ab87-c2f6dbf779a5', 'FCSS0700001', 1309, None),
    ('2221', 'FCSS0700001', 1309, None),
    ('5627', 'FCFW109891', 1982, 1496),
    ('5551', 'FCFW109891', 1982, 1496),
    ('2899', 'FCFW2800001', 667, None),
    ('fa018a9c-885e-42fc-aefb-95d5e4d66537', 'FCFW2800001', 667, None),
    ('8602', 'FCFW2800002', 680, None),
    ('5df3d749-9b91-45d2-8709-769f1f5f824d', 'FCFW2800002', 680, None),
    ('2322', 'FCFW2800002', 680, None),
    ('9297', 'FCFW2800002', 680, None),
    ('d0757af5-6f78-44e3-81d8-6ea7559266c2', 'FCSS0900002', 1706, None),
    ('5070', 'FCFW0900004', 1690, None),
    ('9182', 'FCFW0900004', 1690, None),
    ('7958', 'FCSS0900005', 1739, 1478),
    ('2113b483-234e-41dd-acce-7357645cfd0c', 'FCSS0900003', 1341, None),
    ('794a4786-938d-4dd7-bed7-3b3b99abd9aa', 'FCSS0900003', 1341, None),
    ('2232', 'FCSS0900003', 1341, None),
    ('e01a2676-98f3-47e8-aecf-ba6545e3557f', 'FCSS0900008', 1964, 1375),
    ('cd4a9261-f155-42aa-b989-606a8bfbd492', 'FCSS0900008', 1964, 1375),
    ('c09b70cc-0c49-42b1-bf93-beec502f0e55', 'FCSS0900008', 1964, 1375),
    ('4120', 'FCSS0900006', 1806, None),
    ('8644', 'FCSS0900006', 1806, None),
    ('9740', 'FCFW0900001', 1250, None),
    ('5014', 'FCFW0900001', 1250, None),
    ('03eb1d41-5d65-465c-bac2-e918fc490a69', 'FCSS0900010', 2121, 1485),
    ('7a26f2d3-2759-49d6-9d2f-3ca943a8d5e9', 'FCSS0900010', 2121, 1485),
    ('6f698948-0d86-45b9-8097-0533eb306b3c', 'FCSS0900004', 2096, 1782),
    ('7ceb111c-9e7c-4f72-988c-f9843cca91bd', 'FCSS0900004', 2096, 1782),
    ('9869', 'FCSS0900004', 2096, 1782),
    ('080dcb21-a61d-429c-9d79-18aab62f3e9f', 'FCSS0900004', 2096, 1782),
    ('5076', 'FCFW0900002', 1276, None),
    ('9115', 'FCFW0900002', 1276, None),
    ('8226', 'FCFW0900002', 1276, None),
    ('9032', 'FCFW0900002', 1276, None),
    ('3068', 'FCFW0900002', 1276, None),
    ('4313', 'FCFW0900002', 1276, None),
    ('7e7a58f2-5b82-4d78-b783-ba6e63a1d6cf', 'FCSS0900007', 1500, None),
    ('34bafc5d-d540-4f30-aa19-256735b62054', 'FCSS0900007', 1500, None),
    ('b0e88dde-b55f-4b69-8136-036cdd51cfa9', 'FCSS0900007', 1500, None),
    ('3252bddd-c4a1-4605-9843-7b3171d929b5', 'FCSS0900007', 1500, None),
    ('7459', 'FCFW983712', 1705, None),
    ('e82f55a6-72cf-40ec-8dbb-80b0ed2e68ad', 'FCSS0900009', 2111, 1478),
    ('c783181d-b656-45e6-ab2f-fd7d896e4728', 'FCSS0900009', 2111, 1478),
    ('5304', 'FCFW170928', 1723, None),
    ('8498', 'FCSS0900001', 1296, None),
    ('2066', 'FCSS0900001', 1296, None),
    ('2027', 'FCFW1000001', 3520, 1056),
    ('5056', 'FCFW1000001', 3520, 1056),
    ('3527', 'FCFW1000001', 3520, 1056),
    ('01395b40-ec09-4aef-9de0-4a3556498f0d', 'FCSS1100001', 3099, 2265),
    ('82ee4243-2f96-4bd9-a9e0-ca0ce252ff08', 'FCSS1100001', 2265, None),
    ('693f859d-c593-4b71-b6ca-bff2cd0caa95', 'FCSS1100001', 2265, None),
    ('1594', '1440', 5983, 2874),
    ('eb97d529-1580-4c9f-aff7-77a16123f540', 'FCFW1200001', 5401, 3267),
    ('9544', 'FCFW1200001', 5401, 3267),
    ('8938', 'FCFW1200001', 5401, 3267),
    ('8746', 'FCFW1200003', 3465, None),
    ('9105', 'FCFW1300004', 1400, None),
    ('3430', 'FCFW1300004', 1400, None),
    ('fe25e39d-19d3-47f9-8e75-409073f71c96', 'FCFW1300004', 1400, None),
    ('7354', 'FCFW1300005', 1694, 1440),
    ('8045', 'FCFW1300005', 1694, 1440),
    ('1182', 'FCFW1300005', 1694, 1440),
    ('4051', 'FCFW1300002', 1749, None),
    ('8501', 'FCFW1300002', 1749, None),
    ('bda99ae1-4b9c-4166-bc7e-bc455b0a5686', 'FCSS1400001', 3017, 2112),
    ('4348', 'FCFW992112', 3850, None),
    ('7227', 'FCFW992112', 3850, None),
    ('6bd3ba00-451a-42d1-b138-259cc3e7b994', '77887783', 1747, 1485),
    ('4410', 'FCFW1800009', 1988, 1690),
    ('8632', 'FCFW1800002', 990, None),
    ('1983', 'FCFW1800002', 990, None),
    ('7000', 'FCFW1800002', 990, None),
    ('71f5b699-9c5a-431f-ab40-ff634b9b749c', 'FCSS1800009', 1988, 1690),
    ('9864', 'FCSS1800009', 1988, 1690),
    ('6376ba9f-f5e7-4222-bbdc-6b25d474f1fb', 'FCSS1800014', 2145, 1716),
    ('2b0373b5-95f2-4b65-9056-f97368f02c41', 'FCSS1800014', 2145, 1716),
    ('22b21595-7cc7-4876-944c-2a8a143fbdd0', 'FCSS1800008', 1967, 1672),
    ('de354db2-c0cd-4e83-86c9-10a009da37e9', 'FCSS1800008', 1967, 1672),
    ('1ba9f1ec-e78a-47e6-9566-3ff90c7fa48d', 'FCSS1800008', 1967, 1672),
    ('2dc9785c-12f3-4126-bffa-f06c6c687d22', 'FCSS1800007', 2065, 1755),
    ('09069ee4-5a64-431b-b538-55650ef274c8', 'FCSS1800007', 2065, 1755),
    ('61dc58d4-5998-4554-bc7f-01c6327464c9', 'FCSS1800007', 2065, 1755),
    ('9b7d31a8-901e-4cf9-9c1c-84520145a9b9', 'FCSS1800006', 2011, 1709),
    ('04001a71-726b-42c3-991e-756ab7ec371c', 'FCSS1800006', 2011, 1709),
    ('12ec6d0d-5d7b-4721-908c-b674974ef59b', 'FCSS1800006', 2011, 1709),
    ('23f5a2db-6882-4d03-9730-d865bdffc1ea', 'FCSS1800015', 1782, None),
    ('9084', 'FCSS1800005', 1839, 1563),
    ('4013', 'FCSS1800005', 1839, 1563),
    ('2858', 'FCSS1800005', 1839, 1563),
    ('7d5952e1-3ab6-4f87-809d-88613c098938', '77887783', 1747, 1485),
    ('9738', 'FCSS1800010', 1965, 1670),
    ('3787', 'FCFW1800003', 1488, None),
    ('5969', 'FCFW1800003', 1488, None),
    ('64a6ff94-4ec5-464a-b9f5-2a412f0dfba9', 'FCSS1800001', 1964, 1375),
    ('c86c7ff2-9770-4138-b8a8-58fd32fbb29d', 'FCSS1800001', 1964, 1375),
    ('3667', 'FCSS1800001', 1964, 1375),
    ('3578', 'FCFW1800005', 2061, 1443),
    ('ee642e16-e1bc-4864-9a8d-fb9c15eec7f0', 'FCSS3400001', 2376, 1663),
    ('5ae86961-983e-42cb-8b60-ed959f389a78', 'FCSS3300002', 1584, None),
    ('8ed309c0-fcce-4719-8442-dc5dbc325c02', 'FCSS3300002', 1584, None),
    ('4705', 'FCSS3300002', 1584, None),
    ('e5211128-05dd-4867-92b7-84c6fbd8e22e', 'FCSS3300002', 1584, None),
    ('6966', 'FCSS3300001', 1683, None),
    ('4340', 'FCSS3300001', 1683, None),
    ('1442', 'FCSS3300001', 1683, None),
    ('50f152fd-ee4b-4e19-9113-8b6758401597', 'FCSS3300003', 1836, 1561),
    ('9626', 'FCSS3300003', 1836, 1561),
    ('6422', 'FCFW5500001', 1622, 1379),
    ('6336', 'FCFW5500001', 1622, 1379),
    ('d5fab97d-1a43-40db-b391-06b7129e0432', 'FCSS2000004', 3340, 2338),
    ('f6ee5e69-2840-4e59-805b-cbc50eb73a8d', 'FCSS2000004', 3340, 2338),
    ('3773', 'FCFW0600002', 2400, None),
    ('9d547016-735e-45d2-a630-fbabbd5093d2', 'FCSS2000005', 2759, 1931),
    ('57b67ca7-6ab7-4c8d-b65a-f5039c821253', 'FCSS2000005', 2759, 1931),
    ('4e096d7f-0f83-4c25-accb-24833242449b', 'FCSS1800013', 2051, 1436),
    ('66f6246d-d7f5-45e1-aa3b-c37e4552221d', 'FCSS1800013', 2051, 1436),
    ('c8b51c6d-65f8-4f49-9574-ac95177a65db', 'FCSS1800013', 2051, 1436),
    ('48b65e32-cff9-494c-a767-a3ad7dc34004', 'FCSS2000006', 2784, 1949),
    ('15773198-1d94-468e-bd5d-661294a05f1c', 'FCFW1800008', 1912, 1625),
    ('1990', 'FCFW1800008', 1912, 1625),
    ('92d17bc4-b18e-4c97-8848-82162517c989', 'FCSS2000003', 3279, 2295),
    ('c3441acc-1cbd-4e27-9cc8-9e257a4d7257', 'FCSS2000008', 3630, 2541),
    ('a67f6f65-3b72-49fe-9a9c-5c3feecee7d1', 'FCSS2000008', 3630, 2541),
    ('3956', 'FCSS2000001', 3017, 2112),
    ('b588d656-f94e-47c4-9afd-9b6543a3f77e', 'FCSS2000001', 3017, 2112),
    ('29e862a2-38af-4880-a367-57ba161f08be', 'FCSS2000011', 4096, 2178),
    ('f35d45f7-5b6f-4d3d-bd3e-ffe83142ba31', 'FCSS2000007', 3253, 2277),
    ('d901da67-ca2b-4806-a2d9-599123196b46', 'FCSS2000007', 3253, 2277),
    ('5308', 'FCSS983310', 3250, 2530),
    ('e80ae1ed-c519-4dca-aa85-514166bb83ca', 'FCSS1800011', 1634, None),
    ('4a1d862c-03fb-44f5-a951-cf7e38d81b56', 'FCSS1800011', 1634, None),
    ('d9a28692-0c96-4489-8080-d8b9ea3ef196', 'FCSS2000010', 2939, 2057),
    ('4162', 'FCFW1800007', 1683, None),
    ('3abcd410-5b58-45f2-b5d1-8b8a483d3d42', 'FCFW1800007', 1683, None),
    ('88d8735a-5404-4218-bd90-04838710460b', 'FCFW1800007', 1683, None),
    ('f0cbfd16-d33f-49f0-bbba-998028627120', 'FCSS2000002', 2619, 2226),
    ('bb60d95b-f43e-4453-8dfc-d0ae786a5b31', 'FCSS2000002', 2619, 2226),
    ('4712', 'FCFW1800005', 2256, 1579),
    ('4113', 'FCFW2000001', 1941, 1650),
    ('1807', 'FCFW2000001', 2047, 1740),
    ('3791', 'FCSS2100002', 1210, None),
    ('5205', 'FCSS2100002', 1210, None),
    ('4828', 'FCSS2100002', 1210, None),
    ('3663', 'FCSS2100002', 1210, None),
    ('3732', 'FCSS2100002', 1210, None),
    ('8685', 'FCSS2100002', 1210, None),
    ('3043', 'FCFW994865', 1162, None),
    ('6591', 'FCFW994865', 1162, None),
    ('0c5e1762-219a-4897-b3cc-10f856739894', 'FCSS2100001', 1058, 899),
    ('1088', 'FCSS2100001', 1058, 899),
    ('75760bc4-4058-4249-8134-32cc835cd5ea', '48289894', 3025, None),
    ('6935', 'FCFW859466', 2431, None),
    ('4265', 'FCFW628283', 2640, None),
    ('026d7db6-b77a-451d-ab4b-c471961907d8', 'FCFW2300001', 2600, 1820),
    ('768c752a-8ac4-4124-b2d4-b730ba571a11', 'FCFW2300001', 2600, 1820),
    ('26336854-59bb-4944-aa00-0bc6a15b5f79', 'FCSS2500001', 1560, None),
    ('f3f1860b-4f88-43a4-9375-e5dfa446d753', 'FCSS2500001', 1560, None),
    ('9a2033a1-b61b-4357-a041-12dba7d71121', 'FCSS2500001', 1560, None),
    ('3723', 'FCSS2700001', 1122, None),
    ('3f7f3c8e-fcc0-41fb-afc8-39783be95843', 'FCSS2700004', 1760, 1232),
    ('ef0e32e6-b130-44a3-a3d6-8c94687427a8', 'FCSS2700004', 1760, 1232),
    ('8f479683-48c7-446f-983d-7bd830248e84', 'FCSS2700004', 1760, 1232),
    ('a737d6b4-8af5-4f28-81ae-1c2e367c0727', 'FCSS2700002', 1739, 1217),
    ('2417dc63-d45b-49cf-ae74-617fc2e77818', 'FCSS3500001', 1980, 1386),
    ('92bef741-dd5f-48ed-b20f-2c9e8155774c', 'FCSS3500001', 1980, 1386),
]


def _f(v) -> float:
    try:
        return float(str(v).replace(",", ".")) if v not in (None, "") else 0.0
    except Exception:
        return 0.0


async def apply(db) -> dict:
    """Bayrak korumalı; ikinci çalıştırmada hiçbir şey yapmaz."""
    if ((await db.settings.find_one({"id": FLAG}, {"_id": 0, "done": 1})) or {}).get("done"):
        return {"skipped": "done"}
    try:
        from routes.integrations_hepsiburada import _hb_pick_base_price, _hb_price_source
        hb_source = await _hb_price_source()
    except Exception:
        _hb_pick_base_price, hb_source = None, "price"
    now = datetime.now(timezone.utc).isoformat()
    changed, missing, mismatch = [], [], []
    for pid, sc, new_price, new_sale in PLAN:
        p = await db.products.find_one({"id": pid}, {"_id": 0, "id": 1, "stock_code": 1, "price": 1,
                                                     "sale_price": 1, "member_price_1": 1, "variants": 1})
        if not p:
            missing.append(sc)
            continue
        if str(p.get("stock_code") or "").strip() != sc:
            mismatch.append(sc)
            continue
        old_price, old_sale = p.get("price"), p.get("sale_price")
        upd = {"price": float(new_price),
               "sale_price": (float(new_sale) if (new_sale and 0 < new_sale < new_price) else None),
               "updated_at": now}
        froze = False
        if _f(p.get("member_price_1")) <= 0 and _f(old_price) > 0:
            upd["member_price_1"] = _f(old_price)      # TY/Amazon tabanı sabit
            froze = True
        variants = p.get("variants") or []
        new_vars, v_touched = [], 0
        for v in variants:
            if not isinstance(v, dict):
                new_vars.append(v)
                continue
            v = dict(v)
            if v.get("price") not in (None, "", 0) or v.get("sale_price") not in (None, "", 0):
                if _f(v.get("member_price_1")) <= 0 and _hb_pick_base_price:
                    old_hb = _hb_pick_base_price(v, hb_source)
                    if old_hb > 0:
                        v["member_price_1"] = old_hb    # HB varyant tabanı sabit
                adj = _f(v.get("price_adjustment") or v.get("price_diff"))
                v["price"] = round(float(new_price) + adj, 2)
                v["sale_price"] = (round(float(new_sale) + adj, 2) if upd["sale_price"] else None)
                v_touched += 1
            new_vars.append(v)
        if v_touched:
            upd["variants"] = new_vars
        await db.products.update_one({"id": pid}, {"$set": upd})
        changed.append({"id": pid, "sc": sc, "eski": [old_price, old_sale],
                        "yeni": [upd["price"], upd["sale_price"]], "pazaryeri_sabit": froze,
                        "varyant": v_touched})
    res = {"id": FLAG, "done": True, "at": now, "changed": len(changed), "missing": missing,
           "mismatch": mismatch, "items": changed}
    await db.settings.update_one({"id": FLAG}, {"$set": res}, upsert=True)
    return {k: res[k] for k in ("changed", "missing", "mismatch")}


MEMBER_FLAG = "fiyat_guncelleme_v18_uye_tipi"


async def apply_member_price(db) -> dict:
    """TEK SEFERLİK (Kadir isteği 2026-09-29): aynı listedeki ETİKET fiyatı (Etikete Yazılacak)
    Üye Tipi Fiyatı 1'e (member_price_1) de yazılır — ürün ve varyant düzeyinde (varyantta
    fiyat farkı eklenir). BİLEREK: pazaryeri tabanı bu alandır → Trendyol/Amazon/HB fiyatları
    sonraki senkronda bu fiyattan (marj ile) hesaplanır (kullanıcı onayladı)."""
    if ((await db.settings.find_one({"id": MEMBER_FLAG}, {"_id": 0, "done": 1})) or {}).get("done"):
        return {"skipped": "done"}
    now = datetime.now(timezone.utc).isoformat()
    changed, missing = 0, []
    for pid, sc, new_price, _sale in PLAN:
        p = await db.products.find_one({"id": pid}, {"_id": 0, "stock_code": 1, "variants": 1})
        if not p or str(p.get("stock_code") or "").strip() != sc:
            missing.append(sc)
            continue
        upd = {"member_price_1": float(new_price), "updated_at": now}
        vs, touched = [], 0
        for v in (p.get("variants") or []):
            if isinstance(v, dict) and (_f(v.get("member_price_1")) > 0 or v.get("price") not in (None, "", 0)
                                        or v.get("sale_price") not in (None, "", 0)):
                v = dict(v)
                v["member_price_1"] = round(float(new_price) + _f(v.get("price_adjustment") or v.get("price_diff")), 2)
                touched += 1
            vs.append(v)
        if touched:
            upd["variants"] = vs
        await db.products.update_one({"id": pid}, {"$set": upd})
        changed += 1
    await db.settings.update_one({"id": MEMBER_FLAG}, {"$set": {"id": MEMBER_FLAG, "done": True, "at": now,
                                 "changed": changed, "missing": missing}}, upsert=True)
    return {"changed": changed, "missing": missing}
