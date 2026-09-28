# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

For knowledge that spans both this project and the sibling `Rakuba` project (JRA prediction
backend), see `C:\Projects\CLAUDE.md` — that file covers the data contract for
`Rakuba/backend/output/` and the weekly JV-Link publish rhythm. This file covers PostDoctor only.

Before any large design change, consult `docs/design_decisions.md` for the rationale behind past decisions.

ユーザーへの報告・応答は常に日本語で行うこと(コード自体のコメントや変数名は従来通り英語で問題ない)。

## What this is

PostDoctor analyzes how an X (Twitter) account's own posts perform (`fetch`/`analyze`/`prescribe`/
`dashboard`), and separately (`reply_scout`) finds high-engagement horse-racing posts from *other*
accounts and drafts replies that cite the sibling Rakuba project's real prediction data. It never
posts or sends anything automatically — reply_scout drafts are always sent by a human, manually.

## Commands

```powershell
# setup
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt

# own-post analytics pipeline
python main.py fetch --account <name>       # since_id diff fetch from X API
python main.py analyze --account <name> [--metric engagement_rate]   # local, no API cost
python main.py prescribe --account <name>   # diagnosis + recommended posting slots
python main.py dashboard --account <name>   # self-contained HTML dashboard
python main.py run --account <name>         # fetch -> analyze (all metrics) -> prescribe
python main.py run --all
python main.py serve --account <name>       # serves dashboard at 127.0.0.1:8765, "更新" button re-runs the pipeline

# reply_scout
python main.py reply-scout scout     --account <name>   # collect + score candidates, select top 10
python main.py reply-scout draft     --account <name>   # generate 2 reply drafts per draftable candidate
python main.py reply-scout run       --account <name>    # scout -> draft
python main.py reply-scout track     --account <name>   # refresh engagement metrics on sent replies
python main.py reply-scout status    --account <name> --id <tweet_id> --status 送信済み|見送り
python main.py reply-scout scorecard --account <name>   # score_factors + reactions -> data/<account>/score_review.csv

# tests
./venv/Scripts/python.exe -m pytest -q                          # full suite
./venv/Scripts/python.exe -m pytest tests/reply_scout/ -q        # reply_scout only
./venv/Scripts/python.exe -m pytest tests/reply_scout/test_analyzer.py::test_final_ranking_dedups_by_author -q   # single test
```

Tests never touch the real Rakuba path or `data/` — `tests/reply_scout/fixtures/rakuba_output/`
is a hermetic fixture tree standing in for `C:\Projects\Rakuba\backend\output\`.

## Architecture

Two independent 4-layer stacks live in the same `postdoctor/` package, each layer calling only the
public functions of the layer below it (no cross-layer SQL/API leakage):

- **Own-post analytics**: `fetcher/` (X API v2, since_id diff) → `storage/` (SQLite, one DB per
  account at `data/<account>/posts.db`) → `analysis/` (pandas/matplotlib heatmap, DB-free) →
  `prescription/` (Markdown diagnosis + recommended slots, exposes a structured `diagnose(df)`
  so other layers can reuse it without reparsing Markdown).
- **reply_scout**: `reply_scout/fetcher.py` (X API v2 recent search + follower enrichment, cost-
  capped by `config/keywords.json`'s `daily_read_limit`) → `reply_scout/db.py` (separate
  `data/<account>/reply_scout.db`) → `reply_scout/analyzer.py` (scoring, no DB/API access) →
  `reply_scout/prescriber.py` (Claude-drafted replies). `reply_scout/prediction_data.py` is a
  parallel layer that reads the sibling Rakuba project's real output directly.
- `dashboard/` renders both stacks into one HTML dashboard (`data/<account>/dashboard.html`) with
  a tab per stack. `server.py` is a stdlib-only local HTTP server exposing it at
  `127.0.0.1:8765`, with a `POST /api/refresh`-style action per button that reruns the relevant
  pipeline. `orchestrator.py` holds the shared scout/draft/track functions so `cli.py` and
  `server.py` call the same code (returns `list[str]` log lines; caller decides print vs. JSON).
- `config.py` resolves `Account`s from `config/accounts.yaml`, with credentials from `.env`
  (`credential_prefix` lets an account use a differently-named API app's keys).

### reply_scout's three-layer anti-fabrication defense

reply_scout's core design constraint: never let an LLM state a race result, evaluation mark, or
score — those come from Rakuba's real data only, assembled by plain Python string concatenation.

1. **Fabrication defense**: `prediction_data.find_match()` builds the entire `fact_sentence`
   (mark, score, race name, finishing position) in Python from `HorseRow`/`race_results.csv` data.
   Claude (`prescriber.py`) only ever writes a short, tone-appropriate reaction with an explicit
   system-prompt instruction to include no numbers/marks/kanji-numeral placings. Any variant that
   still matches `NUMERIC_MARK_RE` (`[0-9◎〇△×▲]` or kanji-numeral+`着`) is discarded outright
   (retried once, then falls back to a placeholder) before being concatenated with `fact_sentence`
   — never edited/truncated, since editing could leave a broken partial fact.
2. **Tense defense**: replying to a *concluded* race with pre-race-framed language in the original
   post (`前哨戦分析`/`予想`/`全頭短評`/etc., `PRE_RACE_INDICATORS` in `prediction_data.py`) is
   treated as a time mismatch and hard-skipped (`prediction_status = '時制スキップ'`, distinct
   from `'予測データ未投入'` = no horse-name match at all). Known accepted false-positive: the
   keyword scan covers the whole post, so a post mixing retrospective content with an unrelated
   forward-looking question can false-skip a good citation — accepted trade-off, not fixed.
3. **Staleness defense**: `prediction_data.compute_predictions_signature()` hashes all of Rakuba's
   prediction files; `db.save_drafts()` stores that hash per draft row. If Rakuba's predictions
   change after a draft was generated (e.g. a horse's post-position gets confirmed and the pick
   changes), the dashboard card shows a "⚠ 予測更新あり" banner and requires an extra confirm
   before the "送信済みにする" button proceeds.

**`confirmed_race_keys()` is the single source of truth for "did this race actually happen"**: a
`race_key` counts as concluded only if at least one row for it has `chakujun > 0` — never merely
because a row exists (Rakuba pre-writes placeholder rows, `uma_num=0`, before a race runs; treating
row-presence as confirmation once produced a fabricated result on an unrun race). Do not loosen
this rule. See `C:\Projects\CLAUDE.md` for the full data-contract semantics shared with Rakuba.

**Misses are cited openly, not hidden or softened** — `@rakuba_ai` is positioned as
"still-validating," so hiding misses would undermine that. `fact_sentence` branches into 4 result
categories (`CATEGORY_HIT`/`MISS_HIGH`/`MISS_LOW`/`AS_EXPECTED`, by high/low evaluation crossed
with good/bad placing) with distinct templates and a matching Claude tone instruction per category
(proud-but-modest / humble-acknowledge-miss / humble-outdone / matter-of-fact).

**Matching is horse-name-only** — a race-name-only fallback to Rakuba's own top pick was tried and
removed, because it produced citations disconnected from what the poster actually said (e.g.
citing Rakuba's pick when the poster was talking about their own pick for the same race). Names
≤3 characters require race-name/venue co-occurrence in the same post to avoid false positives.

### Drafts must be regenerated after logic changes

`prescriber.draft_top_candidates()` skips any candidate that already has a `drafts` row — it does
not know whether the existing draft reflects current logic. **After any change to
`prediction_data.py`, `prescriber.py`, or `analyzer.py`'s scoring/filtering, existing `drafts` rows
are stale and must be cleared and regenerated**, or old (possibly wrong) drafts survive silently:

```powershell
# back up first — data/backup/<timestamp>/ is gitignored, so this step is manual
Copy-Item data/<account>/reply_scout.db data/backup/<timestamp>/reply_scout.db
# then, against data/<account>/reply_scout.db:
DELETE FROM drafts;
```
Then re-run `reply-scout draft` (or `run`, which also re-scores). Note `reply-scout draft`/`run`
do not call `generate_dashboard()` — run `python main.py dashboard --account <name>` afterward (or
use the dashboard's own refresh button, which bundles scout+draft+regen but costs X API money for
the scout half).

### Live server does not hot-reload

A `serve --account <name> --no-browser` process typically stays running in the background (started
at Windows logon via a Startup-folder shortcut, not Task Scheduler — see below). Python does not
hot-reload already-imported modules, so **after editing anything under `postdoctor/` (especially
`reply_scout/`) or its config files, the running `serve` process must be restarted** for changes to
take effect — a page reload alone is not enough, and `dashboard.html` is only regenerated by
explicit calls to `generate_dashboard()`. To restart: find the PID bound to port 8765
(`netstat -ano | findstr 8765`), confirm its full command line via
`Get-CimInstance Win32_Process -Filter 'ProcessId=<pid>' | Select CommandLine` (a bare PID doesn't
confirm which account/interpreter it's running), stop it, and relaunch the same command line via
`venv\Scripts\pythonw.exe`. Always re-check `netstat` after relaunching — `Start-Process` can
return before the OS confirms the new bind, and two processes can end up racing for the port.

Note: the venv's `pythonw.exe` launcher may spawn the *base* Python install's `pythonw.exe` as a
child process (visible in `Get-Process`/`Get-CimInstance` as the system Python's path, not the
venv's) — this is expected on this machine and does not by itself mean packages are missing; the
port only binds at all if `server.py`'s eager imports (pandas/matplotlib/tweepy, imported at module
load) already succeeded. Task Scheduler registration (`Register-ScheduledTask`/`schtasks.exe`) is
denied on this machine; `scripts/install_startup_task.ps1` uses a Startup-folder `.lnk` shortcut
instead.

## Config files

- `config/accounts.yaml` — multi-account registry (`screen_name`, `user_id`, optional
  `credential_prefix`).
- `config/keywords.json` — reply_scout search keywords, `daily_read_limit`, `min_likes`,
  `top_user_lookup_limit`, `draft_top_n`, `claude_model`, `rakuba_output_dir` (path to Rakuba's
  `backend/output/`), `trusted_authors`. `draft_top_n` must be kept in sync with the dashboard's
  display count (`top_n=10`, hardcoded in `orchestrator.run_scout()`) — there is no shared constant
  enforcing this. `trusted_authors` starts curated (not empty) as of 2026-08-03; add an account only
  once it has (a) actually replied to one of our sent replies (`other_reply_count>0`, surfaced via
  the dashboard's unacknowledged-reply alert) or (b) produced impressions on `score_review.csv`
  clearly above what its follower count would predict — don't add speculatively.
- Topic specificity (formerly a `specific_terms` word list in `config/keywords.json`, now removed)
  is no longer config-driven: `analyzer._specificity()` checks candidate text against the real
  horse/race names from `prediction_data.all_horse_names()`/`all_race_names()` (the same data
  `run_scout()` already loads for the analytical-signal bonus) — a static word list would only ever
  duplicate and go stale against that live data.
- `config/ng_words.json` — two separate tiers, not interchangeable: `ng_words` (engagement-bait
  language like "いいねで"/"プレゼント" — halves the candidate's score but doesn't exclude it) and
  `solicitation_words` (paid-tip-selling language like "限定"/"有料"/"教える" — excluded from
  ranking entirely, in both `analyzer.preliminary_score()` and `final_ranking()`).
- `config/analysis_terms.json` — words like 斤量/適性/ラップ that only grant a scoring bonus when
  they co-occur with a known horse name in the same post (analysis words alone don't trigger it).

## Brand principles (drive reply_scout's design choices above)

- Never auto-send, auto-like, or auto-follow — a human always sends the final reply manually.
- Never let an LLM originate a number, mark, or result — only Python-assembled fact sentences from
  real Rakuba data.
- Disclose misses as openly as hits, just with a different tone — hiding a miss would undermine
  `@rakuba_ai`'s "still validating" positioning.
