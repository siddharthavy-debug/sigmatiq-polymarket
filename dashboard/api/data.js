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

const FILES = ["state", "trades", "traders", "analysis"];

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

  const out = {};
  await Promise.all(
    FILES.map(async (name) => {
      const url =
        `https://raw.githubusercontent.com/${repo}/${branch}/data/${name}.json` +
        `?t=${Date.now()}`;
      try {
        const r = await fetch(url, { cache: "no-store" });
        out[name] = r.ok ? await r.json() : null;
      } catch {
        out[name] = null;
      }
    })
  );

  if (!out.state) {
    return res.status(502).json({
      error:
        "Couldn't read data/state.json from the repository. Check GITHUB_REPO " +
        "and that the bot has committed at least once.",
    });
  }

  res.setHeader("Cache-Control", "no-store");
  return res.status(200).json(out);
}
