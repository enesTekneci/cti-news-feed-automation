# CTI Tehdit Brifingi — Terim Sözlüğü

## Gönderildi

Bir günün brifinginin **Gönderildi** sayılması, o güne ait `cti-briefing.yml`
workflow'unun `DRY_RUN` olmadan (yani gerçek bir e-posta göndererek)
başarıyla tamamlanmasıdır.

Bu terim, `.github/workflows/cti-briefing.yml`'deki "dedup" (aynı gün iki
kez göndermeyi önleme) mantığının dayandığı temel varsayımdır: bir
çalıştırmanın `success` ile bitmesi, yalnızca `cti_automation.py`'nin
gerçekten `Email sent` satırına ulaştığı anlamına gelir — kod, mail
göndermeden başarıyla bitecek şekilde TASARLANMAMIŞTIR. Dolayısıyla
"bugün zaten Gönderildi mi?" sorusunun cevabı, o günün workflow
çalıştırma geçmişine (job başarı/başarısızlık durumu) bakılarak
güvenle çıkarılabilir; ayrıca bir "mail gönderildi" bayrağı tutmaya
gerek yoktur.

**Sınır durum:** `workflow_dispatch` ile `dry_run=true` çalıştırılan bir
görev de `success` ile biter ama **Gönderildi** SAYILMAZ (mail atılmadı).
Dedup mantığı bunu, çalıştırmanın "Dry run onizlemesini yukle" adımını
başarıyla çalıştırıp çalıştırmadığına bakarak ayırt eder (bu adım sadece
`dry_run=true` iken çalışır).
