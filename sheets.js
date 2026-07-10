// Публичный Binance API — не требует ключей, только для чтения цен.
const BINANCE_BASE = "https://api.binance.com";

/**
 * Загружает последние N свечей (klines) для символа/таймфрейма.
 * Возвращает массив объектов { openTime, open, high, low, close, volume, closeTime }
 * в порядке от старых к новым.
 */
async function getKlines(symbol, interval, limit = 200) {
  const url = `${BINANCE_BASE}/api/v3/klines?symbol=${symbol}&interval=${interval}&limit=${limit}`;
  const resp = await fetch(url);
  if (!resp.ok) {
    const text = await resp.text().catch(() => "");
    throw new Error(`Binance klines error ${resp.status}: ${text}`);
  }
  const raw = await resp.json();
  return raw.map((k) => ({
    openTime: k[0],
    open: parseFloat(k[1]),
    high: parseFloat(k[2]),
    low: parseFloat(k[3]),
    close: parseFloat(k[4]),
    volume: parseFloat(k[5]),
    closeTime: k[6],
  }));
}

/** Текущая цена (последняя сделка) по символу. */
async function getCurrentPrice(symbol) {
  const url = `${BINANCE_BASE}/api/v3/ticker/price?symbol=${symbol}`;
  const resp = await fetch(url);
  if (!resp.ok) {
    const text = await resp.text().catch(() => "");
    throw new Error(`Binance price error ${resp.status}: ${text}`);
  }
  const data = await resp.json();
  return parseFloat(data.price);
}

module.exports = { getKlines, getCurrentPrice };
