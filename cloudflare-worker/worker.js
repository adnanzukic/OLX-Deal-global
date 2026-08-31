/**
 * OLX Deal Watcher - Cloudflare Worker (backend)
 *
 * Ovo je "server" između web obrasca (docs/index.html) i GitHub repozitorija.
 * Drži GitHub token BEZBJEDNO (kao Worker secret, nikad vidljiv u browseru),
 * i prima jednostavne zahtjeve sa obrasca da napravi/ažurira
 * users/<korisnik>/criteria.json u repozitoriju.
 *
 * Potrebni Worker "secrets" (podešavaju se u Cloudflare dashboardu, ne ovdje):
 *   GITHUB_TOKEN      - GitHub Personal Access Token sa 'repo' dozvolom
 *   TELEGRAM_BOT_TOKEN - isti bot token koji koristi i scraper.py (za "pronađi moj chat ID" pomoćnu funkciju)
 */

const OWNER = "adnanzukic";
const REPO = "OLX-Deal-global";
const BRANCH = "main";

function corsHeaders() {
  return {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
  };
}

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8", ...corsHeaders() },
  });
}

function slugify(name) {
  return (name || "")
    .toString()
    .toLowerCase()
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "") // skini dijakritike (č,ć,š,ž,đ -> c,c,s,z,dj-ish)
    .replace(/đ/g, "dj")
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 40);
}

function utf8ToBase64(str) {
  return btoa(unescape(encodeURIComponent(str)));
}

function base64ToUtf8(str) {
  return decodeURIComponent(escape(atob(str.replace(/\n/g, ""))));
}

async function githubRequest(path, options, env) {
  const url = `https://api.github.com/repos/${OWNER}/${REPO}/${path}`;
  return fetch(url, {
    ...options,
    headers: {
      Authorization: `Bearer ${env.GITHUB_TOKEN}`,
      Accept: "application/vnd.github+json",
      "User-Agent": "olx-deal-watcher-worker",
      ...(options && options.headers ? options.headers : {}),
    },
  });
}

async function getFile(path, env) {
  const resp = await githubRequest(`contents/${path}?ref=${BRANCH}`, {}, env);
  if (resp.status === 404) return null;
  if (!resp.ok) throw new Error(`GitHub GET (${path}) greška: ${resp.status}`);
  const data = await resp.json();
  return { content: base64ToUtf8(data.content), sha: data.sha };
}

async function putFile(path, contentObj, message, env) {
  const existing = await getFile(path, env);
  const body = {
    message,
    content: utf8ToBase64(JSON.stringify(contentObj, null, 2)),
    branch: BRANCH,
  };
  if (existing) body.sha = existing.sha;

  const resp = await githubRequest(
    `contents/${path}`,
    { method: "PUT", body: JSON.stringify(body) },
    env
  );
  if (!resp.ok) {
    const errText = await resp.text();
    throw new Error(`GitHub PUT (${path}) greška: ${resp.status} ${errText}`);
  }
  return resp.json();
}

function parseCsvList(value) {
  return (value || "")
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
}

async function handleGetConfig(url, env) {
  const userSlug = slugify(url.searchParams.get("user"));
  if (!userSlug) return json({ error: "Nedostaje 'user' parametar." }, 400);

  const file = await getFile(`users/${userSlug}/criteria.json`, env);
  if (!file) return json({ exists: false });
  return json({ exists: true, criteria: JSON.parse(file.content) });
}

async function handlePostConfig(request, env) {
  const body = await request.json();
  const userSlug = slugify(body.user);

  if (!userSlug) return json({ error: "Unesi svoje ime." }, 400);
  if (!body.search_url || !/^https:\/\/olx\.ba\//.test(body.search_url)) {
    return json({ error: "Link mora biti pravi OLX.ba link pretrage (počinje sa https://olx.ba/)." }, 400);
  }
  if (!body.telegram_chat_id) {
    return json({ error: "Nedostaje Telegram Chat ID." }, 400);
  }

  const criteria = {
    display_name: body.display_name || body.user,
    search_url: body.search_url,
    max_price_km: body.max_price_km ? Number(body.max_price_km) : null,
    required_keywords_any: parseCsvList(body.required_any),
    required_keywords_all: parseCsvList(body.required_all),
    excluded_keywords: parseCsvList(body.excluded),
    min_speed_mhz: body.min_speed_mhz ? Number(body.min_speed_mhz) : null,
    single_stick_max_price_km: body.single_stick_max_price_km
      ? Number(body.single_stick_max_price_km)
      : null,
    use_vision_fallback: body.use_vision_fallback !== false,
    telegram_chat_id: String(body.telegram_chat_id),
  };

  await putFile(
    `users/${userSlug}/criteria.json`,
    criteria,
    `Postavke sačuvane preko obrasca: ${userSlug}`,
    env
  );

  const existingState = await getFile(`users/${userSlug}/seen_listings.json`, env);
  if (!existingState) {
    await putFile(`users/${userSlug}/seen_listings.json`, {}, `Nov korisnik: ${userSlug}`, env);
  }

  return json({ ok: true, user: userSlug });
}

async function handleFindChatId(env) {
  const resp = await fetch(
    `https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/getUpdates?limit=5`
  );
  if (!resp.ok) return json({ found: false, error: "Telegram API nedostupan." }, 502);

  const data = await resp.json();
  const messages = (data.result || []).slice().reverse();
  const found = messages.find((u) => u.message && u.message.chat);

  if (!found) return json({ found: false });

  const chat = found.message.chat;
  return json({ found: true, chat_id: chat.id, first_name: chat.first_name || "" });
}

export default {
  async fetch(request, env) {
    if (request.method === "OPTIONS") {
      return new Response(null, { headers: corsHeaders() });
    }

    const url = new URL(request.url);

    try {
      if (url.pathname === "/api/config" && request.method === "GET") {
        return await handleGetConfig(url, env);
      }
      if (url.pathname === "/api/config" && request.method === "POST") {
        return await handlePostConfig(request, env);
      }
      if (url.pathname === "/api/find-chatid" && request.method === "GET") {
        return await handleFindChatId(env);
      }
      return json({ error: "Nepoznata ruta." }, 404);
    } catch (err) {
      return json({ error: String((err && err.message) || err) }, 500);
    }
  },
};
