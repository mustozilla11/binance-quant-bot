"""
market_structure.py
===================
Piyasa Yapısı (Market Structure), Destek/Direnç Kümeleri (Swing S/R),
Hacim Profili (Volume Profile - POC) ve Trend Doğrusu Analiz Modülü.

Özellikler:
- Fraktal Tepe/Dip (Swing High/Low) tespiti ve tolerans bantlı kümeleme.
- Hacim Profili (Volume Profile) üzerinden Point of Control (POC) hesabı.
- Lineer regresyon ile trend yönü ve saatlik eğim açısı tespiti.
- 'Duvara Çarpma' (Room-to-Run) kontrolü ve Direnç Kırılım (Breakout) doğrulaması.
- Yapısal Dinamik Kâr Alma (Take-Profit) ve Destek Arkası Zarar Durdurma (Stop-Loss) önerileri.
"""

from dataclasses import dataclass
from typing import Dict, List, Optional, Any, Tuple
import numpy as np
import pandas as pd


@dataclass
class MarketStructureResult:
    symbol: str
    current_price: float
    nearest_resistance: Optional[float]
    dist_to_resistance_pct: Optional[float]
    nearest_support: Optional[float]
    dist_to_support_pct: Optional[float]
    poc_price: float
    poc_volume: float
    trend_direction: str          # "Yükselen Trend ↗", "Düşen Trend ↘", "Yatay Bant ↔"
    trend_slope_hourly_pct: float
    is_breakout: bool
    broken_resistance: Optional[float]
    has_room_to_run: bool
    rejection_reason: Optional[str]
    suggested_tp_pct: float
    suggested_sl_pct: float
    all_levels: List[Dict[str, Any]]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
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
            "broken_resistance": self.broken_resistance,
            "has_room_to_run": self.has_room_to_run,
            "rejection_reason": self.rejection_reason,
            "suggested_tp_pct": self.suggested_tp_pct,
            "suggested_sl_pct": self.suggested_sl_pct,
            "top_levels_count": len(self.all_levels)
        }


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

    # 1. Lokal Tepe (Swing High) ve Lokal Dip (Swing Low) Tespiti
    for i in range(window, n - window):
        is_high = highs[i] == np.max(highs[i - window : i + window + 1])
        is_low = lows[i] == np.min(lows[i - window : i + window + 1])

        if is_high:
            pivots.append(("high", float(highs[i])))
        if is_low:
            pivots.append(("low", float(lows[i])))

    if not pivots:
        return []

    # 2. Tolerans Dahilinde Fiyat Kümelerine Birleştir
    # Fiyata göre sırala
    pivots.sort(key=lambda x: x[1])
    clusters: List[List[Tuple[str, float]]] = []

    for p_type, price in pivots:
        matched = False
        for cluster in clusters:
            cluster_avg = sum(p[1] for p in cluster) / len(cluster)
            # Yüzdesel mesafe tolerans içinde mi?
            if abs(price - cluster_avg) / cluster_avg * 100.0 <= tolerance_pct:
                cluster.append((p_type, price))
                matched = True
                break
        if not matched:
            clusters.append([(p_type, price)])

    # 3. En Az 'min_touches' Kez Test Edilmiş Güçlü Seviyeleri Derle
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

    # Test edilme sayısına (güce) göre azalan sırala
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

    # Tipik fiyat üzerinden hacim dağılımı
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
        # 5 dakikalık periyotta 12 mum = 1 saat.
        # Saatlik bazda fiyata göre yüzde değişim eğimi:
        hourly_pct = (slope * 12.0 / closes[0]) * 100.0
        hourly_pct = round(float(hourly_pct), 2)
    except Exception:
        return "Yatay Bant ↔", 0.0

    if hourly_pct >= 0.30:
        direction = "Yükselen Trend ↗"
    elif hourly_pct <= -0.30:
        direction = "Düşen Trend ↘"
    else:
        direction = "Yatay Bant ↔"

    return direction, hourly_pct


def analyze_market_structure(
    symbol: str,
    df: pd.DataFrame,
    current_price: float,
    prev_close: Optional[float] = None,
    window: int = 5,
    tolerance_pct: float = 0.4,
    min_room_to_resistance_pct: float = 1.2,
    default_tp_pct: float = 3.0,
    default_sl_pct: float = 2.0,
    dynamic_tp_enabled: bool = True,
    structural_sl_enabled: bool = True
) -> MarketStructureResult:
    """
    Tüm piyasa yapısı bileşenlerini birleştirir:
    - Destek/Direnç kümeleri
    - POC seviyesi
    - Trend eğimi
    - Kırılım (Breakout) ve Duvara Çarpma (Room to Run) kontrolü
    - Dinamik TP & Yapısal SL hesaplaması
    """
    if current_price <= 0:
        current_price = float(df["close"].iloc[-1]) if (df is not None and len(df) > 0) else 1.0

    # 1. Kümeleri ve POC'yi çıkar
    levels = find_sr_clusters(df, window=window, tolerance_pct=tolerance_pct, min_touches=2)
    poc_price, poc_vol = calculate_volume_profile_poc(df, bins=24)
    trend_dir, trend_slope = calculate_trend_slope(df, period=60)

    # 2. Üstteki Dirençler ve Alttaki Destekler
    resistances = [lvl for lvl in levels if lvl["price"] > current_price]
    supports = [lvl for lvl in levels if lvl["price"] < current_price]

    # Fiyata en yakın olanlar:
    # Dirençler: fiyattan büyükler arasında en küçük fiyatlısı
    nearest_res_obj = min(resistances, key=lambda x: x["price"]) if resistances else None
    # Destekler: fiyattan küçükler arasında en büyük fiyatlısı
    nearest_sup_obj = max(supports, key=lambda x: x["price"]) if supports else None

    nearest_res = nearest_res_obj["price"] if nearest_res_obj else None
    nearest_sup = nearest_sup_obj["price"] if nearest_sup_obj else None

    dist_res_pct = None
    if nearest_res is not None:
        dist_res_pct = round(((nearest_res - current_price) / current_price) * 100.0, 2)

    dist_sup_pct = None
    if nearest_sup is not None:
        dist_sup_pct = round(((current_price - nearest_sup) / current_price) * 100.0, 2)

    # 3. Kırılım (Breakout) Tespiti
    # Eğer önceki mum kapanışı bir direncin altındayken bu mum o direncin üzerine çıktıysa kırılımdır
    is_breakout = False
    broken_res = None
    if prev_close is not None and prev_close > 0:
        for lvl in levels:
            lvl_p = lvl["price"]
            # Direnç seviyesi önceki kapanış ile mevcut fiyat arasında mı?
            # Veya mevcut fiyat direncin %0.0 - %0.6 kadar hemen üzerinde mi?
            if prev_close <= lvl_p <= current_price or (
                current_price > lvl_p and ((current_price - lvl_p) / lvl_p * 100.0) <= 0.6
            ):
                is_breakout = True
                broken_res = lvl_p
                break

    # 4. 'Duvara Çarpma' (Room-to-Run) Değerlendirmesi
    has_room = True
    rejection_reason = None

    if nearest_res is not None and not is_breakout:
        if dist_res_pct is not None and dist_res_pct < min_room_to_resistance_pct:
            has_room = False
            rejection_reason = (
                f"Direnç Çok Yakın (+%{dist_res_pct:.2f} < +%{min_room_to_resistance_pct:.2f}). "
                f"Duvara çarpma riski yüksek (Direnç: ${nearest_res:.4f})"
            )

    # 5. Dinamik TP (Take-Profit)
    suggested_tp = default_tp_pct
    if dynamic_tp_enabled and nearest_res is not None and dist_res_pct is not None:
        # Eğer direnç +%1.5 ile varsayılan TP arasındaysa, kârı direncin hemen altına kilitleriz
        if 1.5 <= dist_res_pct <= default_tp_pct:
            suggested_tp = max(1.5, round(dist_res_pct - 0.15, 2))

    # 6. Yapısal SL (Stop-Loss)
    suggested_sl = default_sl_pct
    if structural_sl_enabled:
        # En yakın destek veya POC seviyesi girişin altındaysa koruyucu zemin kabul edilir
        structural_floors = []
        if nearest_sup is not None and dist_sup_pct is not None and 0.5 <= dist_sup_pct <= (default_sl_pct + 0.5):
            structural_floors.append(dist_sup_pct)

        if poc_price > 0 and poc_price < current_price:
            poc_dist_pct = ((current_price - poc_price) / current_price) * 100.0
            if 0.5 <= poc_dist_pct <= (default_sl_pct + 0.5):
                structural_floors.append(poc_dist_pct)

        if structural_floors:
            # En yakın sağlam zeminin %0.20 altına stop koy
            floor_dist = min(structural_floors)
            calc_sl = round(floor_dist + 0.20, 2)
            # Aşırı geniş stop'a izin verme, default_sl_pct ile sınırla
            suggested_sl = min(default_sl_pct, max(1.0, calc_sl))

    return MarketStructureResult(
        symbol=symbol,
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
        broken_resistance=broken_res,
        has_room_to_run=has_room,
        rejection_reason=rejection_reason,
        suggested_tp_pct=suggested_tp,
        suggested_sl_pct=suggested_sl,
        all_levels=levels[:8]  # En güçlü ilk 8 seviye
    )
