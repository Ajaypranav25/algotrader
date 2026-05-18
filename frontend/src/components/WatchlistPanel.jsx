import React from 'react';

export default function WatchlistPanel({ ticks = {} }) {
  const symbolList = Object.keys(ticks);

  return (
    <div className="bg-slate-900 border border-slate-800 rounded-xl p-4 shadow-xl flex-1">
      <h3 className="text-sm font-bold uppercase tracking-wider text-slate-400 mb-3 flex items-center gap-2">
        📊 Live Monitor Watchlist
      </h3>
      
      <div className="flex flex-col gap-2">
        {symbolList.length === 0 ? (
          <div className="text-xs text-slate-500 italic text-center py-6 border border-dashed border-slate-800 rounded-lg">
            Waiting for live price streaming ticks...
          </div>
        ) : (
          symbolList.map((symbol) => {
            const price = ticks[symbol];
            return (
              <div 
                key={symbol} 
                className="flex justify-between items-center bg-slate-950 px-3 py-2.5 rounded-lg border border-slate-850 hover:border-slate-700 transition-colors"
              >
                <span className="text-xs font-bold tracking-wide text-slate-200">{symbol}</span>
                <span className="font-mono font-semibold text-xs text-cyan-400">
                  ₹{price ? price.toFixed(2) : '0.00'}
                </span>
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}