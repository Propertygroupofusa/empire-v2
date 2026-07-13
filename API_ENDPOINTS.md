# 📡 Revenue Automation API - Complete Reference

All endpoints available at: `https://your-app.railway.app/revenue/*`

## 🎬 Daily Publishing

### Check Schedule
```bash
GET /revenue/publishing/status
```
Returns: Publishing enabled status, schedule, next jobs

### Start Publishing
```bash
POST /revenue/publishing/start
```
Enables automatic 4x daily video generation

### Generate Video Now
```bash
POST /revenue/publishing/generate-now
```
Queues a video immediately (doesn't wait for schedule)

---

## 💰 YouTube Monetization

### Channel Metrics
```bash
GET /revenue/youtube/metrics/channel
```
Returns: Subscribers, total views, video count

### Analytics (Last N Days)
```bash
GET /revenue/youtube/metrics/analytics?days_back=30
```
Returns: Daily views, watch time, estimated revenue, RPM, CPM

### Monthly Projections
```bash
GET /revenue/youtube/metrics/projections
```
Returns: Daily/monthly/annual revenue forecasts

### Top Videos
```bash
GET /revenue/youtube/videos/top?limit=10
```
Returns: Best performing videos with stats

### Complete Summary
```bash
GET /revenue/youtube/summary
```
Returns: All YouTube metrics in one response

---

## 💼 Video Service (Client Orders)

### Get Pricing
```bash
GET /revenue/video-service/pricing
```
Returns: 3 tiers (Standard $500, Pro $750, Premium $1000)

### Create Order
```bash
POST /revenue/video-service/order
```
Parameters:
- `client_email` (string, required)
- `script` (string, required)
- `tier` (string: "standard", "pro", "premium")

Example:
```bash
curl -X POST https://your-app.railway.app/revenue/video-service/order \
  -H "Content-Type: application/json" \
  -d '{
    "client_email": "client@example.com",
    "script": "Today we made $5000 trading AAPL...",
    "tier": "professional"
  }'
```

Returns: Order ID, status, delivery time

### Check Order Status
```bash
GET /revenue/video-service/order/{order_id}
```
Returns: Current status, download link when ready, revision count

### Request Revision
```bash
POST /revenue/video-service/order/{order_id}/revision
```
Parameters:
- `revised_script` (string, required)

Returns: New job ID, revisions remaining

### Get All Orders
```bash
GET /revenue/video-service/orders
```
Query parameters:
- `client_email` (optional, filter by client)
- `status` (optional: "pending", "processing", "ready", "delivered")

### Service Statistics
```bash
GET /revenue/video-service/stats
```
Returns: Total orders, completed, revenue, average order value

---

## 📧 Lead Generation

### Create Lead
```bash
POST /revenue/leads/create
```
Parameters:
- `name` (string, required)
- `email` (string, required)
- `source` (required: "youtube_video", "youtube_description", "website_form", "email_signup", "social_media", "referral", "course_interest", "video_service")
- `source_detail` (string, optional: video ID, form name, etc.)
- `phone` (string, optional)
- `company` (string, optional)
- `tags` (array, optional: ["tag1", "tag2"])

Example:
```bash
curl -X POST https://your-app.railway.app/revenue/leads/create \
  -H "Content-Type: application/json" \
  -d '{
    "name": "John Trader",
    "email": "john@example.com",
    "source": "youtube_description",
    "source_detail": "video_12345",
    "company": "ABC Trading",
    "tags": ["interested_in_courses", "high_engagement"]
  }'
```

### Get Lead Details
```bash
GET /revenue/leads/{lead_id}
```
Returns: All lead information, engagement score, history

### Get Leads (Filtered)
```bash
GET /revenue/leads
```
Query parameters:
- `status` (optional: "new", "contacted", "qualified", "nurturing", "converted", "lost")
- `source` (optional: see create lead)
- `min_score` (optional: minimum engagement score)

### Update Lead Status
```bash
PUT /revenue/leads/{lead_id}/status
```
Parameters:
- `status` (required: "new", "contacted", "qualified", "nurturing", "converted", "lost")
- `notes` (string, optional)

### Log Engagement
```bash
POST /revenue/leads/{lead_id}/engagement
```
Parameters:
- `action` (string, required: "email_clicked", "form_submitted", "video_watched", etc.)
- `points` (integer, default 10)

### Convert Lead
```bash
POST /revenue/leads/{lead_id}/convert
```
Parameters:
- `value` (number, required: dollar amount)
- `conversion_type` (string, required: "video_service", "course", "consulting", etc.)

### Lead Metrics
```bash
GET /revenue/leads/metrics/summary
```
Returns: Total leads, qualified, converted, conversion rate, total value

### Hot Leads (Sales Ready)
```bash
GET /revenue/leads/hot?limit=10
```
Returns: Top leads by engagement score ready for outreach

### YouTube Description CTA
```bash
GET /revenue/leads/cta/youtube-description?video_title=Market%20Analysis
```
Returns: Pre-formatted YouTube description with lead capture CTAs

### Landing Page Form
```bash
GET /revenue/leads/forms/landing-page
```
Returns: Form template for capturing leads

---

## 📚 Course Builder

### Create Course
```bash
POST /revenue/courses/create
```
Parameters:
- `title` (string, required)
- `description` (string, required)
- `level` (required: "beginner", "intermediate", "advanced")
- `price` (number, required: in dollars, e.g., 97)

Example:
```bash
curl -X POST https://your-app.railway.app/revenue/courses/create \
  -H "Content-Type: application/json" \
  -d '{
    "title": "Video Marketing Mastery",
    "description": "Learn to create viral videos that convert",
    "level": "beginner",
    "price": 97
  }'
```

### Get All Courses
```bash
GET /revenue/courses
```
Query parameters:
- `published_only` (boolean, default true)

### Public Course Catalog
```bash
GET /revenue/courses/catalog
```
Returns: Published courses ready for sale

### Add Module to Course
```bash
POST /revenue/courses/{course_id}/module
```
Parameters:
- `title` (string, required)
- `description` (string, required)
- `order` (integer, optional)

### Add Lesson to Module
```bash
POST /revenue/courses/{course_id}/module/{module_id}/lesson
```
Parameters:
- `video_id` (string, required)
- `title` (string, required)
- `duration` (integer, required: in seconds)
- `content` (string, required: transcript or notes)

### Publish Course
```bash
POST /revenue/courses/{course_id}/publish
```
Makes course available for student enrollment

### Enroll Student
```bash
POST /revenue/courses/{course_id}/enroll
```
Parameters:
- `student_id` (string, required)
- `student_email` (string, required)

Returns: Enrollment confirmation, payment intent if applicable

### Get Student Progress
```bash
GET /revenue/courses/{course_id}/progress/{student_id}
```
Returns: Lessons completed, completion %, course completion status

### Mark Lesson Complete
```bash
POST /revenue/courses/{course_id}/progress/{student_id}/lesson/{lesson_id}
```
Tracks student progress

### Course Statistics
```bash
GET /revenue/courses/{course_id}/stats
```
Returns: Enrollments, revenue, average rating, completion rate

---

## 📊 Revenue Dashboard

### All Metrics
```bash
GET /revenue/dashboard/all-metrics
```
Returns: Everything across all revenue streams

### Revenue Summary
```bash
GET /revenue/dashboard/revenue-summary
```
Returns: Total revenue, breakdown by source (YouTube, services, courses, leads)

### Executive Summary
```bash
GET /revenue/dashboard/executive-summary
```
Returns: 10 key metrics for quick review

### ROI Analysis
```bash
GET /revenue/dashboard/roi-analysis
```
Returns: Profit, ROI %, cost breakdown, break-even analysis

### Growth Forecast
```bash
GET /revenue/dashboard/growth-forecast
```
Returns: 30/60/90-day projections, subscriber growth

---

## 🔍 Response Examples

### Dashboard Executive Summary
```json
{
  "total_revenue_30d": 2450.50,
  "projected_annual_revenue": 29406,
  "profit_this_month": 2350.50,
  "roi_percent": 2350.5,
  "youtube_subscribers": 2450,
  "youtube_views_30d": 125000,
  "total_leads": 842,
  "lead_conversion_rate": 3.3,
  "active_students": 45,
  "key_insight": "Strong revenue growth - maintain daily publishing schedule"
}
```

### Publishing Status
```json
{
  "enabled": true,
  "schedule": {
    "morning": "08:00",
    "midday": "12:00",
    "close": "16:30",
    "evening": "18:00"
  },
  "next_jobs": [
    {
      "name": "Morning market analysis",
      "trigger": "cron[hour: 8, minute: 0]",
      "next_run": "2026-07-12T08:00:00Z"
    }
  ]
}
```

### Video Order Response
```json
{
  "success": true,
  "order_id": "ORD-abc12345",
  "status": "processing",
  "delivery_time": "12-24 hours",
  "message": "Your professional video has been queued for generation"
}
```

### YouTube Analytics
```json
{
  "period": "Last 30 days",
  "totals": {
    "views": 125000,
    "watch_time_minutes": 450000,
    "revenue": 750.25,
    "avg_rpm": 6.0,
    "avg_cpm": 2.5
  },
  "daily_data": [
    {
      "date": "2026-07-11",
      "views": 5000,
      "watch_time_minutes": 15000,
      "estimated_revenue": 30.0,
      "rpm": 6.0
    }
  ]
}
```

---

## 🚀 Quick Start Commands

```bash
# Set your app URL
APP=https://your-app.railway.app

# Enable daily publishing
curl -X POST $APP/revenue/publishing/start

# Check status
curl $APP/revenue/publishing/status

# View dashboard
curl $APP/revenue/dashboard/executive-summary

# Get YouTube earnings
curl $APP/revenue/youtube/metrics/analytics?days_back=30

# View leads
curl $APP/revenue/leads/hot?limit=20

# Create course
curl -X POST $APP/revenue/courses/create \
  -H "Content-Type: application/json" \
  -d '{"title":"My Course","price":97,"level":"beginner","description":"Course description"}'

# See everything
curl $APP/revenue/dashboard/all-metrics
```

---

## 🔑 Authentication

Currently all endpoints are open (you'll want to add authentication before production).

To add authentication:
1. Update `routers/revenue_automation.py`
2. Add OAuth/JWT verification
3. Require API key headers

---

## 📈 Monitoring

Check these endpoints regularly:

**Daily:**
```bash
curl https://your-app.railway.app/revenue/dashboard/executive-summary
```

**Weekly:**
```bash
curl https://your-app.railway.app/revenue/youtube/metrics/analytics?days_back=7
curl https://your-app.railway.app/revenue/leads/hot
curl https://your-app.railway.app/revenue/video-service/stats
```

**Monthly:**
```bash
curl https://your-app.railway.app/revenue/dashboard/all-metrics
curl https://your-app.railway.app/revenue/dashboard/roi-analysis
curl https://your-app.railway.app/revenue/dashboard/growth-forecast
```

---

**Last Updated**: 2026-07-11  
**Status**: All endpoints live and earning 💰
