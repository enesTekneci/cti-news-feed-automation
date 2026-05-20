#!/usr/bin/env python3
"""
CTI News Feed Automation
Fetches security RSS feeds, matches against product inventory,
analyzes with Gemini AI, and sends email briefings via Exchange SMTP.
"""

import locale
import os
import re
import html
import logging
import logging.handlers
import smtplib
import ssl
import time
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from html.parser import HTMLParser
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import feedparser
import requests
from google import genai
from dotenv import load_dotenv

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
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        # Max 5 MB per file, keep 3 backups (5MB × 3 = 15MB max disk)
        logging.handlers.RotatingFileHandler(
            LOG_DIR / "cti_automation.log",
            maxBytes=5 * 1024 * 1024,
            backupCount=3,
        ),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("cti")

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  INVENTORY
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

INVENTORY = [
    "Yoast Wordpress Seo", "Wp Fastest Cache", "Wordpress",
    "Jasperreports Server", "Telerik Ui For Asp.Net Ajax",
    "Sun Java System Web Server", "Sophos Sfos", "Sonicwall Sonicos",
    "Sip Protocol", "Contact Form 7", "Python", "Pureftpd", "Proftpd",
    "Powerdns Authoritative Server", "Postfix", "Spring Boot",
    "Spring Framework", "Php", "Plesk", "Parallels Plesk Panel",
    "Javafx", "Iplanet Web Server", "Openssh", "Openresty", "Nginx",
    "Netsweeper", "Microsoft Iis", "Office 365", ".Net",
    "Microsoft Exchange", "Sharepoint", "Outlook Web Access",
    "Windows Server 2008", "Windows Server 2012",
    "Internet Information Services", "Lotus Domino",
    "Litespeed Web Server", "Lighttpd", "Kerio Connect",
    "Redirection Plugin", "Isc Bind", "Hp Jetdirect",
    "Bootstrap Framework", "Fortios", "Fortigate", "Big-Ip Ltm",
    "Big-Ip Local Traffic Manager",
    "Big-Ip Application Security Manager", "Expressjs", "Exim",
    "Sonicwall Network Security Appliance 2400", "Cpanel",
    "Netscaler Gateway Firmware", "Gaia Os", "Vpn-1 Firewall-1 Vsx",
    "Tinyproxy", "Wp Super Cache", "Apache Tomcat",
    "Apache Http Server", "Coyote Http Connector", "Coldfusion Builder",
    "Mini-Httpd", "Zoom", "Esxi", "Vmware Tools", "Vsphere", "Vcenter",
    "Vsphere Esxi", "Vsphere Client",
    "Vrealize Suite Lifecycle Manager", "Netbackup Appliance",
    "Enterprise Vault", "Veeam Backup And Replication", "Teamviewer",
    "Teamviewer Remote", "Solarwinds Platform",
    "Solarwinds Virtualization Manager", "Solarwinds Netflow Realtime",
    "Solarwinds Network Performance Monitor",
    "Orion Ip Address Manager", "Network Configuration Manager",
    "Orion User Device Tracker", "Orion Web Performance Monitor",
    "Server And Application Monitor",
    "Ip Address Manager Web Interface",
    "Orion Netflow Traffic Analyzer", "Sap Router", "Sap Netweaver",
    "Sap Netweaver Abap", "Sap Solution Manager", "Sap Data Services",
    "Sap Web Dispatcher", "Sap Cloud Connector", "Sap Netweaver Java",
    "Sap Enterprise Resource Planning",
    "Sap Netweaver Application Server",
    "Sap Supplier Relationship Management",
    "Sap Business Objects Business Intelligence Platform",
    "Red Hat Enterprise Linux", "Linux Kernel",
    "Red Hat Network Satellite", "Putty", "Pan-Os",
    "Palo Alto Networks", "Oracle Database",
    "Primavera P6 Professional Project Management",
    "Primavera P6 Enterprise Project Portfolio Management", "Chatgpt",
    "Nessus", "Microsoft Teams", "365 Copilot",
    "System Center Configuration Manager", "Keepass",
    "Ibm Security Guardium", "Tivoli Identity Manager",
    "Security Identity Manager", "Qradar SIEM",
    "Hikcentral Professional", "Google Chrome", "Vertex Gemini Api",
    "Fortimanager", "Fortigate 60E", "Fortigate 60F", "Fortigate 80F",
    "Fortigate 100D", "Fortigate 400E", "Fortigate 100F",
    "Fortigate 200E", "Fortigate 1200D", "Fortigate 2600F",
    "Fortiauthenticator", "Forticlient Ems",
    "Forticlient Sslvpn Client", "Forticlient", "Big-Ip I2800",
    "Big-Ip 4000", "Big-Ip Advanced Web Application Firewall",
    "Cyberark Identity", "Cyberark Viewfinity",
    "Endpoint Privilege Manager", "Enterprise Password Vault",
    "Privileged Session Manager", "Citrix Workspace", "Cisco 1921",
    "Cisco 1941", "Cisco 2921", "Cisco 3945", "Cisco Isr 4451-X",
    "Cisco Nexus 9000", "Cisco N9K-C9332Pq", "Cisco Ws-C3850-48T",
    "Cisco Catalyst 2950", "Cisco Catalyst 4500",
    "Cisco Ws-C3850-24Xs", "Cisco N9K-C93108Tc-Ex",
    "Cisco N9K-C93180Yc-Ex", "Cisco N9K-C93240Yc-Fx2",
    "Cisco Catalyst 2960-24Tc-L", "Cisco Catalyst 2960-48Tc-L",
    "Cisco Catalyst 3560X-48P-S", "Cisco Catalyst 2960-48Pst-L",
    "Cisco Catalyst 2960S-24Ts-L", "Cisco Catalyst 2960S-24Ts-S",
    "Cisco Catalyst 2960X-24Ps-L", "Cisco Catalyst 2960X-48Ts-L",
    "Cisco Catalyst 2960S-48Lps-L", "Cisco Catalyst 2960X-48Fps-L",
    "Cisco Catalyst 2960X-48Lpd-L", "Cisco Catalyst 2960X-48Lps-L",
    "Cisco Catalyst 2960Xr-24Ps-L",
    "Cisco Catalyst 2960-Plus 24Tc-L",
    "Cisco Catalyst 2960-Plus 48Tc-L",
    "Cisco Catalyst 2960-Plus 48Pst-L",
    "Cisco Catalyst 2960-Plus 48Pst-S",
    "Cisco 2921 Integrated Services Router", "Autocad", "Autocad Lt",
    "Autocad Plant 3D", "Arubanetworks Clearpass", "Claude Code",
    "7-Zip",
    # ── Microsoft Defender ailesi ────────────────────────
    "Windows Defender", "365 Defender Portal",
    "Defender For Endpoint", "Defender For Identity",
    "Windows Defender For Endpoint",
    "Defender For Endpoint EDR Sensor",
    "Defender Security Intelligence Updates",
    # ── Microsoft diğer ─────────────────────────────────
    "Windows",
    # ── VMware ──────────────────────────────────────────
    "Vmware Workstation",
    # ── SolarWinds (Orion uzun formları) ────────────────
    "Orion Network Performance Monitor",
    "Orion Server And Application Manager",
    "Orion Network Configuration Manager",
    # ── IBM (uzun form varyantları) ─────────────────────
    "QRadar Security Information And Event Manager",
    "Security Guardium Database Activity Monitor",
    # ── F5 ──────────────────────────────────────────────
    "Big-Ip",
    # ── Red Hat ─────────────────────────────────────────
    "Enterprise Linux Kernel",
    # ── Fortinet (uzun form varyantı) ───────────────────
    "Forticlient Endpoint Management Server",
]

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  RSS FEEDS
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
    ("Fortinet Threat Research", "https://feeds.fortinet.com/fortinet/blog/threat-research"),
    ("Fortinet PSIRT", "https://filestore.fortinet.com/fortiguard/rss/ir.xml"),
    ("Google Project Zero", "https://googleprojectzero.blogspot.com/feeds/posts/default"),
    ("Krebs on Security", "https://krebsonsecurity.com/feed"),
    ("Microsoft MSRC Update Guide", "https://api.msrc.microsoft.com/update-guide/rss"),
    ("Microsoft Security Blog", "https://microsoft.com/en-us/security/blog/feed"),
    ("NCSC UK", "https://www.ncsc.gov.uk/api/1/services/v1/all-rss-feed.xml"),
    ("NVD Recent CVEs", "https://nvd.nist.gov/feeds/xml/cve/misc/nvd-rss.xml"),
    ("Palo Alto Security Advisories", "https://security.paloaltonetworks.com/rss.xml"),
    ("Palo Alto Unit 42", "https://unit42.paloaltonetworks.com/feed"),
    ("Recorded Future", "https://www.recordedfuture.com/feed"),
    ("SANS ISC", "https://isc.sans.edu/rssfeed_full.xml"),
    ("Securelist (Kaspersky)", "https://securelist.com/feed"),
    ("SolarWinds Security Advisories", "https://www.solarwinds.com/shared-content/rss-feed/solarwinds-cve-rss-feed"),
    ("SOCRadar", "https://socradar.io/feed/"),
    ("The Record by Recorded Future", "https://therecord.media/feed"),
    ("Veeam Security Advisories", "https://www.veeam.com/services/open/kb/security-feed"),
    # Tier 2: Supporting Sources
    ("Bitdefender Labs", "https://bitdefender.com/blog/api/rss/labs"),
    ("Bleeping Computer", "https://www.bleepingcomputer.com/feed/"),
    ("Broadcom/Symantec Blog", "https://sed-cms.broadcom.com/rss/v1/blogs/rss.xml"),
    ("BSI CERT-Bund", "https://wid.cert-bund.de/content/public/securityAdvisory/rss"),
    ("Cybersecurity News", "https://cybersecuritynews.com/feed/"),
    ("Infosecurity Magazine", "https://infosecurity-magazine.com/rss/news"),
    ("JPCERT/CC", "http://jvndb.jvn.jp/en/rss/jvndb_new.rdf"),
    ("Malwarebytes Labs", "https://blog.malwarebytes.com/feed"),
    ("Maryland MCAC Cyber Threats", "https://mcac.maryland.gov/tag/cyber-threats/feed"),
    ("Microsoft MSRC Blog", "https://msrc.microsoft.com/blog/feed"),
    ("NIST Cybersecurity Insights", "https://nist.gov/blogs/cybersecurity-insights/rss.xml"),
    ("Security Affairs", "https://securityaffairs.co/feed"),
    ("SentinelOne", "https://sentinelone.com/feed"),
    ("SOC Prime", "https://socprime.com/feed"),
    ("The Hacker News", "https://thehackernews.com/feeds/posts/default"),
    ("Wired", "https://www.wired.com/feed/category/security/latest/rss"),
    # Tier 3: Ek kaynaklar
    ("Cisco Event Responses", "https://sec.cloudapps.cisco.com/security/center/eventResponses_20.xml"),
    ("Cisco Talos (FeedBurner)", "http://feeds.feedburner.com/feedburner/Talos"),
    ("DFIR Report", "https://thedfirreport.com/feed/"),
    ("FortiGuard PSIRT", "https://fortiguard.fortinet.com/rss/ir.xml"),
    ("GitHub Advisory — npm", "https://azu.github.io/github-advisory-database-rss/npm.json"),
    ("GitHub Advisory — pip", "https://azu.github.io/github-advisory-database-rss/pip.json"),
    ("GitHub Advisory — Maven", "https://azu.github.io/github-advisory-database-rss/maven.json"),
    ("GitHub Advisory — Go", "https://azu.github.io/github-advisory-database-rss/go.json"),
    ("Infostealers", "https://www.infostealers.com/rss-feed/"),
    ("NVD Analyzed CVEs", "https://nvd.nist.gov/feeds/xml/cve/misc/nvd-rss-analyzed.xml"),
    ("Palo Alto Security Advisories (legacy)", "http://securityadvisories.paloaltonetworks.com/"),
    ("Ransomware Live", "https://www.ransomware.live/rss"),
    ("Red Canary", "https://redcanary.com/blog/feed/"),
    ("Red Hat Security Advisories", "https://access.redhat.com/security/data/rhsa.rss"),
    ("SentinelOne Labs", "https://www.sentinelone.com/labs/feed/"),
    ("Recorded Future (FeedBurner)", "https://feeds.feedburner.com/threatintelligence/pvexyqv7v0v"),
    ("Unit 42 Threat Research", "https://unit42.paloaltonetworks.com/category/threat-research/feed/"),
    ("USOM Duyurular", "https://www.usom.gov.tr/rss/duyuru.rss"),
    ("USOM Tehditler", "https://www.usom.gov.tr/rss/tehdit.rss"),
    ("USOM Zararlı Bağlantılar", "https://www.usom.gov.tr/rss/zararli-baglanti.rss"),
    ("ZDI Upcoming Advisories", "https://www.zerodayinitiative.com/rss/upcoming/"),
    ("ZDI Published Advisories", "https://www.zerodayinitiative.com/rss/published/"),
]

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  HIGH-SIGNAL KEYWORDS & VENDOR ALIASES
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
]

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


def score_article(text: str) -> int:
    """Makale metnine göre öncelik puanı hesapla (yüksek = daha kritik)."""
    total = 0
    for kw in HIGH_SIGNAL:
        if kw in text:
            total += _SIGNAL_SCORES.get(kw, _DEFAULT_SIGNAL_SCORE)
    return total

VENDOR_ALIASES = [
    {
        # Cisco router/switch modelleri envanterde — OS ve platform alias'ları
        "vendor_key": "cisco",
        "aliases": [
            "cisco ios", "cisco nx-os", "cisco asa", "cisco ftd",
            "cisco catalyst", "cisco nexus", "cisco isr",
        ],
    },
    {
        # FortiOS/FortiGate envanterde — "forti" prefix ile eşleşme
        "vendor_key": "forti",
        "aliases": [
            "fortinet", "fortios", "fortigate", "fortimanager",
            "forticlient", "fortiauthenticator", "fortianalyzer",
        ],
    },
    {
        # ESXi/vSphere/vCenter envanterde — VMware platform alias'ları
        "vendor_key": "vmware",
        "aliases": [
            "vmware", "esxi", "vsphere", "vcenter", "vrealize",
        ],
    },
    {
        # Big-IP LTM/ASM/AWAF envanterde — F5 alternatif adları
        "vendor_key": "big-ip",
        "aliases": [
            "big-ip", "f5 big-ip", "f5 ltm", "f5 asm", "f5 waf",
        ],
    },
    {
        # SAP Netweaver/Web Dispatcher envanterde — SAP platform alias'ları
        "vendor_key": "sap",
        "aliases": [
            "sap netweaver", "sap abap", "sap solution manager",
            "sap web dispatcher",
        ],
    },
    {
        # Exchange/IIS/Teams/SharePoint envanterde — Microsoft alias'ları
        "vendor_key": "microsoft",
        "aliases": [
            "windows server", "microsoft exchange", "sharepoint",
            "office 365", "microsoft 365", "microsoft iis",
            "active directory", "microsoft teams", "ms exchange",
        ],
    },
    {
        # PAN-OS envanterde — Palo Alto alias'ları
        "vendor_key": "palo alto",
        "aliases": [
            "pan-os", "palo alto networks",
        ],
    },
    {
        # SolarWinds Platform envanterde — Orion alias'ları
        "vendor_key": "solarwinds",
        "aliases": ["solarwinds", "orion platform", "solarwinds orion"],
    },
    {
        # Veeam Backup And Replication envanterde
        "vendor_key": "veeam",
        "aliases": ["veeam backup", "veeam replication"],
    },
    {
        # WordPress/Yoast/WP Cache envanterde — sadece core alias'lar
        "vendor_key": "wordpress",
        "aliases": [
            "wordpress",
        ],
    },
    {
        # Apache Tomcat / HTTP Server envanterde
        "vendor_key": "apache",
        "aliases": [
            "apache tomcat", "apache httpd", "apache http server",
        ],
    },
    {
        # Citrix Workspace / Netscaler envanterde
        "vendor_key": "citrix",
        "aliases": [
            "citrix", "netscaler", "citrix adc", "citrix workspace",
        ],
    },
    {
        # CyberArk Identity/Viewfinity/EPM envanterde
        "vendor_key": "cyberark",
        "aliases": [
            "cyberark", "privileged access manager",
        ],
    },
]

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  GEMINI SYSTEM PROMPT
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

SYSTEM_PROMPT = """Sen kıdemli bir Siber Tehdit İstihbaratı (CTI) Analistsin ve bir güvenlik operasyonları ekibine doğrudan danışmanlık yapıyorsun.

Sana numaralandırılmış bir güvenlik haberleri listesi verilecek. Her haber, ortamımızdaki bir ürünle eşleştirilmiş olacak.

HER HABER İÇİN aşağıdaki HTML formatında bir brifing bloğu yaz:

<div style="margin-bottom:24px;padding:16px;border-left:4px solid [SEVERİTE_RENK];background:#f9f9f9;font-family:Arial,sans-serif;">
  <h3 style="margin:0 0 8px 0;color:[SEVERİTE_RENK];">[SEVERİTE: YÜKSEK/ORTA/DÜŞÜK] Haber Başlığı</h3>
  <p><strong>📅 Tarih:</strong> Yayın tarihi</p>
  <p><strong>🔗 Kaynak:</strong> <a href="LINK">LINK</a></p>
  <p><strong>💾 Eşleşen Ürün:</strong> matched_product değeri</p>
  <p><strong>🔴 Etkilenen Sürümler:</strong> Zafiyetten etkilenen (savunmasız) versiyon numaraları/aralıkları</p>
  <p><strong>🟢 Yamalı Sürümler:</strong> Yamayı içeren güvenli versiyon numaraları (yükseltme hedefi)</p>
  <p><strong>🎯 Etkilenen:</strong> Etkilenen yazılım, donanım veya gruplar</p>
  <p><strong>📝 Özet:</strong> Temel tehdit veya sorunu 25 kelimede özetle</p>
  <p><strong>🛡️ Aksiyon:</strong> Doğrudan talimat</p>
  <p><strong>💡 Öneri:</strong> Bir stratejik tavsiye</p>
</div>

SEVERİTE_RENK: YÜKSEK=#dc3545, ORTA=#fd7e14, DÜŞÜK=#28a745

KURALLAR:
- Yanıtın tamamı TÜRKÇE olmalı. Teknik terimler, CVE numaraları, ürün isimleri ve komutlar İNGİLİZCE kalmalı.
- Giriş veya sonuç cümlesi YAZMA. Doğrudan ilk brifing bloğuyla başla.
- "Özet" 25 kelimeyi geçmemeli.
- "Aksiyon" imperatif ve doğrudan olmalı. Spesifik bir aksiyon yoksa: "Güncellemeleri takip et."
- Eğer iki haber aynı CVE veya olayı işliyorsa, ikincisi için yalnızca şunu yaz:
  <div style="margin-bottom:24px;padding:12px;border-left:4px solid #6c757d;background:#f9f9f9;font-family:Arial,sans-serif;">
    <p><strong>Aynı konu hakkında ek haber:</strong> İlk haberin başlığı</p>
    <p><strong>🔗 Link:</strong> <a href="LINK">LINK</a></p>
  </div>
- Her haberde "Full Article Content" ve "Detected Versions" alanları verilmiştir. Versiyon bilgisini doldururken bu verileri DİKKATLİCE analiz et:
  * "Etkilenen Sürümler" alanına YALNIZCA zafiyetten etkilenen (savunmasız) versiyonları yaz. "< 12.1.4-h5" ifadesi "12.1.4-h5'ten önceki tüm sürümler etkileniyor" demektir. Ürün adıyla birlikte yaz (örn. "PAN-OS 11.2.0 – 11.2.4-h16", "FortiOS < 7.4.7").
  * "Yamalı Sürümler" alanına yamayı/düzeltmeyi içeren güvenli sürümleri yaz. ">= 12.1.4-h5" veya "fixed in 7.4.7" ifadesi yamalı sürümdür. Yükseltme hedefi olarak göster (örn. "PAN-OS >= 11.2.4-h17", "FortiOS 7.4.7 veya üzeri").
  * Birden fazla ürün dalı (branch) etkileniyorsa her dalı ayrı ayrı listele.
  * "Affected/Unaffected" veya "before/prior to" gibi bağlamsal ipuçlarına dikkat et.
  * Haberde hiçbir versiyon bilgisi gerçekten yoksa her iki alan için de "Belirtilmemiş — kaynağı kontrol edin" yaz.
- SEVERİTE belirleme rehberi: YÜKSEK = aktif exploitation / kritik RCE / veri ihlali; ORTA = yaması mevcut kritik açık / aktif campaign; DÜŞÜK = potansiyel risk / öneri niteliğinde. Tüm haberleri yüksek SEVERİTE'den düşük SEVERİTE'ye doğru sırala. """


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  HELPERS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_HTML_TAG = re.compile(r"<[^>]*>")
_WHITESPACE = re.compile(r"\s+")


def strip_html(html: str) -> str:
    return _WHITESPACE.sub(" ", _HTML_TAG.sub(" ", html or "")).strip()


def norm(s: str) -> str:
    return _WHITESPACE.sub(" ", (s or "").lower()).strip()


# Versiyon numaralarını yakalayan regex desenleri
_VERSION_RE = re.compile(
    r"""
    # "version 7.4.2", "ver 3.1.0", "v2.0.1"
    (?:versions?\s*:?\s*|ver\.?\s*|[Vv])(\d+\.\d+(?:\.\d+)+(?:[a-z0-9._-]*)?)
    |
    # "FortiOS 7.0.0 through 7.4.2", "7.0 – 7.6", "< 9.0.98"
    (\d+\.\d+(?:\.\d+)*)\s*(?:through|thru|to|–|—|-)\s*(\d+\.\d+(?:\.\d+)*)
    |
    # "before 9.0.98", "prior to 10.2.1", "earlier than 7.6.3", "< 3.1.0"
    (?:before|prior\s+to|earlier\s+than|<)\s*(\d+\.\d+(?:\.\d+)+)
    |
    # Ürün adından sonra gelen bağımsız versiyon: "FortiOS 7.4.2", "PHP 8.3.12"
    (?<=[A-Za-z]\s)(\d+\.\d+\.\d+(?:\.\d+)*(?:[a-z0-9._-]*)?)
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Yanlış pozitif versiyonları filtrele (tarihler, CVE numaraları vb.)
_FALSE_VERSION_RE = re.compile(
    r"^(?:20[012]\d\.\d|CVE-|CWE-|19\d\d\.|0\.0\.0$)",
    re.IGNORECASE,
)


def extract_versions(text: str) -> list[str]:
    """Metinden versiyon numaralarını/aralıklarını çıkar."""
    found: list[str] = []
    for m in _VERSION_RE.finditer(text):
        if m.group(2) and m.group(3):
            token = f"{m.group(2)} – {m.group(3)}"
        elif m.group(4):
            token = f"< {m.group(4)}"
        else:
            token = m.group(1) or m.group(5) or ""
        token = token.strip(" .,;)")
        if not token or len(token) < 3:
            continue
        if _FALSE_VERSION_RE.match(token):
            continue
        if token not in found:
            found.append(token)
    return found


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  HTML SANITIZATION (Gemini output → email injection protection)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_ALLOWED_TAGS = frozenset([
    "div", "p", "h1", "h2", "h3", "h4", "strong", "em", "a", "br",
    "span", "ul", "ol", "li", "table", "tr", "td", "th", "thead", "tbody",
])

_ALLOWED_ATTRS = frozenset(["style", "href", "class"])

# Dangerous patterns in attribute values
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


_REQUEST_HEADERS = {
    "User-Agent": "CTI-Automation/1.0 (Security Feed Scanner)",
    "Accept": "text/html,application/xhtml+xml",
}

# SSRF koruması: iç ağ adreslerine istek yapılmasını engelle
_SSRF_BLOCKED = re.compile(
    r"^https?://("
    r"localhost|127\.|10\.|172\.(1[6-9]|2\d|3[01])\.|192\.168\."
    r"|169\.254\.|0\.0\.0\.0|\[::1\]|metadata\.google"
    r")",
    re.IGNORECASE,
)


def fetch_article_body(url: str, timeout: int = 12) -> str:
    """Makale URL'sine gidip sayfa içeriğini düz metin olarak döndürür."""
    if not url or not url.startswith("http"):
        return ""
    if _SSRF_BLOCKED.search(url):
        log.warning("SSRF blocked: %s", url)
        return ""
    try:
        session = requests.Session()
        session.max_redirects = 3
        resp = session.get(
            url, headers=_REQUEST_HEADERS, timeout=timeout, verify=True,
            allow_redirects=True,
        )
        resp.raise_for_status()
        raw = strip_html(resp.text)
        return _WHITESPACE.sub(" ", raw).strip()[:5000]
    except Exception as exc:
        log.warning("Article fetch failed (%s): %s", url, exc)
        return ""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  RSS FETCHING
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def fetch_feed(name: str, url: str) -> list[dict]:
    try:
        feed = feedparser.parse(url)
        articles = []
        for entry in feed.entries:
            articles.append({
                "title": getattr(entry, "title", ""),
                "link": getattr(entry, "link", getattr(entry, "id", "")),
                "pubDate": getattr(entry, "published", getattr(entry, "updated", "")),
                "isoDate": getattr(entry, "published", getattr(entry, "updated", "")),
                "description": getattr(entry, "summary", ""),
                "content_encoded": (
                    entry.content[0].value if hasattr(entry, "content") and entry.content else ""
                ),
                "source": name,
            })
        return articles
    except Exception as e:
        log.warning("Feed %s failed: %s", name, e)
        return []


def fetch_all_feeds() -> list[dict]:
    all_articles = []
    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = {pool.submit(fetch_feed, name, url): name for name, url in FEEDS}
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
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def parse_date(date_str: str) -> datetime | None:
    if not date_str:
        return None
    for fmt in (
        "%a, %d %b %Y %H:%M:%S %z",
        "%a, %d %b %Y %H:%M:%S %Z",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
    ):
        try:
            dt = datetime.strptime(date_str.strip(), fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    try:
        import email.utils
        parsed = email.utils.parsedate_to_datetime(date_str)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except Exception:
        return None


def filter_recent(articles: list[dict], hours: int = 24) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    recent = []
    for a in articles:
        dt = parse_date(a.get("isoDate") or a.get("pubDate", ""))
        if dt and dt >= cutoff:
            recent.append(a)
    return recent


def match_articles(articles: list[dict]) -> list[dict]:
    exact_products = [norm(p) for p in INVENTORY if len(p) >= 3]

    active_aliases = []
    for entry in VENDOR_ALIASES:
        if any(entry["vendor_key"] in p for p in exact_products):
            active_aliases.extend(norm(a) for a in entry["aliases"])

    seen_titles: set[str] = set()
    matches = []

    for article in articles:
        raw_content = article.get("content_encoded") or article.get("description", "")
        title = article.get("title", "")
        norm_title = norm(title)

        if norm_title in seen_titles:
            continue

        clean_content = norm(strip_html(raw_content))[:3000]
        text = norm_title + " " + clean_content

        if not any(kw in text for kw in HIGH_SIGNAL):
            continue

        matched_product = None
        for product in exact_products:
            if len(product) < 3:
                continue
            escaped = re.escape(product)
            if re.search(rf"(?<![\w-]){escaped}(?![\w-])", text, re.IGNORECASE):
                matched_product = product
                break

        if not matched_product:
            for alias in active_aliases:
                escaped_alias = re.escape(alias)
                if re.search(rf"(?<![\w-]){escaped_alias}(?![\w-])", text, re.IGNORECASE):
                    matched_product = alias
                    break

        if not matched_product:
            continue

        seen_titles.add(norm_title)
        matches.append({
            "title": title,
            "link": article.get("link", ""),
            "pubDate": article.get("pubDate", ""),
            "matched_product": matched_product,
            "content": clean_content[:500],
            "priority_score": score_article(text),
        })

    # Öncelik puanına göre sırala (en kritik haberler önce)
    matches.sort(key=lambda x: x["priority_score"], reverse=True)
    return matches


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  PROMPT BUILDING
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def build_prompt(matched: list[dict]) -> str:
    capped = matched[:15]

    # Makale sayfalarını paralel çek (versiyon bilgisi için)
    log.info("Fetching %d article pages for version details...", len(capped))
    article_bodies: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        future_map = {
            pool.submit(fetch_article_body, a.get("link", "")): a.get("link", "")
            for a in capped
        }
        for future in as_completed(future_map):
            url = future_map[future]
            try:
                article_bodies[url] = future.result()
            except Exception:
                article_bodies[url] = ""

    parts = []
    for i, a in enumerate(capped, 1):
        link = a.get("link", "")
        full_body = article_bodies.get(link, "")
        rss_content = a.get("content", "")

        # Versiyon çıkarma: TAM METİN kullan (5000 char) → kalite korunsun
        combined_text = f"{rss_content} {full_body}"
        versions = extract_versions(combined_text)
        version_str = ", ".join(versions) if versions else "None detected in source"

        # Gemini'ye gönderim: KISA BAĞLAM yeter (1500 char) → token tasarrufu
        # Versiyonlar zaten "Detected Versions" alanında ayrıca veriliyor
        body_for_gemini = full_body[:1500]

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
    return "\n\n---\n\n".join(parts)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  GEMINI ANALYSIS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def analyze_with_gemini(prompt: str, max_retries: int = 3) -> str:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY not set")

    client = genai.Client(api_key=api_key)
    last_error = None

    for attempt in range(1, max_retries + 1):
        try:
            response = client.models.generate_content(
                model="gemini-3.5-flash",
                contents=prompt,
                config=genai.types.GenerateContentConfig(
                    system_instruction=SYSTEM_PROMPT,
                ),
            )
            return response.text
        except Exception as exc:
            last_error = exc
            if attempt < max_retries:
                wait = 2 ** attempt  # 2s, 4s, 8s exponential backoff
                log.warning(
                    "Gemini API attempt %d/%d failed: %s — retrying in %ds",
                    attempt, max_retries, exc, wait,
                )
                time.sleep(wait)
            else:
                log.error("Gemini API failed after %d attempts: %s", max_retries, exc)

    raise RuntimeError(f"Gemini API failed after {max_retries} attempts") from last_error


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  EMAIL
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

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

NO_THREATS_CONTENT = """\
<div style="padding:24px;text-align:center;">
  <p style="font-size:48px;margin:0;">✅</p>
  <h2 style="color:#28a745;">Tehdit Tespit Edilmedi</h2>
  <p style="color:#555;">Bugün envanterinizdeki ürünleri etkileyen aktif bir tehdit veya kritik güvenlik açığı tespit edilmedi.</p>
  <p style="color:#888;font-size:13px;margin-top:16px;">Sonraki tarama yarın saat 09:00'da gerçekleştirilecektir.</p>
</div>"""

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
    """Gemini kapsamı dışında kalan makaleler için basit HTML tablo oluştur."""
    if not overflow_articles:
        return ""
    rows = []
    for a in overflow_articles:
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


def send_email(subject: str, html_body: str) -> None:
    smtp_server = os.environ.get("SMTP_SERVER", "smtp.gmail.com")
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    username = os.environ.get("SMTP_USERNAME", "")
    password = os.environ.get("SMTP_PASSWORD", "")
    email_from = os.environ.get("EMAIL_FROM", username)
    email_to_raw = os.environ.get("EMAIL_TO", "")
    # Virgülle ayrılmış birden fazla alıcı desteklenir
    recipients = [addr.strip() for addr in email_to_raw.split(",") if addr.strip()]

    if not all([username, password, recipients]):
        raise RuntimeError("SMTP credentials or EMAIL_TO not configured")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = email_from
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    context = ssl.create_default_context()
    with smtplib.SMTP(smtp_server, smtp_port) as server:
        server.ehlo()
        server.starttls(context=context)
        server.ehlo()
        server.login(username, password)
        server.sendmail(email_from, recipients, msg.as_string())

    log.info("Email sent to %s", ", ".join(recipients))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  MAIN
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def main() -> None:
    log.info("=" * 60)
    log.info("CTI News Feed Automation — started")
    today = turkish_date()

    # 1. Fetch all RSS feeds
    log.info("Fetching %d RSS feeds...", len(FEEDS))
    all_articles = fetch_all_feeds()
    log.info("Total articles fetched: %d", len(all_articles))

    # 2. Filter to last 24 hours
    recent = filter_recent(all_articles)
    log.info("Articles from last 24h: %d", len(recent))

    # 3. Match against inventory
    matched = match_articles(recent)
    log.info("Articles matching inventory: %d", len(matched))

    # 4. Analyze & send
    if matched:
        # İlk 15 makale Gemini ile detaylı analiz edilir
        top_matches = matched[:15]
        overflow_matches = matched[15:]

        prompt = build_prompt(top_matches)
        log.info("Sending %d articles to Gemini for analysis...", len(top_matches))
        if overflow_matches:
            log.info("Overflow: %d additional articles will be listed without AI analysis.", len(overflow_matches))

        raw_briefing = analyze_with_gemini(prompt)
        briefing_html = sanitize_gemini_html(raw_briefing)

        # Taşma bölümünü ekle (varsa)
        overflow_html = build_overflow_html(overflow_matches)
        full_content = briefing_html + overflow_html

        email_body = EMAIL_TEMPLATE.replace("{date}", today).replace("{content}", full_content)
        send_email(
            subject=f"🛡️ CTI Tehdit Brifing — {today}",
            html_body=email_body,
        )
        log.info("Threat briefing sent successfully.")
    else:
        email_body = EMAIL_TEMPLATE.replace("{date}", today).replace("{content}", NO_THREATS_CONTENT)
        send_email(
            subject=f"✅ CTI Tarama — Tehdit Yok — {today}",
            html_body=email_body,
        )
        log.info("No threats — notification sent.")

    log.info("CTI News Feed Automation — finished")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log.exception("CTI Automation — unhandled exception")
        raise
