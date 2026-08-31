// Ovaj skript izvršava OLX-ov "__NUXT__" JavaScript blok (koji sadrži
// kompletnu listu oglasa u kompresovanom obliku) i ispisuje čiste,
// razumljive podatke kao JSON na stdout. Poziva ga scraper.py automatski -
// ne treba ga ručno pokretati.

const fs = require("fs");

const jsFilePath = process.argv[2];
const mode = process.argv[3] || "search"; // "search" ili "full"

if (!jsFilePath) {
  console.error("Nedostaje putanja do .js fajla kao argument.");
  process.exit(1);
}

global.window = {};

try {
  const script = fs.readFileSync(jsFilePath, "utf-8");
  eval(script);

  if (mode === "full") {
    // Ispiši CIJELO stanje - koristimo se kad tražimo detalje pojedinačnog
    // oglasa (opis, osobine), čija tačna struktura nam nije unaprijed poznata.
    process.stdout.write(JSON.stringify(window.__NUXT__ || {}));
  } else {
    const results = (window.__NUXT__ && window.__NUXT__.state &&
                     window.__NUXT__.state.search &&
                     window.__NUXT__.state.search.results) || [];
    process.stdout.write(JSON.stringify(results));
  }
} catch (err) {
  console.error("Greška pri izvršavanju NUXT skripte: " + err.message);
  process.exit(1);
}
