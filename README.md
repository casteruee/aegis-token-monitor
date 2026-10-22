# Aegis Token Monitor

A local, read-only desktop dashboard that watches the **quota and balance** of four LLM
providers in one window: **Codex**, **z.ai (GLM)**, **MiniMax** and **DeepSeek**.

It answers one question — *"how much headroom do I have left right now?"* — without ever
calling a metered completion endpoint and without storing any credential.

## What it does

- Four provider cards with 5-hour window / weekly window usage, reset time and collection time.
- DeepSeek wallet card (balance, plus optionally "already spent" — only when a top-up record
  exists and its basis matches the current balance).
- Local SQLite history of observations, 7-day activity view, detail and history pages,
  JSON backup/restore.
- Strict data-honesty rules baked in: `null ≠ 0`, a cached value keeps the **original**
  collection timestamp, a balance change is never reported as token consumption, 5h and
  weekly windows are never summed, percentages from different providers are never added.

## Tech stack

| Layer | Choice |
| --- | --- |
| Language | Python 3.12 |
| GUI | PySide6 6.9.1 (Qt Widgets), hand-written theme, no Qt Designer files |
| Transport | stdlib `urllib` (proxy aware), stdlib `sqlite3`, `json` |
| Tests | `pytest` — fixtures ship with the repo, no network needed |

## Layout

```
aegis/
  providers/        codex.py · zai.py · minimax.py · deepseek.py   (one per source)
  services/         monitor / contracts / validation / proclife / topup
  history/          SQLite store + 7-day activity
  desktop/          theme / adapter / widgets / pages / app
  model.py normalize.py net.py cache.py credentials.py ui.py
run_gui.py  run_gui.bat     # desktop entry
run.py      run.bat         # CLI entry (--json for machine-readable output)
docs/                       # architecture, acceptance, UI acceptance, decisions
tests/                      # unit + integration tests with offline fixtures
tools/                      # self-check and UI smoke helpers
config/topup.json.example   # optional top-up ledger for DeepSeek "spent"
```

## Install & run (Windows)

```bat
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-ui.txt
.venv\Scripts\python.exe run_gui.py
```

CLI:

```bat
.venv\Scripts\python.exe run.py           :: live table
.venv\Scripts\python.exe run.py --json    :: machine-readable snapshot
```

External prerequisite: `codexbar-cli.exe` (CodexBar) for the Codex card. The path is
resolved from the `CODEXBAR_CLI` environment variable, falling back to
`%LOCALAPPDATA%\Programs\CodexBar\codexbar-cli.exe`. If it is missing, the Codex card
degrades to an explicit error state instead of crashing.

## Credentials — read-only, never printed

The tool never asks for a key. It reads existing local files **read-only**:
`~/.cc-switch/cc-switch.db` (SQLite opened with `mode=ro`), `~/.pi/agent/auth.json`,
`~/AppData/Local/hermes/config.yaml` (the `sk-` segment is extracted by regex because the
stored value may carry a prefix). Keys live in memory only; they are never logged, written
to the cache, or included in `to_dict()` output. A `NO_SECRET_LEAK` self-check is part of
the acceptance record.

## Current limitations

- **Not a token counter.** Provider APIs expose quota percentages and balance, not token
  counts; polling cannot convert one into the other.
- **DeepSeek "spent" is conditional.** The API has no lifetime-spend field. "Spent" appears
  only when `config/topup.json` records a cumulative top-up consistent with the current
  balance; otherwise the card shows `—` with an explanation.
- **Windows-oriented.** Paths and `.bat` launchers assume Windows; the provider modules
  themselves are portable.
- **Unofficial endpoints.** The z.ai / MiniMax / DeepSeek usage endpoints are the ones their
  web consoles call; they can change without notice. A provider that changes shape degrades
  to an error card rather than to a wrong number.
- **No tray, no autostart, no packaging.** Explicitly out of scope for this version.

## Verification status

From `docs/ACCEPTANCE.md` (independent acceptance run, not the coding agent's self-report):

- `pytest tests -q` → **341 passed** (v0.1 frozen baseline).
- Live panel vs. raw provider sources at the same moment → **4/4 MATCH**.
- `tools/selfcheck.py` → `NO_SECRET_LEAK: PASS`, `OVERALL: PASS`.
- Cache invariants (a failure never overwrites, a stale entry keeps its own timestamp,
  live and cached values are never merged) → 3/3 PASS.
- Git hygiene: runtime data (`probe_raw/`, `cache/`, `logs/`) never committed.

The published snapshot is the last committed state of `v0.4`
(`feat(v0.4.2): aurora usage-bar palette`). A small uncommitted console-window fix that
existed in the working tree is **not** included, because it had not been verified.

## Publication note

This copy was prepared for public release: absolute paths containing a local user name were
replaced with `%USERPROFILE%`, the hard-coded CLI path became an environment variable, and
real account balance figures inside the acceptance docs were replaced with placeholder
values. Test counts, verdicts and defect records were left untouched.

## License

No license has been declared for this repository yet, which means all rights are reserved by
default. If you want to reuse it, ask first.
