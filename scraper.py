"""
OLX Deal Watcher - MULTI-USER verzija. Prati oglase po kriterijima SVAKOG
korisnika ponaosob (folder users/<ime>/) i šalje Telegram obavijesti na
NJEGOV vlastiti chat, tako da se poruke različitih ljudi ne miješaju.

Kako radi (ukratko):
1. Pronađe sve foldere unutar users/ - svaki predstavlja jednog korisnika
   sa svojim criteria.json (šta traži) i seen_listings.json (šta je već
   viđeno za NJEGA).
2. Za svakog korisnika ponaosob, ponovi isti postupak kao i ranije:
   - Otvori njegovu filtriranu OLX pretragu (search_url iz njegovog fajla)
   - OLX ugrađuje kompletnu listu oglasa kao "kompresovan" JavaScript objekat
     direktno u HTML stranicu (Nuxt.js framework). Da bismo pouzdano
     pročitali te podatke, moramo taj JS kod stvarno IZVRŠITI - zato
     koristimo Node.js kao pomoćni alat (vidi nuxt_extractor.js).
   - Za svaki NOV oglas, provjera ide u slojevima (naslov -> stvaran opis
     sa stranice oglasa -> AI čitanje slike ako ni opis nije dovoljan).
     Ako se ne može SIGURNO potvrditi da oglas odgovara, tretira se kao
     "ne odgovara" (bolje propustiti nesiguran oglas nego slati pogrešne
     obavijesti).
   - Za nove poklapajuće oglase ILI postojeće kojima je cijena pala ispod
     prethodno zabilježene, pošalji Telegram poruku NA NJEGOV chat_id.
   - Sačuvaj ažurirano stanje u users/<ime>/seen_listings.json
3. Gemini API ključ i Telegram bot token su ZAJEDNIČKI (jedan bot, jedan
   ključ za sve korisnike) - jedino što je različito po korisniku je NJEGOV
   Telegram chat_id (kome se šalje) i njegovi lični kriteriji pretrage.
"""

import json
import os
import re
import subprocess
import sys
import time
import logging
from pathlib import Path

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("olx-watcher")

BASE_DIR = Path(__file__).resolve().parent
USERS_DIR = BASE_DIR / "users"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "bs-BA,bs;q=0.9,en-US;q=0.8,en;q=0.7",
}

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

# Uzmi u obzir samo prvih N oglasa sa stranice pretrage (dovoljno za "top of list" skeniranje)
MAX_LISTINGS_TO_CHECK = 40
# Koliko sekundi pauze između provjere pojedinačnih oglasa (da ne "bombardujemo" server)
REQUEST_DELAY_SECONDS = 1.5


# ---------------------------------------------------------------------------
# Pomoćne funkcije
# ---------------------------------------------------------------------------

def load_json(path, default):
    if not path.exists():
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log.error(f"Greška pri čitanju {path}: {e}")
        return default


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def normalize_text(text):
    return (text or "").lower().replace("č", "c").replace("ć", "c") \
        .replace("š", "s").replace("ž", "z").replace("đ", "dj")


def parse_price_to_int(price_text):
    """'120 KM' -> 120, '1.234,90 KM' -> 1234"""
    if not price_text:
        return None
    digits = re.sub(r"[^\d]", "", price_text.split(",")[0])
    if not digits:
        return None
    try:
        return int(digits)
    except ValueError:
        return None


def slugify(text):
    """Napravi URL-friendly slug od naslova, npr. 'DDR4 16GB' -> 'ddr4-16gb'"""
    norm = normalize_text(text)
    norm = re.sub(r"[^a-z0-9]+", "-", norm).strip("-")
    return norm or "oglas"


def build_listing_url(listing_id, title=None):
    """Konstruiše URL ka pojedinačnom oglasu. Potvrđeno stvaran format: olx.ba/artikal/<id>"""
    return f"https://olx.ba/artikal/{listing_id}"


# ---------------------------------------------------------------------------
# Scraping OLX pretrage (lista oglasa)
# ---------------------------------------------------------------------------

NUXT_EXTRACTOR_JS = BASE_DIR / "nuxt_extractor.js"


def fetch_search_results(search_url):
    """Vrati listu kandidata sa stranice pretrage:
    [{id, title, price, url, image_urls}, ...]

    Pristup: preuzmemo sirovi HTML, izvučemo ugrađeni '__NUXT__' JavaScript
    blok (tu OLX drži kompletnu listu oglasa u kompresovanom obliku), pa ga
    stvarno IZVRŠIMO kroz Node.js da dobijemo čiste, čitljive podatke -
    mnogo pouzdanije nego pogađanje HTML CSS klasa koje se mogu promijeniti.
    """
    log.info(f"Otvaram pretragu: {search_url}")
    resp = requests.get(search_url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    html = resp.text

    script_content = _extract_nuxt_script(html)
    if not script_content:
        log.warning("Nisam pronašao __NUXT__ podatke na stranici - sačuvavam snapshot za debug.")
        _save_debug_snapshot(html)
        return []

    raw_results = _run_nuxt_extractor(script_content, mode="search")
    if raw_results is None:
        _save_debug_snapshot(html)
        return []

    listings = []
    for r in raw_results[:MAX_LISTINGS_TO_CHECK]:
        if r.get("status") != "active" or r.get("visible") is False:
            continue

        listing_id = str(r.get("id"))
        title = r.get("title") or ""
        price = r.get("price")
        if price is None:
            price = parse_price_to_int(r.get("display_price", ""))

        if not title or price is None:
            continue

        image_urls = r.get("images") or ([r["image"]] if r.get("image") else [])

        listings.append({
            "id": listing_id,
            "title": title,
            "price": int(price),
            "url": build_listing_url(listing_id, title),
            "image_urls": image_urls,
        })

    log.info(f"Pronađeno {len(listings)} oglasa na stranici pretrage.")
    return listings


def _extract_nuxt_script(html):
    """Izvuci sadržaj <script> taga koji sadrži window.__NUXT__ podatke."""
    idx = html.find("__NUXT__")
    if idx == -1:
        return None
    script_start = html.rfind("<script", 0, idx)
    script_end = html.find("</script>", idx)
    if script_start == -1 or script_end == -1:
        return None
    tag_close = html.find(">", script_start)
    script_content = html[tag_close + 1:script_end]
    if "window.__NUXT__" not in script_content:
        script_content = script_content.replace("__NUXT__=", "window.__NUXT__=", 1)
    return script_content


def _run_nuxt_extractor(script_content, mode="search"):
    """Pokreni Node.js da izvrši dati JS blok i vrati parsirani JSON (ili None)."""
    js_path = BASE_DIR / "tmp" / "_nuxt_extract_tmp.js"
    js_path.parent.mkdir(parents=True, exist_ok=True)
    with open(js_path, "w", encoding="utf-8") as f:
        f.write(script_content)

    try:
        result = subprocess.run(
            ["node", str(NUXT_EXTRACTOR_JS), str(js_path), mode],
            capture_output=True, text=True, timeout=30,
            encoding="utf-8", errors="replace",
        )
    except FileNotFoundError:
        log.error(
            "Node.js nije instaliran ili nije dostupan u PATH-u. "
            "Instaliraj ga sa nodejs.org (Korak 4 u README-u) i probaj ponovo."
        )
        return None

    if result.returncode != 0:
        log.error(f"Greška pri izvršavanju NUXT ekstraktora: {result.stderr[:1000]}")
        return None

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as e:
        log.error(f"Ne mogu parsirati izlaz Node ekstraktora: {e}")
        return None


def _find_listing_object(data, listing_id):
    """Rekurzivno pretraži strukturu podataka tražeći objekat konkretnog
    oglasa (prepoznaje se po tome da ima 'id' jednak našem ID-u I bar jedno
    od polja koja bi sadržavala opis/specifikacije). Ne znamo unaprijed
    tačnu putanju u strukturi na stranici pojedinačnog oglasa, pa tražimo
    posvuda - ovo je robusnije od pretpostavljanja fiksne putanje.
    """
    target_id = str(listing_id)
    seen = set()

    def walk(obj):
        if id(obj) in seen:
            return None
        if isinstance(obj, dict):
            seen.add(id(obj))
            obj_id = obj.get("id")
            if obj_id is not None and str(obj_id) == target_id and (
                "description" in obj or "attributes" in obj or "properties" in obj or "params" in obj
            ):
                return obj
            for v in obj.values():
                found = walk(v)
                if found is not None:
                    return found
        elif isinstance(obj, list):
            for item in obj:
                found = walk(item)
                if found is not None:
                    return found
        return None

    return walk(data)


def fetch_listing_full_text(url, listing_id):
    """Otvori stranicu pojedinačnog oglasa i vrati kombinovan tekst svih
    dostupnih specifikacija (naziv+vrijednost parova) i detaljnog opisa.
    Vrati None ako ne uspije (stranica ima drugačiju strukturu nego
    očekujemo - u tom slučaju se čuva debug snapshot).
    """
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
    except Exception as e:
        log.warning(f"Ne mogu otvoriti oglas {url}: {e}")
        return None

    script_content = _extract_nuxt_script(resp.text)
    if not script_content:
        log.warning(f"Nema __NUXT__ podataka na stranici oglasa {url}.")
        _save_debug_snapshot(resp.text, "last_listing_debug.html")
        return None

    full_state = _run_nuxt_extractor(script_content, mode="full")
    if full_state is None:
        return None

    listing_obj = _find_listing_object(full_state, listing_id)
    if listing_obj is None:
        log.warning(
            f"Nisam pronašao podatke o oglasu {listing_id} u NUXT strukturi - "
            "čuvam snapshot za debug."
        )
        debug_path = BASE_DIR / "tmp" / "last_listing_full_state.json"
        debug_path.parent.mkdir(parents=True, exist_ok=True)
        with open(debug_path, "w", encoding="utf-8") as f:
            json.dump(full_state, f, ensure_ascii=False, indent=2)
        return None

    parts = []

    description = listing_obj.get("description") or listing_obj.get("opis") or ""
    if description:
        parts.append(str(description))

    # "Osobine" tabela - može biti pod raznim imenima/oblicima u zavisnosti
    # od strukture stranice, pokušavamo par uobičajenih.
    attrs = listing_obj.get("attributes") or listing_obj.get("properties") or listing_obj.get("params") or []
    if isinstance(attrs, dict):
        attrs = list(attrs.values())
    if isinstance(attrs, list):
        for attr in attrs:
            if isinstance(attr, dict):
                label = attr.get("name") or attr.get("label") or attr.get("key") or ""
                value = attr.get("value") or attr.get("val") or ""
                if label or value:
                    parts.append(f"{label}: {value}")
            elif isinstance(attr, str):
                parts.append(attr)

    if not parts:
        return ""  # stranica je pronađena, ali nema ni opisa ni specifikacija - legitimno prazno

    return " | ".join(parts)


def _save_debug_snapshot(html, filename="last_page_debug.html"):
    debug_path = BASE_DIR / "tmp" / filename
    debug_path.parent.mkdir(parents=True, exist_ok=True)
    with open(debug_path, "w", encoding="utf-8") as f:
        f.write(html)


# ---------------------------------------------------------------------------
# Provjera kriterija (tekst)
# ---------------------------------------------------------------------------

def text_matches_criteria(text, criteria, price=None):
    """Vrati (matched: bool, confident: bool).
    matched = da li tekst zadovoljava obavezne uslove
    confident = da li smo SIGURNI (npr. brzina eksplicitno piše) ili treba
                dodatna provjera (opis / slika)
    """
    norm = normalize_text(text)
    # Kompaktna verzija bez razmaka oko 'x' (hvata "2x8", "2x 8", "2 x8", "2 x 8" - sve isto)
    norm_compact = re.sub(r"\s*x\s*", "x", norm)

    for bad_word in criteria.get("excluded_keywords", []):
        bad_norm = normalize_text(bad_word)
        bad_compact = re.sub(r"\s*x\s*", "x", bad_norm)
        if bad_norm in norm or bad_compact in norm_compact:
            return False, True  # isključeno, i sigurni smo u to

    all_required = criteria.get("required_keywords_all", [])
    if not all(normalize_text(kw) in norm for kw in all_required):
        return False, False  # nedostaje obavezan ključni pojam, nismo 100% sigurni (možda piše drugačije)

    any_required = criteria.get("required_keywords_any", [])
    if any_required:
        found = any(
            normalize_text(kw) in norm or re.sub(r"\s*x\s*", "x", normalize_text(kw)) in norm_compact
            for kw in any_required
        )
        if not found:
            return False, False

    # Poseban slučaj: pojedinačan 1x16GB štapić (single-channel, nije par) -
    # prihvatljiv SAMO ako mu je cijena unutar posebnog, nižeg praga (jer
    # nema dual-channel prednost koju inače tražimo).
    single_stick_patterns = ["1x16"]
    is_single_stick = any(p in norm_compact for p in single_stick_patterns)
    if is_single_stick:
        single_max = criteria.get("single_stick_max_price_km")
        if single_max is not None:
            if price is None:
                # Ne znamo cijenu u ovom pozivu (npr. provjera samog naslova
                # prije nego smo sigurni o čemu se radi) - ne možemo još
                # potvrditi, tražimo dalje potvrdu.
                return True, False
            if price > single_max:
                return False, True  # prekoračio poseban prag za pojedinačan štapić, sigurni smo

    # Provjeri brzinu (npr. "3200" mora se pojaviti da smo sigurni da je dovoljno brz)
    min_speed = criteria.get("min_speed_mhz")
    if min_speed:
        speed_match = re.search(r"(\d{4})\s*mhz|\b(\d{4})\b", norm)
        if speed_match:
            found_speed = int(speed_match.group(1) or speed_match.group(2))
            if found_speed < min_speed:
                return False, True  # eksplicitno prespora, sigurni smo
            return True, True  # eksplicitno dovoljno brza, sigurni smo
        else:
            # Brzina se ne pominje eksplicitno u tekstu - NE tretiramo ovo
            # kao "vjerovatno odgovara" (to je dovodilo do lažnih pozitivnih
            # obavijesti), nego kao "još nepotvrđeno". Pozivalac (determine_match)
            # će pokušati potvrditi kroz opis oglasa i/ili sliku; ako ni to ne
            # uspije, ostaje "ne odgovara" - bolje propustiti nesiguran oglas
            # nego slati pogrešne obavijesti.
            return False, False

    return True, True


# ---------------------------------------------------------------------------
# Vision fallback (Gemini) - koristi se SAMO kad tekst nije dovoljno jasan
# ---------------------------------------------------------------------------

def analyze_images_with_gemini(image_urls):
    """Preuzmi slike i zamoli Gemini vision model da PROČITA tekst sa
    naljepnice na modulu (proizvođač, kapacitet, brzina, CAS latencija) i
    vrati ga kao običan tekst - baš kao da je to prodavac napisao u opisu.
    Namjerno NE donosimo odluku o poklapanju kriterija ovdje - taj tekst se
    poslije provjerava kroz istu funkciju (text_matches_criteria) koja se
    koristi i za naslov/opis, radi dosljednosti i manjeg rizika od greške
    (model koji direktno odgovara "DA/NE" je skloniji nagađanju kad nije
    siguran, dok prosto prepisivanje pročitanog teksta ostavlja manje
    prostora za pogrešnu procjenu).

    Vrati string (može biti prazan ako ništa nije pročitljivo) ili None
    ako je došlo do greške / Gemini nije dostupan.
    """
    if not GEMINI_API_KEY:
        log.warning("GEMINI_API_KEY nije podešen - preskačem vision provjeru.")
        return None

    try:
        import google.generativeai as genai
        from PIL import Image
        from io import BytesIO

        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel("gemini-2.5-flash")

        images = []
        for url in image_urls[:3]:
            try:
                r = requests.get(url, headers=HEADERS, timeout=15)
                r.raise_for_status()
                images.append(Image.open(BytesIO(r.content)))
            except Exception as e:
                log.warning(f"Ne mogu preuzeti sliku {url}: {e}")

        if not images:
            return None

        prompt = (
            "Ovo su fotografije sa oglasa računarske komponente na OLX u "
            "Bosni. NAJPRIJE provjeri da li se na slici stvarno vidi RAM "
            "memorijski modul (ne SSD, ne HDD, ne neka druga komponenta). "
            "Ako NIJE RAM modul, odgovori isključivo sa riječju 'NIJE_RAM'. "
            "Ako JESTE RAM modul, pažljivo pročitaj SAMO tekst koji je "
            "stvarno vidljiv na naljepnici modula (proizvođač, tip - "
            "DDR3/DDR4/DDR5, kapacitet u GB, brzinu u MHz, CAS latenciju/CL "
            "ako piše). Odgovori isključivo sa pročitanim specifikacijama u "
            "kratkoj formi, npr: 'Kingston DDR4 8GB 3200MHz CL16'. Ako je na "
            "slici više modula, navedi sve. Ako ne možeš pouzdano pročitati "
            "neki podatak (nejasna slika, prekriven tekst, odsjaj), NEMOJ ga "
            "pogađati niti pretpostavljati - jednostavno ga izostavi iz "
            "odgovora. Ako ne možeš pročitati apsolutno ništa korisno sa "
            "naljepnice, odgovori samo sa riječju 'NEČITKO'."
        )

        response = model.generate_content([prompt] + images)
        answer = (response.text or "").strip()
        log.info(f"Gemini vision je pročitao: {answer}")
        if "NEČITKO" in answer.upper() or "NIJE_RAM" in answer.upper():
            return ""
        return answer

    except Exception as e:
        log.error(f"Greška u Gemini vision provjeri: {e}")
        return None


# ---------------------------------------------------------------------------
# Telegram notifikacija
# ---------------------------------------------------------------------------

def send_telegram_message(chat_id, text):
    if not TELEGRAM_BOT_TOKEN or not chat_id:
        log.warning("Telegram nije podešen (nedostaje token ili chat_id) - ispisujem u log umjesto slanja:")
        log.info(text)
        return

    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    try:
        r = requests.post(api_url, data=payload, timeout=15)
        r.raise_for_status()
        log.info("Telegram poruka poslana.")
    except Exception as e:
        log.error(f"Greška pri slanju Telegram poruke: {e}")


def format_notification(listing, reason):
    price_line = f"💰 <b>{listing['price']} KM</b>"
    if reason == "price_drop":
        old_price = listing.get("old_price")
        price_line = f"📉 Cijena spuštena: <s>{old_price} KM</s> → <b>{listing['price']} KM</b>"

    return (
        f"🔔 <b>Novi oglas!</b>\n\n"
        f"{listing['title']}\n"
        f"{price_line}\n\n"
        f"🔗 {listing['url']}"
    )


def determine_match(listing, criteria):
    """Slojevita provjera, od najpouzdanijeg ka najnesigurnijem izvoru:
    1) naslov, 2) stvarni opis/osobine sa stranice oglasa, 3) AI čitanje slike.
    Staje čim dobijemo SIGURAN odgovor (bilo pozitivan bilo negativan).
    Vrati True/False.
    """
    matched, confident = text_matches_criteria(listing["title"], criteria, price=listing["price"])
    if confident:
        return matched

    log.info(f"Naslov nije dovoljno jasan za '{listing['title']}' - otvaram oglas da pročitam opis...")
    detail_text = fetch_listing_full_text(listing["url"], listing["id"])
    time.sleep(REQUEST_DELAY_SECONDS)

    if detail_text:
        combined_text = listing["title"] + " | " + detail_text
        matched, confident = text_matches_criteria(combined_text, criteria, price=listing["price"])
        if confident:
            return matched

    if not criteria.get("use_vision_fallback"):
        return matched  # najbolja procjena koju imamo, bez vision provjere

    image_urls = listing.get("image_urls") or []
    if not image_urls:
        return matched

    log.info(f"Ni opis nije dovoljno jasan za '{listing['title']}' - provjeravam sliku...")
    vision_text = analyze_images_with_gemini(image_urls)
    time.sleep(REQUEST_DELAY_SECONDS)

    if vision_text:
        combined_text = listing["title"] + " | " + vision_text
        matched, confident = text_matches_criteria(combined_text, criteria, price=listing["price"])

    return matched


# ---------------------------------------------------------------------------
# Migracija: stara struktura (jedan watch po korisniku) -> nova (više watchesa)
# ---------------------------------------------------------------------------

def migrate_user_to_multi_watch(user_dir):
    """
    Ako korisnik ima staru strukturu (criteria.json na razini users/<user>/),
    migruiraj ga na novu strukturu (criteria.json u users/<user>/watches/<watch_id>/).
    
    Strategija:
    - Ako users/<user>/criteria.json postoji i users/<user>/watches/ NE postoji:
      - Koristi display_name ili "default" kao watch_id (slugify)
      - Premjesti criteria.json u users/<user>/watches/<watch_id>/criteria.json
      - Premjesti seen_listings.json u users/<user>/watches/<watch_id>/seen_listings.json
      - Dodaj "active": true u criteria
    - Migracija je idempotentna (može se pokrenuti više puta bez štete)
    """
    username = user_dir.name
    old_criteria_path = user_dir / "criteria.json"
    old_state_path = user_dir / "seen_listings.json"
    watches_dir = user_dir / "watches"
    
    # Ako stara struktura ne postoji, nema šta migrirati
    if not old_criteria_path.exists():
        return False
    
    # Ako nova struktura već postoji, migracija je već obavljena
    if watches_dir.exists() and any(watches_dir.iterdir()):
        log.info(f"[{username}] Već je migrirano - nema ništa za obaviti.")
        return False
    
    # Učitaj stare datoteke
    old_criteria = load_json(old_criteria_path, {})
    if not old_criteria:
        log.warning(f"[{username}] Stara criteria.json postoji ali je prazan ili nevalidan - preskačem migraciju.")
        return False
    
    # Nappravi watch_id od display_name ili koristi default
    watch_name = old_criteria.get("display_name", username)
    watch_id = slugify(watch_name)
    if not watch_id:
        watch_id = "default"
    
    watch_dir = watches_dir / watch_id
    
    # Ako watch već postoji, ne prepisujem (idempotencija)
    if watch_dir.exists() and (watch_dir / "criteria.json").exists():
        log.info(f"[{username}][{watch_id}] Watch direktorij već postoji - ne prepisujem.")
        return False
    
    watch_dir.mkdir(parents=True, exist_ok=True)
    
    # Dodaj "active": true u criteria ako već ne postoji
    if "active" not in old_criteria:
        old_criteria["active"] = True
    
    # Spremi migriranu criteria
    new_criteria_path = watch_dir / "criteria.json"
    save_json(new_criteria_path, old_criteria)
    log.info(f"[{username}][{watch_id}] Premješten criteria.json.")
    
    # Spremi migriranu state (seen_listings) ako postoji
    if old_state_path.exists():
        old_state = load_json(old_state_path, {})
        new_state_path = watch_dir / "seen_listings.json"
        save_json(new_state_path, old_state)
        log.info(f"[{username}][{watch_id}] Premješten seen_listings.json ({len(old_state)} stavki).")
    else:
        # Kreiraj prazan seen_listings
        new_state_path = watch_dir / "seen_listings.json"
        save_json(new_state_path, {})
    
    log.info(f"[{username}] ✓ Migracija završena. Novo: users/{username}/watches/{watch_id}/")
    return True



# ---------------------------------------------------------------------------
# Glavna logika - MULTI-WATCH: obradi svakog korisnika i sve njegove watchese
# ---------------------------------------------------------------------------

def process_watch(user_dir, watch_dir, watch_id):
    """Obradi jedan watch - vrati broj poslanih obavijesti."""
    username = user_dir.name
    criteria_path = watch_dir / "criteria.json"
    state_path = watch_dir / "seen_listings.json"

    criteria = load_json(criteria_path, {})
    if not criteria:
        log.warning(f"[{username}][{watch_id}] Nema criteria.json ili je prazan - preskačem.")
        return 0

    # Provjeri da li je watch aktivan
    if not criteria.get("active", True):
        log.info(f"[{username}][{watch_id}] Pauzirano - preskačem.")
        return 0

    chat_id = criteria.get("telegram_chat_id")
    if not chat_id:
        log.warning(f"[{username}][{watch_id}] Nema podešen telegram_chat_id - preskačem.")
        return 0

    search_url = criteria.get("search_url")
    if not search_url:
        log.warning(f"[{username}][{watch_id}] Nema search_url - preskačem.")
        return 0

    display_name = criteria.get("display_name", watch_id)
    log.info(f"=== [{username}] Obrađujem watch: {display_name} ===")

    state = load_json(state_path, {})
    listings = fetch_search_results(search_url)

    max_price = criteria.get("max_price_km")
    notified_count = 0

    for listing in listings:
        listing_id = listing["id"]

        if max_price is not None and listing["price"] > max_price:
            continue

        previous = state.get(listing_id)

        if previous is not None:
            # Već smo ranije odlučili da li ovaj oglas odgovara kriterijima -
            # ne ponavljamo (skupu) provjeru, samo pratimo eventualni pad cijene.
            matched = previous.get("matched", False)
            reason = None
            if matched and listing["price"] < previous.get("price", listing["price"]):
                reason = "price_drop"
                listing["old_price"] = previous["price"]
        else:
            matched = determine_match(listing, criteria)
            reason = "new" if matched else None

        if reason:
            log.info(f"[{username}][{watch_id}] MATCH ({reason}): {listing['title']} - {listing['price']} KM")
            # Dodaj naziv watcha u notifikaciju
            watch_name_emoji = f"👁️ <b>{display_name}</b>\n\n" if display_name != watch_id else ""
            telegram_text = watch_name_emoji + format_notification(listing, reason)
            send_telegram_message(chat_id, telegram_text)
            notified_count += 1
            time.sleep(1)

        state[listing_id] = {
            "price": listing["price"],
            "title": listing["title"],
            "url": listing["url"],
            "matched": matched,
        }

    save_json(state_path, state)
    log.info(f"[{username}][{watch_id}] Gotovo. Poslano {notified_count} obavijesti. Praćeno oglasa: {len(state)}.")
    return notified_count


def process_user(user_dir):
    """Obradi jednog korisnika - svi njegovi watchesi. Vrati broj poslanih obavijesti."""
    username = user_dir.name
    total_notified = 0
    
    # Migruiraj ako je potrebno
    migrate_user_to_multi_watch(user_dir)
    
    # Obradi sve watchese u korisnikovom direktoriju
    watches_dir = user_dir / "watches"
    if not watches_dir.exists():
        log.warning(f"[{username}] Nema watches/ direktorija - nema što obraditi.")
        return 0
    
    watch_dirs = sorted([d for d in watches_dir.iterdir() if d.is_dir() and (d / "criteria.json").exists()])
    if not watch_dirs:
        log.warning(f"[{username}] Nema nijednog validnog watcha u watches/ - nema što obraditi.")
        return 0
    
    for watch_dir in watch_dirs:
        watch_id = watch_dir.name
        try:
            watch_notifications = process_watch(user_dir, watch_dir, watch_id)
            total_notified += watch_notifications
        except Exception as e:
            log.error(f"[{username}][{watch_id}] Greška pri obradi watcha: {e}")
            # Nastavi sa sljedećim watchem - jedna greška ne zaustavlja sve ostale
    
    return total_notified


def main():
    if not USERS_DIR.exists():
        log.warning("users/ folder još ne postoji - vjerovatno se niko još nije prijavio preko obrasca. Prekidam bez greške.")
        return

    user_dirs = sorted([d for d in USERS_DIR.iterdir() if d.is_dir()])

    if not user_dirs:
        log.warning("Nema nijednog korisnika u users/ folderu - ništa za obraditi.")
        return

    total_notified = 0
    processed_users = 0
    
    for user_dir in user_dirs:
        try:
            user_notified = process_user(user_dir)
            if user_notified > 0 or (user_dir / "watches").exists():
                total_notified += user_notified
                processed_users += 1
        except Exception as e:
            log.error(f"[{user_dir.name}] Neočekivana greška pri obradi korisnika: {e}")

    log.info(f"=== Sve gotovo. Obrađeno korisnika: {processed_users}. Ukupno poslano obavijesti: {total_notified}. ===")


if __name__ == "__main__":
    main()
