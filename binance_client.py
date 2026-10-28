"""
binance_client.py
=================
Binance USDT-M Vadeli İşlemler Public API İstemcisi.
Borsa API Anahtarı KULLANMAZ (Yalnızca herkese açık /fapi/v1/* ve /futures/data/* uç noktaları).
DPI/ISP kısıtlamalarını aşmak için akıllı CloudFront yedekleme ve otomatik yeniden deneme (backoff) mekanizmasına sahiptir.
"""

import time
import logging
import requests
import pandas as pd
from typing import List, Dict, Any, Optional

logger = logging.getLogger("BinanceClient")

class BinanceFuturesClient:
    """
    Binance Vadeli İşlemler için sağlam, API-anahtarsız REST istemcisi.
    """
    DEFAULT_HOST = "fapi.binance.com"
    CLOUDFRONT_HOST = "d2ukl3c6tymv7q.cloudfront.net"

    def __init__(self, timeout: int = 10, max_retries: int = 3):
        self.timeout = timeout
        self.max_retries = max_retries
        self.session = requests.Session()
        
        # Türkiye / ISP SNI filtreleme kontrolü ve uygun URL kökünü belirleme
        self.base_url, self.custom_headers = self._determine_working_endpoint()

    def _determine_working_endpoint(self) -> tuple[str, dict]:
        """
        Doğrudan bağlantıyı test eder. ISP/DPI engeli (SSL: WRONG_VERSION_NUMBER)
        varsa CloudFront CNAME üzerinden bağlantı kurar.
        """
        direct_url = f"https://{self.DEFAULT_HOST}/fapi/v1/ping"
        try:
            r = self.session.get(direct_url, timeout=3)
            if r.status_code == 200:
                logger.info("Doğrudan Binance API bağlantısı başarılı: https://%s", self.DEFAULT_HOST)
                return f"https://{self.DEFAULT_HOST}", {}
        except Exception:
            pass

        # CloudFront yedek uç noktası
        cf_url = f"https://{self.CLOUDFRONT_HOST}/fapi/v1/ping"
        cf_headers = {"Host": self.DEFAULT_HOST}
        try:
            r = self.session.get(cf_url, headers=cf_headers, timeout=5)
            if r.status_code == 200:
                logger.info("Yedek CloudFront uç noktası aktif edildi: https://%s", self.CLOUDFRONT_HOST)
                return f"https://{self.CLOUDFRONT_HOST}", cf_headers
        except Exception as e:
            logger.warning("Yedek uç nokta testi sırasında hata: %s", e)

        # Varsayılan fallback
        return f"https://{self.CLOUDFRONT_HOST}", {"Host": self.DEFAULT_HOST}

    def _request(self, endpoint: str, params: Optional[Dict[str, Any]] = None) -> Any:
        """
        Hata yakalama, rate-limit koruması ve backoff ile API isteği gönderir.
        """
        url = f"{self.base_url}{endpoint}"
        headers = dict(self.custom_headers)
        
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = self.session.get(url, params=params, headers=headers, timeout=self.timeout)
                if resp.status_code == 200:
                    return resp.json()
                elif resp.status_code == 429:
                    wait_time = int(resp.headers.get("Retry-After", 10))
                    logger.warning("Rate limit aşıldı (429)! %d saniye bekleniyor...", wait_time)
                    time.sleep(wait_time)
                else:
                    logger.warning("API Yanıt Kodu %d (%s) - Deneme %d/%d", resp.status_code, endpoint, attempt, self.max_retries)
                    time.sleep(1.0 * attempt)
            except Exception as e:
                logger.error("API Bağlantı Hatası: %s (Deneme %d/%d)", e, attempt, self.max_retries)
                time.sleep(1.5 * attempt)

        return None

    def get_top_volume_pairs(self, limit: int = 25) -> List[str]:
        """
        24 saatlik en yüksek USDT hacmine sahip vadeli pariteleri çeker.
        Sabit coinler (USDC, FDUSD vb.) elenir.
        """
        data = self._request("/fapi/v1/ticker/24hr")
        if not data or not isinstance(data, list):
            return ["BTCUSDT", "ETHUSDT", "SOLUSDT", "DOGEUSDT", "PEPEUSDT"]

        # Yalnızca USDT paritelerini filtrele ve stabil coin çiftlerini ele
        excluded_stables = {"USDCUSDT", "FDUSDUSDT", "TUSDUSDT", "BUSDUSDT", "EURUSDT", "USDPUSDT"}
        usdt_pairs = []

        for item in data:
            symbol = item.get("symbol", "")
            if symbol.endswith("USDT") and symbol not in excluded_stables:
                try:
                    quote_volume = float(item.get("quoteVolume", 0.0))
                    usdt_pairs.append((symbol, quote_volume))
                except (ValueError, TypeError):
                    continue

        # Hacme göre azalan sırala
        usdt_pairs.sort(key=lambda x: x[1], reverse=True)
        return [pair[0] for pair in usdt_pairs[:limit]]

    def get_klines(self, symbol: str, interval: str = "5m", limit: int = 100) -> Optional[pd.DataFrame]:
        """
        Mum (kline) verilerini çeker ve pandas DataFrame olarak döndürür.
        """
        params = {
            "symbol": symbol,
            "interval": interval,
            "limit": limit
        }
        raw_klines = self._request("/fapi/v1/klines", params=params)
        if not raw_klines or not isinstance(raw_klines, list):
            return None

        columns = [
            "open_time", "open", "high", "low", "close", "volume",
            "close_time", "quote_volume", "count", "taker_buy_volume",
            "taker_buy_quote_volume", "ignore"
        ]
        df = pd.DataFrame(raw_klines, columns=columns)
        
        # Sayısal dönüşümler
        numeric_cols = ["open", "high", "low", "close", "volume", "quote_volume"]
        for col in numeric_cols:
            df[col] = pd.to_numeric(df[col], errors="coerce")

        df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
        df["close_time"] = pd.to_datetime(df["close_time"], unit="ms")
        return df

    def get_open_interest_change(self, symbol: str) -> Optional[float]:
        """
        Son 15 dakikadaki Açık Pozisyon (Open Interest) yüzdesel değişimini hesaplar.
        /futures/data/openInterestHist?symbol=...&period=15m&limit=2
        """
        params = {
            "symbol": symbol,
            "period": "15m",
            "limit": 3
        }
        data = self._request("/futures/data/openInterestHist", params=params)
        if not data or not isinstance(data, list) or len(data) < 2:
            return None

        try:
            # Kronolojik sıralı son iki veriyi al
            prev_oi = float(data[-2]["sumOpenInterest"])
            curr_oi = float(data[-1]["sumOpenInterest"])
            if prev_oi <= 0:
                return 0.0
            pct_change = ((curr_oi - prev_oi) / prev_oi) * 100.0
            return pct_change
        except (KeyError, ValueError, ZeroDivisionError, IndexError) as e:
            logger.debug("OI hesaplama hatası (%s): %s", symbol, e)
            return None

    def get_top_trader_long_short_ratio(self, symbol: str) -> Optional[float]:
        """
        Top Trader Long/Short pozisyon oranını getirir.
        /futures/data/topLongShortPositionRatio?symbol=...&period=15m&limit=1
        """
        params = {
            "symbol": symbol,
            "period": "15m",
            "limit": 2
        }
        data = self._request("/futures/data/topLongShortPositionRatio", params=params)
        if not data or not isinstance(data, list) or len(data) == 0:
            return None

        try:
            latest = data[-1]
            ratio = float(latest["longShortRatio"])
            return ratio
        except (KeyError, ValueError, IndexError) as e:
            logger.debug("L/S oranı hesaplama hatası (%s): %s", symbol, e)
            return None

    def get_latest_price(self, symbol: str) -> Optional[float]:
        """
        Tek bir sembolün güncel fiyatını getirir.
        """
        data = self._request("/fapi/v1/ticker/price", params={"symbol": symbol})
        if data and "price" in data:
            try:
                return float(data["price"])
            except ValueError:
                pass
        return None

    def get_all_prices(self) -> Dict[str, float]:
        """
        Tüm vadeli sembollerin anlık fiyat haritasını döndürür.
        """
        data = self._request("/fapi/v1/ticker/price")
        price_map = {}
        if data and isinstance(data, list):
            for item in data:
                s = item.get("symbol")
                p = item.get("price")
                if s and p:
                    try:
                        price_map[s] = float(p)
                    except ValueError:
                        continue
        return price_map
