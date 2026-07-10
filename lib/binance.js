const fetch = require('node-fetch');

async function getPrice(symbol) {
  const url = `https://api.binance.com/api/v3/ticker/price?symbol=${symbol}`;
  const response = await fetch(url);
  const data = await response.json();
  return parseFloat(data.price);
}

module.exports = { getPrice };
