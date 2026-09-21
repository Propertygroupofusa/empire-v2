import { NextRequest, NextResponse } from "next/server";
import crypto from "crypto";

export async function POST(request: NextRequest) {
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
    const body = await request.json();
    const { product_id, size, side, order_type } = body;

    const timestamp = Date.now() / 1000;
    const method = "POST";
    const path = "/api/v3/brokerage/orders";
    const bodyStr = JSON.stringify({
      client_order_id: crypto.randomUUID(),
      product_id,
      side,
      order_configuration: {
        market_market_ioc: {
          quote_size: size,
        },
      },
    });

    const message = timestamp + method + path + bodyStr;
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
        "Content-Type": "application/json",
      },
      body: bodyStr,
    });

    if (!response.ok) {
      const error = await response.text();
      throw new Error(error);
    }

    const order = await response.json();

    return NextResponse.json({
      success: true,
      order_id: order.order_id,
      status: order.order_type,
    });
  } catch (error: any) {
    return NextResponse.json(
      { message: error.message || "Failed to place order" },
      { status: 400 }
    );
  }
}
