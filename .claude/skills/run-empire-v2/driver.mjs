#!/usr/bin/env node

/**
 * Driver script for Empire v2 FastAPI platform.
 * Starts the server and runs smoke tests on key endpoints.
 * Usage: node driver.mjs [command]
 * Commands: start, test, health, quote-form, create-order, status, stop
 */

import { spawn, spawnSync } from "child_process";
import * as http from "http";
import * as path from "path";
import { fileURLToPath } from "url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const projectRoot = path.resolve(__dirname, "../../..");
const PORT = 8000;
const BASE_URL = `http://localhost:${PORT}`;

let serverProcess = null;

// Helper to make HTTP requests. `query` becomes URL search params (GET
// filters, or the second-factor `email` on the customer-portal route);
// `jsonBody` is sent as a real JSON request body for endpoints declared
// with a Pydantic model (FastAPI 422s if you send those as query params
// instead - this bit both of us before).
async function makeRequest(method, endpoint, query = null, jsonBody = null) {
  return new Promise((resolve, reject) => {
    const url = new URL(`${BASE_URL}${endpoint}`);
    if (query) {
      Object.entries(query).forEach(([key, value]) => {
        url.searchParams.append(key, value);
      });
    }

    const bodyStr = jsonBody ? JSON.stringify(jsonBody) : null;
    const options = {
      method,
      timeout: 5000,
      headers: bodyStr ? { "Content-Type": "application/json", "Content-Length": Buffer.byteLength(bodyStr) } : {},
    };

    const req = http.request(url, options, (res) => {
      let data = "";
      res.on("data", (chunk) => (data += chunk));
      res.on("end", () => {
        try {
          resolve({
            status: res.statusCode,
            headers: res.headers,
            body: data ? JSON.parse(data) : null,
            raw: data,
          });
        } catch (e) {
          resolve({
            status: res.statusCode,
            headers: res.headers,
            body: null,
            raw: data,
          });
        }
      });
    });

    req.on("error", reject);
    req.end(bodyStr || undefined);
  });
}

// Wait for server to be ready
async function waitForServer(maxAttempts = 30) {
  for (let i = 0; i < maxAttempts; i++) {
    try {
      const res = await makeRequest("GET", "/health");
      if (res.status === 200) {
        console.log("✓ Server ready");
        return true;
      }
    } catch (e) {
      // Server not ready yet
    }
    await new Promise((r) => setTimeout(r, 500));
  }
  throw new Error("Server failed to start within timeout");
}

// Start the server
async function startServer() {
  return new Promise((resolve, reject) => {
    process.chdir(projectRoot);
    console.log(`Starting FastAPI server on port ${PORT}...`);

    serverProcess = spawn("python", ["main.py"], {
      stdio: ["ignore", "pipe", "pipe"],
      cwd: projectRoot,
    });

    let started = false;

    serverProcess.stdout.on("data", (data) => {
      const output = data.toString();
      if (output.includes("Router loaded")) {
        console.log(output.trim());
      }
      if (output.includes("Application startup complete")) {
        started = true;
      }
    });

    serverProcess.stderr.on("data", (data) => {
      const output = data.toString();
      if (output.includes("INFO") || output.includes("WARNING")) {
        // Log but don't reject on warnings
        if (output.includes("ERROR") || output.includes("error")) {
          console.error(output.trim());
        }
      }
    });

    serverProcess.on("error", reject);

    // Give it a moment to start, then check
    setTimeout(async () => {
      try {
        await waitForServer();
        resolve();
      } catch (e) {
        reject(e);
      }
    }, 1000);
  });
}

// Stop the server
function stopServer() {
  if (serverProcess) {
    console.log("\nStopping server...");
    serverProcess.kill("SIGTERM");
    serverProcess = null;
  }
}

// Test endpoints
async function runTests() {
  console.log("\n📊 Running smoke tests...\n");

  const tests = [
    {
      name: "Health Check",
      method: "GET",
      endpoint: "/health",
      expectedStatus: 200,
    },
    {
      name: "Quote Form HTML",
      method: "GET",
      endpoint: "/quote",
      expectedStatus: 200,
      checkBody: true,
    },
    {
      name: "List Subscription Tiers",
      method: "GET",
      endpoint: "/subscriptions/tiers",
      expectedStatus: 200,
      expectedKeys: ["tiers", "total_tiers"],
      onSuccess: (body) => `${body.total_tiers} tiers`,
    },
    {
      name: "Get Pro Tier Details",
      method: "GET",
      endpoint: "/subscriptions/tiers/pro",
      expectedStatus: 200,
      expectedKeys: ["tier"],
      onSuccess: () => "tier detail loaded",
    },
    {
      name: "Create Order (Request Quote)",
      method: "POST",
      // NOTE: single "/orders" prefix, not "/orders/orders" - that double
      // prefix was true when this route was declared as
      // @router.post("/orders/request-quote") under the "/orders" mount;
      // it's since been changed to a relative @router.post("/request-quote"),
      // so the old double-prefix quirk from an earlier version of this
      // skill no longer applies. Verify with the actual registered path if
      // this 404s again: `python3 -c "from main import app; ..."` and grep
      // app.routes for "request-quote".
      endpoint: "/orders/request-quote",
      // JSON body, not query params - the route takes a Pydantic
      // QuoteRequest model (routers/orders.py) since the DB-backed
      // storage migration; query params here 422 with a validation error.
      jsonBody: {
        customer_name: "Test User",
        customer_email: "test@example.com",
        customer_company: "Test Company",
        video_type: "explainer",
        script_or_topic: "Test script",
        target_audience: "Business professionals",
        avatar: "anna",
        language: "english_us",
      },
      expectedStatus: 200,
      expectedKeys: ["success", "order_id", "quote_price"],
      onSuccess: (body) => `Order #${body.order_id} created (${body.quote_price} cents)`,
    },
  ];

  let orderId = null;
  let passedTests = 0;
  let failedTests = 0;

  for (const test of tests) {
    try {
      const res = await makeRequest(test.method, test.endpoint, test.query, test.jsonBody);

      if (res.status !== test.expectedStatus) {
        console.log(
          `✗ ${test.name}: Expected ${test.expectedStatus}, got ${res.status}`
        );
        failedTests++;
        continue;
      }

      if (test.checkBody) {
        if (res.raw.includes("<!DOCTYPE html>") || res.raw.includes("<html")) {
          console.log(`✓ ${test.name}: HTML form loaded`);
          passedTests++;
        } else {
          console.log(`✗ ${test.name}: Expected HTML, got ${res.raw.slice(0, 100)}`);
          failedTests++;
        }
      } else if (test.expectedKeys) {
        const missing = test.expectedKeys.filter((k) => !(k in res.body));
        if (missing.length === 0) {
          if (res.body.order_id !== undefined) orderId = res.body.order_id;
          const detail = test.onSuccess ? test.onSuccess(res.body) : "OK";
          console.log(`✓ ${test.name}: ${detail}`);
          passedTests++;
        } else {
          console.log(
            `✗ ${test.name}: Missing keys: ${missing.join(", ")}`
          );
          failedTests++;
        }
      } else {
        console.log(`✓ ${test.name}`);
        passedTests++;
      }
    } catch (error) {
      console.log(`✗ ${test.name}: ${error.message}`);
      failedTests++;
    }
  }

  // Test order status if order was created. This is the customer-facing
  // portal route (routers/orders.py, GET /orders/customer/{order_id}) -
  // it requires the same email the order was placed under as a second
  // factor against ID enumeration, instead of an admin key. The plain
  // GET /orders/{order_id} route is admin-key-gated (see admin_auth) and
  // isn't the one a real customer (or this smoke test) would hit.
  if (orderId !== null) {
    try {
      const res = await makeRequest(
        "GET",
        `/orders/customer/${orderId}`,
        { email: "test@example.com" }
      );
      if (res.status === 200 && res.body?.order_id === orderId) {
        console.log(`✓ Get Order Status: Order #${orderId} retrieved`);
        passedTests++;
      } else {
        console.log(`✗ Get Order Status: Expected 200 w/ matching order_id, got ${res.status} ${JSON.stringify(res.body)}`);
        failedTests++;
      }
    } catch (error) {
      console.log(`✗ Get Order Status: ${error.message}`);
      failedTests++;
    }
  }

  console.log(`\n📈 Results: ${passedTests} passed, ${failedTests} failed`);
  return failedTests === 0;
}

// Main CLI
async function main() {
  const command = process.argv[2] || "start";

  try {
    switch (command) {
      case "start":
        await startServer();
        console.log(
          `\n✓ Empire v2 server running at ${BASE_URL}`
        );
        console.log("  - Quote form: /quote");
        console.log("  - API docs: /docs");
        console.log("\nRun 'node driver.mjs test' to run smoke tests");
        break;

      case "test":
        await startServer();
        const success = await runTests();
        stopServer();
        process.exit(success ? 0 : 1);
        break;

      case "health":
        const health = await makeRequest("GET", "/health");
        console.log(JSON.stringify(health.body, null, 2));
        break;

      case "quote-form":
        const quoteForm = await makeRequest("GET", "/quote");
        if (quoteForm.status === 200) {
          console.log("✓ Quote form loads successfully");
          console.log(
            `Response size: ${quoteForm.raw.length} bytes`
          );
        } else {
          console.log(`✗ Quote form failed: ${quoteForm.status}`);
        }
        break;

      case "create-order":
        const order = await makeRequest(
          "POST",
          "/orders/request-quote",
          null,
          {
            customer_name: "Test User",
            customer_email: "test@example.com",
            customer_company: "Test Company",
            video_type: "explainer",
            script_or_topic: "Test script",
            target_audience: "Business professionals",
            avatar: "anna",
            language: "english_us",
          }
        );
        console.log(JSON.stringify(order.body, null, 2));
        break;

      case "stop":
        stopServer();
        console.log("Server stopped");
        break;

      default:
        console.log(`Unknown command: ${command}`);
        console.log("Available commands: start, test, health, quote-form, create-order, stop");
        process.exit(1);
    }
  } catch (error) {
    console.error(`Error: ${error.message}`);
    stopServer();
    process.exit(1);
  }
}

// Handle cleanup
process.on("SIGINT", () => {
  stopServer();
  process.exit(0);
});

process.on("SIGTERM", () => {
  stopServer();
  process.exit(0);
});

main();
