import { NextRequest, NextResponse } from "next/server";
import crypto from "crypto";

export async function GET(request: NextRequest) {
  const apiKey = process.env.COINBASE_API_KEY;
  const apiSecret = process.env.COINBASE_SECRET_KEY;
  const passphrase = process.env.COINBASE_PASSPHRASE;

  if (!apiKey || !apiSecret || !passphrase) {
    return NextResponse.json(
      { message: "Coinbase API keys not configured" },
      { status: 401 }
    );
  }

  try {
    const timestamp = Date.now() / 1000;
    const method = "GET";
    const path = "/api/v3/brokerage/accounts";
    const body = "";

    const message = timestamp + method + path + body;
    const signature = crypto
      .createHmac("sha256", apiSecret)
      .update(message)
      .digest("base64");

    const response = await fetch(`https://api.coinbase.com${path}`, {
      method,
      headers: {
        "CB-ACCESS-KEY": apiKey,
        "CB-ACCESS-SIGN": signature,
        "CB-ACCESS-TIMESTAMP": timestamp.toString(),
        "CB-ACCESS-PASSPHRASE": passphrase,
      },
    });

    if (!response.ok) {
      throw new Error("Failed to fetch accounts");
    }

    const data = await response.json();

    // Transform accounts data (filter to assets with balance > 0)
    const accounts = data.accounts || [];
    const formatted = accounts
      .filter((acc: any) => parseFloat(acc.available_balance.value) > 0)
      .map((acc: any) => ({
        currency: acc.currency,
        amount: parseFloat(acc.available_balance.value),
        current_price: Math.random() * 100, // Placeholder
        value: parseFloat(acc.available_balance.value) * (Math.random() * 100),
        pnl: 0,
        pnl_pct: 0,
      }));

    return NextResponse.json(formatted);
  } catch (error) {
    return NextResponse.json(
      { message: "Failed to fetch accounts" },
      { status: 500 }
    );
  }
}
