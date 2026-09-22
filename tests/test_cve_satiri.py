#!/usr/bin/env python3
"""CVE satırı testleri — makale_cveleri(), _cve_satiri(), render entegrasyonu.
Ağsız, Gemini'siz, saf fonksiyonlar."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ortak import Sayac, cti  # noqa: E402

s = Sayac()
kontrol = s.kontrol

# ── makale_cveleri: çıkarma kaynakları ve davranışı ──
def test_makale_cveleri():
    a = {"title": "FortiOS CVE-2026-1111 açığı", "rss_metni": "Ayrıca CVE-2026-2222 etkileniyor."}
    kontrol("başlık + RSS'ten CVE'ler çıkarıldı",
            cti.makale_cveleri(a) == ["CVE-2026-1111", "CVE-2026-2222"])

    kontrol("sayfa metnindeki CVE de yakalanır",
            "CVE-2026-3333" in cti.makale_cveleri(a, "Sayfada CVE-2026-3333 geçiyor"))

    tekrarli = {"title": "CVE-2026-1111", "rss_metni": "cve-2026-1111 ve CVE-2026-1111 tekrar"}
    kontrol("tekrarlar tekilleştirilir ve büyük harfe normalize edilir",
            cti.makale_cveleri(tekrarli) == ["CVE-2026-1111"])

    sirali = {"title": "", "rss_metni": "CVE-2026-9999 sonra CVE-2026-1111"}
    kontrol("sıra korunur (ilk geçen önce)",
            cti.makale_cveleri(sirali) == ["CVE-2026-9999", "CVE-2026-1111"])

    kontrol("CVE yoksa boş liste", cti.makale_cveleri({"title": "Güncelleme yayınlandı"}) == [])

    cok = {"title": "", "rss_metni": " ".join(f"CVE-2026-{1000+i}" for i in range(50))}
    kontrol("makale_cveleri KIRPMAZ (limit çağıranın işi)",
            len(cti.makale_cveleri(cok)) == 50, str(len(cti.makale_cveleri(cok))))


# ── _cve_satiri: gösterim mantığı ──
def test_cve_satiri():
    kontrol("CVE yoksa satır hiç basılmaz (boş string)", cti._cve_satiri([]) == "")
    kontrol("None de boş string döndürür", cti._cve_satiri(None) == "")

    s = cti._cve_satiri(["CVE-2026-1111", "CVE-2026-2222"])
    kontrol("CVE'ler virgülle yan yana yazılır", "CVE-2026-1111, CVE-2026-2222" in s)
    kontrol("satır 'CVE:' etiketi taşıyor", "CVE:</strong>" in s)
    kontrol("az sayıda CVE'de 'daha' eki YOK", "tane daha" not in s)

    cok = [f"CVE-2026-{1000+i}" for i in range(20)]
    s_cok = cti._cve_satiri(cok)
    gosterilen = s_cok.count("CVE-2026-")
    kontrol("limit uygulanır (MAX_CVE_IN_BRIEFING kadar gösterilir)",
            gosterilen == cti.MAX_CVE_IN_BRIEFING, f"{gosterilen} gösterildi")
    kontrol("kalan sayı doğru yazılır",
            f"ve {20 - cti.MAX_CVE_IN_BRIEFING} tane daha" in s_cok)

    tam = [f"CVE-2026-{1000+i}" for i in range(cti.MAX_CVE_IN_BRIEFING)]
    kontrol("tam limitte 'daha' eki YOK (sınır kenarı)",
            "tane daha" not in cti._cve_satiri(tam))

    kontrol("zararlı içerik escape edilir",
            "&lt;script&gt;" in cti._cve_satiri(["<script>alert(1)</script>"]))


# ── render_briefing_block entegrasyonu ──
ARTICLE = {"title": "FortiOS açığı", "pubDate": "22 Eylül 2026",
           "matched_product": "fortigate", "link": "https://example.com/h"}
ANALIZ = {"baslik": "FortiOS'ta kritik açık", "severite": "YÜKSEK", "ozet": "özet",
          "etkilenen_surumler": "7.4.0", "yamali_surumler": "7.4.7",
          "etkilenen_kapsam": "x", "aksiyon": "y", "oneri": "z", "olay_tarihi": ""}


def test_render_entegrasyon():
    h = cti.render_briefing_block(ARTICLE, ANALIZ, None, ["CVE-2026-1111", "CVE-2026-2222"])
    kontrol("brifing bloğunda CVE satırı görünüyor",
            "🔖 CVE:" in h and "CVE-2026-1111, CVE-2026-2222" in h)

    h_yok = cti.render_briefing_block(ARTICLE, ANALIZ, None, [])
    kontrol("CVE'siz makalede CVE satırı HİÇ yok", "🔖 CVE:" not in h_yok)

    h_vars = cti.render_briefing_block(ARTICLE, ANALIZ, None)
    kontrol("cveler parametresi verilmezse (geriye uyumluluk) çökmez, satır yok",
            "🔖 CVE:" not in h_vars and "FortiOS&#x27;ta kritik açık" in h_vars)

    kontrol("CVE satırı 'Eşleşen Ürün'den SONRA geliyor",
            h.index("Eşleşen Ürün") < h.index("🔖 CVE:") < h.index("Etkilenen Sürümler"))


# ── fetch_cve_context sözleşmesi (ağsız: fetch_cve_record sahte) ──
def test_fetch_cve_context_tuple_dondurur():
    orij = cti.fetch_cve_record
    cti.fetch_cve_record = lambda cve: "SÜRÜM VERİSİ"
    try:
        makaleler = [{"title": "CVE-2026-1111 açığı", "link": "l1", "rss_metni": ""},
                     {"title": "CVE yok burada", "link": "l2", "rss_metni": ""}]
        baglamlar, cve_listesi = cti.fetch_cve_context(makaleler, {})
        kontrol("iki değer döner (bağlamlar, cve listesi)",
                isinstance(baglamlar, dict) and isinstance(cve_listesi, dict))
        kontrol("CVE'li makale listede", cve_listesi.get("l1") == ["CVE-2026-1111"])
        kontrol("CVE'siz makale listede YOK", "l2" not in cve_listesi)

        bos_b, bos_l = cti.fetch_cve_context([{"title": "hiç CVE yok", "link": "l3"}], {})
        kontrol("hiç CVE yoksa da iki değer döner (çökmez)",
                bos_b == {} and bos_l == {})
    finally:
        cti.fetch_cve_record = orij


test_makale_cveleri()
test_cve_satiri()
test_render_entegrasyon()
test_fetch_cve_context_tuple_dondurur()

s.bitir()
