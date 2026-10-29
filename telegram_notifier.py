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

    def get_updates(self, offset: Optional[int] = None, timeout: int = 2) -> list:
        """
        Telegram gelen komut ve mesajları kontrol eder (/durum, /rapor vb.).
        """
        if not self.token:
            return []
        url = f"{self.base_url}/getUpdates"
        params: Dict[str, Any] = {"timeout": timeout}
        if offset is not None:
            params["offset"] = offset

        try:
            resp = requests.get(url, params=params, timeout=timeout + 5)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("ok"):
                    return data.get("result", [])
        except Exception:
            pass
        return []

    def notify_break_even(self, symbol: str, entry_price: float, peak_pnl: float, side: str = "LONG") -> bool:
        """
        Break-Even (Başabaş) koruma tetiklendiğinde bilgilendirme atar.
        """
        fmt = ".4f" if entry_price < 10 else ".2f"
        side_upper = side.upper()
        dir_text = "altına (-%0.20 net kâra)" if side_upper == "SHORT" else "üzerine (+%0.20 net kâra)"
        text = (
            f"🛡️ *BAŞABAŞ (BREAK-EVEN) AKTİF: {symbol} ({side_upper})*\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Giriş Fiyatı: `${entry_price:{fmt}}`\n"
            f"Zirve Kâr: `+%{peak_pnl:.2f}`\n\n"
            f"✅ *Stop-Loss seviyesi giriş fiyatının {dir_text} taşındı.*\n"
            "Bu işlem artık zararla kapanamaz."
        )
        return self.send_message(text)

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
        config: Dict[str, Any],
        structure_info: Optional[Dict[str, Any]] = None,
        side: str = "LONG"
    ) -> bool:
        """
        Canlı piyasa tarayıcısından kriterler sağlandığında atılan görsel sinyal kartı (LONG / SHORT).
        """
        sl_pct = config.get("stop_loss_pct", 2.0)
        tp_pct = config.get("trailing_trigger_pct", 3.0)
        delta_pct = config.get("trailing_delta_pct", 1.0)
        timeout_m = config.get("position_timeout_minutes", 40)
        max_ls = config.get("top_trader_ls_max", 2.2)
        min_ls = config.get("top_trader_ls_min", 0.65)
        trade_amount = float(config.get("trade_amount_usdt", 100.0))
        qty = (trade_amount / entry_price) if entry_price > 0 else 0.0

        side_upper = side.upper()
        is_short = (side_upper == "SHORT")

        # Fiyat formatlama (kuruş hassasiyeti)
        fmt = ".4f" if entry_price < 10 else ".2f"
        qty_fmt = ".2f" if qty >= 10 else (".4f" if qty >= 0.1 else ".6f")
        base_coin = symbol.replace("USDT", "")

        struct_section = ""
        if structure_info:
            nearest_sup = structure_info.get("nearest_support")
            sup_dist = structure_info.get("dist_to_support_pct")
            nearest_res = structure_info.get("nearest_resistance")
            res_dist = structure_info.get("dist_to_resistance_pct")
            poc_p = structure_info.get("poc_price")
            trend_dir = structure_info.get("trend_direction", "Bilinmiyor")
            trend_slope = structure_info.get("trend_slope_hourly_pct", 0.0)
            is_breakout = structure_info.get("is_breakout", False)
            is_breakdown = structure_info.get("is_breakdown", False)

            sup_str = f"${nearest_sup:{fmt}} (-%{sup_dist:.2f})" if nearest_sup else "Açık Taban"
            res_str = f"${nearest_res:{fmt}} (+%{res_dist:.2f})" if nearest_res else "Açık Tavan"
            poc_str = f"${poc_p:{fmt}}" if (poc_p and poc_p > 0) else "Yok"

            break_tag = ""
            if is_breakout:
                break_tag = " ⚡ *[DİRENÇ KIRILIMI - BREAKOUT]*"
            elif is_breakdown:
                break_tag = " ⚡ *[DESTEK KIRILIMI - BREAKDOWN]*"

            htf_bias = structure_info.get("htf_bias", "NEUTRAL")
            htf_desc = structure_info.get("htf_desc", "")
            atr_pct = structure_info.get("atr_pct", 0.0)
            rsi_val = structure_info.get("rsi_value", 50.0)

            htf_badge = "🟢 Boğa (Bullish)" if htf_bias == "BULLISH" else ("🔴 Ayı (Bearish)" if htf_bias == "BEARISH" else "⚪ Nötr")

            struct_section = (
                f"🧱 *Piyasa Yapısı & Makro Rejim:*{break_tag}\n"
                f"• 1H Makro Trend: *{htf_badge}*\n"
                f"• Volatilite (ATR-14): `%{atr_pct:.2f}` | RSI(14): `{rsi_val:.1f}`\n"
                f"• En Yakın Destek: `{sup_str}`\n"
                f"• Hacim POC: `{poc_str}`\n"
                f"• En Yakın Direnç: `{res_str}`\n"
                f"• 5m Trend: *{trend_dir}* (`{trend_slope:+.2f}%/saat`)\n\n"
            )

        title = "🔴 *YENİ SANAL SHORT SİNYALİ AÇILDI*" if is_short else "🟢 *YENİ SANAL LONG SİNYALİ AÇILDI*"
        action_name = "SHORT (Düşüş Yönlü)" if is_short else "LONG (Yükseliş Yönlü)"
        ls_rule = f">={min_ls}" if is_short else f"<={max_ls}"
        sl_sign = "+" if is_short else "-"
        tp_sign = "-" if is_short else "+"

        text = (
            f"{title}\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🪙 Parite: *{symbol}* | Yön: *{action_name}*\n"
            f"💵 Giriş Fiyatı: `${entry_price:{fmt}}`\n"
            f"💰 Pozisyon Tutarı: *${trade_amount:.2f} USDT*\n"
            f"📦 Sanal Miktar: *{qty:{qty_fmt}} {base_coin}*\n\n"
            "📊 *Anomali ve Teyit Metrikleri:*\n"
            f"• 5m Hacim Artışı: *{vol_mult:.2f}x SMA(20)* 🚀\n"
            f"• 15m Açık Pozisyon (OI): *+%{oi_change:.2f}* 🟢\n"
            f"• Top Trader L/S: *{ls_ratio:.2f}* (Kural: {ls_rule})\n\n"
            f"{struct_section}"
            "🎯 *Pozisyon Çıkış Parametreleri:*\n"
            f"• Stop-Loss ({sl_sign}%{sl_pct}): `${sl_price:{fmt}}`\n"
            f"• Trailing TP Tetik ({tp_sign}%{tp_pct}): `${tp_trigger_price:{fmt}}`\n"
            f"• Trailing Delta: `%{delta_pct}` (Kârdan sarkma)\n"
            f"• Zaman Aşımı: `{timeout_m} Dakika`\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "⚡ *Paper Trading Modu:* $100'lık sanal emir deftere işlendi."
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
        pnl_usd = trade_info.get("pnl_usd", 0.0)
        amount_usdt = trade_info.get("amount_usdt", 100.0)
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
            f"📤 Çıkış Fiyatı: `${exit_price:{fmt}}`\n"
            f"💵 İşlem Büyüklüğü: `${amount_usdt:.2f} USDT`\n\n"
            "📈 *İşlem Performansı:*\n"
            f"• Net P/L (Komisyon Düşüldü): *%{pnl_pct:+.2f}* (*${pnl_usd:+.2f} USDT*) {pnl_emoji}\n"
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
        max_pos = config.get("max_concurrent_positions", 0)
        max_pos_str = "Sınırsız (Unlimited) ♾️" if (not max_pos or int(max_pos) <= 0) else f"{max_pos} adet"
        trade_amt = float(config.get("trade_amount_usdt", 100.0))

        text = (
            "🤖 *BINANCE QUANT BOT AKTİF EDİLDİ*\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "Mode: *Paper Trading (USDT-M Vadeli)*\n"
            "Mimari: *Walk-Forward Self-Optimizing*\n\n"
            "📋 *Aktif Parametreler (config.json):*\n"
            f"• Sabit İşlem Büyüklüğü: `${trade_amt:.2f} USDT / işlem` 💵\n"
            f"• Eşzamanlı İşlem Limiti: `{max_pos_str}`\n"
            f"• Hacim Eşiği: `{config.get('volume_multiplier', 2.8)}x SMA(20)`\n"
            f"• Stop-Loss: `%{config.get('stop_loss_pct', 2.0)}`\n"
            f"• Trailing TP: `%{config.get('trailing_trigger_pct', 3.0)}` (Delta: `%{config.get('trailing_delta_pct', 1.0)}`)\n"
            f"• Break-Even (Başabaş): `+%{config.get('break_even_trigger_pct', 1.5)} kârda aktif` 🛡️\n"
            f"• Taker Komisyon Hesabı: `%{config.get('commission_pct', 0.12)} / işlem` 🏷️\n"
            f"• 15m OI Filtresi: `>= %{config.get('oi_min_increase_pct', 2.0)}`\n"
            f"• L/S Oran Filtresi: `<= {config.get('top_trader_ls_max', 2.2)}`\n"
            f"• Zaman Aşımı: `{config.get('position_timeout_minutes', 40)} dk`\n"
            f"• Tarama Periyodu: `{config.get('scan_interval_seconds', 60)} sn`\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "💬 *Komutlar:* `/durum` (Açık işlemler), `/rapor` (İşlem karnesi)\n"
            "🚀 Canlı piyasa taraması başlatıldı..."
        )
        return self.send_message(text)
