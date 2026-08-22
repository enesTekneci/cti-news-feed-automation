# Görsel Yoğunluğu Artırma Implementasyon Planı

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `cti_automation.py`'de görsel arama havuzunu sabit ilk-10 kuralından kurtarıp Gemini'nin analiz ettiği tüm makalelere genişletmek; Gemini'nin token yazmadığı (duplicate bloğa düşen) makalelerin görselini bütçeden/e-postadan çıkarmak; görsel indirmeyi Gemini çağrısıyla eşzamanlı çalıştırarak süre kazanmak.

**Architecture:** Değişikliğin tamamı `cti_automation.py`'nin `main()` fonksiyonundaki tek bir bloğu (görsel indirme → Gemini çağrısı → bütçe uygulama → enjeksiyon sırası) yeniden düzenliyor. Yeni fonksiyon/dosya yok; mevcut `ThreadPoolExecutor`/`re` importları yeniden kullanılıyor.

**Tech Stack:** Python 3.12, `concurrent.futures.ThreadPoolExecutor`, `re` (stdlib, ek bağımlılık yok).

## Global Constraints

- Spec kaynağı: `docs/superpowers/specs/2026-08-22-image-density-design.md` — her adım bu spec'in "Değişiklikler" bölümüyle birebir eşleşmeli.
- Sanitizer whitelist'i (`_ALLOWED_TAGS`, `_ALLOWED_ATTRS`) **hiçbir task'ta değişmez** — bu invariant her task sonunda ayrıca doğrulanır.
- Bu repoda pytest/otomatik test paketi yok. "Test" adımları, doğrulanmış pinned bağımlılıklara sahip venv (`/tmp/cti_venv/bin/python3` — google-genai==2.2.0, Pillow==12.3.0, requests==2.34.1, `requirements.txt` ile birebir aynı) üzerinde çalıştırılan bağımsız Python doğrulama betikleridir. Bu venv yoksa: `python3 -m venv /tmp/cti_venv && /tmp/cti_venv/bin/pip install -r requirements.txt`.
- **Sırayla lokal commit, tek push:** Task 1–3 arası yalnızca `git commit` (lokal) yapılır, **push edilmez** — çünkü bu repo public ve `main`'e her push GitHub Actions'ın canlı zamanlanmış çalışmasını etkiler; Task 1 tek başına push edilirse (Task 2'nin bütçe filtresi olmadan) spec'in çözmeye çalıştığı israf sorununu genişletilmiş havuzda büyütmüş olur. Task 4'te Mac senkronu + tam regresyon sonrası **tek seferlik push** yapılır.
- Kod bizzat Claude (bu implementasyonu yürüten ajan) tarafından yazılır — Antigravity'e devredilmez (kullanıcı kararı: değişiklik küçük/mekanik, delegasyonun sabit denetim yükü buna değmiyor).
- Her task sonunda hem `cti_automation.py` (Linux) hem `/Users/enestekneci/Documents/CTI Project/cti_automation.py` (macOS) senkron tutulur — Task 4'e kadar senkron sadece Task 4'te toplu yapılabilir (aşağıda belirtildi), ara task'larda Linux dosyası üzerinde çalışılır.

---

### Task 1: Arama havuzunu genişlet, sabiti kaldır, worker sayısını artır

**Files:**
- Modify: `cti_automation.py:92` (sabit tanımı silinecek)
- Modify: `cti_automation.py:1407-1417` (döngü + worker sayısı)

**Interfaces:**
- Consumes: `top_matches: list[dict]` (mevcut, `main()` içinde zaten tanımlı), `MAX_TOTAL_IMAGE_BYTES` (mevcut sabit, değişmiyor)
- Produces: `image_tasks: list[tuple[int, str, str, str]]` — sonraki task'lar bu ismi ve `(index, url, link, title)` sırasını kullanır

- [ ] **Step 1: Mevcut davranışı doğrulayan referans betiği yaz (regresyon karşılaştırması için)**

`/tmp/verify_task1.py`:
```python
"""Task 1 sonrasi: MAX_IMAGE_ARTICLES kalkti mi, worker=10 mu, havuz tam mi?"""
import sys
sys.path.insert(0, "/Users/enestekneci/Documents/CTI Project Linux")
import os
os.environ.setdefault("GEMINI_API_KEY", "x")
import cti_automation as m
import inspect

fails = []
def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        fails.append(name)

check("MAX_IMAGE_ARTICLES artik yok", not hasattr(m, "MAX_IMAGE_ARTICLES"))

src = inspect.getsource(m.main)
check("Dongu top_matches[:MAX_IMAGE_ARTICLES] icermiyor",
      "top_matches[:MAX_IMAGE_ARTICLES]" not in src)
check("Dongu tam top_matches kullaniyor",
      "enumerate(top_matches, 1)" in src)
check("Worker sayisi 10", "ThreadPoolExecutor(max_workers=10)" in src)
check("Whitelist hala saglam (guvenlik invariant)",
      "img" not in m._ALLOWED_TAGS and "src" not in m._ALLOWED_ATTRS)

print("\n" + ("✓ TUM KONTROLLER GECTI" if not fails else f"✗ BASARISIZ: {fails}"))
sys.exit(1 if fails else 0)
```

Run: `/tmp/cti_venv/bin/python3 /tmp/verify_task1.py`
Expected: `FAIL` (henüz değişiklik yapılmadı — `MAX_IMAGE_ARTICLES artik yok` ve `Worker sayisi 10` testleri düşmeli)

- [ ] **Step 2: Sabiti kaldır**

`cti_automation.py:92` satırını (`MAX_IMAGE_ARTICLES     = 10          # Görsel aranacak makale sayısı`) tamamen sil.

- [ ] **Step 3: Döngüyü genişlet, worker sayısını artır**

`cti_automation.py:1407-1417` (mevcut hâli):
```python
        # Görselleri indir ve optimize et (sadece en kritik makaleler)
        image_tasks = []
        for i, a in enumerate(top_matches[:MAX_IMAGE_ARTICLES], 1):
            url = a.get("image_candidate") or a.get("og_image")
            if url:
                image_tasks.append((i, url, a.get("link", ""), a.get("title", "")))

        results_by_index = {}
        if image_tasks:
            log.info("Processing %d candidate images...", len(image_tasks))
            with ThreadPoolExecutor(max_workers=5) as pool:
```

Yeni hâli:
```python
        # Görselleri indir ve optimize et (Gemini'ye giden tüm makalelerde aranır —
        # gerçek sınır MAX_TOTAL_IMAGE_BYTES, sabit makale sayısı değil)
        image_tasks = []
        for i, a in enumerate(top_matches, 1):
            url = a.get("image_candidate") or a.get("og_image")
            if url:
                image_tasks.append((i, url, a.get("link", ""), a.get("title", "")))

        results_by_index = {}
        if image_tasks:
            log.info("Processing %d candidate images...", len(image_tasks))
            with ThreadPoolExecutor(max_workers=10) as pool:
```

- [ ] **Step 4: Doğrulama betiğini tekrar çalıştır**

Run: `/tmp/cti_venv/bin/python3 /tmp/verify_task1.py`
Expected: `PASS` (5/5)

- [ ] **Step 5: Derleme + import kontrolü**

Run:
```bash
python3 -m py_compile "/Users/enestekneci/Documents/CTI Project Linux/cti_automation.py"
/tmp/cti_venv/bin/python3 -c "import sys; sys.path.insert(0,'/Users/enestekneci/Documents/CTI Project Linux'); import cti_automation; print('import ok')"
```
Expected: İkisi de hatasız, `import ok` yazdırır.

- [ ] **Step 6: Lokal commit (push YOK)**

```bash
cd "/Users/enestekneci/Documents/CTI Project Linux"
git add cti_automation.py
git commit -m "Widen image search pool to all matched articles, remove MAX_IMAGE_ARTICLES

Real governor is now MAX_TOTAL_IMAGE_BYTES alone. Worker count 5->10
since the workload is I/O-bound and public-repo runners have 4vCPU/16GB.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 2: Bütçeyi Gemini'nin gerçekten kullandığı indekslerle sınırla

**Files:**
- Modify: `cti_automation.py` — Gemini çağrısını bütçe döngüsünden önceye taşı, `used_indices` filtresi ekle (Task 1 sonrası tam satır numaraları için Step 1'i çalıştır)

**Interfaces:**
- Consumes: `image_tasks` (Task 1'den), `results_by_index` (mevcut), `briefing_html: str` (mevcut, konum değişiyor)
- Produces: `used_indices: set[int]` — Task 3 bu ismi aynı şekilde kullanır

- [ ] **Step 1: Güncel satır numaralarını al**

Run: `grep -n "raw_briefing = analyze_with_gemini\|briefing_html = sanitize_gemini_html\|# Priority sırasıyla bütçeye ekle\|for task in image_tasks:\|injected_html = inject_images" "/Users/enestekneci/Documents/CTI Project Linux/cti_automation.py"`

(Task 1 sadece yukarıdaki satırları kaydırdı, içeriklerini değiştirmedi — aşağıdaki old/new blokları bulmak için bu grep çıktısını kullan.)

- [ ] **Step 2: used_indices filtresini doğrulayan bağımsız test yaz**

`/tmp/verify_task2.py`:
```python
"""Task 2: Gemini'nin token yazmadigi (duplicate) makalenin gorseli butceye/eke girmemeli."""
import sys
sys.path.insert(0, "/Users/enestekneci/Documents/CTI Project Linux")
import os
os.environ.setdefault("GEMINI_API_KEY", "x")
import re

fails = []
def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        fails.append(name)

# main()'in ic mantigini izole simule ediyoruz (gercek main() network/API cagirir,
# burada sadece filtre mantigini dogruluyoruz)
briefing_html = (
    '<div><h3>Haber 1</h3>[[IMG:1]]<p>...</p></div>'
    '<div><p>Aynı konu hakkında ek haber: Haber 1</p></div>'  # [[IMG:2]] YOK -- duplicate blok
)
used_indices = {int(n) for n in re.findall(r"\[\[IMG:(\d+)\]\]", briefing_html)}
check("used_indices sadece {1} iceriyor", used_indices == {1}, f"bulunan: {used_indices}")

# Butce dongusu simulasyonu: idx=2 icin gercek indirilmis gorsel olsa bile eklenmemeli
image_tasks = [(1, "http://x/a.jpg", "http://x/1", "Haber 1"),
               (2, "http://x/b.jpg", "http://x/2", "Haber 2 (duplicate)")]
results_by_index = {1: (b"fake-bytes-1", "Haber 1"), 2: (b"fake-bytes-2", "Haber 2 (duplicate)")}
MAX_TOTAL_IMAGE_BYTES = 5_000_000

cid_map, titles_map, total_image_bytes, final_images = {}, {}, 0, []
for task in image_tasks:
    idx = task[0]
    if idx not in used_indices:
        continue
    img_bytes, title = results_by_index.get(idx, (None, ""))
    if img_bytes:
        if total_image_bytes + len(img_bytes) > MAX_TOTAL_IMAGE_BYTES:
            break
        total_image_bytes += len(img_bytes)
        cid = f"img{idx}"
        cid_map[idx] = cid
        titles_map[idx] = title
        final_images.append((cid, img_bytes))

check("cid_map sadece idx=1 iceriyor", list(cid_map.keys()) == [1], f"bulunan: {cid_map}")
check("final_images'ta sadece 1 gorsel var", len(final_images) == 1, f"bulunan: {final_images}")
check("Duplicate makalenin bytes'i butceye HIC eklenmedi",
      total_image_bytes == len(b"fake-bytes-1"))

print("\n" + ("✓ TUM KONTROLLER GECTI" if not fails else f"✗ BASARISIZ: {fails}"))
sys.exit(1 if fails else 0)
```

Run: `/tmp/cti_venv/bin/python3 /tmp/verify_task2.py`
Expected: `PASS` (bu test `main()`'e dokunmuyor, saf mantığı doğruluyor — bağımsız olarak zaten geçer; asıl amaç mantığın doğru olduğunu implementasyondan ÖNCE kanıtlamak)

- [ ] **Step 3: `main()` içinde Gemini çağrısını yukarı taşı, filtreyi ekle**

Eski (Task 1 sonrası hâli — `results_by_index` doldurma bloğunun hemen ardından):
```python
        cid_map = {}
        titles_map = {}
        total_image_bytes = 0
        final_images = []

        # Priority sırasıyla bütçeye ekle
        for task in image_tasks:
            idx = task[0]
            img_bytes, title = results_by_index.get(idx, (None, ""))
            if img_bytes:
                if total_image_bytes + len(img_bytes) > MAX_TOTAL_IMAGE_BYTES:
                    log.warning("Total image bytes limit exceeded. Skipping remaining images.")
                    break
                total_image_bytes += len(img_bytes)
                cid = f"img{idx}"
                cid_map[idx] = cid
                titles_map[idx] = title
                final_images.append((cid, img_bytes))

        # Gemini analizi al ve HTML olarak sanitize et (XSS koruması)
        raw_briefing = analyze_with_gemini(prompt)
        briefing_html = sanitize_gemini_html(raw_briefing)

        # Görselleri enjekte et (token'ları img tag'i ile değiştir veya sil)
        injected_html = inject_images(briefing_html, cid_map, titles_map)
```

Yeni:
```python
        # Gemini analizi al ve HTML olarak sanitize et (XSS koruması)
        raw_briefing = analyze_with_gemini(prompt)
        briefing_html = sanitize_gemini_html(raw_briefing)

        # Gemini'nin GERÇEKTEN token yazdığı indeksler — "aynı konu hakkında ek
        # haber" bloğuna düşen makaleler [[IMG:n]] yazmaz, bu yüzden onların
        # görseli indirilmiş olsa bile bütçeye/eke hiç girmemeli.
        # sanitize_gemini_html() bu metni değiştirmez (HTML özel karakteri yok).
        used_indices = {int(n) for n in re.findall(r"\[\[IMG:(\d+)\]\]", briefing_html)}

        cid_map = {}
        titles_map = {}
        total_image_bytes = 0
        final_images = []

        # Priority sırasıyla, sadece Gemini'nin kullandığı indeksler için bütçeye ekle
        for task in image_tasks:
            idx = task[0]
            if idx not in used_indices:
                continue
            img_bytes, title = results_by_index.get(idx, (None, ""))
            if img_bytes:
                if total_image_bytes + len(img_bytes) > MAX_TOTAL_IMAGE_BYTES:
                    log.warning("Total image bytes limit exceeded. Skipping remaining images.")
                    break
                total_image_bytes += len(img_bytes)
                cid = f"img{idx}"
                cid_map[idx] = cid
                titles_map[idx] = title
                final_images.append((cid, img_bytes))

        # Görselleri enjekte et (token'ları img tag'i ile değiştir veya sil)
        injected_html = inject_images(briefing_html, cid_map, titles_map)
```

- [ ] **Step 4: Derleme + import + whitelist invariant**

Run:
```bash
python3 -m py_compile "/Users/enestekneci/Documents/CTI Project Linux/cti_automation.py"
/tmp/cti_venv/bin/python3 -c "
import sys; sys.path.insert(0,'/Users/enestekneci/Documents/CTI Project Linux')
import os; os.environ.setdefault('GEMINI_API_KEY','x')
import cti_automation as m
assert 'img' not in m._ALLOWED_TAGS and 'src' not in m._ALLOWED_ATTRS
print('import ok, whitelist saglam')
"
```
Expected: Hatasız, `import ok, whitelist saglam` yazdırır.

- [ ] **Step 5: Task 1'in doğrulama betiğini tekrar çalıştır (regresyon yok)**

Run: `/tmp/cti_venv/bin/python3 /tmp/verify_task1.py`
Expected: `PASS` (5/5) — Task 1'in kazanımları Task 2 ile bozulmadı

- [ ] **Step 6: Lokal commit (push YOK)**

```bash
cd "/Users/enestekneci/Documents/CTI Project Linux"
git add cti_automation.py
git commit -m "Only spend image budget on articles Gemini actually referenced

Gemini's duplicate-topic block emits no [[IMG:n]] token, but images for
those articles were downloaded, processed and attached anyway -- wasted
bandwidth and a budget slot that could go to a genuinely displayed image.
Move the Gemini call before the budget loop and filter by the indices
that actually appear in its output.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 3: Görsel indirmeyi Gemini çağrısıyla eşzamanlı çalıştır

**Files:**
- Modify: `cti_automation.py` — `analyze_with_gemini(prompt)` çağrısını arka plan thread'ine al

**Interfaces:**
- Consumes: `prompt: str` (mevcut), `analyze_with_gemini` (mevcut fonksiyon, imzası değişmiyor)
- Produces: davranış değişikliği yok — sadece zamanlama; `raw_briefing` aynı tip/değeri üretir

- [ ] **Step 1: Eşzamanlılığın gerçekten çalıştığını doğrulayan test yaz**

`/tmp/verify_task3.py`:
```python
"""Task 3: Gemini cagrisi ile 'gorsel indirme' esit anda mi calisiyor (sequential degil)?"""
import sys, time
from concurrent.futures import ThreadPoolExecutor

fails = []
def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        fails.append(name)

def fake_gemini_call(prompt):
    time.sleep(0.3)  # Gemini'yi simule eder
    return "raw briefing"

def fake_image_download():
    time.sleep(0.3)  # goruntu indirmeyi simule eder
    return "images done"

# ESKI (sequential) davranis: toplam ~0.6sn surer
t0 = time.time()
fake_image_download()
fake_gemini_call("x")
sequential_time = time.time() - t0

# YENI (concurrent) davranis: toplam ~0.3sn surmeli (max, toplam degil)
t0 = time.time()
with ThreadPoolExecutor(max_workers=1) as gemini_pool:
    fut = gemini_pool.submit(fake_gemini_call, "x")
    fake_image_download()
    result = fut.result()
concurrent_time = time.time() - t0

check("Concurrent calisma sequential'dan belirgin hizli",
      concurrent_time < sequential_time * 0.7,
      f"sequential={sequential_time:.2f}s concurrent={concurrent_time:.2f}s")
check("Concurrent sure ~max(0.3,0.3)=0.3sn'ye yakin (toplam 0.6 degil)",
      concurrent_time < 0.5, f"concurrent={concurrent_time:.2f}s")
check("Sonuc dogru donuyor", result == "raw briefing")

print("\n" + ("✓ TUM KONTROLLER GECTI" if not fails else f"✗ BASARISIZ: {fails}"))
sys.exit(1 if fails else 0)
```

Run: `/tmp/cti_venv/bin/python3 /tmp/verify_task3.py`
Expected: `PASS` (3/3) — bu, `main()`'e dokunmadan ThreadPoolExecutor deseninin doğru çalıştığını kanıtlar

- [ ] **Step 2: `main()`'de Gemini çağrısını arka plana al**

Eski (Task 2 sonrası hâli):
```python
        results_by_index = {}
        if image_tasks:
            log.info("Processing %d candidate images...", len(image_tasks))
            with ThreadPoolExecutor(max_workers=10) as pool:
                future_to_idx = {
                    pool.submit(process_image, task[1], task[2]): task
                    for task in image_tasks
                }
                for future in as_completed(future_to_idx):
                    task = future_to_idx[future]
                    idx = task[0]
                    try:
                        results_by_index[idx] = (future.result(), task[3])
                    except Exception as e:
                        log.warning("Image worker failed for %s: %s", task[1], e)
                        results_by_index[idx] = (None, task[3])

        # Gemini analizi al ve HTML olarak sanitize et (XSS koruması)
        raw_briefing = analyze_with_gemini(prompt)
        briefing_html = sanitize_gemini_html(raw_briefing)
```

Yeni:
```python
        # Gemini cagrisini arka plan thread'inde baslat -- gorsel indirme onun
        # ciktisina bagimli degil (hangi gorselin kullanilacagi haric, o da
        # asagida ayrica filtreleniyor), bu yuzden ikisi eszamanli surebilir
        results_by_index = {}
        with ThreadPoolExecutor(max_workers=1) as gemini_pool:
            gemini_future = gemini_pool.submit(analyze_with_gemini, prompt)

            if image_tasks:
                log.info("Processing %d candidate images...", len(image_tasks))
                with ThreadPoolExecutor(max_workers=10) as pool:
                    future_to_idx = {
                        pool.submit(process_image, task[1], task[2]): task
                        for task in image_tasks
                    }
                    for future in as_completed(future_to_idx):
                        task = future_to_idx[future]
                        idx = task[0]
                        try:
                            results_by_index[idx] = (future.result(), task[3])
                        except Exception as e:
                            log.warning("Image worker failed for %s: %s", task[1], e)
                            results_by_index[idx] = (None, task[3])

            # Gemini analizi al ve HTML olarak sanitize et (XSS koruması)
            raw_briefing = gemini_future.result()

        briefing_html = sanitize_gemini_html(raw_briefing)
```

- [ ] **Step 3: Derleme + import + whitelist invariant**

Run:
```bash
python3 -m py_compile "/Users/enestekneci/Documents/CTI Project Linux/cti_automation.py"
/tmp/cti_venv/bin/python3 -c "
import sys; sys.path.insert(0,'/Users/enestekneci/Documents/CTI Project Linux')
import os; os.environ.setdefault('GEMINI_API_KEY','x')
import cti_automation as m
assert 'img' not in m._ALLOWED_TAGS and 'src' not in m._ALLOWED_ATTRS
print('import ok, whitelist saglam')
"
```
Expected: Hatasız.

- [ ] **Step 4: Hata yayılımını doğrula (Gemini istisnası hâlâ yukarı çıkıyor mu?)**

`/tmp/verify_task3_error.py`:
```python
"""gemini_future.result() Gemini istisnasini aynen yeniden firlatiyor mu?"""
import sys
from concurrent.futures import ThreadPoolExecutor

fails = []
def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        fails.append(name)

def failing_gemini_call(prompt):
    raise RuntimeError("Gemini API: tüm modeller başarısız oldu")

try:
    with ThreadPoolExecutor(max_workers=1) as gemini_pool:
        fut = gemini_pool.submit(failing_gemini_call, "x")
        result = fut.result()
    check("Istisna firlatilmadi (BEKLENMEDIK)", False)
except RuntimeError as e:
    check("RuntimeError dogru mesajla yeniden firlatildi",
          "tüm modeller başarısız" in str(e))

print("\n" + ("✓ TUM KONTROLLER GECTI" if not fails else f"✗ BASARISIZ: {fails}"))
sys.exit(1 if fails else 0)
```

Run: `/tmp/cti_venv/bin/python3 /tmp/verify_task3_error.py`
Expected: `PASS` (1/1)

- [ ] **Step 5: Task 1 ve Task 2'nin doğrulama betiklerini tekrar çalıştır (tam regresyon)**

Run:
```bash
/tmp/cti_venv/bin/python3 /tmp/verify_task1.py
/tmp/cti_venv/bin/python3 /tmp/verify_task2.py
```
Expected: İkisi de `PASS`.

- [ ] **Step 6: Lokal commit (push YOK)**

```bash
cd "/Users/enestekneci/Documents/CTI Project Linux"
git add cti_automation.py
git commit -m "Run image downloads concurrently with the Gemini call

Image fetching has no dependency on Gemini's output (only which image
ends up used does, and that's already filtered separately). Wrapping
the Gemini call in a background ThreadPoolExecutor(max_workers=1) lets
both proceed at once, shaving the spec's worst-case runtime estimate
from ~17min to ~16min. Exception propagation from analyze_with_gemini
is preserved via gemini_future.result().

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 4: Mac senkronu, tam regresyon, tek seferlik push

**Files:**
- Modify: `/Users/enestekneci/Documents/CTI Project/cti_automation.py` (Linux'tan kopyalanacak)

**Interfaces:**
- Consumes: Task 1-3 sonrası `cti_automation.py` (Linux, tam final hâli)
- Produces: Yok — bu son task, sadece dağıtım/doğrulama

- [ ] **Step 1: Linux dosyasının final hâlini Mac'e kopyala**

```bash
cp "/Users/enestekneci/Documents/CTI Project Linux/cti_automation.py" \
   "/Users/enestekneci/Documents/CTI Project/cti_automation.py"
chmod 700 "/Users/enestekneci/Documents/CTI Project/cti_automation.py"
diff "/Users/enestekneci/Documents/CTI Project/cti_automation.py" \
     "/Users/enestekneci/Documents/CTI Project Linux/cti_automation.py" && echo "✓ birebir ayni"
```
Expected: `✓ birebir ayni` (diff çıktısı boş).

- [ ] **Step 2: Mac venv'inde import doğrulaması**

```bash
"/Users/enestekneci/Documents/CTI Project/venv/bin/python3" -c "
import cti_automation as m
assert 'img' not in m._ALLOWED_TAGS and 'src' not in m._ALLOWED_ATTRS
assert not hasattr(m, 'MAX_IMAGE_ARTICLES')
print('✓ Mac import ok, whitelist saglam, sabit kalkti')
"
```
Expected: Hatasız, mesaj yazdırır. (İlk import yavaş olabilir — google-genai ilk bytecode derlemesi; ~1dk'ya kadar normal, ikinci çalıştırma hızlıdır.)

- [ ] **Step 3: Tüm doğrulama betiklerini son kez, temiz halde çalıştır**

```bash
/tmp/cti_venv/bin/python3 /tmp/verify_task1.py
/tmp/cti_venv/bin/python3 /tmp/verify_task2.py
/tmp/cti_venv/bin/python3 /tmp/verify_task3.py
/tmp/cti_venv/bin/python3 /tmp/verify_task3_error.py
```
Expected: Dördü de `✓ TUM KONTROLLER GECTI`.

- [ ] **Step 4: Önceki oturumdaki bağımsız güvenlik test paketini de tekrar çalıştır (SSRF/SVG/sanitizer regresyonu yok)**

Eğer `/tmp/my_review_test.py` hâlâ mevcutsa:
```bash
/tmp/cti_venv/bin/python3 /tmp/my_review_test.py
```
Expected: `✓ TUM KONTROLLER GECTI` (13/13). Dosya yoksa bu adımı atla — Task 1-3 sanitizer/SSRF kodunun hiçbirine dokunmadı, bu yüzden zorunlu değil, ek güvence.

- [ ] **Step 5: git log ile 3 lokal commit'in sırayla durduğunu doğrula, sonra tek seferde push**

```bash
cd "/Users/enestekneci/Documents/CTI Project Linux"
git log --oneline -4
git push origin main
git log --oneline -1
git ls-remote origin -h refs/heads/main
```
Expected: Son iki komutun SHA'ları eşleşir (push başarılı).

- [ ] **Step 6: Geçici doğrulama betiklerini temizle**

```bash
rm -f /tmp/verify_task1.py /tmp/verify_task2.py /tmp/verify_task3.py /tmp/verify_task3_error.py
```

- [ ] **Step 7: Canlıda doğrulama (manuel tetikleme)**

```bash
gh workflow run "CTI Tehdit Brifingi"
```
Ardından `gh run watch <run_id> --exit-status` ile bitmesini bekle, `gh run view <run_id> --log | grep -iE "matching inventory|Processing.*candidate images|Email sent"` ile log'da:
- Eşleşen makale sayısına yakın (artık 10'a sabitlenmemiş) bir "Processing N candidate images..." satırı
- `Email sent to ***` satırı

görüldüğünü doğrula. Bu adım implementasyonun bir parçası değil, kullanıcıya sunulacak son kanıttır.
