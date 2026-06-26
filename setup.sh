#!/bin/bash
# Hatada hemen dur, tanimsiz degisken kullanma, pipe'da hata varsa raporla
set -euo pipefail

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  CTI News Feed Automation — Linux/Ubuntu Setup
#  Bu script projeyi sistemde otomatik kurar:
#  - Python venv, sistem kullanicisi, systemd timer
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Proje sabitleri
INSTALL_DIR="/opt/cti-project"     # Kurulum hedefi
SERVICE_NAME="cti-newsfeed"        # systemd unit adi
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"  # Bu scriptin oldugu klasor (dosyalar buradan kopyalanir)

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  CTI News Feed Automation — Ubuntu Setup"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# Root kontrolu — systemd ve /opt yazma yetkisi gerekli
if [ "$(id -u)" -ne 0 ]; then
    echo ""
    echo "  HATA: Bu script root olarak calistirilmali."
    echo "  Kullanim: sudo bash setup.sh"
    exit 1
fi

# ── 1. Sistem bagimliliklar ──────────────────────────
# python3 (calistirma), python3-venv (sanal ortam), git (gelecekte gerekebilir)
echo ""
echo "[1/7] Sistem paketleri kuruluyor..."
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip git > /dev/null
echo "  ✓ python3, python3-venv, git kuruldu"

# Python versiyon kontrolu (>= 3.10 gerekli — type hint'ler ve match statement icin)
PY_VER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null)
PY_MAJOR=$(echo "$PY_VER" | cut -d. -f1)
PY_MINOR=$(echo "$PY_VER" | cut -d. -f2)
if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 10 ]; }; then
    echo ""
    echo "  ✗ HATA: Python >= 3.10 gerekli (mevcut: $PY_VER)"
    echo "  Ubuntu 22.04+ onerilir."
    exit 1
fi
echo "  ✓ Python $PY_VER (>= 3.10 gereksinimi karsilandi)"

# ── 2. Servis kullanicisi ────────────────────────────
# Yetkisiz sistem kullanicisi (root degil) — guvenlik icin sart
# --no-create-home: home klasoru olusturma, --shell nologin: shell erisimi yok
echo ""
echo "[2/7] Servis kullanicisi olusturuluyor..."
if id "cti" &>/dev/null; then
    echo "  ✓ 'cti' kullanicisi zaten mevcut"
else
    useradd --system --no-create-home --shell /usr/sbin/nologin cti
    echo "  ✓ 'cti' sistem kullanicisi olusturuldu"
fi

# ── 3. Proje dosyalari ──────────────────────────────
echo ""
echo "[3/7] Proje dosyalari kopyalaniyor..."
# logs/ klasoru yoksa olustur (script ilk calismadan once gerekli)
mkdir -p "${INSTALL_DIR}/logs"

# Ana Python scripti ve bagimlilik listesi
cp "${SCRIPT_DIR}/cti_automation.py" "${INSTALL_DIR}/"
cp "${SCRIPT_DIR}/requirements.txt"  "${INSTALL_DIR}/"

# .env dosyasi yoksa ornek sablonundan olustur (kullanici elle duzenleyecek)
if [ ! -f "${INSTALL_DIR}/.env" ]; then
    cp "${SCRIPT_DIR}/.env.example" "${INSTALL_DIR}/.env"
    echo ""
    echo "  ⚠  .env dosyasi olusturuldu — DUZENLEMENIZ GEREKIYOR:"
    echo "     sudo nano ${INSTALL_DIR}/.env"
    echo ""
    echo "     Gerekli degerler:"
    echo "       GEMINI_API_KEY   — Google Gemini API anahtariniz"
    echo "       SMTP_USERNAME    — Gmail adresiniz"
    echo "       SMTP_PASSWORD    — Gmail App Password"
    echo "       EMAIL_FROM       — Gonderen adres"
    echo "       EMAIL_TO         — Alici adres (birden fazla icin virgulle ayir)"
else
    echo "  ✓ .env dosyasi zaten mevcut — atlanıyor"
fi

echo "  ✓ Dosyalar ${INSTALL_DIR}/ altina kopyalandi"

# ── 4. Python sanal ortami ───────────────────────────
# Sistem Python'unu kirletmemek icin izole venv
echo ""
echo "[4/7] Python sanal ortami olusturuluyor..."
python3 -m venv "${INSTALL_DIR}/venv"
# pip'in en son surumune yukselt (eski pip bazi paketleri kuramayabilir)
"${INSTALL_DIR}/venv/bin/pip" install --quiet --upgrade pip
# Pinned bagimliliklari yukle (requirements.txt'te exact versions)
"${INSTALL_DIR}/venv/bin/pip" install --quiet -r "${INSTALL_DIR}/requirements.txt"
echo "  ✓ venv olusturuldu ve bagimliliklar yuklendi"

# ── 5. Dosya izinleri ────────────────────────────────
# Tum proje klasorunun sahibini cti yap (cti kendi dosyalarini okuyabilsin)
echo ""
echo "[5/7] Guvenli dosya izinleri ayarlaniyor..."
chown -R cti:cti "${INSTALL_DIR}"
# .env API key icerir — sadece cti okuyabilir (600 = rw-------)
chmod 600 "${INSTALL_DIR}/.env"
# Script execute + read — sadece cti (700 = rwx------)
chmod 700 "${INSTALL_DIR}/cti_automation.py"
# logs klasoru — cti ve grup (750 = rwxr-x---)
chmod 750 "${INSTALL_DIR}/logs"
echo "  ✓ .env → 600 (sadece cti kullanicisi okuyabilir)"
echo "  ✓ script → 700, logs → 750"

# ── 6. Systemd dosyalari ────────────────────────────
echo ""
echo "[6/7] Systemd service ve timer kuruluyor..."
# service: ne calistirilacagi tanimi (gunluk gorev)
cp "${SCRIPT_DIR}/cti-newsfeed.service" /etc/systemd/system/
# timer: ne zaman calistirilacagi (her gun 11:15 Istanbul)
cp "${SCRIPT_DIR}/cti-newsfeed.timer"   /etc/systemd/system/
# 644 = systemd standart izni (root okuma/yazma, digerleri sadece okuma)
chmod 644 /etc/systemd/system/cti-newsfeed.service
chmod 644 /etc/systemd/system/cti-newsfeed.timer
# Yeni unit dosyalarini systemd'ye tanit
systemctl daemon-reload
echo "  ✓ Service ve timer dosyalari /etc/systemd/system/ altina kopyalandi"

# ── 7. Timer etkinlestirme ───────────────────────────
# enable: sistem baslangicinda otomatik baslat
# start: simdi calistir
echo ""
echo "[7/7] Timer etkinlestiriliyor..."
systemctl enable "${SERVICE_NAME}.timer"
systemctl start  "${SERVICE_NAME}.timer"
echo "  ✓ Timer etkin ve calisiyor"

# ── Ozet & yardimci komutlar ─────────────────────────
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Kurulum tamamlandi!"
echo ""
echo "  Zamanlama : Her gun saat 11:15 (Istanbul)"
echo "  Proje     : ${INSTALL_DIR}/"
echo "  Loglar    : ${INSTALL_DIR}/logs/"
echo ""
echo "  Komutlar:"
echo "    Hemen calistir : sudo systemctl start ${SERVICE_NAME}.service"
echo "    Durum kontrol  : sudo systemctl status ${SERVICE_NAME}.timer"
echo "    Log takibi     : sudo journalctl -u ${SERVICE_NAME}.service -f"
echo "    Sonraki calisma: systemctl list-timers | grep ${SERVICE_NAME}"
echo "    Durdur         : sudo systemctl stop ${SERVICE_NAME}.timer"
echo "    Devre disi     : sudo systemctl disable ${SERVICE_NAME}.timer"
echo ""

# .env hala ornek degerlerle dolu mu? Kullaniciyi uyar
if [ ! -s "${INSTALL_DIR}/.env" ] || grep -q "your-gemini-api-key-here" "${INSTALL_DIR}/.env" 2>/dev/null; then
    echo "  ⚠  ONEMLI: .env dosyasini duzenleyin:"
    echo "     sudo nano ${INSTALL_DIR}/.env"
    echo ""
fi

# Timezone notu — timer artik "Europe/Istanbul" ekiyle saati kendisi sabitliyor,
# yani sunucu hangi saat diliminde olursa olsun calisma 11:15 Istanbul'da olur.
# (11:15: Gemini kotasinin gece yarisi Pasifik sifirlamasindan SONRAYA denk gelir.)
echo "  ℹ  Zamanlama Istanbul saatine sabit (sunucu TZ'sinden bagimsiz)."
echo "     Dogrulama:  systemctl list-timers | grep ${SERVICE_NAME}"
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
