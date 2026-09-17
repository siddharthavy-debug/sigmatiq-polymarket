# Dashboard

Static page plus two serverless functions. No framework, no build step.

## Deploy

1. In Vercel, import the repo and set **Root Directory** to `dashboard`
2. Set Application Preset to **Other**
3. Add environment variables:

   | name | value | needed for |
   |---|---|---|
   | `DASHBOARD_PIN` | your PIN | everything |
   | `GITHUB_REPO` | `owner/sigmatiq-polymarket` | reading state |
   | `GITHUB_BRANCH` | `main` | reading state |
   | `GITHUB_TOKEN` | classic token, `repo` scope | **saving settings** |

Without `GITHUB_TOKEN` the dashboard still shows everything, but the Save
button will tell you it can't write.

The PIN is checked server-side, so it never reaches the page source and the
repo can stay public.

## Controls

Saving writes `data/settings.json` to the repo. The bot re-reads it at the
start of every pass, so changes take effect within about 30 seconds.

Only a fixed list of keys is honoured, and every number is clamped to a sane
range on both the server and in the bot. A settings file that has been
tampered with cannot reach anything else.

Switching to Live takes two presses, and even then the bot stays on paper
unless `POLY_PRIVATE_KEY` exists in GitHub Secrets. A dashboard click alone
cannot start spending real money.
