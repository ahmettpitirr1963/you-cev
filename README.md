# You-Cev — YouTube → Türkçe seslendirme + altyazı

İngilizce YouTube videolarını (veya kendi video dosyalarınızı) **eş zamanlı Türkçe seslendirme ve Türkçe altyazıyla** izlemenizi sağlar. Tamamen ücretsizdir. Ağır işler bilgisayarınızda (GPU) yapılır.

Sistem videonun **önünden giderek** çalışır: Sesi önceden indirir, gerçek zamandan çok daha hızlı işler ve her Türkçe cümleyi orijinal cümlenin başladığı anda çalar. Çeviri yetişmezse video birkaç saniye bekletilir, böylece hiçbir bölüm kaçmaz.

## Kurulum

1. **Sunucu:** `start.bat` dosyasına çift tıklayın. İlk çalıştırmada Python paketleri kurulur (~3 GB, bir kerelik). Modeller (`cache/models/`, ~3 GB) ilk açılışta arka planda iner; yarıda kalırsa kaldığı yerden devam eder.
2. **Chrome eklentisi:** `chrome://extensions` → sağ üstten *Geliştirici modu* açın → *Paketlenmemiş öğe yükle* → `extension` klasörünü seçin.
3. YouTube'da bir video açın. Türkçe ses ve altyazı otomatik başlar. Eklenti simgesindeki rozet hazırlık durumunu gösterir (✓ = tamamı hazır). Ayarlar için simgeye tıklayın; pencerenin açık kalması gerekmez.

**Web oynatıcı (eklentisiz):** http://127.0.0.1:8765 adresine link yapıştırın veya video dosyası yükleyin. Başka sitelerde oynatılması kapalı videolar için yerel kopya (360p) otomatik indirilir.

**Komut satırı testi:** `cd backend` ve `..\.venv\Scripts\python cli.py <link>` — hız raporu verir ve SRT dosyalarını yazar.

**Oynatıcı testleri:** `node tests\player-test.js` ve `node tests\seek-test.js`.

## Nasıl çalışır

```
yt-dlp (ses / elle yüklenmiş altyazı) → faster-whisper large-v3-turbo (GPU)
  → cümle birleştirme → çeviri → seslendirme → süreye sığdırma → önbellek → WebSocket → senkron oynatıcı
```

- Video 20 sn + 60'ar sn'lik parçalara, sessiz noktalardan bölünür. İleri sardığınızda o bölge hemen öne alınır.
- Türkçe konuşma sırasında orijinal ses kısılır (oranı ayarlanabilir).
- Aynı video ikinci kez anında açılır (`cache/` klasörü).

## Kesintisizlik: yedekler

Ücretsiz internet servisleri zaman zaman kısıtlama yapar veya tek bir sesi bozuk bırakır. Program bunu algılayınca o servise yüklenmeyi bırakır ve sıradaki yedeğe geçer:

| Adım | 1. tercih | Yedekler |
|---|---|---|
| Çeviri | Google (gtx) | Google (dict uç noktaları) → **yerel NLLB-200** (GPU, internetsiz) |
| Seslendirme | Seçilen Microsoft sesi | Diğer Türkçe Microsoft sesi → **yerel Piper** (internetsiz) |

- Çevrilemeyen bir parça boş kalmaz: artan aralıklarla kendiliğinden yeniden denenir.
- Ayarlardan çeviri motoru "Yerel (internetsiz)" seçilirse hiç internet servisi kullanılmaz.

## Daha iyi çeviri (isteğe bağlı): Ollama

https://ollama.com adresinden Ollama'yı kurup `ollama pull gemma3:4b-it-qat` çalıştırın. Çeviri motoru "Otomatik" iken Ollama varsa kendiliğinden kullanılır (bağlamlı, seslendirmeye uygun kısa çeviri).

## Sınırlar

- Canlı yayınlar desteklenmez.
- YouTube değişiklikleri yt-dlp'yi bozabilir. `start.bat` her açılışta yt-dlp'yi günceller. İndirme, token gerektirmeyen istemcileri sırayla dener (`pipeline/downloader.py` → `DOWNLOAD_CLIENTS`).
- edge-tts'te 4'ten fazla eşzamanlı istek Microsoft tarafından reddediliyor (`config.yaml` → `tts.concurrency`).
