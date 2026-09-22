#!/usr/bin/env python3
"""Tüm test dosyalarını çalıştır, özet ver, uygun exit code ile çık.

Kullanım:
    python3 tests/calistir.py            # ağ gerektirenler dahil hepsi
    python3 tests/calistir.py --agsiz    # ağ gerektirenleri atla (CI için)

Her test dosyası AYRI bir süreçte çalışır: biri çökerse ötekileri
etkilemez ve modül seviyesindeki sahte ortam/monkeypatch'ler birbirine
sızmaz.
"""
import subprocess
import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent

# Gerçek ağ isteği yapan testler — CI'da atlanabilsin diye işaretli.
# (Ağ kesintisi ya da kaynak sitenin geçici hatası yüzünden yeşil bir
# değişikliğin kırmızı görünmesini istemiyoruz.)
AG_GEREKTIREN = {"test_extract.py"}


def main() -> int:
    agsiz = "--agsiz" in sys.argv
    dosyalar = sorted(p for p in TESTS_DIR.glob("test_*.py"))
    if agsiz:
        dosyalar = [p for p in dosyalar if p.name not in AG_GEREKTIREN]

    if not dosyalar:
        print("Çalıştırılacak test dosyası bulunamadı.")
        return 1

    basarisiz: list[str] = []
    for dosya in dosyalar:
        # flush: alt sürecin çıktısıyla karışmasın (başlık testlerden sonra
        # basılıyormuş gibi görünüyordu)
        print(f"\n{'=' * 60}\n{dosya.name}\n{'=' * 60}", flush=True)
        sonuc = subprocess.run([sys.executable, str(dosya)], cwd=TESTS_DIR.parent)
        if sonuc.returncode != 0:
            basarisiz.append(dosya.name)

    print(f"\n{'=' * 60}")
    print(f"ÖZET: {len(dosyalar) - len(basarisiz)}/{len(dosyalar)} dosya geçti")
    if agsiz:
        print(f"(--agsiz: {', '.join(sorted(AG_GEREKTIREN))} atlandı)")
    if basarisiz:
        print("BAŞARISIZ: " + ", ".join(basarisiz))
    print("=" * 60)
    return 1 if basarisiz else 0


if __name__ == "__main__":
    sys.exit(main())
