const { google } = require("googleapis");

// Структура Google Sheet, которую нужно создать вручную (см. README):
//
// Вкладка "Trades" (заголовки в строке 1):
//   Date | Symbol | Direction | Reason | Entry | SL | TP | Qty | ExitPrice | ExitTime | PnL | PnL% | Status
//
// Вкладка "State" (одна строка данных, строка 2):
//   Symbol | Direction | Entry | SL | PartialTP | FullTP | PartialTaken | TradeRow | Qty
//
// Вкладка "Summary" (одна строка данных, строка 2):
//   TotalTrades | Wins | Losses | TotalPnL | Balance

function getAuth() {
  const email = process.env.GOOGLE_SERVICE_ACCOUNT_EMAIL;
  const key = (process.env.GOOGLE_PRIVATE_KEY || "").replace(/\\n/g, "\n");
  if (!email || !key) {
    throw new Error("Не заданы GOOGLE_SERVICE_ACCOUNT_EMAIL / GOOGLE_PRIVATE_KEY в переменных окружения.");
  }
  return new google.auth.JWT(email, null, key, ["https://www.googleapis.com/auth/spreadsheets"]);
}

function getSheetsClient() {
  return google.sheets({ version: "v4", auth: getAuth() });
}

const SHEET_ID = () => {
  const id = process.env.GOOGLE_SHEET_ID;
  if (!id) throw new Error("Не задан GOOGLE_SHEET_ID в переменных окружения.");
  return id;
};

async function getState() {
  const sheets = getSheetsClient();
  const res = await sheets.spreadsheets.values.get({
    spreadsheetId: SHEET_ID(),
    range: "State!A2:I2",
  });
  const row = res.data.values && res.data.values[0];
  if (!row || !row[0]) return null;
  return {
    symbol: row[0],
    direction: row[1],
    entry: parseFloat(row[2]),
    sl: parseFloat(row[3]),
    partialTp: parseFloat(row[4]),
    fullTp: parseFloat(row[5]),
    partialTaken: row[6] === "TRUE" || row[6] === "true",
    tradeRow: parseInt(row[7], 10),
    qty: parseFloat(row[8]),
  };
}

async function setState(position) {
  const sheets = getSheetsClient();
  await sheets.spreadsheets.values.update({
    spreadsheetId: SHEET_ID(),
    range: "State!A2:I2",
    valueInputOption: "RAW",
    requestBody: {
      values: [[
        position.symbol, position.direction, position.entry, position.sl,
        position.partialTp, position.fullTp,
        position.partialTaken ? "TRUE" : "FALSE",
        position.tradeRow, position.qty,
      ]],
    },
  });
}

async function clearState() {
  const sheets = getSheetsClient();
  await sheets.spreadsheets.values.update({
    spreadsheetId: SHEET_ID(),
    range: "State!A2:I2",
    valueInputOption: "RAW",
    requestBody: { values: [["", "", "", "", "", "", "", "", ""]] },
  });
}

/** Текущий баланс из Summary (для расчета размера позиции по % риска). */
async function getBalance(defaultBalance) {
  const sheets = getSheetsClient();
  const res = await sheets.spreadsheets.values.get({
    spreadsheetId: SHEET_ID(),
    range: "Summary!E2",
  });
  const val = res.data.values && res.data.values[0] && res.data.values[0][0];
  return val ? parseFloat(val) : defaultBalance;
}

async function appendTradeOpen(trade) {
  const sheets = getSheetsClient();
  const values = [[
    new Date().toISOString(),
    trade.symbol,
    trade.direction,
    trade.reason,
    trade.entry,
    trade.sl,
    trade.fullTp,
    trade.qty,
    "", "", "", "", // ExitPrice, ExitTime, PnL, PnL%
    "OPEN",
  ]];
  const res = await sheets.spreadsheets.values.append({
    spreadsheetId: SHEET_ID(),
    range: "Trades!A:M",
    valueInputOption: "RAW",
    insertDataOption: "INSERT_ROWS",
    requestBody: { values },
  });
  const range = res.data.updates.updatedRange;
  const match = range.match(/(\d+)(?::|$)/);
  return match ? parseInt(match[1], 10) : null;
}

/** Помечает частичное закрытие в колонке Reason (D), не меняя статус сделки. */
async function appendPartialNote(rowNumber, note) {
  const sheets = getSheetsClient();
  const current = await sheets.spreadsheets.values.get({
    spreadsheetId: SHEET_ID(),
    range: `Trades!D${rowNumber}`,
  });
  const existing = (current.data.values && current.data.values[0] && current.data.values[0][0]) || "";
  await sheets.spreadsheets.values.update({
    spreadsheetId: SHEET_ID(),
    range: `Trades!D${rowNumber}`,
    valueInputOption: "RAW",
    requestBody: { values: [[`${existing} | ${note}`]] },
  });
}

async function updateTradeClose(rowNumber, { exitPrice, pnl, pnlPct, exitReason }) {
  const sheets = getSheetsClient();
  await sheets.spreadsheets.values.update({
    spreadsheetId: SHEET_ID(),
    range: `Trades!I${rowNumber}:M${rowNumber}`,
    valueInputOption: "RAW",
    requestBody: {
      values: [[exitPrice, new Date().toISOString(), pnl.toFixed(2), `${pnlPct.toFixed(2)}%`, `CLOSED (${exitReason})`]],
    },
  });
}

async function updateSummary(pnl) {
  const sheets = getSheetsClient();
  const res = await sheets.spreadsheets.values.get({
    spreadsheetId: SHEET_ID(),
    range: "Summary!A2:E2",
  });
  const row = res.data.values && res.data.values[0] ? res.data.values[0] : ["0", "0", "0", "0", "1000"];
  const totalTrades = parseInt(row[0] || "0", 10) + 1;
  const wins = parseInt(row[1] || "0", 10) + (pnl > 0 ? 1 : 0);
  const losses = parseInt(row[2] || "0", 10) + (pnl <= 0 ? 1 : 0);
  const totalPnl = parseFloat(row[3] || "0") + pnl;
  const balance = parseFloat(row[4] || "1000") + pnl;

  await sheets.spreadsheets.values.update({
    spreadsheetId: SHEET_ID(),
    range: "Summary!A2:E2",
    valueInputOption: "RAW",
    requestBody: { values: [[totalTrades, wins, losses, totalPnl.toFixed(2), balance.toFixed(2)]] },
  });
  return balance;
}

module.exports = {
  getState, setState, clearState, getBalance,
  appendTradeOpen, appendPartialNote, updateTradeClose, updateSummary,
};
