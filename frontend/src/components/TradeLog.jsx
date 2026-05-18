import React, { useEffect, useRef } from 'react';

export default function TradeLog({ logs = [] }) {
  const terminalEndRef = useRef(null);

  // Auto-scroll logic to pin the terminal to the latest execution update
  useEffect(() => {
    if (terminalEndRef.current) {
      terminalEndRef.current.scrollIntoView({ behavior: 'smooth' });
    }
  }, [logs]);

  // Colorize log records dynamically to isolate trading events at a glance
  const getLogStyle = (logText) => {
    if (logText.includes('[TRADE OPENED]')) return 'text-emerald-400 font-semibold';
    if (logText.includes('[TRADE CLOSED]')) return 'text-rose-400 font-semibold';
    if (logText.includes('[Gemini Signal]')) return 'text-cyan-400';
    if (logText.includes('🚨') || logText.includes('⚠️')) return 'text-amber-400 font-medium animate-pulse';
    return 'text-slate-400';
  };

  return (
    <div className="bg-slate-900 border border-slate-800 rounded-xl p-4 shadow-xl flex flex-col h-[280px]">
      <h3 className="text-sm font-bold uppercase tracking-wider text-slate-400 mb-3 flex items-center gap-2">
        📟 Core Execution Terminal
      </h3>
      
      {/* Scrollable Terminal Interface Box */}
      <div className="flex-1 overflow-y-auto bg-slate-950 p-3 rounded-lg border border-slate-850 font-mono text-[11px] leading-relaxed flex flex-col gap-1.5 shadow-inner custom-scrollbar">
        {logs.length === 0 ? (
          <div className="text-slate-600 italic text-center my-auto">
            [SYSTEM] Awaiting incoming order routing stream matrix ticks...
          </div>
        ) : (
          // Render logs upside down so oldest records sit at the top, matching standard server shells
          [...logs].reverse().map((log, index) => (
            <div key={index} className={`whitespace-pre-wrap border-l-2 border-transparent pl-2 hover:border-slate-800 transition-colors ${getLogStyle(log)}`}>
              <span className="text-slate-600 select-none mr-2">
                [{new Date().toLocaleTimeString()}]
              </span>
              {log}
            </div>
          ))
        )}
        <div ref={terminalEndRef} />
      </div>
    </div>
  );
}