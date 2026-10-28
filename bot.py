"""
bot.py
======
Binance USDT-M Vadeli İşlemler Canlı Piyasa Tarayıcısı,
Sanal Pozisyon Yöneticisi (Paper Trading) ve Telegram Bildirim Botu.

Özellikler:
- config.json dosyasını her döngüde okur (Hot-Reload).
- Başlangıçta konfigürasyon yoksa veya eskimişse optimizer.py'ı tetikler.
- Haftada bir otomatik Walk-Forward yeniden optimizasyon planlar.
- En hacimli ilk 25 pariteyi 60 saniyede bir tarar.
- 3'lü Teyit: 5m Hacim Anomalisi + 15m OI Artışı >= %2.0 + Top Trader L/S <= 2.2.
- 30 dakika parite soğuma (cooldown).
- Maksimum 2 eşzamanlı sanal LONG pozisyonu yönetir.
- Trailing Take-Profit, Stop-Loss ve 40 dakikalık Zaman Aşımı çıkışları uygular.
- paper_trades.csv dosyasına kayıt tutar ve Telegram'a emojili kartlar iletir.
"""

import os
import sys

# Script dizinini sys.path'e ekle
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import time
import json
import signal
import logging
import threading
from datetime import datetime
from dataclasses import dataclass, asdict
from typing import Dict, List, Optional, Any

import pandas as pd
import pandas_ta as ta

from binance_client import BinanceFuturesClient
from telegram_notifier import TelegramNotifier
from optimizer import run_optimization, load_config, CONFIG_PATH

# Loglama Konfigürasyonu
LOG_PATH = os.path.join(SCRIPT_DIR, "bot.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_PATH, encoding="utf-8")
    ]
)
logger = logging.getLogger("QuantBot")

CSV_PATH = os.path.join(SCRIPT_DIR, "paper_trades.csv")


@dataclass
class Position:
    symbol: str
    entry_price: float
    entry_time: float          # time.time()
    volume_mult: float
    oi_change: float
    ls_ratio: float
    sl_pct: float
    tp_trigger_pct: float
    tp_delta_pct: float
    timeout_minutes: int
    amount_usdt: float = 100.0
    qty: float = 0.0
    trailing_active: bool = False
    peak_price: float = 0.0
    peak_pnl_pct: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class PaperTradingBot:
    def __init__(self):
        self.is_running = True
        self.client = BinanceFuturesClient()
        self.config = load_config()
        self.notifier = TelegramNotifier(
            token=self.config.get("telegram_token", ""),
            chat_id=self.config.get("telegram_chat_id", "")
        )

        # Aktif sanal pozisyonlar: symbol -> Position
        self.active_positions: Dict[str, Position] = {}

        # Sinyal soğuma süreleri: symbol -> timestamp
        self.cooldowns: Dict[str, float] = {}

        # CSV Defterini Başlat
        self._init_csv()

        # Sistem Sinyalleri Yakalama (Graceful Shutdown)
        signal.signal(signal.SIGINT, self._handle_exit)
        signal.signal(signal.SIGTERM, self._handle_exit)

    def _init_csv(self) -> None:
        """paper_trades.csv yoksa veya kolonları eksikse günceller."""
        columns = [
            "timestamp", "symbol", "side", "entry_price", "exit_price",
            "qty", "amount_usdt", "pnl_pct", "pnl_usd",
            "duration_minutes", "exit_reason", "peak_pnl_pct"
        ]
        if not os.path.exists(CSV_PATH):
            df = pd.DataFrame(columns=columns)
            df.to_csv(CSV_PATH, index=False, encoding="utf-8")
            logger.info("paper_trades.csv dosyası oluşturuldu.")
        else:
            try:
                existing = pd.read_csv(CSV_PATH)
                if "amount_usdt" not in existing.columns:
                    df = pd.DataFrame(columns=columns)
                    df.to_csv(CSV_PATH, index=False, encoding="utf-8")
                    logger.info("paper_trades.csv başlıkları $100 tutar formatına güncellendi.")
            except Exception:
                pass

    def _log_trade_to_csv(self, trade_record: Dict[str, Any]) -> None:
        """Kapanan işlemi paper_trades.csv dosyasına ekler."""
        try:
            df = pd.DataFrame([trade_record])
            df.to_csv(CSV_PATH, mode="a", header=False, index=False, encoding="utf-8")
            logger.info("İşlem CSV defterine kaydedildi: %s | PnL: %%%.2f",
                        trade_record["symbol"], trade_record["pnl_pct"])
        except Exception as e:
            logger.error("CSV kayıt hatası: %s", e)

    def _handle_exit(self, signum, frame) -> None:
        """Bot kapatılırken açık pozisyonları güvenle loglar."""
        logger.info("Kapatma sinyali alındı. Bot durduruluyor...")
        self.is_running = False

    def reload_config_if_changed(self) -> None:
        """
        Hot-Reload: config.json dosyasını her döngüde yeniden okur.
        Parametre değiştiğinde botu yeniden başlatmadan anında aktif eder.
        """
        try:
            new_cfg = load_config()
            # Önemli parametreler değiştiyse logla
            keys_to_compare = [
                "volume_multiplier", "stop_loss_pct", "trailing_trigger_pct",
                "trailing_delta_pct", "max_concurrent_positions", "oi_min_increase_pct", "top_trader_ls_max"
            ]
            changes = []
            for k in keys_to_compare:
                if self.config.get(k) != new_cfg.get(k):
                    changes.append(f"{k}: {self.config.get(k)} -> {new_cfg.get(k)}")

            if changes:
                logger.info("🔄 [HOT-RELOAD] config.json güncellendi: %s", ", ".join(changes))

            self.config = new_cfg
            self.notifier.token = self.config.get("telegram_token", "")
            self.notifier.chat_id = self.config.get("telegram_chat_id", "")
        except Exception as e:
            logger.error("Hot-Reload okuma hatası: %s", e)

    def startup_check_and_optimize(self) -> None:
        """
        Başlangıçta config.json'ın varlığını ve güncelliğini kontrol eder.
        Gerekirse doğrudan hızlı bir başlangıç optimizasyonu yapar.
        """
        last_opt = self.config.get("last_optimized_at")
        should_optimize = False

        if not last_opt:
            logger.info("Başlangıç optimizasyonu gerekiyor (last_optimized_at boş)...")
            should_optimize = True
        else:
            try:
                opt_time = datetime.fromisoformat(last_opt)
                diff_days = (datetime.now() - opt_time).total_seconds() / 86400.0
                if diff_days >= 7.0:
                    logger.info("Son optimizasyon %.1f gün önce yapılmış. Model güncelleniyor...", diff_days)
                    should_optimize = True
            except Exception:
                should_optimize = True

        if should_optimize:
            try:
                logger.info("Başlangıç optimizasyonu çalıştırılıyor...")
                run_optimization()
                self.reload_config_if_changed()
            except Exception as e:
                logger.error("Başlangıç optimizasyonu hatası: %s. Mevcut ayarlar kullanılıyor.", e)

    def start_weekly_optimizer_thread(self) -> None:
        """
        Arka planda haftada bir (7 günde bir) çalışan bağımsız optimizasyon iş parçacığı.
        """
        def weekly_loop():
            logger.info("Haftalık arka plan optimizasyon izleyicisi aktif.")
            while self.is_running:
                # 6 saatte bir kontrol et
                time.sleep(21600)
                if not self.is_running:
                    break
                try:
                    last_opt = self.config.get("last_optimized_at")
                    if last_opt:
                        opt_time = datetime.fromisoformat(last_opt)
                        diff_days = (datetime.now() - opt_time).total_seconds() / 86400.0
                        if diff_days >= 7.0:
                            logger.info("📅 7 gün doldu. Otomatik haftalık optimizasyon başlatılıyor...")
                            run_optimization()
                            self.reload_config_if_changed()
                except Exception as e:
                    logger.error("Haftalık optimizasyon döngüsü hatası: %s", e)

        t = threading.Thread(target=weekly_loop, daemon=True, name="WeeklyOptimizerThread")
        t.start()

    def update_and_check_positions(self) -> None:
        """
        Açık sanal pozisyonları günceller:
        1. Stop-Loss kontrolü
        2. Trailing TP kontrolü
        3. 40 dakikalık Zaman Aşımı ("Hacim Söndü") kontrolü
        """
        if not self.active_positions:
            return

        symbols_to_close = []
        now = time.time()

        for symbol, pos in list(self.active_positions.items()):
            curr_price = self.client.get_latest_price(symbol)
            if curr_price is None or curr_price <= 0:
                continue

            entry_price = pos.entry_price
            pnl_pct = ((curr_price - entry_price) / entry_price) * 100.0
            duration_minutes = (now - pos.entry_time) / 60.0

            # 1. Zirve fiyat ve kâr takibi
            if curr_price > pos.peak_price:
                pos.peak_price = curr_price
                pos.peak_pnl_pct = pnl_pct

            # 2. Trailing TP Tetikleme Kontrolü
            if not pos.trailing_active and pos.peak_pnl_pct >= pos.tp_trigger_pct:
                pos.trailing_active = True
                logger.info("🎯 [%s] Trailing TP Aktifleşti! Zirve Kâr: %%%.2f", symbol, pos.peak_pnl_pct)

            # 3. Çıkış Koşulları Değerlendirmesi
            exit_reason = None
            exit_price = curr_price

            # Koşul A: Stop-Loss
            if pnl_pct <= -pos.sl_pct:
                exit_reason = "STOP_LOSS"
                exit_price = entry_price * (1.0 - pos.sl_pct / 100.0)

            # Koşul B: Trailing Stop Gerçekleşmesi
            elif pos.trailing_active:
                trailing_stop_price = pos.peak_price * (1.0 - pos.tp_delta_pct / 100.0)
                if curr_price <= trailing_stop_price:
                    exit_reason = "TRAILING_TP"
                    exit_price = trailing_stop_price

            # Koşul C: 40 Dakikalık Zaman Aşımı ("Hacim Söndü")
            elif duration_minutes >= pos.timeout_minutes:
                exit_reason = "ZAMAN_ASIMI (Hacim Söndü)"
                exit_price = curr_price

            # Kapanış gerekiyorsa uygula
            if exit_reason:
                final_pnl_pct = ((exit_price - entry_price) / entry_price) * 100.0
                pnl_usd = pos.amount_usdt * (final_pnl_pct / 100.0)
                trade_record = {
                    "timestamp": datetime.now().isoformat(),
                    "symbol": symbol,
                    "side": "LONG",
                    "entry_price": round(entry_price, 6),
                    "exit_price": round(exit_price, 6),
                    "qty": round(pos.qty, 6),
                    "amount_usdt": round(pos.amount_usdt, 2),
                    "pnl_pct": round(final_pnl_pct, 2),
                    "pnl_usd": round(pnl_usd, 2),
                    "duration_minutes": round(duration_minutes, 1),
                    "exit_reason": exit_reason,
                    "peak_pnl_pct": round(pos.peak_pnl_pct, 2)
                }

                logger.info("🔴 [%s Pozisyon Kapatıldı] Neden: %s | PnL: %%%.2f ($%.2f USDT) | Süre: %.1f dk",
                            symbol, exit_reason, final_pnl_pct, pnl_usd, duration_minutes)

                self._log_trade_to_csv(trade_record)
                self.notifier.notify_trade_closed(trade_record)
                symbols_to_close.append(symbol)

        for sym in symbols_to_close:
            self.active_positions.pop(sym, None)

    def scan_market(self) -> None:
        """
        En hacimli 25 pariteyi tarar ve 3 sinyal filtresini değerlendirir:
        1. 5m Mum Hacmi > SMA(20) * volume_multiplier (Anomali)
        2. Son 15 dakikada Open Interest artışı >= %2.0 (Taze para teyidi)
        3. Top Trader L/S Oranı <= 2.2 (Sıkışma/Kalabalık long riski filtresi)
        4. Cooldown: Aynı pariteye 30 dakika içinde ikinci sinyal üretilmez
        """
        max_positions = int(self.config.get("max_concurrent_positions", 0))
        is_unlimited = (max_positions <= 0)
        current_open = len(self.active_positions)

        if not is_unlimited and current_open >= max_positions:
            logger.info("Maksimum eşzamanlı pozisyon sayısına ulaşıldı (%d/%d). Yeni tarama atlanıyor.",
                        current_open, max_positions)
            return

        top_n = int(self.config.get("top_pairs_count", 25))
        top_pairs = self.client.get_top_volume_pairs(limit=top_n)
        lim_str = "Sınırsız (Unlimited)" if is_unlimited else f"{current_open}/{max_positions}"
        logger.info("Piyasa taranıyor: Top %d parite analiz ediliyor (Açık Pozisyonlar: %d | Limit: %s)...",
                    len(top_pairs), current_open, lim_str)

        now = time.time()
        cooldown_sec = float(self.config.get("cooldown_minutes", 30)) * 60.0
        vol_mult_threshold = float(self.config.get("volume_multiplier", 2.8))
        min_oi_increase = float(self.config.get("oi_min_increase_pct", 2.0))
        max_ls_ratio = float(self.config.get("top_trader_ls_max", 2.2))

        for symbol in top_pairs:
            if not self.is_running:
                break

            # Zaten pozisyonda mıyız?
            if symbol in self.active_positions:
                continue

            # Cooldown kontrolü (30 dk)
            if symbol in self.cooldowns:
                elapsed = now - self.cooldowns[symbol]
                if elapsed < cooldown_sec:
                    continue

            # 1. Filtre: 5m Hacim Anomalisi
            df = self.client.get_klines(symbol, interval="5m", limit=30)
            if df is None or len(df) < 25:
                continue

            df["vol_sma"] = ta.sma(df["volume"], length=20)
            last_candle = df.iloc[-1]
            prev_candle = df.iloc[-2]

            # Son kapanan veya anlık mumda anomali kontrolü
            candle_to_check = last_candle if (last_candle["close"] > last_candle["open"]) else prev_candle
            vol_sma = candle_to_check["vol_sma"]
            if pd.isna(vol_sma) or vol_sma <= 0:
                continue

            actual_vol_mult = candle_to_check["volume"] / vol_sma
            is_bullish = candle_to_check["close"] > candle_to_check["open"]

            if not (is_bullish and actual_vol_mult >= vol_mult_threshold):
                continue

            logger.info("🚨 [HACİM ANOMALİSİ TESPİT EDİLDİ] %s: Hacim %.2fx SMA(20) (Eşik: %.2fx)",
                        symbol, actual_vol_mult, vol_mult_threshold)

            # 2. Filtre: 15m Açık Pozisyon (OI) Artışı >= %2.0
            oi_change = self.client.get_open_interest_change(symbol)
            if oi_change is None:
                logger.debug("[%s] OI verisi alınamadı, atlanıyor.", symbol)
                continue

            if oi_change < min_oi_increase:
                logger.info("  ↳ [%s] OI Artışı yetersiz: +%%%.2f (Gereken: >= +%%%.2f). Reddedildi.",
                            symbol, oi_change, min_oi_increase)
                continue

            logger.info("  ↳ [%s] OI Teyidi BAŞARILI: +%%%.2f (Taze para girişi doğrulandı) 🟢",
                        symbol, oi_change)

            # 3. Filtre: Top Trader Long/Short Oranı <= 2.2
            ls_ratio = self.client.get_top_trader_long_short_ratio(symbol)
            if ls_ratio is None:
                logger.debug("[%s] L/S oranı verisi alınamadı, atlanıyor.", symbol)
                continue

            if ls_ratio > max_ls_ratio:
                logger.info("  ↳ [%s] Top Trader L/S Oranı çok yüksek: %.2f (Maks Eşik: %.2f - Kalabalık Long Riski). Reddedildi.",
                            symbol, ls_ratio, max_ls_ratio)
                continue

            logger.info("  ↳ [%s] L/S Teyidi BAŞARILI: %.2f <= %.2f (Aşırı kalabalık değil) 🟢",
                        symbol, ls_ratio, max_ls_ratio)

            # TÜM 3 FİLTRE SAĞLANDI -> SANAL LONG AÇ
            entry_price = float(last_candle["close"])
            sl_pct = float(self.config.get("stop_loss_pct", 2.0))
            tp_pct = float(self.config.get("trailing_trigger_pct", 3.0))
            delta_pct = float(self.config.get("trailing_delta_pct", 1.0))
            timeout_m = int(self.config.get("position_timeout_minutes", 40))
            trade_amount_usdt = float(self.config.get("trade_amount_usdt", 100.0))
            qty = (trade_amount_usdt / entry_price) if entry_price > 0 else 0.0

            sl_price = entry_price * (1.0 - sl_pct / 100.0)
            tp_trigger_price = entry_price * (1.0 + tp_pct / 100.0)

            new_pos = Position(
                symbol=symbol,
                entry_price=entry_price,
                entry_time=now,
                volume_mult=actual_vol_mult,
                oi_change=oi_change,
                ls_ratio=ls_ratio,
                sl_pct=sl_pct,
                tp_trigger_pct=tp_pct,
                tp_delta_pct=delta_pct,
                timeout_minutes=timeout_m,
                amount_usdt=trade_amount_usdt,
                qty=qty,
                trailing_active=False,
                peak_price=entry_price,
                peak_pnl_pct=0.0
            )

            self.active_positions[symbol] = new_pos
            self.cooldowns[symbol] = now  # 30 dk soğuma başlat

            logger.info("✅ [YENİ SANAL LONG] %s @ $%.4f ($%.2f USDT, Miktar: %.4f) | SL: $%.4f | Trailing: $%.4f",
                        symbol, entry_price, trade_amount_usdt, qty, sl_price, tp_trigger_price)

            # Telegram Sinyal Kartı İlet
            self.notifier.notify_signal_card(
                symbol=symbol,
                entry_price=entry_price,
                vol_mult=actual_vol_mult,
                oi_change=oi_change,
                ls_ratio=ls_ratio,
                sl_price=sl_price,
                tp_trigger_price=tp_trigger_price,
                config=self.config
            )

            # Eşzamanlı pozisyon limiti varsa ve ulaşıldıysa bu turu bitir
            if not is_unlimited and len(self.active_positions) >= max_positions:
                break

            time.sleep(0.5)

    def run(self) -> None:
        """
        Ana Çalıştırma Döngüsü.
        """
        logger.info("=" * 60)
        logger.info("BINANCE QUANT PAPER TRADING BOT BAŞLATILIYOR")
        logger.info("=" * 60)

        # 1. Başlangıç Kontrolü & Optimizasyon
        self.startup_check_and_optimize()

        # 2. Telegram Başlangıç Özeti Gönder
        self.notifier.notify_bot_started(self.config)

        # 3. Haftalık Arka Plan Optimizatörünü Başlat
        self.start_weekly_optimizer_thread()

        scan_interval = int(self.config.get("scan_interval_seconds", 60))
        last_scan_time = 0.0

        logger.info("Ana tarama motoru hazır. Tarama aralığı: %d saniye.", scan_interval)

        while self.is_running:
            try:
                # Hot-Reload kontrolü
                self.reload_config_if_changed()

                # Pozisyon Takibi (Sık aralıklarla kâr/zarar ve çıkış değerlendirme)
                self.update_and_check_positions()

                now = time.time()
                scan_interval = int(self.config.get("scan_interval_seconds", 60))

                # 60 saniyede bir piyasa taraması
                if now - last_scan_time >= scan_interval:
                    self.scan_market()
                    last_scan_time = now

                # Kısa aralıklarla döngüyü sürdür (pozisyon takibinin gecikmemesi için 5 sn)
                time.sleep(5)

            except Exception as e:
                logger.error("Ana döngü sırasında beklenmeyen hata: %s", e, exc_info=True)
                time.sleep(10)

        logger.info("Quant Bot güvenli bir şekilde kapatıldı.")


if __name__ == "__main__":
    bot = PaperTradingBot()
    bot.run()
