import requests
import pandas as pd

BINANCE_BASE = "https://data-api.binance.com"


def get_klines(symbol: str, interval: str, limit: int = 500) -> pd.DataFrame:
    """Последние N свечей с Binance (публичный API, без ключей)."""
    resp = requests.get(
        f"{BINANCE_BASE}/api/v3/klines",
        params={"symbol": symbol, "interval": interval, "limit": limit},
        timeout=10,
    )
    resp.raise_for_status()
    raw = resp.json()
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
    resp = requests.get(
        f"{BINANCE_BASE}/api/v3/ticker/price",
        params={"symbol": symbol},
        timeout=10,
    )
    resp.raise_for_status()
    return float(resp.json()["price"])
