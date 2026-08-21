# Tasarım: Brifing E-postalarına Haber Görselleri

**Tarih:** 2026-08-21
**Durum:** Onaylandı, uygulamaya hazır
**Dosya:** `cti_automation.py` (macOS ve Linux kopyaları senkron tutulur)

## Amaç

Günlük CTI tehdit brifingi e-postalarında, her haberin kendi görselini
göstermek. Amaç e-postayı görsel olarak daha ilgi çekici kılmak.

## Kapsam

**Dahil:** Öncelik puanına göre en kritik 10 makale, Gemini tarafından analiz
edilen brifing bloklarının içinde tam genişlik banner görsel.

**Hariç (YAGNI):** Taşma (overflow) tablosundaki makaleler, görsel galerisi,
kullanıcıya görsel açma/kapama ayarı.

## Kararlar ve Gerekçeleri

| Karar | Seçim | Gerekçe |
|---|---|---|
| Teslim yöntemi | CID gömme | Her istemcide garantili görünür; hotlink Outlook'ta engellenir, takip sızıntısı yaratır ve kaynak silinince bozulur. Kardeş proje (siber-farkindalik-bulteni) de bu deseni kullanıyor. |
| Enjeksiyon | Placeholder token | Sanitizer whitelist'i genişletmeden görsel eklemeyi mümkün kılar (aşağıda Güvenlik). |
| Kapsam | İlk 10 makale | Mail boyutunu öngörülebilir tutar; advisory kaynakları zaten çoğunlukla görselsiz. |
| Optimizasyon | Pillow ile yeniden boyutlandırma | Ham byte limiti kaliteyi keser. Ölçekleme hem daha iyi kalite hem daha küçük boyut verir; ayrıca WebP→JPEG ve EXIF temizliği sağlar. |

## Mimari

### Veri akışı

```
fetch_all_feeds()
   ↓  (RSS entry'lerinden görsel adayı da toplanır)
match_articles()  → öncelik puanına göre sıralı liste
   ↓
build_prompt()    → makale sayfaları çekilir (og:image aynı yanıttan okunur)
   ↓
analyze_with_gemini()  → HTML brifing ([[IMG:n]] token'ları içerir)
   ↓
sanitize_gemini_html() → whitelist AYNEN korunur
   ↓
inject_images()   → token'lar kod üretimi <img src="cid:..."> ile değiştirilir
   ↓
send_email(..., images=[...])  → MIMEMultipart("related") ile gömülür
```

### Bileşenler

**1. Görsel adayı bulma**

İki kaynak, öncelik sırasıyla:

1. **RSS metadata** (ek istek yok): `media:thumbnail`, `media:content`,
   `enclosure` / `links[rel=enclosure]` (type `image/*`), ve son çare olarak
   içerik HTML'indeki ilk `<img src>`.
2. **`og:image`** (ek istek yok): Makale sayfaları versiyon çıkarma için
   **zaten indiriliyor**. `fetch_article_body` → `fetch_article_page` olarak
   yeniden düzenlenir ve `(body_text, og_image_url)` döndürür. Böylece görsel
   için ikinci bir HTTP isteği atılmaz.

Göreceli URL'ler (`/img/a.jpg`) makale linkine göre mutlak hale getirilir.

**2. İndirme ve doğrulama** — yeni fonksiyon

Her aday için sırayla:

- Şema `http`/`https` olmalı
- `_SSRF_BLOCKED` kontrolü **hem istek öncesi hem redirect sonrası nihai URL
  üzerinde** (redirect ile iç ağa yönlenme bilinen bypass yöntemi)
- `max_redirects = 3`, timeout 8 sn, `stream=True`
- Content-Type beyaz listesi: `image/jpeg`, `image/png`, `image/gif`,
  `image/webp`. **`image/svg+xml` kesinlikle reddedilir** (script taşıyabilir)
- Akış sırasında `MAX_DOWNLOAD_BYTES` aşılırsa indirme kesilir ve aday atlanır
- **Magic byte doğrulaması**: Content-Type başlığına güvenilmez, dosyanın
  gerçek imzası kontrol edilir

**3. E-posta için optimizasyon (Pillow)**

- `Image.MAX_IMAGE_PIXELS` açıkça sınırlanır (decompression bomb koruması);
  aşılırsa görsel atlanır
- Genişlik `IMAGE_TARGET_WIDTH`'ten büyükse orantılı küçültülür
  (küçük görseller **büyütülmez**)
- JPEG olarak `IMAGE_JPEG_QUALITY` ile yeniden kodlanır; şeffaflık içeren
  görseller beyaz zemine yerleştirilir
- Sonuç EXIF'siz olur (metadata sızıntısı önlenir)
- Animasyonlu GIF'ler JPEG'e çevrildiği için **ilk kareye** düşer; bu kabul
  edilen bir davranıştır (e-posta istemcilerinde animasyon zaten güvenilmez)
- Görseller **makale öncelik sırasıyla** işlenir; optimizasyon sonrası toplam
  `MAX_TOTAL_IMAGE_BYTES`'ı aşarsa kalan görseller eklenmez. Böylece bütçe
  dolduğunda en kritik haberlerin görselleri korunmuş olur

**4. Enjeksiyon**

- `SYSTEM_PROMPT`: her brifing bloğunda `<h3>` başlığın hemen altına
  `[[IMG:n]]` yazma talimatı eklenir (`n` = prompt'taki makale numarası)
- `inject_images(html, cid_map)`:
  - Eşleşen token → `<img src="cid:imgN" style="width:100%;height:auto;
    border-radius:4px;margin:8px 0;" alt="...">`
  - `alt` metni makale başlığından üretilir ve HTML-escape edilir
  - Eşleşmeyen tüm `[[IMG:*]]` token'ları **silinir** (görselsiz makaleler ve
    Gemini'nin fazladan yazdığı token'lar)

**5. MIME yapısı**

Görsel varsa:

```
MIMEMultipart("related")
├── MIMEMultipart("alternative")
│   └── MIMEText(html, "html", "utf-8")
└── MIMEImage(data, "jpeg")  × N     Content-ID: <imgN>, inline

`N`, placeholder'daki makale numarasıyla **birebir aynıdır** (`[[IMG:3]]` →
`Content-ID: <img3>` → `src="cid:img3"`). Bu, eşleşmeyi tek anlamlı kılar ve
Gemini haberleri yeniden sıralasa bile doğru görselin doğru bloğa gitmesini
garantiler.
```

Görsel yoksa mevcut `MIMEMultipart("alternative")` yapısı korunur — geriye
dönük uyumlu.

## Güvenlik

**Sanitizer whitelist'i DEĞİŞTİRİLMEZ.** `img` ve `src` beyaz listeye
eklenmez. Enjeksiyon sanitizasyondan **sonra** yapıldığı için eklenen HTML'i
tamamen kod üretir.

Bu sıralama olmasaydı: zararlı bir RSS feed'i, prompt injection ile Gemini'ye
`<img src="http://saldirgan/takip.gif">` yazdırıp e-posta açılma takibi
yapabilir veya veri sızdırabilirdi. Mevcut tasarımda model yalnızca bir
**numara** döndürür, URL'leri kod eşleştirir.

Diğer kontroller: SSRF (redirect sonrası dahil), SVG reddi, magic byte
doğrulama, indirme boyut tavanı, decompression bomb koruması, EXIF temizliği.

## Hata Yönetimi

Görsel katmanı brifingi **asla** bozmaz. Tüm görsel işlemleri try/except ile
sarılır; başarısızlık durumunda o görsel sessizce atlanır (WARNING loglanır) ve
akış devam eder. Bütün görseller başarısız olursa e-posta bugünkü haliyle
gider.

Başarısızlık sayılan durumlar: aday URL yok, SSRF engeli, timeout, HTTP hatası,
izin verilmeyen içerik türü, magic byte uyuşmazlığı, boyut aşımı, Pillow decode
hatası, toplam bütçe dolması.

## Performans

Görsel indirmeleri mevcut desendeki gibi `ThreadPoolExecutor` ile paralel
yapılır (RSS ve makale çekiminde kullanılan yaklaşım). Ek HTTP isteği
olmadığı ve indirmeler paralel olduğu için toplam çalışma süresine etkisi
ihmal edilebilir.

## Sabitler

Dosyanın başındaki mevcut limit bloğuna eklenir:

```python
MAX_IMAGE_ARTICLES     = 10          # Görsel aranacak makale sayısı
MAX_DOWNLOAD_BYTES     = 2_000_000   # İndirme tavanı (optimizasyon öncesi)
IMAGE_TARGET_WIDTH     = 1280        # 640px görüntüleme × 2 (retina)
IMAGE_JPEG_QUALITY     = 85
MAX_TOTAL_IMAGE_BYTES  = 5_000_000   # Tüm görsellerin toplam tavanı
IMAGE_FETCH_TIMEOUT    = 8
```

Boyut gerekçesi: e-posta şablonu konteyneri 700px, blok içi görüntüleme
genişliği ~640px. 1280px hedef retina ekranlarda tam keskinlik verir, ötesi
görünmeyen israftır. Base64 kodlaması boyutu **%33 şişirir**;
`MAX_TOTAL_IMAGE_BYTES` buna göre belirlendi (5 MB ham ≈ 6.8 MB e-posta).

## Bağımlılık

`requirements.txt`'e pinned olarak `Pillow` eklenir (mevcut supply-chain
politikasına uygun).

## Doğrulama

Bu projede otomatik test paketi yok. Doğrulama macOS kopyası elle
çalıştırılarak yapılır: gelen e-postada görsellerin göründüğü, görselsiz
makalelerde artık token kalmadığı ve mail boyutunun beklenen aralıkta olduğu
kontrol edilir.
