# Railway Network Egress Allowlist

The Railway Network Egress Allowlist for the `empire-v2` project needs to be configured manually in the Railway dashboard. This repository includes a server watchdog (RAILWAY_SETUP.sh) that expects the app to be able to reach external services (Stripe, HeyGen, trading/data providers, GitHub, SMTP). If the allowlist is not configured, external integrations will fail.

IMPORTANT: This is a manual step that must be completed in the Railway dashboard. It cannot be set from within the repository.

What to do (manual step required):

1. Open the Railway dashboard: https://railway.app/dashboard
2. Select the project: `empire-v2`
3. Select the service: `main-app` (or the service running the FastAPI app)
4. Navigate to Settings → Network (or the "Policies" tab)
5. Find the "Egress Allowlist" section
6. Add the following hosts (add both exact hosts and wildcards as shown):

   - api.alpaca.markets (Alpaca futures)
   - data.alpaca.markets (Alpaca data)
   - api.coinbase.com (Coinbase trading)
   - *.coinbase.com (Coinbase wildcard)
   - stripe.com (Stripe)
   - *.stripe.com (Stripe wildcard)
   - api.polygon.io (Market data)
   - github.com
   - raw.githubusercontent.com
   - smtp.gmail.com

7. Click Save / Apply

Why this is required
- The FastAPI app communicates with external APIs (Stripe, HeyGen, Alpaca, Coinbase, Polygon, GitHub raw content, and SMTP). If Railway egress is restricted, the server will be unable to call these services and key flows (quote generation, payments, webhooks, video generation, email delivery) will fail.

If you want, I can also add an additional sanity-check script in the repo (e.g., scripts/check_egress.sh) that you can run from a Railway shell to verify connectivity to the required hosts once the allowlist is configured.

Author: GitHub Copilot
