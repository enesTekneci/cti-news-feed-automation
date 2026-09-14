#!/usr/bin/env python3
"""
CTI News Feed Automation
Fetches security RSS feeds, matches against product inventory,
analyzes with Gemini AI, and sends email briefings via Exchange SMTP.

Çalışma akışı:
  1. Tüm RSS feed'lerini paralel olarak çek (FEEDS listesi)
  2. Son 24 saatteki makaleleri filtrele
  3. Envanterdeki ürünlerle eşleşenleri bul
  4. En kritik makaleleri Gemini'ye gönder (limit: MAX_GEMINI_ARTICLES), derin analiz al
  5. HTML e-posta olarak SMTP üzerinden gönder
"""

# Standart kütüphane modülleri
import os
import re
import json                          # Envanter/alias + Gemini JSON çıktısı
import html                          # HTML escape (model çıktısı için ZORUNLU)
import logging
import logging.handlers              # RotatingFileHandler (log boyut sınırı)
import smtplib                       # SMTP ile e-posta gönderme
import ssl                           # STARTTLS bağlantısı
import threading                     # Haber-başına analiz worker'ları
import time                          # Hız sınırı / süre bütçesi
import urllib.parse                  # URL resolve için
from io import BytesIO               # Bellekte görsel işlemek için
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.image import MIMEImage
from concurrent.futures import ThreadPoolExecutor, as_completed  # Paralel RSS çekme
from pathlib import Path

# Üçüncü parti paketler
import feedparser                    # RSS/Atom/JSON Feed parser
import requests                      # HTTP istekleri (article fetch)
from google import genai             # Gemini AI SDK
from dotenv import load_dotenv       # .env dosyasından credentials oku
from PIL import Image                # Görsel optimizasyonu

# Decompression bomb koruması: Pillow MAX_IMAGE_PIXELS açıkça sınırlanır (Spec kuralı)
Image.MAX_IMAGE_PIXELS = 40_000_000

# .env dosyasını yükle — API key'ler ve SMTP bilgileri buradan gelir
load_dotenv(Path(__file__).parent / ".env")

# ── Türkçe tarih (locale-bağımsız) ──────────────────
_TR_MONTHS = {
    1: "Ocak", 2: "Şubat", 3: "Mart", 4: "Nisan", 5: "Mayıs", 6: "Haziran",
    7: "Temmuz", 8: "Ağustos", 9: "Eylül", 10: "Ekim", 11: "Kasım", 12: "Aralık",
}
_TR_DAYS = {
    0: "Pazartesi", 1: "Salı", 2: "Çarşamba", 3: "Perşembe",
    4: "Cuma", 5: "Cumartesi", 6: "Pazar",
}


def turkish_date(dt: datetime | None = None) -> str:
    """'17 Mayıs 2026, Cumartesi' formatında Türkçe tarih döndürür."""
    if dt is None:
        dt = datetime.now()
    return f"{dt.day} {_TR_MONTHS[dt.month]} {dt.year}, {_TR_DAYS[dt.weekday()]}"


LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)  # logs/ klasörü yoksa oluştur

# Loglama: hem dosyaya hem konsola yaz
# RotatingFileHandler: dosya 5MB'a ulaşınca yenisi açılır, en fazla 3 backup tutulur
# Bu sayede log dosyası diski doldurmaz (max 15MB)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.handlers.RotatingFileHandler(
            LOG_DIR / "cti_automation.log",
            maxBytes=5 * 1024 * 1024,  # 5 MB
            backupCount=3,              # 3 eski log tut (cti_automation.log.1, .2, .3)
        ),
        logging.StreamHandler(),        # systemd journal'a da yazsın
    ],
)
log = logging.getLogger("cti")

# ── Gemini analiz limitleri ───────────────────────────────────────────────────
# Tüm limitler tek noktadan yönetilir — gerekirse buradan ayarla.
MAX_GEMINI_ARTICLES = 50       # Derin analiz yapılacak maks makale sayısı
# Makale sayfasından çekilecek maks metin (site şablonu ayıklandıktan SONRA,
# bkz. _ana_metin_cikar). Ham sayfa boyutu için bir üst sınır — asıl modele
# giden tavan GEMINI_BODY_CHARS'tır, bu ikisi artık AYRI (aşağıya bkz.).
MAX_BODY_CHARS      = 20_000
# RSS'ten taşınan temiz gövdenin tavanı (match_articles → "rss_metni").
# GEMINI_BODY_CHARS ile hizalı: zengin bir RSS makalesi (Cisco Talos ~27K,
# Cloudflare ~9K karakter tam makale veriyor) modele gitmeden önceden
# kırpılmasın.
RSS_BODY_CHARS      = 12_000
# Bu uzunluktaki temiz rss_metni zaten tam bir makale sayılır — sayfayı
# ayrıca çekmek gereksiz ağ isteği demektir (main()'de kullanılır). Eşik,
# gerçek "sadece kısa bir teaser" RSS özetleriyle (çoğu PSIRT/CVE-DB
# kaynağı 300-1500 karakter civarında) gerçek tam-makale veren kaynakları
# (Talos/Cloudflare/US-CERT, binlerce karakter) net ayıracak kadar yüksek
# tutuldu.
ZENGIN_RSS_ESIGI    = 3_000
# 2026-09-10, İKİNCİ GEÇİŞ: Modele giden metnin büyük kısmının site şablonu
# (nav menüsü, "ilgili haberler" listesi, footer) olduğu ölçüldü — bir
# SecurityWeek makalesinde tam sayfanın ~%40'ı bu çöptü. _ana_metin_cikar
# bunu artık ayıklıyor (bkz. o fonksiyonun yorumu). Bu sayede GEMINI_BODY_CHARS
# MAX_BODY_CHARS'tan AYRILDI: eskiden "haber başına birleşik metin" tavanı
# sayfa tavanıyla aynıydı (20.000), artık şablon çöpü gittiği için modele
# giden SEÇİLMİŞ metin (bkz. _analiz_metni) çok daha küçük olabiliyor.
#
# 12.000 GEÇİCİ bir değer — 10K/20K A/B testi Gemini ücretsiz-katman kotası
# tükendiği için YARIM kaldı (10 istekten 6'sı 429 aldı). Kota sıfırlanınca
# 10K/12K/16K arası DRY_RUN karşılaştırmasıyla (sürüm alanı doluluk oranı)
# kesinleştirilmeli — bkz. plan dosyası "Doğrulama" bölümü.
GEMINI_BODY_CHARS   = 12_000
MAX_DOWNLOAD_BYTES     = 2_000_000   # İndirme tavanı (optimizasyon öncesi)
IMAGE_TARGET_WIDTH     = 1280        # 640px görüntüleme × 2 (retina)
IMAGE_JPEG_QUALITY     = 85
MAX_TOTAL_IMAGE_BYTES  = 5_000_000   # Tüm görsellerin toplam tavanı
IMAGE_FETCH_TIMEOUT    = 8
# ── Haber-başına (fan-out) analiz ────────────────────────────────────────────
# 2026-09-10 MİMARİ DEĞİŞİKLİĞİ: Eskiden 50 haber TEK prompt'ta analiz ediliyordu.
# Bu, "attention dilution" denen bilinen bir kalite sorununa yol açıyordu — model
# dikkat bütçesini 50 habere bölüştürdüğü için her haberin özeti ve sürüm alanları
# sığ kalıyordu. Map-reduce literatürünün tam olarak tarif ettiği durum:
#   "each document gets focused scrutiny in the map phase — the LLM isn't
#    competing 50 documents for attention. Senior teams use this not just for
#    scale but for CORRECTNESS."
# Artık her haber KENDİ isteğinde analiz ediliyor (map fazı). Klasik map-reduce'un
# darboğazı olan "reduce" LLM çağrısı bizde YOK: çapraz-haber dedup zaten kodda
# (CVE + başlık benzerliği) yapılıyor, sıralama da severite'ye göre mekanik.
#
# Kota matematiği (ücretsiz katman, her modelin AYRI kotası var):
#   Model başına 5 RPM / 20 RPD / 250K TPM. 5 model → 100 RPD kapasite, ~25 RPM.
#   50 makale = 50 istek → kapasitenin ~%50'si, retry/manuel çalıştırma payı kalır.
#   Ölçülen tek makale analizi: ~4-5 sn → 5 paralel model ile 50 makale ~1 dk.
# Birincil havuz — analiz kalitesi burada en yüksek. Normal bir günde (50 makale)
# yalnızca bunlar kullanılır: 5 model × 20 RPD = 100 istek kapasite.
ANALYSIS_MODELS = (
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-2.5-flash",
)
# Yedek havuz — SADECE birincil havuzun kotası tükenip hâlâ analiz bekleyen
# makale kaldığında devreye girer. "lite" modeller daha sığ analiz üretir,
# bu yüzden normal günlerde hiç kullanılmazlar; amaçları, yoğun bir günde
# (veya 503 fırtınasında retry'ler kotayı yakınca) haberlerin analizsiz
# kalmasındansa biraz daha sığ analiz almalarını sağlamak.
# 2026-09-10'da bu ihtiyaç gerçek oldu: 4 modelin de günlük kotası dolunca
# 34 makalenin 14'ü analizsiz kaldı.
FALLBACK_MODELS = (
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-2.5-flash-lite",
)
MODEL_RPM = 5                              # Ücretsiz katmanda model başına dakikalık istek
_MODEL_MIN_INTERVAL = 60.0 / MODEL_RPM     # Aynı modelde iki istek arası asgari saniye
# Bir makale en fazla kaç kez denenir. Sınırsız bırakılırsa, sürekli 503 alan
# TEK bir makale kuyrukta dönüp durur ve diğer makalelerin payına düşen günlük
# kotayı yer (2026-09-10 dry-run'ında bir haber 6 kez denendi).
MAX_ATTEMPTS_PER_ARTICLE = 3
# Tek makale analizi küçük bir istek (~4K token girdi, ~400 token çıktı) ve
# ölçümde ~4-5 sn sürüyor. 120 sn, en yavaş gözlemlenen yanıtın çok üstünde
# ama asılı kalan bir isteğin tüm bütçeyi yemesine izin vermeyecek kadar dar.
ARTICLE_TIMEOUT_MS = 120_000
# Tüm fan-out'un toplam süre bütçesi (saniye). Bu süre dolduğunda kalan
# makaleler analiz edilmeden taşma tablosuna düşer — brifing yine gider.
# 30 dk'lık GitHub Actions job limitinin içinde rahatça kalır.
ANALYSIS_TOTAL_BUDGET_SEC = 900

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  INVENTORY
#  Ortamda kullanılan ürünlerin listesi. Bu liste organizasyonun gerçek
#  saldırı yüzeyini (hangi ürünleri kullandığını) ifşa ettiği için KAYNAK
#  KODUNDA TUTULMAZ — public repo'da görünmemesi gerekir. Bunun yerine
#  INVENTORY_JSON ortam değişkeninden (yerelde .env, GitHub Actions'ta
#  repository secret) JSON dizisi olarak okunur.
#  match_articles() bu listede bulunan ürün adlarını makale içinde arar.
#  Yeni ürün eklemek için secret/​.env içindeki JSON'u güncellemen yeterli.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _load_json_env(var_name: str, description: str) -> list:
    """Hassas listeleri (envanter, vendor alias) ortam değişkeninden JSON olarak yükle.

    Sessizce boş listeyle devam ETMEZ — eksik/bozuk secret durumunda hemen
    RuntimeError fırlatır. Aksi halde envanter boş kalır, match_articles()
    hiçbir şey eşleştirmez ve otomasyon sessizce "tehdit yok" maili atarak
    hiçbir şeyin kaçmadığı yanılsaması yaratır (bu, sessiz veri kaybından
    çok daha tehlikelidir — bkz. GEMINI_API_KEY için aynı fail-loud deseni).
    """
    raw = os.environ.get(var_name, "")
    if not raw:
        raise RuntimeError(
            f"{var_name} ortam değişkeni tanımlı değil ({description}). "
            f"Yerelde .env dosyasına, GitHub Actions'ta repository secret "
            f"olarak eklenmeli — aksi halde envanter boş kalır ve hiçbir "
            f"makale eşleşmez."
        )
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{var_name} geçerli bir JSON dizisi değil: {exc}") from exc


INVENTORY = _load_json_env("INVENTORY_JSON", "ürün envanteri")

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  RSS FEEDS
#  Güvenlik haber/advisory kaynakları (her biri 10 worker ile paralel çekilir).
#  Tier 1: Birincil CTI kaynakları (CERT, vendor PSIRT, CVE akışları)
#  Tier 2: Destekleyici kaynaklar (araştırma blogları, exploit istihbaratı)
#  Tier 3: Ek kaynaklar (haber siteleri, ZDI, ransomware tracker vb.)
#
#  BAKIM NOTU (2026-08-27): Tüm liste canlı olarak taranıp doğrulandı.
#  Yanıt vermeyen 16 kaynak çıkarıldı (NVD RSS 404'e düştü, GitHub Advisory
#  aynası bozuldu, USOM RSS'i HTML'e yönlendiriyor, SolarWinds/MSRC Blog/
#  Palo Alto legacy adresleri kapandı, feeds.fortinet.com sertifikası geçersiz).
#  Yerlerine envanterdeki vendor'ları kapsayan 27 doğrulanmış kaynak eklendi.
#  Yeni kaynak eklemeden ÖNCE canlı olarak test et: URL'in entry döndürmesi
#  YETMEZ, fetch_feed() üzerinden (bu UA ve timeout ile) test edilmeli —
#  bazı siteler feedparser'ın kendi UA'sına içerik verip bize vermiyor.
#
#  Bazı büyük vendor'ların artık çalışan bir RSS ucu YOK (advisory'lerini
#  yalnızca web portalından yayınlıyorlar veya bot isteklerini engelliyorlar).
#  Bu boşluk NCSC-NL, BSI CERT-Bund, CISA, cvefeed ve VulDB gibi vendor-üstü
#  kaynaklar üzerinden dolaylı olarak kapanıyor — o vendor'lar için ayrı bir
#  kaynak aramaya gerek yok, aramadan önce bu satırı hatırla.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

FEEDS = [
    # Tier 1: Primary CTI Sources
    ("AhnLab ASEC", "https://asec.ahnlab.com/en/feed"),
    ("CERT-EU Security Advisories", "https://cert.europa.eu/publications/security-advisories-rss"),
    ("CERT-EU Threat Intelligence", "https://cert.europa.eu/publications/threat-intelligence-rss"),
    ("CERT/CC Vulnerability Notes", "https://www.kb.cert.org/vuls/atomfeed/"),
    ("CISA Advisories", "https://cisa.gov/cybersecurity-advisories/all.xml"),
    ("US-CERT Alerts", "https://www.cisa.gov/cybersecurity-advisories/cybersecurity-advisories.xml"),
    ("Cisco PSIRT Advisories", "https://sec.cloudapps.cisco.com/security/center/psirtrss20/CiscoSecurityAdvisory.xml"),
    ("Cisco Talos Intelligence", "https://blog.talosintelligence.com/rss"),
    ("Cloudflare Security", "https://blog.cloudflare.com/tag/security/rss"),
    ("CrowdStrike", "https://crowdstrike.com/blog/feed"),
    ("EclecticIQ", "https://blog.eclecticiq.com/rss.xml"),
    ("Fortinet PSIRT", "https://filestore.fortinet.com/fortiguard/rss/ir.xml"),
    ("Google Project Zero", "https://googleprojectzero.blogspot.com/feeds/posts/default"),
    ("Krebs on Security", "https://krebsonsecurity.com/feed"),
    ("Microsoft MSRC Update Guide", "https://api.msrc.microsoft.com/update-guide/rss"),
    ("Microsoft Security Blog", "https://microsoft.com/en-us/security/blog/feed"),
    ("NCSC UK", "https://www.ncsc.gov.uk/api/1/services/v1/all-rss-feed.xml"),
    # NVD kendi RSS uçlarını kapattı (404) — yerine cvefeed.io ve VulDB akışları
    ("CVE Feed — Son CVE'ler", "https://cvefeed.io/rssfeed/latest.xml"),
    ("CVE Feed — Yüksek/Kritik", "https://cvefeed.io/rssfeed/severity/high.xml"),
    ("VulDB Son Zafiyetler", "https://vuldb.com/?rss.recent"),
    ("Palo Alto Security Advisories", "https://security.paloaltonetworks.com/rss.xml"),
    ("Palo Alto Unit 42", "https://unit42.paloaltonetworks.com/feed"),
    ("Recorded Future", "https://www.recordedfuture.com/feed"),
    ("SANS ISC", "https://isc.sans.edu/rssfeed_full.xml"),
    ("Securelist (Kaspersky)", "https://securelist.com/feed"),
    ("SOCRadar", "https://socradar.io/feed/"),
    ("The Record by Recorded Future", "https://therecord.media/feed"),
    ("Veeam Security Advisories", "https://www.veeam.com/services/open/kb/security-feed"),
    # ── Envanterdeki vendor'lar için eklenen PSIRT/CERT kaynakları ──
    ("Red Hat Product Security", "https://www.redhat.com/en/rss/blog/channel/security"),
    ("Ubuntu Security Notices", "https://ubuntu.com/security/notices/rss.xml"),
    ("Debian Security Advisories", "https://www.debian.org/security/dsa-long"),
    ("Google Cloud Security Bulletins", "https://cloud.google.com/feeds/google-cloud-security-bulletins.xml"),
    ("Chrome Releases", "https://chromereleases.googleblog.com/feeds/posts/default"),
    ("VMware Security (Broadcom)", "https://blogs.vmware.com/security/feed"),
    ("CISA ICS Advisories", "https://www.cisa.gov/cybersecurity-advisories/ics-advisories.xml"),
    ("NCSC-NL Advisories", "https://advisories.ncsc.nl/rss/advisories"),
    ("CERT-FR Avis", "https://www.cert.ssi.gouv.fr/avis/feed/"),
    # Tier 2: Supporting Sources
    ("Bitdefender Labs", "https://bitdefender.com/blog/api/rss/labs"),
    ("Bleeping Computer", "https://www.bleepingcomputer.com/feed/"),
    ("Broadcom/Symantec Blog", "https://sed-cms.broadcom.com/rss/v1/blogs/rss.xml"),
    ("BSI CERT-Bund", "https://wid.cert-bund.de/content/public/securityAdvisory/rss"),
    ("Infosecurity Magazine", "https://infosecurity-magazine.com/rss/news"),
    ("JPCERT/CC", "http://jvndb.jvn.jp/en/rss/jvndb_new.rdf"),
    ("Malwarebytes Labs", "https://blog.malwarebytes.com/feed"),
    ("Maryland MCAC Cyber Threats", "https://mcac.maryland.gov/tag/cyber-threats/feed"),
    ("NIST Cybersecurity Insights", "https://nist.gov/blogs/cybersecurity-insights/rss.xml"),
    ("Security Affairs", "https://securityaffairs.co/feed"),
    ("SentinelOne", "https://sentinelone.com/feed"),
    ("SOC Prime", "https://socprime.com/feed"),
    ("The Hacker News", "https://thehackernews.com/feeds/posts/default"),
    ("Wired", "https://www.wired.com/feed/category/security/latest/rss"),
    # ── Exploit/zafiyet araştırma blogları (envanterdeki edge cihazlara odaklı) ──
    ("watchTowr Labs", "https://labs.watchtowr.com/rss/"),
    ("Horizon3.ai", "https://horizon3.ai/feed/"),
    ("Check Point Research", "https://research.checkpoint.com/feed/"),
    ("Rapid7 Blog", "https://blog.rapid7.com/rss/"),
    ("Qualys Blog", "https://blog.qualys.com/feed"),
    ("Tenable Blog", "https://www.tenable.com/blog/feed"),
    ("GreyNoise Blog", "https://www.greynoise.io/blog/rss.xml"),
    ("Exploit-DB", "https://www.exploit-db.com/rss.xml"),
    # ── Ürün-özel kaynaklar (SAP, Sophos, PHP envanterde var) ──
    ("Onapsis (SAP Güvenliği)", "https://onapsis.com/feed/"),
    ("SecurityBridge (SAP)", "https://securitybridge.com/feed/"),
    ("PHP Releases", "https://www.php.net/feed.atom"),
    ("Sophos News", "https://news.sophos.com/en-us/category/security-operations/feed/"),
    # The Register (theregister.com/security/headlines.atom) aday olarak
    # denendi ama eklenmedi: her istemciye XML yerine HTML sayfası dönüyor.
    # Tier 3: Ek kaynaklar
    ("Cisco Event Responses", "https://sec.cloudapps.cisco.com/security/center/eventResponses_20.xml"),
    ("Cisco Talos (FeedBurner)", "http://feeds.feedburner.com/feedburner/Talos"),
    ("DFIR Report", "https://thedfirreport.com/feed/"),
    ("FortiGuard PSIRT", "https://fortiguard.fortinet.com/rss/ir.xml"),
    ("Ransomware Live", "https://www.ransomware.live/rss"),
    ("Red Canary", "https://redcanary.com/blog/feed/"),
    ("SentinelOne Labs", "https://www.sentinelone.com/labs/feed/"),
    ("Recorded Future (FeedBurner)", "https://feeds.feedburner.com/threatintelligence/pvexyqv7v0v"),
    ("Unit 42 Threat Research", "https://unit42.paloaltonetworks.com/category/threat-research/feed/"),
    ("Dark Reading", "https://www.darkreading.com/rss.xml"),
    ("SecurityWeek", "https://feeds.feedburner.com/securityweek"),
    ("Help Net Security", "https://www.helpnetsecurity.com/feed/"),
    ("ZDI Upcoming Advisories", "https://www.zerodayinitiative.com/rss/upcoming/"),
    ("ZDI Published Advisories", "https://www.zerodayinitiative.com/rss/published/"),
]

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  HIGH-SIGNAL KEYWORDS & VENDOR ALIASES
#  HIGH_SIGNAL: Bir makalenin güvenlik haberi olarak değerlendirilmesi için
#               içermesi GEREKEN kelime listesi (en az 1 tane).
#               Gürültüyü azaltır — sadece kritik güvenlik haberleri geçer.
#  VENDOR_ALIASES: Ürünün alternatif/kısa adları (bir ürünün ticari adı ile
#                  advisory'lerde geçen kısa adı farklı olabiliyor).
#                  Envanterde olmayan vendor'ların alias'ları devreye girmez.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

HIGH_SIGNAL = [
    "cve-", "cvss",
    "zero-day", "0-day",
    "actively exploited", "exploited in the wild", "exploitation detected",
    "remote code execution", "rce",
    "authentication bypass",
    "privilege escalation",
    "critical vulnerability", "critical flaw",
    "ransomware", "data breach", "supply chain attack",
    "apt group", "threat actor", "nation-state",
    "backdoor", "malware campaign",
    "proof of concept exploit", "poc exploit",
    "arbitrary code execution",
    "security advisory", "security bulletin",
    "patch tuesday", "emergency patch",
    # 2026-09-14: has_high_signal() sadece bir ÖN-eleme — ürün adı eşleşmesi
    # ayrıca ZORUNLU (bkz. match_articles), bu yüzden liste genişlemesi TEK
    # BAŞINA yanlış pozitif üretmez. Önceden sadece bileşik ifadeler vardı
    # ("critical vulnerability", "authentication bypass"), çıplak/tek kelime
    # hâlleri eksikti — "New vulnerability disclosed in X" gibi meşru ama
    # sade ifadeli haberler score_article()'a hiç girmeden, taşma tablosuna
    # bile düşmeden sessizce kayboluyordu. Bilinçli olarak EKLENMEYEN: çıplak
    # "patch" (oyun/yazılım güncellemesi bağlamlarında çok yaygın gürültü),
    # çıplak "dos"/"kev" (kısa/belirsiz akronimler — tam ifadeleri aşağıda).
    "exploit", "vulnerable", "vulnerability",
    "denial of service", "cwe-", "known exploited vulnerabilities",
    # ── İngilizce dışı kaynaklar için sinyal kelimeleri ──────────────────
    # BSI CERT-Bund (DE, 250 entry/gün), CERT-FR (FR) ve JPCERT gibi
    # kaynaklar İngilizce başlık kullanmıyor; bu kelimeler olmadan bu
    # feed'lerin TAMAMI HIGH_SIGNAL süzgecinde eleniyordu.
    "schwachstelle", "schwachstellen", "sicherheitslücke", "sicherheitsupdate",
    "sicherheitsanfälligkeit", "ausnutzung",
    "vulnérabilité", "vulnérabilités", "faille de sécurité", "correctif de sécurité",
    "zafiyet", "güvenlik açığı", "kritik açık", "istismar",
]

# ── Gürültü süzgeci (başlıkta aranır) ───────────────────────────────
# Pazarlama/etkinlik içeriği HIGH_SIGNAL kelimelerini taşıyabiliyor
# (örn. "Webinar: Defending Against Ransomware"). Bunlar brifingde
# yer kaplayıp gerçek advisory'leri MAX_GEMINI_ARTICLES limitinden
# dışarı itiyor. Sadece BAŞLIKTA aranır — makale gövdesinde "webinar"
# geçmesi haberi gürültü yapmaz.
NOISE_TITLE_PATTERNS = [
    "webinar", "podcast", "on-demand demo", "register now", "register today",
    "sponsored", "sponsored content", "whitepaper", "white paper", "e-book",
    "ebook", "join us", "watch the replay", "we're hiring", "we are hiring",
    "press release", "customer story", "case study", "magic quadrant",
    "forrester wave", "product launch", "now generally available",
    "sign up for", "save the date", "meet us at", "recap:",
]


def _keyword_pattern(keyword: str) -> str:
    """Bir HIGH_SIGNAL kelimesi için kelime-sınırlı regex parçası üret.

    Neden gerekli: eskiden eşleştirme düz alt-dize (`kw in text`) ile
    yapılıyordu ve "rce" kelimesi "source", "resource", "workforce"
    içinde eşleşiyordu. Bu, İÇİNDE "open source" geçen HER makaleye
    RCE'nin 3 puanını veriyor, önceliklendirmeyi bozuyordu.

    "cve-" gibi tire ile biten önekler sağ sınır ALMAZ — aksi halde
    "cve-2026" eşleşmez (tireden sonra rakam gelir).
    """
    escaped = re.escape(keyword)
    left = r"(?<![0-9A-Za-zÀ-ÿ])"
    right = "" if keyword.endswith("-") else r"(?![0-9A-Za-zÀ-ÿ])"
    return f"{left}{escaped}{right}"


# Uzun kelimeler önce denensin ("poc exploit" < "proof of concept exploit")
_HIGH_SIGNAL_RE = re.compile(
    "|".join(_keyword_pattern(k) for k in sorted(HIGH_SIGNAL, key=len, reverse=True)),
    re.IGNORECASE,
)

_NOISE_TITLE_RE = re.compile(
    "|".join(_keyword_pattern(k) for k in NOISE_TITLE_PATTERNS),
    re.IGNORECASE,
)

# Ürün/alias eşleşmesi (_product_pattern) ve CVE numarası tespiti aynı sol
# sınırı paylaşır: nokta VE kısa çizgi dışlanır — "index.php" gibi bir dosya
# adının içindeki "php"yi ya da "MSCVE-2024-1234" gibi bir alt-dizeyi
# "CVE-2024-1234" sanmayı önler. Tek yerde tanımlanır (bkz. _product_pattern
# docstring'indeki aynı gerekçe — kopyalanan sınırlar sessizce eskir).
_SOL_SINIR = r"(?<![\w.-])"

# CVE numarası (dedup ve puanlama için). Önceden sol kelime sınırı yoktu —
# "MSCVE-2024-1234" gibi bir alt-dizeyi de yanlışlıkla eşleştirebilirdi.
_CVE_RE = re.compile(rf"{_SOL_SINIR}CVE-\d{{4}}-\d{{4,7}}", re.IGNORECASE)

# "CVSS 9.8", "CVSSv3 Score: 9.1", "Severity: 8.8 | HIGH", "base score of 10.0"
_CVSS_SCORE_RE = re.compile(
    r"(?:cvss(?:v\d(?:\.\d)?)?\s*(?:score|rating|base\s+score)?\s*[:of]{0,3}\s*"
    r"|base\s+score\s*[:of]{0,3}\s*|severity\s*:\s*)(\d{1,2}(?:\.\d)?)",
    re.IGNORECASE,
)

# ── Önceliklendirme puanlama tablosu ────────────────
# Kritik (3 puan): aktif sömürü, zero-day, RCE
# Yüksek (2 puan): kritik açık, bypass, privilege escalation, PoC
# Normal (1 puan): genel güvenlik sinyalleri
_SIGNAL_SCORES: dict[str, int] = {
    "actively exploited": 3, "exploited in the wild": 3,
    "exploitation detected": 3, "zero-day": 3, "0-day": 3,
    "remote code execution": 3, "rce": 3,
    "critical vulnerability": 2, "critical flaw": 2,
    "authentication bypass": 2, "privilege escalation": 2,
    "arbitrary code execution": 2, "emergency patch": 2,
    "proof of concept exploit": 2, "poc exploit": 2,
    # HIGH_SIGNAL'da vardı ama bu tabloda yoktu — _DEFAULT_SIGNAL_SCORE=1
    # alıyorlardı. CVE/CVSS'siz meşru bir "nation-state actor targets X"
    # advisory'si yoğun günlerde (MAX_GEMINI_ARTICLES=50 dolduğunda) düşük
    # öncelikle overflow'a düşme riski taşıyordu.
    "apt group": 2, "threat actor": 2, "nation-state": 2,
}
_DEFAULT_SIGNAL_SCORE = 1  # HIGH_SIGNAL'da olup tabloda olmayan keyword'ler

# Bağlamsal bonuslar — kelime sayısı tek başına önceliği iyi belirlemiyordu
_TITLE_MATCH_BONUS = 3   # Ürün adı BAŞLIKTA geçiyorsa haber gerçekten o ürünle ilgili
_CVE_PRESENT_BONUS = 2   # Somut bir CVE var → advisory, spekülasyon değil
_CVSS_CRITICAL_BONUS = 4  # CVSS >= 9.0
_CVSS_HIGH_BONUS = 2      # CVSS 7.0 – 8.9


def has_high_signal(text: str) -> bool:
    """Metin en az bir HIGH_SIGNAL kelimesi içeriyor mu? (kelime sınırlı)"""
    return _HIGH_SIGNAL_RE.search(text) is not None


def max_cvss(text: str) -> float:
    """Metindeki en yüksek CVSS puanını döndür (bulunamazsa 0.0).

    Öncelik sıralamasında kullanılır: CVSS 9.8'lik bir advisory,
    aynı kelimeleri içeren CVSS 4.0'lık bir advisory'nin önüne geçmeli.
    """
    best = 0.0
    for m in _CVSS_SCORE_RE.finditer(text):
        try:
            score = float(m.group(1))
        except ValueError:
            continue
        if 0.0 < score <= 10.0:
            best = max(best, score)
    return best


def score_article(text: str, title: str = "", matched_product: str = "") -> int:
    """Makaleye öncelik puanı hesapla (yüksek = daha kritik).

    Bu puan MAX_GEMINI_ARTICLES limitinde önceliği belirler: en kritik
    makaleler Gemini'ye gider, geri kalanı taşma tablosunda gösterilir.

    Puan bileşenleri:
      1. HIGH_SIGNAL kelimeleri (kelime sınırlı — her kelime bir kez sayılır)
      2. Ürün adı başlıkta mı? (gövdede tek geçiş zayıf sinyaldir)
      3. Somut CVE var mı?
      4. CVSS taban puanı
    """
    total = 0
    # set(): aynı kelimenin 10 kez geçmesi puanı 10'a katlamasın
    for hit in {h.lower() for h in _HIGH_SIGNAL_RE.findall(text)}:
        total += _SIGNAL_SCORES.get(hit, _DEFAULT_SIGNAL_SCORE)

    if matched_product and title and matched_product in norm(title):
        total += _TITLE_MATCH_BONUS

    if _CVE_RE.search(text):
        total += _CVE_PRESENT_BONUS

    cvss = max_cvss(text)
    if cvss >= 9.0:
        total += _CVSS_CRITICAL_BONUS
    elif cvss >= 7.0:
        total += _CVSS_HIGH_BONUS

    return total

# Alias matching iki aşamalı çalışır:
# 1) vendor_key envanter ürün adlarından birinde geçiyor mu?
# 2) Geçiyorsa o vendor'un alias'ları aktif olur
# Böylece kullanmadığın vendor'un alias'ları false positive üretmez.
VENDOR_ALIASES = _load_json_env("VENDOR_ALIASES_JSON", "vendor alias eşleştirme tablosu")

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  GEMINI — HABER BAŞINA ANALİZ
#  Model artık HTML üretmiyor; SADECE yapılandırılmış veri (JSON) döndürüyor.
#  HTML'i render_briefing_block() üretir. Bunun üç somut faydası var:
#    1) XSS yüzeyi yok — model çıktısı hiçbir zaman HTML olarak yorumlanmıyor,
#       her alan html.escape()'ten geçip kendi şablonumuza yerleşiyor.
#       (Eski whitelist tabanlı HTML sanitizer'ı tamamen gereksiz kıldı.)
#    2) Format tutarlılığı garanti — model bazen <p> unutuyor, bazen fazladan
#       giriş cümlesi yazıyordu; artık bunlar yapısal olarak imkânsız.
#    3) Sürüm alanları TEST EDİLEBİLİR veri — HTML'den regex ile geri
#       ayıklamaya gerek yok.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

SEVERITE_RENK = {"YÜKSEK": "#dc3545", "ORTA": "#fd7e14", "DÜŞÜK": "#28a745"}

# Gemini structured-output şeması. response_schema ile birlikte verildiğinde
# model bu alanların DIŞINA çıkamaz ve eksik alan döndüremez.
ANALYSIS_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        # Triage alanı — en başta, model önce bunu karara bağlamalı. main()
        # false dönen makaleleri brifingden çıkarıp taşma tablosuna düşürür
        # (bkz. filter_irrelevant_analyses) — regex eşleşmesi yanlış pozitif
        # olabilir, bu alan modelin semantik anlayışıyla o riski kapatır.
        "urun_ile_ilgili_mi": {"type": "BOOLEAN"},
        "severite": {"type": "STRING", "enum": ["YÜKSEK", "ORTA", "DÜŞÜK"]},
        "etkilenen_surumler": {"type": "STRING"},
        "yamali_surumler": {"type": "STRING"},
        "etkilenen_kapsam": {"type": "STRING"},
        "ozet": {"type": "STRING"},
        "olay_tarihi": {"type": "STRING"},
        "aksiyon": {"type": "STRING"},
        "oneri": {"type": "STRING"},
    },
    "required": [
        "urun_ile_ilgili_mi", "severite", "etkilenen_surumler", "yamali_surumler",
        "etkilenen_kapsam", "ozet", "aksiyon", "oneri",
    ],
}

# NOT — sürüm çıkarma hakkında: Burada modele BİLEREK hiçbir regex/biçim kuralı
# verilmiyor. Eskiden kod, metinden regex ile sürüm çekip ("Detected Versions")
# modele ipucu olarak veriyordu ve modelden bu işaretleri yorumlamasını
# istiyordu; buna rağmen sürüm alanları sürekli eksik kalıyordu çünkü regex
# her advisory biçimini yakalayamıyordu. Artık modelin TAM makale metnini
# görüp sürümleri kendi bağlam anlayışıyla çıkarması isteniyor — dil modelinin
# regex'ten yapısal olarak daha iyi olduğu iş tam olarak budur.
SYSTEM_PROMPT = """Sen kıdemli bir Siber Tehdit İstihbaratı (CTI) analistisin ve bir güvenlik operasyonları ekibine danışmanlık yapıyorsun.

Sana TEK bir güvenlik haberi verilecek: başlığı, yayın tarihi, ortamımızdaki hangi ürünle eşleştiği ve makalenin tam metni. Bu haberi derinlemesine analiz edip verilen JSON şemasına göre yanıt ver.

ALANLAR:

urun_ile_ilgili_mi — Bu haber GERÇEKTEN "eşleşen ürün" ile mi ilgili? Ürün adı metinde geçtiği için otomatik olarak eşleştirildi, ama bu bir YANLIŞ POZİTİF olabilir — örn. ürün adı bambaşka bir bağlamda/kelime oyununda geçiyor, farklı bir şirketin/projenin ürünü kastediliyor, ya da haberin konusu gerçekten eşleşen ürünle ilgisiz. false döndür SADECE bundan GERÇEKTEN eminsen. Şüpheli durumlarda ve haber makul ölçüde ürünü konu alıyorsa true döndür — belirsizlikte varsayılan true'dur, emin olmadığın haberleri eleme.

severite — Tehdidin bizim ortamımız için aciliyeti:
  YÜKSEK = aktif olarak istismar ediliyor / kritik RCE / veri ihlali / yama yok
  ORTA   = yaması mevcut kritik açık / devam eden bir kampanya
  DÜŞÜK  = potansiyel risk / bilgilendirme / öneri niteliğinde

etkilenen_surumler — Zafiyetten ETKİLENEN (savunmasız) sürümler. Makale metnini dikkatle oku ve sürüm bilgisini KENDİN çıkar. Ürün adıyla birlikte, insanın okuyacağı şekilde yaz (örn. "FortiOS 7.4.0 – 7.4.6 ve 7.2.0 – 7.2.10", "PAN-OS 11.2.4'ten önceki tüm sürümler"). Birden fazla ürün dalı etkileniyorsa hepsini yaz. Metinde sürüm gerçekten geçmiyorsa: "Belirtilmemiş — kaynağı kontrol edin".

yamali_surumler — Yamayı içeren GÜVENLİ sürümler, yani yükseltme hedefi (örn. "FortiOS 7.4.7 ve 7.2.11", "Windows: KB5031354"). Microsoft ürünlerinde yamanın kimliği sürüm numarası değil KB numarasıdır, varsa onu yaz. Yama henüz yayınlanmadıysa bunu açıkça belirt.

etkilenen_kapsam — Etkilenen yazılım/donanım/kullanıcı grubu, kısa bir ifadeyle.

ozet — Tehdidin ÖZÜ. En fazla 40 kelime. Neyin, nasıl istismar edildiğini ve bizim için neden önemli olduğunu somut yaz. Genel geçer ifadelerden ("güvenlik açığı tespit edildi") kaçın; saldırı vektörünü ve etkisini söyle.

olay_tarihi — Zafiyetin istismar edildiği/keşfedildiği veya olayın gerçekleştiği SPESİFİK tarih, metinde geçiyorsa. Bu, haberin YAYIN tarihi DEĞİLDİR. Türkçe yaz (örn. "15 Ağustos 2026") ve ozet alanında da AYNEN bu şekilde geçir. Metinde böyle bir tarih yoksa boş string döndür — ASLA tarih uydurma.

aksiyon — Güvenlik ekibinin ŞİMDİ yapması gereken somut, emir kipinde talimat (örn. "FortiOS'u 7.4.7'ye yükselt"). Somut bir aksiyon yoksa: "Güncellemeleri takip et."

oneri — Bu olaydan çıkarılacak bir stratejik tavsiye.

KURALLAR:
- Yanıtın tamamı TÜRKÇE. Teknik terimler, CVE numaraları, ürün adları ve komutlar İNGİLİZCE kalır.
- Sadece verilen makale metnine dayan. Metinde olmayan bir bilgiyi UYDURMA.
- Tüm alanları doldur; bilgi yoksa alanın kendi kuralındaki "yok" ifadesini kullan."""



# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  HELPERS
#  Metin temizleme, versiyon çıkarma, HTML işleme yardımcıları
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Pre-compiled regex'ler (her kullanımda yeniden compile etmemek için)
# _SCRIPT_STYLE: <script>/<style> etiketlerinin İÇERİĞİYLE BİRLİKTE silinmesi
# ZORUNLU. 2026-09-10'da ölçüldü: sadece tag işaretlerini silmek (aşağıdaki
# _HTML_TAG) CSS ve JavaScript gövdesini "metin" olarak bırakıyordu —
# bir Cisco advisory'sinin modele giden İLK 300 KARAKTERİ ham JavaScript'ti,
# 10.000. karakter civarı ise saf CSS. Yani makale metni için ayrılan payın
# büyük kısmı site şablonuna gidiyor, gerçek advisory içeriği hiç
# görülmüyordu. Sürüm alanlarının uzun makalelerde bile boş kalmasının
# sebeplerinden biri buydu.
_SCRIPT_STYLE = re.compile(
    r"<(script|style|noscript|template)\b[^>]*>.*?</\1\s*>",
    re.IGNORECASE | re.DOTALL,
)
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_HTML_TAG = re.compile(r"<[^>]*>")
_WHITESPACE = re.compile(r"\s+")


def strip_html(raw: str) -> str:
    """HTML'den okunabilir metin çıkar: script/style/yorum gövdeleri dahil temizler.

    Sıra ÖNEMLİ ve dört adımın da gerekçesi var:

    1) _SCRIPT_STYLE — <script>/<style> etiketleri İÇERİKLERİYLE silinir.
       Sadece tag işaretlerini silmek CSS/JS gövdesini metin olarak bırakır
       (bkz. _SCRIPT_STYLE yorumu: modele giden payın büyük kısmını yiyordu).
    2) _HTML_COMMENT — <!-- --> yorumları silinir (bazı sitelerde eski/taslak
       içerik yorum satırına gömülü kalıyor, görünmeyen metin olarak sızmasın).
    3) _HTML_TAG — kalan tag işaretleri silinir.
    4) html.unescape — entity'ler çözülür. Bu, sürüm bilgisi için KRİTİK:
       CISA/vendor advisory'leri eşikleri "&lt;=20.1.0" olarak yayınlıyor.

    Entity çözümü EN SONA bırakılır: daha önce yapılsaydı "&lt;script&gt;"
    gerçek bir <script> etiketine dönüşüp temizlikten kaçardı.

    NOT: Bu fonksiyon sadece tag/entity temizler — site ŞABLONUNU (nav/footer/
    ilgili-haberler) atmaz. Makale SAYFALARI için bkz. _ana_metin_cikar; bu
    fonksiyon RSS gövdesi gibi zaten şablonsuz kaynaklarda tek başına kullanılır.
    """
    metin = _SCRIPT_STYLE.sub(" ", raw or "")
    metin = _HTML_COMMENT.sub(" ", metin)
    return _WHITESPACE.sub(" ", html.unescape(_HTML_TAG.sub(" ", metin))).strip()


# ── Sayfa şablonunu at, ana makale metnini izole et ─────────────────────────
# 2026-09-10, ikinci geçiş: strip_html tag/script/style temizliyor ama nav
# menüsü, footer, "ilgili haberler" listesi gibi site ŞABLONUNU metin olarak
# BIRAKIYOR. Ölçüldü (gerçek SecurityWeek makalesi): tam sayfa strip_html
# çıktısı 8.214 karakter; ilk ~400'ü saf nav menüsü ("SECURITYWEEK NETWORK:
# Cybersecurity News Webcasts Virtual Events Podcast..."), ortası bir "Latest
# articles" listesi. Sayfada 6 ayrı <article> etiketi vardı (ilgili haberler
# de <article> ile sarılıymış) — sadece İLKİNİ almak 4.816 karaktere indirdi
# (~%40 azalma), gerçek makale gövdesiyle başlıyordu.
#
# Yeni bağımlılık YOK (kullanıcı kararı) — _SCRIPT_STYLE ile AYNI teknik:
# önce şablon etiketleri İÇERİKLERİYLE silinir, sonra ana bölge aday
# desenleriyle izole edilmeye çalışılır. Hiçbir aday _MIN_BOLGE_KARAKTER
# eşiğini geçmezse (ör. site hiç semantik etiket kullanmıyorsa) şablonu
# temizlenmiş TAM metne düşülür — bu, düzeltme ÖNCESİ davranışla aynıdır,
# yani sonuç bugünden asla daha kötü olamaz.
_CHROME_BLOKLARI = re.compile(
    r"<(nav|header|footer|aside|form)\b[^>]*>.*?</\1\s*>",
    re.IGNORECASE | re.DOTALL,
)
# Öncelik sırasıyla ana bölge adayları. Non-greedy (.*?): ilk kapanışta durur;
# "ilgili haberler" kartları gerçek makaleden SONRA geldiği için bu genelde
# doğru makaleyi yakalar (iç içe/malformed <article> nadir edge-case'lerde
# erken kesebilir — _MIN_BOLGE_KARAKTER eşiği bunu filtreler).
_ANA_BOLGE_MAIN = re.compile(r"<main\b[^>]*>(.*?)</main\s*>", re.IGNORECASE | re.DOTALL)
_ANA_BOLGE_ARTICLE = re.compile(r"<article\b[^>]*>(.*?)</article\s*>", re.IGNORECASE | re.DOTALL)
# WordPress/genel CMS kalıbı: class="entry-content"/"article-content"/
# "post-content"/"articleBody". İç içe <div>'ler regex ile dengelenemediği
# için açılıştan belge SONUNA kadar alınır; kalan footer/nav zaten
# _CHROME_BLOKLARI'nda silinmiş, kalan varsa karakter tavanı budar.
_ANA_BOLGE_SINIF = re.compile(
    r'<div\b[^>]*\bclass="[^"]*(?:entry|article|post)-content[^"]*"[^>]*>(.*)$',
    re.IGNORECASE | re.DOTALL,
)
# Bir bölgenin "gerçek makale" sayılması için asgari temiz karakter. Altında
# kalırsa güvenilmez kabul edilip bir sonraki adaya (veya tam metne) geçilir.
# Ölçülen gerçek örnekler: SecurityWeek izole makale 4.816 krk (kazanır);
# MSRC'nin JS kabuğu 58 krk (hiçbir aday bunu geçemez, tam metne düşülür).
_MIN_BOLGE_KARAKTER = 800


def _ana_metin_cikar(html_ham: str) -> str:
    """Makale sayfası HTML'inden site şablonunu ATIP ana metni döndür.

    Dosyanın elle-yazılmış regex HTML işleme çizgisini sürdürür (bkz.
    strip_html / _SCRIPT_STYLE yorumu) — yeni bağımlılık eklemeden.

    Adımlar: script/style/yorum silinir → nav/header/footer/aside/form
    gövdeleri silinir (_CHROME_BLOKLARI) → ana bölge izole edilmeye
    çalışılır (<main> → ilk <article> → *-content class'lı div,
    _MIN_BOLGE_KARAKTER eşiğini İLK geçen kazanır) → hiçbiri tutmazsa
    şablonu temizlenmiş TAM metin kullanılır (asla daha kötü olmaz).
    """
    onceki = _SCRIPT_STYLE.sub(" ", html_ham or "")
    onceki = _HTML_COMMENT.sub(" ", onceki)
    govdesiz = _CHROME_BLOKLARI.sub(" ", onceki)
    for desen in (_ANA_BOLGE_MAIN, _ANA_BOLGE_ARTICLE, _ANA_BOLGE_SINIF):
        m = desen.search(govdesiz)
        if m:
            aday = _WHITESPACE.sub(" ", html.unescape(_HTML_TAG.sub(" ", m.group(1)))).strip()
            if len(aday) >= _MIN_BOLGE_KARAKTER:
                return aday
    return _WHITESPACE.sub(" ", html.unescape(_HTML_TAG.sub(" ", govdesiz))).strip()


def norm(s: str) -> str:
    """Metni normalize et: küçük harf + boşlukları tekleştir (eşleştirme için)."""
    return _WHITESPACE.sub(" ", (s or "").lower()).strip()


# ── Sürüm deseni (SADECE ürün eşleştirme için) ──────────────────────
# NOT: Metinden sürüm ÇIKARMA işi 2026-09-10'da tamamen Gemini'ye devredildi
# (bkz. SYSTEM_PROMPT yanındaki not) — _VERSION_RE, extract_versions(),
# _FALSE_VERSION_RE ve MAX_VERSIONS silindi. Aşağıdaki desen sadece
# _NEARBY_VERSION_RE için duruyor: jenerik bir ürün adının ("php") hemen
# ardından sürüm numarası gelmesi, o haberin gerçekten o ürünle ilgili
# olduğunu gösteren bir EŞLEŞME sinyalidir; sürüm bilgisinin kendisini
# çıkarmak için kullanılmaz.
#
# Build/patch soneki: "-h5", "-h16-rc1" gibi. Tire sonrası HARF şartı var, bu yüzden
# "7.0-7.6" gibi aralık ayırıcısı tire ile KARIŞMAZ (7 bir harf değil, sayı).
_BUILD_SUFFIX = r"(?:-[a-zA-Z]+\d*)*"
_VER_LOOSE = rf"\d+\.\d+(?:\.(?:\d+|[xX*]))*{_BUILD_SUFFIX}"


# ── HTML güvenliği ──────────────────────────────────────────────────
# 2026-09-10: Whitelist tabanlı HTML sanitizer (_HTMLSanitizer, _ALLOWED_TAGS,
# sanitize_gemini_html) TAMAMEN SİLİNDİ ve daha güçlü bir garantiyle
# değiştirildi. Artık Gemini HTML üretmiyor — yapılandırılmış JSON döndürüyor,
# HTML'i biz üretiyoruz ve modelden gelen her alan html.escape()'ten geçiyor.
# Yani model çıktısı hiçbir noktada HTML olarak YORUMLANMIYOR; "hangi tag'e
# izin verelim" sorusu ortadan kalktı. Sanitizer'ı düzeltmek yerine ona olan
# ihtiyacı yok etmek, saldırı yüzeyini kapatmanın daha kesin yolu.


# HTTP istek başlıkları — User-Agent kimliği ve kabul edilen MIME türleri
# Makale sayfası indirme başlıkları. UA tarayıcı UA'sı olmalı: "CTI-Automation/1.0
# (Security Feed Scanner)" gibi bot UA'ları cisa.gov ve vuldb.com tarafından 403
# ile reddediliyordu. Bu istekler makalenin TAM METNİNİ getiriyor ve versiyon
# çıkarma tamamen buna dayanıyor — 403 alınan her makale "sürüm bilgisi yok"
# olarak brifinge giriyordu.
_REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# SSRF koruması: iç ağ adreslerine istek yapılmasını engelle
# (saldırgan RSS feed'inde 127.0.0.1, AWS metadata URL'si vb. enjekte ederse engeller)
_SSRF_BLOCKED = re.compile(
    r"^https?://("
    r"localhost|127\.|10\.|172\.(1[6-9]|2\d|3[01])\.|192\.168\."
    r"|169\.254\.|0\.0\.0\.0|\[::1\]|metadata\.google"
    r")",
    re.IGNORECASE,
)


def _ssrf_kontrol(url: str, asama: str) -> bool:
    """URL SSRF kara listesine takılıyorsa logla ve True döndür (= engelle).

    fetch_article_page VE process_image AYNI deseni (istek öncesi + redirect
    SONRASI nihai adres) uygular — iç ağa yönlendirme bilinen bir SSRF
    bypass yöntemi olduğu için ikisi de iki kez kontrol eder. Tek yerde
    tanımlanır ki biri güncellenince öteki unutulmasın (2026-09-11'e kadar
    fetch_article_page'in post-redirect kontrolü eksikti).
    """
    if _SSRF_BLOCKED.search(url):
        log.warning("SSRF blocked (%s): %s", asama, url)
        return True
    return False


_OG_IMAGE_RE = re.compile(
    r'<meta[^>]+(?:property|name)=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']|<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\']og:image["\']',
    re.IGNORECASE
)


def _fragment_bolumu_cikar(url: str, html_ham: str) -> str:
    """URL'de bir #fragment (sayfa-içi bağlantı) varsa, SADECE o bölümü izole et.

    2026-09-11'de ölçülerek bulundu: Google Cloud Security Bulletins feed'i
    TEK bir arşiv sayfasına (711K karakter, 306 bülten) fragment ile bağlantı
    veriyor (ör. .../index#gcp-2026-033). URL fragment'ı TARAYICI-İÇİ bir
    mekanizmadır, sunucuya HİÇ gönderilmez — requests.get() her zaman TÜM
    arşivi indirir. Bunu ayıklamadan _ana_metin_cikar'a bırakınca arşivin
    yarısını "ana bölge" sanıp 245.956 karakter döndürdü; tek bültenin
    gerçek boyutu 898 karakter.

    Google Cloud'da fragment sayfada birebir id="..." olarak geçiyor (her
    bülten <h2 id="gcp-2026-033">...). Bu genel bir kalıp olduğu için
    (tek-sayfa dokümantasyon/SSS/değişiklik günlüğü siteleri hep böyle
    yapar) site'ye özel değil: fragment'i taşıyan etiketten, AYNI etiket
    türünde bir SONRAKİ id'li etikete kadar olan bölüm alınır. Fragment
    sayfada id olarak geçmiyorsa (çoğu haber sitesi) boş döner —
    fetch_article_page o zaman normal _ana_metin_cikar yoluna düşer.
    """
    fragment = urllib.parse.urlparse(url).fragment
    if not fragment:
        return ""
    baslangic = re.search(rf'<(\w+)\b[^>]*\bid="{re.escape(fragment)}"', html_ham, re.IGNORECASE)
    if not baslangic:
        return ""
    etiket = baslangic.group(1)
    sonraki = re.search(rf'<{re.escape(etiket)}\b[^>]*\bid="', html_ham[baslangic.end():], re.IGNORECASE)
    bitis = baslangic.end() + sonraki.start() if sonraki else len(html_ham)
    return strip_html(html_ham[baslangic.start():bitis])


# Dosyadaki diğer tüm zaman aşımları modül sabiti (FEED_FETCH_TIMEOUT,
# ARTICLE_TIMEOUT_MS, IMAGE_FETCH_TIMEOUT gibi) — bu da aynı disipline uyar.
ARTICLE_PAGE_FETCH_TIMEOUT_SEC = 12


def fetch_article_page(url: str, timeout: int = ARTICLE_PAGE_FETCH_TIMEOUT_SEC) -> tuple[str, str]:
    """Makale URL'sine gidip ANA makale metnini ve og:image URL'sini döndürür.

    Site şablonu (nav/footer/ilgili-haberler) _ana_metin_cikar tarafından
    atılır — dönen metin sadece tag'leri silinmiş ham sayfa DEĞİL, mümkün
    olduğunca makalenin kendisidir. og:image e-posta içi görsel
    optimizasyonunda aday olarak kullanılır.
    """
    if not url or not url.startswith("http"):
        return "", ""
    if _ssrf_kontrol(url, "article pre-request"):
        return "", ""
    try:
        # max_redirects=3: sonsuz redirect loop'unu önler
        session = requests.Session()
        session.max_redirects = 3
        resp = session.get(
            url, headers=_REQUEST_HEADERS, timeout=timeout, verify=True,
            allow_redirects=True,
        )
        resp.raise_for_status()

        # SSRF: redirect sonrası NIHAI adresi tekrar kontrol et
        # (iç ağa yönlendirme bilinen bypass yöntemidir)
        if _ssrf_kontrol(resp.url, "article post-redirect"):
            return "", ""

        # og:image çıkar (sayfa başındaki meta tag'lerde aranır, ilk 8KB yeterli)
        og_image = ""
        m = _OG_IMAGE_RE.search(resp.text[:8192])
        if m:
            og_image = m.group(1) or m.group(2) or ""

        # Önce fragment'e özel bölüm dene (bkz. _fragment_bolumu_cikar —
        # tek-sayfa arşiv/SSS siteleri için); tutmazsa (çoğu haber sitesi)
        # normal ana-bölge izolasyonuna düş. İkisi de zaten strip()'li/
        # boşluğu tekleştirilmiş döner, ekstra _WHITESPACE.sub gerekmiyor.
        metin = _fragment_bolumu_cikar(url, resp.text) or _ana_metin_cikar(resp.text)
        return metin[:MAX_BODY_CHARS], og_image
    except Exception as exc:
        # Bir makale çekilemese bile diğerleri devam etmeli — sessizce logla
        log.warning("Article fetch failed (%s): %s", url, exc)
        return "", ""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  RSS FETCHING
#  feedparser RSS, Atom ve JSON Feed formatlarını destekler.
#  Her feed paralel çekilir (10 worker thread), tek bir yavaş feed
#  toplam süreyi yavaşlatmaz.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# User-Agent stratejisi ÖLÇÜMLE belirlendi, tercih meselesi değil — kaynaklar
# birbiriyle ÇELİŞEN UA politikaları uyguluyor ve tek bir UA hepsini memnun
# etmiyor:
#   cisa.gov          "compatible; Bot/1.0; +RSS" tarzı UA'ları 403 ile reddeder
#   securelist.com    tarayıcı UA'sına 504 döner, RSS istemcisi UA'sına 200
#   news.sophos.com   tarayıcı UA'sında read-timeout, RSS istemcisi UA'sında anında yanıt
# Bu yüzden önce doğal bir RSS istemcisi UA'sı denenir, boş dönerse tarayıcı
# UA'sıyla bir kez daha denenir.
_FEED_UA_PRIMARY = feedparser.USER_AGENT
_FEED_UA_FALLBACK = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
# Accept başlığı: bazı CDN'ler bu olmadan XML yerine HTML sayfası döndürüyor
_FEED_ACCEPT = (
    "application/atom+xml,application/rss+xml,application/xml;q=0.9,"
    "text/xml;q=0.2,*/*;q=0.1"
)
# (connect, read) saniye — yavaş kaynak tüm brifingi geciktirmesin.
# ubuntu.com gibi ara sıra yavaşlayan kaynaklar 20 sn'ye takılıyordu.
FEED_FETCH_TIMEOUT = (10, 25)


def _download_feed(url: str):
    """Feed'i indir ve ayrıştır; UA politikası yüzünden boş dönerse yeniden dene.

    feedparser.parse(url) ağ isteğini kendi yapar ve TIMEOUT KOYMAZ — yanıt
    vermeyen tek bir kaynak worker thread'ini süresiz bloke eder ve kaynak
    sayısı arttıkça tüm brifingi GitHub Actions'ın 20 dk job limitine
    çarptırabilir. Bu yüzden indirme requests ile (timeout'lu) yapılır.
    """
    last_error = None
    parsed = None
    for user_agent in (_FEED_UA_PRIMARY, _FEED_UA_FALLBACK):
        try:
            resp = requests.get(
                url,
                timeout=FEED_FETCH_TIMEOUT,
                headers={"User-Agent": user_agent, "Accept": _FEED_ACCEPT},
                allow_redirects=True,
            )
            resp.raise_for_status()
            parsed = feedparser.parse(resp.content)
            if parsed.entries:
                return parsed
        except requests.RequestException as exc:
            last_error = exc
    if parsed is not None:
        return parsed          # her iki UA da boş döndü — çağıran 0 entry loglar
    raise last_error or RuntimeError(f"Feed indirilemedi: {url}")


def _entry_datetime(entry) -> datetime | None:
    """feedparser'ın ayrıştırdığı tarihi timezone-aware datetime'a çevir."""
    for attr in ("published_parsed", "updated_parsed", "created_parsed"):
        parsed = getattr(entry, attr, None)
        if parsed:
            try:
                return datetime(*parsed[:6], tzinfo=timezone.utc)
            except (TypeError, ValueError):
                continue
    return None


def get_rss_image(entry) -> str:
    """RSS entry'den görsel adayı çıkar."""
    # Check media:thumbnail
    media_thumbnail = getattr(entry, "media_thumbnail", [])
    if media_thumbnail and isinstance(media_thumbnail, list) and 'url' in media_thumbnail[0]:
        return media_thumbnail[0]['url']
    # Check media:content
    media_content = getattr(entry, "media_content", [])
    if media_content and isinstance(media_content, list) and 'url' in media_content[0]:
        return media_content[0]['url']
    # Check enclosures
    enclosures = getattr(entry, "enclosures", [])
    for enc in enclosures:
        if getattr(enc, "type", "").startswith("image/") and hasattr(enc, "href"):
            return enc.href
    # Check links
    links = getattr(entry, "links", [])
    for link in links:
        if getattr(link, "rel", "") == "enclosure" and getattr(link, "type", "").startswith("image/") and hasattr(link, "href"):
            return link.href
    # Fallback: first <img src> in content
    content_str = ""
    if hasattr(entry, "content") and entry.content:
        content_str = entry.content[0].value
    elif hasattr(entry, "summary"):
        content_str = entry.summary
    m = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', content_str, re.IGNORECASE)
    if m:
        return m.group(1)
    return ""

def fetch_feed(name: str, url: str) -> tuple[list[dict], bool]:
    """Tek bir RSS feed'i çek; (makale listesi, basarili_mi) döndürür.

    basarili=False SADECE bir istisna (ağ/parse hatası) durumunda döner.
    Feed'in gerçekten 0 entry döndürmesi (o gün yeni advisory yok) hata
    SAYILMAZ — aksi halde fetch_all_feeds'teki toplam-başarısızlık eşiği
    sakin/az üretken kaynakları da yanlışlıkla alarm sebebi sayardı.
    """
    try:
        feed = _download_feed(url)
        articles = []
        # Her entry'den standart alanları çıkar (RSS/Atom uyumluluğu için getattr)
        for entry in feed.entries:
            articles.append({
                "title": getattr(entry, "title", ""),
                "link": getattr(entry, "link", getattr(entry, "id", "")),
                "pubDate": getattr(entry, "published", getattr(entry, "updated", "")),
                "isoDate": getattr(entry, "published", getattr(entry, "updated", "")),
                # feedparser'ın kendi ayrıştırdığı struct_time — string
                # ayrıştırmadan ÇOK daha güvenilir; kaynağa özgü tarih
                # formatları (Debian, cvefeed, JPCERT) burada zaten çözülmüş
                # oluyor. Bu alan olmadan o makaleler tarih ayrıştırılamadığı
                # için sessizce 24 saat süzgecine takılıp düşüyordu.
                "parsed_date": _entry_datetime(entry),
                "description": getattr(entry, "summary", ""),
                # content:encoded varsa kullan (Atom'da daha zengin içerik)
                "content_encoded": (
                    entry.content[0].value if hasattr(entry, "content") and entry.content else ""
                ),
                "image_candidate": get_rss_image(entry),
                "source": name,
            })
        return articles, True
    except Exception as e:
        # Bir feed çökse de diğerleri devam eder
        log.warning("Feed %s failed: %s", name, e)
        return [], False


def fetch_all_feeds() -> tuple[list[dict], int]:
    """Tüm FEEDS listesini 10 paralel worker ile çek, hepsini birleştir.

    (tüm makaleler, başarısız feed sayısı) döner — ikincisi main()'in
    toplam bir kesinti mi yoksa sakin/normal bir gün mü olduğunu ayırt
    edebilmesi için (bkz. _feed_failure_alert_gerekli).
    """
    all_articles = []
    failed = 0
    with ThreadPoolExecutor(max_workers=10) as pool:
        # Her feed için bir future oluştur
        futures = {pool.submit(fetch_feed, name, url): name for name, url in FEEDS}
        # Tamamlananları sırayla işle (sırasız geliyor, as_completed ile)
        for future in as_completed(futures):
            name = futures[future]
            try:
                articles, basarili = future.result()
                log.info("  %s: %d articles", name, len(articles))
                all_articles.extend(articles)
                if not basarili:
                    failed += 1
            except Exception as e:
                # fetch_feed kendi içindeki tüm istisnaları zaten yutuyor —
                # buraya düşmesi beklenmez, yine de savunma amaçlı sayılır
                log.warning("  %s: error — %s", name, e)
                failed += 1
    return all_articles, failed


# Feed'lerin bu orandan fazlası hata verirse muhtemel bir DNS/ağ kesintisi
# ya da genel bir kod regresyonu vardır — sessizce "temiz gün" maili atmak
# yerine main() burada fail-loud olur (bkz. modül başı _load_json_env
# docstring'indeki aynı ilke).
FEED_FAILURE_ALERT_RATIO = 0.5


def _feed_failure_alert_gerekli(basarisiz: int, toplam: int) -> bool:
    """Saf/test edilebilir eşik kontrolü — main() içine gömülü olsaydı test edilemezdi."""
    return toplam > 0 and (basarisiz / toplam) > FEED_FAILURE_ALERT_RATIO


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  FILTERING & MATCHING
#  3 aşamalı süzgeç:
#    1. Son 24 saat filtresi
#    2. HIGH_SIGNAL kelime kontrolü (güvenlik haberi mi?)
#    3. Envanter eşleşmesi (önce exact, sonra alias)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def parse_date(date_str: str) -> datetime | None:
    """RSS'den gelen farklı tarih formatlarını datetime'a çevir.

    Çoklu format dener: RFC 822 (RSS), ISO 8601 (Atom), basit tarih vb.
    Hiçbiri uymazsa Python'un email.utils.parsedate_to_datetime'ını dener.
    Timezone yoksa UTC varsayar.
    """
    if not date_str:
        return None
    # Yaygın tarih formatlarını sırayla dene
    for fmt in (
        "%a, %d %b %Y %H:%M:%S %z",      # RSS: "Mon, 17 May 2026 12:00:00 +0000"
        "%a, %d %b %Y %H:%M:%S %Z",      # RSS: "Mon, 17 May 2026 12:00:00 GMT"
        "%Y-%m-%dT%H:%M:%S%z",           # Atom: "2026-05-17T12:00:00+00:00"
        "%Y-%m-%dT%H:%M:%S.%f%z",        # Atom microsecond ile
        "%Y-%m-%dT%H:%M:%SZ",            # ISO UTC suffix
        "%Y-%m-%d %H:%M:%S",             # SQL benzeri
        "%Y-%m-%d",                      # Sadece tarih
    ):
        try:
            dt = datetime.strptime(date_str.strip(), fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    # Son çare: Python'un email tarih parser'ı (esnek)
    try:
        import email.utils
        parsed = email.utils.parsedate_to_datetime(date_str)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except Exception:
        return None


def filter_recent(articles: list[dict], hours: int = 24) -> list[dict]:
    """Sadece son N saatteki makaleleri tut (varsayılan 24 saat).

    Tarih iki kanaldan okunur: önce feedparser'ın kendi ayrıştırdığı
    struct_time (güvenilir), o yoksa string ayrıştırma. Hiçbiri işe
    yaramazsa makale ATILMAZ — tarihsiz bırakılıp elde tutulur, çünkü
    "tarihi okunamadı" ile "eski haber" aynı şey değildir ve sessizce
    atmak advisory kaybına yol açıyordu.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    recent = []
    undated = 0
    for a in articles:
        dt = a.get("parsed_date") or parse_date(a.get("isoDate") or a.get("pubDate", ""))
        if dt is None:
            undated += 1
            recent.append(a)
            continue
        if dt >= cutoff:
            recent.append(a)
    if undated:
        log.info("  %d makalenin tarihi okunamadı — elde tutuldu", undated)
    return recent


_TITLE_TOKEN_RE = re.compile(r"[0-9a-zà-ÿ]+")
# Başlık benzerliğinde ayırt edici olmayan kelimeler
_STOPWORDS = frozenset("""
a an the of in on for to and or with new via as at by is are was were
yeni ile ve bir bu
""".split())
# İki başlığın "aynı haber" sayılması için gereken token örtüşmesi
_TITLE_SIMILARITY_THRESHOLD = 0.75
# Bu sayıdan fazla CVE içeren makale bir "toplu derleme"dir (Patch Tuesday
# gibi); CVE tabanlı dedup'a sokulmaz, yoksa tek tek CVE haberlerini yutar.
_MAX_CVES_FOR_DEDUP = 3


def _title_tokens(norm_title: str) -> frozenset[str]:
    """Başlığı ayırt edici kelime kümesine indirge (dedup için)."""
    return frozenset(
        t for t in _TITLE_TOKEN_RE.findall(norm_title)
        if t not in _STOPWORDS and len(t) > 2
    )


def _is_near_duplicate(tokens: frozenset[str], seen: list[frozenset[str]]) -> bool:
    """Başlık daha önce görülen bir başlıkla büyük ölçüde örtüşüyor mu?

    Aynı olay 5 farklı feed'den "Fortinet FortiWeb RCE Exploited in Attacks"
    ve "Hackers Exploit FortiWeb RCE Vulnerability" gibi farklı başlıklarla
    geliyordu; birebir başlık karşılaştırması bunları yakalayamıyor ve
    brifingde aynı haber tekrar tekrar yer alıyordu.
    """
    if not tokens:
        return False
    for prev in seen:
        if not prev:
            continue
        overlap = len(tokens & prev) / min(len(tokens), len(prev))
        if overlap >= _TITLE_SIMILARITY_THRESHOLD:
            return True
    return False


# Bu uzunluğun altındaki TEK KELİMELİK ürün adları jenerik sayılır
_SPECIFIC_NAME_MIN_LEN = 10


def _is_specific_name(name: str) -> bool:
    """Ürün adı tek başına eşleşmeye yetecek kadar ayırt edici mi?

    Model numarası veya birden fazla kelime içeren bir ürün adı bir makalede
    geçiyorsa haber gerçekten o ürünle ilgilidir. Ama tek kelimelik, yaygın
    işletim sistemi / programlama dili adları neredeyse HER güvenlik haberinin
    gövdesinde bir kez geçer — bambaşka bir platformun malware analizinde bile.
    Bu tür adların gövdede tek geçişi eşleşme için yeterli sayılmamalı.
    """
    return (
        " " in name or "-" in name
        or any(ch.isdigit() for ch in name)
        or len(name) >= _SPECIFIC_NAME_MIN_LEN
    )


# Jenerik bir ürün adının hemen ardından (~20 karakter içinde) bir sürüm
# numarası gelmesi ("PHP 8.3.12", "Apache 2.4.67"), o haberin GERÇEKTEN
# o ürünün kendisiyle ilgili olduğuna dair güçlü bir sinyaldir.
_NEARBY_VERSION_RE = re.compile(rf"\s{{0,20}}{_VER_LOOSE}")


def _has_nearby_version(text: str, pattern: re.Pattern) -> bool:
    """`pattern`'ın metindeki herhangi bir geçişinin hemen ardından bir
    sürüm numarası var mı? (bkz. _NEARBY_VERSION_RE yorumu)

    "index.php", "usr-check.php" gibi dosya adı/uzantısı kullanımları bu
    döngüye hiç GİRMEZ — `pattern`'ın kendisi (bkz. _compile) bir noktadan
    hemen sonra gelen eşleşmeleri zaten üretmiyor. Ayrı bir "bu bir dosya
    uzantısı mı?" filtresi tutmak yerine, sınırı KAYNAĞINDA (regex'in
    kendisinde) doğru tanımlamak, bu fonksiyonun ve _title_match_is_genuine'in
    o filtreyi ayrı ayrı hatırlaması gerekliliğini tamamen ortadan kaldırır.
    """
    return any(
        _NEARBY_VERSION_RE.match(text, m.end())
        for m in pattern.finditer(text)
    )


# Zafiyet SINIFI adları: jenerik bir ürün adının hemen ardından bunlardan
# biri geliyorsa ("PHP Object Injection", "SQL Injection"), o ürün adı
# orada bir DİL/PLATFORM niteleyicisi olarak kullanılıyordur — advisory'nin
# KONUSU o ürün değil, adı geçen BAŞKA bir yazılımdaki (genelde bir WordPress
# eklentisi) bir kusurun CWE tipidir. Bu ifadeler VulDB/cvefeed gibi
# kaynaklarda HER ZAMAN başlığın kendisinde geçtiği için, salt "başlıkta
# geçiyor" kontrolü bunları elemeye yetmiyordu (2026-08-27: "php" ile
# eşleşen 4 haberin 4'ü de bu desendi, ilgisiz WordPress eklenti zafiyetiydi).
_VULN_CLASS_PHRASES = (
    "object injection", "sql injection", "code injection", "command injection",
    "cross-site scripting", "cross site scripting", "remote file inclusion",
    "local file inclusion", "path traversal", "directory traversal",
    "server-side request forgery", "cross-site request forgery",
    "deserialization", "type juggling", "template injection",
    "xml external entity", "ldap injection", "header injection",
)
_VULN_CLASS_RE = re.compile(
    r"\s{0,3}(?:" + "|".join(re.escape(p) for p in _VULN_CLASS_PHRASES) + ")",
    re.IGNORECASE,
)


def _title_match_is_genuine(norm_title: str, pattern: re.Pattern) -> bool:
    """Başlıkta jenerik ad geçiyor VE bu geçiş bir zafiyet sınıfı
    ifadesinin (bkz. _VULN_CLASS_PHRASES) parçası DEĞİL mi?

    Aynı başlıkta hem "gerçek" hem "sınıf-adı" kullanımı bir arada olabilir
    (nadir) — o yüzden İLK eşleşmede değil, HERHANGİ bir eşleşmede genuine
    olan varsa kabul edilir.
    """
    return any(
        _VULN_CLASS_RE.match(norm_title, m.end()) is None
        for m in pattern.finditer(norm_title)
    )


def _find_product(text: str, norm_title: str,
                  patterns: list[tuple[str, re.Pattern]]) -> str | None:
    """Metinde eşleşen en spesifik ürün adını bul.

    patterns UZUNDAN KISAYA sıralı gelir; ilk eşleşme en spesifik olandır.
    Jenerik (tek kelimelik, kısa) adlar İKİ durumdan birinde kabul edilir:
      - bir sürüm numarasıyla BİTİŞİK geçiyorsa (bkz. _has_nearby_version), VEYA
      - başlıkta, bir zafiyet SINIFI ifadesinin parçası OLMADAN geçiyorsa
        (bkz. _title_match_is_genuine)
    Aksi halde (bağlamsız, tek başına geçiş — ya da sadece bir zafiyet
    sınıfının niteleyicisi olarak geçiş) reddedilir.

    2026-08-27: Eskiden "metinde HERHANGİ bir CVE varsa kabul et" ve "başlıkta
    HERHANGİ bir geçiş yeterli" gibi gevşek kurallar vardı; VulDB/cvefeed gibi
    kaynaklar HER başlığa hem CVE numarası hem zafiyet sınıfı adını koyduğu
    için bu kurallar pratikte hiçbir şeyi elemiyordu. "index.php" gibi dosya
    uzantısı kullanımları ayrı bir yanlış pozitif kaynağıydı — bunlar için
    ayrı bir filtre fonksiyonu tutmak yerine sınır regex'i (bkz. _compile)
    düzeltildi, bu yüzden burada görünmezler.
    """
    for name, pattern in patterns:
        if not pattern.search(text):
            continue
        if _is_specific_name(name):
            return name
        if _has_nearby_version(text, pattern) or _title_match_is_genuine(norm_title, pattern):
            return name
    return None


def _product_pattern(name: str) -> re.Pattern:
    """Bir ürün/alias adı için kelime-sınırlı eşleşme deseni derle.

    match_articles() içindeki tüm ürün ve alias adları BU fonksiyonla
    derlenir — sınırın tanımı (özellikle noktanın sol sınırda dışlanması,
    bkz. aşağıdaki yorum) TEK bir yerde yaşar. Testler de dahil hiçbir
    çağıran kendi regex'ini elle kopyalamamalı; öyle yapılırsa (2026-08-27'de
    test_product_match.py'de olduğu gibi) sınır burada düzeltildiğinde
    kopya sessizce eskimiş kalır.

    Sol sınır noktayı da (".") dışlar: "index.php", "usr-check.php" gibi
    dosya adı/uzantısı kullanımları üçüncü parti bir ürünün iç dosya
    yapısıdır, bizim envanterimizdeki dilin/platformun kendisiyle ilgisi
    yoktur (Veno File Manager/LimeSurvey CVE'leri "php" ile yanlış
    eşleşiyordu). Sağ sınır da (2026-09-11) noktayı dışlayacak şekilde SOL
    ile SİMETRİK hale getirildi — önceden sadece sol taraf noktayı
    dışlıyordu, bu da "php.net", "cisco.com" gibi üçüncü-taraf alan adı
    kullanımlarının sağ tarafta yanlışlıkla ürünün kendisi sanılmasına yol
    açıyordu.

    NOT: Kısa çizgi ("-") her iki tarafta da (ilk commit'ten beri) sınır
    kabul edilir — yani "PHP-based" gibi bitişik-tireli kullanımlar
    eşleşMEZ. Bu AYRI, önceden var olan bir tasarım kararı; bilinçli
    olarak DEĞİŞTİRİLMEDİ — 4e0f372 commit'indeki gibi canlı veriyle
    (kaç haber kazanılır/kaybedilir) ölçülmeden bu davranış değiştirilmemeli.
    """
    return re.compile(rf"{_SOL_SINIR}{re.escape(name)}(?![\w.-])", re.IGNORECASE)


def match_articles(articles: list[dict]) -> list[dict]:
    """Makaleleri envantere göre eşleştir, puanla ve sırala.

    Akış:
      1. Gürültü başlıklarını at (webinar/podcast/pazarlama)
      2. HIGH_SIGNAL kelime yoksa at (kelime sınırlı — "source" artık RCE değil)
      3. Ürün eşleştir: en SPESİFİK ad kazanır, tek kelimelik jenerik
         adlar ek kanıt ister (başlıkta geçmeli ya da bir sürüm numarasıyla
         bitişik geçmeli — bkz. _find_product/_has_nearby_version)
      4. Yinelenenleri ele: başlık benzerliği + CVE örtüşmesi
      5. Öncelik puanı hesapla ve sırala
    """
    # Envanteri normalize et; UZUN adlar önce denensin ki en spesifik ürün
    # kazansın (model numarası içeren tam ad, aynı vendor'un kısa adı yerine).
    # Eskiden envanterdeki rastgele sıra hangi ürünün eşleşeceğini belirliyordu
    # ve brifingde haberle ilgisiz, daha genel bir ürün adı görünebiliyordu.
    exact_products = sorted(
        {norm(p) for p in INVENTORY if len(norm(p)) >= 3},
        key=len, reverse=True,
    )

    # Sadece envanterde olan vendor'ların alias'larını aktif et
    active_aliases = []
    for entry in VENDOR_ALIASES:
        if any(entry["vendor_key"] in p for p in exact_products):
            active_aliases.extend(norm(a) for a in entry["aliases"])
    active_aliases.sort(key=len, reverse=True)

    # Ürün adı → derlenmiş kelime-sınırlı desen (bkz. _product_pattern).
    # Her makalede yeniden compile etmek 160 ürün × 500 makale = 80.000
    # gereksiz compile demekti; bir kez derlenip döngü boyunca kullanılır.
    product_patterns = [(p, _product_pattern(p)) for p in exact_products]
    alias_patterns = [(a, _product_pattern(a)) for a in active_aliases]

    seen_token_sets: list[frozenset[str]] = []
    seen_cves: set[str] = set()
    matches = []

    for article in articles:
        raw_content = article.get("content_encoded") or article.get("description", "")
        title = article.get("title", "")
        norm_title = norm(title)
        if not norm_title:
            continue

        # 1) Pazarlama/etkinlik gürültüsü — sadece başlıkta aranır
        if _NOISE_TITLE_RE.search(norm_title):
            continue

        # İçeriği temizle ve eşleştirme metnini oluştur
        clean_content = norm(strip_html(raw_content))[:3000]
        text = norm_title + " " + clean_content

        # 2) HIGH_SIGNAL kelime yoksa güvenlik haberi değil — atla
        if not has_high_signal(text):
            continue

        # 3) Yinelenen başlık (birebir veya yakın benzer)
        tokens = _title_tokens(norm_title)
        if _is_near_duplicate(tokens, seen_token_sets):
            continue

        # 4) Ürün eşleştir — önce envanter adları, sonra alias'lar
        matched_product = _find_product(text, norm_title, product_patterns)
        if not matched_product:
            matched_product = _find_product(text, norm_title, alias_patterns)
        if not matched_product:
            continue

        # 5) CVE tabanlı çapraz-feed dedup: aynı CVE'yi işleyen ikinci haber
        #    aynı brifingde ayrı blok olarak yer kaplamasın.
        cves = {c.upper() for c in _CVE_RE.findall(text)}
        if 0 < len(cves) <= _MAX_CVES_FOR_DEDUP and cves & seen_cves:
            continue

        seen_token_sets.append(tokens)
        seen_cves |= cves
        matches.append({
            "title": title,
            "link": article.get("link", ""),
            "pubDate": article.get("pubDate", ""),
            "matched_product": matched_product,
            # 2026-09-10: eskiden burada "content": clean_content[:500] vardı —
            # KÜÇÜK HARFE çevrilmiş (norm()) ve 500 karaktere kırpılmıştı, tek
            # tüketicisi _analiz_metni idi. Bazı kaynaklar (Cisco Talos ~27K,
            # Cloudflare ~9K, US-CERT ~19K karakter) content_encoded/description
            # alanında TAM makaleyi veriyor; o zenginlik 500'e kırpılınca hiç
            # modele ulaşmıyordu. Artık orijinal büyük/küçük harfle, RSS_BODY_CHARS
            # tavanına kadar taşınıyor — analiz kalitesi VE (rss_metni yeterince
            # zenginse) sayfa çekmeyi atlama kararı (bkz. main) buna dayanıyor.
            # KURAL: bu dict'teki İngilizce alan adları yapısal/meta veri
            # taşır; "rss_metni" bilinçli bir istisna — ham/zengin metin
            # taşıyan tek alan olduğu için Türkçe bırakıldı.
            "rss_metni": strip_html(raw_content)[:RSS_BODY_CHARS],
            "priority_score": score_article(text, norm_title, matched_product),
            "image_candidate": article.get("image_candidate", ""),
        })

    # Öncelik puanına göre sırala (en kritik haberler önce, ilk MAX_GEMINI_ARTICLES tanesi Gemini'ye gider)
    matches.sort(key=lambda x: x["priority_score"], reverse=True)
    return matches


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  ANALİZ MOTORU — haber başına Gemini çağrısı (map fazı)
#  Her makale KENDİ isteğinde, tam metniyle analiz edilir. Neden böyle:
#  bkz. ANALYSIS_MODELS yanındaki "MİMARİ DEĞİŞİKLİĞİ" notu.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# ── CVE kayıtları: sürüm bilgisinin YETKİLİ kaynağı ──────────────────────────
# 2026-09-10, ölçümle bulundu: Modele giden makale metninin MEDYANI 1572 karakter,
# makalelerin yarısı 1500 karakterin altında (VulDB bot isteklerini 403'lüyor,
# MSRC sayfaları JS ile render ediliyor). Yani sürüm alanlarının boş kalmasının
# sebebi modelin beceriksizliği DEĞİL — o metinlerde sürüm bilgisi HİÇ YOK.
# Ne regex ne de dil modeli, var olmayan veriyi çıkaramaz.
#
# Ama bu haberlerin başlığı literal olarak "CVE-2026-0307 | ..." biçiminde ve
# CVE kayıtlarının resmi, yapılandırılmış ve ücretsiz bir kaynağı var. Örnek:
# 297 karakterlik bir VulDB özetinden hiçbir sürüm çıkmazken, aynı CVE'nin
# CNA kaydı "6.3.0 → 6.3.3-h15'ten küçük, 6.2.0 → 6.2.8-h14, 6.0.0 → 6.0.15"
# veriyor — hem etkilenen hem yamalı sürümü.
#
# Bu, modele KURAL yazmak değil, daha iyi VERİ vermektir: yorumlamayı yine
# model yapar, biz sadece doğru kaynağı önüne koyarız.
CVE_API_URL = "https://cveawg.mitre.org/api/cve/{cve}"
CVE_API_TIMEOUT = 10
MAX_CVE_PER_ARTICLE = 3      # Patch Tuesday derlemeleri onlarca CVE içerebiliyor
_cve_cache: dict[str, str] = {}          # Aynı CVE birden çok haberde geçebilir
_cve_cache_lock = threading.Lock()


def _format_cve_versions(data: dict) -> str:
    """CVE kaydından insan/model okunur sürüm özeti çıkar.

    CNA kayıtları sürümü birkaç farklı biçimde yazıyor (`version` + `lessThan`,
    `lessThanOrEqual`, ya da sadece `version`). Hepsi tek bir okunabilir
    satıra indirgenir; modele yorumlaması için ham yapı değil, düz metin verilir.
    """
    cna = data.get("containers", {}).get("cna", {})
    satirlar = []
    for etkilenen in cna.get("affected", [])[:6]:
        urun = " ".join(
            p for p in (etkilenen.get("vendor"), etkilenen.get("product")) if p and p != "n/a"
        )
        parcalar = []
        for v in etkilenen.get("versions", [])[:8]:
            if v.get("status") != "affected":
                continue
            baslangic = v.get("version")
            if v.get("lessThan"):
                parcalar.append(f"{baslangic} ile {v['lessThan']} arası ({v['lessThan']} hariç)")
            elif v.get("lessThanOrEqual"):
                parcalar.append(f"{baslangic} – {v['lessThanOrEqual']} (dahil)")
            elif baslangic:
                parcalar.append(str(baslangic))
        if urun and parcalar:
            satirlar.append(f"  {urun}: {', '.join(parcalar)}")
    return "\n".join(satirlar)


def fetch_cve_record(cve_id: str) -> str:
    """Tek bir CVE'nin resmi kaydından sürüm özetini getir (önbellekli).

    Kayıt bulunamazsa veya istek başarısız olursa boş string döner — analiz
    her hâlükârde makale metniyle devam eder, bu bir ZENGİNLEŞTİRME'dir.
    """
    with _cve_cache_lock:
        if cve_id in _cve_cache:
            return _cve_cache[cve_id]
    sonuc = ""
    try:
        resp = requests.get(
            CVE_API_URL.format(cve=cve_id),
            timeout=CVE_API_TIMEOUT,
            headers={"User-Agent": _FEED_UA_PRIMARY, "Accept": "application/json"},
        )
        if resp.status_code == 200:
            sonuc = _format_cve_versions(resp.json())
    except Exception as exc:
        log.debug("CVE kaydı alınamadı (%s): %s", cve_id, exc)
    with _cve_cache_lock:
        _cve_cache[cve_id] = sonuc
    return sonuc


def fetch_cve_context(articles: list[dict], bodies: dict[str, str]) -> dict[str, str]:
    """Makalelerde geçen CVE'lerin resmi sürüm kayıtlarını paralel topla.

    Döndürülen sözlük {makale linki: sürüm bağlamı} biçimindedir; boş değer
    "bu makale için ek veri yok" demektir.

    Tarama girdisi başlık + RSS gövdesi + sayfa metni — SADECE sayfa değil.
    2026-09-10: sayfa hiç çekilmemiş olabilir (main()'deki ZENGIN_RSS_ESIGI
    kısayolu) ya da JS kabuğu olabilir (MSRC); o durumlarda CVE kimliği
    yalnızca RSS metninde geçiyor olabilir — taranmazsa hiç yakalanmazdı.
    """
    makale_cveleri: dict[str, list[str]] = {}
    tum_cveler: set[str] = set()
    for a in articles:
        rss = (a.get("rss_metni") or "")[:4000]
        sayfa = bodies.get(a.get("link", ""), "")[:4000]
        metin = f"{a.get('title', '')} {rss} {sayfa}"
        # dict.fromkeys: sırayı koruyarak tekilleştir (ilk geçen CVE en alakalısı)
        cveler = list(dict.fromkeys(c.upper() for c in _CVE_RE.findall(metin)))[:MAX_CVE_PER_ARTICLE]
        if cveler:
            makale_cveleri[a.get("link", "")] = cveler
            tum_cveler.update(cveler)

    if not tum_cveler:
        return {}

    log.info("Fetching %d CVE records for authoritative version data...", len(tum_cveler))
    with ThreadPoolExecutor(max_workers=8) as pool:
        pool.map(fetch_cve_record, tum_cveler)

    baglamlar: dict[str, str] = {}
    for link, cveler in makale_cveleri.items():
        bloklar = [f"{c}:\n{fetch_cve_record(c)}" for c in cveler if fetch_cve_record(c)]
        if bloklar:
            baglamlar[link] = "\n".join(bloklar)
    log.info("  %d/%d makale için resmi sürüm verisi bulundu",
             len(baglamlar), len(articles))
    return baglamlar


def fetch_article_bodies(articles: list[dict]) -> tuple[dict[str, str], dict[str, str]]:
    """Makale sayfalarını paralel indir; {link: metin} ve {link: og_image} döndür.

    Hem analiz (tam metin) hem görsel (og:image) bu tek geçişi kullanır —
    aynı sayfayı iki kez indirmeye gerek yok. İndirilemeyen sayfa için metin
    boş kalır; analiz o zaman RSS özetiyle devam eder (bkz. analyze_articles).
    """
    bodies: dict[str, str] = {}
    og_images: dict[str, str] = {}
    log.info("Fetching %d article pages...", len(articles))
    with ThreadPoolExecutor(max_workers=8) as pool:
        future_map = {
            pool.submit(fetch_article_page, a.get("link", "")): a.get("link", "")
            for a in articles
        }
        for future in as_completed(future_map):
            link = future_map[future]
            try:
                bodies[link], og_images[link] = future.result()
            except Exception:
                bodies[link], og_images[link] = "", ""
    bos = sum(1 for b in bodies.values() if not b)
    if bos:
        log.info("  %d makale sayfası indirilemedi — RSS özetiyle analiz edilecek", bos)
    return bodies, og_images



_PERMANENT_KEYWORDS = (
    "api key not valid", "api_key_invalid", "permission denied",
    "invalid argument", "unauthenticated",
)
_QUOTA_KEYWORDS = ("resource_exhausted", "quota", "rate limit", "not_found", "429")


def _is_quota_error(exc: Exception) -> bool:
    """Hata, bu modelin günlük/dakikalık kotasının dolduğunu mu gösteriyor?

    Kota hatası alan bir worker kendi modelini bırakır — aynı modele tekrar
    tekrar vurmak kotayı geri getirmez, sadece kalan makaleleri geciktirir.
    """
    msg = str(exc).lower()
    code = getattr(exc, "code", None)
    return code in (429, 404) or any(k in msg for k in _QUOTA_KEYWORDS)


def _is_permanent_error(exc: Exception) -> bool:
    """Hata kalıcı mı (geçersiz anahtar/istek)? Bunda hiçbir model işe yaramaz."""
    msg = str(exc).lower()
    code = getattr(exc, "code", None)
    return code in (400, 401, 403) or any(k in msg for k in _PERMANENT_KEYWORDS)


# 2026-09-10, ikinci geçiş: _analiz_metni eskiden RSS + sayfayı HER ZAMAN
# birleştiriyordu. Artık ikisi de TEMİZ (rss_metni artık orijinal harfli/tam,
# sayfa metni artık şablonsuz — bkz. _ana_metin_cikar), bu yüzden uzunluk
# "hangisi daha bilgi dolu" için dürüst bir vekil oldu: daha UZUN olan
# birincil kaynak seçilir, ikincisi sadece belirgin şekilde FARKLI bilgi
# taşıyorsa (düşük kelime örtüşmesi) etiketli ek blok olarak eklenir.
# Amaç: aynı içeriği iki kez göndermemek, ama RSS'in sayfada olmayan bir
# sürüm tablosu taşıdığı nadir durumu da kaybetmemek.
_JS_KABUK_ESIGI = 500       # Bunun altındaki sayfa metni JS kabuğu/boş sayılır
_IKINCIL_MIN = 300          # İkincil kaynağı eklemeye değmesi için asgari uzunluk
_IKINCIL_ORTAKLIK = 0.5     # Kelime örtüşmesi bunun üstündeyse "zaten aynı metin"
_IKINCIL_TAVAN = 3_000


def _kelime_ortaklik(kucuk: str, buyuk: str) -> float:
    """kucuk'teki 5+ harfli benzersiz kelimelerin kaçta kaçı buyuk'te de var?

    Yeni bağımlılık istemeyen kaba "yakın kopya mı" sezgisi — tam bir metin
    benzerlik algoritması değil, sadece "ikincil kaynak birincilin tekrarı mı,
    yoksa gerçekten farklı bilgi mi taşıyor" sorusuna ucuz bir cevap.
    Kısa kelimeler ("ve", "bir", "for") her metinde geçtiği için ayırt edici
    değildir, elenirler.
    """
    a = set(re.findall(r"[a-zçğıöşü0-9]{5,}", kucuk.lower()))
    if not a:
        return 1.0  # boş metin "zaten örtüşüyor" sayılır — eklemeye değmez
    b = set(re.findall(r"[a-zçğıöşü0-9]{5,}", buyuk.lower()))
    return len(a & b) / len(a)


def _analiz_metni(article: dict, sayfa_metni: str) -> str:
    """Analize gidecek metni SEÇ: RSS gövdesi vs sayfa metni, hangisi daha iyiyse o.

    - Sayfa metni _JS_KABUK_ESIGI altındaysa (MSRC gibi JS-render sayfalar
      temizlik sonrası ~58 karakter bırakıyor; ya da sayfa hiç çekilmediyse —
      bkz. main()'deki ZENGIN_RSS_ESIGI kısayolu) YOK sayılır, RSS'e düşülür.
    - İkisi de geçerliyse daha UZUN olan birincil kaynaktır.
    - İkincil kaynak yalnızca hem asgari uzunluktaysa (_IKINCIL_MIN) hem de
      birincilden belirgin şekilde FARKLIYSA (_kelime_ortaklik düşük) ve
      birincilde yer varsa etiketli ek blok olarak eklenir.
    """
    rss = (article.get("rss_metni") or "").strip()
    sayfa = (sayfa_metni or "").strip()

    if len(sayfa) < _JS_KABUK_ESIGI:
        return rss or sayfa   # sayfa güvenilmez/boş — elde RSS varsa o, yoksa ne varsa
    if not rss:
        return sayfa

    if len(sayfa) >= len(rss):
        birincil, birincil_etiket = sayfa, "Makale sayfası"
        ikincil, ikincil_etiket = rss, "RSS gövdesi"
    else:
        birincil, birincil_etiket = rss, "RSS gövdesi"
        ikincil, ikincil_etiket = sayfa, "Makale sayfası"

    yer_var = len(birincil) < GEMINI_BODY_CHARS - 500
    if (len(ikincil) >= _IKINCIL_MIN and yer_var
            and _kelime_ortaklik(ikincil, birincil) < _IKINCIL_ORTAKLIK):
        return (f"{birincil_etiket}:\n{birincil}\n\n"
                f"[Ek kaynak — {ikincil_etiket}]:\n{ikincil[:_IKINCIL_TAVAN]}")
    return birincil


def build_article_prompt(article: dict, body: str, cve_baglami: str = "") -> str:
    """Tek bir makale için kullanıcı mesajını oluştur.

    Model rolünü ve alan kurallarını SYSTEM_PROMPT taşır; burada sadece
    o makaleye ait ham veriler var. Sürüm bilgisi için TAM metin verilir —
    çoğu advisory sürümleri metnin ilerleyen kısımlarında yazar.

    `cve_baglami` doluysa (bkz. fetch_cve_context) makale metninin ÜSTÜNE
    konur: haber metni kısa/eksik olduğunda bile modelin elinde sürümlerin
    yetkili kaynağı bulunur.
    """
    parcalar = [
        f"Başlık: {article.get('title', '')}",
        f"Yayın tarihi: {article.get('pubDate', 'Bilinmiyor')}",
        f"Ortamımızda eşleşen ürün: {article.get('matched_product', '')}",
        f"Kaynak: {article.get('link', '')}",
    ]
    if cve_baglami:
        parcalar.append(
            "\nResmi CVE kaydından etkilenen sürümler (YETKİLİ KAYNAK — haber "
            "metniyle çelişirse buna güven):\n" + cve_baglami
        )
    parcalar.append(f"\nMakale metni:\n{body[:GEMINI_BODY_CHARS]}")
    return "\n".join(parcalar)


_WHITESPACE_RE = re.compile(r"\s+")


def _normalize_model_metni(deger: str) -> str:
    """Model çıktısındaki fazla/kaçak boşlukları teke indir.

    Canlı örnek (2026-09-11): yamali_surumler = "Belirtilmemiş \nT\n\n\n\n
    \n\n\n\n\n\n\n\n\n\n\n\n— kaynağı kontrol edin" — modelin kendi JSON
    string değerine sıkıştırdığı fazladan whitespace/karakter. html.escape()
    (render_briefing_block'taki alan() helper'ı) bunu AYNEN geçirdiği için
    brifingde görsel bozukluk olarak çıkıyordu.
    """
    return _WHITESPACE_RE.sub(" ", deger).strip()


def analyze_one_article(client, model: str, article: dict, body: str,
                        cve_baglami: str = "") -> dict:
    """Tek makaleyi analiz et ve şemaya uygun sözlük döndür.

    Hata durumunda istisna FIRLATIR — çağıran (worker) hatanın türüne göre
    (kota / kalıcı / geçici) ne yapacağına karar verir.

    Dönen sözlükteki tüm STRING alanlar (ANALYSIS_SCHEMA'daki 8 alanın
    hepsi STRING) _normalize_model_metni'den geçer. Kaynakta normalize
    etmek — render_briefing_block'un alan() helper'ında DEĞİL — önemli:
    olay_tarihi _vurgula_olay_tarihi'ye alan()'dan GEÇMEDEN ham haliyle
    veriliyor; sadece alan()'ı düzeltmek ozet'i temizleyip olay_tarihi'ni
    kirli bırakır ve tarih vurgusunu (mavi span) sessizce kırabilirdi.
    """
    response = client.models.generate_content(
        model=model,
        contents=build_article_prompt(article, body, cve_baglami),
        config=genai.types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            response_mime_type="application/json",
            response_schema=ANALYSIS_SCHEMA,
            http_options=genai.types.HttpOptions(timeout=ARTICLE_TIMEOUT_MS),
        ),
    )
    analiz = json.loads(response.text)
    return {k: (_normalize_model_metni(v) if isinstance(v, str) else v)
            for k, v in analiz.items()}


def analyze_articles(articles: list[dict], bodies: dict[str, str],
                     cve_baglamlari: dict[str, str] | None = None) -> dict[int, dict]:
    """Makaleleri paralel analiz et; {makale indeksi: analiz} döndür.

    Model başına BİR worker çalışır ve her worker kendi modelinin hız sınırına
    (MODEL_RPM) uyar. Modeller birbirinden bağımsız kotalara sahip olduğu için
    bu, global bir kilit/kuyruk gerektirmeden doğal bir hız sınırlaması sağlar.

    İki aşamalı havuz: önce ANALYSIS_MODELS (yüksek kalite). Bunların günlük
    kotası tükenip hâlâ analiz bekleyen makale kalırsa FALLBACK_MODELS devreye
    girer — "biraz daha sığ analiz", "hiç analiz yok"tan iyidir.

    Başarısız olan makale sonuçlara HİÇ girmez — çağıran onu taşma tablosuna
    düşürür. Tek bir makalenin analizi patladığında brifingin tamamının
    düşmesi (eski tek-prompt mimarisinin kırılganlığı) artık imkânsız.
    """
    if not articles:
        return {}

    pending = list(enumerate(articles))
    pending_lock = threading.Lock()
    denemeler: dict[int, int] = {}           # makale indeksi → kaç kez denendi
    results: dict[int, dict] = {}
    results_lock = threading.Lock()
    deadline = time.monotonic() + ANALYSIS_TOTAL_BUDGET_SEC
    permanent_error: list[Exception] = []

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

    def worker(model: str) -> None:
        son_istek = 0.0
        while True:
            if permanent_error or time.monotonic() >= deadline:
                return
            with pending_lock:
                if not pending:
                    return
                index, article = pending.pop(0)
                denemeler[index] = denemeler.get(index, 0) + 1

            # Bu modelin kendi hız sınırı — diğer worker'ları etkilemez
            bekle = _MODEL_MIN_INTERVAL - (time.monotonic() - son_istek)
            if bekle > 0:
                time.sleep(bekle)
            son_istek = time.monotonic()

            link = article.get("link", "")
            body = _analiz_metni(article, bodies.get(link, ""))
            try:
                analiz = analyze_one_article(
                    client, model, article, body, (cve_baglamlari or {}).get(link, "")
                )
            except Exception as exc:
                if _is_permanent_error(exc):
                    log.error("Gemini kalıcı hata (%s): %s", model, exc)
                    # pending/denemeler ile aynı disiplin — permanent_error
                    # önceden lock'suzdu, dosyanın kendi kilit kuralına aykırıydı
                    with pending_lock:
                        permanent_error.append(exc)
                    return
                kota_hatasi = _is_quota_error(exc)
                # Kota hatası bu makalenin suçu değil — deneme hakkını geri ver,
                # yoksa kotası dolan bir model makaleleri boş yere tüketir.
                with pending_lock:
                    if kota_hatasi:
                        denemeler[index] -= 1
                    if denemeler[index] < MAX_ATTEMPTS_PER_ARTICLE:
                        pending.append((index, article))
                    else:
                        log.warning("Makale %d deneme sonrası bırakıldı: %s",
                                    MAX_ATTEMPTS_PER_ARTICLE, article.get("title", "")[:60])
                if kota_hatasi:
                    log.warning("Model '%s' günlük kotası doldu, worker duruyor", model)
                    return
                log.warning("Makale analizi başarısız (%s, %s): %s",
                            model, article.get("title", "")[:60], exc)
                continue

            with results_lock:
                results[index] = analiz

    def havuzu_calistir(modeller: tuple[str, ...]) -> list[threading.Thread]:
        """Bir model havuzunu başlat, bütçe dolana/bitene kadar bekle.

        join(timeout=...) süre bütçesi dolduğunda thread hâlâ çalışıyor
        olsa bile döner (daemon thread arka planda devam eder) — çağıran
        hâlâ-canlı thread'leri görüp buna göre karar versin diye döndürülür.
        """
        threads = [
            threading.Thread(target=worker, args=(m,), name=f"analiz-{m}", daemon=True)
            for m in modeller
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=max(0.0, deadline - time.monotonic()))
        hala_calisan = [t for t in threads if t.is_alive()]
        if hala_calisan:
            log.warning("%d worker süre bütçesi dolduğunda hâlâ çalışıyordu: %s",
                        len(hala_calisan), ", ".join(t.name for t in hala_calisan))
        return hala_calisan

    canli_kalanlar = havuzu_calistir(ANALYSIS_MODELS)

    # Yedek havuz SADECE birincil havuzun TÜM thread'leri gerçekten
    # bittiyse başlar — aksi halde sürüklenen bir ANALYSIS_MODELS thread'i
    # ile yeni başlayan FALLBACK_MODELS thread'leri aynı anda pending/
    # results/permanent_error'a erişir (lock'lar veri bozulmasını önler
    # ama gereksiz çakışmayı ve "geç yazma" riskini büyütür).
    if canli_kalanlar:
        log.warning("Birincil havuzdan %d thread hâlâ canlı, yedek havuz BAŞLATILMIYOR",
                    len(canli_kalanlar))
    elif pending and not permanent_error and time.monotonic() < deadline:
        log.warning("Birincil model havuzu tükendi, %d makale için yedek havuza geçiliyor",
                    len(pending))
        havuzu_calistir(FALLBACK_MODELS)

    if permanent_error:
        raise RuntimeError(
            "Gemini API kalıcı hata (API anahtarı/istek geçersiz)"
        ) from permanent_error[0]

    if len(results) < len(articles):
        log.warning("%d/%d makale analiz edilemedi — taşma tablosuna düşecekler",
                    len(articles) - len(results), len(articles))

    # Çıplak `results` referansı yerine kilit altında bir KOPYA döndürülür:
    # bütçe dolduktan sonra hâlâ canlı kalan bir straggler thread (yukarıdaki
    # canli_kalanlar) geç bir results[index]=... yazabilir; main() ise aynı
    # anda bu sözlüğü sorted(...) ile iterate ediyor — "dictionary changed
    # size during iteration" riski. Kopya main()'e sızan referansı koparır.
    with results_lock:
        return dict(results)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  EMAIL
#  HTML şablonu, taşma tablosu, SMTP gönderim mantığı.
#  Gmail App Password ile STARTTLS üzerinden gönderilir.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Ana e-posta şablonu — {date} ve {content} replace edilir
# NOT (2026-09-11): render_briefing_block f-string kullanıyor, bu şablonlar
# (EMAIL_TEMPLATE/OVERFLOW_*/NO_THREATS_CONTENT) ise .replace("{x}", ...)
# kullanıyor — tutarsız ama bilinçli olarak DEĞİŞTİRİLMEDİ. Tam bir
# template-engine'e (Jinja2 vb.) geçiş yeni bir bağımlılık ekler (projenin
# "yeni bağımlılık yok" ilkesine aykırı) ve fayda/risk oranı düşük —
# gelecekte ele alınabilecek bir iyileştirme notu olarak burada bırakıldı.
EMAIL_TEMPLATE = """\
<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="margin:0;padding:20px;background:#f4f4f4;font-family:Arial,sans-serif;">
  <div style="max-width:700px;margin:0 auto;background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.1);">
    <div style="background:#1a1a2e;padding:24px 32px;">
      <h1 style="margin:0;color:#fff;font-size:22px;">🛡️ CTI Günlük Tehdit Brifing</h1>
      <p style="margin:6px 0 0;color:#a0a0c0;font-size:13px;">{date} — Otomatik Tarama Raporu</p>
    </div>
    <div style="padding:24px 32px;">
      {content}
    </div>
    <div style="background:#f0f0f0;padding:16px 32px;text-align:center;font-size:12px;color:#888;">
      Bu rapor CTI News Feed Automation tarafından otomatik olarak oluşturulmuştur.
    </div>
  </div>
</body>
</html>"""

# Eşleşen makale yokken gönderilen "temiz" e-posta içeriği
NO_THREATS_CONTENT = """\
<div style="padding:24px;text-align:center;">
  <p style="font-size:48px;margin:0;">✅</p>
  <h2 style="color:#28a745;">Tehdit Tespit Edilmedi</h2>
  <p style="color:#555;">Bugün envanterinizdeki ürünleri etkileyen aktif bir tehdit veya kritik güvenlik açığı tespit edilmedi.</p>
  <p style="color:#888;font-size:13px;margin-top:16px;">Sonraki taramada güncel durum tekrar bildirilecektir.</p>
</div>"""

# Taşma tablosu — MAX_GEMINI_ARTICLES üzerindeki eşleşmeler için (Gemini analizi yok, sadece liste)
OVERFLOW_HEADER = """\
<div style="margin-top:32px;padding-top:24px;border-top:2px solid #e0e0e0;">
  <h3 style="color:#495057;font-family:Arial,sans-serif;">📋 Ek Eşleşen Haberler ({count} adet)</h3>
  <p style="color:#6c757d;font-size:13px;margin-bottom:16px;">Aşağıdaki haberler envanterinizle eşleşti ancak detaylı AI analizi kapsamı dışında kaldı. Gerekirse manuel inceleme yapın.</p>
  <table style="width:100%;border-collapse:collapse;font-family:Arial,sans-serif;font-size:13px;">
    <thead>
      <tr style="background:#f8f9fa;">
        <th style="text-align:left;padding:8px;border-bottom:1px solid #dee2e6;">Haber</th>
        <th style="text-align:left;padding:8px;border-bottom:1px solid #dee2e6;">Ürün</th>
      </tr>
    </thead>
    <tbody>
"""

OVERFLOW_ROW = """\
      <tr>
        <td style="padding:8px;border-bottom:1px solid #f0f0f0;"><a href="{link}" style="color:#0366d6;text-decoration:none;">{title}</a></td>
        <td style="padding:8px;border-bottom:1px solid #f0f0f0;color:#555;">{product}</td>
      </tr>
"""

OVERFLOW_FOOTER = """\
    </tbody>
  </table>
</div>"""


def _vurgula_olay_tarihi(ozet: str, olay_tarihi: str) -> str:
    """Özet içinde geçen olay tarihini mavi renkle vurgula (kalın DEĞİL).

    Model, tarihi hem `olay_tarihi` alanında hem de özet metninin içinde
    AYNEN geçirmekle yükümlü (bkz. SYSTEM_PROMPT). Burada birebir metin
    değişimi yapılır: tarih özet içinde bulunamazsa hiçbir şey vurgulanmaz
    (bozuk HTML üretmek yerine sessizce vazgeçilir).

    Girdi ZATEN html.escape()'ten geçmiş olmalı — bu fonksiyon üretilen tek
    HTML olan <span>'i ekler.
    """
    if not olay_tarihi:
        return ozet
    kacisli_tarih = html.escape(olay_tarihi)
    if kacisli_tarih not in ozet:
        return ozet
    return ozet.replace(
        kacisli_tarih,
        f'<span style="color:#0d6efd;">{kacisli_tarih}</span>',
        1,
    )


def filter_irrelevant_analyses(
    analizler: dict[int, dict],
    top_matches: list[dict],
    overflow_matches: list[dict],
) -> tuple[dict[int, dict], list[dict]]:
    """Model 'urun_ile_ilgili_mi: false' dediği makaleleri analizlerden çıkar.

    ORİJİNAL makale dict'i (top_matches[i] — analiz sonucu DEĞİL,
    build_overflow_html 'title'/'link'/'matched_product' okuyor) olarak
    taşma listesine eklenir; hiçbir istihbarat tamamen kaybolmaz.

    Alan eksikse (beklenmez, ANALYSIS_SCHEMA'da required) fail-open: True
    varsayılır — şüpheli durumda haber ELENMEZ.
    """
    ilgisiz = {i for i, a in analizler.items() if not a.get("urun_ile_ilgili_mi", True)}
    if not ilgisiz:
        return analizler, overflow_matches
    log.info("Ürünle ilgisiz bulunan %d makale taşma tablosuna düşürüldü.", len(ilgisiz))
    yeni_overflow = [top_matches[i] for i in ilgisiz] + overflow_matches
    yeni_analizler = {i: a for i, a in analizler.items() if i not in ilgisiz}
    return yeni_analizler, yeni_overflow


# ── Görselsiz haberler için placeholder ─────────────────────────────────────
# 2026-09-14: İlk sürüm Pillow'un ImageDraw/ImageFont'uyla programatik
# çiziyordu (kalkan/daire motifleri) — kullanıcı sonucu beğenmedi. Yerine
# GERÇEK AI-üretilmiş görseller geçti: Nano Banana ücretsiz katmanda
# olmadığı için (proje billing'i bilinçli olarak kapalı) API'den ÇAĞRILMIYOR
# — kullanıcı kendi Nano Banana hesabıyla, burada verilen prompt'larla 6
# görsel üretti, dosya olarak paylaştı; buraya assets/placeholders/ altına
# 640x360 JPEG olarak optimize edilip commit edildi. Artık BURADA hiç çizim
# yok, sadece statik dosya okuma + cache.
PLACEHOLDER_WIDTH = 640
PLACEHOLDER_HEIGHT = 360
# Severite başına 2 varyant — hem severite tutarlılığı (renk) hem çeşitlilik
# (aynı görsel hep tekrar etmesin) sağlar. index % bu değer ile DETERMİNİSTİK
# seçilir (main() içinde) — rastgele DEĞİL, test edilebilir kalsın.
PLACEHOLDER_VARIANTS_PER_SEVERITY = 2
# cid Content-ID header'ına gider — ASCII zorunlu, bu yüzden severite'nin
# kendisi (Türkçe/aksanlı) değil bu slug kullanılır. Aynı slug dosya adının
# da öneki (ör. "high_1.jpg") — bkz. _load_placeholder_image.
_PLACEHOLDER_SEVERITE_SLUG = {"YÜKSEK": "high", "ORTA": "medium", "DÜŞÜK": "low"}
_PLACEHOLDER_DIR = Path(__file__).parent / "assets" / "placeholders"

_placeholder_cache: dict[tuple[str, int], tuple[str, bytes]] = {}


def _load_placeholder_image(severite: str, variant: int) -> bytes | None:
    """assets/placeholders/{slug}_{1|2}.jpg dosyasını oku (JPEG bytes).

    Dosya eksikse (beklenmez — assets/ repo ile birlikte deploy edilir) None
    döner ve sadece logla geçilir: placeholder "nice to have" bir özellik,
    eksikliği mail gönderimini ASLA engellememeli.
    """
    slug = _PLACEHOLDER_SEVERITE_SLUG.get(severite, _PLACEHOLDER_SEVERITE_SLUG["DÜŞÜK"])
    yol = _PLACEHOLDER_DIR / f"{slug}_{variant + 1}.jpg"
    try:
        return yol.read_bytes()
    except OSError as exc:
        log.warning("Placeholder görseli okunamadı (%s): %s", yol, exc)
        return None


def get_placeholder_image(severite: str, variant: int) -> tuple[str, bytes] | None:
    """Severite+variant için (cid, jpeg_bytes) döndür — process ömrü boyunca önbellekli.

    Aynı severite+variant kombinasyonu HER ZAMAN aynı cid'i döndürür — bu,
    aynı severiteyi paylaşan tüm makalelerin AYNI Content-ID'yi referans
    etmesini (ve final_images'e sadece 1 kez eklenmesini) main() tarafında
    mümkün kılar; mail boyutu aynı görseli tekrar tekrar göndererek şişmez.
    Dosya okunamazsa None döner (bkz. _load_placeholder_image).
    """
    slug = _PLACEHOLDER_SEVERITE_SLUG.get(severite, _PLACEHOLDER_SEVERITE_SLUG["DÜŞÜK"])
    variant = variant % PLACEHOLDER_VARIANTS_PER_SEVERITY
    key = (slug, variant)
    if key not in _placeholder_cache:
        veri = _load_placeholder_image(severite, variant)
        if veri is None:
            return None
        _placeholder_cache[key] = (f"placeholder_{slug}_{variant}", veri)
    return _placeholder_cache[key]


def assign_placeholder_images(
    sirali: list[tuple[int, dict]],
    cid_map: dict[int, str],
) -> tuple[dict[int, str], list[tuple[str, bytes]]]:
    """cid_map'te karşılığı olmayan (görselsiz kalan) her makaleye placeholder ata.

    ÜÇ senaryonun (aday hiç yok / process_image None döndü / görsel bütçesi
    aşıldı) TEK birleşim noktası: cid_map.get(index) boş dönen her index.
    Saf fonksiyon — I/O yok (get_placeholder_image kendi içinde cache'li disk
    okuması yapar), main()'in orkestrasyon mantığından ayrı test edilebilir.

    Döner: (genişletilmiş cid_map, final_images'e eklenecek YENİ (cid, bytes)
    çiftleri — aynı placeholder'ı paylaşan makaleler için TEKRARSIZ).
    """
    yeni_cid_map = dict(cid_map)
    kullanilanlar: dict[str, bytes] = {}
    for index, analiz in sirali:
        if index in yeni_cid_map:
            continue
        severite = analiz.get("severite", "DÜŞÜK")
        sonuc = get_placeholder_image(severite, index % PLACEHOLDER_VARIANTS_PER_SEVERITY)
        if sonuc is None:
            continue
        cid, img_bytes = sonuc
        kullanilanlar[cid] = img_bytes
        yeni_cid_map[index] = cid
    return yeni_cid_map, list(kullanilanlar.items())


def render_briefing_block(article: dict, analiz: dict, img_cid: str | None) -> str:
    """Bir makalenin analizinden HTML brifing bloğu üret.

    Modelden gelen HER alan html.escape()'ten geçer — model çıktısı hiçbir
    zaman HTML olarak yorumlanmaz (eski sanitizer'ın yerini alan garanti).
    Tek istisna, aşağıda kendi ürettiğimiz <span> etiketidir.
    """
    severite = analiz.get("severite", "DÜŞÜK")
    renk = SEVERITE_RENK.get(severite, SEVERITE_RENK["DÜŞÜK"])

    def alan(ad: str, varsayilan: str = "Belirtilmemiş") -> str:
        return html.escape(str(analiz.get(ad) or varsayilan))

    ozet = _vurgula_olay_tarihi(alan("ozet", "—"), analiz.get("olay_tarihi", ""))

    gorsel = ""
    if img_cid:
        gorsel = (
            f'<img src="cid:{html.escape(img_cid)}" '
            f'alt="{html.escape(article.get("title", ""))}" '
            'style="max-width:100%;height:auto;border-radius:4px;margin:8px 0;">'
        )

    return f"""<div style="margin-bottom:24px;padding:16px;border-left:4px solid {renk};background:#f9f9f9;font-family:Arial,sans-serif;">
  <h3 style="margin:0 0 8px 0;color:{renk};">[{html.escape(severite)}] {html.escape(article.get('title', 'Başlıksız'))}</h3>
  {gorsel}
  <p><strong>📅 Haber Tarihi:</strong> {html.escape(str(article.get('pubDate', 'Bilinmiyor')))}</p>
  <p><strong>💾 Eşleşen Ürün:</strong> {html.escape(str(article.get('matched_product', '—')))}</p>
  <p><strong>🔴 Etkilenen Sürümler:</strong> {alan('etkilenen_surumler')}</p>
  <p><strong>🟢 Yamalı Sürümler:</strong> {alan('yamali_surumler')}</p>
  <p><strong>🎯 Etkilenen:</strong> {alan('etkilenen_kapsam')}</p>
  <p><strong>📝 Özet:</strong> {ozet}</p>
  <p><strong>🛡️ Aksiyon:</strong> {alan('aksiyon', 'Güncellemeleri takip et.')}</p>
  <p><strong>💡 Öneri:</strong> {alan('oneri', '—')}</p>
  <p style="margin:16px 0 0;text-align:center;">
    <a href="{html.escape(article.get('link', '#'))}" style="display:inline-block;padding:10px 22px;background:#1a1a2e;color:#ffffff;text-decoration:none;border-radius:6px;font-weight:bold;font-size:13px;">Habere Git →</a>
  </p>
</div>"""


# Severite sıralaması — brifingde en kritik haber en üstte olmalı.
# Eskiden bu sıralamayı modele yaptırıyorduk ("hepsini YÜKSEK'ten DÜŞÜK'e
# sırala"); haber başına analizde model diğer haberleri görmediği için
# sıralama artık kodun işi (ve deterministik).
_SEVERITE_SIRASI = {"YÜKSEK": 0, "ORTA": 1, "DÜŞÜK": 2}


def build_overflow_html(overflow_articles: list[dict]) -> str:
    """Gemini kapsamı dışında kalan makaleler için basit HTML tablo oluştur.

    Bu makaleler analiz edilmez ama e-postanın sonunda başlık+link+ürün
    olarak listelenir → istihbarat kaybı önlenir.
    """
    if not overflow_articles:
        return ""
    rows = []
    for a in overflow_articles:
        # HTML escape — başlık veya link özel karakter içerebilir
        title_escaped = html.escape(a.get("title", "Başlıksız"))
        link = html.escape(a.get("link", "#"))
        product = html.escape(a.get("matched_product", "—"))
        rows.append(
            OVERFLOW_ROW.replace("{title}", title_escaped)
            .replace("{link}", link)
            .replace("{product}", product)
        )
    return (
        OVERFLOW_HEADER.replace("{count}", str(len(overflow_articles)))
        + "".join(rows)
        + OVERFLOW_FOOTER
    )


# Aynı görsel URL'i birden fazla makalede image_candidate olarak çıkabilir
# (ör. aynı vendor'a ait iki haber aynı logo/banner'ı kullanır) — full_url
# bazında önbellekler, _cve_cache/_cve_cache_lock deseninin birebir kopyası.
_image_cache: dict[str, bytes | None] = {}
_image_cache_lock = threading.Lock()


def process_image(url: str, article_link: str) -> bytes | None:
    """Görseli indir/optimize et — full_url bazında önbellekli.

    Check-then-set atomik değil (indirme lock DIŞINDA yapılır) — _cve_cache
    ile aynı, bilinçli olarak kabul edilmiş bir risk: iki thread'in AYNI
    URL'i eşzamanlı ilk kez görme ihtimali düşük, sonucu en kötü "aynı
    görsel 2 kez indirilir" (cache OLMADAN zaten olan durumun ta kendisi).
    Lock'u indirme boyunca tutmak ThreadPoolExecutor(max_workers=10)'un
    paralelliğini fiilen iptal eder — bu yüzden tercih edilmedi.
    """
    if not url:
        return None
    full_url = urllib.parse.urljoin(article_link, url)
    with _image_cache_lock:
        if full_url in _image_cache:
            return _image_cache[full_url]
    sonuc = _process_image_indir(url, full_url)
    with _image_cache_lock:
        _image_cache[full_url] = sonuc
    return sonuc


def _process_image_indir(url: str, full_url: str) -> bytes | None:
    """process_image'ın önbelleksiz indirme/işleme gövdesi.

    Güvenlik zinciri: SSRF (istek öncesi + redirect sonrası) → SVG reddi →
    boyut tavanı → magic byte doğrulaması → Pillow ile yeniden boyutlandırma.
    Herhangi bir adım başarısız olursa None döner; brifing etkilenmez.
    """
    try:
        if not full_url.startswith(("http://", "https://")):
            return None

        if _ssrf_kontrol(full_url, "image pre-request"):
            return None

        session = requests.Session()
        session.max_redirects = 3
        # stream=True: tamamını belleğe almadan boyut tavanını uygulayabilmek için
        resp = session.get(full_url, headers=_REQUEST_HEADERS, timeout=IMAGE_FETCH_TIMEOUT, stream=True)
        resp.raise_for_status()

        if _ssrf_kontrol(resp.url, "image post-redirect"):
            return None

        # SVG reddi — script taşıyabilir
        content_type = resp.headers.get("Content-Type", "").lower()
        if "image/svg+xml" in content_type:
            log.warning("SVG rejected: %s", full_url)
            return None

        chunks = []
        downloaded = 0
        for chunk in resp.iter_content(chunk_size=8192):
            if chunk:
                chunks.append(chunk)
                downloaded += len(chunk)
                if downloaded > MAX_DOWNLOAD_BYTES:
                    log.warning("Image exceeded MAX_DOWNLOAD_BYTES: %s", full_url)
                    return None
        data = b"".join(chunks)
        if not data:
            return None

        # Magic byte doğrulaması — Content-Type başlığına güvenilmez
        is_jpeg = data.startswith(b"\xff\xd8\xff")
        is_png = data.startswith(b"\x89PNG\r\n\x1a\n")
        is_gif = data.startswith(b"GIF8")
        is_webp = data.startswith(b"RIFF") and len(data) > 11 and data[8:12] == b"WEBP"

        if not (is_jpeg or is_png or is_gif or is_webp):
            log.warning("Magic byte mismatch or unsupported format: %s", full_url)
            return None

        with Image.open(BytesIO(data)) as img:
            # Sadece hedeften büyükse küçült (küçük görseller büyütülmez)
            if img.width > IMAGE_TARGET_WIDTH:
                ratio = IMAGE_TARGET_WIDTH / img.width
                new_height = int(img.height * ratio)
                img = img.resize((IMAGE_TARGET_WIDTH, new_height), Image.Resampling.LANCZOS)

            # Şeffaflık varsa beyaz zemine yerleştir, JPEG için RGB'ye çevir
            if img.mode in ('RGBA', 'LA') or (img.mode == 'P' and 'transparency' in img.info):
                bg = Image.new('RGB', img.size, (255, 255, 255))
                if img.mode != 'RGBA':
                    img = img.convert('RGBA')
                bg.paste(img, mask=img.split()[3])
                img = bg
            elif img.mode != 'RGB':
                img = img.convert('RGB')

            out = BytesIO()
            img.save(out, format="JPEG", quality=IMAGE_JPEG_QUALITY, optimize=True)
            return out.getvalue()
    except Exception as exc:
        log.warning("Image processing failed (%s): %s", url, exc)
        return None

# NOT: inject_images() ve [[IMG:n]] token mekanizması 2026-09-10'da silindi.
# O mekanizma, HTML'i MODEL ürettiği için gerekliydi: modele "görselin yerine
# bu token'ı yaz" dedirtip sonra token'ı kodla <img>'e çeviriyorduk. Artık
# HTML'i baştan sona kod ürettiği için (render_briefing_block) görselin nereye
# gireceğini zaten biliyoruz — araya token koyup geri ayıklamaya gerek yok.


# DRY_RUN=true: RSS/eşleştirme/Gemini/versiyon çıkarma tam olarak çalışır
# (secret'lar ve pipeline gerçekten test edilir), ama son adımda SMTP hiç
# çağrılmaz — mail atılmaz. Önizleme yerine logs/dry_run_preview.html'e
# yazılır (görsel <img cid:...> referansları düz HTML'de kırık görünür,
# bu bilinen ve kabul edilen bir sınırlama — asıl amaç metin/versiyon/
# tarih alanlarını mail göndermeden doğrulamak).
DRY_RUN = os.environ.get("DRY_RUN", "").strip().lower() == "true"


def _write_html_preview(dosya_adi: str, email_body: str, sebep: str) -> None:
    """E-posta gövdesini logs/ altına yaz — DRY_RUN önizlemesi VE gönderim
    tüm denemelerde başarısız olduğunda son çare kaydı aynı yolu kullanır."""
    preview_path = LOG_DIR / dosya_adi
    preview_path.write_text(email_body, encoding="utf-8")
    log.info("%s: %s", sebep, preview_path)


def _write_dry_run_preview(email_body: str) -> None:
    """DRY_RUN modunda e-posta gövdesini dosyaya yaz, SMTP'ye hiç dokunma."""
    _write_html_preview("dry_run_preview.html", email_body,
                         "DRY_RUN aktif — mail GÖNDERİLMEDİ. Önizleme")


# send_email için zaman aşımı/retry sabitleri — geçici bir SMTP sorunu
# günün baştan sona hesaplanmış brifingini kaybettirmesin diye.
SMTP_TIMEOUT_SEC = 30
SMTP_SEND_MAX_ATTEMPTS = 2
SMTP_RETRY_DELAY_SEC = 5


def send_email(subject: str, html_body: str,
               images: list[tuple[str, bytes]] | None = None) -> None:
    """E-postayı SMTP üzerinden gönder. STARTTLS + Gmail App Password kullanır.

    EMAIL_TO virgülle ayrılarak birden fazla alıcıya gönderim destekler.
    Tüm SMTP denemeleri başarısız olursa brifing içeriği logs/'a yazılıp
    (kaybolmasın diye) yine de RuntimeError fırlatılır — fail-loud korunur.
    """
    # SMTP ayarlarını ortamdan oku (varsayılanlar Gmail için)
    smtp_server = os.environ.get("SMTP_SERVER", "smtp.gmail.com")
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    username = os.environ.get("SMTP_USERNAME", "")
    password = os.environ.get("SMTP_PASSWORD", "")
    email_from = os.environ.get("EMAIL_FROM", username)
    email_to_raw = os.environ.get("EMAIL_TO", "")

    # Virgülle ayrılmış birden fazla alıcı desteklenir
    # "a@x.com, b@x.com" → ["a@x.com", "b@x.com"]
    recipients = [addr.strip() for addr in email_to_raw.split(",") if addr.strip()]

    if not all([username, password, recipients]):
        raise RuntimeError("SMTP credentials or EMAIL_TO not configured")

    # "@" içermeyen bir adres denemeden ÖNCE açık hata versin — aksi halde
    # smtplib alt katmanda daha belirsiz bir hatayla patlar.
    gecersiz = [a for a in recipients if "@" not in a]
    if gecersiz:
        raise RuntimeError(f"EMAIL_TO içinde geçersiz adres(ler): {gecersiz}")

    if images:
        msg = MIMEMultipart("related")
        msg_alt = MIMEMultipart("alternative")
        msg_alt.attach(MIMEText(html_body, "html", "utf-8"))
        msg.attach(msg_alt)
        for cid, data in images:
            img_part = MIMEImage(data, _subtype="jpeg")
            img_part.add_header("Content-ID", f"<{cid}>")
            img_part.add_header("Content-Disposition", "inline")
            msg.attach(img_part)
    else:
        # Görsel yoksa mevcut alternative yapısı korunur
        msg = MIMEMultipart("alternative")
        msg.attach(MIMEText(html_body, "html", "utf-8"))

    # Header'lar her iki dalda da aynı — tek yerden set edilir
    msg["Subject"] = subject
    msg["From"] = email_from
    msg["To"] = ", ".join(recipients)

    # Güvenli SSL bağlamı (sertifika doğrulama açık)
    context = ssl.create_default_context()
    son_hata: Exception | None = None
    for deneme in range(1, SMTP_SEND_MAX_ATTEMPTS + 1):
        try:
            with smtplib.SMTP(smtp_server, smtp_port, timeout=SMTP_TIMEOUT_SEC) as server:
                server.ehlo()
                server.starttls(context=context)  # Şifreli kanala geç (587 → TLS)
                server.ehlo()
                server.login(username, password)
                # sendmail() liste bekler — tek string verirsen Gmail reddeder
                server.sendmail(email_from, recipients, msg.as_string())
            log.info("Email sent to %s", ", ".join(recipients))
            return
        except Exception as exc:
            son_hata = exc
            log.warning("SMTP gönderim denemesi %d/%d başarısız: %s",
                        deneme, SMTP_SEND_MAX_ATTEMPTS, exc)
            if deneme < SMTP_SEND_MAX_ATTEMPTS:
                time.sleep(SMTP_RETRY_DELAY_SEC)

    # Tüm denemeler başarısız — brifing kaybolmasın diye diske yaz, sonra
    # yine de FIRLAT (fail-loud: Actions "failed" işaretlensin, log artifact
    # yüklensin, 15:00 yedek slotu tekrar dener).
    _write_html_preview(
        "failed_send_preview.html", html_body,
        "SMTP gönderimi tüm denemelerde başarısız — brifing içeriği diske yazıldı",
    )
    raise RuntimeError(
        f"SMTP gönderimi {SMTP_SEND_MAX_ATTEMPTS} denemede de başarısız"
    ) from son_hata


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  MAIN
#  Akışın orkestratörü: fetch → filter → match → analyze → email
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def main() -> None:
    log.info("=" * 60)
    log.info("CTI News Feed Automation — started")
    today = turkish_date()  # "17 Mayıs 2026, Cumartesi"

    # 1. Tüm RSS feed'lerini paralel çek (10 worker)
    log.info("Fetching %d RSS feeds...", len(FEEDS))
    all_articles, failed_feeds = fetch_all_feeds()
    log.info("Total articles fetched: %d (%d/%d feed hata verdi)",
              len(all_articles), failed_feeds, len(FEEDS))
    if _feed_failure_alert_gerekli(failed_feeds, len(FEEDS)):
        raise RuntimeError(
            f"{failed_feeds}/{len(FEEDS)} feed hata verdi — muhtemel DNS/ağ "
            "kesintisi ya da regresyon. 'Tehdit yok' maili sessizce atmak "
            "yerine burada durur (bkz. FEED_FAILURE_ALERT_RATIO)."
        )

    # 2. Sadece son 24 saatte yayınlanan makaleleri tut
    recent = filter_recent(all_articles)
    log.info("Articles from last 24h: %d", len(recent))

    # 3. Envantere göre eşleştir (HIGH_SIGNAL + exact/alias match + öncelik puanı)
    matched = match_articles(recent)
    log.info("Articles matching inventory: %d", len(matched))

    # 4. Eşleşme varsa Gemini'ye gönder ve e-posta at
    if matched:
        # Öncelik puanına göre sıralı — ilk MAX_GEMINI_ARTICLES makale Gemini ile analiz edilir
        top_matches = matched[:MAX_GEMINI_ARTICLES]
        overflow_matches = matched[MAX_GEMINI_ARTICLES:]  # Kalanı listede gösterilir

        log.info("Analyzing %d articles individually (%d model workers)...",
                 len(top_matches), len(ANALYSIS_MODELS))
        if overflow_matches:
            log.info("Overflow: %d additional articles will be listed without AI analysis.", len(overflow_matches))

        # RSS zaten zenginse (bazı kaynaklar tam makaleyi content_encoded'da
        # veriyor — bkz. rss_metni yorumu) sayfayı hiç çekmiyoruz: hem daha
        # temiz metin (sayfa şablonu içermiyor) hem ~8 paralel HTTP isteği
        # tasarrufu. Ödün: bu makaleler için og:image adayı kaybolur ama
        # image_candidate (RSS kaynaklı) zaten öncelikli görsel kaynağı,
        # aşağıdaki görsel borusu buna göre tasarlanmıştı.
        sayfa_gereken = [a for a in top_matches if len(a.get("rss_metni", "")) < ZENGIN_RSS_ESIGI]
        zengin_rss_sayisi = len(top_matches) - len(sayfa_gereken)
        if zengin_rss_sayisi:
            log.info("  %d makale zaten zengin RSS içeriğine sahip, sayfa çekilmeyecek",
                     zengin_rss_sayisi)

        # Makale tam metinlerini paralel çek — hem analiz hem görsel bunu kullanır
        bodies, og_images = fetch_article_bodies(sayfa_gereken)

        # Haber metni sürüm bilgisi için çoğu zaman yetersiz (bkz. fetch_cve_context
        # yorumu) — CVE'lerin resmi kayıtlarından yetkili sürüm verisini topla
        cve_baglamlari = fetch_cve_context(top_matches, bodies)

        # Görsel adayları: RSS'ten gelen veya makale sayfasının og:image'i
        image_tasks = []
        for i, a in enumerate(top_matches):
            url = a.get("image_candidate") or og_images.get(a.get("link", ""), "")
            if url:
                image_tasks.append((i, url, a.get("link", ""), a.get("title", "")))

        # Analiz (fan-out) ile görsel indirmeyi eşzamanlı yürüt — birbirine bağlı değiller
        images_by_index: dict[int, bytes | None] = {}
        with ThreadPoolExecutor(max_workers=1) as analiz_pool:
            analiz_future = analiz_pool.submit(analyze_articles, top_matches, bodies, cve_baglamlari)

            if image_tasks:
                log.info("Processing %d candidate images...", len(image_tasks))
                with ThreadPoolExecutor(max_workers=10) as pool:
                    future_to_task = {
                        pool.submit(process_image, task[1], task[2]): task
                        for task in image_tasks
                    }
                    for future in as_completed(future_to_task):
                        task = future_to_task[future]
                        try:
                            images_by_index[task[0]] = future.result()
                        except Exception as e:
                            log.warning("Image worker failed for %s: %s", task[1], e)
                            images_by_index[task[0]] = None

            analizler = analiz_future.result()

        log.info("Analysis complete: %d/%d articles analyzed", len(analizler), len(top_matches))

        # Analiz edilemeyen makaleler kaybolmaz — taşma tablosuna düşerler
        basarisiz = [a for i, a in enumerate(top_matches) if i not in analizler]
        if basarisiz:
            overflow_matches = basarisiz + overflow_matches

        # Ürünle ilgisiz bulunan makaleler ATILMAZ — taşma tablosuna düşürülür
        # ("hiçbir istihbarat kaybolmaz" ilkesiyle tutarlı)
        analizler, overflow_matches = filter_irrelevant_analyses(
            analizler, top_matches, overflow_matches)

        # En kritik haber en üstte: severite, eşitlikte öncelik puanı
        sirali = sorted(
            analizler.items(),
            key=lambda kv: (
                _SEVERITE_SIRASI.get(kv[1].get("severite"), 3),
                -top_matches[kv[0]].get("priority_score", 0),
            ),
        )

        # Görsel bütçesi — SADECE brifingde gerçekten yer alan makaleler için
        cid_map: dict[int, str] = {}
        total_image_bytes = 0
        final_images = []
        budget_exceeded = False
        for index, _ in sirali:
            img_bytes = images_by_index.get(index)
            if not img_bytes:
                continue
            if total_image_bytes + len(img_bytes) > MAX_TOTAL_IMAGE_BYTES:
                log.warning("Total image bytes limit exceeded. Skipping remaining images.")
                budget_exceeded = True
                break
            total_image_bytes += len(img_bytes)
            cid = f"img{index}"
            cid_map[index] = cid
            final_images.append((cid, img_bytes))

        log.info(
            "Image budget: %d attached, %.1f KB / %.1f KB used%s",
            len(final_images), total_image_bytes / 1024, MAX_TOTAL_IMAGE_BYTES / 1024,
            " (budget exceeded — remaining skipped)" if budget_exceeded else "",
        )

        # Görselsiz kalan makalelere severite bazlı placeholder ata — üç
        # senaryo (aday yok / indirme başarısız / bütçe aşıldı) burada birleşir.
        # BİLİNÇLİ OLARAK bütçe döngüsünden SONRA: önce buraya eklenseydi
        # placeholder'ın kendi boyutu gerçek makale görsellerinin
        # MAX_TOTAL_IMAGE_BYTES bütçesini çalardı VE bütçe-aşımı senaryosunu
        # kaçırırdı.
        cid_map, placeholder_images = assign_placeholder_images(sirali, cid_map)
        final_images.extend(placeholder_images)

        # Brifing HTML'ini KOD üretir (model sadece veri döndürdü)
        briefing_html = "".join(
            render_briefing_block(top_matches[index], analiz, cid_map.get(index))
            for index, analiz in sirali
        )

        # Taşma bölümünü ekle (analiz edilemeyenler + limit üstü makaleler)
        full_content = briefing_html + build_overflow_html(overflow_matches)

        # E-posta gövdesini oluştur ve gönder
        email_body = EMAIL_TEMPLATE.replace("{date}", today).replace("{content}", full_content)
        if DRY_RUN:
            _write_dry_run_preview(email_body)
        else:
            send_email(
                subject=f"🛡️ CTI Tehdit Brifing — {today}",
                html_body=email_body,
                images=final_images
            )
            log.info("Threat briefing sent successfully.")
    else:
        # Eşleşme yoksa "tehdit yok" bildirimi gönder
        email_body = EMAIL_TEMPLATE.replace("{date}", today).replace("{content}", NO_THREATS_CONTENT)
        if DRY_RUN:
            _write_dry_run_preview(email_body)
        else:
            send_email(
                subject=f"✅ CTI Tarama — Tehdit Yok — {today}",
                html_body=email_body,
            )
            log.info("No threats — notification sent.")

    log.info("CTI News Feed Automation — finished")


# Script direkt çalıştırıldığında main() tetiklenir
# Yakalanmayan hatalar log'a yazılır ve exit code 1 ile systemd'ye yansır
# (servis "failed" olarak işaretlenir; auto-restart kapalı olduğu için tekrar başlatılmaz)
if __name__ == "__main__":
    try:
        main()
    except Exception:
        log.exception("CTI Automation — unhandled exception")
        raise
