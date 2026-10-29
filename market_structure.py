"""
market_structure.py
===================
Piyasa Yapısı (Market Structure), Destek/Direnç Kümeleri (Swing S/R),
Hacim Profili (Volume Profile - POC) ve Trend Doğrusu Analiz Modülü.

Özellikler:
- Fraktal Tepe/Dip (Swing High/Low) tespiti ve tolerans bantlı kümeleme.
- Hacim Profili (Volume Profile) üzerinden Point of Control (POC) hesabı.
- Lineer regresyon ile trend yönü ve saatlik eğim açısı tespiti.
- LONG için 'Duvara Çarpma' (Room-to-Resistance) ve Direnç Kırılım (Breakout) doğrulaması.
- SHORT için asimetrik 'Dirençten Red / Fakeout' (Resistance Rejection) ve 'Tabana Çarpma' kontrolü.
- Çift yönlü asimetrik Kâr Alma (Take-Profit) ve yapısal Zarar Durdurma (Stop-Loss) önerileri.
"""

from dataclasses import dataclass
from typing import Dict, List, Optional, Any, Tuple
import numpy as np
import pandas as pd


@dataclass
class HTFTrendBias:
    bias: str                     # "BULLISH", "BEARISH", "NEUTRAL"
    ema50: float
    ema200: float
    desc: str


@dataclass
class MarketStructureResult:
    symbol: str
    side: str                     # "LONG" veya "SHORT"
    current_price: float
    nearest_resistance: Optional[float]
    dist_to_resistance_pct: Optional[float]
    nearest_support: Optional[float]
    dist_to_support_pct: Optional[float]
    poc_price: float
    poc_volume: float
    trend_direction: str          # "Yükselen Trend ↗", "Düşen Trend ↘", "Yatay Bant ↔"
    trend_slope_hourly_pct: float
    is_breakout: bool             # Direnç yukarı kırıldı mı? (LONG için)
    is_breakdown: bool            # Destek aşağı kırıldı mı? (SHORT için)
    is_rejection: bool            # Direnç / POC tepesinden red yedi mi? (SHORT için altın kural)
    rejection_source: Optional[str]
    broken_level: Optional[float]
    has_room_to_run: bool
    rejection_reason: Optional[str]
    suggested_tp_pct: float
    suggested_sl_pct: float
    all_levels: List[Dict[str, Any]]
    atr_value: float = 0.0
    atr_pct: float = 0.0
    rsi_value: float = 50.0
    htf_bias: str = "NEUTRAL"
    htf_desc: str = ""
    is_overextended: bool = False

    @property
    def broken_resistance(self) -> Optional[float]:
        return self.broken_level if self.is_breakout else None

    @property
    def broken_support(self) -> Optional[float]:
        return self.broken_level if self.is_breakdown else None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "side": self.side,
            "current_price": self.current_price,
            "nearest_resistance": self.nearest_resistance,
            "dist_to_resistance_pct": self.dist_to_resistance_pct,
            "nearest_support": self.nearest_support,
            "dist_to_support_pct": self.dist_to_support_pct,
            "poc_price": self.poc_price,
            "poc_volume": self.poc_volume,
            "trend_direction": self.trend_direction,
            "trend_slope_hourly_pct": self.trend_slope_hourly_pct,
            "is_breakout": self.is_breakout,
            "is_breakdown": self.is_breakdown,
            "is_rejection": self.is_rejection,
            "rejection_source": self.rejection_source,
            "broken_level": self.broken_level,
            "has_room_to_run": self.has_room_to_run,
            "rejection_reason": self.rejection_reason,
            "suggested_tp_pct": self.suggested_tp_pct,
            "suggested_sl_pct": self.suggested_sl_pct,
            "top_levels_count": len(self.all_levels),
            "atr_value": self.atr_value,
            "atr_pct": self.atr_pct,
            "rsi_value": self.rsi_value,
            "htf_bias": self.htf_bias,
            "htf_desc": self.htf_desc,
            "is_overextended": self.is_overextended
        }


def calculate_atr(df: pd.DataFrame, period: int = 14) -> Tuple[float, float]:
    """
    Saf pandas/numpy ile Average True Range (ATR) hesaplar.
    Dönen: (atr_value, atr_pct)
    """
    if df is None or len(df) < period + 1:
        return 0.0, 0.0

    high = df["high"].values
    low = df["low"].values
    close = df["close"].values

    tr1 = high[1:] - low[1:]
    tr2 = np.abs(high[1:] - close[:-1])
    tr3 = np.abs(low[1:] - close[:-1])
    tr = np.maximum(tr1, np.maximum(tr2, tr3))

    tr_series = pd.Series(tr)
    atr_series = tr_series.rolling(window=period).mean()
    atr_val = float(atr_series.iloc[-1])
    if np.isnan(atr_val) or atr_val <= 0:
        return 0.0, 0.0

    curr_p = float(close[-1])
    atr_pct = (atr_val / curr_p * 100.0) if curr_p > 0 else 0.0
    return round(atr_val, 6), round(float(atr_pct), 2)


def calculate_rsi(df: pd.DataFrame, period: int = 14) -> float:
    """
    Saf pandas ile Wilder RSI hesaplar.
    """
    if df is None or len(df) < period + 1:
        return 50.0

    close = df["close"]
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    roll_up = gain.ewm(alpha=1.0 / period, adjust=False).mean()
    roll_down = loss.ewm(alpha=1.0 / period, adjust=False).mean()

    rs = roll_up / (roll_down + 1e-9)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    last_rsi = float(rsi.iloc[-1])
    return round(float(np.clip(last_rsi, 0.0, 100.0)), 2) if not np.isnan(last_rsi) else 50.0


def calculate_htf_trend(df_1h: Optional[pd.DataFrame]) -> HTFTrendBias:
    """
    1 Saatlik (1h) mumlar üzerinden HTF (Higher Timeframe) trend yönünü tespit eder.
    EMA50 ve EMA200 ilişkisine bakar.
    """
    if df_1h is None or len(df_1h) < 25:
        return HTFTrendBias("NEUTRAL", 0.0, 0.0, "Yetersiz 1H Mum Verisi")

    closes = df_1h["close"]
    curr_price = float(closes.iloc[-1])
    ema50 = float(closes.ewm(span=min(50, len(closes)), adjust=False).mean().iloc[-1])
    ema200 = float(closes.ewm(span=min(200, len(closes)), adjust=False).mean().iloc[-1])

    if curr_price >= ema50 and ema50 >= ema200:
        desc = f"Güçlü 1H Boğa Trendi (Fiyat ${curr_price:.4f} >= EMA50 ${ema50:.4f} >= EMA200 ${ema200:.4f})"
        return HTFTrendBias("BULLISH", round(ema50, 6), round(ema200, 6), desc)
    elif curr_price <= ema50 and ema50 <= ema200:
        desc = f"Güçlü 1H Ayı Trendi (Fiyat ${curr_price:.4f} <= EMA50 ${ema50:.4f} <= EMA200 ${ema200:.4f})"
        return HTFTrendBias("BEARISH", round(ema50, 6), round(ema200, 6), desc)
    elif curr_price > ema50:
        desc = f"1H Pozitif Eğim (Fiyat ${curr_price:.4f} > EMA50 ${ema50:.4f})"
        return HTFTrendBias("BULLISH", round(ema50, 6), round(ema200, 6), desc)
    elif curr_price < ema50:
        desc = f"1H Negatif Eğim (Fiyat ${curr_price:.4f} < EMA50 ${ema50:.4f})"
        return HTFTrendBias("BEARISH", round(ema50, 6), round(ema200, 6), desc)
    else:
        return HTFTrendBias("NEUTRAL", round(ema50, 6), round(ema200, 6), "1H Nötr / Yatay Rejim")


def find_sr_clusters(
    df: pd.DataFrame,
    window: int = 5,
    tolerance_pct: float = 0.4,
    min_touches: int = 2
) -> List[Dict[str, Any]]:
    """
    Son N mumdaki lokal tepe/dipleri (fraktalları) tespit eder
    ve birbirine yakın olanları kümeleyerek destek/direnç bantları oluşturur.
    """
    if df is None or len(df) < (window * 2 + 1):
        return []

    highs = df["high"].values
    lows = df["low"].values
    n = len(df)

    pivots: List[Tuple[str, float]] = []

    for i in range(window, n - window):
        is_high = highs[i] == np.max(highs[i - window : i + window + 1])
        is_low = lows[i] == np.min(lows[i - window : i + window + 1])

        if is_high:
            pivots.append(("high", float(highs[i])))
        if is_low:
            pivots.append(("low", float(lows[i])))

    if not pivots:
        return []

    pivots.sort(key=lambda x: x[1])
    clusters: List[List[Tuple[str, float]]] = []

    for p_type, price in pivots:
        matched = False
        for cluster in clusters:
            cluster_avg = sum(p[1] for p in cluster) / len(cluster)
            if abs(price - cluster_avg) / cluster_avg * 100.0 <= tolerance_pct:
                cluster.append((p_type, price))
                matched = True
                break
        if not matched:
            clusters.append([(p_type, price)])

    levels = []
    for cluster in clusters:
        if len(cluster) >= min_touches:
            avg_price = sum(p[1] for p in cluster) / len(cluster)
            high_touches = sum(1 for p in cluster if p[0] == "high")
            low_touches = sum(1 for p in cluster if p[0] == "low")
            levels.append({
                "price": round(avg_price, 6),
                "touches": len(cluster),
                "high_touches": high_touches,
                "low_touches": low_touches
            })

    levels.sort(key=lambda x: x["touches"], reverse=True)
    return levels


def calculate_volume_profile_poc(
    df: pd.DataFrame,
    bins: int = 24
) -> Tuple[float, float]:
    """
    Hacim Profili (Volume Profile) oluşturur ve en çok işlem gören
    Point of Control (POC) fiyat seviyesini ve o seviyedeki hacmi döner.
    """
    if df is None or len(df) == 0:
        return 0.0, 0.0

    lows = df["low"].values
    highs = df["high"].values
    closes = df["close"].values
    volumes = df["volume"].values

    min_p = float(np.min(lows))
    max_p = float(np.max(highs))

    if min_p >= max_p or np.isnan(min_p) or np.isnan(max_p):
        mid = float(closes[-1]) if len(closes) > 0 else 0.0
        return mid, 0.0

    bin_edges = np.linspace(min_p, max_p, bins + 1)
    bin_vols = np.zeros(bins, dtype=float)

    typical_prices = (highs + lows + closes) / 3.0
    bin_indices = np.digitize(typical_prices, bin_edges) - 1
    bin_indices = np.clip(bin_indices, 0, bins - 1)

    np.add.at(bin_vols, bin_indices, volumes)

    poc_idx = int(np.argmax(bin_vols))
    poc_price = float((bin_edges[poc_idx] + bin_edges[poc_idx + 1]) / 2.0)
    poc_volume = float(bin_vols[poc_idx])

    return round(poc_price, 6), round(poc_volume, 2)


def calculate_trend_slope(
    df: pd.DataFrame,
    period: int = 60
) -> Tuple[str, float]:
    """
    Son 'period' mumun (varsayılan 60 mum = 5 saat) lineer regresyon eğimini hesaplar.
    Saatlik yüzde değişim hızı ve yön etiketi üretir.
    """
    if df is None or len(df) < 15:
        return "Yatay Bant ↔", 0.0

    sub_df = df.tail(period)
    closes = sub_df["close"].values
    n = len(closes)
    if n < 2 or closes[0] <= 0:
        return "Yatay Bant ↔", 0.0

    x = np.arange(n)
    try:
        slope, _ = np.polyfit(x, closes, 1)
        hourly_pct = (slope * 12.0 / closes[0]) * 100.0
        hourly_pct = round(float(hourly_pct), 2)
    except Exception:
        return "Yatay Bant ↔", 0.0

    if hourly_pct >= 0.25:
        direction = "Yükselen Trend ↗"
    elif hourly_pct <= -0.25:
        direction = "Düşen Trend ↘"
    else:
        direction = "Yatay Bant ↔"

    return direction, hourly_pct


def detect_resistance_rejection(
    candle: pd.Series,
    levels: List[Dict[str, Any]],
    poc_price: float,
    tolerance_pct: float = 0.5
) -> Tuple[bool, Optional[float], str]:
    """
    SHORT için altın kurulum:
    Fiyatın bir direnç kümesini veya POC seviyesini yukarı test edip (Fakeout/Retest)
    oradan sert satışla dönüp dönmediğini (Rejection) ölçer.
    """
    try:
        high = float(candle["high"])
        low = float(candle["low"])
        close = float(candle["close"])
        op = float(candle["open"])
    except Exception:
        return False, None, ""

    rng = high - low
    if rng <= 0:
        return False, None, ""

    upper_wick = high - max(op, close)
    wick_ratio = upper_wick / rng

    # 1. Direnç Kümelerinden Reddi Kontrol Et
    for lvl in levels:
        lvl_p = float(lvl["price"])
        if lvl_p <= low:
            continue
        # Fiyat dirence yaklaştı veya deldi, ancak altında kapattı
        if high >= lvl_p * (1.0 - tolerance_pct / 100.0) and close <= lvl_p:
            if wick_ratio >= 0.30 or close < op:
                return True, lvl_p, f"Direnç Reddi (${lvl_p:.4f})"

    # 2. Hacim POC Seviyesinden Reddi Kontrol Et
    if poc_price > low:
        if high >= poc_price * (1.0 - tolerance_pct / 100.0) and close <= poc_price:
            if wick_ratio >= 0.30 or close < op:
                return True, poc_price, f"POC Hacim Reddi (${poc_price:.4f})"

    return False, None, ""


def analyze_market_structure(
    symbol: str,
    df: pd.DataFrame,
    df_1h: Optional[pd.DataFrame] = None,
    current_price: float = 0.0,
    prev_close: Optional[float] = None,
    side: str = "LONG",
    window: int = 5,
    tolerance_pct: float = 0.4,
    min_room_to_resistance_pct: float = 1.2,
    min_room_to_support_pct: float = 1.2,
    default_tp_pct: float = 3.0,
    default_sl_pct: float = 2.0,
    dynamic_tp_enabled: bool = True,
    structural_sl_enabled: bool = True,
    use_htf_filter: bool = True,
    use_atr_dynamic_sl: bool = True,
    atr_period: int = 14,
    atr_sl_multiplier: float = 1.8,
    min_sl_pct: float = 1.0,
    max_sl_pct: float = 3.5,
    max_candle_atr_ratio: float = 3.0,
    max_long_rsi: float = 78.0,
    min_short_rsi: float = 22.0
) -> MarketStructureResult:
    """
    Kapsamlı ve Asimetrik Piyasa Yapısı Analiz Motoru:
    1. Fraktal S/R Kümeleri ve Hacim POC Tespiti
    2. Yüksek Zaman Dilimi (HTF 1H) Makro Trend Teyidi (Boğa / Ayı Tuzağı Koruması)
    3. Tepe / Dip Kovalama ve Likidite Tuzağı Engeli (Aşırı Uzamış Mum > 3.0x ATR & RSI Ekstremleri)
    4. LONG için Direnç Kırılım (Breakout) ve Duvara Çarpma Kontrolü
    5. SHORT için Dirençten Red (Rejection/Fakeout) ve Destek Kırılım (Breakdown) Kontrolü
    6. Dinamik Volatilite (ATR) ve Yapısal Hibrit Stop-Loss & Take-Profit Hesabı
    """
    if current_price <= 0:
        current_price = float(df["close"].iloc[-1]) if (df is not None and len(df) > 0) else 1.0

    side = side.upper()

    # 1. Göstergeleri Hesapla (ATR, RSI, HTF Trend)
    atr_val, atr_pct = calculate_atr(df, period=atr_period)
    rsi_val = calculate_rsi(df, period=atr_period)
    htf_res = calculate_htf_trend(df_1h)

    # 2. Fraktal Kümeleri ve Hacim Profilini Çıkar
    levels = find_sr_clusters(df, window=window, tolerance_pct=tolerance_pct, min_touches=2)
    poc_price, poc_vol = calculate_volume_profile_poc(df, bins=24)
    trend_dir, trend_slope = calculate_trend_slope(df, period=60)

    # 3. Üstteki Dirençler ve Alttaki Destekler
    resistances = [lvl for lvl in levels if lvl["price"] > current_price]
    supports = [lvl for lvl in levels if lvl["price"] < current_price]

    nearest_res_obj = min(resistances, key=lambda x: x["price"]) if resistances else None
    nearest_sup_obj = max(supports, key=lambda x: x["price"]) if supports else None

    nearest_res = nearest_res_obj["price"] if nearest_res_obj else None
    nearest_sup = nearest_sup_obj["price"] if nearest_sup_obj else None

    dist_res_pct = None
    if nearest_res is not None:
        dist_res_pct = round(((nearest_res - current_price) / current_price) * 100.0, 2)

    dist_sup_pct = None
    if nearest_sup is not None:
        dist_sup_pct = round(((current_price - nearest_sup) / current_price) * 100.0, 2)

    # 4. Kırılım ve Red Tespiti
    is_breakout = False
    is_breakdown = False
    is_rejection = False
    rejection_source = None
    broken_level = None

    last_candle = df.iloc[-1] if df is not None and len(df) > 0 else None

    if prev_close is not None and prev_close > 0:
        for lvl in levels:
            lvl_p = lvl["price"]
            if side == "LONG":
                if prev_close <= lvl_p <= current_price or (
                    current_price > lvl_p and ((current_price - lvl_p) / lvl_p * 100.0) <= 0.6
                ):
                    is_breakout = True
                    broken_level = lvl_p
                    break
            else:
                if prev_close >= lvl_p >= current_price or (
                    current_price < lvl_p and ((lvl_p - current_price) / lvl_p * 100.0) <= 0.6
                ):
                    is_breakdown = True
                    broken_level = lvl_p
                    break

    if side == "SHORT" and last_candle is not None:
        is_rejection, rej_p, rej_src = detect_resistance_rejection(
            candle=last_candle,
            levels=levels,
            poc_price=poc_price,
            tolerance_pct=0.5
        )
        if is_rejection:
            rejection_source = rej_src

    # 5. Kalite ve Güvenlik Filtreleri
    has_room = True
    rejection_reason = None
    is_overextended = False

    # A) TEPE/DİP KOVALAMA ENGELİ (FOMO & Likidite Tuzağı Koruması)
    if last_candle is not None and atr_val > 0:
        candle_range = float(last_candle["high"]) - float(last_candle["low"])
        atr_ratio = candle_range / atr_val
        if atr_ratio > max_candle_atr_ratio:
            is_overextended = True
            has_room = False
            rejection_reason = (
                f"Aşırı Parabolik Mum ({atr_ratio:.1f}x ATR > {max_candle_atr_ratio:.1f}x). "
                f"Tepe kovalama / likidite tuzağı riski engellendi"
            )

    if has_room:
        if side == "LONG" and rsi_val > max_long_rsi:
            has_room = False
            rejection_reason = (
                f"Aşırı Alım Bölgesi (RSI {rsi_val:.1f} > {max_long_rsi:.1f}). "
                f"Tepeden alım (FOMO) engellendi"
            )
        elif side == "SHORT" and rsi_val < min_short_rsi:
            has_room = False
            rejection_reason = (
                f"Aşırı Satım Bölgesi (RSI {rsi_val:.1f} < {min_short_rsi:.1f}). "
                f"Dipten shortlama (Squeeze) riski engellendi"
            )

    # B) HTF (1H) MAKRO TREND TEYİDİ (Yüksek Zaman Dilimi Körlüğü Çözümü)
    if has_room and use_htf_filter and df_1h is not None and len(df_1h) >= 25:
        if side == "LONG" and htf_res.bias == "BEARISH":
            has_room = False
            rejection_reason = (
                f"1H HTF Makro Ayı Trendi ({htf_res.desc}). "
                f"Akıntıya karşı long / Boğa Tuzağı (Bull Trap) engellendi"
            )
        elif side == "SHORT" and htf_res.bias == "BULLISH":
            has_room = False
            rejection_reason = (
                f"1H HTF Makro Boğa Trendi ({htf_res.desc}). "
                f"Akıntıya karşı short / Ayı Tuzağı (Bear Trap) engellendi"
            )

    # C) DUVARA VE TABANA ÇARPMA KONTROLÜ
    if has_room:
        if side == "LONG":
            if nearest_res is not None and not is_breakout:
                if dist_res_pct is not None and dist_res_pct < min_room_to_resistance_pct:
                    has_room = False
                    rejection_reason = (
                        f"Direnç Çok Yakın (+%{dist_res_pct:.2f} < +%{min_room_to_resistance_pct:.2f}). "
                        f"Duvara çarpma riski (Direnç: ${nearest_res:.4f})"
                    )
        else:
            # SHORT: Güçlü boğa trendinde short açılmaz
            if trend_slope >= 0.25:
                has_room = False
                rejection_reason = (
                    f"5m Güçlü Yükselen Trend (+%{trend_slope:.2f}/saat). "
                    f"Boğa trendinde short açmak yüksek risklidir"
                )
            elif nearest_sup is not None and not is_breakdown:
                if dist_sup_pct is not None and dist_sup_pct < min_room_to_support_pct:
                    has_room = False
                    rejection_reason = (
                        f"Destek Çok Yakın (-%{dist_sup_pct:.2f} < -%{min_room_to_support_pct:.2f}). "
                        f"Tabana çarpıp sekme riski yüksek (Destek: ${nearest_sup:.4f})"
                    )

    # 6. Dinamik TP (Take-Profit)
    suggested_tp = default_tp_pct
    if dynamic_tp_enabled:
        if side == "LONG" and nearest_res is not None and dist_res_pct is not None:
            if 1.5 <= dist_res_pct <= default_tp_pct:
                suggested_tp = max(1.5, round(dist_res_pct - 0.15, 2))
        elif side == "SHORT" and nearest_sup is not None and dist_sup_pct is not None:
            if 1.2 <= dist_sup_pct <= default_tp_pct:
                suggested_tp = max(1.2, round(dist_sup_pct - 0.15, 2))

    # 7. Dinamik Volatilite (ATR) ve Yapısal Stop-Loss
    # Sabit %1.5 stop yerine coinin volatilitesine (ATR) duyarlı stop
    if use_atr_dynamic_sl and atr_pct > 0:
        base_sl = float(np.clip(round(atr_pct * atr_sl_multiplier, 2), min_sl_pct, max_sl_pct))
    else:
        base_sl = float(default_sl_pct)

    suggested_sl = base_sl

    if structural_sl_enabled:
        if side == "LONG":
            structural_floors = []
            if nearest_sup is not None and dist_sup_pct is not None and 0.4 <= dist_sup_pct <= (base_sl + 0.6):
                structural_floors.append(dist_sup_pct)
            if 0 < poc_price < current_price:
                poc_dist_pct = ((current_price - poc_price) / current_price) * 100.0
                if 0.4 <= poc_dist_pct <= (base_sl + 0.6):
                    structural_floors.append(poc_dist_pct)
            if structural_floors:
                floor_dist = min(structural_floors)
                calc_sl = round(floor_dist + 0.15, 2)
                suggested_sl = float(np.clip(calc_sl, min_sl_pct, max_sl_pct))
        else:
            # SHORT: Tavan (Direnç veya üstteki POC) koruyucu çatı kabul edilir
            structural_roofs = []
            if nearest_res is not None and dist_res_pct is not None and 0.4 <= dist_res_pct <= (base_sl + 0.6):
                structural_roofs.append(dist_res_pct)
            if poc_price > current_price:
                poc_dist_pct = ((poc_price - current_price) / current_price) * 100.0
                if 0.4 <= poc_dist_pct <= (base_sl + 0.6):
                    structural_roofs.append(poc_dist_pct)
            if structural_roofs:
                roof_dist = min(structural_roofs)
                calc_sl = round(roof_dist + 0.15, 2)
                suggested_sl = float(np.clip(calc_sl, min_sl_pct, max_sl_pct))

    return MarketStructureResult(
        symbol=symbol,
        side=side,
        current_price=round(current_price, 6),
        nearest_resistance=nearest_res,
        dist_to_resistance_pct=dist_res_pct,
        nearest_support=nearest_sup,
        dist_to_support_pct=dist_sup_pct,
        poc_price=poc_price,
        poc_volume=poc_vol,
        trend_direction=trend_dir,
        trend_slope_hourly_pct=trend_slope,
        is_breakout=is_breakout,
        is_breakdown=is_breakdown,
        is_rejection=is_rejection,
        rejection_source=rejection_source,
        broken_level=broken_level,
        has_room_to_run=has_room,
        rejection_reason=rejection_reason,
        suggested_tp_pct=suggested_tp,
        suggested_sl_pct=suggested_sl,
        all_levels=levels[:8],
        atr_value=atr_val,
        atr_pct=atr_pct,
        rsi_value=rsi_val,
        htf_bias=htf_res.bias,
        htf_desc=htf_res.desc,
        is_overextended=is_overextended
    )
