"""
telegram_notifier.py
====================
Telegram Bildirim Motoru.
Anlık görsel emojili sinyal kartları, işlem kapanış raporları,
haftalık optimizasyon sonuçları ve sistem durumu bildirimlerini iletir.
"""

import logging
import requests
from typing import Dict, Any, Optional

logger = logging.getLogger("TelegramNotifier")

class TelegramNotifier:
    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = chat_id
        self.base_url = f"https://api.telegram.org/bot{self.token}"

    def send_message(self, text: str, parse_mode: str = "Markdown") -> bool:
        """
        Telegram API üzerinden mesaj gönderir.
        """
        if not self.token or not self.chat_id:
            logger.warning("Telegram token veya chat_id tanımlı değil!")
            return False

        url = f"{self.base_url}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True
        }

        try:
            resp = requests.post(url, json=payload, timeout=10)
            if resp.status_code == 200:
                return True
            else:
                logger.error("Telegram gönderim hatası: %d - %s", resp.status_code, resp.text)
                # Parse mode hatası varsa düz metin olarak tekrar dene
                if "can't parse entities" in resp.text:
                    payload.pop("parse_mode", None)
                    requests.post(url, json=payload, timeout=10)
                return False
        except Exception as e:
            logger.error("Telegram API bağlantı hatası: %s", e)
            return False

    def notify_model_updated(self, best_params: Dict[str, Any], stats: Dict[str, Any]) -> bool:
        """
        Optimizasyon tamamlandığında gönderilen model güncelleme kartı.
        Kullanıcı gereksinimindeki zorunlu şablon:
        ⚙️ *HAFTALIK MODEL GÜNCELLENDİ*
        Son 14 gün analiz edildi.
        Yeni Hacim Eşiği: {X}x
        Yeni Stop-Loss: %{SL}
        Yeni Trailing TP: %{TP} (Delta: %{DELTA})
        """
        vol = best_params.get("volume_multiplier", 2.8)
        sl = best_params.get("stop_loss_pct", 2.0)
        tp = best_params.get("trailing_trigger_pct", 3.0)
        delta = best_params.get("trailing_delta_pct", 1.0)
        
        pf = stats.get("profit_factor", 0.0)
        wr = stats.get("win_rate", 0.0)
        mdd = stats.get("max_drawdown", 0.0)
        trades = stats.get("total_trades", 0)
        pairs_str = ", ".join(stats.get("pairs", []))

        text = (
            "⚙️ *HAFTALIK MODEL GÜNCELLENDİ*\n"
            "Son 14 gün analiz edildi.\n"
            f"Yeni Hacim Eşiği: {vol:.1f}x\n"
            f"Yeni Stop-Loss: %{sl:.1f}\n"
            f"Yeni Trailing TP: %{tp:.1f} (Delta: %{delta:.1f})\n\n"
            "📊 *Geriye Dönük Test Performansı:*\n"
            f"• Profit Factor: `{pf:.2f}`\n"
            f"• Kazanma Oranı (Win Rate): `%{wr:.1f}`\n"
            f"• Max Drawdown: `%{mdd:.2f}`\n"
            f"• Simüle Edilen İşlem: `{trades}` adet\n"
            f"• Analiz Edilen Pariteler: `{pairs_str}`"
        )
        return self.send_message(text)

    def notify_signal_card(
        self,
        symbol: str,
        entry_price: float,
        vol_mult: float,
        oi_change: float,
        ls_ratio: float,
        sl_price: float,
        tp_trigger_price: float,
        config: Dict[str, Any]
    ) -> bool:
        """
        Canlı piyasa tarayıcısından 3 kriter sağlandığında atılan görsel sinyal kartı.
        """
        sl_pct = config.get("stop_loss_pct", 2.0)
        tp_pct = config.get("trailing_trigger_pct", 3.0)
        delta_pct = config.get("trailing_delta_pct", 1.0)
        timeout_m = config.get("position_timeout_minutes", 40)
        max_ls = config.get("top_trader_ls_max", 2.2)

        # Fiyat formatlama (kuruş hassasiyeti)
        fmt = ".4f" if entry_price < 10 else ".2f"

        text = (
            "🟢 *YENİ SANAL LONG SİNYALİ AÇILDI*\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🪙 Parite: *{symbol}*\n"
            f"💵 Giriş Fiyatı: `${entry_price:{fmt}}`\n\n"
            "📊 *Anomali ve Teyit Metrikleri:*\n"
            f"• 5m Hacim Artışı: *{vol_mult:.2f}x SMA(20)* 🚀\n"
            f"• 15m Açık Pozisyon (OI): *+%{oi_change:.2f}* 🟢\n"
            f"• Top Trader L/S: *{ls_ratio:.2f}* (Kural: <= {max_ls})\n\n"
            "🎯 *Pozisyon Çıkış Parametreleri:*\n"
            f"• Stop-Loss (%{sl_pct}): `${sl_price:{fmt}}`\n"
            f"• Trailing TP Tetik (%{tp_pct}): `${tp_trigger_price:{fmt}}`\n"
            f"• Trailing Delta: `%{delta_pct}` (Zirveden çekilme)\n"
            f"• Zaman Aşımı: `{timeout_m} Dakika`\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "⚡ *Paper Trading Modu:* Emir sanal deftere işlendi."
        )
        return self.send_message(text)

    def notify_trade_closed(self, trade_info: Dict[str, Any]) -> bool:
        """
        Sanal pozisyon kapandığında gönderilen kâr/zarar bildirim kartı.
        """
        symbol = trade_info["symbol"]
        entry_price = trade_info["entry_price"]
        exit_price = trade_info["exit_price"]
        pnl_pct = trade_info["pnl_pct"]
        duration = trade_info.get("duration_minutes", 0)
        reason = trade_info.get("exit_reason", "Bilinmiyor")
        peak_pnl = trade_info.get("peak_pnl_pct", 0.0)

        fmt = ".4f" if entry_price < 10 else ".2f"
        
        is_win = pnl_pct > 0
        status_emoji = "🎉" if is_win else "🛑"
        pnl_emoji = "🟢 KÂR" if is_win else "🔴 ZARAR"

        text = (
            f"{status_emoji} *SANAL İŞLEM KAPANDI: {symbol}*\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📌 Kapanış Nedeni: *{reason}*\n"
            f"📥 Giriş Fiyatı: `${entry_price:{fmt}}`\n"
            f"📤 Çıkış Fiyatı: `${exit_price:{fmt}}`\n\n"
            "📈 *İşlem Performansı:*\n"
            f"• Net P/L: *%{pnl_pct:+.2f}* ({pnl_emoji})\n"
            f"• İşlem Süresi: `{duration:.1f} dakika`\n"
            f"• Görülen Zirve Kâr: `%{peak_pnl:+.2f}`\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "📁 İşlem kaydı `paper_trades.csv` dosyasına yazıldı."
        )
        return self.send_message(text)

    def notify_bot_started(self, config: Dict[str, Any]) -> bool:
        """
        Bot başladığında Telegram'a durum özeti gönderir.
        """
        text = (
            "🤖 *BINANCE QUANT BOT AKTİF EDİLDİ*\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "Mode: *Paper Trading (USDT-M Vadeli)*\n"
            "Mimari: *Walk-Forward Self-Optimizing*\n\n"
            "📋 *Aktif Parametreler (config.json):*\n"
            f"• Hacim Eşiği: `{config.get('volume_multiplier', 2.8)}x SMA(20)`\n"
            f"• Stop-Loss: `%{config.get('stop_loss_pct', 2.0)}`\n"
            f"• Trailing TP: `%{config.get('trailing_trigger_pct', 3.0)}` (Delta: `%{config.get('trailing_delta_pct', 1.0)}`)\n"
            f"• 15m OI Filtresi: `>= %{config.get('oi_min_increase_pct', 2.0)}`\n"
            f"• L/S Oran Filtresi: `<= {config.get('top_trader_ls_max', 2.2)}`\n"
            f"• Eşzamanlı Maks Pozisyon: `{config.get('max_concurrent_positions', 2)}`\n"
            f"• Zaman Aşımı: `{config.get('position_timeout_minutes', 40)} dk`\n"
            f"• Tarama Periyodu: `{config.get('scan_interval_seconds', 60)} sn`\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "🚀 Canlı piyasa taraması başlatıldı..."
        )
        return self.send_message(text)
