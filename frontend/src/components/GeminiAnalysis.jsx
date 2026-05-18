import React from 'react';

export default function GeminiAnalysis({ analysis = null }) {
  if (!analysis) {
    return (
      <div className="bg-slate-900 border border-slate-800 rounded-xl p-4 shadow-xl flex flex-col h-[260px] justify-center items-center text-center">
        <div className="text-slate-500 animate-pulse text-xs">
          🧠 Waiting for Gemini Engine market signals...
        </div>
      </div>
    );
  }

  // Determine signal colors based on structural types
  const isBuy = analysis.signal === 'BUY';
  const isSell = analysis.signal === 'SELL';
  
  const badgeStyle = isBuy 
    ? 'bg-emerald-500/10 text-emerald-400 border border-emerald-500/30' 
    : isSell 
      ? 'bg-rose-500/10 text-rose-400 border border-rose-500/30' 
      : 'bg-slate-800 text-slate-400 border border-slate-700';

  const confidencePct = (analysis.confidence || 0) * 100;

  return (
    <div className="bg-slate-900 border border-slate-800 rounded-xl p-4 shadow-xl flex flex-col h-[260px]">
      <h3 className="text-sm font-bold uppercase tracking-wider text-slate-400 mb-2.5 flex items-center gap-2">
        🧠 Gemini Signal Insight
      </h3>

      <div className="flex-1 flex flex-col overflow-y-auto pr-1 gap-3 custom-scrollbar">
        {/* Core Direction & Target Matrix Row */}
        <div className="grid grid-cols-3 gap-2 text-center">
          <div className={`p-2 rounded-lg ${badgeStyle} flex flex-col justify-center`}>
            <span className="text-[10px] text-slate-400 uppercase tracking-tight block">Signal</span>
            <span className="text-base font-black tracking-wide">{analysis.signal}</span>
          </div>
          <div className="p-2 rounded-lg bg-slate-950 border border-slate-850 flex flex-col justify-center">
            <span className="text-[10px] text-slate-500 uppercase tracking-tight block">Target</span>
            <span className="text-xs font-mono font-bold text-cyan-400">
              {analysis.target_price ? `₹${analysis.target_price.toFixed(2)}` : '—'}
            </span>
          </div>
          <div className="p-2 rounded-lg bg-slate-950 border border-slate-850 flex flex-col justify-center">
            <span className="text-[10px] text-slate-500 uppercase tracking-tight block">Stop Loss</span>
            <span className="text-xs font-mono font-bold text-rose-400">
              {analysis.stop_loss ? `₹${analysis.stop_loss.toFixed(2)}` : '—'}
            </span>
          </div>
        </div>

        {/* Confidence Progress Meter */}
        <div className="bg-slate-950 px-3 py-2 rounded-lg border border-slate-850">
          <div className="flex justify-between items-center text-[10px] mb-1">
            <span className="text-slate-500 uppercase tracking-tight">Signal Confidence</span>
            <span className="font-mono text-cyan-400 font-bold">{confidencePct.toFixed(0)}%</span>
          </div>
          <div className="w-full bg-slate-800 h-1.5 rounded-full overflow-hidden">
            <div 
              className="h-full bg-cyan-400 shadow-[0_0_8px_#22d3ee] transition-all duration-500" 
              style={{ width: `${confidencePct}%` }}
            />
          </div>
        </div>

        {/* Textual Logical Reasoning Container */}
        <div className="flex-1 bg-slate-950 p-2.5 rounded-lg border border-slate-850 font-sans text-[11px] leading-relaxed text-slate-300">
          <span className="text-slate-500 font-bold block mb-0.5 uppercase text-[9px] tracking-wider">Rationale:</span>
          <p className="italic text-slate-400">{analysis.rationale}</p>
        </div>
      </div>
    </div>
  );
}