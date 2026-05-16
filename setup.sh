#!/bin/bash
set -euo pipefail

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  CTI News Feed Automation — Linux/Ubuntu Setup
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

INSTALL_DIR="/opt/cti-project"
SERVICE_NAME="cti-newsfeed"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  CTI News Feed Automation — Ubuntu Setup"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# Root kontrolu
if [ "$(id -u)" -ne 0 ]; then
    echo ""
    echo "  HATA: Bu script root olarak calistirilmali."
    echo "  Kullanim: sudo bash setup.sh"
    exit 1
fi

# ── 1. Sistem bagimliliklar ──────────────────────────
echo ""
echo "[1/7] Sistem paketleri kuruluyor..."
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip git > /dev/null
echo "  ✓ python3, python3-venv, git kuruldu"

# ── 2. Servis kullanicisi ────────────────────────────
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
mkdir -p "${INSTALL_DIR}/logs"

# Ana proje dosyalarini kopyala (ayni klasorden)
cp "${SCRIPT_DIR}/cti_automation.py" "${INSTALL_DIR}/"
cp "${SCRIPT_DIR}/requirements.txt"  "${INSTALL_DIR}/"

# .env dosyasi yoksa ornekten olustur
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
    echo "       EMAIL_TO         — Alici adres"
else
    echo "  ✓ .env dosyasi zaten mevcut — atlanıyor"
fi

echo "  ✓ Dosyalar ${INSTALL_DIR}/ altina kopyalandi"

# ── 4. Python sanal ortami ───────────────────────────
echo ""
echo "[4/7] Python sanal ortami olusturuluyor..."
python3 -m venv "${INSTALL_DIR}/venv"
"${INSTALL_DIR}/venv/bin/pip" install --quiet --upgrade pip
"${INSTALL_DIR}/venv/bin/pip" install --quiet -r "${INSTALL_DIR}/requirements.txt"
echo "  ✓ venv olusturuldu ve bagimliliklar yuklendi"

# ── 5. Dosya izinleri ────────────────────────────────
echo ""
echo "[5/7] Guvenli dosya izinleri ayarlaniyor..."
chown -R cti:cti "${INSTALL_DIR}"
chmod 600 "${INSTALL_DIR}/.env"
chmod 700 "${INSTALL_DIR}/cti_automation.py"
chmod 750 "${INSTALL_DIR}/logs"
echo "  ✓ .env → 600 (sadece cti kullanicisi okuyabilir)"
echo "  ✓ script → 700, logs → 750"

# ── 6. Systemd dosyalari ────────────────────────────
echo ""
echo "[6/7] Systemd service ve timer kuruluyor..."
cp "${SCRIPT_DIR}/cti-newsfeed.service" /etc/systemd/system/
cp "${SCRIPT_DIR}/cti-newsfeed.timer"   /etc/systemd/system/
chmod 644 /etc/systemd/system/cti-newsfeed.service
chmod 644 /etc/systemd/system/cti-newsfeed.timer
systemctl daemon-reload
echo "  ✓ Service ve timer dosyalari /etc/systemd/system/ altina kopyalandi"

# ── 7. Timer etkinlestirme ───────────────────────────
echo ""
echo "[7/7] Timer etkinlestiriliyor..."
systemctl enable "${SERVICE_NAME}.timer"
systemctl start  "${SERVICE_NAME}.timer"
echo "  ✓ Timer etkin ve calisiyor"

# ── Ozet ─────────────────────────────────────────────
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Kurulum tamamlandi!"
echo ""
echo "  Zamanlama : Her gun saat 09:00"
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
if [ ! -s "${INSTALL_DIR}/.env" ] || grep -q "your-gemini-api-key-here" "${INSTALL_DIR}/.env" 2>/dev/null; then
    echo "  ⚠  ONEMLI: .env dosyasini duzenleyin:"
    echo "     sudo nano ${INSTALL_DIR}/.env"
    echo ""
fi
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
