"""
optimizer.py
============
Walk-Forward Self-Optimizing (Geçmiş Veriyle Kendi Kendine Öğrenen) Optimizasyon Motoru.

İşleyiş:
1. Son 14 günün 15 dakikalık mum verilerini hacimli ilk 5 parite (örn: BTC, ETH, SOL, DOGE, PEPE) için çeker.
2. Grid Search yöntemiyle şu parametre aralıklarını geçmiş veri üzerinde simüle eder:
   - Hacim Eşiği: [2.2x, 2.8x, 3.5x]
   - Stop-Loss Oranı: [%1.5, %2.0, %3.0]
   - Trailing TP Tetikleme: [%2.0, %3.0, %4.5]
   - Trailing Geri Çekilme (Delta): [%0.6, %1.0, %1.4]
3. En yüksek kâr faktörüne (Profit Factor = Brüt Kâr / Brüt Zarar) ve en düşük Max Drawdown'a sahip parametre kümesini seçer.
4. Yeni parametreleri config.json dosyasına yazar (Hot-Reload) ve Telegram'a rapor atar.
"""

import os
import sys

# Script dizinini sys.path'e ekle
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import json
import time
import logging
import itertools
from datetime import datetime
from typing import Dict, List, Any, Tuple, Optional
import pandas as pd

from binance_client import BinanceFuturesClient
from telegram_notifier import TelegramNotifier

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("Optimizer")

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

# Varsayılan konfigürasyon şablonu
DEFAULT_CONFIG = {
    "volume_multiplier": 2.8,
    "stop_loss_pct": 2.0,
    "trailing_trigger_pct": 3.0,
    "trailing_delta_pct": 1.0,
    "max_concurrent_positions": 0,
    "trade_amount_usdt": 100.0,
    "commission_pct": 0.12,
    "break_even_trigger_pct": 1.5,
    "break_even_lock_pct": 0.2,
    "min_taker_buy_ratio": 0.55,
    "min_close_strength": 0.60,
    "position_timeout_minutes": 40,
    "cooldown_minutes": 30,
    "scan_interval_seconds": 60,
    "top_pairs_count": 25,
    "oi_min_increase_pct": 2.0,
    "top_trader_ls_max": 2.2,
    "top_trader_ls_min": 0.65,
    "allow_long": True,
    "allow_short": True,
    "use_sr_filter": True,
    "sr_pivot_window": 5,
    "sr_cluster_tolerance_pct": 0.4,
    "min_room_to_resistance_pct": 1.2,
    "min_room_to_support_pct": 1.2,
    "dynamic_tp_enabled": True,
    "structural_sl_enabled": True,
    "blacklist": [
        "BZUSDT",
        "XAUUSDT",
        "XAGUSDT",
        "PAXGUSDT",
        "XAUTUSDT",
        "COPPERUSDT",
        "NATGASUSDT",
        "EURUSDT",
        "USDCUSDT",
        "FDUSDUSDT",
        "TUSDUSDT",
        "BUSDUSDT",
        "USDPUSDT"
    ],
    "telegram_token": "9012920209:AAG7Ydbts5nlwzurqWk9Ivv9FJY4q9uRvWA",
    "telegram_chat_id": "5355048685",
    "last_optimized_at": None,
    "model_version": "v1.0"
}


def load_config() -> Dict[str, Any]:
    """config.json dosyasını yükler, yoksa varsayılan ayarlarla oluşturur."""
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
                # Eksik anahtarları tamamla
                for k, v in DEFAULT_CONFIG.items():
                    if k not in cfg:
                        cfg[k] = v
                return cfg
        except Exception as e:
            logger.error("config.json okuma hatası, varsayılan ayarlar kullanılacak: %s", e)
    return dict(DEFAULT_CONFIG)


def save_config(new_params: Dict[str, Any], stats: Optional[Dict[str, Any]] = None) -> None:
    """Yeni parametreleri config.json dosyasına yazar."""
    current_cfg = load_config()
    current_cfg.update(new_params)
    current_cfg["last_optimized_at"] = datetime.now().isoformat()
    if stats:
        current_cfg["last_optimization_stats"] = {
            "profit_factor": round(stats.get("profit_factor", 0.0), 2),
            "win_rate": round(stats.get("win_rate", 0.0), 2),
            "max_drawdown": round(stats.get("max_drawdown", 0.0), 2),
            "total_trades": stats.get("total_trades", 0)
        }

    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(current_cfg, f, indent=4, ensure_ascii=False)
    logger.info("config.json başarıyla güncellendi (Hot-Reload hazır).")


class StrategyBacktester:
    """
    Geçmiş mum verileri üzerinde parametre kombinasyonunu simüle eden hızlı motor.
    """

    @staticmethod
    def simulate_coin(
        df: pd.DataFrame,
        vol_multiplier: float,
        stop_loss_pct: float,
        trailing_trigger_pct: float,
        trailing_delta_pct: float,
        timeout_bars: int = 8,         # 8 x 5m = 40 dk (zaman aşımı)
        cooldown_bars: int = 6,        # 6 x 5m = 30 dk soğuma
        break_even_trigger: float = 1.5,
        break_even_lock: float = 0.20,
        commission_pct: float = 0.12,
        min_taker_buy_ratio: float = 0.55,
        min_close_strength: float = 0.60
    ) -> List[Dict[str, Any]]:
        """
        Tek bir parite için geçmiş 5m veride sanal işlem simülasyonu yapar.
        Komisyon (%0.12), alıcı baskısı ve başabaş (break-even) korumasını dahil eder.
        """
        trades = []
        n = len(df)
        if n < 30:
            return trades

        closes = df["close"].values
        highs = df["high"].values
        lows = df["low"].values
        opens = df["open"].values
        volumes = df["volume"].values
        vol_smas = df["vol_sma"].values
        taker_buys = pd.to_numeric(df["taker_buy_volume"], errors="coerce").fillna(0).values if "taker_buy_volume" in df.columns else (volumes * 0.6)

        last_exit_bar = -999

        i = 20
        while i < n - 1:
            # Soğuma kontrolü
            if i - last_exit_bar < cooldown_bars:
                i += 1
                continue

            vol_sma = vol_smas[i]
            if vol_sma is None or pd.isna(vol_sma) or vol_sma <= 0:
                i += 1
                continue

            # Giriş Koşulları: Hacim anomalisi
            vol_ok = volumes[i] >= (vol_sma * vol_multiplier)
            if not vol_ok:
                i += 1
                continue

            bullish_ok = closes[i] > opens[i]
            bearish_ok = closes[i] < opens[i]
            rng = highs[i] - lows[i]

            target_side = None
            if bullish_ok:
                taker_ratio = (taker_buys[i] / volumes[i]) if volumes[i] > 0 else 0
                strength = ((closes[i] - lows[i]) / rng) if rng > 0 else 1.0
                if taker_ratio >= min_taker_buy_ratio and strength >= min_close_strength:
                    target_side = "LONG"
            elif bearish_ok:
                taker_sell = max(0.0, volumes[i] - taker_buys[i])
                taker_ratio = (taker_sell / volumes[i]) if volumes[i] > 0 else 0
                strength = ((highs[i] - closes[i]) / rng) if rng > 0 else 1.0
                if taker_ratio >= min_taker_buy_ratio and strength >= min_close_strength:
                    target_side = "SHORT"

            if target_side is not None:
                entry_price = closes[i]
                entry_bar = i
                trailing_active = False
                break_even_active = False
                peak_price = entry_price
                exit_price = entry_price
                exit_reason = "TIMEOUT"
                exit_bar = i + 1
                is_short = (target_side == "SHORT")

                # İşlemi ileriye doğru simüle et
                for j in range(i + 1, min(i + 1 + timeout_bars, n)):
                    bar_high = highs[j]
                    bar_low = lows[j]
                    bar_close = closes[j]

                    if is_short:
                        curr_profit_pct = ((entry_price - bar_low) / entry_price) * 100.0
                        if curr_profit_pct > ((entry_price - peak_price) / entry_price * 100.0):
                            peak_price = bar_low

                        # 1. Stop-Loss Kontrolü
                        if break_even_active:
                            effective_sl = entry_price * (1.0 - break_even_lock / 100.0)
                            if bar_high >= effective_sl:
                                exit_price = effective_sl
                                exit_reason = "BREAK_EVEN"
                                exit_bar = j
                                break
                        else:
                            sl_price = entry_price * (1.0 + stop_loss_pct / 100.0)
                            if bar_high >= sl_price:
                                exit_price = sl_price
                                exit_reason = "STOP_LOSS"
                                exit_bar = j
                                break

                        # Başabaş tetikleme
                        peak_profit_pct = ((entry_price - peak_price) / entry_price) * 100.0
                        if peak_profit_pct >= break_even_trigger:
                            break_even_active = True

                        # Trailing TP
                        if peak_profit_pct >= trailing_trigger_pct:
                            trailing_active = True

                        if trailing_active:
                            trailing_stop_price = peak_price * (1.0 + trailing_delta_pct / 100.0)
                            if bar_high >= trailing_stop_price:
                                exit_price = trailing_stop_price
                                exit_reason = "TRAILING_TP"
                                exit_bar = j
                                break
                    else:
                        curr_profit_pct = ((bar_high - entry_price) / entry_price) * 100.0
                        if curr_profit_pct > ((peak_price - entry_price) / entry_price * 100.0):
                            peak_price = bar_high

                        # 1. Stop-Loss Kontrolü
                        if break_even_active:
                            effective_sl = entry_price * (1.0 + break_even_lock / 100.0)
                            if bar_low <= effective_sl:
                                exit_price = effective_sl
                                exit_reason = "BREAK_EVEN"
                                exit_bar = j
                                break
                        else:
                            sl_price = entry_price * (1.0 - stop_loss_pct / 100.0)
                            if bar_low <= sl_price:
                                exit_price = sl_price
                                exit_reason = "STOP_LOSS"
                                exit_bar = j
                                break

                        # Başabaş tetikleme
                        peak_profit_pct = ((peak_price - entry_price) / entry_price) * 100.0
                        if peak_profit_pct >= break_even_trigger:
                            break_even_active = True

                        # Trailing TP
                        if peak_profit_pct >= trailing_trigger_pct:
                            trailing_active = True

                        if trailing_active:
                            trailing_stop_price = peak_price * (1.0 - trailing_delta_pct / 100.0)
                            if bar_low <= trailing_stop_price:
                                exit_price = trailing_stop_price
                                exit_reason = "TRAILING_TP"
                                exit_bar = j
                                break

                    # Zaman Aşımı son bar kontrolü
                    if j == i + timeout_bars or j == n - 1:
                        exit_price = bar_close
                        exit_reason = "TIMEOUT"
                        exit_bar = j
                        break

                # Net PnL (Borsa komisyonu düşülmüş)
                if is_short:
                    gross_pnl_pct = ((entry_price - exit_price) / entry_price) * 100.0
                else:
                    gross_pnl_pct = ((exit_price - entry_price) / entry_price) * 100.0

                net_pnl_pct = gross_pnl_pct - commission_pct
                trades.append({
                    "entry_bar": entry_bar,
                    "exit_bar": exit_bar,
                    "side": target_side,
                    "pnl_pct": net_pnl_pct,
                    "exit_reason": exit_reason
                })

                last_exit_bar = exit_bar
                i = exit_bar + 1
            else:
                i += 1

        return trades


class WalkForwardOptimizer:
    """
    Son 14 günün verisini toplayıp 81 parametre kombinasyonunu Grid Search ile tarar.
    """

    # Grid Search Aralıkları (Kullanıcı spesifikasyonu)
    VOLUME_MULTIPLIERS = [2.2, 2.8, 3.5]
    STOP_LOSS_RATES = [1.5, 2.0, 3.0]
    TRAILING_TRIGGERS = [2.0, 3.0, 4.5]
    TRAILING_DELTAS = [0.6, 1.0, 1.4]

    def __init__(self, client: Optional[BinanceFuturesClient] = None, notifier: Optional[TelegramNotifier] = None):
        self.config = load_config()
        self.client = client or BinanceFuturesClient()
        self.notifier = notifier or TelegramNotifier(
            token=self.config.get("telegram_token", ""),
            chat_id=self.config.get("telegram_chat_id", "")
        )

    def select_top_crypto_pairs(self, count: int = 5) -> List[str]:
        """
        En hacimli ilk 5 kripto vadeli paritesini seçer.
        (Örn: BTCUSDT, ETHUSDT, SOLUSDT, DOGEUSDT, 1000PEPEUSDT vb.)
        """
        blacklist = self.config.get("blacklist", [])
        pairs = self.client.get_top_volume_pairs(limit=30, blacklist=blacklist)
        target_crypto = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "DOGEUSDT", "1000PEPEUSDT", "XRPUSDT", "BNBUSDT", "SUIUSDT"]
        
        selected = []
        for p in pairs:
            if p in target_crypto and p not in selected:
                selected.append(p)
            if len(selected) >= count:
                break

        # Yeterli bulunamazsa ilk gelenleri ekle
        for p in pairs:
            if p not in selected:
                selected.append(p)
            if len(selected) >= count:
                break

        return selected[:count]

    def fetch_14d_klines(self, pairs: List[str]) -> Dict[str, pd.DataFrame]:
        """
        Her parite için son 5 günlük 5m mum verilerini çeker (1500 mum/parite).
        SMA(20) hacim indikatörünü saf pandas ile hesaplar (pandas-ta gerektirmez).
        """
        logger.info("Son 5 günlük 5m mum verileri indiriliyor (Pariteler: %s)...", pairs)
        klines_data = {}

        for symbol in pairs:
            try:
                # 1500 mum x 5 dk = 125 saat (~5.2 gün) - canlı 5m tarayıcısıyla senkronize
                df = self.client.get_klines(symbol=symbol, interval="5m", limit=1500)
                if df is not None and len(df) >= 100:
                    df["vol_sma"] = df["volume"].rolling(window=20).mean()
                    klines_data[symbol] = df
                    logger.info("✓ %s: %d mum başarıyla alındı.", symbol, len(df))
                else:
                    logger.warning("! %s için yeterli mum verisi alınamadı.", symbol)
            except Exception as e:
                logger.error("Mum indirme hatası (%s): %s", symbol, e)
            time.sleep(0.3)  # Rate limit koruma

        return klines_data

    def run_optimization(self) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """
        Grid Search döngüsünü çalıştırır, en karlı parametre kümesini belirler,
        config.json'u günceller ve Telegram'a rapor atar.
        """
        logger.info("=" * 60)
        logger.info("WALK-FORWARD SELF-OPTIMIZER BAŞLATILDI")
        logger.info("=" * 60)

        pairs = self.select_top_crypto_pairs(5)
        logger.info("Analiz edilecek pariteler: %s", pairs)

        klines_dict = self.fetch_14d_klines(pairs)
        if not klines_dict:
            logger.error("Optimizasyon için veri çekilemedi, mevcut konfigürasyon korunuyor.")
            return {}, {}

        # 3 x 3 x 3 x 3 = 81 kombinasyon
        param_grid = list(itertools.product(
            self.VOLUME_MULTIPLIERS,
            self.STOP_LOSS_RATES,
            self.TRAILING_TRIGGERS,
            self.TRAILING_DELTAS
        ))
        logger.info("Toplam %d farklı parametre kombinasyonu simüle ediliyor...", len(param_grid))

        results = []

        comm = float(self.config.get("commission_pct", 0.12))
        be_trig = float(self.config.get("break_even_trigger_pct", 1.5))
        be_lock = float(self.config.get("break_even_lock_pct", 0.2))
        min_taker = float(self.config.get("min_taker_buy_ratio", 0.55))
        min_close = float(self.config.get("min_close_strength", 0.60))

        for vol_mult, sl, tp, delta in param_grid:
            all_trades = []
            for symbol, df in klines_dict.items():
                trades = StrategyBacktester.simulate_coin(
                    df=df,
                    vol_multiplier=vol_mult,
                    stop_loss_pct=sl,
                    trailing_trigger_pct=tp,
                    trailing_delta_pct=delta,
                    break_even_trigger=be_trig,
                    break_even_lock=be_lock,
                    commission_pct=comm,
                    min_taker_buy_ratio=min_taker,
                    min_close_strength=min_close
                )
                all_trades.extend(trades)

            # Performans metriklerini hesapla
            total_trades = len(all_trades)
            if total_trades < 3:
                continue

            winning_trades = [t for t in all_trades if t["pnl_pct"] > 0]
            losing_trades = [t for t in all_trades if t["pnl_pct"] <= 0]

            gross_profit = sum(t["pnl_pct"] for t in winning_trades)
            gross_loss = abs(sum(t["pnl_pct"] for t in losing_trades))

            if gross_loss > 0:
                profit_factor = gross_profit / gross_loss
            else:
                profit_factor = gross_profit if gross_profit > 0 else 1.0

            win_rate = (len(winning_trades) / total_trades) * 100.0

            # Max Drawdown hesapla (kümülatif getiri eğrisinden)
            equity = 100.0
            equity_curve = [equity]
            for t in all_trades:
                equity *= (1.0 + t["pnl_pct"] / 100.0)
                equity_curve.append(equity)

            peak = equity_curve[0]
            max_dd = 0.0
            for eq in equity_curve:
                if eq > peak:
                    peak = eq
                dd = ((peak - eq) / peak) * 100.0
                if dd > max_dd:
                    max_dd = dd

            # Skorlama: Profit Factor en yüksek, Max Drawdown en düşük
            # Profit Factor birincil, Max Drawdown cezalandırıcı
            score = profit_factor / (1.0 + max_dd * 0.05)

            results.append({
                "params": {
                    "volume_multiplier": vol_mult,
                    "stop_loss_pct": sl,
                    "trailing_trigger_pct": tp,
                    "trailing_delta_pct": delta
                },
                "stats": {
                    "profit_factor": profit_factor,
                    "win_rate": win_rate,
                    "max_drawdown": max_dd,
                    "total_trades": total_trades,
                    "gross_profit": gross_profit,
                    "gross_loss": gross_loss,
                    "pairs": list(klines_dict.keys())
                },
                "score": score
            })

        if not results:
            logger.warning("Yeterli işlem üreten kombinasyon bulunamadı. Varsayılan ayarlar korunuyor.")
            return {}, {}

        # En iyi kombinasyonu seç (En yüksek skor / Profit Factor, en düşük MDD)
        results.sort(key=lambda x: (x["score"], x["stats"]["profit_factor"], -x["stats"]["max_drawdown"]), reverse=True)
        best = results[0]
        best_params = best["params"]
        best_stats = best["stats"]

        logger.info("=" * 60)
        logger.info("OPTİMİZASYON TAMAMLANDI - EN İYİ PARAMETRELER:")
        logger.info("Hacim Eşiği: %.1fx | Stop-Loss: %%%.1f | Trailing TP: %%%.1f (Delta: %%%.1f)",
                    best_params["volume_multiplier"], best_params["stop_loss_pct"],
                    best_params["trailing_trigger_pct"], best_params["trailing_delta_pct"])
        logger.info("Profit Factor: %.2f | Win Rate: %%%.1f | Max DD: %%%.2f | Toplam İşlem: %d",
                    best_stats["profit_factor"], best_stats["win_rate"],
                    best_stats["max_drawdown"], best_stats["total_trades"])
        logger.info("=" * 60)

        # 1. config.json'a kaydet (Hot-Reload)
        save_config(best_params, best_stats)

        # 2. Telegram'a kullanıcı şablonunda bildirim at
        self.notifier.notify_model_updated(best_params, best_stats)

        return best_params, best_stats


def run_optimization() -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Dış modüllerden çağırmak için doğrudan fonksiyon arayüzü."""
    opt = WalkForwardOptimizer()
    return opt.run_optimization()


if __name__ == "__main__":
    run_optimization()
