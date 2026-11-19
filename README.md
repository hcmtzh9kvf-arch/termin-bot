# Ausländeramt Aachen – Termin Botu

GitHub'da (ücretsiz) her 5 dakikada bir çalışır, `termine.staedteregion-aachen.de` sitesinde
**Infostelle → Beratungs- und Antragsservice** için en erken boş randevuyu kontrol eder.
`ALERT_BEFORE` tarihinden (varsayılan **03.03.2027**) önce bir randevu çıkarsa telefonuna
**ntfy** ile anlık bildirim gönderir. Randevuyu **almaz** – sadece haber verir, rezervasyonu sen yaparsın.

## Kurulum (iPad'den ~10 dakika)

1. **ntfy uygulaması:** App Store'dan *ntfy* indir → **+** → topic adı: `efe-termin-h0qyny6dv5`
   (sunucu: `ntfy.sh`) → Subscribe. Bildirim izni ver.
2. **GitHub repo:** github.com → **New repository** → ad: `termin-bot` → **Public** seç
   (public = sınırsız ücretsiz çalışma süresi) → *Create repository*.
3. **Dosyaları yükle:** Repo sayfasında *uploading an existing file* → zip'ten çıkan 4 dosyayı seç
   (`termin_bot.py`, `requirements.txt`, `README.md`, `termin.yml`) → **Commit changes**.
4. **Workflow'u doğru klasöre taşı:** Repoda `termin.yml`'e tıkla → ✏️ (Edit) → dosya adı kutusunda
   adın başına `.github/workflows/` yaz (yani `.github/workflows/termin.yml` olsun) → **Commit changes**.
5. **Bildirim adresini gizli olarak ekle:** **Settings → Secrets and variables → Actions →
   New repository secret** → Name: `NTFY_TOPIC`, Secret: `efe-termin-h0qyny6dv5` → *Add secret*.
6. **Test:** **Actions** sekmesi → (sorarsa *enable*) → *Auslaenderamt Termin Bot* → **Run workflow** →
   mode: `test-notify` → yeşil olunca telefona "✅ Test" bildirimi gelmeli.
7. **İlk gerçek kontrol:** Yine **Run workflow** → mode: `check`. Çalışmaya tıklayıp log'da
   `frühester Termin = 03.03.2027 11:40` gibi bir satır görmelisin. Bundan sonra kendiliğinden
   5 dakikada bir çalışır (GitHub bazen 5–15 dk geciktirir).

## Ayarlar (`.github/workflows/termin.yml` içinde, ✏️ ile düzenle)

| Ayar | Ne işe yarar |
|---|---|
| `ALERT_BEFORE` | Bu tarihten önce randevu çıkınca bildirim gelir (`GG.AA.YYYY`). |
| `TARGETS` | İzlenen Anliegen'ler, satır başına `Kategori \| Anliegen`. Anliegen yerine sıra no yazılabilir (`RWTH \| 1` = RWTH bölümündeki 1. seçenek). `#` ile başlayan satır kapalıdır. |
| `HEARTBEAT_HOUR` | Her gün bu saatte "🤖 bot çalışıyor, şu an en erken: …" bildirimi. Boş = kapalı. |

Kategori/Anliegen adlarını görmek için: **Run workflow → mode: `list`** → log'da tüm liste çıkar.

## Bildirimler
- 🎉 **Daha erken randevu var!** – yüksek öncelik. Bildirime dokununca site açılır. Aynı randevu için tekrar etmez; kaybolup yeniden çıkarsa tekrar haber verir.
- ⚠️ **Termin botu hata veriyor** – 3 kontrol üst üste başarısız olursa bir kez. Mesajda hata yazar (site yapısı değiştiyse mevcut kategori/Anliegen listesi de görünür).
- 🤖 Günlük "çalışıyor" mesajı (sessiz).

## Notlar
- Site GitHub sunucularını engellerse (hata bildirimi `403` derse) botu kendi bilgisayarında çalıştırabilirsin:
  `pip install -r requirements.txt` → `NTFY_TOPIC=efe-termin-h0qyny6dv5 python termin_bot.py --loop 300`
- Telegram istersen `TG_TOKEN` ve `TG_CHAT_ID` secret'larını da ekle; ikisi birlikte çalışır.
- GitHub, 60 gün hiç commit olmayan public repolarda zamanlanmış görevleri durdurur. Bot her gün
  `state.json`'ı güncelleyip commit ettiği için bu olmamalı; olursa GitHub e-posta atar, Actions'ta
  *Enable workflow* demen yeterli.
