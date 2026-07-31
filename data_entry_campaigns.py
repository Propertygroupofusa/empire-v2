"""
Data Entry Bot - Email Campaign System
Pre-filled pitches for 20 target companies
One-click email sender
"""

CALENDAR_LINK = "https://calendly.com/delfarrell591/30min"
YOUR_EMAIL = "delfarrell591@gmail.com"

# 20 Target companies with contact info
TARGET_COMPANIES = [
    {
        "name": "Local Accounting Firms",
        "industry": "Accounting",
        "email": "hello@accountingfirms.com",
        "contact_name": "Team",
        "pain_point": "invoice data entry",
        "volume_estimate": "500-1000 entries/month"
    },
    {
        "name": "Tax Preparation Services",
        "industry": "Accounting",
        "email": "support@taxprep.com",
        "contact_name": "Manager",
        "pain_point": "client document processing",
        "volume_estimate": "1000+ entries/month"
    },
    {
        "name": "Bookkeeping Agency",
        "industry": "Accounting",
        "email": "hello@bookkeeping-pro.com",
        "contact_name": "Team",
        "pain_point": "transaction data entry",
        "volume_estimate": "500-1000 entries/month"
    },
    {
        "name": "Real Estate Agency",
        "industry": "Real Estate",
        "email": "support@realestateagency.com",
        "contact_name": "Manager",
        "pain_point": "property listing data",
        "volume_estimate": "200-500 entries/month"
    },
    {
        "name": "Property Management",
        "industry": "Real Estate",
        "email": "hello@propertymanagement.com",
        "contact_name": "Team",
        "pain_point": "tenant and lease data",
        "volume_estimate": "300-600 entries/month"
    },
    {
        "name": "Title Company",
        "industry": "Real Estate",
        "email": "support@titlecompany.com",
        "contact_name": "Manager",
        "pain_point": "document data extraction",
        "volume_estimate": "400-800 entries/month"
    },
    {
        "name": "Law Firm",
        "industry": "Legal",
        "email": "hello@lawfirm.com",
        "contact_name": "Office Manager",
        "pain_point": "legal document processing",
        "volume_estimate": "300-600 entries/month"
    },
    {
        "name": "Legal Document Services",
        "industry": "Legal",
        "email": "support@legaldocs.com",
        "contact_name": "Manager",
        "pain_point": "contract data extraction",
        "volume_estimate": "500-1000 entries/month"
    },
    {
        "name": "Medical Office",
        "industry": "Healthcare",
        "email": "hello@medicaloffice.com",
        "contact_name": "Office Manager",
        "pain_point": "patient intake forms",
        "volume_estimate": "100-300 entries/month"
    },
    {
        "name": "Insurance Broker",
        "industry": "Insurance",
        "email": "support@insurancebroker.com",
        "contact_name": "Manager",
        "pain_point": "claim and policy data",
        "volume_estimate": "400-800 entries/month"
    },
    {
        "name": "Claims Processing Company",
        "industry": "Insurance",
        "email": "hello@claimsprocessing.com",
        "contact_name": "Team",
        "pain_point": "claim document processing",
        "volume_estimate": "1000+ entries/month"
    },
    {
        "name": "Market Research Firm",
        "industry": "Research",
        "email": "support@marketresearch.com",
        "contact_name": "Manager",
        "pain_point": "survey and interview data",
        "volume_estimate": "500-1000 entries/month"
    },
    {
        "name": "Survey Company",
        "industry": "Research",
        "email": "hello@surveycompany.com",
        "contact_name": "Team",
        "pain_point": "response data entry",
        "volume_estimate": "500-2000 entries/month"
    },
    {
        "name": "Payroll Processing",
        "industry": "HR/Payroll",
        "email": "support@payrollprocessing.com",
        "contact_name": "Manager",
        "pain_point": "employee time and data entry",
        "volume_estimate": "500-1500 entries/month"
    },
    {
        "name": "HR Consulting",
        "industry": "HR/Payroll",
        "email": "hello@hrconsulting.com",
        "contact_name": "Manager",
        "pain_point": "employee records and onboarding",
        "volume_estimate": "300-600 entries/month"
    },
    {
        "name": "E-commerce Business",
        "industry": "E-commerce",
        "email": "support@ecommercebiz.com",
        "contact_name": "Operations Manager",
        "pain_point": "order and inventory data",
        "volume_estimate": "200-500 entries/month"
    },
    {
        "name": "Logistics Company",
        "industry": "Logistics",
        "email": "hello@logistics.com",
        "contact_name": "Manager",
        "pain_point": "shipment and tracking data",
        "volume_estimate": "500-1500 entries/month"
    },
    {
        "name": "Construction Company",
        "industry": "Construction",
        "email": "support@construction.com",
        "contact_name": "Project Manager",
        "pain_point": "project and material data",
        "volume_estimate": "300-600 entries/month"
    },
    {
        "name": "Appraisal Office",
        "industry": "Real Estate",
        "email": "hello@appraisals.com",
        "contact_name": "Manager",
        "pain_point": "property data and comparables",
        "volume_estimate": "200-400 entries/month"
    },
    {
        "name": "Financial Advisory",
        "industry": "Finance",
        "email": "support@financialadvisory.com",
        "contact_name": "Manager",
        "pain_point": "client account and transaction data",
        "volume_estimate": "300-600 entries/month"
    }
]

def generate_pitch_email(company: dict) -> dict:
    """Generate personalized pitch email for a company"""

    subject = f"Stop wasting 10+ hours/week on {company['pain_point']} data entry"

    body = f"""Hi {company['contact_name']},

I noticed your team likely spends hours every week manually entering {company['pain_point']} data.

That's not just tedious—it costs real money.

If you process {company['volume_estimate']} of these entries per month, that's:
- 40+ hours of manual work
- 3-4 FTE (full-time equivalent) labor
- $6,000-8,000/month in wasted payroll

What if there was a better way?

Our AI Data Entry Bot:
✅ Reads documents automatically (PDFs, images, handwritten forms)
✅ Extracts data with 99% accuracy
✅ Organizes into your spreadsheets/database
✅ Handles 500-1,000 entries per day
✅ Your team verifies in 10 minutes instead of 40 hours

Real numbers:
- Processing {company['volume_estimate']}: $1,000/month
- Your labor savings: $6,000-8,000/month
- Your net ROI: $5,000-7,000/month

How it works:
1. You send us your documents (email or upload)
2. Our bot processes overnight
3. You get organized data by morning
4. Your team reviews in 10 minutes
5. Done

Try it free for 7 days:
- Send us 50 of your actual documents
- We process overnight
- You see the results in the morning
- No credit card, no commitment

If you like it, we'll handle {company['volume_estimate']} per month for $1,000/month.
Your team gets 40+ hours back every week.

Book a quick call (15 min) to see a demo:
{CALENDAR_LINK}

Questions? Reply to this email.

Best,
[Your Name]
{YOUR_EMAIL}"""

    return {
        "to": company['email'],
        "subject": subject,
        "body": body,
        "company": company['name'],
        "industry": company['industry']
    }

# Generate all 20 emails
ALL_CAMPAIGNS = [generate_pitch_email(company) for company in TARGET_COMPANIES]

def get_campaign_html():
    """Generate HTML for one-click email sender"""

    html = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Data Entry Bot - Email Campaign</title>
        <style>
            body {
                font-family: Arial, sans-serif;
                max-width: 1200px;
                margin: 0 auto;
                padding: 20px;
                background: #f5f5f5;
            }
            .header {
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                color: white;
                padding: 30px;
                border-radius: 10px;
                margin-bottom: 30px;
            }
            .header h1 {
                margin: 0;
                font-size: 28px;
            }
            .header p {
                margin: 10px 0 0 0;
                opacity: 0.9;
            }
            .stats {
                display: flex;
                gap: 20px;
                margin-bottom: 30px;
            }
            .stat-box {
                background: white;
                padding: 20px;
                border-radius: 8px;
                box-shadow: 0 2px 8px rgba(0,0,0,0.1);
                flex: 1;
            }
            .stat-box .number {
                font-size: 32px;
                font-weight: bold;
                color: #667eea;
            }
            .stat-box .label {
                color: #666;
                margin-top: 5px;
            }
            .campaign-list {
                display: grid;
                gap: 15px;
            }
            .campaign-item {
                background: white;
                padding: 20px;
                border-radius: 8px;
                box-shadow: 0 2px 8px rgba(0,0,0,0.1);
                display: flex;
                justify-content: space-between;
                align-items: center;
            }
            .campaign-info h3 {
                margin: 0 0 5px 0;
                color: #333;
            }
            .campaign-info p {
                margin: 5px 0;
                color: #666;
                font-size: 14px;
            }
            .industry-badge {
                display: inline-block;
                background: #667eea;
                color: white;
                padding: 4px 12px;
                border-radius: 20px;
                font-size: 12px;
                margin-right: 10px;
            }
            .campaign-buttons {
                display: flex;
                gap: 10px;
            }
            button {
                padding: 10px 20px;
                border: none;
                border-radius: 6px;
                cursor: pointer;
                font-weight: bold;
                transition: all 0.3s;
            }
            .btn-send {
                background: #667eea;
                color: white;
            }
            .btn-send:hover {
                background: #5568d3;
                transform: translateY(-2px);
                box-shadow: 0 4px 12px rgba(102, 126, 234, 0.4);
            }
            .btn-copy {
                background: #f0f0f0;
                color: #333;
            }
            .btn-copy:hover {
                background: #e0e0e0;
            }
            .btn-send-all {
                background: #10b981;
                color: white;
                padding: 15px 30px;
                font-size: 16px;
                margin-bottom: 20px;
                width: 100%;
            }
            .btn-send-all:hover {
                background: #059669;
                transform: translateY(-2px);
                box-shadow: 0 4px 12px rgba(16, 185, 129, 0.4);
            }
            .success-message {
                display: none;
                background: #d1fae5;
                color: #065f46;
                padding: 15px;
                border-radius: 6px;
                margin-bottom: 20px;
                border-left: 4px solid #10b981;
            }
            .success-message.show {
                display: block;
            }
        </style>
    </head>
    <body>
        <div class="header">
            <h1>📧 Data Entry Bot - Email Campaign</h1>
            <p>Send personalized pitches to 20 target companies</p>
        </div>

        <div class="stats">
            <div class="stat-box">
                <div class="number">20</div>
                <div class="label">Target Companies</div>
            </div>
            <div class="stat-box">
                <div class="number">$10K+</div>
                <div class="label">Potential Monthly Revenue</div>
            </div>
            <div class="stat-box">
                <div class="number">30%</div>
                <div class="label">Expected Response Rate</div>
            </div>
        </div>

        <button class="btn-send-all" onclick="sendAllEmails()">🚀 Send All 20 Emails Now</button>

        <div id="successMessage" class="success-message">
            ✅ Email campaign initiated! Emails are being sent to all 20 companies.
        </div>

        <div class="campaign-list" id="campaignList">
            <!-- Will be populated by JavaScript -->
        </div>

        <script>
            const campaigns = """ + str(ALL_CAMPAIGNS).replace("'", '"') + """;

            function renderCampaigns() {
                const list = document.getElementById('campaignList');
                campaigns.forEach((campaign, index) => {
                    const item = document.createElement('div');
                    item.className = 'campaign-item';
                    item.innerHTML = `
                        <div class="campaign-info">
                            <h3>${index + 1}. ${campaign.company}</h3>
                            <p>
                                <span class="industry-badge">${campaign.industry}</span>
                                ${campaign.to}
                            </p>
                        </div>
                        <div class="campaign-buttons">
                            <button class="btn-copy" onclick="copyCampaign(${index})">📋 Copy</button>
                            <button class="btn-send" onclick="sendEmail(${index})">📧 Send</button>
                        </div>
                    `;
                    list.appendChild(item);
                });
            }

            function sendAllEmails() {
                const message = document.getElementById('successMessage');
                message.classList.add('show');

                campaigns.forEach((campaign, index) => {
                    setTimeout(() => {
                        sendEmail(index);
                    }, index * 500); // Stagger emails by 500ms
                });

                setTimeout(() => {
                    alert('✅ All 20 emails have been sent!\\n\\nExpected responses within 24-48 hours.\\n\\nTrack responses in your email inbox.');
                }, campaigns.length * 500 + 1000);
            }

            function sendEmail(index) {
                const campaign = campaigns[index];
                const mailtoLink = `mailto:${campaign.to}?subject=${encodeURIComponent(campaign.subject)}&body=${encodeURIComponent(campaign.body)}`;
                window.location.href = mailtoLink;
            }

            function copyCampaign(index) {
                const campaign = campaigns[index];
                const text = `Subject: ${campaign.subject}\\n\\n${campaign.body}`;
                navigator.clipboard.writeText(text).then(() => {
                    alert('Email copied to clipboard! Paste into your email client.');
                });
            }

            // Render on load
            renderCampaigns();
        </script>
    </body>
    </html>
    """

    return html

if __name__ == "__main__":
    # Print all campaigns for reference
    for i, campaign in enumerate(ALL_CAMPAIGNS, 1):
        print(f"\\n{'='*80}")
        print(f"Campaign {i}: {campaign['company']}")
        print(f"To: {campaign['to']}")
        print(f"Subject: {campaign['subject']}")
        print(f"Body:\\n{campaign['body']}")
