# google-flow-mcp

Google Flow için gelişmiş bir MCP sunucusu. Kendi Google hesabınla Nano Banana resimleri ve Omni Flash / Veo 3.1 videoları üretir. Streamable HTTP ile çalışır ve [Obot](https://obot.ai) üzerinde kapsayıcı olarak yayına alınmak için tasarlandı. Claude Code, Claude Desktop, Cursor ve stdio isteyen diğer istemcilerle de çalışır.

> Resmî olmayan bir araçtır, Google ile bağlantısı yoktur. Flow'un herkese açık bir API'si yok. Bu sunucu senin oturumunla gerçek bir Chrome sekmesini kullanır. Google'ın şartları geçerlidir ve hesabın risk altında olabilir. Kendi hesabında ve sorumluluğunda kullan.

## Neler yapıyor

- **Resim (ücretsiz):** metinden resim, resimden resim, resim düzenleme, 1 ile 4 arası varyant, tek çağrıda 4'e kadar farklı prompt, tohum (seed), 5 en-boy oranı. Nano Banana Pro, 2.1 ve Lite.
- **Karakter tutarlılığı:** önceki resimleri `reference_images` olarak vererek aynı kişiyi, ürünü ya da stili koru.
- **Video (kredi harcar):** Omni Flash ile metinden video, ilk kareden video, ilk ve son kare arası video, referans resimlerden video (4, 6, 8, 10 saniye, 360p/720p). Veo 3.1 ile ilk kareden video.
- **Storyboard:** her çekim için tutarlı bir kare üretir, istersen her kareyi canlandırır.
- **Büyütme:** resimleri 2K veya 4K'ya büyütür.
- **Arka plan işleri:** uzun videolar `job_id` döner, `flow_wait` ile sonucu alırsın. İstemci zaman aşımına takılmazsın.
- **Kredi koruması:** video ancak `confirm=true` ile gider. `FLOW_BUDGET_CREDITS` kesin bir tavandır ve aşılacaksa istek hiç gönderilmez. `flow_estimate_video_cost` ücretsizdir.
- **Esnek resim girişi:** Flow medya kimliği, https URL, data URI ya da base64. Gerekirse önce yüklenir.
- **Uzak çalışmaya hazır:** Docker imajı, Obot katalog dosyası, `/healthz`, isteğe bağlı Bearer token, SSRF koruması.

Diğer projelerle ayrıntılı karşılaştırma: [docs/COMPARISON.md](docs/COMPARISON.md).

## Nasıl çalışıyor

```
MCP istemcisi ──Streamable HTTP──> google-flow-mcp ──page.evaluate──> Chrome (Xvfb, gerçek oturum) ──> flow.google.com
                                     │                                   │
                                     ├─ iş kuyruğu ve kredi bütçesi       └─ reCAPTCHA token'ı sayfada üretilir,
                                     └─ batchexecute zarfları                 aynı anda batchexecute isteği gönderilir
```

Eylül 2026'dan beri Flow `flow.google.com` adresinde ve her çağrı tek bir `batchexecute` uç noktasına gidiyor. Üretim çağrıları yalnız gerçek Flow sayfasında üretilebilen, tek kullanımlık bir reCAPTCHA Enterprise token'ı istiyor. Google `--headless` Chrome'u bot sayıyor. Bu yüzden imaj, Xvfb sanal ekranında "başlı" çalışan gerçek Google Chrome kullanıyor. Oturum kalıcı bir profil yerine dışa aktarılmış çerez dosyasından geliyor. Böylece Obot'un geçici podlarında da çalışıyor.

Protokol katmanı [crisng95/flowkit](https://github.com/crisng95/flowkit) ve [alarconcesar/flow-mcp](https://github.com/alarconcesar/flow-mcp) projelerinin MIT lisanslı çalışmasından türetildi.

## Hızlı başlangıç

### 1. Çerezleri dışa aktar

```bash
pipx install git+https://github.com/UmutKDev/google-flow-mcp
google-flow-mcp login
```

Açılan Chrome'da Google'a ve flow.google.com'a giriş yap. Komut `~/.google-flow-mcp/cookies.json` dosyasını yazar. Cookie-Editor JSON'u ve `cookies.txt` de kabul edilir.

### 2a. Obot

Adım adım anlatım: [docs/OBOT.md](docs/OBOT.md). Kısaca: bu repoyu Obot'a katalog kaynağı olarak ekle, `obot/google-flow-mcp.yaml` girdisi oluşur. Çerez dosyasını base64'e çevirip (`base64 -w0 cookies.json`) `FLOW_COOKIES` alanına yapıştır.

### 2b. Docker

```bash
docker run --rm -p 8080:8080 \
  -v ~/.google-flow-mcp/cookies.json:/secrets/cookies.json:ro \
  -e FLOW_COOKIES_FILE=/secrets/cookies.json \
  -e FLOW_MCP_AUTH_TOKEN=uzun-rastgele-bir-deger \
  ghcr.io/umutkdev/google-flow-mcp:latest

claude mcp add --transport http google-flow http://localhost:8080/mcp \
  --header "Authorization: Bearer uzun-rastgele-bir-deger"
```

### 2c. Yerel stdio

```bash
claude mcp add google-flow --env FLOW_COOKIES_FILE="$HOME/.google-flow-mcp/cookies.json" \
  -- google-flow-mcp serve --transport stdio
```

Yerelde Chrome penceresi görünür. Görmek istemiyorsan Linux'ta `xvfb-run` ile başlat.

## Araçlar

| Araç | Ne yapar | Kredi |
| --- | --- | --- |
| `flow_status` | Oturum, hesap, varsayılan proje, bütçe ve işler | – |
| `flow_list_models` | Modeller, oranlar, süreler ve tahmini maliyetler | – |
| `flow_create_project`, `flow_use_project` | Proje aç ya da varsayılanı seç | – |
| `flow_list_media`, `flow_get_media` | Projedeki medyayı listele, süresi dolan URL'leri yenile | – |
| `flow_upload_image` | Resmi projeye yükle, medya kimliği al | – |
| `flow_generate_image` | Nano Banana ile resim üret, düzenle, referansla tutarlı tut | Ücretsiz |
| `flow_upscale_image` | 2K/4K büyütme | Ücretsiz |
| `flow_estimate_video_cost` | Videonun tahmini maliyeti ve kalan bütçe | – |
| `flow_generate_video` | Omni/Veo video, `confirm=true` gerekir | Harcar |
| `flow_storyboard` | Çok çekimli tutarlı kareler, isteğe bağlı canlandırma | Canlandırırsa harcar |
| `flow_wait`, `flow_job_status`, `flow_cancel_job` | Arka plan işlerini yönet | – |
| `flow_screenshot`, `flow_restart_browser` | Hata ayıklama | – |

En az kredi harcayan akış: önce ücretsiz resimle kareyi oturt, sonra yalnız onaylanan kareyi `mode="frame"` ile canlandır.

## Yapılandırma

| Değişken | Varsayılan | Açıklama |
| --- | --- | --- |
| `FLOW_COOKIES_FILE` | – | Bağlanan çerez dosyasının yolu (Docker birimi, Kubernetes Secret) |
| `FLOW_COOKIES` | – | Aynı içerik metin olarak: base64 (önerilen), JSON ya da cookies.txt. Obot için bunu kullan |
| `FLOW_PROFILE_DIR` | – | Kalıcı Chrome profili (Docker birimi). Verilirse çerezlere gerek kalmaz |
| `FLOW_PROJECT_ID` | – | Varsayılan Flow projesi |
| `FLOW_BUDGET_CREDITS` | `200` | Videoya harcanabilecek en fazla kredi. `0` videoyu kapatır |
| `FLOW_CONFIRM_THRESHOLD` | `1` | Bu tahminin üstündeki isteklerde `confirm=true` zorunlu |
| `FLOW_DEFAULT_IMAGE_MODEL` | `NANO_BANANA_PRO` | Varsayılan resim modeli |
| `FLOW_MIN_SUBMIT_INTERVAL` | `4` | İki ücretli istek arasındaki en kısa süre (saniye) |
| `FLOW_SYNC_WAIT` | `40` | Bir aracın iş kimliği dönmeden önce bekleyeceği süre |
| `FLOW_JOB_TIMEOUT` | `900` | Bir videonun hazır olması için en uzun bekleme |
| `FLOW_EXPECTED_ACCOUNT` | – | Beklenen Google e-postası |
| `FLOW_BROWSER_PROXY` | – | Chrome için vekil sunucu (ör. `http://host:port`) |
| `FLOW_INPUT_DIRS` | – | `flow_upload_image`'ın dosya okuyabileceği ek dizinler |
| `FLOW_OUTPUT_DIR` | `/tmp/google-flow-mcp` | Büyütülmüş dosyalar, bütçe defteri, durum |
| `FLOW_MCP_TRANSPORT` | `streamable-http` | `stdio` da olabilir |
| `PORT` / `FLOW_MCP_PATH` | `8080` / `/mcp` | HTTP portu ve yolu |
| `FLOW_MCP_AUTH_TOKEN` | – | Verilirse her istekte `Authorization: Bearer` aranır |
| `FLOW_HEADLESS` | `0` | `1` başsız Chrome kullanır (reCAPTCHA'da reddedilebilir) |

## Doğrulama durumu

- 34 otomatik test geçiyor. Bunlardan biri gerçek Chrome'u sahte bir `flow.google.com` ile sürüyor ve reCAPTCHA atlatmasını, yüklemeyi, resim üretimini, metinden ve kareden videoyu, yoklamayı, büyütmeyi, bütçe tavanını ve MCP araçlarını uçtan uca sınıyor.
- Sunucu Xvfb ve gerçek Chrome ile Streamable HTTP üzerinden çalıştırıldı, Bearer token denetimi ve `/healthz` denendi.
- **Canlı Flow'a karşı henüz denenmedi.** İstek şekilleri, Ekim–Kasım 2026'da canlı doğrulanmış iki projeden alındı ama Google bunları her an değiştirebilir. İlk kullanımda ücretsiz araçlarla başla: `flow_status`, `flow_generate_image`, sonra 4 saniyelik 360p bir Omni videosu.
- Kredi maliyetleri topluluk ölçümlerine dayalı, yukarı yuvarlanmış tahminlerdir. Kesin rakam Flow'un kendi gösterdiğidir.

## Geliştirme

```bash
pip install -e ".[dev]"
python -m playwright install chromium
pytest -q
ruff check src tests
python scripts/smoke_http.py http://localhost:8080/mcp --token ...
```

## Lisans

MIT. Türetilen kodun lisansları `LICENSES/` altında.
