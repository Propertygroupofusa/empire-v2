#!/usr/bin/env python3
"""
SaaS Cold Email Campaign
Automated outreach to SaaS companies for video production
"""

import csv

COLD_EMAIL_SUBJECT = "Quick question: Does {company} use product videos?"

COLD_EMAIL_BODY_TEMPLATE = """Hi {first_name},

I noticed {company} is doing great work with {product_type}.

Quick question: Do you use explainer videos or demo videos to onboard new users?

Most SaaS companies see:
• 25% increase in conversion rates with explainer videos
• 340% engagement boost on landing pages
• 50% reduction in support tickets (video tutorials)

We just built a flat-fee video production service:
• $1,500-2,000 per explainer/demo video
• 3-5 day turnaround
• Professional avatars, custom scripts, HD quality
• Unlimited revisions

Perfect for:
- Product explainer videos
- User onboarding videos
- Feature overview videos
- Tutorial/documentation videos
- Customer testimonial videos

Your first video: Let's discuss what {company} needs.

https://empire-v2-production.up.railway.app/quote

Are you interested in exploring this?

{sender_name}
Property Group of USA
https://empire-v2-production.up.railway.app
"""

def generate_saas_emails():
    """Generate personalized emails for each SaaS company"""

    # Map companies to their product type
    company_products = {
        "Slack": "communication platform",
        "Zapier": "automation platform",
        "Stripe": "payment processing",
        "Figma": "design tool",
        "Notion": "productivity tool",
        "Intercom": "customer platform",
        "HubSpot": "CRM",
        "Salesforce": "enterprise CRM",
        "Asana": "project management",
        "Monday": "work OS",
        "Calendly": "scheduling tool",
        "Webflow": "no-code platform",
        "Framer": "design tool",
        "Typeform": "form builder",
        "Segment": "data platform",
        "Amplitude": "analytics platform",
        "Mixpanel": "analytics platform",
        "Firebase": "backend platform",
        "AWS": "cloud platform",
        "Azure": "cloud platform",
        "LaunchDarkly": "feature management",
        "Twilio": "communications API",
        "Sendgrid": "email service",
        "Shopify": "e-commerce platform"
    }

    print("=" * 80)
    print("📧 SAAS COLD EMAIL CAMPAIGN")
    print("=" * 80)

    # Load prospects
    prospects = []
    try:
        with open("saas_prospects.csv", "r") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get("Email Guess"):
                    prospects.append(row)
    except FileNotFoundError:
        print("❌ saas_prospects.csv not found")
        return

    print(f"\n📊 Campaign Overview:")
    print(f"   Target: {len(prospects)} SaaS companies")
    print(f"   Expected close rate: 8-12%")
    print(f"   Expected closed deals: {int(len(prospects) * 0.10)} companies")
    print(f"   Revenue per deal: $1,500-2,000")
    print(f"   Total potential: ${int(len(prospects) * 0.10 * 1750):,}")

    print(f"\n📧 Email Details:")
    print(f"   Subject: Changes based on company")
    print(f"   Body: Personalized for each product type")
    print(f"   CTA: Quote form link")
    print(f"   Follow-up: Recommended after 3-5 days")

    print(f"\n📋 First 5 Sample Emails:\n")

    for i, prospect in enumerate(prospects[:5]):
        company = prospect["Company Name"]
        contact = prospect["Contact Title"].split()[0]  # Get first name approximation
        email = prospect["Email Guess"]
        product = company_products.get(company, "their platform")

        subject = COLD_EMAIL_SUBJECT.format(company=company)
        body = COLD_EMAIL_BODY_TEMPLATE.format(
            first_name=contact,
            company=company,
            product_type=product,
            sender_name="Sales Team"
        )

        print(f"{'='*80}")
        print(f"TO: {email}")
        print(f"SUBJECT: {subject}")
        print(f"{'-'*80}")
        print(body)
        print()

    print("=" * 80)
    print("✅ NEXT STEPS TO LAUNCH CAMPAIGN")
    print("=" * 80)
    print("\n1. Review emails above to ensure personalization")
    print("2. Set up bulk email tool: Outreach.io, Lemlist, or similar")
    print("3. Load saas_prospects.csv into email tool")
    print("4. Set sending rate: 10-15 emails/day (avoid spam filters)")
    print("5. Set up auto-follow-up after 3-5 days if no response")
    print("6. Track opens, clicks, and replies")
    print("7. Close rate tracking: Expected 8-12% → ~4-6 deals/month")
    print("\n💡 Automation tools:")
    print("   - Outreach.io (enterprise, most powerful)")
    print("   - Lemlist (affordable, creative)")
    print("   - HubSpot (free plan available)")
    print("   - Apollo.io (real-time email finding)")
    print("\n📊 Expected Results (30 days):")
    print(f"   - Emails sent: {len(prospects)}")
    print(f"   - Opens: ~{int(len(prospects) * 0.35)} (35% typical)")
    print(f"   - Clicks: ~{int(len(prospects) * 0.15)} (15% typical)")
    print(f"   - Replies: ~{int(len(prospects) * 0.08)} (8% typical)")
    print(f"   - Qualified deals: ~{int(len(prospects) * 0.05)} (5%)")
    print(f"   - Closed deals: ~{int(len(prospects) * 0.10)} (10% close rate)")
    print(f"   - Revenue: ${int(len(prospects) * 0.10 * 1750):,}")

if __name__ == "__main__":
    generate_saas_emails()
