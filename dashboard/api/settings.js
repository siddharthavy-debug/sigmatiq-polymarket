/**
 * /api/settings — read and write the bot's runtime settings.
 *
 * The bot runs on GitHub Actions, so the only way a web page can change its
 * behaviour is to change a file in the repo. This writes data/settings.json
 * through the GitHub API; the bot re-reads it at the start of every pass.
 *
 * Environment variables (Vercel project settings):
 *   DASHBOARD_PIN   required for every call
 *   GITHUB_REPO     owner/repository
 *   GITHUB_BRANCH   defaults to main
 *   GITHUB_TOKEN    a classic token with 'repo' scope — WRITING NEEDS THIS
 */

const PATH = "data/settings.json";

// Everything the dashboard may change, with the bounds enforced again here.
// The bot validates too; doing it on both sides means a bad value can't be
// written in the first place.
const FIELDS = {
  mode:             { type: "enum", values: ["paper", "live"] },
  paused:           { type: "bool" },
  allocation:       { type: "num", min: 5, max: 100000 },
  total_balance:    { type: "num", min: 5, max: 10000000 },
  trade_size:       { type: "num", min: 1, max: 10000 },
  max_position_pct: { type: "num", min: 0.005, max: 1 },
  max_market_days:  { type: "num", min: 0.04, max: 400 },
  min_market_hours: { type: "num", min: 0, max: 240 },
  max_buy_price:    { type: "num", min: 0.05, max: 0.99 },
  min_buy_price:    { type: "num", min: 0.01, max: 0.95 },
  top_n_traders:    { type: "int", min: 1, max: 50 },
  max_per_trader:   { type: "int", min: 1, max: 50 },
};

function pinOk(supplied) {
  const expected = process.env.DASHBOARD_PIN || "";
  const a = String(supplied || "");
  if (!expected || a.length !== expected.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ expected.charCodeAt(i);
  return diff === 0;
}

function clean(input) {
  const out = {};
  for (const [key, spec] of Object.entries(FIELDS)) {
    if (!(key in input)) continue;
    let v = input[key];
    if (spec.type === "bool") {
      out[key] = Boolean(v);
    } else if (spec.type === "enum") {
      if (spec.values.includes(v)) out[key] = v;
    } else {
      v = Number(v);
      if (!Number.isFinite(v)) continue;
      v = Math.min(spec.max, Math.max(spec.min, v));
      out[key] = spec.type === "int" ? Math.round(v) : v;
    }
  }
  return out;
}

async function gh(path, options = {}) {
  const repo = process.env.GITHUB_REPO;
  const token = process.env.GITHUB_TOKEN;
  return fetch(`https://api.github.com/repos/${repo}/${path}`, {
    ...options,
    headers: {
      Authorization: `Bearer ${token}`,
      Accept: "application/vnd.github+json",
      "Content-Type": "application/json",
      ...(options.headers || {}),
    },
  });
}

export default async function handler(req, res) {
  if (!pinOk(req.body && req.body.pin)) {
    return res.status(401).json({ error: "Wrong PIN." });
  }

  const repo = process.env.GITHUB_REPO;
  const branch = process.env.GITHUB_BRANCH || "main";
  if (!repo) return res.status(500).json({ error: "GITHUB_REPO is not set." });

  // ---- read
  if (req.method === "GET" || !req.body.settings) {
    try {
      const r = await fetch(
        `https://raw.githubusercontent.com/${repo}/${branch}/${PATH}?t=${Date.now()}`,
        { cache: "no-store" }
      );
      return res.status(200).json({ settings: r.ok ? await r.json() : {} });
    } catch {
      return res.status(200).json({ settings: {} });
    }
  }

  // ---- write
  if (!process.env.GITHUB_TOKEN) {
    return res.status(500).json({
      error:
        "GITHUB_TOKEN is not set in this Vercel project, so settings can't be " +
        "saved. Add a classic GitHub token with 'repo' scope.",
    });
  }

  const settings = clean(req.body.settings);
  if (!Object.keys(settings).length) {
    return res.status(400).json({ error: "Nothing valid to save." });
  }

  try {
    // need the current file's sha to update it
    let sha;
    const head = await gh(`contents/${PATH}?ref=${branch}`);
    if (head.ok) sha = (await head.json()).sha;

    const body = {
      message: "settings from dashboard",
      content: Buffer.from(JSON.stringify(settings, null, 2)).toString("base64"),
      branch,
      ...(sha ? { sha } : {}),
    };

    const put = await gh(`contents/${PATH}`, {
      method: "PUT",
      body: JSON.stringify(body),
    });

    if (!put.ok) {
      const detail = await put.text();
      return res.status(502).json({
        error: `GitHub refused the write (${put.status}). ${detail.slice(0, 200)}`,
      });
    }
    return res.status(200).json({ saved: settings });
  } catch (e) {
    return res.status(500).json({ error: String(e).slice(0, 300) });
  }
}
