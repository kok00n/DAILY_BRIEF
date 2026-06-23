# 🎙️ DAILY_BRIEF

Codzienny, ~40-minutowy **poranny brief głosowy** dla tradera stóp/obligacji.
Narzędzie samo zbiera dane, pisze skrypt i publikuje odcinek jako **prywatny
podcast (RSS)**, który telefon pobiera przed Twoim wyjazdem do pracy.

W poniedziałek automatycznie cofa się do **weekendu** (od piątku); w pozostałe
dni obejmuje **ostatnie 24 godziny**.

## Co robi (pipeline)

```
collect → aggregate → generate script (Opus 4.8) → text-to-speech → publish (RSS)
```

1. **Dane rynkowe** (darmowe, deterministyczne i *datowane*): FRED (US: 2/5/10/30Y,
   2s10s, SOFR, fed funds, breakevens, HY OAS), **Bundesbank** (Bund 10Y + Schatz 2Y;
   fallback **ECB**), **ECB** (FX: EUR/USD, USD/PLN, EUR/PLN, USD/JPY + DXY z koszyka —
   Yahoo jako fallback), Yahoo/yfinance (indeksy, surowce, VIX, WIG/WIG20),
   CoinGecko + Fear&Greed (krypto). Cash govie CEE (PL/CZ/HU): walidowany snapshot /
   CNB ARAD → miesięczny anchor FRED/OECD; dzienny odczyt liczy się tylko gdy świeży
   (≤7 dni), inaczej jawnie „dane miesięczne". *(Stooq wyłączony — apikey-owy CSV padł
   ~2026-06-05.)* Dzienny obraz CEE-rates niesie głównie **swap rates (IRS)**
   PLN/HUF/CZK/EUR z **BlueGamma** (publiczne API, bez klucza) + nachylenia krzywej
   (2s10s, 5s10s… → steepenery/flattenery).
2. **Kalendarz na dziś** (forward-looking): FairEconomy/ForexFactory (majors z
   godzinami, impactem, prognozą/poprzednią) + suplement CEE przez Perplexity
   (NBP/CNB/MNB + dzisiejsi mówcy Fed/ECB) — bo feed majors nie ma PL/CZ/HU.
3. **Newsy**: Perplexity Sonar Pro — targetowane zapytania per sekcja.
4. **FinTwit + analitycy**: xAI Grok `x_search` — **tematycznie po całym X**
   (bias na jakość) + lista „never-miss" auto-batchowana po ≤20 kont +
   `web_search` (Substacki, breaking) — **bez** płatnego X API.
5. **Skrypt**: Claude **Opus 4.8** składa ~40 min narracji po polsku
   (z angielskimi terminami), z naciskiem na **stopy/obligacje (cores + CEE)**,
   sekcją **AI & Tech** oraz **trade ideas**.
6. **Audio**: `edge-tts` (darmowy polski głos `pl-PL-MarekNeural`).
7. **Publikacja**: MP3 + feed RSS na **Cloudflare R2**; telefon subskrybuje feed.

Struktura odcinka i wszystkie parametry: [config.yaml](config.yaml).

---

## Wymagania

- **Python 3.11+** (masz 3.11.9 ✅)
- Klucze API: `ANTHROPIC_API_KEY`, `PERPLEXITY_API_KEY`, `XAI_API_KEY`
- Darmowy `FRED_API_KEY` — rejestracja 1 min: https://fredaccount.stlouisfed.org/apikeys
- `STOOQ_API_KEY` — *(nieaktywny)* Stooq jest wyłączony (`cee_yields.stooq.enabled: false`),
  bo apikey-owy CSV padł ~2026-06-05 (anti-bot challenge). Sekret niepotrzebny, dopóki
  Stooq nie przywróci CSV.
- *(opcjonalny)* `CNB_API_KEY` — klucz CNB ARAD jako zapas dla czeskich 10Y (uwaga:
  bieżący indikator dzienny bywa nieaktualizowany). Rejestracja: https://www.cnb.cz/arad/ .
  Bez niego CZ schodzi do miesięcznego FRED/OECD.
- Konto **Cloudflare** (darmowy R2) do hostingu MP3 + RSS
- *(zalecane)* `ffmpeg` do czystego łączenia audio: `winget install Gyan.FFmpeg`
  (bez ffmpeg działa fallback — binarne łączenie MP3, też grywalne)

## Instalacja

```powershell
# 1) środowisko + zależności
powershell -ExecutionPolicy Bypass -File scripts\setup.ps1

# 2) klucze
copy .env.example .env
notepad .env        # uzupełnij klucze

# 3) (opcjonalnie) ffmpeg
winget install Gyan.FFmpeg
```

## Konfiguracja Cloudflare R2 (hosting + RSS)

1. Cloudflare → **R2** → *Create bucket* (np. `daily-brief`).
2. R2 → *Manage R2 API Tokens* → utwórz token (Object Read & Write) →
   zapisz **Access Key ID** i **Secret Access Key**.
3. Bucket → *Settings* → **Public Access** → włącz **r2.dev** subdomenę
   (albo podłącz własną domenę). Skopiuj publiczny URL, np.
   `https://pub-xxxxxxxx.r2.dev`.
4. Wypełnij w `.env`: `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`,
   `R2_SECRET_ACCESS_KEY`, `R2_BUCKET`, `R2_PUBLIC_BASE_URL`.

> Account ID znajdziesz w panelu R2 (prawy panel / URL `https://<ACCOUNT_ID>.r2.cloudflarestorage.com`).

## Test (bez chmury / krok po kroku)

```powershell
# tylko skrypt (bez audio, bez uploadu) — szybki test danych + LLM
scripts\run_brief.ps1 --skip-audio --local

# skrypt + audio lokalnie (bez R2)
scripts\run_brief.ps1 --skip-publish

# pełny bieg lokalnie (RSS i MP3 zapisane w output\)
scripts\run_brief.ps1 --local
```

Wszystkie artefakty lądują w [output/](output/):
`dossier_*.json`, `research_*.txt`, `script_*.txt/json`, `brief_*.mp3`, `feed.xml`,
oraz log `run_*.log`.

Przydatne flagi do iteracji bez palenia tokenów:
`--reuse-dossier` (pomija zbieranie danych), `--reuse-script` (pomija Opus).

## ☁️ Chmura — GitHub Actions (zalecane, działa bez Twojego PC)

Codzienny bieg odpala się na serwerach GitHuba — komputer może być wyłączony.
Workflow: [.github/workflows/daily-brief.yml](.github/workflows/daily-brief.yml).

1. Załóż **prywatne** repo na GitHub i wypchnij projekt:
   ```bash
   git init && git add . && git commit -m "DAILY_BRIEF"
   git branch -M main
   git remote add origin https://github.com/<ty>/daily-brief.git
   git push -u origin main
   ```
   (`.env`, `output/`, MP3 są w `.gitignore` — nie trafią do repo.)
2. Repo → **Settings → Secrets and variables → Actions → New repository secret** —
   dodaj każdy osobno: `ANTHROPIC_API_KEY`, `PERPLEXITY_API_KEY`, `XAI_API_KEY`,
   `FRED_API_KEY`, `CNB_API_KEY` (opcjonalny), `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`,
   `R2_SECRET_ACCESS_KEY`, `R2_BUCKET`, `R2_PUBLIC_BASE_URL`.
3. Repo → **Actions** → *Daily Brief* → **Run workflow** (ręczny test). Przy błędzie
   pobierz artefakt `brief-debug-*` (log + skrypt + research).
4. Dalej leci sam wg crona (domyślnie 03:00 UTC ≈ 05:00 latem / 04:00 zimą).

Ważne:
- **Stan feedu jest w R2** (`episodes.json`), nie na runnerze — RSS pamięta
  poprzednie odcinki mimo że runner GitHuba jest jednorazowy.
- **Cron jest w UTC i „best-effort"** (potrafi się spóźnić 5–30 min) — stąd bufor.
  Godzinę zmienisz w pliku workflow (linia `cron:`).
- Scheduled workflow **wyłącza się po 60 dniach bez aktywności** w repo — wystarczy
  od czasu do czasu commit / ręczny run.
- **edge-tts z IP datacenter** bywa throttlowany (403). Jest retry; gdyby zaczął
  zawodzić — dorzucę fallback Azure Speech (te same głosy Marek/Zofia).

## Alternatywa: lokalnie na Windows (PC musi działać o tej godzinie)

```powershell
# pełny bieg (upload do R2 + aktualizacja RSS)
scripts\run_brief.ps1

# zadanie w Harmonogramie Windows: codziennie o 06:00, z wybudzaniem PC
powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1 -At 06:00

# test zadania od razu:
Start-ScheduledTask -TaskName DailyBrief
```

## Subskrypcja na telefonie

Po pierwszym pełnym biegu w logu znajdziesz **RSS feed URL**
(`https://pub-xxxx.r2.dev/feed.xml`). Dodaj go w aplikacji podcastowej:

- **Pocket Casts**: Profile → Add by URL *(zalecane do auta)*
- **Apple Podcasts**: Biblioteka → … → *Dodaj podcast przez URL*
- **Overcast / AntennaPod**: Add URL / „+"

Ustaw auto-pobieranie nowych odcinków rano — telefon ściągnie brief sam.
*(Spotify nie pozwala dodać dowolnego RSS-a ręcznie — użyj jednej z powyższych.)*

## Koszty (orientacyjnie / dzień)

| Składnik | Koszt |
|---|---|
| Claude Opus 4.8 (skrypt ~40 min) | ~$1–2 |
| Perplexity Sonar Pro (8 zapytań) | ~$0.05–0.20 |
| xAI Grok (5 grup x_search + priority + web ≈ 8 wywołań) | ~$0.25–0.70 |
| edge-tts, FRED, Bundesbank, ECB, CNB, BlueGamma, Yahoo, CoinGecko | **darmowe** |
| Cloudflare R2 (10 GB free) | **darmowe** |

Chcesz taniej? W [config.yaml](config.yaml) zmień `claude.model` na
`claude-sonnet-4-6`.

## Najważniejsze parametry ([config.yaml](config.yaml))

- `general.brief_target_minutes` — długość (domyślnie 40), `words_per_minute` — tempo
- `editions` — wersje językowe z tego samego researchu (domyślnie **PL + EN**); każda
  ma własny `prompt`, `voice` i flagę `apply_pronunciations`. Oba odcinki lądują w
  jednym feedzie (PL i `[EN]` codziennie). Usuń edycję = jeden język.
  - `format: monologue | dialogue` (toggle per edycja). **dialogue** = rozmowa dwóch
    analityków (kwestie `[[A]]`/`[[B]]`) czytana **dwoma głosami** (`voices.A`/`voices.B`;
    PL Marek+Zofia, EN Andrew+Ava), prompt z `dialogue_prompt`. **monologue** = jeden
    głos (`voice`) + `prompt`. Styl dialogu: gęsto, dwóch ekspertów, bez small-talku.
- `voice.name` — domyślny/zapasowy głos; `voice.rate` np. `-5%`; `voice.pronunciations`
  — słownik wymowy (stosowany tylko gdy `apply_pronunciations: true`, czyli dla PL)
- `sections` — kolejność, `title`/`title_en`, docelowe minuty (steruje też długością)
- `grok.topic_groups` — grupy tematów X (każda grupa = osobny, głębszy `x_search`
  po całym X; rates ma własny pass; keywordy/cashtagi ostrzą recall);
  `grok.priority_handles` — lista „never-miss" (auto-batchowana po ≤20 kont)
- `grok.enable_image_understanding` — Grok czyta wykresy/tabele w postach;
  `grok.dedup_topics_from_priority` — tematy pomijają core (czyste odkrywanie)
- `news.deny_domains` (globalny denylist) + `news.allow_domains.{cee,crypto,ai_tech}`
  (allowlisty jakościowych źródeł per temat) — sterowanie jakością Perplexity
- **Okno czasu Perplexity/Groka**: wt–pt = ostatnie **24h**, poniedziałek = **48h**
  (weekend). Sterowane w `dailybrief/util.py` (`compute_window`); Perplexity w pon.
  używa `search_after_date_filter` (brak kubełka 48h), Grok dostaje `from_date`
- `calendar.min_impact` / `calendar.cee_supplement` — zakres agendy na dziś
- `markets.*` — uniwersum symboli (źle działający symbol jest pomijany, nie wywala biegu)

## Troubleshooting

- **„Missing/placeholder env var …"** — uzupełnij `.env`.
- **Brak/nieaktualne rentowności CEE** — DE z Bundesbanku (fallback ECB), cash govie
  PL/CZ/HU z walidowanego snapshotu/CNB → miesięcznego FRED/OECD (Stooq wyłączony,
  padł ~2026-06-05). Dzienny odczyt tylko gdy świeży (≤7 dni). Dzienny obraz CEE
  niesie krzywa swapowa (BlueGamma). Strojenie: bloki `cee_yields` / `swaps` / `fx`
  w `config.yaml`.
- **FX** — domyślnie deterministycznie z **ECB** (EUR/USD, USD/PLN, EUR/PLN, USD/JPY,
  DXY z koszyka); gdy ECB zawiedzie dla danej pary, używany jest fallback z Yahoo.
- **Audio „skacze" na złączeniach** — zainstaluj ffmpeg (czyste łączenie).
- **edge-tts błąd / throttling** — uruchamiasz za często; bieg dzienny jest OK.
- **Zadanie nie odpala przy uśpionym PC** — w zadaniu jest `-WakeToRun`, ale
  sprawdź w Windows: *Zasilanie → uśpienie → zezwól zegarom wybudzania*.
- Pełny log: `output\run_YYYYMMDD.log`.

## Architektura (pliki)

```
run_brief.py                 # orkiestrator + CLI
config.yaml                  # cała konfiguracja
prompts/system_brief.md      # persona + format dla Opus
dailybrief/
  config.py  util.py
  aggregate.py               # uruchamia kolektory, składa dossier + tekst kontekstu
  generate_script.py         # Opus 4.8 -> skrypt (markery sekcji, pass wydłużający)
  synthesize.py              # edge-tts -> MP3 (ffmpeg/binary concat)
  publish.py                 # feedgen RSS + upload R2 + prune
  collectors/
    market_data.py  news_perplexity.py  social_grok.py
scripts/
  setup.ps1  run_brief.ps1  install_task.ps1
```

> Trade ideas generowane przez narzędzie to materiał informacyjny/edukacyjny,
> **nie** rekomendacja inwestycyjna ani porada.
