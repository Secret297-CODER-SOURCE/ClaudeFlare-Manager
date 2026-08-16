# DEXTER ONE — Cloudflare Manager

A terminal manager for bulk Cloudflare zone operations: adding domains, cleaning DNS, CNAME templates, phishing/banned zone detection, and NS history — all in a single TUI, no web dashboard required.

![DEXTER ONE — Cloudflare Manager](assets/dashboard.png)

## Features

- **Multi-account** — work across several Cloudflare accounts at once (`dex_accounts.json`), with zones grouped by team and API keys masked in the UI.
- **Add zones** — bulk-load domains (`domains.txt`), create zones in parallel with configurable concurrency and a live progress bar.
- **DNS Cleaner** — batch cleanup/normalization of DNS records across a domain list.
- **CNAME templates** — manage a set of target CNAMEs (`dex_cnames.json`) and apply them to selected zones.
- **Live zones** — a table of all zones across accounts with filters, search, phishing/ban detection, and bulk actions (delete, apply CNAME).
- **NS history** — a log of nameserver changes with filtering and export.
- **Classic mode** — a compatible curses fallback for when Textual is unavailable or the app isn't running in a tty (`--classic`).

## Installation

```bash
python3 -m pip install -r requirements.txt
```

Requires Python 3.11+.

## Usage

```bash
python3 cloudflare_manager.py
```

By default this opens the new [Textual](https://github.com/Textualize/textual)-based TUI. To force the classic curses interface:

```bash
python3 cloudflare_manager.py --classic
```

## Navigation

| Key | Section |
| --- | --- |
| `F1` | Add zones |
| `F2` | DNS Cleaner |
| `F3` | Accounts |
| `F4` | CNAME |
| `F5` | Domains |
| `F6` | NS history |
| `F7` | Live zones |
| `F8` | Search |
| `Ctrl+R` | Refresh current screen |
| `Ctrl+Q` | Quit |

## Project layout

```
cf_manager/
├── tui.py        # Textual UI: screens, modals, panels
├── services.py   # Cloudflare API client (zones, DNS, search, phishing)
├── jobs.py       # Concurrent background job runner with progress tracking
├── storage.py    # Storage for accounts, CNAMEs, domains, and NS history
└── models.py     # Dataclasses: Account, Zone, CnameTarget, JobResult/Progress

cloudflare_manager.py  # Entry point + classic curses mode
tests/                 # pytest suite for cf_manager
```

## Data and configuration

The app keeps its state alongside itself in JSON/txt files:

- `dex_accounts.json` — Cloudflare accounts (email + API key)
- `dex_cnames.json` — CNAME target templates
- `domains.txt` / `failed_domains.txt` — domain lists to process
- `logs/` — session logs

These files contain sensitive data (API keys) — don't commit them with real values.

## Tests

```bash
pytest
```
