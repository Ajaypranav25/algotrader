import React from 'react';

export default function StatusBar({ 
  connected, 
  totalPnL, 
  isPaperMode, 
  botRunning, 
  smartapiConnected, 
  haltTriggered 
}) {
  
  // Format PnL dynamically with color styling (+ or -)
  const pnlColor = totalPnL >= 0 ? 'text-emerald-400' : 'text-rose-500';
  const formattedPnL = totalPnL >= 0 ? `+₹${totalPnL}` : `-₹${Math.abs(totalPnL)}`;

  return (
    <div className="flex flex-wrap items-center gap-3 bg-slate-950/60 backdrop-blur-md px-4 py-2 rounded-lg border border-slate-800 text-xs font-medium text-slate-300">
      
      {/* 1. Dashboard WebSocket Linkage State */}
      <div className="flex items-center gap-2 pr-2 border-r border-slate-800">
        <span className="text-slate-500">WS Link:</span>
        <span className={`px-2 py-0.5 rounded uppercase tracking-wide text-[10px] font-bold ${
          connected ? 'bg-emerald-500/10 text-emerald-400 border border-emerald-500/20' : 'bg-rose-500/10 text-rose-400 border border-rose-500/20'
        }`}>
          {connected ? 'Connected' : 'Offline'}
        </span>
      </div>

      {/* 2. Angel One SmartAPI Linkage State */}
      <div className="flex items-center gap-2 pr-2 border-r border-slate-800">
        <span className="text-slate-500">Broker API:</span>
        <span className={`px-2 py-0.5 rounded uppercase tracking-wide text-[10px] font-bold ${
          smartapiConnected ? 'bg-emerald-500/10 text-emerald-400 border border-emerald-500/20' : 'bg-rose-500/10 text-rose-400 border border-rose-500/20'
        }`}>
          {smartapiConnected ? 'Ready' : 'Disconnected'}
        </span>
      </div>

      {/* 3. Main Logic Orchestration Execution State */}
      <div className="flex items-center gap-2 pr-2 border-r border-slate-800">
        <span className="text-slate-500">Bot Logic:</span>
        <span className={`px-2 py-0.5 rounded uppercase tracking-wide text-[10px] font-bold ${
          haltTriggered 
            ? 'bg-rose-500/20 text-rose-400 border border-rose-500/40 animate-pulse' 
            : botRunning 
              ? 'bg-cyan-500/10 text-cyan-400 border border-cyan-500/20' 
              : 'bg-amber-500/10 text-amber-400 border border-amber-500/20'
        }`}>
          {haltTriggered ? 'HALTED 🛑' : botRunning ? 'Active ⚡' : 'Idle ⏳'}
        </span>
      </div>

      {/* 4. Portfolio Operational Risk Environment Mode */}
      <div className="flex items-center gap-2 pr-2 border-r border-slate-800">
        <span className="text-slate-500">Trading Mode:</span>
        <span className={`px-2 py-0.5 rounded uppercase tracking-wide text-[10px] font-bold ${
          isPaperMode 
            ? 'bg-amber-500/10 text-amber-400 border border-amber-500/20' 
            : 'bg-rose-500/10 text-rose-400 border border-rose-500/20 shadow-[0_0_8px_rgba(244,63,94,0.1)]'
        }`}>
          {isPaperMode ? 'Paper Trading 🟡' : 'LIVE MARKET 🚨'}
        </span>
      </div>

      {/* 5. Running Realized Metrics Aggregation Display */}
      <div className="flex items-center gap-2 pl-1">
        <span className="text-slate-500">Realized PnL:</span>
        <span className={`font-mono font-bold text-sm ${pnlColor}`}>
          {formattedPnL}
        </span>
      </div>

    </div>
  );
}