"use client";

import { useState, useEffect } from "react";

interface Stock {
  symbol: string;
  qty: number;
  current_price: number;
  market_value: number;
  pnl: number;
  pnl_pct: number;
}

export default function StocksTab() {
  const [stocks, setStocks] = useState<Stock[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [orderForm, setOrderForm] = useState({
    symbol: "",
    qty: "",
    order_type: "market",
    side: "buy",
  });

  useEffect(() => {
    fetchStocks();
  }, []);

  const fetchStocks = async () => {
    setLoading(true);
    try {
      const response = await fetch("/api/alpaca/positions");
      if (response.ok) {
        const data = await response.json();
        setStocks(data);
        setError("");
      } else if (response.status === 401) {
        setError("Alpaca API keys not configured");
      } else {
        setError("Failed to fetch stocks");
      }
    } catch (err) {
      setError("Connection error");
    } finally {
      setLoading(false);
    }
  };

  const handlePlaceOrder = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoading(true);
    try {
      const response = await fetch("/api/alpaca/order", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(orderForm),
      });

      if (response.ok) {
        setError("");
        setOrderForm({ symbol: "", qty: "", order_type: "market", side: "buy" });
        fetchStocks();
      } else {
        const data = await response.json();
        setError(data.message || "Order failed");
      }
    } catch (err) {
      setError("Failed to place order");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div>
      {error && <div className="error">{error}</div>}

      <div className="grid">
        <div className="card">
          <h2>📍 Place Order</h2>
          <form onSubmit={handlePlaceOrder}>
            <div className="input-group">
              <label>Symbol</label>
              <input
                type="text"
                placeholder="e.g., AAPL"
                value={orderForm.symbol}
                onChange={(e) =>
                  setOrderForm({ ...orderForm, symbol: e.target.value })
                }
                required
              />
            </div>

            <div className="input-group">
              <label>Quantity</label>
              <input
                type="number"
                placeholder="1"
                value={orderForm.qty}
                onChange={(e) =>
                  setOrderForm({ ...orderForm, qty: e.target.value })
                }
                required
              />
            </div>

            <div className="input-group">
              <label>Order Type</label>
              <select
                value={orderForm.order_type}
                onChange={(e) =>
                  setOrderForm({ ...orderForm, order_type: e.target.value })
                }
              >
                <option value="market">Market</option>
                <option value="limit">Limit</option>
              </select>
            </div>

            <div className="input-group">
              <label>Side</label>
              <select
                value={orderForm.side}
                onChange={(e) =>
                  setOrderForm({ ...orderForm, side: e.target.value })
                }
              >
                <option value="buy">Buy</option>
                <option value="sell">Sell</option>
              </select>
            </div>

            <button type="submit" className="btn" disabled={loading}>
              {loading ? "Processing..." : "Place Order"}
            </button>
          </form>
        </div>
      </div>

      <div className="card">
        <h2>📊 Your Positions</h2>
        {loading && !stocks.length ? (
          <div className="loading">Loading positions...</div>
        ) : stocks.length === 0 ? (
          <p>No positions yet. Place an order to get started!</p>
        ) : (
          <div style={{ overflowX: "auto" }}>
            <table style={{ width: "100%", borderCollapse: "collapse" }}>
              <thead>
                <tr>
                  <th style={{ textAlign: "left", padding: "10px", borderBottom: "1px solid rgba(0,255,136,0.2)" }}>Symbol</th>
                  <th style={{ textAlign: "right", padding: "10px", borderBottom: "1px solid rgba(0,255,136,0.2)" }}>Qty</th>
                  <th style={{ textAlign: "right", padding: "10px", borderBottom: "1px solid rgba(0,255,136,0.2)" }}>Price</th>
                  <th style={{ textAlign: "right", padding: "10px", borderBottom: "1px solid rgba(0,255,136,0.2)" }}>Value</th>
                  <th style={{ textAlign: "right", padding: "10px", borderBottom: "1px solid rgba(0,255,136,0.2)" }}>P&L</th>
                </tr>
              </thead>
              <tbody>
                {stocks.map((stock) => (
                  <tr key={stock.symbol}>
                    <td style={{ padding: "10px", borderBottom: "1px solid rgba(0,255,136,0.1)" }}>{stock.symbol}</td>
                    <td style={{ textAlign: "right", padding: "10px", borderBottom: "1px solid rgba(0,255,136,0.1)" }}>{stock.qty}</td>
                    <td style={{ textAlign: "right", padding: "10px", borderBottom: "1px solid rgba(0,255,136,0.1)" }}>${stock.current_price.toFixed(2)}</td>
                    <td style={{ textAlign: "right", padding: "10px", borderBottom: "1px solid rgba(0,255,136,0.1)" }}>${stock.market_value.toFixed(2)}</td>
                    <td style={{ textAlign: "right", padding: "10px", borderBottom: "1px solid rgba(0,255,136,0.1)", color: stock.pnl >= 0 ? "#51cf66" : "#ff6b6b" }}>
                      {stock.pnl >= 0 ? "+" : ""}${stock.pnl.toFixed(2)} ({stock.pnl_pct >= 0 ? "+" : ""}{stock.pnl_pct.toFixed(2)}%)
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
