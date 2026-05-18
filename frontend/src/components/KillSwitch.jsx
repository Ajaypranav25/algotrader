import React, { useState } from 'react';

export default function KillSwitch({ backendUrl = 'http://localhost:8000' }) {
  const [loading, setLoading] = useState(false);

  const triggerAction = async (endpoint, alertMsg) => {
    if (endpoint.includes('kill-switch') && !window.confirm("⚠️ ACTIVATE EMERGENCY KILL SWITCH? This will stop the bot and close ALL open positions!")) {
      return;
    }

    setLoading(true);
    try {
      const response = await fetch(`${backendUrl}/api${endpoint}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
      });
      const data = await response.json();
      alert(`${alertMsg}\nServer Response: ${data.message || 'Success'}`);
    } catch (err) {
      console.error(`Control error on ${endpoint}:`, err);
      alert(`Failed to execute system control action: ${err.message}`);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="flex items-center gap-2 bg-slate-900 px-3 py-1.5 rounded-lg border border-slate-800">
      {/* Bot Execution Controls */}
      <button
        disabled={loading}
        onClick={() => triggerAction('/bot/start', '⚡ Starting trading loop...')}
        className="px-3 py-1 bg-emerald-600/20 hover:bg-emerald-600 text-emerald-400 hover:text-white rounded text-xs font-semibold tracking-wider transition-all border border-emerald-500/30 disabled:opacity-50"
      >
        START
      </button>

      <button
        disabled={loading}
        onClick={() => triggerAction('/bot/stop', '⏳ Halting processing pipeline...')}
        className="px-3 py-1 bg-amber-600/20 hover:bg-amber-600 text-amber-400 hover:text-white rounded text-xs font-semibold tracking-wider transition-all border border-amber-500/30 disabled:opacity-50"
      >
        STOP
      </button>

      {/* Emergency Kill Switch */}
      <button
        disabled={loading}
        onClick={() => triggerAction('/bot/kill-switch', '🚨 EMERGENCY SHUTDOWN ACTUATED')}
        className="px-4 py-1.5 bg-rose-600 hover:bg-rose-700 text-white font-bold text-xs tracking-widest rounded shadow-[0_0_15px_rgba(225,29,72,0.3)] hover:shadow-[0_0_20px_rgba(225,29,72,0.6)] transition-all border border-rose-500/50 animate-pulse disabled:opacity-50"
      >
        KILL SWITCH
      </button>
    </div>
  );
}