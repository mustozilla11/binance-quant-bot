"""
market_regime.py
================
Kripto Vadeli Piyasası Otomatik Rejim Tespiti ve Dinamik Parametre Yöneticisi.

Piyasa Rejimleri:
- BOĞA (BULL): Güçlü yukarı trend, geniş likidite, momentum takibi (3.0x ATR TP, 0.8% trailing delta, 5 pozisyon).
- AYI (BEAR): Sert aşağı trend veya satış dalgası (sadece teyitli short squeeze'de LONG, 3 pozisyon).
- YATAY (CHOP): Yönsüz testere piyasası, sahte kırılımlar yaygın (2.0x ATR TP, sıkı sweep filtresi, 3 pozisyon).
- BELİRSİZ (UNCERTAIN): Rejim geçiş evresi, temkinli savunma modu (2 pozisyon, sıkı koruma).

Göstergeler (Saf Pandas / NumPy):
1. BTC 4H EMA50 / EMA200 Seviyesi & EMA50 Eğimi
2. BTC 4H ADX(14) Trend Gücü (ADX > 25 = Trend, ADX < 20 = Yatay)
3. Piyasa Genişliği (Top 15-20 coinin EMA50 üzerindeki oranı)
4. Ortalama Fonlama Oranı (Funding Rate) Aşırılığı
5. 3 Aşamalı Histerezis (Gereksiz zikzak ve rejim yalpalamasını önleme)
"""

import os
import json
import time
import logging
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Dict, Any, Optional, Tuple, List
import numpy as np
import pandas as pd

from binance_client import BinanceFuturesClient

logger = logging.getLogger("MarketRegime")

REGIME_STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "regime_state.json")


def calculate_adx(df: pd.DataFrame, period: int = 14) -> float:
    """
    Wilder's ADX (Average Directional Index) hesaplaması (Saf Pandas/NumPy).
    Dönen değer 0 ile 100 arasındadır:
    - ADX < 20: Zayıf / Yönsüz Yatay Piyasa (CHOP)
    - 20 <= ADX < 25: Kararsız Bölge
    - ADX >= 25: Güçlü Trend (Boğa veya Ayı)
    """
    if df is None or len(df) < period * 2:
        return 0.0

    high = df["high"].values
    low = df["low"].values
    close = df["close"].values

    up_move = high[1:] - high[:-1]
    down_move = low[:-1] - low[1:]

    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    tr1 = high[1:] - low[1:]
    tr2 = np.abs(high[1:] - close[:-1])
    tr3 = np.abs(low[1:] - close[:-1])
    tr = np.maximum(tr1, np.maximum(tr2, tr3))

    alpha = 1.0 / float(period)
    tr_s = pd.Series(tr).ewm(alpha=alpha, adjust=False).mean().values
    plus_dm_s = pd.Series(plus_dm).ewm(alpha=alpha, adjust=False).mean().values
    minus_dm_s = pd.Series(minus_dm).ewm(alpha=alpha, adjust=False).mean().values

    tr_s = np.where(tr_s == 0, 1e-9, tr_s)
    plus_di = 100.0 * (plus_dm_s / tr_s)
    minus_di = 100.0 * (minus_dm_s / tr_s)

    sum_di = plus_di + minus_di
    sum_di = np.where(sum_di == 0, 1e-9, sum_di)
    dx = 100.0 * np.abs(plus_di - minus_di) / sum_di
    adx_series = pd.Series(dx).ewm(alpha=alpha, adjust=False).mean().values

    if len(adx_series) == 0 or np.isnan(adx_series[-1]):
        return 0.0
    return round(float(adx_series[-1]), 2)


@dataclass
class RegimeAnalysis:
    regime: str                    # "BULL", "BEAR", "CHOP", "UNCERTAIN"
    score: int                     # -4 ile +4 arası toplam rejim puanı
    btc_price: float
    btc_ema50: float
    btc_ema200: float
    btc_slope_pct: float
    adx_value: float
    market_breadth_pct: float      # EMA50 üstü parite yüzdesi
    avg_funding_rate: float
    raw_candidate: str             # Histerezis öncesi aday rejim
    consecutive_count: int         # Adayın kaç kontroldür devam ettiği
    is_manual: bool
    timestamp: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class MarketRegimeDetector:
    """
    Piyasa rejimini periyodik olarak analiz eden ve bot parametrelerini uyarlayan yönetici.
    """

    def __init__(self, client: BinanceFuturesClient, notifier=None):
        self.client = client
        self.notifier = notifier
        self.current_regime: str = "UNCERTAIN"
        self.candidate_regime: str = "UNCERTAIN"
        self.consecutive_candidate_count: int = 0
        self.enabled: bool = True
        self.manual_override: Optional[str] = None
        self.regime_start_time: float = time.time()
        self.last_checked_timestamp: float = 0.0
        self.check_interval_seconds: int = 900  # 15 dakikada bir kontrol
        self.last_analysis: Optional[RegimeAnalysis] = None

        self._load_state()

    def _load_state(self) -> None:
        """Kalıcı durumu dosyadan yükler."""
        if os.path.exists(REGIME_STATE_PATH):
            try:
                with open(REGIME_STATE_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.current_regime = data.get("current_regime", "UNCERTAIN")
                    self.candidate_regime = data.get("candidate_regime", "UNCERTAIN")
                    self.consecutive_candidate_count = int(data.get("consecutive_candidate_count", 0))
                    self.enabled = bool(data.get("enabled", True))
                    self.manual_override = data.get("manual_override")
                    self.regime_start_time = float(data.get("regime_start_time", time.time()))
                    logger.info("Kayıtlı rejim durumu yüklendi: %s (Manuel: %s, Aktif: %s)",
                                self.current_regime, self.manual_override, self.enabled)
            except Exception as e:
                logger.error("Rejim durumu yükleme hatası: %s", e)

    def _save_state(self) -> None:
        """Kalıcı durumu dosyaya yazar."""
        try:
            data = {
                "current_regime": self.current_regime,
                "candidate_regime": self.candidate_regime,
                "consecutive_candidate_count": self.consecutive_candidate_count,
                "enabled": self.enabled,
                "manual_override": self.manual_override,
                "regime_start_time": self.regime_start_time,
                "updated_at": datetime.now().isoformat()
            }
            with open(REGIME_STATE_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=4)
        except Exception as e:
            logger.error("Rejim durumu kaydetme hatası: %s", e)

    def update(self, force: bool = False) -> RegimeAnalysis:
        """
        Piyasa rejimini günceller.
        force=True ise zaman aşımını beklemeden anında hesaplar.
        """
        now = time.time()
        if not force and (now - self.last_checked_timestamp < self.check_interval_seconds) and self.last_analysis:
            return self.last_analysis

        self.last_checked_timestamp = now

        # Manuel zorlama modu aktifse
        if not self.enabled or self.manual_override:
            effective = self.manual_override if self.manual_override else "UNCERTAIN"
            analysis = RegimeAnalysis(
                regime=effective,
                score=0,
                btc_price=0.0,
                btc_ema50=0.0,
                btc_ema200=0.0,
                btc_slope_pct=0.0,
                adx_value=0.0,
                market_breadth_pct=0.0,
                avg_funding_rate=0.0,
                raw_candidate=effective,
                consecutive_count=0,
                is_manual=True,
                timestamp=datetime.now().isoformat()
            )
            self.last_analysis = analysis
            return analysis

        # 1. BTC 4H Verilerini ve Göstergelerini Çek
        df_btc = self.client.get_klines("BTCUSDT", interval="4h", limit=100, cache_ttl_sec=300)
        btc_price = 0.0
        btc_ema50 = 0.0
        btc_ema200 = 0.0
        btc_slope = 0.0
        adx_val = 0.0
        btc_score = 0

        if df_btc is not None and len(df_btc) >= 50:
            closes = df_btc["close"]
            btc_price = float(closes.iloc[-1])
            ema50_series = closes.ewm(span=50, adjust=False).mean()
            ema200_series = closes.ewm(span=min(len(closes), 200), adjust=False).mean()
            btc_ema50 = float(ema50_series.iloc[-1])
            btc_ema200 = float(ema200_series.iloc[-1])

            # EMA50 eğimi (son 4 mum / ~16 saatlik değişim)
            prev_ema50 = float(ema50_series.iloc[-5]) if len(ema50_series) >= 5 else btc_ema50
            btc_slope = ((btc_ema50 - prev_ema50) / prev_ema50 * 100.0) if prev_ema50 > 0 else 0.0

            # ADX(14) Trend Gücü
            adx_val = calculate_adx(df_btc, period=14)

            # BTC Skorlama
            if btc_price >= btc_ema50 and btc_ema50 >= btc_ema200:
                btc_score += 1
            elif btc_price <= btc_ema50 and btc_ema50 <= btc_ema200:
                btc_score -= 1

            if btc_slope >= 0.20:
                btc_score += 1
            elif btc_slope <= -0.20:
                btc_score -= 1

        # 2. Piyasa Genişliği (Market Breadth - Top 15 paritenin EMA50 durumu)
        top_pairs = self.client.get_top_volume_pairs(limit=15)
        breadth_above = 0
        breadth_total = 0
        funding_sum = 0.0
        funding_count = 0

        for sym in top_pairs:
            # 1H verisi üzerinden paritenin EMA50 durumu
            df_sym = self.client.get_klines(sym, interval="1h", limit=55, cache_ttl_sec=300)
            if df_sym is not None and len(df_sym) >= 50:
                c = float(df_sym["close"].iloc[-1])
                e50 = float(df_sym["close"].ewm(span=50, adjust=False).mean().iloc[-1])
                if c >= e50:
                    breadth_above += 1
                breadth_total += 1

            fr = self.client.get_funding_rate(sym)
            if fr is not None:
                funding_sum += fr
                funding_count += 1

        breadth_pct = (breadth_above / breadth_total * 100.0) if breadth_total > 0 else 50.0
        avg_funding = (funding_sum / funding_count) if funding_count > 0 else 0.0

        # Genişlik Puanı
        breadth_score = 0
        if breadth_pct >= 65.0:
            breadth_score = +1
        elif breadth_pct <= 35.0:
            breadth_score = -1

        # Fonlama Puanı
        funding_score = 0
        if avg_funding <= -0.005:  # Aşırı short baskısı -> Short squeeze potansiyeli
            funding_score = +1
        elif avg_funding >= 0.035:  # Aşırı long şişmesi -> Aşırı kalabalık riski
            funding_score = -1

        total_score = btc_score + breadth_score + funding_score

        # 3. Ham Rejim Adayı Belirleme
        if adx_val < 20.0:
            raw_candidate = "CHOP"
        elif total_score >= 2 and adx_val >= 25.0:
            raw_candidate = "BULL"
        elif total_score <= -2 and adx_val >= 25.0:
            raw_candidate = "BEAR"
        elif -1 <= total_score <= 1:
            raw_candidate = "CHOP"
        else:
            raw_candidate = "UNCERTAIN"

        # 4. Histerezis ve Geçiş Durumu (Zikzak Önleme)
        old_regime = self.current_regime
        new_regime = old_regime

        if raw_candidate == old_regime:
            self.candidate_regime = raw_candidate
            self.consecutive_candidate_count = 0
        else:
            if raw_candidate == self.candidate_regime:
                self.consecutive_candidate_count += 1
            else:
                self.candidate_regime = raw_candidate
                self.consecutive_candidate_count = 1

            # Eğer sert yön değişimi varsa (örn: BULL -> BEAR), doğrudan atlamadan önce
            # 1. adımda hemen UNCERTAIN (Savunma Modu) yap
            if old_regime in ["BULL", "BEAR"] and raw_candidate != old_regime and self.consecutive_candidate_count == 1:
                new_regime = "UNCERTAIN"
                logger.info("⚠️ [REJİM ZAYIFLADI] %s rejiminden çıkılıyor, temkinli UNCERTAIN moduna geçildi.", old_regime)

            # 3 teyit üst üste gelirse yeni rejime tam geçiş yap
            if self.consecutive_candidate_count >= 3:
                new_regime = raw_candidate
                logger.info("🔄 [REJİM KİLİTLENDİ] 3 teyit tamamlandı. Yeni Rejim: %s", new_regime)
                self.consecutive_candidate_count = 0

        # Rejim gerçekten değiştiyse bildirim gönder ve kaydet
        if new_regime != old_regime:
            self.current_regime = new_regime
            self.regime_start_time = now
            self._save_state()
            self._notify_regime_change(old_regime, new_regime, total_score, adx_val, breadth_pct)

        analysis = RegimeAnalysis(
            regime=self.current_regime,
            score=total_score,
            btc_price=round(btc_price, 2),
            btc_ema50=round(btc_ema50, 2),
            btc_ema200=round(btc_ema200, 2),
            btc_slope_pct=round(btc_slope, 2),
            adx_value=round(adx_val, 2),
            market_breadth_pct=round(breadth_pct, 1),
            avg_funding_rate=round(avg_funding, 5),
            raw_candidate=raw_candidate,
            consecutive_count=self.consecutive_candidate_count,
            is_manual=False,
            timestamp=datetime.now().isoformat()
        )
        self.last_analysis = analysis
        return analysis

    def _notify_regime_change(self, old_r: str, new_r: str, score: int, adx: float, breadth: float) -> None:
        """Telegram'a rejim değişim uyarısı atar."""
        if not self.notifier:
            return

        emoji_map = {
            "BULL": "🟢 *BOĞA (BULL)*",
            "BEAR": "🔴 *AYI (BEAR)*",
            "CHOP": "🟡 *YATAY (CHOP)*",
            "UNCERTAIN": "⚪ *BELİRSİZ (GEÇİŞ / SAVUNMA)*"
        }

        old_str = emoji_map.get(old_r, old_r)
        new_str = emoji_map.get(new_r, new_r)

        msg = (
            "🧭 *PİYASA REJİMİ DEĞİŞTİ!*\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            f"• Eski Rejim: {old_str}\n"
            f"• Yeni Rejim: {new_str}\n"
            f"• Rejim Skoru: `{score:+d}` (BTC + Genişlik + Fonlama)\n"
            f"• ADX Trend Gücü: `{adx:.1f}`\n"
            f"• Piyasa Genişliği: `%{breadth:.1f}` (EMA50 Üstü)\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "🛡️ *Uygulanan Dinamik Ayarlar:*\n"
        )

        params = self.get_regime_params()
        msg += (
            f"• Maks Açık Pozisyon: `{params['max_concurrent_positions']}`\n"
            f"• Dinamik TP Çarpanı: `{params['atr_tp_multiplier']}x ATR`\n"
            f"• Takip Eden Stop Mesafesi: `%{params['trailing_delta_pct']}`\n"
            f"• LONG Girişleri: `{'Aktif' if params['allow_long'] else 'Kısıtlı (Sadece Squeeze)'}`\n"
            f"• Sahte Kırılım (Sweep) Koruması: `{params['sweep_filter_mode']}`"
        )

        self.notifier.send_message(msg)

    def get_regime_params(self) -> Dict[str, Any]:
        """
        Mevcut rejime göre dinamik yürütme parametrelerini döner.
        """
        r = self.current_regime

        if r == "BULL":
            return {
                "max_concurrent_positions": 5,
                "allow_long": True,
                "allow_short": False,  # Boğada ters yöne SHORT açılmaz
                "atr_tp_multiplier": 3.0,
                "atr_sl_multiplier": 2.0,
                "trailing_delta_pct": 0.8,
                "funding_exhaustion_action": "TIGHTEN_TRAILING",  # Fonlama +%0.05'e vurursa erken satma, trailing'i %0.2'ye sıkılaştır
                "funding_exhaustion_trailing_delta": 0.2,
                "sweep_filter_mode": "LOOSE",  # Gerçek kırılımların önünü kesme
                "min_close_strength": 0.62
            }
        elif r == "CHOP":
            return {
                "max_concurrent_positions": 3,
                "allow_long": True,
                "allow_short": True,
                "atr_tp_multiplier": 2.0,
                "atr_sl_multiplier": 1.8,
                "trailing_delta_pct": 0.4,
                "funding_exhaustion_action": "EXIT_EARLY",  # Testerede fonlama şişerse kârı erken al
                "funding_exhaustion_trailing_delta": 0.2,
                "sweep_filter_mode": "STRICT",  # Yatayda sahte kırılımları sıkı filtrele
                "min_close_strength": 0.68
            }
        elif r == "BEAR":
            return {
                "max_concurrent_positions": 3,
                "allow_long": False,  # Ayı piyasasında LONG sadece Short Squeeze onaylıysa girer
                "allow_short": True,
                "atr_tp_multiplier": 2.0,
                "atr_sl_multiplier": 1.8,
                "trailing_delta_pct": 0.4,
                "funding_exhaustion_action": "EXIT_EARLY",
                "funding_exhaustion_trailing_delta": 0.2,
                "sweep_filter_mode": "STRICT",
                "min_close_strength": 0.65
            }
        else:  # UNCERTAIN (Geçiş veya Belirsiz Savunma Modu)
            return {
                "max_concurrent_positions": 2,
                "allow_long": True,
                "allow_short": True,
                "atr_tp_multiplier": 2.0,
                "atr_sl_multiplier": 1.8,
                "trailing_delta_pct": 0.4,
                "funding_exhaustion_action": "EXIT_EARLY",
                "funding_exhaustion_trailing_delta": 0.2,
                "sweep_filter_mode": "STRICT",
                "min_close_strength": 0.70  # Çok seçici ol
            }

    def set_manual_override(self, mode: Optional[str]) -> str:
        """Manuel rejim ataması yapar (BULL, BEAR, CHOP veya None)."""
        if mode is None or mode.upper() in ["SIFIRLA", "AUTO", "OTOMATIK"]:
            self.manual_override = None
            self._save_state()
            self.update(force=True)
            return "✅ Otomatik rejim tespiti tekrar devrede."

        mode_clean = mode.upper().strip()
        alias_map = {
            "BOGA": "BULL", "BOĞA": "BULL", "BULL": "BULL",
            "AYI": "BEAR", "BEAR": "BEAR",
            "YATAY": "CHOP", "TESTERE": "CHOP", "CHOP": "CHOP",
            "BELIRSIZ": "UNCERTAIN", "BELİRSİZ": "UNCERTAIN"
        }

        if mode_clean not in alias_map:
            return f"⚠️ Geçersiz rejim modu: `{mode}`. Geçerli seçenekler: `boga`, `ayi`, `yatay`, `sifirla`"

        target_r = alias_map[mode_clean]
        self.manual_override = target_r
        self.current_regime = target_r
        self.regime_start_time = time.time()
        self._save_state()
        return f"🚨 Manuel Rejim Kilitlendi: *{target_r}* (Otomatik analiz ezildi)."

    def set_enabled(self, enabled: bool) -> str:
        """Rejim sistemini açar veya kapatır."""
        self.enabled = enabled
        self._save_state()
        if enabled:
            self.update(force=True)
            return "✅ Piyasa rejim sistemi *AKTİF* edildi."
        else:
            return "⏸️ Piyasa rejim sistemi *KAPATILDI*. Bot statik config.json ayarlarıyla çalışacak."

    def get_summary(self) -> str:
        """Telegram /rejim komutu için detaylı durum metni üretir."""
        analysis = self.update(force=False)

        emoji_map = {
            "BULL": "🟢 *BOĞA (BULL)*",
            "BEAR": "🔴 *AYI (BEAR)*",
            "CHOP": "🟡 *YATAY (CHOP)*",
            "UNCERTAIN": "⚪ *BELİRSİZ (SAVUNMA)*"
        }
        reg_str = emoji_map.get(self.current_regime, self.current_regime)
        duration_hours = (time.time() - self.regime_start_time) / 3600.0

        status_flag = "MANUEL KİLİTLİ 🔒" if self.manual_override else ("AKTİF (OTOMATİK) 🟢" if self.enabled else "DEVRE DIŞI 🔴")

        msg = (
            "🧭 *PİYASA REJİM RAPORU*\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            f"• Mevcut Rejim: {reg_str}\n"
            f"• Sistem Durumu: `{status_flag}`\n"
            f"• Rejim Süresi: `{duration_hours:.1f} saattir bu rejimde`\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "📊 *MAKRO & KANTİTATİF VERİLER:*\n"
            f"• BTC Fiyatı: `${analysis.btc_price:,.2f}`\n"
            f"• BTC 4H EMA50: `${analysis.btc_ema50:,.2f}`\n"
            f"• BTC 4H EMA200: `${analysis.btc_ema200:,.2f}`\n"
            f"• BTC EMA50 Eğimi: `%{analysis.btc_slope_pct:+.2f}`\n"
            f"• ADX Trend Gücü: `{analysis.adx_value:.1f}` (Eşik: >25 Trend, <20 Yatay)\n"
            f"• Piyasa Genişliği: `%{analysis.market_breadth_pct:.1f}` (EMA50 Üstü)\n"
            f"• Ort. Fonlama: `%{analysis.avg_funding_rate * 100:.4f}`\n"
            f"• Toplam Puan: `{analysis.score:+d}` (Min: -4, Maks: +4)\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "⚙️ *AKTİF DİNAMİK BOT PARAMETRELERİ:*\n"
        )

        params = self.get_regime_params()
        msg += (
            f"• Maksimum Pozisyon Limiti: `{params['max_concurrent_positions']}`\n"
            f"• Kâr Hedefi Çarpanı (TP): `{params['atr_tp_multiplier']}x ATR`\n"
            f"• Trailing Stop Mesafesi: `%{params['trailing_delta_pct']}`\n"
            f"• LONG İzni: `{'AÇIK' if params['allow_long'] else 'KAPALI (Sadece Squeeze)'}`\n"
            f"• SHORT İzni: `{'AÇIK' if params['allow_short'] else 'KAPALI'}`\n"
            f"• Sahte Kırılım (Sweep) Filtresi: `{params['sweep_filter_mode']}`\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "💡 *Komutlar:* `/rejim kapat`, `/rejim ac`, `/rejim boga`, `/rejim ayi`, `/rejim yatay`, `/rejim sifirla`"
        )
        return msg
