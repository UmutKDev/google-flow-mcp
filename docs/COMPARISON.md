# Mevcut Google Flow MCP projelerinin karşılaştırması

8 Kasım 2026'da GitHub'da "google flow mcp", "flow-mcp veo", "labs.google flow" ve benzeri aramalarla 30'dan fazla repo bulundu. Bunlardan aktif olan ve farklı bir yaklaşım taşıyan 14 tanesini klonlayıp kodlarını okudum. Bu belge o incelemenin özeti ve bu sunucunun hangi fikri nereden aldığını gösteriyor.

## Özet

Hiçbir proje Obot gibi uzak bir platformda çalışmaya uygun değil. Hepsi stdio ile çalışıyor ve kullanıcının kendi bilgisayarındaki bir Chrome'a bağlanıyor. Ayrıca Google, Eylül 2026'da Flow'u `labs.google/fx/tools/flow` adresinden `flow.google.com` adresine taşıdı ve iç API'yi değiştirdi. Bu yüzden eski REST (`aisandbox-pa.googleapis.com` + `Bearer ya29…`) yaklaşımını kullanan projelerin çoğu artık çalışmıyor ya da yalnız arayüze tıklayarak çalışıyor.

Yeni API'yi doğrudan konuşan iki güvenilir kaynak var: **crisng95/flowkit** ve **alarconcesar/flow-mcp**. İkisi de her çağrıyı Flow sekmesinin içinde `batchexecute` RPC'si olarak çalıştırıyor ve reCAPTCHA token'ını sayfada üretiyor. Bu sunucunun protokol katmanı bu iki projeden türetildi (ikisi de MIT lisanslı, lisansları `LICENSES/` altında).

## Proje proje

| Proje | ★ | Dil | Yaklaşım | Güçlü yanı | Eksiği |
| --- | --- | --- | --- | --- | --- |
| [crisng95/flowkit](https://github.com/crisng95/flowkit) | 1133 | Python + Chrome eklentisi | Eklenti sekmede batchexecute çalıştırıyor | Yeni API'nin en eksiksiz haritası: resim, Omni t2v/i2v/ilk-son kare/referans, yükleme, 2K/4K büyütme. Karakter tutarlılığı, sahne zinciri, 36 skill | MCP sunucusu değil, Chrome eklentisi ve yerel FastAPI gerektiriyor. Veo referans ve video büyütme yeni API'de yok |
| [GTAI-1/flow-mcp](https://github.com/GTAI-1/flow-mcp) | 542 | TypeScript | CDP ile gerçek düğmelere tıklıyor | En popüler. Uzun çağrılar için iş kimliği ve `flow_wait`, karakterler, FFmpeg ile birleştirme, seslendirme, altyazı, yerel kontrol paneli | Ekranı açık bir Mac/Windows gerektiriyor, sunucuya kurulamaz. Arayüz değişince kırılıyor |
| [ffroliva/gflow-cli](https://github.com/ffroliva/gflow-cli) | 367 | Python | Playwright ile arayüz otomasyonu + yeni API gözlemi | En olgun test ve doğrulama süreci. Uzatma (extend), video büyütme, karakter, "Agent Mode" talimatları, S3/GCS çıktı, Docker denemesi | Çok büyük (240 bin satır). Kapsayıcıda giriş ve üretim hiç doğrulanmamış |
| [kodelyx/flow-agent](https://github.com/kodelyx/flow-agent) | 259 | Python | CLI + OpenAI uyumlu REST + MCP | OpenAI uyumlu API fikri | Eski API'ye dayanıyor |
| [TMSSS05/google-flow-browser-mcp](https://github.com/TMSSS05/google-flow-browser-mcp) | 124 | JavaScript | Playwright + CDP, arayüz tıklama | 17 araçlık ilk kapsamlı set (karakter, sahne, araç galerisi) | Seçicilere bağımlı, eski arayüz |
| [useapi/google-flow-api](https://github.com/useapi/google-flow-api) | 87 | JS/Python | Ücretli üçüncü taraf barındırılan API | Hazır uzak MCP uç noktası | Ücretli, Google oturumunu üçüncü tarafa veriyorsun |
| [miyakejima/google-flow-mcp](https://github.com/miyakejima/google-flow-mcp) | 21 | TypeScript | Arayüz otomasyonu | Hesabın canlı yeteneklerini okuyan `flow_inspect_account`, çoklu hesap, indirilen dosyayı doğrulama | Yerel ve stdio |
| [GabrielGargiuloDev/google-flow-mcp](https://github.com/GabrielGargiuloDev/google-flow-mcp) | 20 | JavaScript | TMSSS05'in yeni arayüze uyarlanmış hâli | Windows desteği, Claude Code skill'i, kredi maliyeti notları | Flow ajanına prompt yazarak üretiyor, model/süre uyumsuzsa sessizce takılıyor |
| [Mitanshp5/Google-Flow_MCP](https://github.com/Mitanshp5/Google-Flow_MCP) | 17 | JavaScript | CDP | 35 araç, senaryo (script) çalıştırma ve kaldığı yerden devam | Çok sayıda araç ama hepsi arayüz tıklaması |
| [roshanarnav25-sloth/google-flow-mcp](https://github.com/roshanarnav25-sloth/google-flow-mcp) | 15 | TypeScript | Önce iç API, olmazsa tıklama ("transport ladder") | **Kesin kredi bütçesi** (`FLOW_BUDGET_CEILING`), maliyeti ücretsiz sorgulama, harcama defteri | Kendi notuna göre canlı Flow'a karşı doğrulanmamış (v0.1) |
| [eddie-fqh/flow-py](https://github.com/eddie-fqh/flow-py) | 14 | Python | Okumalar HTTP, üretim arayüz + ağ yakalama | Uzatma, kamera hareketi, nesne ekleme/silme, ücretsiz 1080p/4K büyütme | Eski API |
| [RohaanA/google-flow-mcp](https://github.com/RohaanA/google-flow-mcp) | 12 | TypeScript | İç API | Flow ajanıyla sohbet, karakter portresi, sahne dışa aktarma, varsayılan ayarlar | Eski API |
| [ParkSangGwon/google-flow-mcp](https://github.com/ParkSangGwon/google-flow-mcp) | 9 | TypeScript | Arayüz | Scene Builder: klip ekleme, uzatma | Eski arayüz |
| [alarconcesar/flow-mcp](https://github.com/alarconcesar/flow-mcp) | 1 | Python | Yeni batchexecute, Playwright içinde | **Başsız (headless) sunucuda resim üretimi çalışıyor.** reCAPTCHA "zehirleme" atlatma, tarayıcı havuzu, 4'e kadar farklı prompt tek çağrıda, tohum (seed) | Yalnız resim; video yok |

## Herkesin takıldığı üç teknik engel

1. **reCAPTCHA Enterprise.** Her üretim çağrısı tek kullanımlık bir token istiyor ve bu token yalnız gerçek Flow sayfasında üretilebiliyor. Flow'un yeni paketi `grecaptcha.enterprise.execute` çağrısını `Object.assign` ile sarıp eylemi `extension_hijack_detected` yapıyor. alarconcesar bu atamayı çağrı süresince etkisiz hâle getirerek temiz token alıyor. Bu sunucu aynı tekniği kullanıyor.
2. **Başsız Chrome reddediliyor.** gflow-cli'nin ölçümüne göre Google'ın oturum ve reCAPTCHA katmanı `--headless` Chrome'u reddediyor. Xvfb üzerinde "başlı" (headed) çalışan gerçek Chrome ise temiz görünüyor. Bu sunucunun Docker imajı bu yüzden Xvfb ile gerçek Google Chrome çalıştırıyor.
3. **Uzun süren işler.** Çoğu MCP istemcisi bir araç çağrısını yaklaşık 60 saniyede kesiyor. Video 1 ile 5 dakika sürüyor. GTAI-1'in iş kimliği + `flow_wait` yaklaşımı bu sunucuya da alındı.

## Bu sunucuya ne alındı

| Fikir | Kaynak |
| --- | --- |
| batchexecute protokolü, RPC kimlikleri, istek şekilleri, yanıt okuyucular | flowkit, alarconcesar |
| reCAPTCHA zehirleme atlatma, sayfa içinde token üretip aynı anda gönderme | alarconcesar |
| Xvfb + gerçek Chrome, kalıcı oturumun kapsayıcı dışında tutulması | gflow-cli'nin kapsayıcı deneyi |
| İş kimliği ve `flow_wait` | GTAI-1 |
| Kesin kredi bütçesi, onaysız video yok, ücretsiz maliyet sorgusu | roshanarnav25-sloth |
| Referans resimlerle karakter tutarlılığı, çok çekimli storyboard | flowkit, GTAI-1 |
| Karşılaştırmalı model kataloğu ve bilinen maliyetler | GabrielGargiuloDev, flowkit |

## Bu sunucunun eklediği

- **Streamable HTTP** ile uzak çalışma ve Obot'un `containerized` çalışma zamanına uygun imaj (UID 1000, yetkisiz, kalıcı disk gerektirmiyor).
- Oturumun kalıcı Chrome profili yerine **dışa aktarılmış çerez dosyasıyla** taşınması. Obot bunu `file: true` gizli alanı olarak bağlıyor ve her kullanıcıya ayrı pod açıyor.
- Playwright `storage_state`, Cookie-Editor JSON'u, `cookies.txt` ve bunların base64 hâlini tanıyan çerez okuyucu. Google dışı çerezler atılıyor.
- Resim kaynağı olarak medya kimliği, https URL, data URI ya da base64 kabul eden tek tip giriş. Medya kimliği olmayan her şey önce Flow'a yükleniyor.
- Özel ağ adreslerine giden URL'leri reddeden SSRF koruması ve sunucudaki dosyalara yalnız izinli dizinlerden erişim.
- Gerçek Chrome'u sahte bir `flow.google.com` ile sürerek reCAPTCHA atlatmasını, RPC'leri, yoklamayı ve MCP araçlarını uçtan uca sınayan test.

## Bilinçli olarak dışarıda bırakılanlar

- **Veo ile metinden video, Veo referans videosu, video büyütme ve video uzatma.** flowkit'e göre bu isteklerin yeni API'deki şekli henüz yakalanmadı. gflow-cli uzatmayı yalnız arayüze tıklayarak yapıyor. Bunları tahminle göndermek kredi yakabileceği için eklemedim. Omni aynı işlerin çoğunu karşılıyor.
- **Arayüz tıklamaya dayalı araçlar** (araç galerisi, Scene Builder zaman çizelgesi). Seçiciler her arayüz güncellemesinde kırılıyor.
- **FFmpeg ile montaj ve seslendirme.** Sunucuyu büyütüyor ve Obot podunda gereksiz. İstenirse ayrı bir araç olarak eklenebilir.
