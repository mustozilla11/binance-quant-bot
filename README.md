# 🤖 Binance USDT-M Vadeli Piyasa Walk-Forward Self-Optimizing Paper Trading Bot

Binance USDT-M Vadeli İşlemler piyasasında çalışan, Açık Pozisyon (Open Interest - OI) ve Hacim anomalilerini tarayan, Telegram bildirimli ve **"Kendi Parametrelerini Geriye Dönük Testle Güncelleyen" (Walk-Forward Self-Optimizing)** modüler kantitatif ticaret (quant trading) botu.

---

## 🏛️ 1. Sistem Mimarisi ve Dosya Yapısı

```
binance_quant_bot/
│
├── config.json              # Aktif işlem parametreleri (Hot-Reload destekli)
├── config.example.json      # Konfigürasyon şablonu
├── binance_client.py        # Public Binance Futures REST API istemcisi (API Key gerektirmez)
├── telegram_notifier.py     # Emojili sinyal, işlem kapanışı ve model bildirimleri
├── optimizer.py             # 14 günlük Walk-Forward Grid Search optimizasyon motoru
├── bot.py                   # Canlı piyasa tarayıcısı ve sanal pozisyon yöneticisi
├── paper_trades.csv         # Sanal işlemlerin kaydedildiği defter
├── requirements.txt         # Gerekli Python kütüphaneleri
├── .gitignore               # Git tarafından yok sayılacak dosyalar
└── README.md                # Kapsamlı mimari ve çalıştırma kılavuzu
```

---

## 🛡️ 2. Güvenlik ve Bağlantı Mimarisi

- **Sıfır Borsa API Anahtarı Riski:** Sistem kesinlikle borsa API Key veya Secret Key talep etmez veya kullanmaz. Tamamen Binance'in herkese açık halka açık uç noktalarından beslenir:
  - `/fapi/v1/ticker/24hr` (24 saatlik hacim sıralaması)
  - `/fapi/v1/klines` (5m ve 15m mum verileri)
  - `/futures/data/openInterestHist` (15m Açık Pozisyon geçmişi)
  - `/futures/data/topLongShortPositionRatio` (Top Trader Long/Short oranları)
- **Akıllı ISP & CloudFront Yönlendirmesi:** Türk Telekom / BTK veya kurumsal ağ filtrelerinde doğrudan `fapi.binance.com` SNI engeli yaşandığında, `binance_client.py` otomatik olarak Binance resmi CloudFront CDN altyapısına geçiş yapar ve kesintisiz çalışmaya devam eder.
- **Telegram Entegrasyonu:** Bot başlatma, anomali sinyalleri, pozisyon kâr/zarar sonuçları ve model optimizasyon raporları anlık olarak Telegram kanalınıza iletilir.

---

## 🧠 3. Kendi Kendine Öğrenen Optimizasyon Motoru (`optimizer.py`)

Sabit parametreli botlar değişen piyasa rejimlerinde verimsizleşir. Bu bot, piyasaya dinamik olarak uyum sağlamak için Walk-Forward Grid Search optimizasyonu çalıştırır:

1. **Veri Toplama:** En hacimli ilk 5 kripto vadeli paritesinin (BTC, ETH, SOL, XRP, PEPE) son 14 günlük 15 dakikalık verilerini (1.344 mum/parite) indirir.
2. **Grid Search Alanı:** Toplam 81 kombinasyon simüle edilir:
   - **Hacim Eşiği:** `[2.2x, 2.8x, 3.5x]`
   - **Stop-Loss:** `[%1.5, %2.0, %3.0]`
   - **Trailing TP Tetik:** `[%2.0, %3.0, %4.5]`
   - **Trailing Geri Çekilme (Delta):** `[%0.6, %1.0, %1.4]`
3. **Seçim Kriteri:**
   $$\text{Profit Factor} = \frac{\text{Brüt Kâr}}{\text{Brüt Zarar}}$$
   $$\text{Max Drawdown} = \max \left( \frac{\text{Zirve Bakiye} - \text{Anlık Bakiye}}{\text{Zirve Bakiye}} \right)$$
   En yüksek Profit Factor ve en düşük Max Drawdown üreten parametre kümesi seçilir.
4. **Hot-Reload Güncellemesi:** Yeni parametreler anında `config.json`a yazılır. Ana bot yeniden başlatılmaya gerek kalmadan yeni ayarlara geçer.
5. **Telegram Raporu:**
   ```
   ⚙️ *HAFTALIK MODEL GÜNCELLENDİ*
   Son 14 gün analiz edildi.
   Yeni Hacim Eşiği: 3.5x
   Yeni Stop-Loss: %3.0
   Yeni Trailing TP: %3.0 (Delta: %0.6)
   ```

---

## 🎯 4. Canlı Piyasa Tarayıcısı ve Sinyal Filtreleri (`bot.py`)

Bot her 60 saniyede bir en hacimli 25 pariteyi 3 kademeli teyit filtresiyle süzer:

1. **5 Dakikalık Hacim Anomalisi:**
   $$\text{Hacim}_{5m} > \text{SMA}(20, \text{Hacim}) \times \text{config["volume\_multiplier"]}$$
   Mumun yeşil (yükseliş) olması şarttır (`Close > Open`).
2. **Açık Pozisyon (OI) Teyidi:**
   Son 15 dakikada Open Interest artışı $\ge \%2.0$ olmalıdır. Hacim artarken OI artıyorsa piyasaya "taze kurumsal para" girdiği teyit edilir.
3. **Duyarlılık (Top Trader L/S) Filtresi:**
   Top Trader Long/Short oranı $\le 2.2$ olmalıdır. Oran 2.2 üzerindeyse piyasa aşırı kalabalık long pozisyondadır (Long Squeeze riski yüksektir) ve sinyal reddedilir.
4. **Cooldown (Soğuma):**
   Bir coinde sinyal üretildikten sonra 30 dakika boyunca o pariteden yeni işlem açılmaz.

---

## 📈 5. Sanal İşlem Yönetimi ve Çıkış Algoritması

Sinyal geldiğinde anlık piyasa fiyatından sanal LONG açılır (**Sınırsız Eşzamanlı İşlem & Sabit 100 USDT Büyüklük**):

- **Sabit İşlem Tutarı:** Her işlem sanal olarak tam **100 USDT** tutarında açılır. Kripto miktarı `100 / Giriş Fiyatı` formülüyle otomatik hesaplanır.
- **Sınırsız İşlem Limiti:** `max_concurrent_positions: 0` olarak ayarlanmıştır. Kriterleri sağlayan tüm pariteler aynı anda bağımsız sanal pozisyon olarak açılabilir. (Aynı pariteye 30 dakika soğuma kuralı geçerliliğini korur).
- **Stop-Loss:** Fiyat giriş seviyesinden `%config["stop_loss_pct"]` kadar düşerse anında pozisyon kapatılır.
- **Trailing Take-Profit:**
  - Fiyat `%config["trailing_trigger_pct"]` kadar kâra ulaştığında Trailing Stop **aktifleşir**.
  - Aktifleştiği andan itibaren ulaşılan zirve fiyat (`peak_price`) hafızaya alınır.
  - Fiyat zirveden `%config["trailing_delta_pct"]` kadar gerilerse kâr realizasyonu yapılarak pozisyon kapatılır.
- **Zaman Aşımı (Timeout):**
  - Pozisyon 40 dakika içinde kapanmazsa momentumun kaybolduğu kabul edilir ("Hacim Söndü") ve anlık fiyattan kapatılır.
- **Deftere Kayıt:**
  Kapanan her işlem `paper_trades.csv`ye hem yüzdesel kâr/zarar (`pnl_pct`) hem de dolar bazında net kâr/zarar (`pnl_usd`) olarak eklenir ve Telegram'a bildirilir.

---

## 🚀 6. Detaylı Kurulum ve Çalıştırma Kılavuzu

### Adım 1: Depoyu Klonlama veya Proje Dizinine Geçiş
```bash
git clone https://github.com/mustozilla11/binance-quant-bot.git
cd binance-quant-bot
```

### Adım 2: Python Sanal Ortamını Hazırlama
Proje Python 3.10, 3.11 veya 3.12 sürümleriyle tam uyumludur:
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

*(Eğer sisteminizde `uv` kuruluysa doğrudan `uv venv --python 3.12 .venv && uv pip install -r requirements.txt` şeklinde saniyeler içinde kurabilirsiniz).*

### Adım 3: Telegram ve İşlem Parametrelerini Ayarlama
`config.json` dosyasını açarak Telegram bilgilerinizi kontrol edin:
```json
{
    "volume_multiplier": 3.5,
    "stop_loss_pct": 3.0,
    "trailing_trigger_pct": 3.0,
    "trailing_delta_pct": 0.6,
    "max_concurrent_positions": 0,
    "trade_amount_usdt": 100.0,
    "position_timeout_minutes": 40,
    "cooldown_minutes": 30,
    "scan_interval_seconds": 60,
    "top_pairs_count": 25,
    "oi_min_increase_pct": 2.0,
    "top_trader_ls_max": 2.2,
    "telegram_token": "YOUR_TELEGRAM_BOT_TOKEN",
    "telegram_chat_id": "YOUR_TELEGRAM_CHAT_ID"
}
```

### Adım 4: Model Optimizasyonu (İsteğe Bağlı Manuel Çalıştırma)
Modeli son 14 günlük canlı piyasa verileri üzerinde test etmek ve en iyi parametreleri bulmak için:
```bash
source .venv/bin/activate
python optimizer.py
```
Bu işlem 81 kombinasyonu saniyeler içinde simüle eder, `config.json` dosyasını otomatik günceller ve Telegram'a rapor iletir.

### Adım 5: Canlı Paper Trading Botunu Başlatma

#### Normal Çalıştırma (Terminalde İzleme):
```bash
source .venv/bin/activate
python bot.py
```

#### Arka Planda Sürekli Çalıştırma (nohup):
Terminali kapatsanız dahi botun çalışmaya devam etmesi için:
```bash
source .venv/bin/activate
nohup python bot.py > bot_stdout.log 2>&1 &
```
Çalışan işlemi görmek için:
```bash
ps aux | grep bot.py
tail -f bot.log
```
Durdurmak için:
```bash
pkill -f bot.py
```

#### `tmux` veya `screen` ile Çalıştırma:
```bash
tmux new -s quantbot
source .venv/bin/activate
python bot.py
```
*(Ayrılmak için `Ctrl+B` ardından `D` tuşuna basabilirsiniz. Geri dönmek için: `tmux attach -t quantbot`).*

---

## 📊 7. İşlem Raporlarını İnceleme

Kapanan tüm işlemler anında `paper_trades.csv` dosyasına yazılır.
CSV dosyasını incelemek için:
```bash
cat paper_trades.csv
```
Veya pandas ile hızlı özet almak için:
```python
import pandas as pd
df = pd.read_csv("paper_trades.csv")
print(df.tail(10))
print("Toplam PnL: %", df["pnl_pct"].sum())
```
