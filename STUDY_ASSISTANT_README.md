# AI Study Assistant — MVP Implementation Guide

## Overview

The Study Assistant is a new revenue stream that converts textbook images into AI-generated study materials (guides, quizzes, flashcards). This is a **freemium SaaS** built on FastAPI + Claude Vision API.

---

## Files Created/Modified

### New Files
- **`routers/study.py`** — Core API endpoints for study material generation
- **`static/study.html`** — Web UI for the study assistant
- **`STUDY_ASSISTANT_README.md`** — This file

### Modified Files
- **`models.py`** — Added `StudyMaterial` and `StudyUser` tables
- **`main.py`** — Integrated study router and static file serving

---

## Quick Start

### 1. Database Setup
The database tables are auto-created on first startup via migrations. No manual SQL needed.

### 2. Environment Variables
Make sure you have `ANTHROPIC_API_KEY` set in your Railway config:
```
ANTHROPIC_API_KEY=sk-ant-your_key_here
```

### 3. Run the App
```bash
python main.py
```

The app will start on `http://localhost:8000`

### 4. Access the Study Assistant
- **Web UI**: http://localhost:8000/study-app
- **API Docs**: http://localhost:8000/docs

---

## API Endpoints

### Study Material Generation

#### Upload & Generate
```bash
POST /study/upload-and-generate
Headers: Authorization: Bearer student@example.com
Body: multipart/form-data
  - file: (image file)
  - material_type: "guide" | "quiz" | "flashcards"

Response: {
  "id": 1,
  "material_type": "guide",
  "title": "Study Guide: Cybersecurity Basics",
  "content": { ... generated material ... },
  "created_at": "2025-07-14T..."
}
```

#### Get My Materials
```bash
GET /study/my-materials
Headers: Authorization: Bearer student@example.com

Response: {
  "count": 5,
  "materials": [
    { "id": 1, "title": "...", "material_type": "guide", ... },
    ...
  ]
}
```

#### Get Specific Material
```bash
GET /study/materials/{material_id}
Headers: Authorization: Bearer student@example.com
```

#### User Stats
```bash
GET /study/user-stats
Headers: Authorization: Bearer student@example.com

Response: {
  "email": "student@example.com",
  "tier": "free",
  "materials_generated_this_month": 3,
  "monthly_limit": 5,
  "remaining": 2
}
```

#### Upgrade to Paid
```bash
POST /study/upgrade
Headers: Authorization: Bearer student@example.com
```

#### Delete Material
```bash
POST /study/delete/{material_id}
Headers: Authorization: Bearer student@example.com
```

---

## Authentication

The Study Assistant uses a **simple Bearer token format** for MVP:

```
Authorization: Bearer student@example.com
```

In production, you'll replace this with:
- JWT tokens
- OAuth 2.0
- Stripe customer verification

---

## Features

### Material Types

#### 1. Study Guide
- Comprehensive summary
- Key concepts list
- Detailed key points with explanations
- Review questions

#### 2. Quiz
- 10 multiple-choice questions
- 4 options per question
- Correct answer + explanation for each

#### 3. Flashcards
- 15-20 flashcards per material
- Front: question/term
- Back: answer/definition

---

## Freemium Model

### Free Tier
- **Cost**: $0/month
- **Limit**: 5 materials/month
- **Features**: All material types

### Paid Tier
- **Cost**: $9.99/month (configured in Stripe)
- **Limit**: Unlimited materials
- **Features**: Everything free tier + future premium features

### Monthly Reset
Limits reset automatically on the 30-day anniversary of first upload.

---

## Database Schema

### StudyUser
```sql
CREATE TABLE study_users (
  id INT PRIMARY KEY,
  email VARCHAR UNIQUE,
  tier VARCHAR DEFAULT 'free',  -- free, paid
  stripe_customer_id VARCHAR,
  stripe_subscription_id VARCHAR,
  created_at DATETIME
)
```

### StudyMaterial
```sql
CREATE TABLE study_materials (
  id INT PRIMARY KEY,
  user_id VARCHAR (email),
  material_type VARCHAR,  -- guide, quiz, flashcards
  source_text TEXT,  -- extracted OCR text
  generated_content JSON,  -- the study material
  title VARCHAR,
  created_at DATETIME,
  topic VARCHAR
)
```

---

## Testing

### Test Endpoints with curl

```bash
# Upload and generate a study guide
curl -X POST http://localhost:8000/study/upload-and-generate \
  -H "Authorization: Bearer student@example.com" \
  -F "file=@textbook_page.jpg" \
  -F "material_type=guide"

# Get my materials
curl -H "Authorization: Bearer student@example.com" \
  http://localhost:8000/study/my-materials

# Get user stats
curl -H "Authorization: Bearer student@example.com" \
  http://localhost:8000/study/user-stats

# Get health
curl http://localhost:8000/study/health
```

---

## Deployment (Railway)

1. Commit and push to GitHub
2. Railway auto-deploys (already configured)
3. Study Assistant will be available at: `https://<your-railway-url>/study-app`

### Production Checklist
- [ ] ANTHROPIC_API_KEY set in Railway
- [ ] Test upload with a real textbook image
- [ ] Verify database migrations run
- [ ] Test all 3 material types
- [ ] Monitor API logs for errors

---

## Revenue Opportunities

### MVP (Current)
- Freemium model: Free (5/mo) → Paid ($9.99/mo)
- Stripe integration for payments (Phase 2)

### Phase 2 (Next 30 days)
- White-label for schools ($500-5K/mo per school)
- Certification prep courses ($99-299 each)
- Corporate training digitization ($2K-10K per project)

### Phase 3 (60+ days)
- Video course generation (Synthesia integration)
- PDF export + branding
- API access for partners

---

## Common Issues & Fixes

### Issue: "API key not configured"
**Fix**: Ensure `ANTHROPIC_API_KEY` is set in environment
```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

### Issue: "Failed to extract text from image"
**Fix**: Use a clearer, well-lit textbook page. Avoid rotated/angled images.

### Issue: "JSON decode error"
**Fix**: Claude API response parsing failed. Check Claude API response format.
- Try regenerating the material
- Check logs for the raw response

### Issue: "Database table not found"
**Fix**: Run migrations on first startup
```bash
# Migrations run automatically, but if needed:
python -c "from database import init_db; import asyncio; asyncio.run(init_db())"
```

---

## Next Steps

### Week 1 (MVP Live)
- Launch to 50 beta users
- Collect NPS feedback
- Monitor API performance
- Track conversion rates

### Week 2-3 (Stripe Integration)
- Set up Stripe webhook
- Implement payment verification
- Enable paid tier

### Week 4 (Scale)
- Analyze CAC (Cost to Acquire Customer)
- Start white-label outreach to schools
- Plan Phase 2 features

---

## Support

For issues or questions:
1. Check logs: `Railway → Deployments → Logs`
2. Test endpoint health: `GET /study/health`
3. Verify database: Check PostgreSQL connection in logs

---

## License & Credits

- **Claude Vision API** for textbook image extraction
- **FastAPI** for backend framework
- **SQLAlchemy** for database ORM
- **HTML/JavaScript** for frontend

Built by Del Stennis | Property Group USA
