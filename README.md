# 🤖 Binance USDT-M Vadeli Piyasa Walk-Forward Self-Optimizing Paper Trading Bot

Binance USDT-M Vadeli İşlemler piyasasında çalışan, Açık Pozisyon (Open Interest - OI), Hacim, Taker Baskısı, Mum Kapanış Gücü, **Destek/Direnç Kümeleri (Swing S/R)**, **Hacim Profili (Volume Profile - POC)** ve **Trend Doğrusu** anomalilerini tarayan, **ÇİFT YÖNLÜ (LONG & SHORT)**, Telegram çift yönlü interaktif komut destekli ve **"Kendi Parametrelerini Geriye Dönük Testle Güncelleyen" (Walk-Forward Self-Optimizing)** üst düzey kantitatif ticaret (quant trading) botu.

---

## 🏛️ 1. Sistem Mimarisi ve Dosya Yapısı

```
binance_quant_bot/
│
├── config.json              # Aktif işlem parametreleri (mtime tabanlı Hot-Reload destekli)
├── config.example.json      # Konfigürasyon şablonu
├── binance_client.py        # Public Binance Futures REST API istemcisi (API Key gerektirmez, TradFi filtreli)
├── market_structure.py      # Fraktal Tepe/Dip S/R kümeleri, Hacim POC ve Trend eğimi analiz motoru (LONG & SHORT)
├── telegram_notifier.py     # Emojili bildirimler ve çift yönlü Telegram bot komut dinleyicisi (/durum, /rapor)
├── optimizer.py             # 5m kline & komisyon simülasyonlu çift yönlü Walk-Forward Grid Search motoru
├── bot.py                   # Canlı piyasa tarayıcısı, durum kalıcılığı (persistence) ve çift yönlü pozisyon yöneticisi
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
   - Hem **LONG** hem de **SHORT** pozisyon simülasyonunu içerir.
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

## 🎯 4. Çift Yönlü Canlı Piyasa Tarayıcısı (LONG & SHORT)

Bot her 60 saniyede bir en hacimli 25 pariteyi süzer. Sahte kırılımları ve tuzakları engellemek için **6 kademeli simetrik teyit filtresi** uygulanır:

| Filtre Kademesi | 🟢 LONG Koşulu (Trend Kırılımı) | 🔴 SHORT Koşulu (Direnç Reddi & Asimetrik Scalp) |
|---|---|---|
| **1. Hacim Anomalisi** | Hacim $> \text{SMA}(20) \times \text{mult}$ ve **Yeşil Mum** (`Close > Open`) | Hacim $> \text{SMA}(20) \times \text{mult}$ ve **Kırmızı Mum** (`Close < Open`) |
| **2. Taker Baskısı** | Taker Alış Oranı $\ge \%55$ (Agresif alıcı baskısı) | Taker Satış Oranı $\ge \%55$ (Agresif satıcı baskısı) |
| **3. Mum Formasyonu** | $(Close - Low) / Range \ge 0.60$ (Üst fitil reddi) | **Dirençten / POC'den Red (Rejection)** veya Alt fitilsiz sert satış |
| **4. Açık Pozisyon (OI)** | 15m OI Artışı $\ge +\%2.0$ (Taze para ile Long açılışı) | 15m OI Artışı $\ge +\%2.0$ (Taze para ile Short açılışı) |
| **5. Squeeze / Kalabalık** | Top Trader L/S $\le 2.2$ (Kalabalık Long squeeze engeli) | Top Trader L/S $\ge 0.80$ (Kalabalık Short / Squeeze engeli) |
| **6. Trend Koruması** | - | **Güçlü Boğa Trendinde Short Açılmaz** (Trend Slope $\le 0.25$) |
| **7. Piyasa Yapısı** | Üstteki dirence $\ge \%1.2$ boşluk veya **Direnç Kırılımı (Breakout)** | Alttaki desteğe $\ge \%1.2$ boşluk veya **Dirençten Dönüş (Rejection)** |

---

## 📈 5. Asimetrik Pozisyon Yönetimi, Dinamik TP ve Çıkışlar

Finansal piyasalarda düşüşler hızlı ve sert V-dönüşlerine (Short Squeeze) açık olduğu için, bot **LONG ve SHORT pozisyonları için farklı parametreler** işletir:

- **🎯 Asimetrik Kâr Hedefleri (TP):**
  - **LONG:** `%3.0` Trailing TP tetikleyicisi ve `%0.6` delta ile uzun trend sürüşü.
  - **SHORT (Hızlı Scalp):** `%1.6` (`short_tp_pct`) Trailing TP tetikleyicisi ve `%0.4` sıkı delta ile kârı V-dönüşüne bırakmadan hızlı realizasyon.
- **🛡️ Asimetrik Başabaş (Break-Even):**
  - **LONG:** `+%1.5` kârda stop girişe (`+%0.20`) çekilir.
  - **SHORT:** `+%1.0` (`short_break_even_trigger_pct`) kârda stop anında girişe (`-%0.15`) çekilerek pozisyon dakikalar içinde tamamen risksiz hale getirilir.
- **⏱️ Asimetrik Zaman Aşımı (Timeout):**
  - **LONG:** 40 dakika (`position_timeout_minutes: 40`).
  - **SHORT:** **15 dakika** (`short_timeout_minutes: 15`). Düşüş dalgası 15 dakikada tamamlanamazsa oyalanmadan piyasadan çıkılır.
- **💸 Gerçekçi Borsa Komisyonu (%0.12 Roundtrip):**
  - Taker komisyonları ve kayma payları her iki yönde de net kârdan otomatik düşülür.
- **💾 Durum Kalıcılığı (State Persistence):**
  - Açık pozisyonlar ve yönleri `active_positions.json` dosyasına yazılır. Bot yeniden başlasa da pozisyon takibi kopmaz.

---

## 💬 6. Telegram Çift Yönlü İnteraktif Komutlar

Telegram botunuz sadece bildirim göndermekle kalmaz, Telegram üzerinden yazdığınız komutlara anında yanıt verir:

| Komut | Açıklama | Örnek Çıktı |
|---|---|---|
| `/durum` | Açıkta olan tüm sanal pozisyonları, yönlerini (`[LONG]` / `[SHORT]`), giriş fiyatlarını, anlık kâr/zarar durumlarını, S/R ve POC seviyelerini listeler. | `📊 Aktif Pozisyonlar: SOLUSDT [SHORT]: % +1.82 (BE Aktif) - Destek: $99.40 ...` |
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
    "top_trader_ls_min": 0.80,
    "allow_long": true,
    "allow_short": true,
    "short_tp_pct": 1.6,
    "short_delta_pct": 0.4,
    "short_sl_pct": 1.5,
    "short_timeout_minutes": 15,
    "short_break_even_trigger_pct": 1.0,
    "short_break_even_lock_pct": 0.15,
    "min_taker_buy_ratio": 0.55,
    "min_close_strength": 0.60,
    "use_sr_filter": true,
    "sr_pivot_window": 5,
    "sr_cluster_tolerance_pct": 0.4,
    "min_room_to_resistance_pct": 1.2,
    "min_room_to_support_pct": 1.2,
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
