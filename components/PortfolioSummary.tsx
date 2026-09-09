interface PortfolioData {
  totalValue: number;
  totalPnL: number;
  totalPnLPct: number;
}

export default function PortfolioSummary({ data }: { data: PortfolioData }) {
  const isPosive = data.totalPnL >= 0;

  return (
    <div className="portfolio-summary">
      <div className="summary-card">
        <h3>Total Portfolio Value</h3>
        <div className="value">${data.totalValue.toFixed(2)}</div>
      </div>
      <div className="summary-card">
        <h3>Total P&L</h3>
        <div className={`value ${isPosive ? "" : "negative"}`}>
          {isPosive ? "+" : ""}${data.totalPnL.toFixed(2)}
        </div>
      </div>
      <div className="summary-card">
        <h3>Total P&L %</h3>
        <div className={`value ${isPosive ? "" : "negative"}`}>
          {isPosive ? "+" : ""}
          {data.totalPnLPct.toFixed(2)}%
        </div>
      </div>
    </div>
  );
}
