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
import locale
import os
import re
import json                          # Envanter/alias listelerini env'den yükleme
import html                          # HTML escape (XSS koruması için)
import logging
import logging.handlers              # RotatingFileHandler (log boyut sınırı)
import smtplib                       # SMTP ile e-posta gönderme
import ssl                           # STARTTLS bağlantısı
import time                          # exponential backoff retry
import urllib.parse                  # URL resolve için
from io import BytesIO               # Bellekte görsel işlemek için
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.image import MIMEImage
from html.parser import HTMLParser   # Gemini HTML çıktısını sanitize
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
MAX_GEMINI_ARTICLES = 50       # Gemini'ye gönderilecek maks makale sayısı
MAX_BODY_CHARS      = 10_000   # Makale sayfasından çekilecek maks metin (versiyon çıkarma)
GEMINI_BODY_CHARS   = 3_000    # Gemini prompt'una gönderilecek makale bağlamı
MAX_PROMPT_TOKENS   = 100_000  # Toplam prompt token üst sınırı (TPM güvenlik payı)
MAX_DOWNLOAD_BYTES     = 2_000_000   # İndirme tavanı (optimizasyon öncesi)
IMAGE_TARGET_WIDTH     = 1280        # 640px görüntüleme × 2 (retina)
IMAGE_JPEG_QUALITY     = 85
MAX_TOTAL_IMAGE_BYTES  = 5_000_000   # Tüm görsellerin toplam tavanı
IMAGE_FETCH_TIMEOUT    = 8
# Gemini isteğinin azami süresi (ms). google-genai'nin kendisi hiç timeout
# koymuyor — yüksek yoğunlukta istek yanıtsız asılı kalabilir (20 dk+),
# bu durumda script-içi retry/model-fallback mantığına HİÇ sıra gelmez
# (istisna fırlamadığı için yakalanamaz). Bu sınır olmadan tek bir asılı
# istek, GitHub Actions'ın job timeout'una çarpıp brifingi iptal ettirebilir
# (2026-08-22'de yaşandı).
#
# 2026-08-27: 90 sn'lik eski değer ÇOK DÜŞÜKTÜ ve brifingi tamamen düşürüyordu.
# Ölçüm (43 makale, ~120 KB prompt, ~57 KB HTML çıktı):
#     gemini-3.5-flash → 280,6 sn        gemini-2.5-flash → 133,3 sn
# Yani model doğru çalışırken bile her istek 90 sn'de kesiliyor, üç model de
# 504 veriyor ve "tüm modeller başarısız" hatasıyla mail hiç gitmiyordu.
# 360 sn: ölçülen en yavaş yanıtın (~281 sn) belirgin üstünde, sonsuzdan uzak.
GEMINI_REQUEST_TIMEOUT_MS = 360_000

# Tüm model zincirinin (retry'lar dahil) toplam süre bütçesi (saniye).
# Tek istek sınırını yükseltmek tek başına yetmez: 4 model × 3 deneme ×
# 360 sn = 72 dakika eder ve job timeout'unu yine patlatır. Bu bütçe,
# zincirin ne kadar uzarsa uzasın toplamda sınırlı kalmasını garanti eder.
#
# 2026-08-27: 900 sn (15 dk) yetersiz çıktı — analyze_with_gemini()'deki
# _HANG_THRESHOLD_SECONDS yorumuna bak: bir model art arda 2 kez ~300 sn
# asılı kalıp koptu, bütçenin çoğu (2×300 sn + backoff) TEK modelde tükendi
# ve zincir sağlıklı olan yedek modele hiç ulaşamadı (workflow run
# 33064763459, mail gitmedi). O sorun asıl olarak retry mantığındaki
# tasarım hatasıydı (aynı asılı modeli tekrar tekrar denemek) ve ayrıca
# düzeltildi. Bütçe yine de 1200 sn'ye (20 dk) çıkarıldı: 30 dk'lık job
# timeout'u içinde rahatça sığıyor (feed çekme + görsel işleme ~2-3 dk,
# mail gönderme saniyeler sürüyor) ve artık israf edilmeyen bu süre,
# gerçekten birden fazla modelin aynı anda dalgalandığı nadir durumlarda
# zincirin daha derinlerine inebilmeyi sağlıyor.
GEMINI_TOTAL_BUDGET_SEC = 1200
# Token matematiği (50 makale × ~900 token/makale ≈ 45K token):
#   Günlük bütçe: 250K → %18 kullanım. TPM: tek istek/gün, aşım riski yok.
#   Makale sayısı artarsa body_chars dinamik olarak kısılır (build_prompt içinde).

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

# CVE numarası (dedup ve puanlama için)
_CVE_RE = re.compile(r"CVE-\d{4}-\d{4,7}", re.IGNORECASE)

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
#  GEMINI SYSTEM PROMPT
#  Gemini'ye verilen rol tanımı ve çıktı şablonu.
#  Türkçe HTML brifing üretir: tarih, kaynak, etkilenen/yamalı sürümler,
#  özet, aksiyon, öneri. Severite (YÜKSEK/ORTA/DÜŞÜK) renkli işaretlenir.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

SYSTEM_PROMPT = """Sen kıdemli bir Siber Tehdit İstihbaratı (CTI) Analistsin ve bir güvenlik operasyonları ekibine doğrudan danışmanlık yapıyorsun.

Sana numaralandırılmış bir güvenlik haberleri listesi verilecek. Her haber, ortamımızdaki bir ürünle eşleştirilmiş olacak.

HER HABER İÇİN aşağıdaki HTML formatında bir brifing bloğu yaz:

<div style="margin-bottom:24px;padding:16px;border-left:4px solid [SEVERİTE_RENK];background:#f9f9f9;font-family:Arial,sans-serif;">
  <h3 style="margin:0 0 8px 0;color:[SEVERİTE_RENK];">[SEVERİTE: YÜKSEK/ORTA/DÜŞÜK] Haber Başlığı</h3>
  [[IMG:n]]
  <p><strong>📅 Haber Tarihi:</strong> Yayın tarihi</p>
  <p><strong>💾 Eşleşen Ürün:</strong> matched_product değeri</p>
  <p><strong>🔴 Etkilenen Sürümler:</strong> Zafiyetten etkilenen (savunmasız) versiyon numaraları/aralıkları</p>
  <p><strong>🟢 Yamalı Sürümler:</strong> Yamayı içeren güvenli versiyon numaraları (yükseltme hedefi)</p>
  <p><strong>🎯 Etkilenen:</strong> Etkilenen yazılım, donanım veya gruplar</p>
  <p><strong>📝 Özet:</strong> Temel tehdit veya sorunu 25 kelimede özetle</p>
  <p><strong>🛡️ Aksiyon:</strong> Doğrudan talimat</p>
  <p><strong>💡 Öneri:</strong> Bir stratejik tavsiye</p>
  <p style="margin:16px 0 0;text-align:center;">
    <a href="LINK" style="display:inline-block;padding:10px 22px;background:#1a1a2e;color:#ffffff;text-decoration:none;border-radius:6px;font-weight:bold;font-size:13px;">Habere Git →</a>
  </p>
</div>

SEVERİTE_RENK: YÜKSEK=#dc3545, ORTA=#fd7e14, DÜŞÜK=#28a745

KURALLAR:
- Yanıtın tamamı TÜRKÇE olmalı. Teknik terimler, CVE numaraları, ürün isimleri ve komutlar İNGİLİZCE kalmalı.
- Giriş veya sonuç cümlesi YAZMA. Doğrudan ilk brifing bloğuyla başla.
- "Özet" 25 kelimeyi geçmemeli.
- "Özet" içinde zafiyetin istismar edildiği, keşfedildiği veya olayın gerçekleştiği SPESİFİK bir tarih geçiyorsa (bu, haberin yayın tarihi DEĞİL — o "Haber Tarihi" alanında zaten var; burası olayın/istismarın kendi tarihi), bu tarihi <span style="color:#0d6efd;">...</span> ile SADECE RENKLİ yaz. KALIN YAPMA — <strong> veya <b> KULLANMA. Örnek: "Zafiyet <span style=\"color:#0d6efd;\">15 Ağustos 2026</span>'dan beri aktif istismar ediliyor." Böyle bir tarih geçmiyorsa bu kuralı uygulama, tarih uydurma.
- "Aksiyon" imperatif ve doğrudan olmalı. Spesifik bir aksiyon yoksa: "Güncellemeleri takip et."
- Her brifing bloğunda <h3> başlığının hemen altına tam olarak [[IMG:n]] yaz (n, o makalenin sana verilen numarasıdır, örneğin [[IMG:1]]). Görseli olsa da olmasa da bu token'ı mutlaka ekle.
- Eğer iki haber aynı CVE veya olayı işliyorsa, ikincisi için yalnızca şunu yaz:
  <div style="margin-bottom:24px;padding:12px;border-left:4px solid #6c757d;background:#f9f9f9;font-family:Arial,sans-serif;">
    <p><strong>Aynı konu hakkında ek haber:</strong> İlk haberin başlığı</p>
    <p style="margin:8px 0 0;text-align:center;">
      <a href="LINK" style="display:inline-block;padding:6px 16px;border:1.5px solid #6c757d;color:#6c757d;text-decoration:none;border-radius:6px;font-weight:bold;font-size:12px;">Habere Git →</a>
    </p>
  </div>
- Her haberde "Full Article Content" ve "Detected Versions" alanları verilmiştir. Versiyon bilgisini doldururken bu verileri DİKKATLİCE analiz et:
  * "Detected Versions" listesindeki işaretler ANLAM taşır, bunları doğru yorumla:
      "11.2.0 – 11.2.4-h16" = bu aralıktaki sürümler ETKİLENİYOR
      "< 9.0.98"            = bu sürümden ÖNCEKİ her şey ETKİLENİYOR
      "<= 20.1.0"           = bu sürüm DAHİL ve öncesi ETKİLENİYOR
      ">= 7.4.7"            = bu sürüm ve sonrası GÜVENLİ (yamalı)
      "fixed in 1.38.3"     = yamayı içeren sürüm, YÜKSELTME HEDEFİ
      "11.2.x"              = o dalın tüm alt sürümleri
      "KB5031354"           = Microsoft yama kimliği (sürüm yerine bunu yaz)
  * "Etkilenen Sürümler" alanına YALNIZCA zafiyetten etkilenen (savunmasız) versiyonları yaz. Ürün adıyla birlikte yaz (örn. "PAN-OS 11.2.0 – 11.2.4-h16", "FortiOS < 7.4.7").
  * "Yamalı Sürümler" alanına yamayı/düzeltmeyi içeren güvenli sürümleri yaz. Yükseltme hedefi olarak göster (örn. "PAN-OS >= 11.2.4-h17", "FortiOS 7.4.7 veya üzeri", "Windows: KB5031354").
  * Birden fazla ürün dalı (branch) etkileniyorsa her dalı ayrı ayrı listele.
  * "Affected/Unaffected" veya "before/prior to" gibi bağlamsal ipuçlarına dikkat et.
  * Haberde hiçbir versiyon bilgisi gerçekten yoksa her iki alan için de "Belirtilmemiş — kaynağı kontrol edin" yaz.
- SEVERİTE belirleme rehberi: YÜKSEK = aktif exploitation / kritik RCE / veri ihlali; ORTA = yaması mevcut kritik açık / aktif campaign; DÜŞÜK = potansiyel risk / öneri niteliğinde. Tüm haberleri yüksek SEVERİTE'den düşük SEVERİTE'ye doğru sırala. """


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  HELPERS
#  Metin temizleme, versiyon çıkarma, HTML işleme yardımcıları
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Pre-compiled regex'ler (her kullanımda yeniden compile etmemek için)
_HTML_TAG = re.compile(r"<[^>]*>")
_WHITESPACE = re.compile(r"\s+")


def strip_html(raw: str) -> str:
    """HTML tag'lerini çıkar, entity'leri çöz, boşlukları tek boşluğa indirge.

    Entity çözümü versiyon çıkarma için KRİTİK: CISA/vendor advisory'leri
    sürüm eşiklerini "&lt;=20.1.0" veya "&lt;4.3.4.1" olarak yayınlıyor.
    Entity çözülmezse regex'in gördüğü metin "&lt;=20.1.0" olur, "<=" ile
    başlayan hiçbir desen eşleşmez ve "etkilenen sürüm" bilgisi kaybolur.

    Sıra önemli: ÖNCE tag'ler silinir, SONRA entity çözülür. Ters sırada
    "&lt;script&gt;" gerçek bir <script> tag'ine dönüşür ve temizlenmeden
    metne karışırdı.
    """
    return _WHITESPACE.sub(" ", html.unescape(_HTML_TAG.sub(" ", raw or ""))).strip()


def norm(s: str) -> str:
    """Metni normalize et: küçük harf + boşlukları tekleştir (eşleştirme için)."""
    return _WHITESPACE.sub(" ", (s or "").lower()).strip()


# ── Versiyon çıkarma ────────────────────────────────────────────────
# Desenler canlı advisory metinleri (Fortinet/Cisco/Palo Alto PSIRT, CISA,
# Ubuntu USN, Dell, cvefeed) taranarak çıkarıldı — tahminle değil.
#
# Build/patch soneki: "-h5", "-h16-rc1" gibi. Tire sonrası HARF şartı var, bu yüzden
# "7.0-7.6" gibi aralık ayırıcısı tire ile KARIŞMAZ (7 bir harf değil, sayı).
_BUILD_SUFFIX = r"(?:-[a-zA-Z]+\d*)*"

# İki bileşenli sürüm (7.4). YALNIZCA bir anahtar kelime bağlam verdiğinde
# kullanılır ("versions 7.4 and earlier") — tek başına aranırsa CVSS puanları
# (8.8, 9.1) ve tablo hücreleri sürüm sanılır.
_VER_LOOSE = rf"\d+\.\d+(?:\.(?:\d+|[xX*]))*{_BUILD_SUFFIX}"
# Üç+ bileşenli sürüm (7.4.2, 7.00.00.182, 7.4.x). Bağlamsız da güvenli.
_VER_STRICT = rf"\d+\.\d+(?:\.(?:\d+|[xX*]))+{_BUILD_SUFFIX}"

# NOT: Alternatif sırası ÖNEMLİ. Regex aynı konumda soldaki dalı seçer;
# bu yüzden anlamı zenginleştiren dallar ("... and earlier", "fixed in ...")
# sade "version X" dalından ÖNCE gelmeli. Aksi halde "versions 20.2 and prior"
# ifadesinde sade dal "20.2"yi yutar ve "≤" anlamı kaybolur (eski davranış).
_VERSION_RE = re.compile(
    rf"""
    # ①a "between 7.0.0 and 7.4.2" / "from 2.4.17 through 2.4.67"
    #     "and" ayıracı YALNIZCA between/from öneki varken geçerli — aksi
    #     halde "affects 1.2.3 and 4.5.6" gibi bir LİSTE aralık sanılırdı.
    (?:between|from)\s+({_VER_LOOSE})\s*(?:and|through|thru|to|–|—|-)\s*({_VER_LOOSE})
    |
    # ①b "versions 1.3.0 - 1.3.6", "7.0.0 through 7.4.2", "12.1.2 through 12.1.4-h*"
    #     "versions?" öneki bu dala dahil — aksi halde sade dal (⑦) önce
    #     eşleşip aralığın sol ucunu yutuyor, sağ ucu kayboluyordu.
    (?:versions?\s+)?({_VER_LOOSE})\s*(?:through|thru|to|–|—|-)\s*({_VER_LOOSE})
    |
    # ② "X and earlier / and below / and prior / or older" → üst sınır
    #    Dell, cvefeed ve CISA advisory'lerinde EN SIK görülen kalıp.
    (?:versions?\s+)?({_VER_LOOSE})\s*(?:and|or)\s+(?:earlier|below|prior|older|lower)
    |
    # ③ "X and later / or above / and newer" → yamalı sürüm eşiği
    (?:versions?\s+)?({_VER_LOOSE})\s*(?:and|or)\s+(?:later|above|newer|higher)
    |
    # ④ "fixed in 7.4.7", "upgrade to 1.38.3", "resolved in 12.1.4-h5"
    (?:fixed\s+in|resolved\s+in|patched\s+in|addressed\s+in|remediated\s+in
      |upgrad(?:e|ing)\s+to|updat(?:e|ing)\s+to)
    \s+(?:version\s+)?({_VER_LOOSE})
    |
    # ⑤ "before 9.0.98", "prior to version 10.2.1", "earlier than 7.6.3",
    #    "up to 3.1.0", "< 3.1.0", "<= 12.1.4-h5"  → üst sınır
    (?:before|prior\s+to|earlier\s+than|older\s+than|up\s+to(?:\s+and\s+including)?|<=?)
    \s*(?:versions?\s+)?({_VER_LOOSE})
    |
    # ⑥ ">= 12.1.4-h5", "> 7.4.6" — yamalı/güvenli sürüm eşiği
    >=?\s*(?:versions?\s+)?({_VER_LOOSE})
    |
    # ⑦ "version 7.4.2", "ver 3.1.0", "v2.0.1"
    #    (?<!cvss\s): "CVSS Version 3.1" tablo başlığını sürüm sanmasın.
    (?<!cvss\s)(?:versions?\s*:?\s*|ver\.?\s*|[Vv])({_VER_STRICT})
    |
    # ⑧ Ürün adından/etiketten sonra bağımsız sürüm: "FortiOS 7.4.2",
    #    "PAN-OS 11.2.x", "Affected: 11.1.4-h33" (iki nokta da tetikler)
    (?<=[A-Za-z:]\s)({_VER_STRICT})
    |
    # ⑨ Microsoft KB numarası — Microsoft ürünlerinde yamanın kimliği
    #    sürüm numarası değil KB numarasıdır.
    (KB\d{{6,8}})
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Yanlış pozitif versiyonları filtrele (tarihler, CVE numaraları, özel IP'ler)
_FALSE_VERSION_RE = re.compile(
    r"""^(?:
        20[0-4]\d\.\d          # tarih benzeri: 2026.8
      | 19\d\d\.               # 1999.x
      | CVE- | CWE- | CVSS
      | 0\.0\.0$               # anlamsız
      | 10\.0\.0\.\d           # özel IP bloğu 10.0.0.x
      | 192\.168\.             # özel IP bloğu
      | 172\.(?:1[6-9]|2\d|3[01])\.   # özel IP bloğu
      | 127\.0\.0\.1$
    )""",
    re.IGNORECASE | re.VERBOSE,
)

MAX_VERSIONS = 40  # Tek makaleden çıkarılacak azami sürüm (prompt şişmesin)


def extract_versions(text: str) -> list[str]:
    """Metinden versiyon numaralarını/aralıklarını çıkar.

    Gemini'ye "Detected Versions" alanı olarak ayrı bir liste verilir,
    böylece model versiyonları kaçırmaz. Tam article body (MAX_BODY_CHARS)
    üzerinden çalışır.

    Çıktı sürümün ANLAMINI da taşır — Gemini "Etkilenen" ile "Yamalı"
    sürümleri ayırabilsin diye:
      "7.0.0 – 7.4.2"    aralık
      "<= 20.1.0"        bu sürüm ve öncesi etkilenir
      ">= 7.4.7"         bu sürüm ve sonrası güvenli
      "< 9.0.98"         bu sürümden önceki her şey etkilenir
      "fixed in 1.38.3"  yamayı içeren sürüm
    """
    found: list[str] = []
    for m in _VERSION_RE.finditer(text):
        (btw_lo, btw_hi, rng_lo, rng_hi, le, ge,
         fixed, lt, gt, kw, standalone, kb) = m.groups()

        lo, hi = (btw_lo or rng_lo), (btw_hi or rng_hi)
        if lo and hi:
            token = f"{lo} – {hi}"
        elif le:
            token = f"<= {le}"
        elif ge:
            token = f">= {ge}"
        elif fixed:
            token = f"fixed in {fixed}"
        elif lt:
            token = f"< {lt}"
        elif gt:
            token = f">= {gt}"
        else:
            token = kw or standalone or kb or ""

        token = token.strip(" .,;)")
        if not token or len(token) < 3:
            continue
        # Operatör önekini atlayıp asıl sürüm numarasını doğrula
        bare = token.split()[-1]
        if _FALSE_VERSION_RE.match(bare) or _FALSE_VERSION_RE.match(token):
            continue
        if token not in found:
            found.append(token)
        if len(found) >= MAX_VERSIONS:
            break
    return found


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  HTML SANITIZATION (Gemini output → email injection protection)
#  Gemini'nin ürettiği HTML doğrudan e-postaya enjekte edildiğinden, XSS
#  ve enjeksiyon riskine karşı whitelist tabanlı temizleyici şart.
#  Sadece izin verilen tag/attribute'lar kalır, gerisi atılır.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# E-postada görünmesine izin verilen HTML tag'leri
_ALLOWED_TAGS = frozenset([
    "div", "p", "h1", "h2", "h3", "h4", "strong", "em", "a", "br",
    "span", "ul", "ol", "li", "table", "tr", "td", "th", "thead", "tbody",
])

# İzin verilen attribute'lar (style: inline CSS, href: linkler için)
_ALLOWED_ATTRS = frozenset(["style", "href", "class"])

# Attribute değerinde tespit edilirse o attribute atılır (XSS vektörleri)
_DANGEROUS_ATTR_VALUE = re.compile(
    r"javascript\s*:|data\s*:|vbscript\s*:|expression\s*\(|url\s*\(",
    re.IGNORECASE,
)


class _HTMLSanitizer(HTMLParser):
    """Whitelist-based HTML sanitizer to prevent XSS via Gemini output."""

    def __init__(self):
        super().__init__()
        self.result: list[str] = []
        self._strip_depth = 0  # depth inside a stripped tag

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]):
        tag_lower = tag.lower()
        if tag_lower not in _ALLOWED_TAGS:
            self._strip_depth += 1
            return
        safe_attrs: list[str] = []
        for attr_name, attr_value in attrs:
            attr_name_lower = attr_name.lower()
            if attr_name_lower not in _ALLOWED_ATTRS:
                continue
            if attr_value and _DANGEROUS_ATTR_VALUE.search(attr_value):
                continue
            # Validate href specifically
            if attr_name_lower == "href" and attr_value:
                if not attr_value.startswith(("http://", "https://", "mailto:")):
                    continue
            escaped_value = html.escape(attr_value or "", quote=True)
            safe_attrs.append(f'{attr_name_lower}="{escaped_value}"')
        attrs_str = (" " + " ".join(safe_attrs)) if safe_attrs else ""
        self.result.append(f"<{tag_lower}{attrs_str}>")

    def handle_endtag(self, tag: str):
        tag_lower = tag.lower()
        if tag_lower not in _ALLOWED_TAGS:
            if self._strip_depth > 0:
                self._strip_depth -= 1
            return
        self.result.append(f"</{tag_lower}>")

    def handle_data(self, data: str):
        if self._strip_depth > 0:
            return  # skip content inside dangerous tags (e.g. <script>)
        self.result.append(html.escape(data))

    def handle_entityref(self, name: str):
        if self._strip_depth == 0:
            self.result.append(f"&{name};")

    def handle_charref(self, name: str):
        if self._strip_depth == 0:
            self.result.append(f"&#{name};")


def sanitize_gemini_html(raw_html: str) -> str:
    """Strip dangerous tags/attributes from Gemini output before email injection."""
    if not raw_html:
        return ""
    sanitizer = _HTMLSanitizer()
    try:
        sanitizer.feed(raw_html)
    except Exception:
        # If parsing fails entirely, escape everything as plain text
        return html.escape(raw_html)
    return "".join(sanitizer.result)


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


_OG_IMAGE_RE = re.compile(
    r'<meta[^>]+(?:property|name)=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']|<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\']og:image["\']',
    re.IGNORECASE
)

def fetch_article_page(url: str, timeout: int = 12) -> tuple[str, str]:
    """Makale URL'sine gidip sayfa içeriğini ve og:image URL'sini döndürür.

    Tam sayfa içeriği (MAX_BODY_CHARS) versiyon çıkarma için kullanılır.
    og:image e-posta içi görsel optimizasyonunda aday olarak kullanılır.
    """
    if not url or not url.startswith("http"):
        return "", ""
    # SSRF koruması
    if _SSRF_BLOCKED.search(url):
        log.warning("SSRF blocked: %s", url)
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

        # og:image çıkar (sayfa başındaki meta tag'lerde aranır, ilk 8KB yeterli)
        og_image = ""
        m = _OG_IMAGE_RE.search(resp.text[:8192])
        if m:
            og_image = m.group(1) or m.group(2) or ""

        raw = strip_html(resp.text)
        return _WHITESPACE.sub(" ", raw).strip()[:MAX_BODY_CHARS], og_image
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

def fetch_feed(name: str, url: str) -> list[dict]:
    """Tek bir RSS feed'i çek ve makale listesi olarak döndür."""
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
        return articles
    except Exception as e:
        # Bir feed çökse de diğerleri devam eder
        log.warning("Feed %s failed: %s", name, e)
        return []


def fetch_all_feeds() -> list[dict]:
    """Tüm FEEDS listesini 10 paralel worker ile çek, hepsini birleştir."""
    all_articles = []
    with ThreadPoolExecutor(max_workers=10) as pool:
        # Her feed için bir future oluştur
        futures = {pool.submit(fetch_feed, name, url): name for name, url in FEEDS}
        # Tamamlananları sırayla işle (sırasız geliyor, as_completed ile)
        for future in as_completed(futures):
            name = futures[future]
            try:
                articles = future.result()
                log.info("  %s: %d articles", name, len(articles))
                all_articles.extend(articles)
            except Exception as e:
                log.warning("  %s: error — %s", name, e)
    return all_articles


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


def _is_filename_extension_usage(s: str, match_start: int) -> bool:
    """Eşleşme hemen bir '.' işaretinden sonra mı geliyor?

    "index.php", "usr-check.php", "save-cvs.php" gibi dosya adı/uzantısı
    kullanımları — normal kelime-sınırı regex'i (`(?<![\\w-])`) bunları
    ELEMEZ çünkü nokta bir kelime karakteri değildir. Bu, üçüncü parti bir
    ürünün (örn. bir dosya yöneticisi eklentisi) İÇ dosya yapısıdır, bizim
    envanterimizdeki dilin/platformun kendisiyle ilgisi yoktur.
    """
    return match_start > 0 and s[match_start - 1] == "."


def _has_nearby_version(text: str, pattern: re.Pattern) -> bool:
    """`pattern`'ın metindeki herhangi bir geçişinin (dosya uzantısı
    kullanımları hariç) hemen ardından bir sürüm numarası var mı?
    (bkz. _NEARBY_VERSION_RE yorumu)
    """
    for m in pattern.finditer(text):
        if _is_filename_extension_usage(text, m.start()):
            continue
        if _NEARBY_VERSION_RE.match(text, m.end()):
            return True
    return False


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
    olan varsa kabul edilir. Dosya uzantısı kullanımları (bkz.
    _is_filename_extension_usage) hiç sayılmaz.
    """
    for m in pattern.finditer(norm_title):
        if _is_filename_extension_usage(norm_title, m.start()):
            continue
        if _VULN_CLASS_RE.match(norm_title, m.end()) is None:
            return True
    return False


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
    için bu kurallar pratikte hiçbir şeyi elemiyordu. Aynı gün ikinci bir
    yanlış pozitif deseni daha bulundu: "index.php", "usr-check.php" gibi
    dosya adı/uzantısı kullanımları (bkz. _is_filename_extension_usage) —
    bunlar da hiçbir koşulda geçerli kanıt sayılmaz.
    """
    for name, pattern in patterns:
        if not pattern.search(text):
            continue
        if _is_specific_name(name):
            return name
        if _has_nearby_version(text, pattern) or _title_match_is_genuine(norm_title, pattern):
            return name
    return None


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

    # Ürün adı → derlenmiş kelime-sınırlı desen (her makalede yeniden
    # compile etmek 160 ürün × 500 makale = 80.000 gereksiz compile demekti)
    def _compile(name: str):
        return re.compile(rf"(?<![\w-]){re.escape(name)}(?![\w-])", re.IGNORECASE)

    product_patterns = [(p, _compile(p)) for p in exact_products]
    alias_patterns = [(a, _compile(a)) for a in active_aliases]

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
            "content": clean_content[:500],  # Gemini prompt'una eklenecek RSS özeti
            "priority_score": score_article(text, norm_title, matched_product),
            "image_candidate": article.get("image_candidate", ""),
        })

    # Öncelik puanına göre sırala (en kritik haberler önce, ilk MAX_GEMINI_ARTICLES tanesi Gemini'ye gider)
    matches.sort(key=lambda x: x["priority_score"], reverse=True)
    return matches


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  PROMPT BUILDING
#  Gemini'ye gönderilecek prompt'u hazırla. İki kanaldan veri toplanır:
#    - RSS özeti (hızlı, kısa)
#    - Makale sayfası tam metni (yavaş, detay için — paralel çekilir)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def build_prompt(matched: list[dict]) -> str:
    """En kritik makaleler için Gemini prompt'unu oluştur (limit: MAX_GEMINI_ARTICLES)."""
    capped = matched[:MAX_GEMINI_ARTICLES]  # Sabit ile kontrol — tek noktadan yönetilir

    # ── Dinamik body limiti: makale sayısına göre TPM güvenliği ──────────
    # Her makalenin overhead'ı ~200 token (Product, Title, Date, Link, RSS, Versions).
    # Kalan bütçeyi body_chars olarak eşit dağıt. Az makale = daha derin bağlam.
    _OVERHEAD_PER_ARTICLE = 200  # sabit alanların tahmini token maliyeti
    _CHARS_PER_TOKEN = 4         # ortalama (İngilizce/Türkçe karışık kaynak)
    available_tokens = MAX_PROMPT_TOKENS - (_OVERHEAD_PER_ARTICLE * len(capped))
    body_limit = min(GEMINI_BODY_CHARS, max(500, int(available_tokens * _CHARS_PER_TOKEN / len(capped))))
    log.info("Dynamic body limit: %d chars/article (articles=%d, budget=%dK tokens)",
             body_limit, len(capped), MAX_PROMPT_TOKENS // 1000)

    # Makale sayfalarını paralel çek (8 worker — feed'lerden hızlı)
    log.info("Fetching %d article pages for version details...", len(capped))
    article_pages: dict[str, tuple[str, str]] = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        future_map = {
            pool.submit(fetch_article_page, a.get("link", "")): a.get("link", "")
            for a in capped
        }
        for future in as_completed(future_map):
            url = future_map[future]
            try:
                article_pages[url] = future.result()
            except Exception:
                article_pages[url] = ("", "")

    # Her makale için prompt parçası oluştur
    parts = []
    for i, a in enumerate(capped, 1):
        link = a.get("link", "")
        full_body, og_image = article_pages.get(link, ("", ""))
        a["og_image"] = og_image
        rss_content = a.get("content", "")

        # Versiyon çıkarma: TAM METİN kullan (MAX_BODY_CHARS) → kalite korunsun
        # Bazı advisory'lerde versiyon bilgisi metnin ilerleyen kısımlarında
        combined_text = f"{rss_content} {full_body}"
        versions = extract_versions(combined_text)
        version_str = ", ".join(versions) if versions else "None detected in source"

        # Gemini'ye gönderim: body_limit dinamik olarak ayarlanır (TPM aşımını önler)
        # Versiyonlar zaten "Detected Versions" alanında ayrıca veriliyor
        body_for_gemini = full_body[:body_limit]

        # Her makaleyi numarayla etiketle ve standart alanlarla biçimle
        parts.append(
            f"[{i}]\n"
            f"Product: {a['matched_product']}\n"
            f"Title: {a['title']}\n"
            f"Date: {a.get('pubDate', 'Unknown')}\n"
            f"Link: {link}\n"
            f"RSS Summary: {rss_content}\n"
            f"Article Context: {body_for_gemini}\n"
            f"Detected Versions: {version_str}"
        )
    # Makaleler arası "---" ayracı (Gemini için görsel bölücü)
    return "\n\n---\n\n".join(parts)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  GEMINI ANALYSIS
#  Prompt'u Gemini API'ye gönder, HTML brifing yanıtını al.
#  Katmanlı dayanıklılık: model-içi retry + yedek model zinciri + kalıcı hatada dur.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# ── Hata sınıflandırma ───────────────────────────────────────────────────────
# Gemini API hataları üç gruba ayrılır; her grup farklı ele alınır:
#   permanent  → anahtar/istek hatası; ne retry ne model değişimi düzeltir → dur
#   next_model → bu model kullanılamıyor (kota dolu / model yok) → yedek modele geç
#   transient  → 503/500/504/ağ; kısa bekle, aynı modelde tekrar dene, sonra yedeğe
_PERMANENT_KEYWORDS = (
    "permission_denied", "unauthenticated", "api key not valid",
    "invalid_argument", "failed_precondition",
)
_NEXT_MODEL_KEYWORDS = ("resource_exhausted", "quota", "rate limit", "not_found")

def _classify_error(exc: Exception) -> str:
    """API hatasını 'permanent' | 'next_model' | 'transient' olarak sınıflandır."""
    msg = str(exc).lower()
    code = getattr(exc, "code", None)  # google-genai HTTP status (int) — varsa
    if code in (400, 401, 403) or any(k in msg for k in _PERMANENT_KEYWORDS):
        return "permanent"
    if code in (404, 429) or any(k in msg for k in _NEXT_MODEL_KEYWORDS):
        return "next_model"
    # 503/500/504/ağ + bilinmeyen hatalar → temkinli: geçici say (retry + fallback)
    return "transient"


# Model zinciri: birincil (en kaliteli) + yedekler. Birincil model erişilemez veya
# kotası dolu olursa sıradaki denenir. Her modelin AYRI günlük kotası ve AYRI
# kapasitesi var → 3.5-flash 20 RPD'yi doldursa ya da 503 verse bile brifing kurtulur.
#
# 2026-08-27: gemini-2.0-flash EMEKLİYE AYRILDI ve zincirin son halkası olduğu
# için sessiz bir tek-nokta-arıza haline gelmişti — 3.5 ve 2.5 aynı anda 504
# verdiğinde son yedek de 404 döndü ve brifing tamamen düştü. Zincir mevcut
# modellerle yenilendi (API'den canlı olarak doğrulandı). Modeller Google
# tarafından emekliye ayrıldığı için bu liste yılda birkaç kez kontrol edilmeli:
#   client.models.list() → generateContent destekleyenleri listeler.
#
# 2026-08-27 (aynı gün, ikinci geçiş): gemini-3.7-flash zincire eklendi —
# client.models.list() ile bu tarihte en yeni GA flash model olduğu, ve
# ai.google.dev/gemini-api/docs/deprecations sayfasında 3.5/3.6/3.7-flash
# için "No shutdown date announced" olduğu doğrulandı (2.0-flash ailesinin
# TAMAMI 1 Haziran 2026'da kapatılmış — üstteki arızanın kök nedeni).
# gemini-flash-latest şu an 3.7-flash'a çözülüyor; yine de son halka olarak
# kalıyor çünkü Google yeni bir GA model çıkardığında bu isim otomatik
# günceli takip eder, listedeki sabit isimler etmez.
_MODEL_CHAIN = (
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-2.5-flash",
    "gemini-flash-latest",  # Google'ın güncel flash takma adı — son çare
)

# Geçici hatada model-içi bekleme programı (saniye). Kısa tutulur: yoğunluk geçmezse
# zaten yedek modele düşülür (toplam süre TimeoutStartSec=600 altında kalsın).
_TRANSIENT_BACKOFF = (10, 30)

# Bir denemeyi başlatmak için gereken asgari kalan süre (saniye). Ölçülen en
# yavaş başarılı yanıt ~281 sn olduğundan, bundan azı kalmışsa yeni istek
# atmak yalnızca bütçeyi tüketir.
_MIN_ATTEMPT_SECONDS = 300

# 2026-08-27: Gerçek üretimde gözlemlenen üçüncü bir hata modu — modelin
# HIZLI 503 vermesi değil, isteği ~5 dakika boyunca yanıtsız ASILI TUTUP
# sonra bağlantıyı koparması ("Server disconnected without sending a
# response"). Bu, sabit sayıda deneme yapan eski mantıkla BİRLEŞTİĞİNDE
# felakete yol açtı: tek bir modelde art arda 2 asılı deneme (~2×300 sn)
# toplam bütçenin (900 sn) çoğunu tüketti ve zincir HİÇ sağlıklı olan
# gemini-3.6-flash'a ulaşamadan "bütçe doldu" hatasıyla durdu — o gün
# hiç mail gitmedi (2026-08-27, workflow run 33064763459).
#
# Çözüm: bir deneme bu eşikten UZUN sürüp başarısız olduysa (yani gerçek
# bir asılı-kalma yaşandıysa), aynı modeli TEKRAR DENEMEDEN doğrudan bir
# sonraki modele geç. Aynı modeli tekrar denemek, o model zaten dakikalarca
# yanıt veremiyorsa yardımcı olmaz — sadece paylaşılan toplam bütçeyi,
# asıl işe yarayacak olan FARKLI bir modelin payından çalar. Kısa/anlık
# hatalar (örn. birkaç saniyede dönen 503) bu eşiğin çok altında kalır ve
# hâlâ normal kısa-bekle-tekrar-dene mantığından geçer.
_HANG_THRESHOLD_SECONDS = 60


def analyze_with_gemini(prompt: str) -> str:
    """Gemini API'yi çağır, HTML brifing yanıtını döndür.

    Katmanlı dayanıklılık:
      1) Kalıcı hata (API anahtarı/istek geçersiz) → hemen dur (hiçbir şey düzeltmez)
      2) Kota dolu / model yok → hemen yedek modele geç (retry anlamsız)
      3) Uzun süre asılı kalıp koptu (bkz. _HANG_THRESHOLD_SECONDS) → aynı
         modeli TEKRAR DENEMEDEN yedek modele geç (paylaşılan bütçeyi korur)
      4) Hızlı geçici hata (anlık 503/500/ağ) → aynı modelde kısa beklemeyle
         tekrar dene, tükenirse yedek modele geç
    """
    # API key'i ortam değişkeninden oku (.env'den geldi)
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY not set")

    # http_options.timeout: istek yanıtsız asılı kalırsa (Google tarafında
    # gerçekten yaşanıyor) bunu bir TimeoutError'a çevirir — böylece aşağıdaki
    # retry/model-fallback döngüsü devreye girebilir
    client = genai.Client(
        api_key=api_key,
        http_options=genai.types.HttpOptions(timeout=GEMINI_REQUEST_TIMEOUT_MS),
    )
    last_error = None
    max_attempts = len(_TRANSIENT_BACKOFF) + 1  # model başına: 1 ilk + retry sayısı
    # Toplam bütçe: zincir ne kadar uzarsa uzasın job timeout'unu aşmasın
    deadline = time.monotonic() + GEMINI_TOTAL_BUDGET_SEC

    # Dış döngü: modeller (birincil → yedekler)
    for model in _MODEL_CHAIN:
        # İç döngü: aynı model için geçici hata retry'ları
        for attempt in range(1, max_attempts + 1):
            remaining = deadline - time.monotonic()
            # Bitmesine imkân olmayan bir isteği başlatma — bütçeyi
            # tüketip job'ı timeout'a sürüklemekten başka işe yaramaz.
            if remaining < _MIN_ATTEMPT_SECONDS:
                log.error(
                    "Gemini toplam süre bütçesi (%d sn) doldu — kalan modeller "
                    "denenmeyecek", GEMINI_TOTAL_BUDGET_SEC,
                )
                raise RuntimeError(
                    "Gemini API: toplam süre bütçesi doldu"
                ) from last_error
            attempt_start = time.monotonic()
            try:
                response = client.models.generate_content(
                    model=model,
                    contents=prompt,
                    config=genai.types.GenerateContentConfig(
                        system_instruction=SYSTEM_PROMPT,     # CTI Analist rolü
                    ),
                )
                # Yedek model kullanıldıysa görünür kıl (kalite/teşhis için)
                if model != _MODEL_CHAIN[0]:
                    log.warning("Brifing YEDEK model ile üretildi: %s", model)
                return response.text
            except Exception as exc:
                elapsed = time.monotonic() - attempt_start
                last_error = exc
                kind = _classify_error(exc)

                # Kalıcı hata → geçersiz anahtar/istek; model veya retry çözmez
                if kind == "permanent":
                    log.error("Gemini kalıcı hata (%s): %s", model, exc)
                    raise RuntimeError(
                        "Gemini API kalıcı hata (API anahtarı/istek geçersiz)"
                    ) from exc

                # Bu model kullanılamıyor (kota dolu / model yok) → yedeğe geç
                if kind == "next_model":
                    log.warning(
                        "Model '%s' kullanılamıyor (kota/model yok), yedeğe geçiliyor: %s",
                        model, exc,
                    )
                    break  # iç döngüden çık → sıradaki model

                # Deneme uzun süre ASILI KALDIKTAN SONRA başarısız olduysa
                # (bkz. _HANG_THRESHOLD_SECONDS yorumu) aynı modeli TEKRAR
                # DENEME — paylaşılan bütçeyi boşa harcamadan direkt yedeğe geç.
                if elapsed >= _HANG_THRESHOLD_SECONDS:
                    log.warning(
                        "Model '%s' %.0f sn asılı kaldıktan sonra koptu — "
                        "aynı model tekrar denenmeyecek, yedeğe geçiliyor: %s",
                        model, elapsed, exc,
                    )
                    break  # iç döngüden çık → sıradaki model

                # Hızlı geçici hata (ör. anlık 503) → kısa bekle ve aynı modelde tekrar dene
                if attempt < max_attempts:
                    wait = _TRANSIENT_BACKOFF[attempt - 1]
                    log.warning(
                        "Model '%s' geçici hata (deneme %d/%d): %s — %dsn sonra tekrar",
                        model, attempt, max_attempts, exc, wait,
                    )
                    time.sleep(wait)
                else:
                    # Bu modelde geçici hata sürüyor → yedeğe geç (iç döngü biter)
                    log.warning(
                        "Model '%s' geçici hatada tükendi, yedeğe geçiliyor: %s",
                        model, exc,
                    )

    # Hiçbir model başaramadı
    log.error("Tüm modeller başarısız oldu: %s", ", ".join(_MODEL_CHAIN))
    raise RuntimeError("Gemini API: tüm modeller başarısız oldu") from last_error


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  EMAIL
#  HTML şablonu, taşma tablosu, SMTP gönderim mantığı.
#  Gmail App Password ile STARTTLS üzerinden gönderilir.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Ana e-posta şablonu — {date} ve {content} replace edilir
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
  <p style="color:#888;font-size:13px;margin-top:16px;">Sonraki tarama yarın saat 11:15'te gerçekleştirilecektir.</p>
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


def process_image(url: str, article_link: str) -> bytes | None:
    """Görseli indir, doğrula ve e-posta için optimize et.

    Güvenlik zinciri: SSRF (istek öncesi + redirect sonrası) → SVG reddi →
    boyut tavanı → magic byte doğrulaması → Pillow ile yeniden boyutlandırma.
    Herhangi bir adım başarısız olursa None döner; brifing etkilenmez.
    """
    if not url:
        return None
    try:
        # Göreceli URL'yi makale linkine göre mutlak hale getir
        full_url = urllib.parse.urljoin(article_link, url)
        if not full_url.startswith(("http://", "https://")):
            return None

        # SSRF: istek göndermeden önce kontrol
        if _SSRF_BLOCKED.search(full_url):
            log.warning("SSRF blocked image pre-request: %s", full_url)
            return None

        session = requests.Session()
        session.max_redirects = 3
        # stream=True: tamamını belleğe almadan boyut tavanını uygulayabilmek için
        resp = session.get(full_url, headers=_REQUEST_HEADERS, timeout=IMAGE_FETCH_TIMEOUT, stream=True)
        resp.raise_for_status()

        # SSRF: redirect sonrası NIHAI adresi tekrar kontrol et
        # (iç ağa yönlendirme bilinen bypass yöntemidir)
        if _SSRF_BLOCKED.search(resp.url):
            log.warning("SSRF blocked image post-redirect: %s", resp.url)
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

def inject_images(html_str: str, cid_map: dict[int, str], titles_map: dict[int, str]) -> str:
    """[[IMG:n]] token'larını gerçek <img> tag'i ile değiştir.

    ÖNEMLİ: Bu fonksiyon sanitize_gemini_html() SONRASINDA çalışır. Böylece
    eklenen HTML'i tamamen kod üretir ve sanitizer whitelist'ine img/src
    eklemek gerekmez (prompt injection ile takip pikseli sokulamaz).
    Görseli olmayan token'lar tamamen silinir.
    """
    def repl(m):
        n = int(m.group(1))
        if n in cid_map:
            alt_text = html.escape(titles_map.get(n, ""), quote=True)
            return f'<img src="cid:{cid_map[n]}" style="width:100%;height:auto;border-radius:4px;margin:8px 0;" alt="{alt_text}">'
        return ""
    return re.sub(r'\[\[IMG:(\d+)\]\]', repl, html_str)


# DRY_RUN=true: RSS/eşleştirme/Gemini/versiyon çıkarma tam olarak çalışır
# (secret'lar ve pipeline gerçekten test edilir), ama son adımda SMTP hiç
# çağrılmaz — mail atılmaz. Önizleme yerine logs/dry_run_preview.html'e
# yazılır (görsel <img cid:...> referansları düz HTML'de kırık görünür,
# bu bilinen ve kabul edilen bir sınırlama — asıl amaç metin/versiyon/
# tarih alanlarını mail göndermeden doğrulamak).
DRY_RUN = os.environ.get("DRY_RUN", "").strip().lower() == "true"


def _write_dry_run_preview(email_body: str) -> None:
    """DRY_RUN modunda e-posta gövdesini dosyaya yaz, SMTP'ye hiç dokunma."""
    preview_path = LOG_DIR / "dry_run_preview.html"
    preview_path.write_text(email_body, encoding="utf-8")
    log.info("DRY_RUN aktif — mail GÖNDERİLMEDİ. Önizleme: %s", preview_path)


def send_email(subject: str, html_body: str,
               images: list[tuple[str, bytes]] | None = None) -> None:
    """E-postayı SMTP üzerinden gönder. STARTTLS + Gmail App Password kullanır.

    EMAIL_TO virgülle ayrılarak birden fazla alıcıya gönderim destekler.
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

    if images:
        msg = MIMEMultipart("related")
        msg["Subject"] = subject
        msg["From"] = email_from
        msg["To"] = ", ".join(recipients)
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
        msg["Subject"] = subject
        msg["From"] = email_from
        msg["To"] = ", ".join(recipients)
        msg.attach(MIMEText(html_body, "html", "utf-8"))

    # Güvenli SSL bağlamı (sertifika doğrulama açık)
    context = ssl.create_default_context()
    with smtplib.SMTP(smtp_server, smtp_port) as server:
        server.ehlo()
        server.starttls(context=context)  # Şifreli kanala geç (587 → TLS)
        server.ehlo()
        server.login(username, password)
        # sendmail() liste bekler — tek string verirsen Gmail reddeder
        server.sendmail(email_from, recipients, msg.as_string())

    log.info("Email sent to %s", ", ".join(recipients))


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
    all_articles = fetch_all_feeds()
    log.info("Total articles fetched: %d", len(all_articles))

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

        prompt = build_prompt(top_matches)
        log.info("Sending %d articles to Gemini for analysis...", len(top_matches))
        if overflow_matches:
            log.info("Overflow: %d additional articles will be listed without AI analysis.", len(overflow_matches))

        # Görselleri indir ve optimize et (Gemini'ye giden tüm makalelerde aranır —
        # gerçek sınır MAX_TOTAL_IMAGE_BYTES, sabit makale sayısı değil)
        image_tasks = []
        for i, a in enumerate(top_matches, 1):
            url = a.get("image_candidate") or a.get("og_image")
            if url:
                image_tasks.append((i, url, a.get("link", ""), a.get("title", "")))

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

        # Gemini'nin GERÇEKTEN token yazdığı indeksler — "aynı konu hakkında ek
        # haber" bloğuna düşen makaleler [[IMG:n]] yazmaz, bu yüzden onların
        # görseli indirilmiş olsa bile bütçeye/eke hiç girmemeli.
        # sanitize_gemini_html() bu metni değiştirmez (HTML özel karakteri yok).
        used_indices = {int(n) for n in re.findall(r"\[\[IMG:(\d+)\]\]", briefing_html)}

        cid_map = {}
        titles_map = {}
        total_image_bytes = 0
        final_images = []
        eligible_count = 0
        budget_exceeded = False

        # Priority sırasıyla, sadece Gemini'nin kullandığı indeksler için bütçeye ekle
        for task in image_tasks:
            idx = task[0]
            if idx not in used_indices:
                continue
            img_bytes, title = results_by_index.get(idx, (None, ""))
            if img_bytes:
                eligible_count += 1
                if total_image_bytes + len(img_bytes) > MAX_TOTAL_IMAGE_BYTES:
                    log.warning("Total image bytes limit exceeded. Skipping remaining images.")
                    budget_exceeded = True
                    break
                total_image_bytes += len(img_bytes)
                cid = f"img{idx}"
                cid_map[idx] = cid
                titles_map[idx] = title
                final_images.append((cid, img_bytes))

        # Bütçe kullanımını gözlemlenebilir kıl — havuz genişledikten sonra bu
        # bütçe artık gerçekten dolabiliyor, üretim loglarında görünür olmalı
        log.info(
            "Image budget: %d attached, %.1f KB / %.1f KB used%s",
            len(final_images), total_image_bytes / 1024, MAX_TOTAL_IMAGE_BYTES / 1024,
            " (budget exceeded — remaining skipped)" if budget_exceeded else "",
        )

        # Görselleri enjekte et (token'ları img tag'i ile değiştir veya sil)
        injected_html = inject_images(briefing_html, cid_map, titles_map)

        # Taşma bölümünü ekle (MAX_GEMINI_ARTICLES üzerindeki makaleler için)
        overflow_html = build_overflow_html(overflow_matches)
        full_content = injected_html + overflow_html

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
