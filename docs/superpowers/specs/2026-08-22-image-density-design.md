# Tasarım: Görsel Yoğunluğunu Havuz Sınırını Kaldırarak Artırma

**Tarih:** 2026-08-22
**Durum:** Onaylandı, uygulamaya hazır
**Dosya:** `cti_automation.py` (macOS ve Linux kopyaları senkron tutulur)
**İlgili:** `docs/superpowers/specs/2026-08-21-email-images-design.md` (orijinal görsel özelliği)

## Sorun

Orijinal görsel özelliği `MAX_IMAGE_ARTICLES = 10` ile arama havuzunu sabit
ilk 10 makaleyle sınırlıyordu. Gerçek çalışma verisi bu sınırın günlük
haber hacmiyle uyuşmadığını gösterdi:

| Çalışma | Eşleşen makale | Görsel adayı bulunan |
|---|---|---|
| 2026-08-21 15:52 | 10 | 7 |
| 2026-08-22 (2 çalışma) | 5 | 1 |

Hiçbir çalışmada indirme/işleme aşamasında bir görsel elenmedi (SSRF/SVG/
magic-byte/boyut uyarısı sıfır) — yani "10'a hiç ulaşılamaması" iki
ayrı nedenden kaynaklanıyor: (1) eşleşen makale sayısı çoğu gün 10'un
altında kalıyor, (2) eşleşenlerin bir kısmında (özellikle CERT/PSIRT
tarzı kaynaklarda) hiç görsel metadata'sı yok. Sabit "ilk 10" kuralı bu
doğal değişkenliği yansıtmıyordu.

## Karar

Arama havuzunu `top_matches[:MAX_IMAGE_ARTICLES]`'tan **Gemini'ye
gönderilen tüm makalelere** (`top_matches`, en fazla
`MAX_GEMINI_ARTICLES=50`) genişlet. `MAX_IMAGE_ARTICLES` sabiti
tamamen kaldırılır — artık iki üst üste binen "fren" yerine tek bir
gerçek fren var: mevcut `MAX_TOTAL_IMAGE_BYTES` (5 MB), öncelik
sırasıyla harcanıyor.

Sonuç: yoğun haber günlerinde çok sayıda görsel, sakin günlerde az
sayıda — hiçbir sabit hedefe bağlı olmadan, gerçek içerik
kullanılabilirliğiyle orantılı.

### Değerlendirilen ve reddedilen alternatifler

- **Daha yüksek sabit sayı (örn. 25):** Aynı "keskin kalıp" sorununu
  büyük ölçekte tekrarlar, kullanıcı açıkça bunu istemedi.
- **Yüzde bazlı havuz (örn. eşleşenlerin %60'ı):** Küçük N'lerde tuhaf
  davranır (5 eşleşende %60 = 3), ekstra karmaşıklık getirir, gerçek
  bir fayda sağlamaz (YAGNI).

## Değişiklikler

**`main()` içinde tek satır:**

```python
# Önce:
for i, a in enumerate(top_matches[:MAX_IMAGE_ARTICLES], 1):
# Sonra:
for i, a in enumerate(top_matches, 1):
```

`MAX_IMAGE_ARTICLES` sabiti ve tanım yorumu kaldırılır.

**Paralellik:** Görsel indirme `ThreadPoolExecutor(max_workers=5)` →
`max_workers=10`. Gerekçe: iş yükü ağ-bağımlı (I/O-bound), CPU-bound
değil; public repo runner'ı 4 vCPU/16 GB RAM sağlıyor (GitHub'ın
resmi spesifikasyonu); kod tabanında zaten RSS çekimi 10 worker,
makale sayfası çekimi 8 worker kullanıyor — tutarlı bir desen.

**Bütçe uygulama döngüsü değişmiyor.** `image_tasks` listesi zaten
`top_matches`'ın önceliğine göre sıralı üretiliyor; `MAX_TOTAL_IMAGE_BYTES`
aşıldığında döngü `break` ile durma davranışı olduğu gibi kalıyor —
genişletilmiş havuza otomatik olarak uygulanıyor.

**Görselsiz makale davranışı değişmiyor.** Görsel adayı bulunamayan
makale placeholder olmadan metin olarak kalır (mevcut tasarım kararı,
kapsam dışı bırakıldı — bilinçli tercih).

## Performans

En kötü senaryo toplam süre tahmini:

| Bileşen | Süre |
|---|---|
| RSS + makale sayfası çekme | ~25 sn |
| Görsel indirme (50 aday, 10 worker, 8 sn timeout) | ~40 sn |
| Gemini analizi (2026-08-22 timeout düzeltmesiyle) | ~15.5 dk |
| **Toplam** | **~17 dk** |

GitHub Actions'ın 20 dakikalık job timeout'unun altında kalıyor. Bu
tahmin, tüm Gemini denemelerinin VE tüm görsel indirmelerinin aynı anda
en kötü şekilde başarısız olmasını gerektiren, gerçekleşme ihtimali
son derece düşük bir bileşik senaryodur.

## Güvenlik ve hata yönetimi

Değişmiyor — SSRF kontrolü (redirect sonrası dahil), SVG reddi, magic
byte doğrulaması, sanitizer whitelist'i, placeholder enjeksiyon sırası,
her görselin bağımsız try/except ile sarılması. Bu tasarım yalnızca
"hangi makalelerde arayalım" sorusunun kapsamını genişletiyor; güvenlik
ve hata izolasyonu katmanlarına dokunmuyor.

## Doğrulama

Otomatik test paketi yok. Doğrulama: `py_compile` + import + sanitizer
whitelist invariant kontrolü + mevcut bağımsız test paketinin
(`/tmp/my_review_test.py` deseni) tekrar çalıştırılması + gerçek bir
GitHub Actions çalıştırmasında (`gh workflow run`) log'daki
"Processing N candidate images..." satırının artık günlük eşleşme
sayısına (10 sınırına değil) yakın çıktığının doğrulanması.
