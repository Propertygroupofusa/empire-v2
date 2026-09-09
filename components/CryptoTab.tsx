"use client";

import { useState, useEffect } from "react";

interface CryptoAsset {
  currency: string;
  amount: number;
  current_price: number;
  value: number;
  pnl: number;
  pnl_pct: number;
}

export default function CryptoTab() {
  const [crypto, setCrypto] = useState<CryptoAsset[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [success, setSuccess] = useState("");
  const [orderForm, setOrderForm] = useState({
    product_id: "BTC-USD",
    size: "",
    order_type: "market",
    side: "buy",
  });

  useEffect(() => {
    fetchCrypto();
  }, []);

  const fetchCrypto = async () => {
    setLoading(true);
    try {
      const response = await fetch("/api/coinbase/accounts");
      if (response.ok) {
        const data = await response.json();
        setCrypto(data);
        setError("");
      } else if (response.status === 401) {
        setError("Coinbase API keys not configured");
      } else {
        setError("Failed to fetch crypto assets");
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
      const response = await fetch("/api/coinbase/order", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(orderForm),
      });

      if (response.ok) {
        setError("");
        setSuccess("Order placed successfully!");
        setOrderForm({
          product_id: "BTC-USD",
          size: "",
          order_type: "market",
          side: "buy",
        });
        fetchCrypto();
      } else {
        const data = await response.json();
        setError(data.message || "Order failed");
        setSuccess("");
      }
    } catch (err) {
      setError("Failed to place order");
      setSuccess("");
    } finally {
      setLoading(false);
    }
  };

  const handleWithdrawAll = async () => {
    const btcAsset = crypto.find((asset) => asset.currency === "BTC");
    if (!btcAsset || btcAsset.amount <= 0) {
      setError("No BTC to withdraw");
      return;
    }

    if (
      !window.confirm(
        `Withdraw ALL ${btcAsset.amount.toFixed(8)} BTC (~$${btcAsset.value.toFixed(2)}) at market price? This action cannot be undone.`
      )
    ) {
      return;
    }

    setLoading(true);
    setError("");
    try {
      const response = await fetch("/api/coinbase/withdraw", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ product_id: "BTC-USD" }),
      });

      if (response.ok) {
        const data = await response.json();
        setSuccess(
          `✓ Sold ${data.btc_amount.toFixed(8)} BTC at $${data.btc_price.toFixed(2)}. Proceeds: $${data.estimated_proceeds.toFixed(2)}`
        );
        setError("");
        setTimeout(() => fetchCrypto(), 1000);
      } else {
        const data = await response.json();
        setError(data.message || "Withdrawal failed");
        setSuccess("");
      }
    } catch (err) {
      setError("Failed to complete withdrawal");
      setSuccess("");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div>
      {error && <div className="error">{error}</div>}
      {success && (
        <div
          className="success"
          style={{
            backgroundColor: "rgba(81, 207, 102, 0.1)",
            border: "1px solid rgba(81, 207, 102, 0.5)",
            color: "#51cf66",
            padding: "12px",
            borderRadius: "4px",
            marginBottom: "20px",
          }}
        >
          {success}
        </div>
      )}

      <div className="grid">
        <div className="card">
          <h2>₿ Place Order</h2>
          <form onSubmit={handlePlaceOrder}>
            <div className="input-group">
              <label>Product ID</label>
              <select
                value={orderForm.product_id}
                onChange={(e) =>
                  setOrderForm({ ...orderForm, product_id: e.target.value })
                }
              >
                <option value="BTC-USD">BTC-USD (Bitcoin)</option>
                <option value="ETH-USD">ETH-USD (Ethereum)</option>
                <option value="SOL-USD">SOL-USD (Solana)</option>
                <option value="XRP-USD">XRP-USD (Ripple)</option>
              </select>
            </div>

            <div className="input-group">
              <label>Amount</label>
              <input
                type="number"
                placeholder="0.1"
                step="0.00000001"
                value={orderForm.size}
                onChange={(e) =>
                  setOrderForm({ ...orderForm, size: e.target.value })
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

        <div className="card">
          <h2>🚀 Emergency Withdrawal</h2>
          <p style={{ marginBottom: "15px", color: "#aaa", fontSize: "0.9em" }}>
            Sell all BTC immediately at market price
          </p>
          <button
            onClick={handleWithdrawAll}
            disabled={loading || !crypto.some((a) => a.currency === "BTC" && a.amount > 0)}
            style={{
              width: "100%",
              padding: "12px",
              backgroundColor: "#c44536",
              color: "white",
              border: "none",
              borderRadius: "4px",
              fontSize: "1em",
              fontWeight: "bold",
              cursor: "pointer",
              opacity: loading || !crypto.some((a) => a.currency === "BTC" && a.amount > 0) ? 0.5 : 1,
            }}
          >
            {loading ? "Processing..." : "💰 Withdraw All BTC Now"}
          </button>
        </div>
      </div>

      <div className="card">
        <h2>💰 Your Assets</h2>
        {loading && !crypto.length ? (
          <div className="loading">Loading assets...</div>
        ) : crypto.length === 0 ? (
          <p>No crypto assets yet. Place an order to get started!</p>
        ) : (
          <div style={{ overflowX: "auto" }}>
            <table style={{ width: "100%", borderCollapse: "collapse" }}>
              <thead>
                <tr>
                  <th style={{ textAlign: "left", padding: "10px", borderBottom: "1px solid rgba(0,255,136,0.2)" }}>Asset</th>
                  <th style={{ textAlign: "right", padding: "10px", borderBottom: "1px solid rgba(0,255,136,0.2)" }}>Amount</th>
                  <th style={{ textAlign: "right", padding: "10px", borderBottom: "1px solid rgba(0,255,136,0.2)" }}>Price</th>
                  <th style={{ textAlign: "right", padding: "10px", borderBottom: "1px solid rgba(0,255,136,0.2)" }}>Value</th>
                  <th style={{ textAlign: "right", padding: "10px", borderBottom: "1px solid rgba(0,255,136,0.2)" }}>P&L</th>
                </tr>
              </thead>
              <tbody>
                {crypto.map((asset) => (
                  <tr key={asset.currency}>
                    <td style={{ padding: "10px", borderBottom: "1px solid rgba(0,255,136,0.1)" }}>{asset.currency}</td>
                    <td style={{ textAlign: "right", padding: "10px", borderBottom: "1px solid rgba(0,255,136,0.1)" }}>{asset.amount.toFixed(8)}</td>
                    <td style={{ textAlign: "right", padding: "10px", borderBottom: "1px solid rgba(0,255,136,0.1)" }}>${asset.current_price.toFixed(2)}</td>
                    <td style={{ textAlign: "right", padding: "10px", borderBottom: "1px solid rgba(0,255,136,0.1)" }}>${asset.value.toFixed(2)}</td>
                    <td style={{ textAlign: "right", padding: "10px", borderBottom: "1px solid rgba(0,255,136,0.1)", color: asset.pnl >= 0 ? "#51cf66" : "#ff6b6b" }}>
                      {asset.pnl >= 0 ? "+" : ""}${asset.pnl.toFixed(2)} ({asset.pnl_pct >= 0 ? "+" : ""}{asset.pnl_pct.toFixed(2)}%)
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
