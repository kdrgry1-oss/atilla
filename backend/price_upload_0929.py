"""
TEK SEFERLİK SİTE FİYAT YÜKLEMESİ — "Site_Fiyat_Yükleme_2026-09-29 - developer.xlsx"
=============================================================================
Kullanıcı isteği: listedeki ürünlerde
  • Satış Fiyatı → etiket fiyatı (price)
  • İndirimli Fiyatı satıştan UCUZSA → indirimli fiyat (sale_price); değilse indirimli fiyat boş
  • Sale'de = Evet → İNDİRİM kategorisine (9306) ekle (yoksa); Hayır → İNDİRİM'deyse çıkar.
Eşleşme: Ürün Kart ID (urun_karti_id) + ürün adı; olmazsa stok kodu + ad. Bedenler aynı fiyatlı.
Pazaryeri tabanı (member_price_1) boşsa eski fiyatla sabitlenir (Trendyol/Amazon kaymasın);
doluysa dokunulmaz. Stok/ad/görsel vb. değişmez. Sonuç: settings.fiyat_yukleme_0929.
"""
from datetime import datetime, timezone

FLAG = "fiyat_yukleme_0929"
SALE_CAT = "9306"

# (ürün kart id, ürün adı, stok kodu, satış fiyatı, indirimli fiyat|None, sale'de evet?)
PLAN = [
    ('2566', 'Yüksek Bel Kapri Pantolon Siyah', 'FCSS1800001', 1390, 1250, True),
    ('2567', 'Yandan Bağlamalı Poplin Gömlek Açık Pembe', 'FCSS0900001', 1290, None, False),
    ('2567', 'Yandan Bağlamalı Poplin Gömlek Sarı', 'FCSS0900001', 1290, None, False),
    ('2571', 'Kolu Volanlı Gömlek Sarı', 'FCFW0900001', 1290, None, False),
    ('2571', 'Kolu Volanlı Gömlek Beyaz', 'FCFW0900001', 1290, None, False),
    ('2576', 'Şardonlu Kolsuz Bluz Pantolon Takım Acı Kahve', 'FCFW2000001', 1690, 1440, True),
    ('2577', 'Şardonlu Kolsuz Bluz Pantolon Takıım Antrasit', 'FCFW2000001', 1690, 1440, True),
    ('2579', 'Yarasa Kol Kayık Yaka Bluz Lacivert', 'FCFW0100001', 890, None, False),
    ('2580', 'Yarasa Kol Kayık Yaka Bluz Acı Kahve', 'FCFW0100001', 890, None, False),
    ('2581', 'Yarasa Kol Kayık Yaka Bluz Siyah', 'FCFW0100001', 890, None, False),
    ('2579', 'Yarasa Kol Kayık Yaka Bluz Bej', 'FCFW0100001', 890, None, False),
    ('2585', 'Fermuarlı Oversize Ceket Kırmızı', 'FCFW0400002', 2390, None, False),
    ('2585', 'Fermuarlı Oversize Ceket Ekru', 'FCFW0400002', 2390, None, False),
    ('2590', 'Yandan Bağlamalı Palazzo Pantolon Acı Kahve', 'FCFW1800003', 1490, None, False),
    ('2591', 'Yandan Bağlamalı Palazzo Pantolon Siyah', 'FCFW1800003', 1490, None, False),
    ('2593', 'Havlu Kumaş Pantolon Kırmızı', 'FCFW1800002', 990, None, False),
    ('2593', 'Havlu Kumaş Pantolon Pembe', 'FCFW1800002', 990, None, False),
    ('2595', 'Havlu Kumaş Pantolon Bej', 'FCFW1800002', 990, None, False),
    ('2596', 'Havlu Kumaş Hırka Kırmızı', 'FCFW1000001', 1090, 930, True),
    ('2596', 'Havlu Kumaş Hırka Pembe', 'FCFW1000001', 1090, 820, True),
    ('2596', 'Havlu Kumaş Hırka Bej', 'FCFW1000001', 1090, 930, True),
    ('2599', 'Havlu Kumaş Etek Kırmızı', 'FCFW0700001', 990, None, False),
    ('2600', 'Havlu Kumaş Etek Pembe', 'FCFW0700001', 990, None, False),
    ('2599', 'Havlu Kumaş Etek Bej', 'FCFW0700001', 990, None, False),
    ('2602', 'Dokulu Blazer Etek Takım Beyaz', 'FCFW0600002', 2390, None, False),
    ('2603', 'Şal Detaylı Bluz Haki', 'FCFW0100003', 1490, None, False),
    ('2604', 'Şal Detaylı Bluz Acı Kahve', 'FCFW0100003', 1490, None, False),
    ('2605', 'Şal Detaylı Bluz Siyah', 'FCFW0100003', 1490, None, False),
    ('2606', 'Şal Detaylı Bluz Bej', 'FCFW0100003', 1490, None, False),
    ('2615', 'Çizgili Belden Oturmalı Fular Detay Blazer Acı Kahve', 'FCFW0400003', 2390, None, False),
    ('2615', 'Çizgili Belden Oturmalı Fular Detay Blazer Lacivert', 'FCFW0400003', 2390, None, False),
    ('2616', 'Çizgili Yüksek Bel Palazzo Pantolon Acı Kahve', 'FCFW1800005', 1590, 1110, True),
    ('2617', 'Çizgili Yüksek Bel Palazzo Pantolon Lacivert', 'FCFW1800005', 1490, 1120, True),
    ('2620', 'Oversize Poplin Gömlek Acı Kahve', 'FCFW0900002', 1290, None, False),
    ('2620', 'Oversize Poplin Gömlek Sarı', 'FCFW0900002', 1290, None, False),
    ('2620', 'Oversize Poplin Gömlek Bordo', 'FCFW0900002', 1290, None, False),
    ('2620', 'Oversize Poplin Gömlek Beyaz', 'FCFW0900002', 1290, None, False),
    ('2620', 'Oversize Poplin Gömlek Haki', 'FCFW0900002', 1290, None, False),
    ('2620', 'Oversize Poplin Gömlek Siyah', 'FCFW0900002', 1290, None, False),
    ('2626', 'Kolsuz Piliseli Elbise Siyah', 'FCFW0600003', 1390, 1180, True),
    ('2630', 'Suni Deri Bomber Ceket Bej', 'FCFW0400005', 2390, None, False),
    ('2631', 'Suni Deri Bomber Ceket Gri', 'FCFW0400005', 2390, None, False),
    ('2635', 'Suni Deri Yırtmaç Detay Pantolon Bej', 'FCFW1800007', 1690, None, False),
    ('2645', 'Düz Triko Fular Acı Kahve', 'FCFW2800001', 690, None, False),
    ('2652', 'Apoletli Kısa Kaşe Kaban Siyah', '1440', 2890, None, False),
    ('2661', 'Önü biye Detay Mini Elbise Siyah', 'FCFW0600006', 1590, 1350, True),
    ('2662', 'Önü biye Detay Mini Elbise Gri', 'FCFW0600006', 1590, 1350, True),
    ('2663', 'Önü biye Detay Mini Elbise Acı Kahve', 'FCFW0600006', 1590, 1350, True),
    ('2664', 'Linea Apoletli Uzun Kaşe Kaban Antrasit', 'FCFW1200001', 3290, None, False),
    ('2664', 'Linea Apoletli Uzun Kaşe Kaban Siyah', 'FCFW1200001', 3290, None, False),
    ('2667', 'Polo Yaka Süveter Beyaz', 'FCFW5500001', 1390, None, False),
    ('2667', 'Polo Yaka Süveter Siyah', 'FCFW5500001', 1390, None, False),
    ('2670', 'Limited Edition Pensli Pantolon Siyah', 'FCFW1800008', 1590, 1430, True),
    ('2671', 'Fular Detaylı Kolej Kazak Kırmızı', 'FCFW1300002', 1790, None, False),
    ('2671', 'Fular Detaylı Kolej Kazak Lacivert', 'FCFW1300002', 1790, None, False),
    ('2673', 'Önden Düğmeli Triko Elbise Acı Kahve', 'FCFW0600007', 1590, None, False),
    ('2673', 'Önden Düğmeli Triko Elbise Vizon', 'FCFW0600007', 1590, None, False),
    ('2675', 'Limited Edition Kuşaklı Ceket Ekru', 'FCFW0400006', 2790, None, False),
    ('2675', 'Limited Edition Kuşaklı Ceket Siyah', 'FCFW0400006', 2790, None, False),
    ('2679', 'Nora Kaşe Blazer Kahverengi', 'FCFW0400007', 3290, None, False),
    ('2681', 'Saç Örgü Atkı Acı Kahve', 'FCFW3200002', 1490, None, False),
    ('2681', 'Saç Örgü Atkı Taş', 'FCFW3200002', 1490, None, False),
    ('2681', 'Saç Örgü Atkı Mint', 'FCFW3200002', 1490, None, False),
    ('2685', 'Basic Atkı Acı Kahve', 'FCFW3200001', 1390, None, False),
    ('2690', 'Kadife Yaka Bomber Süet Ceket Taş', 'FCFW0400008', 2790, None, False),
    ('2691', 'Kadife Yaka Bomber Süet Ceket Bordo', 'FCFW0400008', 2790, None, False),
    ('2692', 'Kadife Yaka Bomber Süet Ceket Haki', 'FCFW0400008', 2790, None, False),
    ('2693', 'Mirelle Dik Yaka Kaşe Kaban Ekru', 'FCFW1200003', 3490, None, False),
    ('2696', 'Basic Uzun Kol Triko Kazak Acı Kahve', 'FCFW1300005', 1490, None, False),
    ('2698', 'Basic Uzun Kol Triko Kazak Beyaz', 'FCFW1300005', 1490, None, False),
    ('2699', 'Basic Kısa Kol Triko Kazak Acı Kahve', 'FCFW1300004', 1390, None, False),
    ('2701', 'Basic Kısa Kol Triko Kazak Beyaz', 'FCFW1300004', 1390, None, False),
    ('2702', 'Carrot Fit Pantolon Acı Kahve', 'FCFW1800009', 1690, 1440, True),
    ('2703', 'Mini Etek Acı Kahve', 'FCFW0700003', 1090, 930, True),
    ('2704', 'Yakasız Belden Oturtmalı Ceket Acı Kahve', 'FCFW0400009', 2890, None, False),
    ('2707', 'Volanlı Bluz Bordo', 'FCFW0100006', 1690, 1520, True),
    ('2708', 'Ekose Fular Mavi', 'FCFW2800002', 690, None, False),
    ('2708', 'Ekose Fular Kahverengi', 'FCFW2800002', 690, None, False),
    ('2708', 'Ekose Fular Yeşil', 'FCFW2800002', 690, None, False),
    ('2713', 'Clair Ekose Gömlek Yeşil', 'FCFW0900004', 1690, None, False),
    ('2714', 'Clair Ekose Gömlek Mavi', 'FCFW0900004', 1690, None, False),
    ('2720', 'Dik Yaka Basic Body Bej', 'FCSS0200001', 690, None, False),
    ('2724', 'Dik Yaka Zıbınlı Sweatshirt Siyah', 'FCSS3300002', 1590, None, False),
    ('2725', 'Polo Yaka Zıbınlı Sweatshirt Kırmızı', 'FCSS3300001', 1690, None, False),
    ('2726', 'Polo Yaka Zıbınlı Sweatshirt Ekru', 'FCSS3300001', 1690, None, False),
    ('2727', 'Polo Yaka Zıbınlı Sweatshirt Siyah', 'FCSS3300001', 1690, None, False),
    ('2729', 'Vela Kareli Midi Etek Bej', 'FCSS0700001', 1290, None, False),
    ('2737', 'Pamuklu Transparan Kayık Yaka Body Acı Kahve', 'FCSS0200003', 790, None, False),
    ('2737', 'Pamuklu Transparan Kayık Yaka Body Beyaz', 'FCSS0200003', 790, None, False),
    ('2737', 'Pamuklu Transparan Kayık Yaka Body Siyah', 'FCSS0200003', 790, None, False),
    ('2740', 'Pamuklu Transparan Dik Yaka Body Ekru', 'FCSS0200002', 790, None, False),
    ('2740', 'Pamuklu Transparan Dik Yaka Body Siyah', 'FCSS0200002', 790, None, False),
    ('2743', 'Omuz Dekolteli Bluz Siyah', 'FCSS0100002', 1590, None, False),
    ('2744', 'Omuz Dekolteli Bluz Mavi', 'FCSS0100002', 1590, None, False),
    ('2749', 'Fularlı Belden Oturtma Gömlek Mavi', 'FCSS0900003', 1390, None, False),
    ('2753', 'Lumen Kısa Ekose Şort Kahverengi', 'FCSS2700001', 1090, None, False),
    ('2755', 'Uzun Kol Basic Tişört Siyah', 'FCSS2100001', 890, 760, True),
    ('2766', 'Nira Şortlu Mini Etek Siyah', 'FCSS0700002', 1190, 1010, True),
    ('2767', 'Lumen Vatkalı Ekose Ceket Kahverengi', 'FCSS0400005', 2690, None, False),
    ('2769', 'Pulse Crop Sweatshirt Siyah', 'FCSS3300003', 1590, None, False),
    ('2770', 'Sienna Pileli Pantolon Vizon', 'FCSS1800005', 1590, 1350, True),
    ('2771', 'Sienna Pileli Pantolon Siyah', 'FCSS1800005', 1590, 1350, True),
    ('2772', 'Sienna Pileli Pantolon Bej', 'FCSS1800005', 1590, 1350, True),
    ('2773', 'Dot Puantiyeli Gömlek Beyaz', 'FCSS0900005', 1490, 1270, True),
    ('2784', 'Knot Bağlamalı Gömlek Açık Mavi', 'FCSS0900006', 1790, None, False),
    ('2784', 'Knot Bağlamalı Gömlek Ekru', 'FCSS0900006', 1790, None, False),
    ('2785', 'Suni Deri Midi Etek Siyah', 'FCSS0700003', 1690, 1440, True),
    ('2790', 'Core Dik Yaka Kısa Kollu T-Shirt Kırmızı', 'FCSS2100002', 1190, None, False),
    ('2791', 'Core Dik Yaka Kısa Kollu T-Shirt Sarı', 'FCSS2100002', 1190, None, False),
    ('2792', 'Core Dik Yaka Kısa Kollu T-Shirt Ekru', 'FCSS2100002', 1190, None, False),
    ('2793', 'Core Dik Yaka Kısa Kollu T-Shirt Siyah', 'FCSS2100002', 1190, None, False),
    ('2794', 'Core Dik Yaka Kısa Kollu T-Shirt Bej', 'FCSS2100002', 1190, None, False),
    ('2795', 'Core Dik Yaka Kısa Kollu T-Shirt Mavi', 'FCSS2100002', 1190, None, False),
    ('2802', 'Suni Deri High Waist Pantolon Siyah', 'FCSS1800010', 1690, 1440, True),
    ('2803', 'Kareli Dökümlü Pantolon Siyah', 'FCSS1800009', 1690, 1440, True),
    ('2815', 'Rufe Kruvaze Oversize Blazer Açık Gri', 'FCSS0400008', 2590, None, False),
    ('2817', 'Lumea Desenli Kısa Ceket Çok Renkli', 'FCSS0400007', 2690, None, False),
    ('2818', 'Motion Paraşüt Ceket Şort Takım Kırmızı', 'FCSS2000001', 2090, 1880, True),
    ('2822', 'Nina Mini Etek Siyah', 'FCSS0700004', 1090, 930, True),
    ('2696', 'Basic Uzun Kol Triko Kazak Siyah', 'FCFW1300005', 1490, None, False),
    ('1967', 'Kol Apoletli Rüzgarlıklı Oversize Trençkot Acı Kahve', '48289894', 2990, None, False),
    ('1968', 'Acı Kahve Düğme Detay Yüksek Bel Palazzo Pantolon Acı Kahve', '77887783', 1490, 1270, True),
    ('1969', 'Siyah Düğme Detay Yüksek Bel Palazzo Pantolon Siyah', '77887783', 1490, 1270, True),
    ('2053', 'Beli Lastikli Midi Etek Ekru', '2025140002', 1390, None, False),
    ('2053', 'Kahverengi Beli Lastikli Midi Etek Kahverengi', '2025140002', 1390, None, False),
    ('2136', 'Kol Apoletli Rüzgarlıklı Oversize Trençkot Siyah', '1967', 2990, None, False),
    ('2136', 'Kol Apoletli Rüzgarlıklı Oversize Trençkot Bej', '1967', 2990, None, False),
    ('2576', 'Şardonlu Kolsuz Bluz Pantolon Takım Bej', 'FCFW2000001', 1690, 1440, True),
    ('2588', 'Önden Bağlamalı Fırfırlı Bluz Sarı', 'FCFW0100002', 1690, None, False),
    ('2588', 'Önden Bağlamalı Fırfırlı Bluz Ekru', 'FCFW0100002', 1690, None, False),
    ('2632', 'Suni Deri Bomber Ceket Kahverengi', 'FCFW0400005', 2390, None, False),
    ('2633', 'Suni Deri Yırtmaç Detay Pantolon Kahverengi', 'FCFW1800007', 1690, None, False),
    ('2634', 'Suni Deri Yırtmaç Detay Pantolon Gri', 'FCFW1800007', 1690, None, False),
    ('2640', 'Asimetrik Kesim Triko Panço Bordo', 'FCFW3100001', 1190, 1010, True),
    ('2645', 'Düz Triko Fular Kırmızı', 'FCFW2800001', 690, None, False),
    ('2656', 'Şal Detay Saten Mini Elbise Bordo', 'FCFW0600005', 1890, 1610, True),
    ('2664', 'Linea Apoletli Uzun Kaşe Kaban Acı Kahve', 'FCFW1200001', 3290, None, False),
    ('2669', 'Limited Edition Pensli Pantolon Ekru', 'FCFW1800008', 1590, 1430, True),
    ('2684', 'Basic Atkı Kırmızı', 'FCFW3200001', 1390, None, False),
    ('2686', 'Basic Atkı Taş', 'FCFW3200001', 1490, None, False),
    ('2707', 'Volanlı Bluz Ekru', 'FCFW0100006', 1690, 1520, True),
    ('2707', 'Volanlı Bluz Siyah', 'FCFW0100006', 1690, None, False),
    ('2710', 'Ekose Fular Kırmızı', 'FCFW2800002', 690, None, False),
    ('2712', 'Noir Toggle Ceket Acı Kahve', 'FCFW0400010', 3090, None, False),
    ('2716', 'Dik Yaka Basic Body Acı Kahve', 'FCSS0200001', 690, None, False),
    ('2720', 'Dik Yaka Basic Body Lacivert', 'FCSS0200001', 690, None, False),
    ('2719', 'Dik Yaka Basic Body Siyah', 'FCSS0200001', 690, None, False),
    ('2721', 'Dik Yaka Zıbınlı Sweatshirt Kırmızı', 'FCSS3300002', 1590, None, False),
    ('2722', 'Dik Yaka Zıbınlı Sweatshirt Ekru', 'FCSS3300002', 1590, None, False),
    ('2723', 'Dik Yaka Zıbınlı Sweatshirt Vizon', 'FCSS3300002', 1590, None, False),
    ('2728', 'Vela Kareli Midi Etek Acı Kahve', 'FCSS0700001', 1290, None, False),
    ('2730', 'Scarlet Gabardin Oversize Ceket Ekru', 'FCSS0400002', 3090, None, False),
    ('2738', 'Pamuklu Transparan Dik Yaka Body Acı Kahve', 'FCSS0200002', 790, None, False),
    ('2742', 'Accent Nakışlı Oversize Gömlek Kırmızı', 'FCSS0900002', 1690, None, False),
    ('2747', 'Fularlı Belden Oturtma Gömlek Bordo', 'FCSS0900003', 1390, None, False),
    ('2748', 'Fularlı Belden Oturtma Gömlek Haki', 'FCSS0900003', 1390, None, False),
    ('2750', 'Cue Süet Kısa Ceket Acı Kahve', 'FCSS0400004', 2190, None, False),
    ('2760', 'Nova Pelerinli Gömlek Turuncu', 'FCSS0900004', 1790, 1520, True),
    ('2761', 'Nova Pelerinli Gömlek Açık Mavi', 'FCSS0900004', 1790, 1520, True),
    ('2762', 'Nova Pelerinli Gömlek Ekru', 'FCSS0900004', 1790, 1520, True),
    ('2764', 'Nira Şortlu Mini Etek Açık Gri', 'FCSS0700002', 1190, 1010, True),
    ('2765', 'Nira Şortlu Mini Etek Acı Kahve', 'FCSS0700002', 1190, 1010, True),
    ('2768', 'Pulse Crop Sweatshirt Antrasit', 'FCSS3300003', 1590, None, False),
    ('2779', 'Frame Çıtçıtlı Bluz Acı Kahve', 'FCSS0100003', 1690, 1440, True),
    ('2779', 'Frame Çıtçıtlı Bluz Siyah', 'FCSS0100003', 1690, 1440, True),
    ('2780', 'Sculpt Balon Pensli Pantolon Beyaz', 'FCSS1800006', 1690, 1440, True),
    ('2781', 'Sculpt Balon Pensli Pantolon Vizon', 'FCSS1800006', 1690, 1440, True),
    ('2782', 'Sculpt Balon Pensli Pantolon Bej', 'FCSS1800006', 1690, 1440, True),
    ('2788', 'Slim Fit Askılı Bodysuit Ekru', 'FCSS0200005', 1190, 1010, True),
    ('2796', 'Pensli Straight Fit Pantolon Acı Kahve', 'FCSS1800008', 1690, 1440, True),
    ('2797', 'Pensli Straight Fit Pantolon Taş', 'FCSS1800008', 1690, 1440, True),
    ('2798', 'Pensli Straight Fit Pantolon Siyah', 'FCSS1800008', 1690, 1440, True),
    ('2799', 'Pileli Balon Kesim Pantolon Acı Kahve', 'FCSS1800007', 1790, 1520, True),
    ('2800', 'Pileli Balon Kesim Pantolon Ekru', 'FCSS1800007', 1790, 1520, True),
    ('2801', 'Pileli Balon Kesim Pantolon Siyah', 'FCSS1800007', 1790, 1520, True),
    ('2803', 'Kareli Dökümlü Pantolon Lacivert', 'FCSS1800009', 1690, 1440, True),
    ('2808', 'Rufe Dökümlü Palazzo Pantolon Açık Gri', 'FCSS1800011', 1590, None, False),
    ('2808', 'Rufe Dökümlü Palazzo Pantolon Vizon', 'FCSS1800011', 1590, None, False),
    ('2809', 'Rive Vatkalı Gömlek Acı Kahve', 'FCSS0900007', 1490, None, False),
    ('2812', 'Rive Vatkalı Gömlek Bordo', 'FCSS0900007', 1490, None, False),
    ('2812', 'Rive Vatkalı Gömlek Ekru', 'FCSS0900007', 1490, None, False),
    ('2812', 'Rive Vatkalı Gömlek Siyah', 'FCSS0900007', 1490, None, False),
    ('2813', 'Alen Askılı Midi Elbise Bordo', 'FCSS0600002', 1890, None, False),
    ('2814', 'Alen Askılı Midi Elbise Siyah', 'FCSS0600002', 1890, 1800, True),
    ('2816', 'Rufe Kruvaze Oversize Blazer Vizon', 'FCSS0400008', 2590, None, False),
    ('2819', 'Motion Paraşüt Ceket Şort Takım Siyah', 'FCSS2000001', 2090, 1880, True),
    ('2821', 'Suni Deri Yakalı Ceket Bej', 'FCSS0400009', 2690, None, False),
    ('2821', 'Suni Deri Yakalı Ceket Mavi', 'FCSS0400009', 2690, None, False),
    ('2825', 'Era Baget Çanta Acı Kahve', 'FCSS2500001', 1590, None, False),
    ('2826', 'Era Baget Çanta Ekru', 'FCSS2500001', 1590, None, False),
    ('2825', 'Era Baget Çanta Siyah', 'FCSS2500001', 1590, None, False),
    ('2828', 'Tina Straight Fit Jean Mavi', 'FCSS1100001', 2290, None, False),
    ('2699', 'Basic Kısa Kol Triko Kazak Siyah', 'FCFW1300004', 1390, None, False),
    ('2838', 'Liora Midi Keten Elbise Lacivert', 'FCSS0600004', 2390, 1670, True),
    ('2839', 'Velora Dantelli Saten Takım Haki', 'FCSS2000002', 2190, 2080, True),
    ('2840', 'Velora Dantelli Saten Takım Bej', 'FCSS2000002', 2190, 2080, True),
    ('2841', 'Liora Crop Etek Takım Siyah', 'FCSS2000003', 2290, 1720, True),
    ('2844', 'Norya Mini Şort Acı Kahve', 'FCSS2700002', 1190, 950, True),
    ('2845', 'Delya Gömlek Şort Takım Beyaz', 'FCSS2000004', 2290, 1950, True),
    ('2845', 'Delya Gömlek Şort Takım Haki', 'FCSS2000004', 2290, 1950, True),
    ('2847', 'Helia Dökümlü Pantolon Acı Kahve', 'FCSS1800013', 1390, 1180, True),
    ('2848', 'Helia Dökümlü Pantolon Kiremit', 'FCSS1800013', 1390, 1180, True),
    ('2849', 'Helia Dökümlü Pantolon Ekru', 'FCSS1800013', 1390, 1180, True),
    ('2852', 'Helia Oversize Gömlek Acı Kahve', 'FCSS0900008', 1390, 1320, True),
    ('2852', 'Helia Oversize Gömlek Kiremit', 'FCSS0900008', 1390, 1320, True),
    ('2854', 'Helia Oversize Gömlek Ekru', 'FCSS0900008', 1390, 1320, True),
    ('2857', 'Selis Sırt Dekolteli Mini Elbise Siyah', 'FCSS0600006', 1490, 1120, True),
    ('2861', 'Norya Fularlı Ceket Acı Kahve', 'FCSS0400010', 1890, 1800, True),
    ('2864', 'Aluna Volanlı Tulum Siyah', 'FCFW2300001', 1790, 1610, True),
    ('2865', 'Aluna Volanlı Tulum Bej', 'FCFW2300001', 1790, 1610, True),
    ('2872', 'Evrin Modal Bluz Etek Takım Ekru', 'FCSS2000005', 1890, 1610, True),
    ('2874', 'Taven Çizgili Gömlek Bej', 'FCSS0900009', 1490, 1270, True),
    ('2874', 'Taven Çizgili Gömlek Mavi', 'FCSS0900009', 1490, 1270, True),
    ('2876', 'Metal Aksesuarlı Kalem Etek Siyah', 'FCSS0700005', 1590, None, False),
    ('2879', 'Metal Aksesuarlı Crop Blazer Ceket Siyah', 'FCSS0400012', 2590, None, False),
    ('2880', 'Lace Midi Pelerin Sarı', 'FCSS3400001', 1690, 1350, True),
    ('2882', 'Mini Bermuda Şort Acı Kahve', 'FCSS2700004', 1190, 1070, True),
    ('2883', 'Mini Bermuda Şort Ekru', 'FCSS2700004', 1190, 1130, True),
    ('2884', 'Mini Bermuda Şort Siyah', 'FCSS2700004', 1190, 1130, True),
    ('2885', 'Olisa Büstiyer Pantolon Takım Acı Kahve', 'FCSS2000007', 2290, 2060, True),
    ('2887', 'Lace Crop Pantolon Takım Sarı', 'FCSS2000006', 1990, 1890, True),
    ('2889', 'Monarc Bluz Pantolon Takım Ekru', 'FCSS2000008', 2590, 2330, True),
    ('2896', 'Maren Bermuda Şortolon Siyah', 'FCSS3500001', 1390, 1180, True),
    ('2897', 'Maren Bermuda Şortolon Bej', 'FCSS3500001', 1390, 1180, True),
    ('2902', 'Maren Oversize Ceket Siyah', 'FCSS0400013', 2590, 2200, True),
    ('2903', 'Maren Oversize Ceket Bej', 'FCSS0400013', 2590, 2200, True),
    ('2904', 'Terra Pamuklu Bağlamalı Kimono Beyaz', 'FCSS1400001', 2090, 1880, True),
    ('2911', 'Flora Dantel Bandana Beyaz', 'FCSS3600002', 690, 480, True),
    ('2912', 'Daisy Dantel Bandana Beyaz', 'FCSS3600001', 690, 480, True),
    ('2679', 'Nora Kaşe Blazer Mürdüm', 'FCFW0400007', 3290, None, False),
    ('2715', 'Dik Yaka Basic Body Off White', 'FCSS0200001', 690, None, False),
    ('2751', 'Noctia Pelerinli Ceket Somon', 'FCSS0400003', 2690, None, False),
    ('2754', 'Uzun Kol Basic Tişört Off White', 'FCSS2100001', 890, 760, True),
    ('2893', 'Stella Panço Etek Takım Açık Yeşil', 'FCSS2000010', 2090, 1990, True),
    ('2898', 'Nerina Oversize Gömlek Antrasit', 'FCSS0900010', 1490, 1270, True),
    ('2900', 'Nerina Pareo Etek Antrasit', 'FCSS0700006', 1390, 1320, True),
    ('2905', 'Mira Dantelli Mini Etek Ekru', 'FCSS0700007', 1290, 1230, True),
    ('2907', 'Mira Balenli Dantelli Büstiyer Ekru', 'FCSS0300001', 1690, 1610, True),
    ('2914', 'Celeste Dantelli Uzun Elbise Acı Kahve', 'FCSS0600010', 1890, 1700, True),
    ('2909', 'Yüksek Bel Kapri Pantolon Acı Kahve', 'FCSS1800001', 1390, None, False),
    ('2910', 'Yüksek Bel Kapri Pantolon Ekru', 'FCSS1800001', 1390, None, False),
    ('2828', 'Tina Straight Fit Jean Buz Mavisi', 'FCSS1100001', 2290, 1830, True),
    ('2873', 'Evrin Modal Bluz Etek Takım Lacivert', 'FCSS2000005', 1890, 1610, True),
    ('2886', 'Olisa Büstiyer Pantolon Takım Antrasit', 'FCSS2000007', 2290, 2060, True),
    ('2890', 'Monarc Bluz Pantolon Takım Siyah', 'FCSS2000008', 2590, 2330, True),
    ('2899', 'Nerina Oversize Gömlek Beyaz', 'FCSS0900010', 1490, 1270, True),
    ('2752', 'Noctia Pelerinli Ceket Mavi', 'FCSS0400003', 2690, None, False),
    ('2828', 'Tina Straight Fit Jean Ekru', 'FCSS1100001', 2290, None, False),
    ('2901', 'Nerina Pareo Etek Beyaz', 'FCSS0700006', 1390, 1320, True),
    ('2906', 'Mira Dantelli Mini Etek Siyah', 'FCSS0700007', 1290, 1160, True),
    ('2908', 'Mira Balenli Dantelli Büstiyer Siyah', 'FCSS0300001', 1690, 1520, True),
    ('2915', 'Celeste Dantelli Uzun Elbise Bej', 'FCSS0600010', 1890, 1700, True),
    ('2977', 'Azura Biyeli Kimono Şort Takım Beyaz', 'FCSS2000012', 3290, None, False),
    ('2978', 'Müslin Madonna Yaka Bluz Pantolon Takım Beyaz', 'FCSS2000011', 2190, 2080, True),
    ('2979', 'Shape Crop Fit Pantolon Siyah', 'FCSS1800015', 1790, None, False),
    ('2980', 'Mold Balon Pantolon Siyah', 'FCSS1800014', 1690, None, False),
    ('2981', 'Mold Balon Pantolon Bej', 'FCSS1800014', 1690, None, False),
    ('2982', 'Siyah Bermuda Şort', 'FCSS2700004', 1090, 1040, True),
    ('2983', 'Polka Puantiye Takım Beyaz', 'FCSS983310', 2490, 2120, True),
    ('2984', 'Colette Puantiyeli Elbise Beyaz', 'FCSS648936', 2290, 1950, True),
    ('2985', 'Alba V Yaka Uzun Triko Elbise Siyah', 'FCSS248000', 1990, 1690, True),
    ('2986', 'Alba V Yaka Uzun Triko Elbise Krem', 'FCSS248000', 1990, 1690, True),
    ('2987', 'Maris Mini Triko Elbise Siyah', 'FCSS0600012', 1990, 1490, True),
    ('2988', 'Maris Mini Triko Elbise Ekru', 'FCSS0600012', 1990, 1490, True),
    ('2989', 'Nola Yakasız Trençkot Bej', 'FCFW859466', 2390, None, False),
    ('2990', 'Vesper Ekose Kısa Ceket Kahverengi', 'FCFW814344', 3890, None, False),
    ('2991', 'Vesper Ekose Kısa Ceket Siyah', 'FCFW814344', 3890, None, False),
    ('2992', 'Bond Pötikare Kısa Ceket Kahverengi', 'FCFW118909', 3190, None, False),
    ('2993', 'Silva Dik Yaka Gömlek  Beyaz', 'FCFW983712', 1690, None, False),
    ('2763', 'Nova Pelerinli Gömlek Lacivert', 'FCSS0900004', 1790, 1520, True),
    ('2995', 'Amora Biyeli Elbise  Siyah', 'FCFW631530', 2690, None, False),
    ('2996', 'Elara Slim Fit Ceket  Haki', 'FCFW140904', 2990, None, False),
    ('2997', 'Elara Mini Etek Haki', 'FCFW179183', 1190, None, False),
    ('2998', 'Noble Yakası Deri Ceket Bej', 'FCFW891462', 2990, None, False),
    ('2999', 'Noble Yakası Deri Ceket Mürdüm', 'FCFW891462', 2990, None, False),
    ('3003', 'Carmen Düğmeli Ceket Siyah', 'FCFW274696', 3290, None, False),
    ('3004', 'Vesta Çıtçıtlı Trençkot Bej', 'FCFW628283', 2690, None, False),
    ('3007', 'Willow Knot Bağlamalı Gömlek Mavi', 'FCFW170928', 1690, None, False),
    ('2658', 'Şal Detay Saten Mini Elbise Siyah', 'FCFW0600005', 1890, None, False),
    ('3008', 'Scarlet Gabardin Oversize Ceket Siyah', 'FCSS0400002', 3090, None, False),
    ('3009', 'Aster Kemerli Ekose Elbise Mavi', 'FCFW113844', 2490, None, False),
    ('3010', 'Aria Vatkalı Kısa Ceket Siyah', 'FCFW463522', 3590, None, False),
    ('3011', 'Aria Vatkalı Kısa Ceket Acı Kahve', 'FCFW463522', 3590, None, False),
    ('3016', 'Vera Şort Etek Bej', 'FCFW109891', 1490, None, False),
    ('3017', 'Vera Şort Etek Acı Kahve', 'FCFW109891', 1490, None, False),
    ('3018', 'Vera Vatkalı Ceket Acı Kahve', 'FCFW920068', 2490, None, False),
    ('3019', 'Vera Vatkalı Ceket Bej', 'FCFW920068', 2490, None, False),
    ('3020', 'Dora Yarasa Kol Ceket Bej', 'FCFW395169', 2690, None, False),
    ('3021', 'Dora Yarasa Kol Ceket Acı Kahve', 'FCFW395169', 2690, None, False),
    ('3022', 'Dora Mini Etek Bej', 'FCFW832487', 1490, None, False),
    ('3023', 'Dora Mini Etek Acı Kahve', 'FCFW832487', 1490, None, False),
    ('3025', 'Kaia Uzun Şişme Mont Siyah', 'FCFW375842', 4290, None, False),
    ('3026', 'Arden Kısa Bomber Mont Kırmızı', 'FCFW992112', 3890, None, False),
    ('3027', 'Arden Kısa Bomber Mont Siyah', 'FCFW992112', 3890, None, False),
    ('3029', 'Aden Vatkalı Polo Yaka Basic Gri', 'FCFW165372', 1690, None, False),
    ('3030', 'Aden Vatkalı Polo Yaka Basic Mavi', 'FCFW165372', 1690, None, False),
    ('3031', 'Aden Vatkalı Polo Yaka Basic Siyah', 'FCFW165372', 1690, None, False),
    ('3035', 'Elen Yarasa Kol Basic Beyaz', 'FCFW994865', 1190, None, False),
    ('3036', 'Elen Yarasa Kol Basic Siyah', 'FCFW994865', 1190, None, False),
    ('3037', 'Alya Şeritli Bomber Ceket', 'FCFW762441', 3290, None, False),
]


def _f(v) -> float:
    try:
        return float(str(v).replace(",", ".")) if v not in (None, "") else 0.0
    except Exception:
        return 0.0


async def _find(db, kid, name, sc):
    proj = {"_id": 0, "id": 1, "name": 1, "price": 1, "sale_price": 1, "member_price_1": 1,
            "variants": 1, "categories": 1, "category_ids": 1, "category_id": 1}
    nm = name.strip().casefold()
    for q in ({"urun_karti_id": {"$in": [kid, int(kid)] if str(kid).isdigit() else [kid]}}, {"stock_code": sc}):
        c = [p async for p in db.products.find(dict(q, is_deleted={"$ne": True}), proj)
             if str(p.get("name") or "").strip().casefold() == nm]
        if len(c) == 1:
            return c[0]
    return None


async def apply(db) -> dict:
    if ((await db.settings.find_one({"id": FLAG}, {"_id": 0, "done": 1})) or {}).get("done"):
        return {"skipped": "done"}
    now = datetime.now(timezone.utc).isoformat()
    changed, missing, added, removed = [], [], 0, 0
    for kid, name, sc, price, sale, evet in PLAN:
        p = await _find(db, kid, name, sc)
        if not p:
            missing.append(f"{kid} {name}")
            continue
        price = float(price)
        sale = float(sale) if (sale and 0 < float(sale) < price) else None
        st = {"price": price, "sale_price": sale, "updated_at": now}
        if _f(p.get("member_price_1")) <= 0 and _f(p.get("price")) > 0:
            st["member_price_1"] = _f(p.get("price"))
        vs, touched = [], 0
        for v in (p.get("variants") or []):
            if isinstance(v, dict) and (v.get("price") not in (None, "", 0) or v.get("sale_price") not in (None, "", 0)):
                v = dict(v)
                adj = _f(v.get("price_adjustment") or v.get("price_diff"))
                v["price"] = round(price + adj, 2)
                v["sale_price"] = round(sale + adj, 2) if sale else None
                touched += 1
            vs.append(v)
        if touched:
            st["variants"] = vs
        cats = [str(x) for x in (p.get("categories") or [])]
        cids = [str(x) for x in (p.get("category_ids") or [])]
        in_sale = SALE_CAT in cats or SALE_CAT in cids
        cat_op = ""
        if evet and not in_sale:
            st["categories"] = cats + [SALE_CAT]
            st["category_ids"] = cids + [SALE_CAT]
            cat_op = "eklendi"
            added += 1
        elif not evet and in_sale:
            st["categories"] = [x for x in cats if x != SALE_CAT]
            st["category_ids"] = [x for x in cids if x != SALE_CAT]
            if str(p.get("category_id") or "") == SALE_CAT:
                st["category_id"] = (st["categories"] or st["category_ids"] or [None])[0]
            cat_op = "çıkarıldı"
            removed += 1
        await db.products.update_one({"id": p["id"]}, {"$set": st})
        changed.append({"id": p["id"], "ad": name, "eski": [p.get("price"), p.get("sale_price")],
                        "yeni": [price, sale], "indirim_kat": cat_op})
    res = {"id": FLAG, "done": True, "at": now, "changed": len(changed), "missing": missing,
           "indirim_eklenen": added, "indirim_cikarilan": removed, "items": changed}
    await db.settings.update_one({"id": FLAG}, {"$set": res}, upsert=True)
    return {k: res[k] for k in ("changed", "missing", "indirim_eklenen", "indirim_cikarilan")}


MEMBER_FLAG = "fiyat_yukleme_0929_uye_tipi"


async def apply_member_price(db) -> dict:
    """TEK SEFERLİK (Kadir isteği 2026-09-29): aynı listenin SATIŞ FİYATI Üye Tipi Fiyatı 1'e
    (member_price_1) de yazılır — ürün ve fiyat alanı olan varyantlarda (fiyat farkı eklenir).
    BİLEREK: pazaryeri tabanı bu alandır → Trendyol/Amazon/HB sonraki senkronda bu fiyattan."""
    if ((await db.settings.find_one({"id": MEMBER_FLAG}, {"_id": 0, "done": 1})) or {}).get("done"):
        return {"skipped": "done"}
    now = datetime.now(timezone.utc).isoformat()
    changed, missing = 0, []
    for kid, name, sc, price, _sale, _evet in PLAN:
        p = await _find(db, kid, name, sc)
        if not p:
            missing.append(f"{kid} {name}")
            continue
        upd = {"member_price_1": float(price), "updated_at": now}
        vs, touched = [], 0
        for v in (p.get("variants") or []):
            if isinstance(v, dict) and (_f(v.get("member_price_1")) > 0 or v.get("price") not in (None, "", 0)
                                        or v.get("sale_price") not in (None, "", 0)):
                v = dict(v)
                v["member_price_1"] = round(float(price) + _f(v.get("price_adjustment") or v.get("price_diff")), 2)
                touched += 1
            vs.append(v)
        if touched:
            upd["variants"] = vs
        await db.products.update_one({"id": p["id"]}, {"$set": upd})
        changed += 1
    await db.settings.update_one({"id": MEMBER_FLAG}, {"$set": {"id": MEMBER_FLAG, "done": True, "at": now,
                                 "changed": changed, "missing": missing}}, upsert=True)
    return {"changed": changed, "missing": missing}
