# Railway Deployment Setup

## Finding Your Railway Service URL

Your landing pages need to know where your API services are hosted. Railway automatically assigns a public domain to your deployment.

### How to find your Railway service URL:

1. **Go to your Railway project**: [railway.app/dashboard](https://railway.app/dashboard)
2. **Find the "empire-v2" service** in your projects
3. **Click on the service** and look for the "Deployment" section
4. **You should see a URL** that looks like one of these:
   - `empire-v2-production.up.railway.app` (most common)
   - `empire-v2-prod.up.railway.app`
   - `empire-v2.up.railway.app`
   - Or a custom domain if you've set one up

### If you don't see the URL:
1. Click on the "Deployments" tab
2. Look for the most recent successful deployment
3. Click on it and find the "URL" or "Domain" field
4. The domain should be visible there

## Updating Your Landing Pages

Once you have your Railway service URL, update the `API_BASE_URL` in each landing page:

### For local development:
- signals_landing.html: Line with `const API_BASE_URL`
- api_landing.html: Line with `const API_BASE_URL`
- dfy_landing.html: Line with `const API_BASE_URL`
- partners_landing.html: Line with `const API_BASE_URL`

Change from:
```javascript
const API_BASE_URL = 'https://empire-v2-production.up.railway.app';
```

To:
```javascript
const API_BASE_URL = 'https://YOUR-ACTUAL-RAILWAY-DOMAIN';
```

### For GitHub Pages deployment:
The same files on the `gh-pages` branch control the live landing pages at:
- https://propertygroupofusa.github.io/signals_landing.html
- https://propertygroupofusa.github.io/api_landing.html
- https://propertygroupofusa.github.io/dfy_landing.html
- https://propertygroupofusa.github.io/partners_landing.html

## Deployment Architecture

Your system now uses an **API Gateway** that:
- Runs on port 10000 (the main Railway public port)
- Routes all landing page requests to the correct microservice
- Provides CORS support for cross-origin requests

### API Routes:
- `/signals/*` → Trading Signals API (port 8001)
- `/api/*` → Content Generation API (port 8002)
- `/dfy/*` → Done-For-You Service (port 8003)
- `/partners/*` → White-Label Platform (port 8004)

## Testing the Connection

After updating the API_BASE_URL:

1. **Visit a landing page**: https://propertygroupofusa.github.io/signals_landing.html
2. **Open browser console**: F12 or right-click → Inspect → Console
3. **Try filling out the form**
4. **Check if the Stripe checkout redirects**
5. **Monitor the console for any errors**

## Troubleshooting

### "Connection error" on landing page:
- Check that the API_BASE_URL is correct (must include `https://`)
- Make sure the Railway service is running and healthy
- Check Railway logs for errors

### 404 errors:
- Verify the path is correct (e.g., `/signals/subscribe` not `/subscribe`)
- Check that all microservices are running in Railway

### CORS errors:
- All API services now have CORS middleware enabled
- Should allow requests from anywhere (`*`)

## Next Steps

1. Find your Railway service URL
2. Update API_BASE_URL in all 4 landing pages
3. Push changes to gh-pages branch for live deployment
4. Restart the Railway service
5. Test a complete payment flow
6. Monitor for first customer signups

Good luck! 🚀
