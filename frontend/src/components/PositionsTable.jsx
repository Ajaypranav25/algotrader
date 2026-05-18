import React from 'react';

export default function PositionsTable({ positions = [], openPositionsCount = 0 }) {
  return (
    <div className="bg-slate-900 border border-slate-800 rounded-xl p-4 shadow-xl flex-1">
      <div className="flex justify-between items-center mb-4">
        <h3 className="text-sm font-bold uppercase tracking-wider text-slate-400 flex items-center gap-2">
          💼 Active Portfolio Holdings
        </h3>
        <span className="bg-slate-950 text-slate-400 border border-slate-800 text-[11px] font-mono font-bold px-2.5 py-0.5 rounded-full">
          Active: {openPositionsCount || positions.length}
        </span>
      </div>

      <div className="overflow-x-auto">
        <table className="w-full text-left border-collapse text-xs">
          <thead>
            <tr className="border-b border-slate-800 text-slate-500 uppercase font-semibold text-[10px] tracking-wider">
              <th className="pb-2">Asset</th>
              <th className="pb-2">Direction</th>
              <th className="pb-2">Volume</th>
              <th className="pb-2">Entry Base</th>
              <th className="pb-2">Current Spot</th>
              <th className="pb-2 text-right">Running PnL</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-850 font-medium">
            {positions.length === 0 ? (
              <tr>
                <td colSpan="6" className="text-center py-8 text-slate-500 italic">
                  No active exposure items opened in current workspace session.
                </td>
              </tr>
            ) : (
              positions.map((pos, index) => {
                const pnl = pos.unrealized_pnl || 0;
                const isLong = pos.direction?.toLowerCase() !== 'short';
                
                return (
                  <tr key={index} className="hover:bg-slate-950/40 transition-colors">
                    <td className="py-3 font-bold text-slate-200">{pos.symbol}</td>
                    <td className="py-3">
                      <span className={`px-2 py-0.5 text-[10px] font-bold uppercase rounded ${
                        isLong ? 'bg-emerald-500/10 text-emerald-400' : 'bg-rose-500/10 text-rose-400'
                      }`}>
                        {isLong ? 'LONG' : 'SHORT'}
                      </span>
                    </td>
                    <td className="py-3 font-mono">{pos.quantity}</td>
                    <td className="py-3 font-mono">₹{pos.entry_price?.toFixed(2)}</td>
                    <td className="py-3 font-mono text-cyan-400">₹{pos.current_price?.toFixed(2) || '0.00'}</td>
                    <td className={`py-3 font-mono text-right font-bold ${pnl >= 0 ? 'text-emerald-400' : 'text-rose-500'}`}>
                      {pnl >= 0 ? `+₹${pnl.toFixed(2)}` : `-₹${Math.abs(pnl).toFixed(2)}`}
                    </td>
                  </tr>
                );
              })
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}