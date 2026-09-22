#!/usr/bin/env python3
"""Test dosyalarının ortak altyapısı: import yolu, sahte ortam, kontrol sayacı.

Testler repo İÇİNDE (tests/) tutulur ve repo köküne GÖRELİ import yapar —
daha önce geçici bir dizinde mutlak yollarla tutuluyorlardı ve 2026-09-22'de
o dizin temizlenince 197 test tek seferde kayboldu. Buradaki yol hesabı
sayesinde testler Mac kopyasında, Linux kopyasında ve GitHub Actions
checkout'unda aynı şekilde çalışır.

Gerçek secret GEREKTİRMEZ: envanter/alias/API anahtarı sahte değerlerle
doldurulur, böylece testler .env olmadan da (CI dahil) çalışır.
"""
import os
import sys
from pathlib import Path

REPO_KOKU = Path(__file__).resolve().parent.parent

# cti_automation.py import edilmeden ÖNCE ortam hazırlanmalı: modül seviyesinde
# _load_json_env çağrılıyor ve değişken yoksa (bilerek) RuntimeError fırlatıyor.
os.environ.setdefault("INVENTORY_JSON", '["Fortigate", "PHP", "Cisco", "NGINX"]')
os.environ.setdefault("VENDOR_ALIASES_JSON", "{}")
os.environ.setdefault("GEMINI_API_KEY", "test-anahtari")

if str(REPO_KOKU) not in sys.path:
    sys.path.insert(0, str(REPO_KOKU))

import cti_automation as cti  # noqa: E402,F401  (test dosyaları buradan alır)


class Sayac:
    """Basit test sayacı — pytest bağımlılığı eklememek için (proje ilkesi:
    yeni bağımlılık yok; testler de aynı kurala tabi)."""

    def __init__(self) -> None:
        self.gecen = 0
        self.kalan = 0

    def kontrol(self, ad: str, kosul, detay: str = "") -> None:
        if kosul:
            self.gecen += 1
            print(f"✓ {ad}")
        else:
            self.kalan += 1
            print(f"✗ {ad}" + (f"  → {detay}" if detay else ""))

    def bitir(self) -> None:
        """Özeti yazdır ve uygun exit code ile çık (CI bunu okur)."""
        durum = "BAŞARILI" if not self.kalan else "BAŞARISIZ"
        print(f"\n{durum}: {self.gecen} geçti, {self.kalan} hata")
        sys.exit(1 if self.kalan else 0)
