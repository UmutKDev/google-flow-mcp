# Obot'ta kurulum

Bu sunucu Obot'un **containerized** çalışma zamanında çalışır. Obot imajı bir pod olarak başlatır, `8080` portundaki `/mcp` uç noktasına Streamable HTTP ile bağlanır ve kullanıcıların kimliğini kendisi doğrular.

## 1. İmaj

`main` dalına yapılan her push, GitHub Actions ile `ghcr.io/umutkdev/google-flow-mcp:latest` imajını yayımlar. `v0.1.0` gibi bir etiket de `ghcr.io/umutkdev/google-flow-mcp:0.1.0` üretir.

GHCR paketleri ilk yayımlandığında özeldir. Obot imajı çekebilsin diye iki yoldan birini seç:

- GitHub'da **Packages → google-flow-mcp → Package settings → Change visibility → Public**.
- Ya da Obot'ta özel kayıt defteri için bir image pull secret tanımla (Obot belgelerinde "Image Pull Secrets").

Üretim için etiketi değil, sabit bir özet (digest) kullan: `ghcr.io/umutkdev/google-flow-mcp@sha256:...`.

## 2. Google oturumunu dışa aktar

Sunucu senin Google hesabınla çalışır ve şifreni hiç görmez. Oturumu bir çerez dosyası olarak verirsin.

En kolay yol kendi bilgisayarında:

```bash
pipx install git+https://github.com/UmutKDev/google-flow-mcp
google-flow-mcp login   # açılan Chrome'da Google'a ve flow.google.com'a giriş yap
```

Bilgisayarında Google Chrome kurulu olmalı. Komut `~/.google-flow-mcp/cookies.json` dosyasını yazar. Yalnız Google çerezlerini içerir.

Alternatif: Chrome'da Cookie-Editor eklentisiyle `flow.google.com` açıkken **Export → JSON** yap ve dosyaya kaydet. `cookies.txt` biçimi de kabul edilir.

Dosyayı kontrol etmek için:

```bash
google-flow-mcp check-cookies ~/.google-flow-mcp/cookies.json
```

Bu dosya şifre gibidir: sahibi senin adına Google'a girebilir. Repoya koyma, paylaşma.

## 3. Katalog girdisini ekle

### GitOps ile (önerilen)

Obot'ta **MCP Servers → Sources** altında bu repoyu katalog kaynağı olarak ekle. Obot `obot/google-flow-mcp.yaml` dosyasını okuyup "Google Flow" girdisini oluşturur.

### Elle

**MCP Servers → Add MCP Server → Hosted Server → Containerized** seç ve şunları gir:

| Alan | Değer |
| --- | --- |
| Image | `ghcr.io/umutkdev/google-flow-mcp:latest` |
| Port | `8080` |
| Path | `/mcp` |

Yapılandırma alanları:

| Anahtar | Tür | Not |
| --- | --- | --- |
| `FLOW_COOKIES_FILE` | Dosya, gerekli, gizli | 2. adımdaki çerez dosyası |
| `FLOW_PROJECT_ID` | İsteğe bağlı | Varsayılan Flow projesi. Boş kalırsa sunucu bir proje açar |
| `FLOW_BUDGET_CREDITS` | İsteğe bağlı | Videoya harcanabilecek en fazla kredi (varsayılan 200, 0 videoyu kapatır) |
| `FLOW_EXPECTED_ACCOUNT` | İsteğe bağlı | Beklenen Google e-postası |

Kaynak önerisi: istek 500m CPU ve 1Gi bellek, sınır 2 CPU ve 3Gi bellek. Chrome bellek sınırı düşük olursa sekme çöker.

## 4. vMCP oluştur ve bağlan

1. **vMCPs → Create vMCP** ile Google Flow girdisini sürükle.
2. `FLOW_COOKIES_FILE` alanı için **Provided at connection** seç. Böylece her kullanıcı kendi hesabıyla bağlanır ve Obot her kullanıcıya ayrı bir pod açar. Tek bir ortak hesap kullanacaksan **Preconfigured** seç.
3. Bağlan. İlk çağrı olarak `flow_status` çalıştır. `signed_in: true` ve hesabın e-postasını görmelisin.

## 5. Çıkış (egress) izinleri

Obot'ta çıkış denetimi açıksa podun şu alan adlarına erişmesi gerekir. Katalog dosyasında hazır tanımlı:

```
flow.google.com, *.google.com, *.gstatic.com, *.googleapis.com,
*.googleusercontent.com, flow-content.google, labs.google, www.recaptcha.net
```

`flow_upload_image` ile başka bir siteden resim URL'si vereceksen o alan adını da ekle. Ya da resmi data URI veya base64 olarak gönder.

## Sorun giderme

| Belirti | Neden ve çözüm |
| --- | --- |
| `Not signed in to Google Flow` | Çerezler eksik ya da süresi doldu. Yeni dosya dışa aktar, alanı güncelle, `flow_restart_browser` çağır. |
| `Chrome could not reach flow.google.com` | Çıkış izinleri eksik. 5. adımdaki listeyi kontrol et. |
| `UNUSUAL_ACTIVITY` | reCAPTCHA skoru düşük kaldı. Veri merkezi IP'leri daha düşük skor alabilir. Daha yavaş çalış (`FLOW_MIN_SUBMIT_INTERVAL`), gerekirse `FLOW_BROWSER_PROXY` ile konut tipi bir vekil sunucu kullan. |
| Pod yeniden başlıyor | Bellek sınırı düşük. Sınırı en az 2Gi yap. |
| `flow_screenshot` | Sekmenin o anki görüntüsü. Giriş ya da arayüz sorunlarında ilk bakılacak yer. |

## Obot dışında çalıştırma

```bash
docker run --rm -p 8080:8080 \
  -v ~/.google-flow-mcp/cookies.json:/secrets/cookies.json:ro \
  -e FLOW_COOKIES_FILE=/secrets/cookies.json \
  -e FLOW_MCP_AUTH_TOKEN=uzun-rastgele-bir-deger \
  ghcr.io/umutkdev/google-flow-mcp:latest
```

`FLOW_MCP_AUTH_TOKEN` verildiğinde her istek `Authorization: Bearer <token>` başlığı taşımalıdır. Obot arkasında buna gerek yok.

Claude Code'dan bağlanmak için:

```bash
claude mcp add --transport http google-flow http://localhost:8080/mcp \
  --header "Authorization: Bearer uzun-rastgele-bir-deger"
```
