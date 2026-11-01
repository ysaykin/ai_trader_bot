import React, { useState, useEffect } from 'react';

const API_BASE = '';

const App = () => {
  const [positions, setPositions] = useState([]);
  const [accountBalance, setAccountBalance] = useState(100000.0);
  const [accountEquity, setAccountEquity] = useState(100000.0);
  const [config, setConfig] = useState({});
  const [activityLog, setActivityLog] = useState([]);

  useEffect(() => {
    fetchStatus();
    fetchConfig();
    const interval = setInterval(fetchStatus, 5000);
    return () => clearInterval(interval);
  }, []);

  const fetchStatus = async () => {
    try {
      const res = await fetch(API_BASE + '/status');
      const data = await res.json();
      setAccountBalance(data.account.balance);
      setAccountEquity(data.account.equity);
      setPositions(data.positions);
    } catch (e) {
      console.error('Failed to fetch status:', e);
    }
  };

  const fetchConfig = async () => {
    try {
      const res = await fetch(API_BASE + '/config');
      const data = await res.json();
      setConfig(data);
    } catch (e) {
      console.error('Failed to fetch config:', e);
    }
  };

  const placeTrade = async () => {
    const formData = new FormData(document.getElementById('tradeForm'));
    const payload = {
      symbol: formData.get('symbol'),
      dte: parseInt(formData.get('dte')),
      short_delta: parseFloat(formData.get('shortDelta')),
      wing_width: parseInt(formData.get('wingWidth')),
      quantity: parseInt(formData.get('quantity')),
      profit_target: parseFloat(formData.get('profitTarget')),
      stop_loss_mult: parseFloat(formData.get('stopLossMult')),
      entry_time: formData.get('entryTime'),
      max_allocation: parseFloat(formData.get('maxAllocation')),
    };

    const btn = document.getElementById('runTradeBtn');
    const originalText = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = '<svg class="animate-spin w-5 h-5" fill="none" viewBox="0 0 24 24"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path></svg>';

    try {
      const res = await fetch(API_BASE + '/trade', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      if (data.success) {
        setPositions(prev => [...prev, data.position]);
        setAccountEquity(data.account_equity);
        logActivity('Trade placed: ' + data.position.symbol + ' Iron Condor @ $' + data.position.credit.toFixed(2));
      } else {
        alert('Error: ' + (data.detail || 'Unknown error'));
      }
    } catch (e) {
      alert('Network error: ' + e.message);
    } finally {
      btn.disabled = false;
      btn.innerHTML = originalText;
    }
  };

  const closeAll = async () => {
    const btn = document.getElementById('closeBtn');
    const originalText = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = '<svg class="animate-spin w-5 h-5" fill="none" viewBox="0 0 24 24"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path></svg>';

    try {
      const res = await fetch(API_BASE + '/close', { method: 'POST' });
      const data = await res.json();
      if (data.success) {
        setPositions([]);
        setAccountEquity(data.account_equity);
        logActivity('Closed ' + data.closed_positions.length + ' position(s). Total P&L: $' + data.total_pnl.toFixed(2));
      } else {
        alert('Error: ' + (data.detail || 'Unknown error'));
      }
    } catch (e) {
      alert('Network error: ' + e.message);
    } finally {
      btn.disabled = false;
      btn.innerHTML = originalText;
    }
  };

  const logActivity = (message) => {
    const time = new Date().toLocaleTimeString();
    setActivityLog(prev => [{ time, message }, ...prev].slice(0, 10));
  };

  const formatCurrency = (val) => '$' + val.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2});

  return (
    <div className="min-h-screen bg-gray-900 text-gray-100">
      <div className="container mx-auto px-4 py-6 max-w-7xl">
        <header className="mb-6">
          <h1 className="text-3xl font-bold text-emerald-500 flex items-center gap-2">
            <svg className="w-8 h-8" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M13 7h8m0 0v8m0-8l-8 8-4-4-6 6"></path>
            </svg>
            Lumibot Trading Dashboard
          </h1>
          <p className="text-gray-400 mt-1">SPX Iron Condor Strategy</p>
        </header>

        <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
          <div className="lg:col-span-2 space-y-6">
            <div className="bg-gray-800 rounded-lg border border-gray-700 p-6">
              <h2 className="text-xl font-semibold mb-4 flex items-center gap-2">
                <svg className="w-5 h-5 text-blue-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z"></path>
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z"></path>
                </svg>
                Strategy Configuration
              </h2>
              <form onSubmit={(event) => { event.preventDefault(); placeTrade(); }} id="tradeForm" className="grid grid-cols-2 md:grid-cols-4 gap-4">
                <div>
                  <label className="block text-sm text-gray-400 mb-1">Symbol</label>
                  <select id="symbol" name="symbol" className="w-full bg-gray-900 border border-gray-600 rounded px-3 py-2 text-white focus:border-blue-500 focus:outline-none">
                    <option value="SPX">SPX</option>
                    <option value="SPXW">SPXW</option>
                    <option value="XSP">XSP</option>
                  </select>
                </div>
                <div>
                  <label className="block text-sm text-gray-400 mb-1">DTE (Days)</label>
                  <input type="number" id="dte" name="dte" defaultValue="0" min="0" className="w-full bg-gray-900 border border-gray-600 rounded px-3 py-2 text-white focus:border-blue-500 focus:outline-none" />
                </div>
                <div>
                  <label className="block text-sm text-gray-400 mb-1">Short Delta</label>
                  <input type="number" id="shortDelta" name="shortDelta" defaultValue="0.15" step="0.01" min="0" max="1" className="w-full bg-gray-900 border border-gray-600 rounded px-3 py-2 text-white focus:border-blue-500 focus:outline-none" />
                </div>
                <div>
                  <label className="block text-sm text-gray-400 mb-1">Wing Width</label>
                  <input type="number" id="wingWidth" name="wingWidth" defaultValue="15" min="1" className="w-full bg-gray-900 border border-gray-600 rounded px-3 py-2 text-white focus:border-blue-500 focus:outline-none" />
                </div>
                <div>
                  <label className="block text-sm text-gray-400 mb-1">Quantity</label>
                  <input type="number" id="quantity" name="quantity" defaultValue="1" min="1" className="w-full bg-gray-900 border border-gray-600 rounded px-3 py-2 text-white focus:border-blue-500 focus:outline-none" />
                </div>
                <div>
                  <label className="block text-sm text-gray-400 mb-1">Profit Target</label>
                  <input type="number" id="profitTarget" name="profitTarget" defaultValue="0.30" step="0.01" min="0" max="1" className="w-full bg-gray-900 border border-gray-600 rounded px-3 py-2 text-white focus:border-blue-500 focus:outline-none" />
                </div>
                <div>
                  <label className="block text-sm text-gray-400 mb-1">Stop Loss Mult</label>
                  <input type="number" id="stopLossMult" name="stopLossMult" defaultValue="2.0" step="0.1" min="0.5" className="w-full bg-gray-900 border border-gray-600 rounded px-3 py-2 text-white focus:border-blue-500 focus:outline-none" />
                </div>
                <div>
                  <label className="block text-sm text-gray-400 mb-1">Entry Time</label>
                  <input type="time" id="entryTime" name="entryTime" defaultValue="10:00" className="w-full bg-gray-900 border border-gray-600 rounded px-3 py-2 text-white focus:border-blue-500 focus:outline-none" />
                </div>
                <div>
                  <label className="block text-sm text-gray-400 mb-1">Max Allocation</label>
                  <input type="number" id="maxAllocation" name="maxAllocation" defaultValue="0.03" step="0.01" min="0" max="1" className="w-full bg-gray-900 border border-gray-600 rounded px-3 py-2 text-white focus:border-blue-500 focus:outline-none" />
                </div>
              </form>
              <div className="mt-4 flex gap-3">
                <button type="submit" form="tradeForm" id="runTradeBtn" className="bg-emerald-600 hover:bg-emerald-700 text-white font-semibold py-2 px-6 rounded-lg transition flex items-center gap-2">
                  <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M13 10V3L4 14h7v7l9-11h-7z"></path>
                  </svg>
                  Run Trade
                </button>
                <button type="button" id="closeBtn" onClick={closeAll} className="bg-red-600 hover:bg-red-700 text-white font-semibold py-2 px-6 rounded-lg transition flex items-center gap-2">
                  <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M6 18L18 6M6 6l12 12"></path>
                  </svg>
                  Close All
                </button>
              </div>
            </div>

            <div className="bg-gray-800 rounded-lg border border-gray-700 p-6">
              <h2 className="text-xl font-semibold mb-4 flex items-center gap-2">
                <svg className="w-5 h-5 text-emerald-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z"></path>
                </svg>
                Open Positions
              </h2>
              <div className="overflow-x-auto">
                <table className="w-full text-left">
                  <thead className="bg-gray-700">
                    <tr>
                      <th className="px-4 py-3 text-sm font-semibold text-gray-300">ID</th>
                      <th className="px-4 py-3 text-sm font-semibold text-gray-300">Symbol</th>
                      <th className="px-4 py-3 text-sm font-semibold text-gray-300">Short Strike</th>
                      <th className="px-4 py-3 text-sm font-semibold text-gray-300">Long Strike</th>
                      <th className="px-4 py-3 text-sm font-semibold text-gray-300">Credit</th>
                      <th className="px-4 py-3 text-sm font-semibold text-gray-300">P&L</th>
                      <th className="px-4 py-3 text-sm font-semibold text-gray-300">P&L %</th>
                      <th className="px-4 py-3 text-sm font-semibold text-gray-300">Entry</th>
                    </tr>
                  </thead>
                  <tbody>
                    {positions.length === 0 ? (
                      <tr className="text-gray-500 text-center">
                        <td colSpan="8">No open positions</td>
                      </tr>
                    ) : (
                      positions.map((p) => (
                        <tr key={p.id} className="border-b border-gray-700 hover:bg-gray-700 transition">
                          <td className="px-4 py-3 font-mono text-xs text-gray-400">{p.id}</td>
                          <td className="px-4 py-3 font-semibold">{p.symbol}</td>
                          <td className="px-4 py-3 font-mono">{p.short_strike.toFixed(0)}</td>
                          <td className="px-4 py-3 font-mono">{p.long_strike.toFixed(0)}</td>
                          <td className="px-4 py-3 font-mono text-emerald-500">+${p.credit.toFixed(2)}</td>
                          <td className={`px-4 py-3 font-mono ${p.pnl >= 0 ? 'text-emerald-500' : 'text-red-500'}`}>
                            {p.pnl >= 0 ? '+' : ''}${p.pnl.toFixed(2)}
                          </td>
                          <td className={`px-4 py-3 font-mono ${p.pnl_percent >= 0 ? 'text-emerald-500' : 'text-red-500'}`}>
                            {p.pnl_percent >= 0 ? '+' : ''}{p.pnl_percent.toFixed(2)}%
                          </td>
                          <td className="px-4 py-3 text-gray-400">{p.entry_time}</td>
                        </tr>
                      ))
                    )}
                  </tbody>
                </table>
              </div>
            </div>
          </div>

          <div className="space-y-6">
            <div className="bg-gray-800 rounded-lg border border-gray-700 p-6">
              <h2 className="text-xl font-semibold mb-4 flex items-center gap-2">
                <svg className="w-5 h-5 text-blue-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M12 8c-1.657 0-3 .895-3 2s1.343 2 3 2 3 .895 3 2-1.343 2-3 2m0-8c1.11 0 2.08.402 2.599 1M12 8V7m0 1v8m0 0v1m0-1c-1.11 0-2.08-.402-2.599-1M21 12a9 9 0 11-18 0 9 9 0 0118 0z"></path>
                </svg>
                Account Status
              </h2>
              <div className="space-y-3">
                <div className="flex justify-between items-center">
                  <span className="text-gray-400">Status</span>
                  <span id="accountStatusText" className="font-semibold text-emerald-500">Connecting...</span>
                </div>
                <div className="flex justify-between items-center">
                  <span className="text-gray-400">Balance</span>
                  <span id="accountBalance" className="font-mono">{formatCurrency(accountBalance)}</span>
                </div>
                <div className="flex justify-between items-center">
                  <span className="text-gray-400">Equity</span>
                  <span id="accountEquity" className="font-mono">{formatCurrency(accountEquity)}</span>
                </div>
                <div className="flex justify-between items-center">
                  <span className="text-gray-400">Cash</span>
                  <span id="accountCash" className="font-mono">{formatCurrency(accountBalance - accountEquity)}</span>
                </div>
              </div>
            </div>

            <div className="bg-gray-800 rounded-lg border border-gray-700 p-6">
              <h2 className="text-xl font-semibold mb-4 flex items-center gap-2">
                <svg className="w-5 h-5 text-blue-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z"></path>
                </svg>
                Recent Activity
              </h2>
              <div id="activityLog" className="space-y-2 max-h-64 overflow-y-auto">
                <div className="text-gray-500 text-sm text-center py-4">No recent activity</div>
              </div>
            </div>

            <div className="bg-gray-800 rounded-lg border border-gray-700 p-6">
              <h2 className="text-xl font-semibold mb-4 flex items-center gap-2">
                <svg className="w-5 h-5 text-blue-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z"></path>
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z"></path>
                </svg>
                Strategy Config
              </h2>
              <div className="space-y-2 text-sm">
                <div className="flex justify-between"><span className="text-gray-400">Symbol:</span> <span id="configSymbol" className="font-mono">SPX</span></div>
                <div className="flex justify-between"><span className="text-gray-400">DTE:</span> <span id="configDte" className="font-mono">0</span></div>
                <div className="flex justify-between"><span className="text-gray-400">Short Delta:</span> <span id="configDelta" className="font-mono">0.15</span></div>
                <div className="flex justify-between"><span className="text-gray-400">Wing Width:</span> <span id="configWing" className="font-mono">15</span></div>
                <div className="flex justify-between"><span className="text-gray-400">Quantity:</span> <span id="configQty" className="font-mono">1</span></div>
                <div className="flex justify-between"><span className="text-gray-400">Profit Target:</span> <span id="configTarget" className="font-mono">0.30</span></div>
                <div className="flex justify-between"><span className="text-gray-400">Stop Loss:</span> <span id="configStop" className="font-mono">2.0</span></div>
                <div className="flex justify-between"><span className="text-gray-400">Entry Time:</span> <span id="configEntry" className="font-mono">10:00</span></div>
                <div className="flex justify-between"><span className="text-gray-400">Max Alloc:</span> <span id="configAlloc" className="font-mono">0.03</span></div>
              </div>
            </div>
          </div>
        </div>

        <footer className="mt-6 text-center text-gray-500 text-sm">
          <p>Lumibot Trading Dashboard - v1.0.0 - Simulated Trading Environment</p>
        </footer>
      </div>
    </div>
  );
};

export default App;
