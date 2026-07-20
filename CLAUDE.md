# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**Empire v2** is a multi-system SaaS platform combining:
1. **Video Production Service** — Stripe-powered quote form → HeyGen video generation → customer delivery
2. **Delivery Platform** (active development) — DoorDash-like food delivery with real-time GPS tracking
3. **Trading Automation** (secondary) — Futures/crypto trading with AI signal confirmation
4. **Content/Revenue Systems** — Email campaigns, YouTube publishing, data retention

**Current Focus:** Building customer + driver delivery app with live GPS tracking via WebSocket.

**Deployment:** Railway (https://empire-v2-production.up.railway.app)
**Branch:** `claude/custom-delivery-app-di39ur`

---

## Architecture

### Core Stack
- **Framework:** FastAPI (async, graceful error handling, WebSocket support)
- **Database:** SQLite via SQLAlchemy (video orders + delivery data)
- **Payments:** Stripe (checkout sessions, webhooks)
- **Video Generation:** HeyGen API (avatar + voice synthesis)
- **Real-time:** WebSocket for live GPS tracking + order status updates
- **Email:** Gmail SMTP (configured via env vars)
- **Monitoring:** Health monitor + data retention manager

### Main Entry Point
**main.py** (17,648 bytes)
- FastAPI app initialization with CORS
- Lifespan context manager for startup/shutdown
- Router registration (auth, workers, clients, jobs, bookings, payments, admin, orders, revenue, social)
- Critical endpoints:
  - `GET /quote` — serves quote_request.html form (reads file, returns HTMLResponse)
  - `GET /health` — deployment health check
  - `GET /order-success` — Stripe success redirect
  - `GET /monitor/*` — monitoring endpoints
  - `GET /retention/*` — data retention status

**Key Pattern:** Routers imported with try-except to prevent crashes if modules missing. Missing routers log warnings but don't stop startup.

### Delivery Platform Architecture
**routers/delivery.py** — main delivery app logic (to be created)

**Data Model:**
- **Restaurants:** name, address, menu_items, cuisine_type, ratings, delivery_fee
- **Menu Items:** restaurant_id, name, price, description, category
- **Orders:** customer_info, restaurant_id, items, status, delivery_address, driver_id, tracking
- **Drivers:** name, phone, current_location, status (available|on_delivery), vehicle_info, ratings
- **Real-time Tracking:** order_id, driver_location (lat/lon), estimated_arrival, customer_watching

**Flow:**
1. **Customer Browse** — `GET /delivery/restaurants` → list restaurants with filters (cuisine, rating, delivery_time)
2. **View Menu** — `GET /delivery/restaurants/{id}/menu` → get menu items with prices
3. **Create Order** — `POST /delivery/orders` → add items to cart, select address/payment
4. **Payment** — Uses Stripe (same as video orders), stores order as "pending"
5. **Auto-Assign Driver** — Background task finds nearest available driver, assigns order
6. **Real-time Tracking** — WebSocket connection `ws://localhost:8000/delivery/ws/track/{order_id}` 
   - Driver location updates sent every 5 seconds
   - Customer receives: driver_lat, driver_lon, estimated_arrival, driver_name, phone
7. **Delivery Complete** — Driver marks complete, customer rates driver/order

**WebSocket Layer (routers/delivery.py):**
```python
@app.websocket("/delivery/ws/track/{order_id}")
async def websocket_endpoint(websocket: WebSocket, order_id: str):
    # Accept connection from customer
    await websocket.accept()
    # Loop: poll driver location from DB, send to customer every 5sec
    # On delivery complete or order cancelled: close connection
```

**Driver Assignment:**
- Pseudo-code: `find_nearest_driver(order_location, radius=5km, status='available')`
- Updates driver status → "on_delivery"
- Stores driver_id in order record
- Driver app polls for new orders or uses WebSocket

### Order/Video Generation Flow
**routers/orders.py** (23,525 bytes) — main business logic

1. **POST /orders/request-quote** — Customer submits video request
   - Accepts: customer info, video type, script, avatar, language, delivery timeline
   - Avatar: 8 options (Anna, Carlos, Emma, James, Lisa, Marcus, Olivia, Ryan)
   - Language: 22 options (English US/UK/AU, Spanish, French, German, Italian, Portuguese, Dutch, Swedish, Norwegian, Danish, Polish, Russian, Japanese, Korean, Chinese Simplified/Traditional, Arabic, Hindi)
   - Returns: order_id + quote_price
   - Stores in `pending_orders` list with all fields

2. **POST /orders/{order_id}/create-checkout** — Stripe session creation
   - Uses stored order data to create Stripe checkout session
   - Returns: session_id for redirectToCheckout()

3. **POST /orders/webhook/stripe** — Payment confirmation webhook
   - Validates webhook signature
   - Marks order as paid
   - Triggers `generate_video_for_order()` background task (if HeyGen available)

4. **Async generate_video_for_order()** — Background video generation
   - Calls HeyGen API with: script, avatar (mapped to HeyGen ID), language (mapped to voice settings)
   - Polls HeyGen API every 10 seconds for up to 10 minutes
   - On completion: stores video_url, sends email to customer
   - On timeout/error: sets status accordingly

5. **GET /orders/customer/{order_id}** — Customer portal
   - Displays order status, video download link (if ready)

6. **GET /orders/admin-dashboard** — Admin video tracking
   - Shows all orders with generation status

### Frontend Form
**quote_request.html** (21,646 bytes)
- Beautiful purple gradient UI
- Two-stage flow: Get Quote → Accept & Pay Now
- Form fields: name, email, company, phone, video type, script, target audience, avatar, language, delivery timeline
- Dynamic pricing calculator
- Stripe.js integration (calls POST /orders/request-quote, then creates checkout session)
- All form data serialized to JSON and sent to backend

### Supporting Systems

**heygan_integration.py** (5,083 bytes)
- `async generate_video()` — calls HeyGen API v1/videos/generate
- `async get_video_url()` — polls HeyGen API for completion
- Avatar/language mapping tables (convert user-friendly names to HeyGen format)

**health_monitor.py** (17,248 bytes)
- Monitors all systems continuously
- Tracks errors, fixed issues, performance metrics
- Stores in permanent archive tables

**data_retention.py** (12,265 bytes)
- Archives old data to permanent storage
- Keeps all data forever (non-deletion retention policy)

**database.py** (941 bytes)
- SQLAlchemy engine + session factory
- SQLite connection string from env var

---

## Development & Deployment

### Local Testing

**Setup:**
```bash
pip install -r requirements.txt
```

**Environment Variables** (create .env or set in Railway):
```
STRIPE_SECRET_KEY=sk_...
STRIPE_PUBLISHABLE_KEY=pk_...
STRIPE_WEBHOOK_SECRET=whsec_...
HEYGAN_API_KEY=...
GMAIL_EMAIL=... (optional)
GMAIL_PASSWORD=... (optional)
```

**Run locally:**
```bash
python main.py
# Runs on http://localhost:8000
# /docs for interactive API docs
# /quote for quote form
```

**Test quote form:**
1. Visit http://localhost:8000/quote
2. Fill form (all fields required except phone, reference URL)
3. Click "Get My Quote"
4. Click "Accept & Pay Now"
5. Stripe checkout redirect (uses test/live keys based on env)

### Testing Delivery App Locally

**WebSocket Test (curl not ideal, use wscat or JavaScript):**
```bash
# Install wscat: npm install -g wscat
wscat -c ws://localhost:8000/delivery/ws/track/order-123

# Or test in browser console:
const ws = new WebSocket('ws://localhost:8000/delivery/ws/track/order-123');
ws.onmessage = (e) => console.log(JSON.parse(e.data));
```

**API Test Flow:**
1. `GET /delivery/restaurants` → browse restaurants
2. `GET /delivery/restaurants/{id}/menu` → view menu
3. `POST /delivery/orders` → create order with items + delivery address
4. WebSocket connect: `ws://localhost:8000/delivery/ws/track/{order_id}`
5. Check order status: `GET /delivery/orders/{order_id}`

### Deployment to Railway

**Branch:** `claude/custom-delivery-app-di39ur`

**Deploy steps:**
1. Push code: `git push -u origin claude/custom-delivery-app-di39ur`
2. Railway auto-detects push and redeploys
3. Wait 2-3 minutes for "Active" status
4. Test: `https://empire-v2-production.up.railway.app/delivery/restaurants`

**Key deployment files:**
- **Dockerfile** — Python 3.11-slim, copies all files, runs `python main.py`
- **railway.json** — defines main-app service + other video services
- **.env vars on Railway** — STRIPE_SECRET_KEY, STRIPE_PUBLISHABLE_KEY, STRIPE_WEBHOOK_SECRET, HEYGAN_API_KEY (no Gmail configured yet)

**Critical Fix Applied:** `/quote` endpoint tries multiple file paths to find quote_request.html (handles Railway's working directory variations):
```python
possible_paths = [
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "quote_request.html"),
    "/app/quote_request.html",
    "quote_request.html",
]
```

### Git Workflow

- **Branch name:** `claude/custom-delivery-app-di39ur` (do NOT push to main without explicit permission)
- **Commits:** Create new commits (don't amend) with clear messages
- **Deployment:** Push to this branch → Railway auto-redeploys in ~2-3 minutes
- **Key pattern:** All development on delivery features goes to this branch only

---

## Critical Implementation Details

### Order Storage
- **In-memory lists:** `pending_orders`, `completed_orders` (FastAPI variables, lost on restart)
- **Future:** Migrate to database persistence in orders table
- Order object schema:
  ```python
  {
    "id": int,
    "status": "quote_requested|payment_received|video_ready|...",
    "customer_name/email/company/phone": str,
    "video_type": str,
    "script_or_topic": str,
    "target_audience": str,
    "avatar": str,  # e.g., "anna"
    "language": str,  # e.g., "english_us"
    "delivery_days": int,
    "quote_price": int (cents),
    "paid": bool,
    "stripe_session_id": str,
    "video_generation_status": "pending|generating|completed|failed|timeout",
    "video_url": str,
    ...
  }
  ```

### HeyGen Integration
- Wraps imports in try-except with `HEYGAN_AVAILABLE` flag (app won't crash if httpx missing)
- Requires `HEYGAN_API_KEY` env var
- Maps user avatars to HeyGen IDs: anna → anna_public_ca_en, etc.
- Maps language codes to HeyGen voice format (language + accent)
- Timeout: 10 minutes max polling (60 attempts × 10 sec)

### Stripe Integration
- Webhook endpoint: `POST /orders/webhook/stripe`
- **Critical:** Validates webhook signature with STRIPE_WEBHOOK_SECRET
- Webhook URL configured in Stripe dashboard: https://empire-v2-production.up.railway.app/orders/webhook/stripe
- Metadata: order_id, customer_email, customer_name (passed to HeyGen)

### Email Notifications
- **Status:** Optional (app works without Gmail credentials)
- **Trigger:** When video_generation_status == "completed"
- **Recipient:** customer_email
- **Env vars:** GMAIL_EMAIL, GMAIL_PASSWORD (App password, not regular password)
- Requires Gmail "App passwords" feature (may not be available on Google Workspace accounts)

### Delivery App Database Schema

**Tables to create:**

```sql
-- Restaurants
CREATE TABLE restaurants (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  address TEXT NOT NULL,
  latitude REAL,
  longitude REAL,
  cuisine_type TEXT,
  rating REAL,
  delivery_fee INTEGER (cents),
  created_at TIMESTAMP
);

-- Menu Items
CREATE TABLE menu_items (
  id INTEGER PRIMARY KEY,
  restaurant_id INTEGER,
  name TEXT NOT NULL,
  price INTEGER (cents),
  description TEXT,
  category TEXT,
  available BOOLEAN
);

-- Delivery Orders
CREATE TABLE delivery_orders (
  id INTEGER PRIMARY KEY,
  customer_id TEXT,
  customer_name TEXT,
  customer_phone TEXT,
  customer_email TEXT,
  restaurant_id INTEGER,
  delivery_address TEXT,
  delivery_lat REAL,
  delivery_lon REAL,
  status TEXT (pending|assigned|on_way|delivered|cancelled),
  driver_id INTEGER,
  total_price INTEGER (cents),
  created_at TIMESTAMP,
  delivered_at TIMESTAMP
);

-- Order Items (line items)
CREATE TABLE order_items (
  id INTEGER PRIMARY KEY,
  order_id INTEGER,
  menu_item_id INTEGER,
  quantity INTEGER,
  price INTEGER (cents at time of order)
);

-- Drivers
CREATE TABLE drivers (
  id INTEGER PRIMARY KEY,
  name TEXT,
  phone TEXT,
  vehicle_type TEXT,
  status TEXT (available|on_delivery|offline),
  current_lat REAL,
  current_lon REAL,
  last_updated TIMESTAMP,
  total_deliveries INTEGER,
  rating REAL
);

-- Driver Locations (history for tracking)
CREATE TABLE driver_locations (
  id INTEGER PRIMARY KEY,
  driver_id INTEGER,
  order_id INTEGER,
  latitude REAL,
  longitude REAL,
  timestamp TIMESTAMP
);
```

---

## Verification Before Deployment

Before pushing changes to Railway, verify:

1. **Python syntax:** `python3 -m py_compile main.py routers/orders.py heygan_integration.py`
2. **Imports:** `python3 -c "from heygan_integration import generate_video; print('OK')"`
3. **File presence:** `test -f quote_request.html && echo "Form exists"`
4. **Endpoint logic:** Check main.py /quote endpoint reads HTML correctly
5. **Git status:** `git status` — ensure no uncommitted changes before deployment

---

## Common Tasks

**Add new video type:**
- Update `quote_request.html` — add `<option>` in videoType select
- Update `baseVideoPrices` object in script (JavaScript pricing calculator)
- Update backend pricing function if logic changes

**Add new avatar:**
- Update `quote_request.html` — add `<option>` in avatar select
- Update `heygan_integration.py` — add to AVATAR_MAP dictionary
- Map to actual HeyGen avatar ID

**Add new language:**
- Update `quote_request.html` — add `<option>` in language select  
- Update `heygan_integration.py` — add to VOICE_MAP dictionary
- Include language name and accent for HeyGen API

**Test payment flow end-to-end:**
1. POST to /orders/request-quote with all required fields
2. Capture order_id from response
3. POST to /orders/{order_id}/create-checkout to get Stripe session_id
4. Simulate Stripe webhook: POST to /orders/webhook/stripe with valid signature
5. Check /orders/admin-dashboard for video generation status

**Monitor deployment:**
- https://empire-v2-production.up.railway.app/health — returns {"status": "ok"}
- https://empire-v2-production.up.railway.app/monitor/status — health monitor status
- https://empire-v2-production.up.railway.app/monitor/errors — error history
- Railway deploy logs show startup sequence

---

## Common Delivery App Tasks

**Seed test restaurants:**
- Create `scripts/seed_restaurants.py` that inserts 5-10 restaurants with lat/lon into database
- Include menu items per restaurant (3-5 items each)
- Run before demo: `python scripts/seed_restaurants.py`

**Add a new restaurant:**
- `POST /delivery/restaurants` with: name, address, latitude, longitude, cuisine_type
- Create menu items: `POST /delivery/restaurants/{id}/menu_items` for each item

**Create a test driver:**
- `POST /delivery/drivers` with: name, phone, vehicle_type, status="available"
- Initialize location: `POST /delivery/drivers/{id}/location` with lat/lon

**Test real-time tracking:**
1. Create an order: `POST /delivery/orders` → capture order_id
2. Open WebSocket: `ws://localhost:8000/delivery/ws/track/{order_id}`
3. Simulate driver movement: `POST /delivery/drivers/{driver_id}/location` with updated lat/lon
4. Verify WebSocket receives: `{driver_lat, driver_lon, estimated_arrival, status}`

**Debug WebSocket connections:**
- Check FastAPI logs: `tail -f app.log` shows connection open/close
- Verify message delivery: Add console.log in browser DevTools
- Test timeout: Keep connection open 30+ minutes, verify doesn't drop

---

## Known Limitations & TODOs

**Video Production:**
- Order persistence: Restart loses pending orders (use DB for production)
- Email: Gmail not configured (skip for now, test with HeyGen generation only)
- Admin auth: No authentication on admin endpoints yet (add before production)

**Delivery App (In Progress):**
- Restaurant seeding: Hardcode 5-10 test restaurants initially
- Driver assignment: Implement auto-assign to nearest driver logic
- WebSocket scaling: Use Redis for pub/sub if scaling to many concurrent orders
- Real-time verification: Test GPS updates at scale (100+ concurrent deliveries)
- Customer/Driver auth: Add JWT auth before production launch
- Payment confirmation: Hook order creation to Stripe payment completion

---

## References

- **API Endpoints:** See API_ENDPOINTS.md
- **Stripe docs:** https://stripe.com/docs/api/checkout/sessions
- **HeyGen docs:** https://docs.heygen.com/
- **FastAPI docs:** http://localhost:8000/docs (when running locally)
