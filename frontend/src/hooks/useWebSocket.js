import { useState, useEffect, useRef } from 'react';

export default function useWebSocket(url) {
  const [connected, setConnected] = useState(false);
  const [botStatus, setBotStatus] = useState({
    running: false,
    paper_trading: true,
    smartapi_connected: false,
    daily_pnl: 0,
    halt_triggered: false,
    open_positions: 0,
    market_open: false,
  });
  const [ticks, setTicks] = useState({});
  const [positions, setPositions] = useState([]);
  const [tradeLogs, setTradeLogs] = useState([]);
  const [geminiSignal, setGeminiSignal] = useState(null);

  const ws = useRef(null);

  useEffect(() => {
    function connect() {
      console.log("Connecting to Backend WebSocket...");
      ws.current = new WebSocket(url);

      ws.current.onopen = () => {
        console.log("WebSocket Connection Established.");
        setConnected(true);
      };

      ws.current.onmessage = (event) => {
        try {
          const payload = JSON.parse(event.data);
          const { event: eventType, data } = payload;

          // Route metrics based on backend "event" string mappings
          switch (eventType) {
            case 'connected':
              console.log("Backend Handshake Success:", data);
              break;

            case 'bot_status':
              setBotStatus(data);
              break;

            case 'tick_update':
              // data is an object: { RELIANCE: 2450.5, INFY: 1600.2, ... }
              setTicks(prev => ({ ...prev, ...data }));
              break;

            case 'gemini_signal':
              setGeminiSignal(data); // Expects the parsed payload object from gemini_engine
              setTradeLogs(prev => [
                `🤖 [Gemini Signal] Generated analysis for strategy execution.`, 
                ...prev.slice(0, 49)
              ]);
              break;

            case 'trade_opened':
              // Expects data to be position info. Track log and update array.
              setTradeLogs(prev => [
                `🟢 [TRADE OPENED] Bought ${data.symbol} Qty: ${data.qty} @ ${data.entry_price}`, 
                ...prev.slice(0, 49)
              ]);
              break;

            case 'trade_closed':
              setTradeLogs(prev => [
                `🔴 [TRADE CLOSED] Closed ${data.symbol} Qty: ${data.qty} @ ${data.entry_price} | PnL: ${data.pnl}`, 
                ...prev.slice(0, 49)
              ]);
              break;

            default:
              console.log("Unhandled backend message event:", eventType);
          }
        } catch (err) {
          console.error("Error parsing incoming WebSocket frame:", err);
        }
      };

      ws.current.onclose = () => {
        console.log("WebSocket closed. Reconnecting in 3 seconds...");
        setConnected(false);
        setTimeout(connect, 3000); // Reconnection loop
      };

      ws.current.onerror = (err) => {
        console.error("WebSocket transport error:", err);
        ws.current.close();
      };
    }

    connect();

    return () => {
      if (ws.current) ws.current.close();
    };
  }, [url]);

  return { connected, botStatus, ticks, positions, tradeLogs, geminiSignal };
}