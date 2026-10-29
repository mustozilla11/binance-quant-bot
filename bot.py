"""
bot.py
======
Binance USDT-M Vadeli İşlemler Canlı Piyasa Tarayıcısı,
Sanal Pozisyon Yöneticisi (Paper Trading) ve Telegram İnteraktif Bildirim Botu.

Özellikler:
- Saf pandas kullanımı (pandas-ta / numba gereksinimi kaldırıldı).
- State Persistence: Açık pozisyonlar active_positions.json dosyasına kaydedilir (Restart/Reboot koruması).
- Hot-Reload: config.json sadece dosya değiştirilme zamanı (mtime) değiştiğinde okunur.
- Batch Fiyat Güncellemesi: Tek HTTP isteğiyle tüm pozisyon fiyatları anlık güncellenir.
- Başabaş (Break-Even) Stop: +%1.5 kâr görüldüğünde Stop-Loss otomatik olarak girişin üzerine (+%0.20 net kâr) taşınır.
- Gerçekçi Borsa Komisyonu: %0.12 Taker komisyonu ve kayma (slippage) her işlemden otomatik düşülür.
- Taker Buy Volume (%55+) ve Üst Fitil (Fakeout / Rejection %60+) filtreleri.
- Otomatik TradFi (Emtia/Hisse/ETF) eleme ve 60 saniyede bir canlı güncellenen Top 25 kripto tarayıcısı.
- Telegram İnteraktif Komut Dinleyicisi (/durum, /rapor, /yardim).
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

from binance_client import BinanceFuturesClient
from telegram_notifier import TelegramNotifier
from optimizer import run_optimization, load_config, CONFIG_PATH
from market_structure import analyze_market_structure, MarketStructureResult

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
POSITIONS_PATH = os.path.join(SCRIPT_DIR, "active_positions.json")


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
    side: str = "LONG"
    amount_usdt: float = 100.0
    qty: float = 0.0
    trailing_active: bool = False
    break_even_active: bool = False
    peak_price: float = 0.0
    peak_pnl_pct: float = 0.0     # MFE (Maximum Favorable Excursion)
    mae_pnl_pct: float = 0.0      # MAE (Maximum Adverse Excursion)
    nearest_support: Optional[float] = None
    nearest_resistance: Optional[float] = None
    poc_price: Optional[float] = None
    trend_direction: str = "Bilinmiyor"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Position":
        valid_keys = cls.__dataclass_fields__.keys()
        filtered = {k: v for k, v in data.items() if k in valid_keys}
        return cls(**filtered)


class PaperTradingBot:
    def __init__(self):
        self.is_running = True
        self.client = BinanceFuturesClient()
        self.config = load_config()
        self._last_config_mtime: float = os.path.getmtime(CONFIG_PATH) if os.path.exists(CONFIG_PATH) else 0.0

        self.notifier = TelegramNotifier(
            token=self.config.get("telegram_token", ""),
            chat_id=self.config.get("telegram_chat_id", "")
        )

        # Aktif sanal pozisyonlar: symbol -> Position (Diskten yükle)
        self.active_positions: Dict[str, Position] = self._load_positions_from_disk()

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
            "qty", "amount_usdt", "pnl_pct", "pnl_usd", "commission_usd",
            "duration_minutes", "exit_reason", "peak_pnl_pct"
        ]
        if not os.path.exists(CSV_PATH):
            df = pd.DataFrame(columns=columns)
            df.to_csv(CSV_PATH, index=False, encoding="utf-8")
            logger.info("paper_trades.csv dosyası oluşturuldu.")
        else:
            try:
                existing = pd.read_csv(CSV_PATH)
                if "commission_usd" not in existing.columns:
                    # Yeni kolonları ekle
                    for col in columns:
                        if col not in existing.columns:
                            existing[col] = 0.0
                    existing.to_csv(CSV_PATH, index=False, encoding="utf-8")
            except Exception:
                pass

    def _load_positions_from_disk(self) -> Dict[str, Position]:
        """Bot yeniden başladığında açık pozisyonları active_positions.json'dan okur."""
        positions = {}
        if os.path.exists(POSITIONS_PATH):
            try:
                with open(POSITIONS_PATH, "r", encoding="utf-8") as f:
                    raw_data = json.load(f)
                    for sym, pos_data in raw_data.items():
                        positions[sym] = Position.from_dict(pos_data)
                if positions:
                    logger.info("📁 Kayıtlı %d adet açık pozisyon diskten yüklendi: %s",
                                len(positions), list(positions.keys()))
            except Exception as e:
                logger.error("active_positions.json okuma hatası: %s", e)
        return positions

    def _save_positions_to_disk(self) -> None:
        """Açık pozisyonları anlık olarak active_positions.json'a yazar."""
        try:
            data = {sym: pos.to_dict() for sym, pos in self.active_positions.items()}
            with open(POSITIONS_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error("active_positions.json yazma hatası: %s", e)

    def _log_trade_to_csv(self, trade_record: Dict[str, Any]) -> None:
        """Kapanan işlemi paper_trades.csv dosyasına ekler."""
        try:
            write_header = not os.path.exists(CSV_PATH) or os.path.getsize(CSV_PATH) == 0
            df = pd.DataFrame([trade_record])
            df.to_csv(CSV_PATH, mode="a", header=write_header, index=False, encoding="utf-8")
            logger.info("İşlem CSV defterine kaydedildi: %s | Net PnL: %%%.2f ($%.2f USDT) | MFE: +%%%.2f | MAE: %%%.2f",
                        trade_record["symbol"], trade_record["pnl_pct"], trade_record.get("pnl_usd", 0.0),
                        trade_record.get("mfe_pct", 0.0), trade_record.get("mae_pct", 0.0))
        except Exception as e:
            logger.error("CSV kayıt hatası: %s", e)

    def _handle_exit(self, signum, frame) -> None:
        """Bot kapatılırken açık pozisyonları güvenle diske kaydeder."""
        logger.info("Kapatma sinyali alındı. Açık pozisyonlar diske kaydediliyor...")
        self._save_positions_to_disk()
        self.is_running = False

    def reload_config_if_changed(self) -> None:
        """
        Hot-Reload: config.json dosyasını sadece mtime değiştiğinde okur (Disk I/O tasarrufu).
        """
        if not os.path.exists(CONFIG_PATH):
            return

        try:
            current_mtime = os.path.getmtime(CONFIG_PATH)
            if current_mtime == self._last_config_mtime:
                return  # Dosya değişmemiş, CPU ve disk harcama

            self._last_config_mtime = current_mtime
            new_cfg = load_config()

            keys_to_compare = [
                "volume_multiplier", "stop_loss_pct", "trailing_trigger_pct",
                "trailing_delta_pct", "max_concurrent_positions", "trade_amount_usdt",
                "break_even_trigger_pct", "commission_pct", "oi_min_increase_pct",
                "top_trader_ls_max", "top_trader_ls_min", "allow_long", "allow_short",
                "short_tp_pct", "short_delta_pct", "short_sl_pct", "short_timeout_minutes",
                "short_break_even_trigger_pct", "short_break_even_lock_pct",
                "use_sr_filter", "min_room_to_resistance_pct", "min_room_to_support_pct",
                "dynamic_tp_enabled", "structural_sl_enabled"
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
        Başlangıçta config.json'ın güncelliğini kontrol eder.
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
                self._last_config_mtime = 0.0  # Reload'ı tetikle
                self.reload_config_if_changed()
            except Exception as e:
                logger.error("Başlangıç optimizasyonu hatası: %s. Mevcut ayarlar kullanılıyor.", e)

    def start_weekly_optimizer_thread(self) -> None:
        """Haftada bir arka planda çalışan otomatik optimizasyon iş parçacığı."""
        def weekly_loop():
            logger.info("Haftalık arka plan optimizasyon izleyicisi aktif.")
            while self.is_running:
                time.sleep(21600)  # 6 saatte bir kontrol et
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
                            self._last_config_mtime = 0.0
                            self.reload_config_if_changed()
                except Exception as e:
                    logger.error("Haftalık optimizasyon döngüsü hatası: %s", e)

        t = threading.Thread(target=weekly_loop, daemon=True, name="WeeklyOptimizerThread")
        t.start()

    def start_telegram_command_listener(self) -> None:
        """
        Telegram üzerinden gelen /durum, /rapor, /yardim komutlarını dinler ve yanıtlar.
        """
        def telegram_loop():
            logger.info("Telegram interaktif komut dinleyicisi başlatıldı (/durum, /rapor).")
            last_update_id = None
            while self.is_running:
                try:
                    updates = self.notifier.get_updates(offset=last_update_id, timeout=2)
                    for update in updates:
                        last_update_id = update["update_id"] + 1
                        message = update.get("message", {})
                        text = message.get("text", "").strip()
                        chat_id = str(message.get("chat", {}).get("id", ""))

                        # Sadece yetkili chat_id'den gelen komutları kabul et
                        if chat_id != str(self.notifier.chat_id):
                            continue

                        if text == "/durum":
                            self._handle_cmd_status()
                        elif text in ["/rapor", "/ozet"]:
                            self._handle_cmd_report()
                        elif text.startswith("/kapat"):
                            self._handle_cmd_close(text)
                        elif text in ["/yardim", "/help", "/start"]:
                            help_msg = (
                                "🤖 *Binance Quant Bot Komutları:*\n"
                                "━━━━━━━━━━━━━━━━━━━━━\n"
                                "• `/durum` - Açık sanal pozisyonları ve anlık PnL durumunu gösterir\n"
                                "• `/rapor` (veya `/ozet`) - Genel kâr/zarar karnesini döner\n"
                                "• `/kapat <SEMBOL>` - Açık pozisyonu anında kapatır (örn: `/kapat SOLUSDT`)\n"
                                "• `/kapat hepsi` - Tüm açık pozisyonları tek tuşla kapatır\n"
                                "• `/yardim` - Bu menüyü gösterir"
                            )
                            self.notifier.send_message(help_msg)
                except Exception as e:
                    logger.debug("Telegram dinleyici hatası: %s", e)
                time.sleep(2)

        t = threading.Thread(target=telegram_loop, daemon=True, name="TelegramListenerThread")
        t.start()

    def _handle_cmd_status(self) -> None:
        """/durum komutu: Açık pozisyonları özetler."""
        if not self.active_positions:
            self.notifier.send_message("ℹ️ Şu anda açık sanal pozisyon bulunmuyor.")
            return

        all_prices = self.client.get_all_prices()
        lines = ["📊 *AKTİF SANAL POZİSYONLAR:*", "━━━━━━━━━━━━━━━━━━━━━"]
        now = time.time()

        for sym, pos in self.active_positions.items():
            curr_p = all_prices.get(sym) or pos.entry_price
            is_short = (pos.side == "SHORT")
            if is_short:
                pnl = ((pos.entry_price - curr_p) / pos.entry_price) * 100.0
            else:
                pnl = ((curr_p - pos.entry_price) / pos.entry_price) * 100.0

            pnl_usd = pos.amount_usdt * (pnl / 100.0)
            duration = (now - pos.entry_time) / 60.0
            emoji = "🟢" if pnl >= 0 else "🔴"
            be_str = " (🛡️ BE Aktif)" if pos.break_even_active else ""
            tr_str = " (🎯 Trailing Aktif)" if pos.trailing_active else ""

            sr_part = ""
            if pos.nearest_resistance or pos.nearest_support or pos.poc_price:
                res_txt = f"${pos.nearest_resistance:.4f}" if pos.nearest_resistance else "Açık"
                sup_txt = f"${pos.nearest_support:.4f}" if pos.nearest_support else "Açık"
                poc_txt = f"${pos.poc_price:.4f}" if pos.poc_price else "Yok"
                sr_part = f"\n   ↳ Destek: `{sup_txt}` | Direnç: `{res_txt}` | POC: `{poc_txt}`"

            lines.append(
                f"{emoji} *{sym}* `[{pos.side}]`: `${curr_p:.4f}` | PnL: *%{pnl:+.2f}* (*${pnl_usd:+.2f}*)\n"
                f"   ↳ Giriş: `${pos.entry_price:.4f}` | Süre: `{duration:.1f} dk`{be_str}{tr_str}{sr_part}"
            )
        lines.append("━━━━━━━━━━━━━━━━━━━━━")
        self.notifier.send_message("\n".join(lines))

    def _handle_cmd_report(self) -> None:
        """/rapor komutu: paper_trades.csv üzerinden genel işlem karnesini döner."""
        if not os.path.exists(CSV_PATH):
            self.notifier.send_message("ℹ️ Henüz tamamlanmış işlem kaydı bulunmuyor.")
            return

        try:
            df = pd.read_csv(CSV_PATH)
            if df.empty:
                self.notifier.send_message("ℹ️ Henüz tamamlanmış işlem kaydı bulunmuyor.")
                return

            if "pnl_pct" not in df.columns:
                self.notifier.send_message("⚠️ CSV defterinde 'pnl_pct' sütunu bulunamadı.")
                return

            # Sayısal dönüşümleri zorunlu yap (hatalı / kaymış satırları güvenle ayıkla)
            df["pnl_pct"] = pd.to_numeric(df["pnl_pct"], errors="coerce")
            df = df.dropna(subset=["pnl_pct"])

            if df.empty:
                self.notifier.send_message("ℹ️ Henüz tamamlanmış geçerli işlem kaydı bulunmuyor.")
                return

            total_trades = len(df)
            winning = df[df["pnl_pct"] > 0]
            losing = df[df["pnl_pct"] <= 0]

            win_rate = (len(winning) / total_trades) * 100.0 if total_trades > 0 else 0.0

            if "pnl_usd" in df.columns:
                df["pnl_usd"] = pd.to_numeric(df["pnl_usd"], errors="coerce").fillna(0.0)
                total_pnl_usd = float(df["pnl_usd"].sum())
            else:
                total_pnl_usd = float(df["pnl_pct"].sum())

            if "commission_usd" in df.columns:
                df["commission_usd"] = pd.to_numeric(df["commission_usd"], errors="coerce").fillna(0.0)
                total_comm_usd = float(df["commission_usd"].sum())
            else:
                total_comm_usd = 0.0

            best_trade = float(df["pnl_pct"].max())
            worst_trade = float(df["pnl_pct"].min())

            mfe_col = "mfe_pct" if "mfe_pct" in df.columns else ("peak_pnl_pct" if "peak_pnl_pct" in df.columns else None)
            mfe_line = ""
            if mfe_col:
                avg_mfe = float(pd.to_numeric(df[mfe_col], errors="coerce").fillna(0.0).mean())
                mfe_line = f"• Ortalama Zirve Kâr (MFE): `+%{avg_mfe:.2f}`\n"

            mae_line = ""
            if "mae_pct" in df.columns:
                avg_mae = float(pd.to_numeric(df["mae_pct"], errors="coerce").fillna(0.0).mean())
                mae_line = f"• Ortalama En Derin Düşüş (MAE): `%{avg_mae:.2f}`\n"

            msg = (
                "📈 *GENEL PAPER TRADING PERFORMANS RAPORU*\n"
                "━━━━━━━━━━━━━━━━━━━━━\n"
                f"• Toplam Tamamlanan İşlem: `{total_trades}` adet\n"
                f"• Kazanma Oranı (Win Rate): `%{win_rate:.1f}`\n"
                f"• Kazançlı / Kayıplı: `{len(winning)}` / `{len(losing)}`\n"
                f"• Net Toplam Kâr/Zarar: *${total_pnl_usd:+.2f} USDT*\n"
                f"• Ödenen Sanal Komisyon: `${total_comm_usd:.2f} USDT`\n"
                f"{mfe_line}"
                f"{mae_line}"
                f"• En İyi İşlem: `%{best_trade:+.2f}`\n"
                f"• En Kötü İşlem: `%{worst_trade:+.2f}`\n"
                "━━━━━━━━━━━━━━━━━━━━━\n"
                "📁 Detaylar: `paper_trades.csv`"
            )
            self.notifier.send_message(msg)
        except Exception as e:
            logger.error("Rapor oluşturma hatası: %s", e)
            self.notifier.send_message(f"⚠️ Rapor hesaplanırken hata oluştu: {e}")

    def _handle_cmd_close(self, text: str) -> None:
        """
        /kapat <SEMBOL> veya /kapat hepsi:
        Açık sanal pozisyonları Telegram komutuyla anında manuel kapatır.
        """
        parts = text.strip().split()
        if len(parts) < 2:
            self.notifier.send_message("⚠️ Kullanım: `/kapat <SEMBOL>` (örn: `/kapat SOLUSDT`) veya `/kapat hepsi`")
            return

        target = parts[1].upper().strip()
        if not self.active_positions:
            self.notifier.send_message("ℹ️ Kapatılacak açık sanal pozisyon bulunmuyor.")
            return

        all_prices = self.client.get_all_prices()
        commission_pct = float(self.config.get("commission_pct", 0.12))
        now = time.time()

        targets_to_close = []
        if target in ["HEPSI", "ALL", "*"]:
            targets_to_close = list(self.active_positions.keys())
        else:
            if not target.endswith("USDT"):
                target += "USDT"
            if target in self.active_positions:
                targets_to_close = [target]
            else:
                self.notifier.send_message(f"⚠️ `{target}` sembolünde açık pozisyon bulunamadı.")
                return

        closed_count = 0
        for sym in targets_to_close:
            pos = self.active_positions.pop(sym, None)
            if not pos:
                continue

            curr_price = all_prices.get(sym) or self.client.get_latest_price(sym) or pos.entry_price
            duration_minutes = (now - pos.entry_time) / 60.0
            is_short = (pos.side == "SHORT")

            if is_short:
                gross_pnl_pct = ((pos.entry_price - curr_price) / pos.entry_price) * 100.0
            else:
                gross_pnl_pct = ((curr_price - pos.entry_price) / pos.entry_price) * 100.0

            net_pnl_pct = gross_pnl_pct - commission_pct
            net_pnl_usd = pos.amount_usdt * (net_pnl_pct / 100.0)
            comm_usd = pos.amount_usdt * (commission_pct / 100.0)

            trade_record = {
                "timestamp": datetime.now().isoformat(),
                "symbol": sym,
                "side": pos.side,
                "entry_price": round(pos.entry_price, 6),
                "exit_price": round(curr_price, 6),
                "qty": round(pos.qty, 6),
                "amount_usdt": round(pos.amount_usdt, 2),
                "gross_pnl_pct": round(gross_pnl_pct, 2),
                "pnl_pct": round(net_pnl_pct, 2),
                "pnl_usd": round(net_pnl_usd, 2),
                "commission_usd": round(comm_usd, 2),
                "duration_minutes": round(duration_minutes, 1),
                "exit_reason": "MANUEL_TELEGRAM (/kapat)",
                "mfe_pct": round(pos.peak_pnl_pct, 2),
                "mae_pct": round(pos.mae_pnl_pct, 2)
            }

            self._log_trade_to_csv(trade_record)
            self.notifier.notify_trade_closed(trade_record)
            closed_count += 1

        self._save_positions_to_disk()
        if closed_count > 0:
            logger.info("Telegram /kapat komutu ile %d adet pozisyon kapatıldı.", closed_count)

    def update_and_check_positions(self) -> None:
        """
        Açık sanal pozisyonları günceller:
        - Batch fiyat çekme (tek HTTP isteği ile tüm coinlerin fiyatı güncellenir).
        - Break-Even (Başabaş) kontrolü (+%1.5 görüldüyse stop girişe çekilir).
        - Stop-Loss, Trailing TP ve Zaman Aşımı kontrolleri.
        - Komisyon düşülmüş net PnL hesabı.
        """
        if not self.active_positions:
            return

        symbols_to_close = []
        now = time.time()
        commission_pct = float(self.config.get("commission_pct", 0.12))
        be_trigger = float(self.config.get("break_even_trigger_pct", 1.5))
        be_lock = float(self.config.get("break_even_lock_pct", 0.20))

        # Tek seferde tüm fiyatları çek (Batch Update - Rate limit tasarrufu)
        all_prices = self.client.get_all_prices()

        for symbol, pos in list(self.active_positions.items()):
            curr_price = all_prices.get(symbol) or self.client.get_latest_price(symbol)
            if curr_price is None or curr_price <= 0:
                continue

            entry_price = pos.entry_price
            is_short = (pos.side == "SHORT")

            if is_short:
                raw_pnl_pct = ((entry_price - curr_price) / entry_price) * 100.0
                be_trigger = float(self.config.get("short_break_even_trigger_pct", 1.0))
                be_lock = float(self.config.get("short_break_even_lock_pct", 0.15))
            else:
                raw_pnl_pct = ((curr_price - entry_price) / entry_price) * 100.0
                be_trigger = float(self.config.get("break_even_trigger_pct", 1.5))
                be_lock = float(self.config.get("break_even_lock_pct", 0.20))

            duration_minutes = (now - pos.entry_time) / 60.0

            # 1. Zirve kâr (MFE) ve dip zarar (MAE) takibi
            if pos.peak_price == 0.0:
                pos.peak_price = entry_price

            if raw_pnl_pct > pos.peak_pnl_pct:
                pos.peak_pnl_pct = raw_pnl_pct

            if raw_pnl_pct < pos.mae_pnl_pct:
                pos.mae_pnl_pct = raw_pnl_pct

            if is_short:
                if curr_price < pos.peak_price:
                    pos.peak_price = curr_price
            else:
                if curr_price > pos.peak_price:
                    pos.peak_price = curr_price

            # 2. Başabaş (Break-Even) Koruma Kontrolü
            if not pos.break_even_active and pos.peak_pnl_pct >= be_trigger:
                pos.break_even_active = True
                pos.sl_pct = -be_lock  # Stop-loss'u kârlı bölgeye (+%0.20 net kâra) taşı
                logger.info("🛡️ [%s (%s)] Break-Even Aktifleşti! Zirve: %%%.2f | Yeni Stop Seviyesi: +%%%.2f",
                            symbol, pos.side, pos.peak_pnl_pct, be_lock)
                self.notifier.notify_break_even(symbol, entry_price, pos.peak_pnl_pct, side=pos.side)

            # 3. Trailing TP Tetikleme Kontrolü
            if not pos.trailing_active and pos.peak_pnl_pct >= pos.tp_trigger_pct:
                pos.trailing_active = True
                logger.info("🎯 [%s (%s)] Trailing TP Aktifleşti! Zirve Kâr: %%%.2f", symbol, pos.side, pos.peak_pnl_pct)

            # 4. Çıkış Koşulları Değerlendirmesi
            exit_reason = None
            exit_price = curr_price

            # Koşul A: Stop-Loss (veya Break-Even Stop)
            if raw_pnl_pct <= -pos.sl_pct:
                exit_reason = "BREAK_EVEN" if pos.break_even_active else "STOP_LOSS"
                if is_short:
                    exit_price = entry_price * (1.0 + pos.sl_pct / 100.0)
                else:
                    exit_price = entry_price * (1.0 - pos.sl_pct / 100.0)

            # Koşul B: Trailing Stop Gerçekleşmesi
            elif pos.trailing_active:
                if is_short:
                    trailing_stop_price = pos.peak_price * (1.0 + pos.tp_delta_pct / 100.0)
                    if curr_price >= trailing_stop_price:
                        exit_reason = "TRAILING_TP"
                        exit_price = trailing_stop_price
                else:
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
                if is_short:
                    gross_pnl_pct = ((entry_price - exit_price) / entry_price) * 100.0
                else:
                    gross_pnl_pct = ((exit_price - entry_price) / entry_price) * 100.0

                net_pnl_pct = gross_pnl_pct - commission_pct
                net_pnl_usd = pos.amount_usdt * (net_pnl_pct / 100.0)
                comm_usd = pos.amount_usdt * (commission_pct / 100.0)

                trade_record = {
                    "timestamp": datetime.now().isoformat(),
                    "symbol": symbol,
                    "side": pos.side,
                    "entry_price": round(entry_price, 6),
                    "exit_price": round(exit_price, 6),
                    "qty": round(pos.qty, 6),
                    "amount_usdt": round(pos.amount_usdt, 2),
                    "gross_pnl_pct": round(gross_pnl_pct, 2),
                    "pnl_pct": round(net_pnl_pct, 2),
                    "pnl_usd": round(net_pnl_usd, 2),
                    "commission_usd": round(comm_usd, 2),
                    "duration_minutes": round(duration_minutes, 1),
                    "exit_reason": exit_reason,
                    "mfe_pct": round(pos.peak_pnl_pct, 2),
                    "mae_pct": round(pos.mae_pnl_pct, 2)
                }

                logger.info("🔴 [%s (%s) Pozisyon Kapatıldı] Neden: %s | Net PnL: %%%.2f ($%.2f USDT) | Süre: %.1f dk",
                            symbol, pos.side, exit_reason, net_pnl_pct, net_pnl_usd, duration_minutes)

                self._log_trade_to_csv(trade_record)
                self.notifier.notify_trade_closed(trade_record)
                symbols_to_close.append(symbol)

        if symbols_to_close:
            for sym in symbols_to_close:
                self.active_positions.pop(sym, None)
            self._save_positions_to_disk()

    def scan_market(self) -> None:
        """
        En hacimli 25 saf kripto paritesini tarar ve 5 kademeli teyit filtresini değerlendirir:
        1. 5m Mum Hacmi > SMA(20) * volume_multiplier (Anomali)
        2. Taker Buy Volume >= %55 (Gerçek agresif alıcı baskısı teyidi)
        3. Mum Kapanış Gücü >= %60 (Üst fitil / fakeout / dumping eleme)
        4. Son 15 dakikada Open Interest artışı >= %2.0 (Taze kurumsal para teyidi)
        5. Top Trader L/S Oranı <= 2.2 (Sıkışma/Kalabalık long riski filtresi)
        6. Cooldown: Aynı pariteye 30 dakika içinde ikinci sinyal üretilmez
        """
        max_positions = int(self.config.get("max_concurrent_positions", 0))
        is_unlimited = (max_positions <= 0)
        current_open = len(self.active_positions)

        if not is_unlimited and current_open >= max_positions:
            logger.info("Maksimum eşzamanlı pozisyon sayısına ulaşıldı (%d/%d). Yeni tarama atlanıyor.",
                        current_open, max_positions)
            return

        top_n = int(self.config.get("top_pairs_count", 25))
        blacklist = set(self.config.get("blacklist", []))
        top_pairs = self.client.get_top_volume_pairs(limit=top_n, blacklist=blacklist)
        lim_str = "Sınırsız (Unlimited)" if is_unlimited else f"{current_open}/{max_positions}"
        logger.info("Piyasa taranıyor: Top %d parite analiz ediliyor (Açık Pozisyonlar: %d | Limit: %s)...",
                    len(top_pairs), current_open, lim_str)

        # 0. Adım: BTC "Şalter" Filtresi (Market Regime Circuit Breaker)
        # BTC 15m'de sert düşüyorsa veya 5m EMA(20) altında eziliyorsa altcoinlerde LONG açılmaz
        btc_breaker_enabled = bool(self.config.get("btc_circuit_breaker_enabled", True))
        btc_drop_threshold = float(self.config.get("btc_15m_drop_threshold_pct", -0.80))

        if btc_breaker_enabled:
            btc_df = self.client.get_klines("BTCUSDT", interval="5m", limit=30)
            if btc_df is not None and len(btc_df) >= 20:
                btc_closes = btc_df["close"]
                btc_curr = float(btc_closes.iloc[-1])
                btc_open_3bars = float(btc_df["open"].iloc[-3])
                btc_15m_pct = ((btc_curr - btc_open_3bars) / btc_open_3bars) * 100.0 if btc_open_3bars > 0 else 0.0
                btc_ema20 = float(btc_closes.ewm(span=20, adjust=False).mean().iloc[-1])

                is_risk_off = False
                risk_reason = ""
                if btc_15m_pct <= btc_drop_threshold:
                    is_risk_off = True
                    risk_reason = f"BTC son 15 dakikada %{btc_15m_pct:+.2f} düştü (Eşik: %{btc_drop_threshold:.2f})"
                elif btc_curr < (btc_ema20 * 0.997):
                    is_risk_off = True
                    risk_reason = f"BTC 5m EMA(20) altında sert satış baskısında (${btc_curr:.1f} < ${btc_ema20:.1f})"

                if is_risk_off:
                    logger.warning("⚠️ [BTC ŞALTERİ DEVREDE - RISK-OFF] %s. Altcoin piyasa taraması ve LONG girişleri geçici olarak durduruldu.", risk_reason)
                    return

        now = time.time()
        cooldown_sec = float(self.config.get("cooldown_minutes", 60)) * 60.0
        vol_mult_threshold = float(self.config.get("volume_multiplier", 2.8))
        min_oi_increase = float(self.config.get("oi_min_increase_pct", 2.0))
        max_ls_ratio = float(self.config.get("top_trader_ls_max", 2.2))
        min_ls_ratio = float(self.config.get("top_trader_ls_min", 0.65))
        allow_long = bool(self.config.get("allow_long", True))
        allow_short = bool(self.config.get("allow_short", True))
        min_taker_ratio = float(self.config.get("min_taker_buy_ratio", 0.58))
        min_close_str = float(self.config.get("min_close_strength", 0.65))
        max_24h_pump = float(self.config.get("max_24h_pump_pct", 60.0))
        max_allowed_atr = float(self.config.get("max_allowed_atr_pct", 4.5))

        # Portföy Yön Dengesi: Halihazırdaki açık yönlerin dağılımı
        open_shorts = sum(1 for p in self.active_positions.values() if p.side == "SHORT")
        open_longs = sum(1 for p in self.active_positions.values() if p.side == "LONG")

        for symbol in top_pairs:
            if not self.is_running:
                break

            # Kara liste kontrolü
            if symbol in blacklist:
                continue

            # Zaten pozisyonda mıyız?
            if symbol in self.active_positions:
                continue

            # Cooldown kontrolü (30 dk)
            if symbol in self.cooldowns:
                elapsed = now - self.cooldowns[symbol]
                if elapsed < cooldown_sec:
                    continue

            # 1. Filtre: 5m Hacim Anomalisi ve Mum Formasyonu
            df = self.client.get_klines(symbol, interval="5m", limit=288)
            if df is None or len(df) < 25:
                continue

            df["vol_sma"] = df["volume"].rolling(window=20).mean()
            last_candle = df.iloc[-1]
            prev_candle = df.iloc[-2]

            # Kapanmış mum teyidi veya güçlü anlık mum
            candle = last_candle if (last_candle["close"] != last_candle["open"]) else prev_candle
            vol_sma = candle["vol_sma"]
            if pd.isna(vol_sma) or vol_sma <= 0:
                continue

            actual_vol_mult = candle["volume"] / vol_sma
            if actual_vol_mult < vol_mult_threshold:
                continue

            is_bullish = candle["close"] > candle["open"]
            is_bearish = candle["close"] < candle["open"]

            target_side = None
            if allow_long and is_bullish:
                target_side = "LONG"
            elif allow_short and is_bearish:
                target_side = "SHORT"
            else:
                continue

            # Portföy Yön Dengesi: 4+ SHORT açıkken genel düşüş piyasasında ters yöne LONG açılmasını sınırla
            if target_side == "LONG" and open_shorts >= 4:
                fr = self.client.get_funding_rate(symbol)
                if fr is None or fr > -0.01:
                    logger.info("  ↳ [%s LONG] Portföyde %d adet SHORT pozisyon açık. Piyasa yön çatışması engellendi 🛑", symbol, open_shorts)
                    continue
            elif target_side == "SHORT" and open_longs >= 4:
                logger.info("  ↳ [%s SHORT] Portföyde %d adet LONG pozisyon açık. Piyasa yön çatışması engellendi 🛑", symbol, open_longs)
                continue

            # Parabolik 24s Pump Koruması (TRB / Absürt Şişme Tuzağı Engeli)
            if target_side == "LONG":
                chg_24h = self.client.get_24h_change_pct(symbol)
                if chg_24h is not None and chg_24h >= max_24h_pump:
                    logger.info("  ↳ [%s LONG] 24s Değişim Aşırı Yüksek (+%%%.1f >= +%%%.1f). Parabolik tepe / TRB tuzağı engellendi 🛑",
                                symbol, chg_24h, max_24h_pump)
                    continue

            # 2. Filtre: Taker Hacim Baskısı (Alıcı veya Satıcı Baskısı Oranı >= %58)
            taker_buy_vol = float(candle.get("taker_buy_volume", 0.0))
            if target_side == "LONG":
                taker_ratio = (taker_buy_vol / candle["volume"]) if candle["volume"] > 0 else 0.0
                if taker_ratio < min_taker_ratio:
                    logger.debug("[%s] LONG için alıcı baskısı yetersiz: %%%.1f < %%%.1f", symbol, taker_ratio * 100, min_taker_ratio * 100)
                    continue
            else:
                # SHORT: Satıcı hacmi = (Toplam Hacim - Taker Buy Hacmi)
                taker_sell_vol = max(0.0, candle["volume"] - taker_buy_vol)
                taker_ratio = (taker_sell_vol / candle["volume"]) if candle["volume"] > 0 else 0.0
                if taker_ratio < min_taker_ratio:
                    logger.debug("[%s] SHORT için satıcı baskısı yetersiz: %%%.1f < %%%.1f", symbol, taker_ratio * 100, min_taker_ratio * 100)
                    continue

            # 3. Filtre: Mum Kapanış Gücü (LONG için üst fitil reddi, SHORT için alt fitil reddi)
            candle_range = candle["high"] - candle["low"]
            if target_side == "LONG":
                close_strength = ((candle["close"] - candle["low"]) / candle_range) if candle_range > 0 else 1.0
            else:
                close_strength = ((candle["high"] - candle["close"]) / candle_range) if candle_range > 0 else 1.0

            if close_strength < min_close_str:
                logger.debug("[%s %s] Mum kapanış gücü yetersiz: %%%.1f < %%%.1f", symbol, target_side, close_strength * 100, min_close_str * 100)
                continue

            baskisi_str = "ALICI" if target_side == "LONG" else "SATICI"
            logger.info("🚨 [HACİM ANOMALİSİ + %s BASKISI TESPİT EDİLDİ] %s (%s): Hacim %.2fx SMA(20) | Baskı: %%%.1f | Kapanış Gücü: %%%.1f",
                        baskisi_str, symbol, target_side, actual_vol_mult, taker_ratio * 100, close_strength * 100)

            # 4. Filtre: 15m Açık Pozisyon (OI) Artışı >= %2.0
            # Fiyat düşerken veya yükselirken OI artışı kurumsal taze pozisyon açılışını teyit eder
            oi_change = self.client.get_open_interest_change(symbol)
            if oi_change is None:
                logger.debug("[%s] OI verisi alınamadı, atlanıyor.", symbol)
                continue

            if oi_change < min_oi_increase:
                logger.info("  ↳ [%s %s] OI Artışı yetersiz: +%%%.2f (Gereken: >= +%%%.2f). Reddedildi.",
                            symbol, target_side, oi_change, min_oi_increase)
                continue

            logger.info("  ↳ [%s %s] OI Teyidi BAŞARILI: +%%%.2f (Taze kurumsal para girişi doğrulandı) 🟢",
                        symbol, target_side, oi_change)

            # 5. Filtre: Top Trader Long/Short Oranı Sınırı
            ls_ratio = self.client.get_top_trader_long_short_ratio(symbol)
            if ls_ratio is None:
                logger.debug("[%s] L/S oranı verisi alınamadı, atlanıyor.", symbol)
                continue

            if target_side == "LONG":
                if ls_ratio > max_ls_ratio:
                    logger.info("  ↳ [%s LONG] Top Trader L/S Oranı çok yüksek: %.2f (Maks Eşik: %.2f - Kalabalık Long Riski). Reddedildi.",
                                symbol, ls_ratio, max_ls_ratio)
                    continue
                logger.info("  ↳ [%s LONG] L/S Teyidi BAŞARILI: %.2f <= %.2f (Aşırı kalabalık değil) 🟢",
                            symbol, ls_ratio, max_ls_ratio)
            else:
                if ls_ratio < min_ls_ratio:
                    logger.info("  ↳ [%s SHORT] Top Trader L/S Oranı çok düşük: %.2f (Min Eşik: %.2f - Kalabalık Short / Squeeze Riski). Reddedildi.",
                                symbol, ls_ratio, min_ls_ratio)
                    continue
                logger.info("  ↳ [%s SHORT] L/S Teyidi BAŞARILI: %.2f >= %.2f (Aşırı kalabalık değil) 🟢",
                            symbol, ls_ratio, min_ls_ratio)

            # 6. Filtre: Piyasa Yapısı (Destek / Direnç / POC / Room-to-Run)
            struct_info = None
            use_sr = bool(self.config.get("use_sr_filter", True))
            entry_price = float(candle["close"])

            if target_side == "SHORT":
                default_sl = float(self.config.get("short_sl_pct", 1.5))
                default_tp = float(self.config.get("short_tp_pct", 1.6))
                delta_pct = float(self.config.get("short_delta_pct", 0.4))
                timeout_m = int(self.config.get("short_timeout_minutes", 15))
            else:
                default_sl = float(self.config.get("stop_loss_pct", 2.0))
                default_tp = float(self.config.get("trailing_trigger_pct", 3.0))
                delta_pct = float(self.config.get("trailing_delta_pct", 0.6))
                timeout_m = int(self.config.get("position_timeout_minutes", 40))

            if use_sr:
                sr_window = int(self.config.get("sr_pivot_window", 5))
                sr_tol = float(self.config.get("sr_cluster_tolerance_pct", 0.4))
                min_room_res = float(self.config.get("min_room_to_resistance_pct", 1.2))
                min_room_sup = float(self.config.get("min_room_to_support_pct", 1.2))
                dyn_tp = bool(self.config.get("dynamic_tp_enabled", True))
                struct_sl = bool(self.config.get("structural_sl_enabled", True))
                prev_c = float(df["close"].iloc[-2]) if len(df) >= 2 else None

                use_htf = bool(self.config.get("use_htf_filter", True))
                df_1h = None
                if use_htf:
                    df_1h = self.client.get_klines(symbol, interval="1h", limit=100, cache_ttl_sec=300)

                use_atr_sl = bool(self.config.get("use_atr_dynamic_sl", True))
                atr_per = int(self.config.get("atr_period", 14))
                atr_mult = float(self.config.get("atr_sl_multiplier", 2.0))
                atr_tp_mult = float(self.config.get("atr_tp_multiplier", 2.5))
                min_sl = float(self.config.get("min_sl_pct", 1.0))
                max_sl = float(self.config.get("max_sl_pct", 3.5))
                max_candle_atr = float(self.config.get("max_candle_atr_ratio", 3.0))
                max_l_rsi = float(self.config.get("max_long_rsi", 78.0))
                min_s_rsi = float(self.config.get("min_short_rsi", 22.0))
                funding_rate = self.client.get_funding_rate(symbol)
                squeeze_th = float(self.config.get("squeeze_funding_rate_threshold", -0.01))

                struct_res = analyze_market_structure(
                    symbol=symbol,
                    df=df,
                    df_1h=df_1h,
                    current_price=entry_price,
                    prev_close=prev_c,
                    side=target_side,
                    window=sr_window,
                    tolerance_pct=sr_tol,
                    min_room_to_resistance_pct=min_room_res,
                    min_room_to_support_pct=min_room_sup,
                    default_tp_pct=default_tp,
                    default_sl_pct=default_sl,
                    dynamic_tp_enabled=dyn_tp,
                    structural_sl_enabled=struct_sl,
                    use_htf_filter=use_htf,
                    use_atr_dynamic_sl=use_atr_sl,
                    atr_period=atr_per,
                    atr_sl_multiplier=atr_mult,
                    atr_tp_multiplier=atr_tp_mult,
                    min_sl_pct=min_sl,
                    max_sl_pct=max_sl,
                    max_candle_atr_ratio=max_candle_atr,
                    max_long_rsi=max_l_rsi,
                    min_short_rsi=min_s_rsi,
                    funding_rate=funding_rate,
                    squeeze_threshold=squeeze_th
                )
                struct_info = struct_res.to_dict()

                if not struct_res.has_room_to_run:
                    logger.info("  ↳ [%s %s] %s. İşlem İptal Edildi 🛑", symbol, target_side, struct_res.rejection_reason)
                    continue

                if struct_res.atr_pct > max_allowed_atr:
                    logger.info("  ↳ [%s %s] ATR(14) aşırı yüksek (%%%.2f > %%%.2f). Tahta delinmesi / kontrolsüz volatilite riski. Reddedildi 🛑",
                                symbol, target_side, struct_res.atr_pct, max_allowed_atr)
                    continue

                if target_side == "LONG":
                    if struct_res.is_short_squeeze:
                        logger.info("  ↳ [%s LONG] 🔥 SHORT SQUEEZE POTANSİYELİ: Fonlama Oranı %%%.4f ile sıkışan satıcılar! 🚀",
                                    symbol, funding_rate or 0.0)

                    if struct_res.is_breakout:
                        logger.info("  ↳ [%s LONG] ⚡ DİRENÇ KIRILIMI ONAYLANDI (Kırılan Direnç: $%.4f) 🟢",
                                    symbol, struct_res.broken_level or 0.0)
                    else:
                        res_str = f"+%{struct_res.dist_to_resistance_pct:.2f}" if struct_res.dist_to_resistance_pct else "Açık Yol"
                        logger.info("  ↳ [%s LONG] Yapısal & Makro Teyit BAŞARILI: Direnç %s | ATR(14): %%%.2f | RSI: %.1f | 1H: %s 🟢",
                                    symbol, res_str, struct_res.atr_pct, struct_res.rsi_value, struct_res.htf_bias)
                else:
                    if struct_res.is_rejection:
                        logger.info("  ↳ [%s SHORT] ⚡ %s TESPİT EDİLDİ (Dirençten Red / Fakeout Doğrulandı) 🟢",
                                    symbol, struct_res.rejection_source)
                    elif struct_res.is_breakdown:
                        logger.info("  ↳ [%s SHORT] ⚡ DESTEK KIRILIMI (BREAKDOWN) ONAYLANDI (Kırılan Destek: $%.4f) 🟢",
                                    symbol, struct_res.broken_level or 0.0)
                    else:
                        sup_str = f"-%{struct_res.dist_to_support_pct:.2f}" if struct_res.dist_to_support_pct else "Açık Taban"
                        logger.info("  ↳ [%s SHORT] Yapısal & Makro Teyit BAŞARILI: Destek %s | ATR(14): %%%.2f | RSI: %.1f | 1H: %s 🟢",
                                    symbol, sup_str, struct_res.atr_pct, struct_res.rsi_value, struct_res.htf_bias)

                sl_pct = struct_res.suggested_sl_pct
                tp_pct = struct_res.suggested_tp_pct
            else:
                sl_pct = default_sl
                tp_pct = default_tp

            # TÜM FİLTRELER SAĞLANDI -> SANAL POZİSYON AÇ (LONG / SHORT)
            trade_amount_usdt = float(self.config.get("trade_amount_usdt", 100.0))
            qty = (trade_amount_usdt / entry_price) if entry_price > 0 else 0.0

            if target_side == "LONG":
                sl_price = entry_price * (1.0 - sl_pct / 100.0)
                tp_trigger_price = entry_price * (1.0 + tp_pct / 100.0)
            else:
                sl_price = entry_price * (1.0 + sl_pct / 100.0)
                tp_trigger_price = entry_price * (1.0 - tp_pct / 100.0)

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
                side=target_side,
                amount_usdt=trade_amount_usdt,
                qty=qty,
                trailing_active=False,
                break_even_active=False,
                peak_price=entry_price,
                peak_pnl_pct=0.0,
                nearest_support=struct_info.get("nearest_support") if struct_info else None,
                nearest_resistance=struct_info.get("nearest_resistance") if struct_info else None,
                poc_price=struct_info.get("poc_price") if struct_info else None,
                trend_direction=struct_info.get("trend_direction", "Bilinmiyor") if struct_info else "Bilinmiyor"
            )

            self.active_positions[symbol] = new_pos
            self.cooldowns[symbol] = now  # 30 dk soğuma başlat
            self._save_positions_to_disk()  # Diske anlık kaydet

            side_emoji = "🟢" if target_side == "LONG" else "🔴"
            logger.info("✅ %s [YENİ SANAL %s] %s @ $%.4f ($%.2f USDT, Miktar: %.4f) | SL: $%.4f (%%%s) | TP Tetik: $%.4f (%%%s)",
                        side_emoji, target_side, symbol, entry_price, trade_amount_usdt, qty, sl_price, sl_pct, tp_trigger_price, tp_pct)

            # Telegram Sinyal Kartı İlet (Dinamik hesaplanan SL ve TP yüzdeleriyle)
            card_cfg = dict(self.config)
            card_cfg["stop_loss_pct"] = sl_pct
            card_cfg["trailing_trigger_pct"] = tp_pct

            self.notifier.notify_signal_card(
                symbol=symbol,
                entry_price=entry_price,
                vol_mult=actual_vol_mult,
                oi_change=oi_change,
                ls_ratio=ls_ratio,
                sl_price=sl_price,
                tp_trigger_price=tp_trigger_price,
                config=card_cfg,
                structure_info=struct_info,
                side=target_side
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

        # 4. Telegram İnteraktif Komut Dinleyicisini Başlat (/durum, /rapor)
        self.start_telegram_command_listener()

        scan_interval = int(self.config.get("scan_interval_seconds", 60))
        last_scan_time = 0.0

        logger.info("Ana tarama motoru hazır. Tarama aralığı: %d saniye.", scan_interval)

        while self.is_running:
            try:
                # Hot-Reload kontrolü (Sadece mtime değiştiğinde)
                self.reload_config_if_changed()

                # Pozisyon Takibi (Batch fiyat güncellemesiyle kâr/zarar ve çıkış değerlendirme)
                self.update_and_check_positions()

                now = time.time()
                scan_interval = int(self.config.get("scan_interval_seconds", 60))

                # 60 saniyede bir piyasa taraması
                if now - last_scan_time >= scan_interval:
                    self.scan_market()
                    last_scan_time = now

                # Pozisyon takibinin gecikmemesi için 5 sn bekleme
                time.sleep(5)

            except Exception as e:
                logger.error("Ana döngü sırasında beklenmeyen hata: %s", e, exc_info=True)
                time.sleep(10)

        logger.info("Quant Bot güvenli bir şekilde kapatıldı.")


if __name__ == "__main__":
    bot = PaperTradingBot()
    bot.run()
