const { getKlines, getCurrentPrice } = require('../lib/binance');
const { findEntrySignal, checkPosition } = require('../lib/strategy');
const sheets = require('../lib/sheets');

const SYMBOL = process.env.SYMBOL || "BTCUSDT";
const INTERVAL = process.env.INTERVAL || "15m";
const RISK_PCT = parseFloat(process.env.RISK_PCT || "1");
const DEFAULT_BALANCE = parseFloat(process.env.INITIAL_BALANCE || "1000");

module.exports = async (req, res) => {
  const secret = req.query.secret || (req.headers.authorization || "").replace("Bearer ", "");
  if (secret !== process.env.CRON_SECRET) {
    res.status(401).json({ error: "unauthorized" });
    return;
  }

  try {
    const currentPrice = await getCurrentPrice(SYMBOL);
    const state = await sheets.getState();

    if (state && state.symbol === SYMBOL) {
      const result = checkPosition(state, currentPrice);

      if (result.action === "partial") {
        const closedQty = state.qty * 0.2;
        const pnlPart = state.direction === "long"
          ? (result.exitPrice - state.entry) * closedQty
          : (state.entry - result.exitPrice) * closedQty;

        await sheets.appendPartialNote(
          state.tradeRow,
          `Частичный тейк ${(pnlPart >= 0 ? "+" : "")}${pnlPart.toFixed(2)}$ на ${result.exitPrice.toFixed(4)}, SL -> БУ`
        );
        await sheets.setState({
          ...state,
          sl: result.newSl,
          partialTaken: true,
          qty: state.qty - closedQty,
        });

        res.status(200).json({ status: "partial_tp", price: currentPrice, pnlPart });
        return;
      }

      if (result.action === "close") {
        const pnl = state.direction === "long"
          ? (result.exitPrice - state.entry) * state.qty
          : (state.entry - result.exitPrice) * state.qty;
        const pnlPct = (pnl / (state.entry * state.qty)) * 100;

        await sheets.updateTradeClose(state.tradeRow, {
          exitPrice: result.exitPrice, pnl, pnlPct, exitReason: result.exitReason,
        });
        const newBalance = await sheets.updateSummary(pnl);
        await sheets.clearState();

        res.status(200).json({ status: "closed", exitReason: result.exitReason, pnl, newBalance });
        return;
      }

      res.status(200).json({ status: "holding", price: currentPrice });
      return;
    }

    const klines = await getKlines(SYMBOL, INTERVAL, 200);
    const signal = findEntrySignal(klines, currentPrice);

    if (!signal) {
      res.status(200).json({ status: "no_signal", price: currentPrice });
      return;
    }

    const balance = await sheets.getBalance(DEFAULT_BALANCE);
    const riskAmount = balance * (RISK_PCT / 100);
    const riskPerUnit = Math.abs(signal.entry - signal.sl);
    const qty = riskPerUnit > 0 ? riskAmount / riskPerUnit : 0;

    if (qty <= 0) {
      res.status(200).json({ status: "signal_invalid_qty", signal });
      return;
    }

    const tradeRow = await sheets.appendTradeOpen({
      symbol: SYMBOL, direction: signal.direction, reason: signal.reason,
      entry: signal.entry, sl: signal.sl, fullTp: signal.fullTp, qty,
    });
    await sheets.setState({
      symbol: SYMBOL, direction: signal.direction, entry: signal.entry, sl: signal.sl,
      partialTp: signal.partialTp, fullTp: signal.fullTp, partialTaken: false,
      tradeRow, qty,
    });

    res.status(200).json({ status: "opened", signal, qty, tradeRow });
  } catch (err) {
    console.error(err);
    res.status(500).json({ error: err.message });
  }
};
