# Pomysły rozwoju statusline

Zweryfikowane pomysły z przeglądu kodu (czerwiec 2026). Każde źródło danych zostało
potwierdzone w oficjalnej dokumentacji statusline
(<https://code.claude.com/docs/en/statusline>) lub w realnych plikach transkryptów.

Wdrożone w `statusline.py` (dla porządku): deduplikacja usage po `message.id`,
osłony przed crashami (statusline nigdy nie drukuje tracebacka), git w worktree /
submodułach / podkatalogach repo, jednoprzebiegowy parser transkryptu z przyrostowym
cache'em offsetu, timeout + scalenie wywołań gita, burn rate `$/h`, kolorowanie %
kontekstu, segment `+N/-N` linii, effort z payloadu (`effort.level`), czas sesji z
`cost.total_duration_ms`, agregacja transkryptów subagentów/workflow
(`<katalog-transkryptu>/<id-sesji>/**/*.jsonl`) i wycena per wpis z `message.model`
(sesje mieszają modele — bez tego szacunek nie zgadzał się z oficjalnym
`total_cost_usd` nawet 5x przy sesjach z workflow), rozbicie kosztu per rodzina
modelu obok nazwy modelu (`+Fable 13%`), wycena fast mode po `usage.speed`, oraz
tygodniowe okna per model (`F:5%`) z `GET /api/oauth/usage` — odświeżane przez
odłączony proces (`--refresh-usage`) do wspólnego cache'u w tmpdir, TTL 10 min,
z lockiem przeciw równoległym zapytaniom; render nigdy nie czeka na sieć,
a `STATUSLINE_NO_USAGE_API=1` wyłącza to całkowicie. Własne szacunki kosztu
(`~$…` oraz `+Fable 13%`) są domyślnie **wyłączone** — włącza je
`STATUSLINE_ESTIMATE_USAGE=1`; bez niej linia pokazuje wyłącznie oficjalny
`cost.total_cost_usd` i burn rate, czyli nic, co mogłoby się rozjechać
z realnym rozliczeniem, gdy tabela stawek się zestarzeje.

## Do zrobienia

### 1. Git ahead/behind vs upstream + badge PR — wartość: średnia, nakład: mały
Strzałki `↑2 ↓1` z jednego dodatkowego wywołania:
`git rev-list --left-right --count @{upstream}...HEAD` (pomijane, gdy brak upstreamu).
Uwaga: wyjście `git status --porcelain -b` (już używane) zawiera `[ahead N, behind M]`
w nagłówku `##` — można sparsować bez dodatkowego procesu.
Dodatkowo pola `pr.number` / `pr.review_state` / `pr.url` z payloadu, gdy istnieje
otwarty PR — opcjonalnie jako klikalny link OSC 8 (dokumentacja jawnie wspiera
klikalne linki w statusline).

### 2. Lepsza etykieta sesji — wartość: średnia, nakład: mały
Obecnie: ostatni opis `Task` ze skanu transkryptu (często nieaktualny opis subagenta).
Lepsza kolejność: `session_name` z payloadu (ustawiane przez `--name` / `/rename`) →
wpis `{"type":"ai-title","aiTitle":...}` z transkryptu (automatyczny tytuł sesji
nadawany przez Claude Code; zweryfikowany w realnych plikach) → dopiero obecna
heurystyka Task.

### 3. Koszt dzienny ze wszystkich sesji („today: $12.40") — wartość: średnia, nakład: średni
Agregacja dzisiejszego wydatku ze wszystkich `~/.claude/projects/*/*.jsonl`
(filtr po mtime). Klocki już istnieją w skrypcie: deduplikacja po `message.id`,
wycena per `message.model` i przyrostowy cache per plik — zostaje pętla po sesjach
z dzisiejszym mtime i współdzielony plik cache z totalami dziennymi. Najbardziej
wartościowe dla użytkowników planów Max — mapuje się wprost na limity.

### 4. Udział czekania na API („api 38%") — wartość: niska, nakład: mały
`cost.total_api_duration_ms / cost.total_duration_ms` — ile czasu sesji to czekanie
na model, a ile praca człowieka. Czysta arytmetyka na payloadzie, zero I/O.
Ukrywać poniżej kilku minut sesji.

### 5. Tryb kompaktowy wg szerokości terminala + konfiguracja env — wartość: średnia, nakład: średni
Claude Code ustawia zmienne `COLUMNS`/`LINES` przed uruchomieniem skryptu
(v2.1.153+; `tput cols` NIE działa, bo wyjście jest przechwytywane). Gdy linia
przekracza `COLUMNS`, zrzucać segmenty o niskim priorytecie w kolejności:
task → tokeny → szczegóły gita → czas. Konfiguracja przez zmienne środowiskowe
w stylu istniejącego `STATUSLINE_DEBUG`, np. `STATUSLINE_HIDE=tokens,task`,
`STATUSLINE_COMPACT=1` — ustawiane w bloku `env` settings.json bez edycji kodu.
Dokumentacja wspiera też wiele linii wyjścia (tryb verbose).

### 6. Dodatkowe badge stanu — wartość: niska, nakład: mały
Warunkowe, tylko gdy nie-domyślne: `output_style.name` (gdy != "default"),
`vim.mode` (gdy vim włączony; współgra z udokumentowanym `hideVimModeIndicator`),
`agent.name` (sesje `--agent` — istotne przy wielu terminalach z różnymi agentami).
Każde to jednolinijkowy `.get()` z wyświetlaniem tylko przy obecności pola.

## Notatki techniczne

- **Stały koszt ~50 ms na render to start interpretera Pythona** (zmierzono:
  `python3 -c pass` ≈ 29 ms, z importami ≈ 49 ms; `python3 -S` ≈ 19 ms).
  Po wdrożeniu cache'u to dominujący koszt. Opcje: wskazanie szczuplejszego
  interpretera w komendzie statusline (np. `python3 -S -E` — skrypt nie używa
  pakietów zewnętrznych), zysk ~10–20 ms/render. Przepisanie na język kompilowany
  nieuzasadnione przy ciepłym cache'u.
- **`statusline-cmd.sh` to wersja legacy** — ma zaktualizowany cennik, ale żadnej
  z powyższych poprawek (podwójne liczenie tokenów, brak cache'u, git w worktree,
  crashe). Docelowo: usunąć albo przeportować zmiany z wersji Python.
- **Payload statusline nie zawiera limitów per model.** Zweryfikowane w binarce
  2.1.252: `rate_limits` składane jest wyłącznie z `five_hour`, `seven_day`
  i `spend_limit`. Kubełki `seven_day_opus`, `seven_day_sonnet` i `model_scoped[]`
  istnieją tylko w protokole `/usage`, a snapshot limitów żyje w pamięci procesu
  (z nagłówków odpowiedzi) i nie trafia na dysk — stąd własne odpytywanie
  `/api/oauth/usage`. W odpowiedzi liczy się `limits[]`: wpisy `kind:"weekly_scoped"`
  z `scope.model.display_name`, bo klucze `seven_day_*` bywają `null` na kontach,
  które mają tylko kubełek Fable.
