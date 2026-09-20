/**
 * /api/data — PIN-gated read of the bot's state.
 *
 * The PIN lives in a Vercel environment variable, never in the repo. The
 * browser sends it, this function checks it server-side, and only then
 * fetches the bot's JSON from GitHub.
 *
 * Environment variables to set in Vercel:
 *   DASHBOARD_PIN   your PIN
 *   GITHUB_REPO     e.g. siddharthavy-debug/sigmatiq-polymarket
 *   GITHUB_BRANCH   defaults to main
 */

const FILES = [
  ["crypto_state", "state"],
  ["crypto_trades", "trades"],
  ["crypto_traders", "traders"],
];

function timingSafeEqual(a, b) {
  if (typeof a !== "string" || typeof b !== "string") return false;
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

export default async function handler(req, res) {
  if (req.method !== "POST") {
    res.setHeader("Allow", "POST");
    return res.status(405).json({ error: "POST only" });
  }

  const expected = process.env.DASHBOARD_PIN;
  if (!expected) {
    return res.status(500).json({
      error: "DASHBOARD_PIN is not set in this project's environment variables.",
    });
  }

  const supplied = (req.body && req.body.pin) || "";
  if (!timingSafeEqual(String(supplied), String(expected))) {
    return res.status(401).json({ error: "Wrong PIN." });
  }

  const repo = process.env.GITHUB_REPO;
  const branch = process.env.GITHUB_BRANCH || "main";
  if (!repo) {
    return res.status(500).json({
      error: "GITHUB_REPO is not set. Use the form owner/repository.",
    });
  }

  // ?engine=crypto (or {engine:"crypto"}) serves the crypto bot's files under
  // the same keys, so one page can render either engine.
  const wanted = FILES;

  const out = {};
  await Promise.all(
    wanted.map(async ([file, name]) => {
      const url =
        `https://raw.githubusercontent.com/${repo}/${branch}/data/${file}.json` +
        `?t=${Date.now()}`;
      try {
        const r = await fetch(url, { cache: "no-store" });
        out[name] = r.ok ? await r.json() : null;
      } catch {
        out[name] = null;
      }
    })
  );

  // A bot that hasn't committed yet is normal, not an error — say so and let
  // the page retry rather than showing a scary failure.
  if (!out.state) {
    res.setHeader("Cache-Control", "no-store");
    return res.status(200).json({
      waiting: true,
      message:
        engine === "crypto"
          ? "The crypto bot hasn't written anything yet. It will appear once it runs."
          : "The bot hasn't written state yet. It will appear after the next run.",
    });
  }

  res.setHeader("Cache-Control", "no-store");
  return res.status(200).json(out);
}
