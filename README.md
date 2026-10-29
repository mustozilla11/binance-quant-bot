# 🤖 Binance USDT-M Vadeli Piyasa Walk-Forward Self-Optimizing Paper Trading Bot

Binance USDT-M Vadeli İşlemler piyasasında çalışan, Açık Pozisyon (Open Interest - OI), Hacim, Taker Alıcı Baskısı, Mum Kapanış Gücü, **Destek/Direnç Kümeleri (Swing S/R)**, **Hacim Profili (Volume Profile - POC)** ve **Trend Doğrusu** anomalilerini tarayan, Telegram çift yönlü interaktif komut destekli ve **"Kendi Parametrelerini Geriye Dönük Testle Güncelleyen" (Walk-Forward Self-Optimizing)** üst düzey kantitatif ticaret (quant trading) botu.

---

## 🏛️ 1. Sistem Mimarisi ve Dosya Yapısı

```
binance_quant_bot/
│
├── config.json              # Aktif işlem parametreleri (mtime tabanlı Hot-Reload destekli)
├── config.example.json      # Konfigürasyon şablonu
├── binance_client.py        # Public Binance Futures REST API istemcisi (API Key gerektirmez, TradFi filtreli)
├── market_structure.py      # Fraktal Tepe/Dip S/R kümeleri, Hacim POC ve Trend eğimi analiz motoru
├── telegram_notifier.py     # Emojili bildirimler ve çift yönlü Telegram bot komut dinleyicisi (/durum, /rapor)
├── optimizer.py             # 5m kline & komisyon simülasyonlu Walk-Forward Grid Search motoru
├── bot.py                   # Canlı piyasa tarayıcısı, durum kalıcılığı (persistence) ve pozisyon yöneticisi
├── active_positions.json    # Açık pozisyonların disk üzerindeki anlık durumu (Kapanıp açılsa da kaybolmaz)
├── paper_trades.csv         # Komisyon kesintili gerçekçi net kâr/zarar defteri
├── requirements.txt         # Minimal bağımlılıklar (Sadece requests ve pandas - C derleyicisi gerektirmez)
├── .gitignore               # Git tarafından yok sayılacak dosyalar
└── README.md                # Kapsamlı mimari ve çalıştırma kılavuzu
```

---

## 🛡️ 2. Güvenlik ve Ağ Mimarisi

- **Sıfır Borsa API Anahtarı Riski:** Sistem kesinlikle borsa API Key veya Secret Key talep etmez veya kullanmaz. Tamamen Binance'in herkese açık halka açık uç noktalarından beslenir:
  - `/fapi/v1/ticker/24hr` (24 saatlik hacim sıralaması)
  - `/fapi/v1/ticker/price` (Tek istekte tüm vadeli fiyatları çekme)
  - `/fapi/v1/klines` (Son 24 saatlik 5m mum verileri & taker buy hacimleri)
  - `/futures/data/openInterestHist` (Açık Pozisyon geçmişi)
  - `/futures/data/topLongShortPositionRatio` (Top Trader Long/Short oranları)
- **Akıllı ISP & CloudFront Yönlendirmesi:** Türk Telekom / BTK veya kurumsal ağ filtrelerinde doğrudan `fapi.binance.com` SNI engeli yaşandığında, `binance_client.py` otomatik olarak Binance resmi CloudFront CDN altyapısına geçiş yapar ve kesintisiz çalışmaya devam eder.
- **Hafif ve Kararlı Bağımlılık Yapısı:** `pandas-ta` ve `numba/llvmlite` bağımlılıkları tamamen kaldırılarak saf `pandas` rolling fonksiyonlarına geçilmiştir. Python 3.10'dan Python 3.14'e kadar tüm sürümlerde derleyici hatası olmadan saniyeler içinde kurulur.

---

## 🧠 3. Kendi Kendine Öğrenen Optimizasyon Motoru (`optimizer.py`)

Sabit parametreli botlar değişen piyasa rejimlerinde verimsizleşir. Bu bot, piyasaya dinamik olarak uyum sağlamak için Walk-Forward Grid Search optimizasyonu çalıştırır:

1. **Veri Toplama:** En hacimli ilk 5 kripto vadeli paritesinin (BTC, ETH, SOL, XRP, PEPE) son 1.500 adet 5 dakikalık mum verilerini (Taker Buy hacimleri dahil) indirir.
2. **Kademeli Filtre & Komisyon Simülasyonu:**
   - Canlı bot ile 1-e-1 aynı 5 dakikalık filtreleri çalıştırır.
   - Her işlemden `%0.12` oranında komisyon ve kayma (roundtrip fee + slippage) kesintisi uygular.
   - Break-Even stop simülasyonunu içerir.
3. **Grid Search Alanı:** Toplam 81 kombinasyon simüle edilir:
   - **Hacim Eşiği:** `[2.2x, 2.8x, 3.5x]`
   - **Stop-Loss:** `[%1.5, %2.0, %3.0]`
   - **Trailing TP Tetik:** `[%2.0, %3.0, %4.5]`
   - **Trailing Geri Çekilme (Delta):** `[%0.6, %1.0, %1.4]`
4. **Seçim Kriteri:**
   $$\text{Profit Factor} = \frac{\text{Net Kâr}}{\text{Net Zarar}}$$
   $$\text{Max Drawdown} = \max \left( \frac{\text{Zirve Bakiye} - \text{Anlık Bakiye}}{\text{Zirve Bakiye}} \right)$$
   En yüksek Profit Factor ve en düşük Max Drawdown üreten parametre kümesi seçilir.
5. **Hot-Reload Güncellemesi:** Yeni parametreler anında `config.json`a yazılır. Ana bot yeniden başlatılmaya gerek kalmadan yeni ayarlara geçer.

---

## 🎯 4. Canlı Piyasa Tarayıcısı ve 6 Kademeli Sinyal Filtresi (`bot.py` & `market_structure.py`)

Bot her 60 saniyede bir en hacimli 25 pariteyi süzer. Sahte kırılımları (fake-out) ve tuzakları engellemek için **6 kademeli teyit filtresi** uygulanır:

1. **5 Dakikalık Hacim Anomalisi:**
   $$\text{Hacim}_{5m} > \text{SMA}(20, \text{Hacim}) \times \text{config["volume\_multiplier"]}$$
   Mumun yeşil (yükseliş) olması şarttır (`Close > Open`).
2. **Taker Alıcı Baskısı (Taker Buy Ratio):**
   $$\frac{\text{Taker Buy Hacmi}}{\text{Toplam Hacim}} \ge 0.55 \quad (\%55)$$
   Hacim patlamasının piyasa yapıcı emirlerle değil, piyasayı agresif süpüren gerçek market alıcılarıyla oluştuğu doğrulanır.
3. **Mum Kapanış Gücü (Upper Wick Reddi):**
   $$\text{Kapanış Gücü} = \frac{\text{Close} - \text{Low}}{\text{High} - \text{Low}} \ge 0.60 \quad (\%60)$$
   Mumun üstünde uzun fitil (satıcı baskısı) kalmamış olması, kapanışın mum tepe noktasına yakın gerçekleşmesi şarttır.
4. **Açık Pozisyon (OI) Teyidi:**
   Son 15 dakikada Open Interest artışı $\ge \%2.0$ olmalıdır. Hacim artarken OI artıyorsa piyasaya "taze kurumsal para" girdiği teyit edilir.
5. **Duyarlılık (Top Trader L/S) Filtresi:**
   Top Trader Long/Short oranı $\le 2.2$ olmalıdır. Oran 2.2 üzerindeyse piyasa aşırı kalabalık long pozisyondadır (Long Squeeze riski) ve sinyal reddedilir.
6. **🧱 Piyasa Yapısı, Hacim POC ve 'Duvara Çarpma' Engeli (`market_structure.py`):**
   - **Swing S/R Kümeleri:** Son 24 saatin (288 mum) lokal fraktal tepe/dipleri kümelenerek destek ve direnç bantları çıkarılır.
   - **Hacim POC (Point of Control):** En çok işlem gören fiyat seviyesi çekim/destek merkezi olarak belirlenir.
   - **Duvara Çarpma Engeli (Room to Run):** Üstteki güçlü direnç giriş fiyatına `%1.20`'den daha yakınsa (`min_room_to_resistance_pct`) işlem **iptal edilir** (tavana yakın alım engellenir). Eğer mum direnci yukarı kırmışsa **Kırılım (Breakout)** etiketiyle işleme izin verilir.
7. **Otomatik TradFi & Emtia Engeli:**
   Binance'in `exchangeInfo` meta verilerindeki `TRADIFI_PERPETUAL` sözleşmeleri ve petrol/altın/endeks varlıkları (`BZUSDT`, `XAUUSDT`, `XAGUSDT`, `SOXLUSDT` vb.) düşük kripto volatilitesi sebebiyle baştan taranmaz.
8. **Cooldown (Soğuma):**
   Bir coinde sinyal üretildikten sonra 30 dakika boyunca o pariteden yeni işlem açılmaz.

---

## 📈 5. Pozisyon Yönetimi, Dinamik TP, Break-Even ve Borsa Komisyonu

Sinyal geldiğinde anlık piyasa fiyatından sanal LONG açılır (**Sınırsız Eşzamanlı İşlem & Sabit 100 USDT Büyüklük**):

- **Sabit 100 USDT Kasa Boyutu:** Her işlem sanal olarak tam **100 USDT** tutarında açılır. Kripto miktarı `100 / Giriş Fiyatı` formülüyle otomatik hesaplanır.
- **Sınırsız İşlem Modu:** `max_concurrent_positions: 0` olarak ayarlandığında, kriterleri sağlayan tüm pariteler bağımsız slotlar halinde eşzamanlı açılır.
- **🎯 Dinamik Kâr Alma (Take-Profit):**
  - Üstte girişin `+%1.5` ile varsayılan TP (`+%3.0`) arasında güçlü bir direnç kümesi varsa, TP tetikleyicisi otomatik olarak `direnç - %0.15` seviyesine çekilir (fiyat direnç duvarından dönmeden kâr kilitlenir).
- **🛡️ Break-Even Koruma (Başa Baş Stop):**
  - Fiyat kârda **+%1.50** (`break_even_trigger_pct`) seviyesine ulaştığında stop seviyesi otomatik olarak girişin **+%0.20** üzerine (`break_even_lock_pct`) çekilir.
  - Böylece işlem riskten arındırılır; borsa komisyonu garanti altına alınır ve pozisyonun zararla kapanma ihtimali sıfırlanır. Telegram'a özel bildirim atılır.
- **Trailing Take-Profit:**
  - Fiyat `%config["trailing_trigger_pct"]` kâra ulaştığında Trailing Stop devreye girer.
  - Ulaşılan tepe fiyattan geriye doğru `%config["trailing_delta_pct"]` kadar sarktığında kâr realizasyonu yapılır.
- **Zaman Aşımı (Timeout):**
  - Pozisyon 40 dakika içinde TP veya SL olmadıysa momentumun kaybolduğu kabul edilir ("Hacim Söndü") ve anlık fiyattan kapatılır.
- **💸 Gerçekçi Borsa Komisyonu (%0.12 Roundtrip):**
  - Gerçek vadeli piyasalarda %0.05 Taker alış + %0.05 Taker satış + ortalama %0.02 slippage bulunur.
  - Bot her kapanan işlemden net `%0.12` komisyon düşerek `paper_trades.csv`ye ve Telegram'a **Net PnL** olarak yansıtır.
- **💾 Durum Kalıcılığı (State Persistence):**
  - Açık pozisyonlar anlık olarak `active_positions.json` dosyasına yazılır.
  - Bot kapatılıp açılsa bile hafıza kaybı yaşanmaz; açık sanal pozisyonlar kaldığı yerden takip edilmeye devam eder.

---

## 💬 6. Telegram Çift Yönlü İnteraktif Komutlar

Telegram botunuz sadece bildirim göndermekle kalmaz, Telegram üzerinden yazdığınız komutlara anında yanıt verir:

| Komut | Açıklama | Örnek Çıktı |
|---|---|---|
| `/durum` | Açıkta olan tüm sanal pozisyonları, giriş fiyatlarını, anlık kâr/zarar durumlarını, S/R ve POC seviyelerini listeler. | `📊 Aktif Pozisyonlar: SOLUSDT: % +1.82 (BE Aktif) - Direnç: $102.99 ...` |
| `/rapor` | `paper_trades.csv` defterini tarayarak toplam işlem sayısı, Win Rate (Kazanma Oranı), Toplam Net Dolar ve Yüzdesel PnL özetini sunar. | `📈 Paper Trading Raporu: Toplam İşlem: 14, Win Rate: %71.4, Net Kasa: +12.45$` |
| `/yardim` | Kullanılabilir bot komutlarını ve açıklamalarını listeler. | Komut listesi ve kılavuz. |

---

## 🚀 7. Detaylı Kurulum ve Çalıştırma Kılavuzu

### Adım 1: Depoyu Klonlama veya Proje Dizinine Geçiş
```bash
git clone https://github.com/mustozilla11/binance-quant-bot.git
cd binance-quant-bot
```

### Adım 2: Python Sanal Ortamını Hazırlama
Proje Python 3.10, 3.11, 3.12, 3.13 ve 3.14 sürümleriyle tam uyumludur:
```bash
python3 -m venv .venv

# Bash / Zsh kullanıcıları için:
source .venv/bin/activate

# Fish Shell kullanıcıları için:
source .venv/bin/activate.fish

pip install --upgrade pip
pip install -r requirements.txt
```

### Adım 3: Konfigürasyon Ayarları (`config.json`)
```json
{
    "volume_multiplier": 2.8,
    "stop_loss_pct": 2.0,
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
    "min_taker_buy_ratio": 0.55,
    "min_close_strength": 0.60,
    "use_sr_filter": true,
    "sr_pivot_window": 5,
    "sr_cluster_tolerance_pct": 0.4,
    "min_room_to_resistance_pct": 1.2,
    "dynamic_tp_enabled": true,
    "structural_sl_enabled": true,
    "break_even_trigger_pct": 1.5,
    "break_even_lock_pct": 0.2,
    "commission_pct": 0.12,
    "telegram_token": "YOUR_TELEGRAM_BOT_TOKEN",
    "telegram_chat_id": "YOUR_TELEGRAM_CHAT_ID"
}
```

### Adım 4: Canlı Paper Trading Botunu Başlatma

#### Doğrudan Sanal Ortam Python'u ile Başlatma (Fish & Bash Uyumlu):
```bash
.venv/bin/python bot.py
```

#### Arka Planda Sürekli Çalıştırma (nohup):
```bash
nohup .venv/bin/python bot.py > bot_stdout.log 2>&1 &
```
Logları canlı takip etmek için:
```bash
tail -f bot.log
```
Durdurmak için:
```bash
pkill -f bot.py
```

---

## 📊 8. İşlem Raporlarını İnceleme

Kapanan tüm işlemler komisyon kesintili olarak `paper_trades.csv`ye yazılır:
```bash
cat paper_trades.csv
```
Veya Telegram üzerinden botunuza doğrudan `/rapor` yazarak tüm istatistiklerinizi anlık olarak alabilirsiniz.
