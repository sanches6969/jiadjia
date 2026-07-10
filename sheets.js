async function getState() { return null; }
async function saveState(state) { return true; }
async function addTrade(trade) { return true; }
async function updateSummary(pnl) { return true; }

module.exports = { getState, saveState, addTrade, updateSummary };
