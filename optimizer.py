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
    "max_concurrent_positions": 5,
    "trade_amount_usdt": 100.0,
    "commission_pct": 0.12,
    "break_even_trigger_pct": 1.5,
    "break_even_lock_pct": 0.2,
    "min_taker_buy_ratio": 0.58,
    "min_close_strength": 0.65,
    "max_24h_pump_pct": 60.0,
    "max_allowed_atr_pct": 4.5,
    "btc_circuit_breaker_enabled": True,
    "btc_15m_drop_threshold_pct": -0.80,
    "position_timeout_minutes": 40,
    "cooldown_minutes": 60,
    "scan_interval_seconds": 60,
    "top_pairs_count": 25,
    "oi_min_increase_pct": 2.0,
    "top_trader_ls_max": 2.2,
    "top_trader_ls_min": 0.80,
    "allow_long": True,
    "allow_short": True,
    "short_tp_pct": 1.6,
    "short_delta_pct": 0.4,
    "short_sl_pct": 1.5,
    "short_timeout_minutes": 15,
    "short_break_even_trigger_pct": 1.0,
    "short_break_even_lock_pct": 0.15,
    "use_sr_filter": True,
    "sr_pivot_window": 5,
    "sr_cluster_tolerance_pct": 0.4,
    "min_room_to_resistance_pct": 1.2,
    "min_room_to_support_pct": 1.2,
    "dynamic_tp_enabled": True,
    "structural_sl_enabled": True,
    "use_htf_filter": True,
    "htf_interval": "1h",
    "use_atr_dynamic_sl": True,
    "atr_period": 14,
    "atr_sl_multiplier": 2.0,
    "atr_tp_multiplier": 2.5,
    "squeeze_funding_rate_threshold": -0.01,
    "min_sl_pct": 1.0,
    "max_sl_pct": 3.5,
    "max_candle_atr_ratio": 3.0,
    "max_long_rsi": 78.0,
    "min_short_rsi": 22.0,
    "walk_forward_train_ratio": 0.70,
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

    @staticmethod
    def evaluate_trades(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        İşlem listesinin kâr faktörü, kazanma oranı ve maksimum drawdown metriklerini hesaplar.
        """
        total_trades = len(trades)
        if total_trades == 0:
            return {
                "profit_factor": 0.0,
                "win_rate": 0.0,
                "max_drawdown": 100.0,
                "total_trades": 0,
                "gross_profit": 0.0,
                "gross_loss": 0.0,
                "score": 0.0
            }

        winning = [t for t in trades if t["pnl_pct"] > 0]
        losing = [t for t in trades if t["pnl_pct"] <= 0]

        gross_profit = sum(t["pnl_pct"] for t in winning)
        gross_loss = abs(sum(t["pnl_pct"] for t in losing))

        if gross_loss > 0:
            profit_factor = gross_profit / gross_loss
        else:
            profit_factor = gross_profit if gross_profit > 0 else 1.0

        win_rate = (len(winning) / total_trades) * 100.0

        # Kümülatif getiri eğrisinden Max Drawdown
        equity = 100.0
        equity_curve = [equity]
        for t in trades:
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

        score = profit_factor / (1.0 + max_dd * 0.05)
        return {
            "profit_factor": round(float(profit_factor), 2),
            "win_rate": round(float(win_rate), 2),
            "max_drawdown": round(float(max_dd), 2),
            "total_trades": total_trades,
            "gross_profit": round(float(gross_profit), 2),
            "gross_loss": round(float(gross_loss), 2),
            "score": round(float(score), 3)
        }

    def fetch_multi_regime_klines(self, pairs: List[str], target_candles: int = 2000) -> Dict[str, pd.DataFrame]:
        """
        Çoklu Piyasa Rejimi (Boğa, Ayı, Testere) için genişletilmiş geçmiş veri çeker (~25-30 gün).
        15 dakikalık mumlarla iki parça halinde toplam 2000+ mum indirip birleştirir.
        SMA(20) hacim ortalamasını hesaplar.
        """
        logger.info("Çoklu rejim için son ~25-30 günlük geçmiş veriler indiriliyor (Pariteler: %s)...", pairs)
        klines_data = {}

        for symbol in pairs:
            try:
                # 1. Parça: Son 1500 mum (15.6 gün)
                df1 = self.client.get_klines(symbol=symbol, interval="15m", limit=1500)
                if df1 is None or len(df1) < 100:
                    logger.warning("! %s için yeterli 15m verisi alınamadı, 5m deneniyor...", symbol)
                    df1 = self.client.get_klines(symbol=symbol, interval="5m", limit=1500)

                combined_df = df1
                if df1 is not None and len(df1) >= 100:
                    earliest_time = df1.iloc[0]["open_time"]
                    if hasattr(earliest_time, "timestamp"):
                        end_ms = int(earliest_time.timestamp() * 1000) - 1
                        df2 = self.client.get_klines(symbol=symbol, interval="15m", limit=1000, end_time=end_ms)
                        if df2 is not None and len(df2) > 0:
                            combined_df = pd.concat([df2, df1], ignore_index=True).drop_duplicates(subset=["open_time"]).sort_values("open_time").reset_index(drop=True)

                if combined_df is not None and len(combined_df) >= 200:
                    combined_df["vol_sma"] = combined_df["volume"].rolling(window=20).mean()
                    klines_data[symbol] = combined_df
                    days_covered = (len(combined_df) * 15) / (60 * 24)
                    logger.info("✓ %s: %d mum başarıyla alındı (~%.1f günlük piyasa rejim verisi).",
                                symbol, len(combined_df), days_covered)
                else:
                    logger.warning("! %s için veri yetersiz.", symbol)
            except Exception as e:
                logger.error("Mum indirme hatası (%s): %s", symbol, e)
            time.sleep(0.3)  # Rate limit koruma

        return klines_data

    def run_optimization(self) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """
        Walk-Forward In-Sample (%70) ve Out-of-Sample (%30) doğrulama ile
        81 parametre kombinasyonunu simüle eder. Aşırı uyum (overfitting) gösterenleri eler,
        en sağlam (robust) parametre kümesini config.json'a yazar.
        """
        logger.info("=" * 60)
        logger.info("WALK-FORWARD IN-SAMPLE & OUT-OF-SAMPLE OPTIMIZER BAŞLATILDI")
        logger.info("=" * 60)

        pairs = self.select_top_crypto_pairs(5)
        logger.info("Analiz edilecek pariteler: %s", pairs)

        klines_dict = self.fetch_multi_regime_klines(pairs)
        if not klines_dict:
            logger.error("Optimizasyon için veri çekilemedi, mevcut konfigürasyon korunuyor.")
            return {}, {}

        # Veriyi In-Sample (%70 Eğitim) ve Out-of-Sample (%30 Test) olarak böl
        train_ratio = float(self.config.get("walk_forward_train_ratio", 0.70))
        train_dict = {}
        test_dict = {}

        for sym, df in klines_dict.items():
            split_idx = int(len(df) * train_ratio)
            train_dict[sym] = df.iloc[:split_idx].reset_index(drop=True)
            test_dict[sym] = df.iloc[split_idx:].reset_index(drop=True)

        logger.info("Veri Walk-Forward bölündü: %%70 Eğitim (In-Sample), %%30 Doğrulama (Out-of-Sample)")

        # 3 x 3 x 3 x 3 = 81 kombinasyon
        param_grid = list(itertools.product(
            self.VOLUME_MULTIPLIERS,
            self.STOP_LOSS_RATES,
            self.TRAILING_TRIGGERS,
            self.TRAILING_DELTAS
        ))
        logger.info("Toplam %d farklı parametre kombinasyonu In-Sample & Out-of-Sample simüle ediliyor...", len(param_grid))

        comm = float(self.config.get("commission_pct", 0.12))
        be_trig = float(self.config.get("break_even_trigger_pct", 1.5))
        be_lock = float(self.config.get("break_even_lock_pct", 0.2))
        min_taker = float(self.config.get("min_taker_buy_ratio", 0.55))
        min_close = float(self.config.get("min_close_strength", 0.60))

        candidates = []

        for vol_mult, sl, tp, delta in param_grid:
            # 1. Aşama: In-Sample (Eğitim Verisi) Simülasyonu
            train_trades = []
            for symbol, df_train in train_dict.items():
                t = StrategyBacktester.simulate_coin(
                    df=df_train,
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
                train_trades.extend(t)

            train_stats = self.evaluate_trades(train_trades)
            if train_stats["total_trades"] < 3 or train_stats["profit_factor"] < 0.8:
                continue

            # 2. Aşama: Out-of-Sample (Görülmemiş Test Verisi) Doğrulaması
            test_trades = []
            for symbol, df_test in test_dict.items():
                t = StrategyBacktester.simulate_coin(
                    df=df_test,
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
                test_trades.extend(t)

            test_stats = self.evaluate_trades(test_trades)

            # Aşırı Uyum (Overfitting) Koruması:
            # Test verisinde kâr faktörü 0.90 altına düşen veya ağır drawdown yaşayanları cezalandır
            is_overfitted = False
            if test_stats["total_trades"] >= 2 and test_stats["profit_factor"] < 0.90:
                is_overfitted = True

            # Birleşik Robustness Skoru: Out-of-Sample performansı daha yüksek ağırlığa (%60) sahiptir
            if is_overfitted:
                combined_score = test_stats["score"] * 0.2
            else:
                combined_score = (train_stats["score"] * 0.40) + (test_stats["score"] * 0.60)

            candidates.append({
                "params": {
                    "volume_multiplier": vol_mult,
                    "stop_loss_pct": sl,
                    "trailing_trigger_pct": tp,
                    "trailing_delta_pct": delta
                },
                "train_stats": train_stats,
                "test_stats": test_stats,
                "is_overfitted": is_overfitted,
                "score": combined_score
            })

        if not candidates:
            logger.warning("Yeterli işlem üreten kombinasyon bulunamadı. Mevcut ayarlar korunuyor.")
            return {}, {}

        # En sağlam (en yüksek skorlu ve aşırı uyumsuz) kombinasyonu seç
        candidates.sort(key=lambda x: (not x["is_overfitted"], x["score"]), reverse=True)
        best = candidates[0]
        best_params = best["params"]
        best_train = best["train_stats"]
        best_test = best["test_stats"]

        logger.info("=" * 60)
        logger.info("WALK-FORWARD DOĞRULAMA TAMAMLANDI - EN SAĞLAM PARAMETRELER:")
        logger.info("Hacim Eşiği: %.1fx | Stop-Loss: %%%.1f | Trailing TP: %%%.1f (Delta: %%%.1f)",
                    best_params["volume_multiplier"], best_params["stop_loss_pct"],
                    best_params["trailing_trigger_pct"], best_params["trailing_delta_pct"])
        logger.info("In-Sample (Train)  -> PF: %.2f | Win Rate: %%%.1f | Max DD: %%%.2f | İşlem: %d",
                    best_train["profit_factor"], best_train["win_rate"],
                    best_train["max_drawdown"], best_train["total_trades"])
        logger.info("Out-of-Sample(Test)-> PF: %.2f | Win Rate: %%%.1f | Max DD: %%%.2f | İşlem: %d",
                    best_test["profit_factor"], best_test["win_rate"],
                    best_test["max_drawdown"], best_test["total_trades"])
        logger.info("=" * 60)

        # Rapor için birleşik istatistik
        combined_stats = {
            "profit_factor": best_test["profit_factor"] if best_test["total_trades"] > 0 else best_train["profit_factor"],
            "win_rate": best_test["win_rate"] if best_test["total_trades"] > 0 else best_train["win_rate"],
            "max_drawdown": max(best_train["max_drawdown"], best_test["max_drawdown"]),
            "total_trades": best_train["total_trades"] + best_test["total_trades"],
            "train_pf": best_train["profit_factor"],
            "test_pf": best_test["profit_factor"],
            "pairs": list(klines_dict.keys())
        }

        # 1. config.json'a kaydet (Hot-Reload)
        save_config(best_params, combined_stats)

        # 2. Telegram'a kullanıcı şablonunda bildirim at
        self.notifier.notify_model_updated(best_params, combined_stats)

        return best_params, combined_stats


def run_optimization() -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Dış modüllerden çağırmak için doğrudan fonksiyon arayüzü."""
    opt = WalkForwardOptimizer()
    return opt.run_optimization()


if __name__ == "__main__":
    run_optimization()
