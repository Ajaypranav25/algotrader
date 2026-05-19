import React from 'react';
import useWebSocket from './hooks/useWebSocket';
import StatusBar from './components/StatusBar';
import WatchlistPanel from './components/WatchlistPanel';
import PositionsTable from './components/PositionsTable';
import TradeLog from './components/TradeLog';
import GeminiAnalysis from './components/GeminiAnalysis';
import KillSwitch from './components/KillSwitch';
import PnLChart from './components/PnLChart';

function App() {
  // Synchronize data flow using your updated event mapping hook
  const { 
    connected, 
    botStatus, 
    ticks, 
    positions, 
    tradeLogs, 
    geminiSignal 
  } = useWebSocket('ws://localhost:8000/api/websocket');

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100 font-sans p-4">
      
      {/* Top Header Section: Live Health Monitoring & Emergency Safety Control */}
      <header className="flex flex-col md:flex-row justify-between items-center bg-slate-900 border border-slate-800 p-4 rounded-xl shadow-lg gap-4 mb-6">
        <div className="flex items-center gap-3">
          {/* Active Pulsing Indicator mapped to WS socket heartbeat */}
          <div className={`h-3 w-3 rounded-full animate-pulse ${
            connected 
              ? 'bg-cyan-400 shadow-[0_0_10px_#22d3ee]' 
              : 'bg-rose-500 shadow-[0_0_10px_#f43f5e]'
          }`}></div>
          <h1 className="text-xl font-bold tracking-wider uppercase text-cyan-400">
            AlgoTrader Dashboard
          </h1>
        </div>
        
        {/* State Injectors for Monitoring and Safety Interventions */}
        <div className="flex flex-wrap items-center gap-4">
          <StatusBar 
            connected={connected} 
            totalPnL={botStatus.daily_pnl} 
            isPaperMode={botStatus.paper_trading} 
            botRunning={botStatus.running}
            smartapiConnected={botStatus.smartapi_connected}
            haltTriggered={botStatus.halt_triggered}
          />
          <KillSwitch backendUrl="http://localhost:8000" />
        </div>
      </header>

      {/* Main Workspace Grid Dashboard Component Layout */}
      <main className="grid grid-cols-1 xl:grid-cols-4 gap-6">
        
        {/* Column 1: Live Market Ticks Feed */}
        <div className="xl:col-span-1 flex flex-col gap-6">
          <WatchlistPanel ticks={ticks} />
        </div>

        {/* Column 2 & 3: Performance Curves & Operational Position Matrices */}
        <div className="xl:col-span-2 flex flex-col gap-6">
          {/* Passing the daily_pnl straight down to plot a trending tracking line */}
          <PnLChart currentPnL={botStatus.daily_pnl} />
          <PositionsTable positions={positions} openPositionsCount={botStatus.open_positions} />
        </div>

        {/* Column 4: AI Engine Context Reasoning & Streaming Terminal Updates */}
        <div className="xl:col-span-1 flex flex-col gap-6">
          <GeminiAnalysis analysis={geminiSignal} />
          <TradeLog logs={tradeLogs} />
        </div>

      </main>
    </div>
  );
}

export default App;