import requests
import pandas as pd

# Несколько зеркал Binance на случай, если конкретный исполняющий IP
# серверлесс-функции попал под гео/юридическую блокировку (HTTP 451) на
# основном домене — перебираем по очереди, пока один не сработает.
# data-api.binance.vision — официальное лёгкое зеркало для публичных
# рыночных данных (котировки/свечи), обычно менее подвержено блокировкам.
BINANCE_BASES = [
    "https://api.binance.com",
    "https://api1.binance.com",
    "https://api2.binance.com",
    "https://api3.binance.com",
    "https://data-api.binance.vision",
]


def _get_with_fallback(path: str, params: dict) -> dict:
    last_err = None
    for base in BINANCE_BASES:
        try:
            resp = requests.get(f"{base}{path}", params=params, timeout=10)
            if resp.status_code == 451:
                last_err = f"451 (заблокировано) на {base}"
                continue
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            last_err = str(e)
            continue
    raise RuntimeError(f"Ни один домен Binance не ответил успешно. Последняя ошибка: {last_err}")


def get_klines(symbol: str, interval: str, limit: int = 500) -> pd.DataFrame:
    """Последние N свечей с Binance (публичный API, без ключей)."""
    raw = _get_with_fallback("/api/v3/klines", {"symbol": symbol, "interval": interval, "limit": limit})
    df = pd.DataFrame(raw, columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "qav", "trades", "tbbav", "tbqav", "ignore",
    ])
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
    df = df.set_index("open_time")
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = df[c].astype(float)
    return df[["open", "high", "low", "close", "volume"]]


def get_current_price(symbol: str) -> float:
    data = _get_with_fallback("/api/v3/ticker/price", {"symbol": symbol})
    return float(data["price"])
