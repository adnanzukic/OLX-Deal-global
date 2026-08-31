# OLX Deal Watcher (multi-user)

Alat koji prati OLX.ba oglase prema kriterijima **više različitih ljudi
istovremeno**, i svakom šalje Telegram obavijesti **samo za njegove**
stvari, na **njegov** chat - bez miješanja. Svako svoje postavke unosi
sam, preko jednostavnog web obrasca, bez potrebe da zna GitHub, JSON, ili
išta tehničko.

## Kako je sve povezano

```
docs/index.html  (web obrazac, hostovan na GitHub Pages, besplatno)
        │  korisnik popuni "šta tražim, do koje cijene, moj Telegram ID"
        ▼
Cloudflare Worker  (mali "server", besplatan, čuva GitHub token bezbjedno)
        │  upisuje users/<ime>/criteria.json u repozitorij
        ▼
GitHub repozitorij (users/<ime>/criteria.json + seen_listings.json)
        ▲
        │  svakih 30 min pročita SVAKI users/<ime>/ folder
GitHub Actions (scraper.py)  ◄── pokreće ga cron-job.org (spolja, pouzdanije)
        │
        ▼
Telegram poruka -> pravom korisniku, na njegov chat_id
```

Ti (vlasnik repozitorija) si jedini koji ikad dira GitHub, Cloudflare i
API ključeve. Svako ko koristi alat (uključujući tebe) samo otvara web
obrazac i popunjava polja.

---

## Dio A: Pripremi GitHub repozitorij

Ovo je isto kao ranije (Telegram bot, Gemini ključ) uz jednu promjenu -
`TELEGRAM_CHAT_ID` sekret **više nije potreban** kao GitHub Actions secret
(sad je to dio svačijeg ličnog `criteria.json`, ne globalna postavka).

**GitHub Actions Secrets** (Settings → Secrets and variables → Actions) -
trebaju ti samo:
- `TELEGRAM_BOT_TOKEN`
- `GEMINI_API_KEY`

(Ako imaš stari `TELEGRAM_CHAT_ID` secret od ranije, slobodno ga obriši -
više se ne koristi.)

Otpremi (ili commit-uj) sve fajlove iz ovog foldera u svoj repozitorij,
uključujući nove: `cloudflare-worker/worker.js`, `docs/index.html`.

---

## Dio B: Napravi Cloudflare Worker (pozadinski "server")

1. Idi na **dash.cloudflare.com**, napravi besplatan nalog (ne treba
   kartica za Workers Free plan)
2. U meniju idi na **Workers & Pages** → **Create** → **Create Worker**
3. Daj mu ime, npr. `olx-deal-watcher` (ovo postaje dio tvog URL-a)
4. Klikni **Deploy** (kreira prazan "Hello World" worker za sad)
5. Klikni **Edit code** (ili "Quick edit")
6. **Obriši sav** postojeći kod u editoru, i zalijepi kompletan sadržaj
   fajla `cloudflare-worker/worker.js` (otvori ga kod sebe u Notepad-u,
   kopiraj sve)
7. Klikni **Save and deploy** (ili **Deploy**)
8. Zapamti/kopiraj svoj Worker URL - izgleda otprilike ovako:
   `https://olx-deal-watcher.TVOJE-IME.workers.dev`

### Dodaj Worker "secrets" (odvojeno od GitHub secrets - ovo je Cloudflare-ov sistem)

1. Na stranici svog Worker-a, idi na **Settings** → **Variables and Secrets**
2. Dodaj dvije varijable, obje kao tip **"Secret"** (ne "Text"):
   - `GITHUB_TOKEN` - vrijednost: tvoj GitHub Personal Access Token (isti
     tip kao za cron-job.org ranije - ako ga još imaš, iskoristi ga; ako
     ne, napravi novi na github.com/settings/tokens sa `repo` dozvolom)
   - `TELEGRAM_BOT_TOKEN` - vrijednost: isti bot token koji koristiš i za
     GitHub Actions
3. Sačuvaj (Save and deploy)

**Bitna napomena o imenu repozitorija:** Otvori `cloudflare-worker/worker.js`
i provjeri da linije `const OWNER = "adnanzukic";` i `const REPO = "OLX-Deal";`
na vrhu fajla tačno odgovaraju tvom GitHub korisničkom imenu i imenu
repozitorija. Ako se ne poklapaju, izmijeni ih i ponovo deploy-uj (Korak 6-7
iznad, samo sa ispravkom).

---

## Dio C: Uključi GitHub Pages (hostuje web obrazac, besplatno)

1. U repozitoriju idi na **Settings** → **Pages**
2. Pod "Build and deployment" → **Source**, izaberi **"Deploy from a branch"**
3. Pod **Branch**, izaberi `main` i folder **`/docs`**
4. Klikni **Save**
5. Sačekaj minut-dva, pa osvježi stranicu - trebao bi vidjeti link tipa:
   `https://TVOJE-IME.github.io/IME-REPOZITORIJA/`

---

## Dio D: Poveži obrazac sa svojim Worker-om i botom

Otvori `docs/index.html` (kroz GitHub "Edit" olovčicu, ili lokalno pa
commit-uj), i promijeni ove dvije linije na dnu fajla (u `<script>` dijelu):

```js
const WORKER_URL = "https://olx-deal-watcher.TVOJ-SUBDOMAIN.workers.dev";
const BOT_USERNAME = "TVOJ_BOT_USERNAME";
```

- `WORKER_URL` = tačan URL tvog Worker-a iz Dijela B
- `BOT_USERNAME` = username tvog Telegram bota (bez @, onaj koji si dao
  BotFather-u)

Sačuvaj/commit-uj izmjenu.

---

## Dio E: Isprobaj

1. Otvori svoj GitHub Pages link (Dio C) u browseru
2. Popuni obrazac (svoje ime, OLX link, cijenu, itd.)
3. Klikni "Otvori bota" link, pošalji mu poruku, vrati se i klikni
   "Pronađi moj Chat ID" - trebalo bi automatski popuniti polje
4. Klikni "Sačuvaj postavke"
5. Provjeri na GitHub-u (Code tab) da li se pojavio novi folder
   `users/<tvoje-ime>/` sa `criteria.json` unutra
6. Idi na **Actions** tab, ručno pokreni "OLX RAM Watcher" (Run workflow),
   ili sačekaj sljedeći cron-job.org ciklus
7. Provjeri log - trebao bi pisati da je obrađen tvoj korisnik i (ako ima
   poklapanja) da su poslane obavijesti

---

## Kako prijatelj koristi ovo

Samo mu pošalji link sa Dijela C (`https://TVOJE-IME.github.io/...`).
On:
1. Otvori link
2. Popuni šta traži (svoj OLX link, cijenu, ključne riječi)
3. Pošalje poruku istom botu, klikne "Pronađi moj Chat ID"
4. Sačuva

Gotovo - njegove obavijesti idu na **njegov** Telegram, potpuno odvojeno
od tvojih, bez da je ikad vidio GitHub, JSON, ili API ključ.

---

## Testiranje lokalno (za tebe, kad nešto popravljaš u kodu)

Isto kao ranije, samo sad `scraper.py` prolazi kroz **sve** foldere u
`users/`. Napravi test folder ručno da probaš:

```
users/test/criteria.json      (kopiraj strukturu iz primjera ispod)
users/test/seen_listings.json  (sadržaj: {})
```

Primjer `criteria.json` strukture:
```json
{
  "display_name": "Test",
  "search_url": "https://olx.ba/pretraga?...",
  "max_price_km": 125,
  "required_keywords_any": ["16gb", "2x8"],
  "required_keywords_all": ["ddr4"],
  "excluded_keywords": ["4x4", "laptop"],
  "min_speed_mhz": 3000,
  "single_stick_max_price_km": 71,
  "use_vision_fallback": true,
  "telegram_chat_id": "TVOJ_CHAT_ID"
}
```

Zatim isto kao ranije:
```
pip install -r requirements.txt
$env:TELEGRAM_BOT_TOKEN="..."
$env:GEMINI_API_KEY="..."
python scraper.py
```

---

## Šta ako nešto ne radi

- **Obrazac javlja grešku pri čuvanju** - otvori browser konzolu (F12 →
  Console) da vidiš tačnu grešku, najčešće je pogrešan `WORKER_URL` ili
  Worker nema ispravno podešene secrete (Dio B)
- **"Pronađi moj Chat ID" ne nalazi ništa** - pošalji botu poruku PRVO, pa
  odmah klikni dugme (Telegram pamti samo skorašnje poruke za ovu svrhu)
- **Notifikacije ne stižu nikom** - provjeri GitHub Actions log (Actions
  tab → klikni na run → check-olx) za tačnu grešku
- Za sve ostalo, pošalji mi poruku o grešci (screenshot loga ili konzole)
  pa rješavamo zajedno, kao i do sad
