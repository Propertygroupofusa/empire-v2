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
    // Step 1: Fetch BTC account balance
    const timestamp1 = Date.now() / 1000;
    const method1 = "GET";
    const path1 = "/api/v3/brokerage/accounts";
    const body1 = "";

    const message1 = timestamp1 + method1 + path1 + body1;
    const signature1 = crypto
      .createHmac("sha256", apiSecret)
      .update(message1)
      .digest("base64");

    const accountsResponse = await fetch(`https://api.coinbase.com${path1}`, {
      method: method1,
      headers: {
        "CB-ACCESS-KEY": apiKey,
        "CB-ACCESS-SIGN": signature1,
        "CB-ACCESS-TIMESTAMP": timestamp1.toString(),
        "CB-ACCESS-PASSPHRASE": passphrase,
      },
    });

    if (!accountsResponse.ok) {
      throw new Error("Failed to fetch accounts");
    }

    const accountsData = await accountsResponse.json();
    const btcAccount = accountsData.accounts?.find(
      (acc: any) => acc.currency === "BTC"
    );

    if (!btcAccount) {
      return NextResponse.json(
        { message: "No BTC account found" },
        { status: 404 }
      );
    }

    const btcAmount = parseFloat(btcAccount.available_balance.value);

    if (btcAmount <= 0) {
      return NextResponse.json(
        { message: "No BTC available to withdraw" },
        { status: 400 }
      );
    }

    // Step 2: Get current BTC price
    const priceResponse = await fetch(
      "https://api.coinbase.com/v2/prices/BTC-USD/spot"
    );

    let btcPrice = 0;
    if (priceResponse.ok) {
      const priceData = await priceResponse.json();
      btcPrice = parseFloat(priceData.data?.amount || "0");
    }

    if (btcPrice <= 0) {
      throw new Error("Could not fetch BTC price");
    }

    // Step 3: Create market sell order for full BTC amount
    const timestamp2 = Date.now() / 1000;
    const method2 = "POST";
    const path2 = "/api/v3/brokerage/orders";
    const bodyObj = {
      client_order_id: crypto.randomUUID(),
      product_id: "BTC-USD",
      side: "sell",
      order_configuration: {
        market_market_ioc: {
          base_size: btcAmount.toString(),
        },
      },
    };
    const bodyStr = JSON.stringify(bodyObj);

    const message2 = timestamp2 + method2 + path2 + bodyStr;
    const signature2 = crypto
      .createHmac("sha256", apiSecret)
      .update(message2)
      .digest("base64");

    const orderResponse = await fetch(`https://api.coinbase.com${path2}`, {
      method: method2,
      headers: {
        "CB-ACCESS-KEY": apiKey,
        "CB-ACCESS-SIGN": signature2,
        "CB-ACCESS-TIMESTAMP": timestamp2.toString(),
        "CB-ACCESS-PASSPHRASE": passphrase,
        "Content-Type": "application/json",
      },
      body: bodyStr,
    });

    if (!orderResponse.ok) {
      const errorText = await orderResponse.text();
      throw new Error(`Order failed: ${errorText}`);
    }

    const orderData = await orderResponse.json();

    return NextResponse.json({
      success: true,
      btc_amount: btcAmount,
      btc_price: btcPrice,
      estimated_proceeds: btcAmount * btcPrice,
      order_id: orderData.order_id,
      status: orderData.order_status,
    });
  } catch (error: any) {
    return NextResponse.json(
      { message: error.message || "Failed to withdraw BTC" },
      { status: 400 }
    );
  }
}
