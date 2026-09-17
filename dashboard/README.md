# Dashboard

Static page plus one serverless function. No framework, no build step.

## Deploy

1. In Vercel, import the repo and set **Root Directory** to `dashboard`
2. Add three environment variables:

   | name | value |
   |---|---|
   | `DASHBOARD_PIN` | your PIN |
   | `GITHUB_REPO` | `owner/sigmatiq-polymarket` |
   | `GITHUB_BRANCH` | `main` |

3. Deploy

The PIN is checked server-side in `api/data.js` and never appears in the page
source, which is why the repo can stay public.

## Local check

```bash
npm i -g vercel
DASHBOARD_PIN=453536 GITHUB_REPO=owner/repo vercel dev
```
