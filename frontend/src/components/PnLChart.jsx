import React, { useState, useEffect } from 'react';

export default function PnLChart({ currentPnL = 0 }) {
  const [history, setHistory] = useState([]);

  // Capture daily_pnl variations to dynamically draw a simple trending sparkline
  useEffect(() => {
    setHistory(prev => {
      const updated = [...prev, currentPnL];
      if (updated.length > 20) updated.shift(); // Constrain window array viewport boundary to 20 samples
      return updated;
    });
  }, [currentPnL]);

  // Map values to a bounded visual coordinates container string
  const pointsCount = history.length;
  const maxVal = Math.max(...history, 100);
  const minVal = Math.min(...history, -100);
  const spread = maxVal - minVal || 1;

  const svgPoints = history.map((val, idx) => {
    const x = (idx / Math.max(pointsCount - 1, 1)) * 300;
    const y = 80 - ((val - minVal) / spread) * 60; // Constrain graph to a clean 80px visual scale height
    return `${x},${y}`;
  }).join(' ');

  const chartColor = currentPnL >= 0 ? '#10b981' : '#f43f5e';

  return (
    <div className="bg-slate-900 border border-slate-800 rounded-xl p-4 shadow-xl flex flex-col h-[180px]">
      <div className="flex justify-between items-center mb-2">
        <h3 className="text-sm font-bold uppercase tracking-wider text-slate-400">
          📈 Live Equity Trend Curve
        </h3>
        <span className={`font-mono text-xs font-black px-2 py-0.5 rounded ${
          currentPnL >= 0 ? 'bg-emerald-500/10 text-emerald-400' : 'bg-rose-500/10 text-rose-400'
        }`}>
          {currentPnL >= 0 ? 'Surplus' : 'Deficit'}
        </span>
      </div>

      <div className="flex-1 bg-slate-950 rounded-lg border border-slate-850 p-2 relative overflow-hidden flex items-center justify-center">
        {history.length < 2 ? (
          <div className="text-[11px] text-slate-600 font-medium">
            Aggregating session metrics to map graph plot trend points...
          </div>
        ) : (
          <svg className="w-full h-full overflow-visible" viewBox="0 0 300 80" preserveAspectRatio="none">
            {/* Midline Zero Reference Pivot Marker */}
            <line 
              x1="0" y1={80 - ((0 - minVal) / spread) * 60} 
              x2="300" y2={80 - ((0 - minVal) / spread) * 60} 
              stroke="#334155" strokeWidth="1" strokeDasharray="3,3" 
            />
            {/* Main Streaming Session Line Graph */}
            <polyline
              fill="none"
              stroke={chartColor}
              strokeWidth="2.5"
              strokeLinecap="round"
              strokeLinejoin="round"
              points={svgPoints}
              className="transition-all duration-300"
            />
          </svg>
        )}
        
        {/* Absolute Scaled Border Edge Indicators */}
        <div className="absolute top-1 right-2 text-[9px] font-mono font-bold text-slate-600">Max: ₹{maxVal.toFixed(0)}</div>
        <div className="absolute bottom-1 right-2 text-[9px] font-mono font-bold text-slate-600">Min: ₹{minVal.toFixed(0)}</div>
      </div>
    </div>
  );
}