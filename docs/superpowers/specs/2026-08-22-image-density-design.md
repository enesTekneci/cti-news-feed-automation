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

### İkinci gözden geçirmede bulunan iki ek karar

Spec ilk onaylandıktan sonra, uygulamaya geçmeden önce yapılan kritik
bir tekrar gözden geçirmede (superpowers akışının "spec self-review"
adımı) iki gerçek açık tespit edildi ve spec'e dahil edildi:

1. **Bütçe israfı — "aynı konu" bloğuna düşen makaleler.** Kod, hangi
   makalelerin görselinin indirilip **bütçeye ekleneceğine** Gemini'yi
   hiç çağırmadan, sadece `top_matches` sırasına bakarak karar
   veriyordu. Ama `SYSTEM_PROMPT` kuralına göre Gemini bazı haberleri
   "aynı konu hakkında ek haber" bloğuna düşürüyor ve bu blokta hiç
   `[[IMG:n]]` token'ı yazmıyor (doğrulandı — bkz. kod). Sonuç: o
   makalenin görseli yine de indirilip işlenip maile ekleniyor ama
   hiçbir yerde görüntülenmiyor — boşa giden bant genişliği, gerçekten
   gösterilecek başka bir görsele gidebilecek bütçe payı, gereksiz
   büyümüş mail boyutu. Havuz genişledikçe bu çakışma ihtimali de
   artıyor.
2. **Gereksiz sıralı (sequential) bekleme.** Görsel indirme/işleme
   tamamı, Gemini çağrısından **önce** bitmesi bekleniyordu. Ama görsel
   indirmenin Gemini'nin ürettiği metne hiçbir bağımlılığı yok (hangi
   görselin nihayetinde kullanılacağı hariç — bkz. madde 1). İkisi de
   bağımsız ağ bekleme süreleri; art arda değil, eşzamanlı
   çalıştırılabilir.

**Birleşik çözüm:** Görsel indirme, Gemini çağrısıyla **eşzamanlı**
başlatılır (görsel indirme mevcut `ThreadPoolExecutor` içinde ana
thread'de sürerken, Gemini çağrısı ayrı bir arka plan thread'inde
bekletilir). Gemini döndükten sonra, ham çıktıda **gerçekten hangi
`[[IMG:n]]` token'larının bulunduğu** bir regex ile tespit edilir;
bütçe döngüsü yalnızca bu indekste görünen makaleler için harcama
yapar. Duplicate'e düşen makalenin görseli paralellik için baştan
indirilir ama hiçbir zaman eklenmez/bütçe yemez.

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

**Gemini çağrısı ile eşzamanlı çalıştırma.** `analyze_with_gemini(prompt)`
çağrısı `ThreadPoolExecutor(max_workers=1)` ile arka plana alınır;
mevcut görsel indirme bloğu (kendi `ThreadPoolExecutor(max_workers=10)`'u
ile) ana thread'de değişmeden çalışmaya devam eder. İkisi de bağımsız
blocking I/O çağrıları olduğu için aralarında paylaşılan mutable state
yok — thread-safety riski yok. Gemini'nin fırlattığı istisna
`gemini_future.result()` çağrıldığında aynen yeniden fırlatılır; üst
seviye hata yönetimi (main()'in `try/except` sarmalayıcısı) değişmeden
çalışır.

**Bütçe uygulama döngüsü — bir koşul eklendi.** `image_tasks` listesi
zaten `top_matches`'ın önceliğine göre sıralı üretiliyor;
`MAX_TOTAL_IMAGE_BYTES` aşıldığında döngü `break` ile durma davranışı
aynen kalıyor. Yeni eklenen: döngüye girmeden önce
`briefing_html` (sanitizasyon sonrası, `inject_images()`'a
verilecek olan string) üzerinde `re.findall(r'\[\[IMG:(\d+)\]\]', ...)`
ile Gemini'nin gerçekten hangi indeksler için token yazdığı çıkarılır
(`used_indices`). Döngüde `if idx not in used_indices: continue` ile
Gemini'nin token yazmadığı (duplicate bloğuna düşen) makaleler bütçe
harcamadan ve ek olarak eklenmeden atlanır. Not: `sanitize_gemini_html()`
`[[IMG:n]]` metnini değiştirmez (HTML özel karakteri içermiyor,
`html.escape()`'ten etkilenmez) — bu yüzden post-sanitize string
üzerinde arama yapmak güvenli ve `inject_images()`'ın zaten kullandığı
string ile tutarlı.

**Görselsiz makale davranışı değişmiyor.** Görsel adayı bulunamayan
makale placeholder olmadan metin olarak kalır (mevcut tasarım kararı,
kapsam dışı bırakıldı — bilinçli tercih).

## Performans

Görsel indirme artık Gemini çağrısıyla eşzamanlı olduğu için, en kötü
senaryo toplam süre bu iki bileşenin **toplamı değil maksimumu** olur:

| Bileşen | Süre |
|---|---|
| RSS + makale sayfası çekme (sıralı, öncesinde) | ~25 sn |
| max(Görsel indirme ~40 sn, Gemini analizi ~15.5 dk) | ~15.5 dk |
| **Toplam** | **~16 dk** |

Önceki (sıralı) tasarıma göre ~1 dakikalık ek pay kazanılmış oldu.
GitHub Actions'ın 20 dakikalık job timeout'unun altında, önceki
tahminden daha rahat bir marjla. Bu tahmin hâlâ tüm Gemini
denemelerinin en kötü şekilde başarısız olmasını gerektiren, gerçekleşme
ihtimali düşük bir senaryodur.

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

Ek olarak, `used_indices` filtresi için özel bir bağımsız test yazılır:
`[[IMG:1]]` içeren ama `[[IMG:2]]` içermeyen sahte bir `briefing_html`
üzerinde, hem 1 hem 2 için indirilmiş görsel olsa bile yalnızca 1'in
`final_images`/bütçeye eklendiği doğrulanır. `analyze_with_gemini`'nin
arka plan thread'inde çalıştırılması, mock bir `time.sleep` içeren sahte
fonksiyonla test edilip görsel indirmenin gerçekten eşzamanlı sürdüğü
(toplam sürenin ikisinin toplamından değil maksimumundan az farkla
eşleştiği) doğrulanır.
