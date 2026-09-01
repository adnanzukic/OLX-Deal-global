/**
 * OLX Deal Watcher - Cloudflare Worker (backend)
 *
 * Ovo je "server" između web obrasca (docs/index.html) i GitHub repozitorija.
 * Drži GitHub token BEZBJEDNO (kao Worker secret, nikad vidljiv u browseru),
 * i prima zahtjeve da upravljam multi-watch arhitekturom.
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
    "Access-Control-Allow-Methods": "GET,POST,OPTIONS,PUT,DELETE,PATCH",
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
      Authorization: `token ${env.GITHUB_TOKEN}`,
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

async function deleteFile(path, message, env) {
  const existing = await getFile(path, env);
  if (!existing) return null;

  const body = {
    message,
    sha: existing.sha,
    branch: BRANCH,
  };

  const resp = await githubRequest(
    `contents/${path}`,
    { method: "DELETE", body: JSON.stringify(body) },
    env
  );
  if (!resp.ok) {
    const errText = await resp.text();
    throw new Error(`GitHub DELETE (${path}) greška: ${resp.status} ${errText}`);
  }
  return resp.json();
}

function parseCsvList(value) {
  return (value || "")
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
}

// ---------------------------------------------------------------------------
// Multi-watch API
// ---------------------------------------------------------------------------

async function handleGetWatches(url, env) {
  const userSlug = slugify(url.searchParams.get("user"));
  if (!userSlug) return json({ error: "Nedostaje 'user' parametar." }, 400);

  try {
    // Pronađi sve watchese - čitaj users/<user>/watches/ direktorij
    const watchesPath = `users/${userSlug}/watches`;
    const resp = await githubRequest(`contents/${watchesPath}?ref=${BRANCH}`, {}, env);
    
    if (resp.status === 404) {
      // Nema watches direktorija - vjerovatno stara struktura
      return json({ watches: [] });
    }
    
    if (!resp.ok) throw new Error(`Ne mogu pročitati watches direktorij: ${resp.status}`);
    
    const items = await resp.json();
    if (!Array.isArray(items)) {
      return json({ watches: [] });
    }
    
    const watches = [];
    for (const item of items) {
      if (item.type === "dir") {
        const watchId = item.name;
        const criteriaFile = await getFile(`${watchesPath}/${watchId}/criteria.json`, env);
        if (criteriaFile) {
          const criteria = JSON.parse(criteriaFile.content);
          watches.push({
            id: watchId,
            display_name: criteria.display_name || watchId,
            active: criteria.active !== false,
            max_price_km: criteria.max_price_km,
            search_url: criteria.search_url,
          });
        }
      }
    }
    
    return json({ watches });
  } catch (e) {
    return json({ error: String(e.message) }, 500);
  }
}

async function handleGetWatch(url, env) {
  const userSlug = slugify(url.searchParams.get("user"));
  const watchId = url.searchParams.get("watch_id");
  
  if (!userSlug || !watchId) {
    return json({ error: "Nedostaju 'user' ili 'watch_id' parametri." }, 400);
  }

  try {
    const file = await getFile(`users/${userSlug}/watches/${watchId}/criteria.json`, env);
    if (!file) return json({ exists: false });
    return json({ exists: true, watch: JSON.parse(file.content) });
  } catch (e) {
    return json({ error: String(e.message) }, 500);
  }
}

async function handlePostWatch(request, env) {
  const body = await request.json();
  const userSlug = slugify(body.user);
  const watchName = body.display_name || body.watch_name || "Novi watch";
  const watchId = slugify(watchName);
  
  if (!userSlug) return json({ error: "Unesi svoje ime." }, 400);
  if (!watchId) return json({ error: "Naziv watcha je obavezan." }, 400);
  if (!body.search_url || !/^https:\/\/olx\.ba\//.test(body.search_url)) {
    return json({ error: "Link mora biti pravi OLX.ba link pretrage (počinje sa https://olx.ba/)." }, 400);
  }
  if (!body.telegram_chat_id) {
    return json({ error: "Nedostaje Telegram Chat ID." }, 400);
  }

  try {
    const criteria = {
      display_name: watchName,
      search_url: body.search_url,
      active: body.active !== false,
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

    const criteriaPath = `users/${userSlug}/watches/${watchId}/criteria.json`;
    await putFile(
      criteriaPath,
      criteria,
      `[${userSlug}] Nov watch: ${watchName}`,
      env
    );

    // Kreiraj prazan seen_listings.json ako ne postoji
    const statePath = `users/${userSlug}/watches/${watchId}/seen_listings.json`;
    const existingState = await getFile(statePath, env);
    if (!existingState) {
      await putFile(statePath, {}, `[${userSlug}] Nov watch seen_listings: ${watchName}`, env);
    }

    return json({ ok: true, user: userSlug, watch_id: watchId });
  } catch (e) {
    return json({ error: String(e.message) }, 500);
  }
}

async function handlePutWatch(url, request, env) {
  const body = await request.json();
  const userSlug = slugify(body.user);
  const watchId = url.searchParams.get("watch_id");
  
  if (!userSlug || !watchId) {
    return json({ error: "Nedostaju 'user' ili 'watch_id' parametri." }, 400);
  }

  try {
    const file = await getFile(`users/${userSlug}/watches/${watchId}/criteria.json`, env);
    if (!file) return json({ error: "Watch nije pronađen." }, 404);
    
    const criteria = JSON.parse(file.content);
    
    // Ažuriraj polja koja su poslana
    if (body.display_name) criteria.display_name = body.display_name;
    if (body.search_url) criteria.search_url = body.search_url;
    if ("active" in body) criteria.active = body.active;
    if (body.max_price_km !== undefined) criteria.max_price_km = body.max_price_km ? Number(body.max_price_km) : null;
    if (body.required_any !== undefined) criteria.required_keywords_any = parseCsvList(body.required_any);
    if (body.required_all !== undefined) criteria.required_keywords_all = parseCsvList(body.required_all);
    if (body.excluded !== undefined) criteria.excluded_keywords = parseCsvList(body.excluded);
    if (body.min_speed_mhz !== undefined) criteria.min_speed_mhz = body.min_speed_mhz ? Number(body.min_speed_mhz) : null;
    if (body.single_stick_max_price_km !== undefined) criteria.single_stick_max_price_km = body.single_stick_max_price_km ? Number(body.single_stick_max_price_km) : null;
    if ("use_vision_fallback" in body) criteria.use_vision_fallback = body.use_vision_fallback;
    if (body.telegram_chat_id) criteria.telegram_chat_id = String(body.telegram_chat_id);

    await putFile(
      `users/${userSlug}/watches/${watchId}/criteria.json`,
      criteria,
      `[${userSlug}] Ažuriranje watcha: ${watchId}`,
      env
    );

    return json({ ok: true });
  } catch (e) {
    return json({ error: String(e.message) }, 500);
  }
}

async function handleDeleteWatch(url, request, env) {
  const body = await request.json();
  const userSlug = slugify(body.user || url.searchParams.get("user"));
  const watchId = url.searchParams.get("watch_id");
  
  if (!userSlug || !watchId) {
    return json({ error: "Nedostaju 'user' ili 'watch_id' parametri." }, 400);
  }

  try {
    // Obriši criteria.json
    await deleteFile(
      `users/${userSlug}/watches/${watchId}/criteria.json`,
      `[${userSlug}] Brisanje watcha: ${watchId}`,
      env
    );

    // Obriši seen_listings.json ako postoji
    try {
      await deleteFile(
        `users/${userSlug}/watches/${watchId}/seen_listings.json`,
        `[${userSlug}] Brisanje watcha seen_listings: ${watchId}`,
        env
      );
    } catch (e) {
      // Ignoriraj grešku ako seen_listings ne postoji
    }

    return json({ ok: true });
  } catch (e) {
    return json({ error: String(e.message) }, 500);
  }
}

async function handleToggleWatch(url, request, env) {
  const body = await request.json();
  const userSlug = slugify(body.user);
  const watchId = url.searchParams.get("watch_id");
  
  if (!userSlug || !watchId) {
    return json({ error: "Nedostaju 'user' ili 'watch_id' parametri." }, 400);
  }

  try {
    const file = await getFile(`users/${userSlug}/watches/${watchId}/criteria.json`, env);
    if (!file) return json({ error: "Watch nije pronađen." }, 404);
    
    const criteria = JSON.parse(file.content);
    criteria.active = !criteria.active;

    await putFile(
      `users/${userSlug}/watches/${watchId}/criteria.json`,
      criteria,
      `[${userSlug}] Toggle active za watch: ${watchId} -> ${criteria.active}`,
      env
    );

    return json({ ok: true, active: criteria.active });
  } catch (e) {
    return json({ error: String(e.message) }, 500);
  }
}

// ---------------------------------------------------------------------------
// Stare API endpoints (za backward compatibility)
// ---------------------------------------------------------------------------

async function handleGetConfig(url, env) {
  const userSlug = slugify(url.searchParams.get("user"));
  if (!userSlug) return json({ error: "Nedostaje 'user' parametar." }, 400);

  try {
    // Prvo pokušaj novu strukturu - prvi watch
    const watchesPath = `users/${userSlug}/watches`;
    const resp = await githubRequest(`contents/${watchesPath}?ref=${BRANCH}`, {}, env);
    
    if (resp.ok) {
      const items = await resp.json();
      if (Array.isArray(items) && items.length > 0) {
        const firstWatch = items.find(item => item.type === "dir");
        if (firstWatch) {
          const criteriaFile = await getFile(
            `${watchesPath}/${firstWatch.name}/criteria.json`,
            env
          );
          if (criteriaFile) {
            return json({ exists: true, criteria: JSON.parse(criteriaFile.content) });
          }
        }
      }
    }
    
    // Fallback na staru strukturu (za backward compatibility)
    const file = await getFile(`users/${userSlug}/criteria.json`, env);
    if (!file) return json({ exists: false });
    return json({ exists: true, criteria: JSON.parse(file.content) });
  } catch (e) {
    return json({ error: String(e.message) }, 500);
  }
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

  try {
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

    // Spremi u novu strukturu (watch je nazvan kao korisnik)
    const watchId = slugify(criteria.display_name) || "default";
    const criteriaPath = `users/${userSlug}/watches/${watchId}/criteria.json`;
    await putFile(criteriaPath, criteria, `Postavke sačuvane preko obrasca: ${userSlug}/${watchId}`, env);

    const statePath = `users/${userSlug}/watches/${watchId}/seen_listings.json`;
    const existingState = await getFile(statePath, env);
    if (!existingState) {
      await putFile(statePath, {}, `Nov watch seen_listings: ${userSlug}/${watchId}`, env);
    }

    return json({ ok: true, user: userSlug, watch_id: watchId });
  } catch (e) {
    return json({ error: String(e.message) }, 500);
  }
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

// ---------------------------------------------------------------------------
// Router
// ---------------------------------------------------------------------------

export default {
  async fetch(request, env) {
    if (request.method === "OPTIONS") {
      return new Response(null, { headers: corsHeaders() });
    }

    const url = new URL(request.url);

    try {
      // Multi-watch endpoints
      if (url.pathname === "/api/watches" && request.method === "GET") {
        return await handleGetWatches(url, env);
      }
      if (url.pathname === "/api/watches" && request.method === "POST") {
        return await handlePostWatch(request, env);
      }
      if (url.pathname.match(/^\/api\/watches\/[^/]+$/) && request.method === "GET") {
        return await handleGetWatch(url, env);
      }
      if (url.pathname.match(/^\/api\/watches\/[^/]+$/) && request.method === "PUT") {
        return await handlePutWatch(url, request, env);
      }
      if (url.pathname.match(/^\/api\/watches\/[^/]+$/) && request.method === "DELETE") {
        return await handleDeleteWatch(url, request, env);
      }
      if (url.pathname.match(/^\/api\/watches\/[^/]+\/toggle$/) && request.method === "PATCH") {
        return await handleToggleWatch(url, request, env);
      }

      // Backward compatibility
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
