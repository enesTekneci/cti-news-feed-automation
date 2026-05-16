# CTI News Feed Automation — Linux/Ubuntu Kurulum Rehberi

Bu rehber, CTI News Feed Automation projesini Ubuntu sunucusuna kurmak ve systemd timer ile her gun otomatik calistirmak icin gereken tum adimlari icerir.

---

## Gereksinimler

- Ubuntu 20.04 / 22.04 / 24.04 (veya Debian tabanli dagitim)
- Python 3.10+
- Internete erisim (RSS feed ve Gemini API icin)
- Gmail hesabi + App Password (SMTP icin)
- Google Gemini API anahtari

---

## Hizli Kurulum (Otomatik)

Repo'yu klonla ve setup scriptini calistir:

```bash
# 1. Repo'yu klonla
cd /tmp
git clone <REPO_URL> cti-project
cd cti-project/linux-ubuntu

# 2. Setup scriptini calistir (root gerekli)
sudo bash setup.sh

# 3. .env dosyasini duzenle (API key ve SMTP bilgileri)
sudo nano /opt/cti-project/.env

# 4. Test et
sudo systemctl start cti-newsfeed.service

# 5. Loglari kontrol et
sudo journalctl -u cti-newsfeed.service -f
```

Setup scripti su islemleri otomatik yapar:
- `python3`, `python3-venv`, `git` paketlerini kurar
- `cti` sistem kullanicisi olusturur
- Proje dosyalarini `/opt/cti-project/` altina kopyalar
- Python sanal ortami olusturur ve bagimliliklari yukler
- Dosya izinlerini guvenli sekilde ayarlar
- Systemd service ve timer dosyalarini kurar
- Timer'i etkinlestirir (her gun 09:00)

---

## Manuel Kurulum (Adim Adim)

Otomatik kurulum yerine her seyi kendiniz yapmak isterseniz:

### Adim 1: Sistem paketlerini kur

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3 python3-venv python3-pip git
```

### Adim 2: Servis kullanicisi olustur

Script'i root olarak degil, izole bir kullanici ile calistirmak guvenlik acisindan onemlidir:

```bash
sudo useradd --system --no-create-home --shell /usr/sbin/nologin cti
```

### Adim 3: Proje dosyalarini al

```bash
# Repo'yu gecici klasore klonla
cd /tmp
git clone <REPO_URL> cti-project-repo

# Proje klasorunu olustur
sudo mkdir -p /opt/cti-project/logs

# Gerekli dosyalari kopyala
sudo cp /tmp/cti-project-repo/cti_automation.py /opt/cti-project/
sudo cp /tmp/cti-project-repo/requirements.txt  /opt/cti-project/
sudo cp /tmp/cti-project-repo/.env.example       /opt/cti-project/.env

# Gecici klasoru temizle
rm -rf /tmp/cti-project-repo
```

### Adim 4: .env dosyasini duzenle

```bash
sudo nano /opt/cti-project/.env
```

Asagidaki alanlari kendi bilgilerinizle doldurun:

```
GEMINI_API_KEY=AIzaSy...              # Google AI Studio'dan alin
SMTP_SERVER=smtp.gmail.com
SMTP_PORT=587
SMTP_USERNAME=sizin-adres@gmail.com
SMTP_PASSWORD=xxxx xxxx xxxx xxxx     # Gmail App Password (16 haneli)
EMAIL_FROM=sizin-adres@gmail.com
EMAIL_TO=alici-adres@gmail.com
```

**Gmail App Password nasil alinir:**
1. https://myaccount.google.com/security adresine gidin
2. "2 Adimli Dogrulama" aktif olmali
3. "Uygulama sifreleri" bolumune gidin
4. "Posta" uygulamasi icin yeni sifre olusturun
5. 16 haneli sifreyi `SMTP_PASSWORD` alanina yapistin

### Adim 5: Python sanal ortamini olustur

```bash
sudo python3 -m venv /opt/cti-project/venv
sudo /opt/cti-project/venv/bin/pip install --upgrade pip
sudo /opt/cti-project/venv/bin/pip install -r /opt/cti-project/requirements.txt
```

### Adim 6: Dosya izinlerini ayarla

```bash
# Tum dosyalarin sahibi cti kullanicisi olsun
sudo chown -R cti:cti /opt/cti-project

# .env sadece cti kullanicisi okuyabilsin (hassas bilgiler iceriyor)
sudo chmod 600 /opt/cti-project/.env

# Script sadece cti kullanicisi calistirabilsin
sudo chmod 700 /opt/cti-project/cti_automation.py

# Log klasoru
sudo chmod 750 /opt/cti-project/logs
```

### Adim 7: Manuel test

```bash
sudo -u cti /opt/cti-project/venv/bin/python3 /opt/cti-project/cti_automation.py
```

Basarili cikti ornegi:
```
CTI News Feed Automation — started
Fetching 44 RSS feeds...
  CISA Advisories: 30 articles
  Cisco PSIRT Advisories: 50 articles
  ...
Total articles fetched: 6535
Articles from last 24h: 283
Articles matching inventory: 28
Sending 15 articles to Gemini for analysis...
Email sent to alici-adres@gmail.com
Threat briefing sent successfully.
CTI News Feed Automation — finished
```

### Adim 8: Systemd service dosyasini kur

```bash
sudo cp /tmp/cti-project-repo/linux-ubuntu/cti-newsfeed.service /etc/systemd/system/
sudo cp /tmp/cti-project-repo/linux-ubuntu/cti-newsfeed.timer   /etc/systemd/system/
sudo chmod 644 /etc/systemd/system/cti-newsfeed.service
sudo chmod 644 /etc/systemd/system/cti-newsfeed.timer
```

Eger repo'yu zaten temizlediyseniz, dosyalari elle olusturabilirsiniz:

**Service dosyasi:**

```bash
sudo nano /etc/systemd/system/cti-newsfeed.service
```

```ini
[Unit]
Description=CTI News Feed Automation — Threat Briefing
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=/opt/cti-project
ExecStart=/opt/cti-project/venv/bin/python3 /opt/cti-project/cti_automation.py
User=cti
Group=cti
Restart=on-failure
RestartSec=300
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/opt/cti-project/logs
PrivateTmp=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictSUIDSGID=true
Environment=PATH=/usr/local/bin:/usr/bin:/bin

[Install]
WantedBy=multi-user.target
```

**Timer dosyasi:**

```bash
sudo nano /etc/systemd/system/cti-newsfeed.timer
```

```ini
[Unit]
Description=CTI News Feed — Gunluk zamanlayici (09:00)

[Timer]
OnCalendar=*-*-* 09:00:00
Persistent=true
AccuracySec=60

[Install]
WantedBy=timers.target
```

### Adim 9: Systemd'yi yukle ve etkinlestir

```bash
# Systemd yapilandirmasini yeniden yukle
sudo systemctl daemon-reload

# Timer'i etkinlestir (yeniden baslatmalarda da aktif kalir)
sudo systemctl enable cti-newsfeed.timer

# Timer'i baslat
sudo systemctl start cti-newsfeed.timer

# Dogrulayin
systemctl list-timers | grep cti
```

---

## Gunluk Yonetim Komutlari

```bash
# ── Durum kontrolu ──────────────────────────────────
sudo systemctl status cti-newsfeed.timer       # Timer aktif mi?
sudo systemctl status cti-newsfeed.service     # Son calistirma nasil bitti?
systemctl list-timers | grep cti               # Sonraki calistirma ne zaman?

# ── Hemen calistir (timer'i beklemeden) ─────────────
sudo systemctl start cti-newsfeed.service

# ── Log takibi ──────────────────────────────────────
sudo journalctl -u cti-newsfeed.service -f              # Canli takip
sudo journalctl -u cti-newsfeed.service --since today   # Bugunun loglari
sudo journalctl -u cti-newsfeed.service --since "1 hour ago"
cat /opt/cti-project/logs/cti_automation.log            # Uygulama logu

# ── Durdur / Devre disi birak ───────────────────────
sudo systemctl stop cti-newsfeed.timer         # Timer'i durdur
sudo systemctl disable cti-newsfeed.timer      # Yeniden baslatmada calismasin

# ── Tekrar etkinlestir ──────────────────────────────
sudo systemctl enable cti-newsfeed.timer
sudo systemctl start cti-newsfeed.timer
```

---

## Calisma zamanlni degistirme

Timer'in calisma saatini degistirmek icin:

```bash
sudo nano /etc/systemd/system/cti-newsfeed.timer
```

`OnCalendar` satirini duzenleyin:

```ini
# Her gun saat 08:30
OnCalendar=*-*-* 08:30:00

# Haftaici saat 09:00 (Pazartesi-Cuma)
OnCalendar=Mon..Fri *-*-* 09:00:00

# Her 12 saatte bir (09:00 ve 21:00)
OnCalendar=*-*-* 09,21:00:00
```

Degisikligi uygulayin:

```bash
sudo systemctl daemon-reload
sudo systemctl restart cti-newsfeed.timer
systemctl list-timers | grep cti    # Yeni zamani dogrulayin
```

---

## Sorun giderme

### Script hata veriyor

```bash
# Loglari kontrol et
sudo journalctl -u cti-newsfeed.service --no-pager -n 50

# Manuel calistir ve ciktiyi gor
sudo -u cti /opt/cti-project/venv/bin/python3 /opt/cti-project/cti_automation.py
```

### "GEMINI_API_KEY not set" hatasi

.env dosyasinin dogru konumda ve okunabilir oldugundan emin olun:

```bash
sudo -u cti cat /opt/cti-project/.env
# Eger "Permission denied" aliyorsaniz:
sudo chown cti:cti /opt/cti-project/.env
sudo chmod 600 /opt/cti-project/.env
```

### Mail gonderilemiyor

1. Gmail'de 2FA aktif mi kontrol edin
2. App Password'un dogru oldugunu kontrol edin
3. "Daha az guvenli uygulamalara erisim" gerekmez — App Password kullaniliyor

```bash
# SMTP baglantisini test edin
python3 -c "
import smtplib, ssl
ctx = ssl.create_default_context()
with smtplib.SMTP('smtp.gmail.com', 587) as s:
    s.starttls(context=ctx)
    s.login('GMAIL_ADRESINIZ', 'APP_PASSWORD')
    print('Baglanti basarili!')
"
```

### Timer calismiyor

```bash
# Timer durumunu kontrol et
systemctl list-timers --all | grep cti

# Timer aktif degil mi?
sudo systemctl enable cti-newsfeed.timer
sudo systemctl start cti-newsfeed.timer

# Systemd yapilandirmasini yenile
sudo systemctl daemon-reload
```

---

## Guncelleme

Yeni bir versiyon geldiginde:

```bash
cd /tmp
git clone <REPO_URL> cti-update
sudo cp /tmp/cti-update/cti_automation.py /opt/cti-project/
sudo cp /tmp/cti-update/requirements.txt  /opt/cti-project/
sudo chown cti:cti /opt/cti-project/cti_automation.py
sudo chmod 700 /opt/cti-project/cti_automation.py

# Bagimliliklari guncelle
sudo /opt/cti-project/venv/bin/pip install -r /opt/cti-project/requirements.txt

# Systemd dosyalari degistiyse
sudo cp /tmp/cti-update/linux-ubuntu/cti-newsfeed.service /etc/systemd/system/
sudo cp /tmp/cti-update/linux-ubuntu/cti-newsfeed.timer   /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl restart cti-newsfeed.timer

# Temizle
rm -rf /tmp/cti-update
```

---

## Kaldirma

Projeyi tamamen kaldirmak icin:

```bash
# Timer ve service'i durdur
sudo systemctl stop cti-newsfeed.timer
sudo systemctl disable cti-newsfeed.timer
sudo systemctl stop cti-newsfeed.service

# Systemd dosyalarini sil
sudo rm /etc/systemd/system/cti-newsfeed.service
sudo rm /etc/systemd/system/cti-newsfeed.timer
sudo systemctl daemon-reload

# Proje dosyalarini sil
sudo rm -rf /opt/cti-project

# Kullaniciyi sil
sudo userdel cti
```
