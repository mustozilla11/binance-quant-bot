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
        
        # TradFi (Emtia/Hisse) sembolleri önbelleği
        self._tradifi_symbols: set[str] = set()
        self._last_tradifi_fetch: float = 0.0

        # HTF ve Mum önbelleği (symbol_interval -> (timestamp, df))
        self._klines_cache: Dict[str, Any] = {}

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

    def get_tradifi_symbols(self) -> set[str]:
        """
        Binance Futures exchangeInfo üzerinden geleneksel finans (TradFi)
        türevlerini (Gümüş XAG, Altın XAU, Petrol BZ, Hisse/ETF tokenları) dinamik tespit eder.
        """
        now = time.time()
        # 1 saatte bir önbelleği tazele
        if self._tradifi_symbols and (now - self._last_tradifi_fetch < 3600):
            return self._tradifi_symbols

        try:
            info = self._request("/fapi/v1/exchangeInfo")
            if info and "symbols" in info:
                tradifi = set()
                for s in info["symbols"]:
                    sym = s.get("symbol", "")
                    ctype = s.get("contractType", "")
                    utype = s.get("underlyingType", "")
                    if ctype == "TRADIFI_PERPETUAL" or utype in ["COMMODITY", "EQUITY", "INDEX"]:
                        tradifi.add(sym)
                if tradifi:
                    self._tradifi_symbols = tradifi
                    self._last_tradifi_fetch = now
                    logger.info("Binance TradFi filtresi güncellendi: %d adet emtia/hisse paritesi elendi.", len(tradifi))
        except Exception as e:
            logger.warning("TradFi sembollerini çekerken hata: %s", e)

        return self._tradifi_symbols

    def get_top_volume_pairs(self, limit: int = 25, blacklist: Optional[Any] = None) -> List[str]:
        """
        24 saatlik en yüksek USDT hacmine sahip saf kripto vadeli paritelerini çeker.
        Gümüş (XAG), Altın (XAU), Petrol (BZ), hisseler, ETF'ler ve sabit coinler tamamen elenir.
        Her 60 saniyede bir çağrıldığında anlık 24h hacme göre listeyi günceller.
        """
        data = self._request("/fapi/v1/ticker/24hr")
        if not data or not isinstance(data, list):
            return ["BTCUSDT", "ETHUSDT", "SOLUSDT", "DOGEUSDT", "PEPEUSDT"]

        # Temel sabit coinler ve bilinen emtia/geleneksel varlıklar
        base_excluded = {
            "USDCUSDT", "FDUSDUSDT", "TUSDUSDT", "BUSDUSDT", "EURUSDT", "USDPUSDT",
            "STABLEUSDT", "USDEXUSDT", "FRAXUSDT", "XAGUSDT", "XAUUSDT", "BZUSDT",
            "PAXGUSDT", "XAUTUSDT", "COPPERUSDT", "NATGASUSDT"
        }
        
        # Binance API'den otomatik tespit edilen tüm TradFi sembolleri (230+ adet)
        tradifi = self.get_tradifi_symbols()
        excluded = base_excluded.union(tradifi)
        if blacklist:
            excluded = excluded.union(set(blacklist))

        usdt_pairs = []

        for item in data:
            symbol = item.get("symbol", "")
            if symbol.endswith("USDT") and symbol not in excluded:
                try:
                    quote_volume = float(item.get("quoteVolume", 0.0))
                    usdt_pairs.append((symbol, quote_volume))
                except (ValueError, TypeError):
                    continue

        # Hacme göre azalan sırala
        usdt_pairs.sort(key=lambda x: x[1], reverse=True)
        return [pair[0] for pair in usdt_pairs[:limit]]

    def get_klines(self, symbol: str, interval: str = "5m", limit: int = 100, cache_ttl_sec: int = 0, end_time: Optional[int] = None) -> Optional[pd.DataFrame]:
        """
        Mum (kline) verilerini çeker ve pandas DataFrame olarak döndürür.
        cache_ttl_sec > 0 ise belirtilen süre boyunca (örn. 1h verisi için 300s) önbellekten sunar.
        """
        now = time.time()
        cache_key = f"{symbol}_{interval}_{limit}_{end_time}"

        if cache_ttl_sec > 0 and cache_key in self._klines_cache:
            cached_time, cached_df = self._klines_cache[cache_key]
            if (now - cached_time) < cache_ttl_sec:
                return cached_df.copy()

        params: Dict[str, Any] = {
            "symbol": symbol,
            "interval": interval,
            "limit": limit
        }
        if end_time is not None:
            params["endTime"] = end_time
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
        numeric_cols = ["open", "high", "low", "close", "volume", "quote_volume", "taker_buy_volume", "taker_buy_quote_volume"]
        for col in numeric_cols:
            df[col] = pd.to_numeric(df[col], errors="coerce")

        df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
        df["close_time"] = pd.to_datetime(df["close_time"], unit="ms")

        if cache_ttl_sec > 0:
            self._klines_cache[cache_key] = (now, df)

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

    def get_funding_rate(self, symbol: str) -> Optional[float]:
        """
        Sembolün anlık fonlama oranını (Funding Rate) yüzdesel (%) olarak getirir.
        /fapi/v1/premiumIndex?symbol=...
        Örn: 0.0001 -> %0.01 | -0.0002 -> -%0.02 (Negatif Fonlama = Sıkışan Shortlar)
        """
        data = self._request("/fapi/v1/premiumIndex", params={"symbol": symbol})
        if data and isinstance(data, dict) and "lastFundingRate" in data:
            try:
                rate = float(data["lastFundingRate"]) * 100.0
                return round(rate, 4)
            except (ValueError, TypeError):
                pass
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
