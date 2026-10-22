# Project: Plex Debrid Media Automation Stack

## Architecture
An automated background media aggregation and stream-resolution daemon running on the dedicated Mac Mini. It links Trakt watchlists and Plex collections directly to Debrid services (Real-Debrid / AllDebrid) for automated torrent-free streaming content resolution, accompanied by scheduled daily synchronization and log rotation.

### System Diagram & Flow
```
[Trakt Watchlist / Plex Watchlist]
                │
                ▼
      [plex_debrid Engine (main.py)]
      ├── Scrapes media releases & torrent hashes
      ├── Resolves cached streams via Debrid API
      └── Adds high-bandwidth streams to Plex Library
                │
         ┌──────┴──────┐
         ▼             ▼
   [com.plexdebrid.daemon]      [com.plexdebrid.daily-sync]
   (Continuous Daemon Service)  (Daily 4:00 AM Sync & Maintenance)
                                ├── scripts/sync_trakt_alldebrid.py
                                ├── scripts/rotate_logs.py
                                └── scripts/alerts.py
```

## Code Layout
```
/Users/markmatthews/plex_debrid/
├── main.py                          # Primary plex_debrid service runner
├── settings.json                    # Application credentials, library IDs & scraper rules
├── alerts_config.json               # Alert notification webhook settings
├── reauth_trakt.py                  # Standalone Trakt OAuth refresh/re-authorization helper
├── base/                            # Core service primitives
├── content/                         # Content providers (Plex, Trakt, Overseerr)
├── debrid/                          # Debrid service adapters (RealDebrid, AllDebrid)
├── scraper/                         # Release parsers and indexers
├── store/                           # Local state and cache management
├── ui/                              # Terminal and web UI components
├── scripts/                         # Maintenance automation
│   ├── sync_trakt_alldebrid.py      # Automated Trakt ↔ AllDebrid sync runner
│   ├── rotate_logs.py               # Log maintenance and rotation utility
│   └── alerts.py                    # Alert dispatch utility
├── sync_maintenance.log             # Daily 4:00 AM maintenance execution log
├── plex_debrid.log                  # Core daemon streaming and resolution log
├── README.md                        # Upstream project documentation
└── PROJECT.md                       # Master architecture & project state
```

## Feature Inventory
| # | Feature | Description | Status |
|---|---------|-------------|--------|
| F1 | Continuous Debrid Daemon | Real-time scraper and debrid cache resolver | ACTIVE (`com.plexdebrid.daemon`) |
| F2 | Daily Scheduled Sync | Automated 4:00 AM Trakt and AllDebrid sync | ACTIVE (`com.plexdebrid.daily-sync`) |
| F3 | Trakt Re-Authorization | Dedicated OAuth reauth tool (`reauth_trakt.py`) | ACTIVE |
| F4 | Automated Log Rotation | Daily archiving of `sync_maintenance.log` to keep storage lean | ACTIVE (`scripts/rotate_logs.py`) |
| F5 | Discord/Webhook Alerts | Status and failure alerts dispatched on sync interruptions | ACTIVE (`scripts/alerts.py`) |

---

## Environment & Operational Contracts

### 1. Host Machine & Remote Environment
* **Hardware**: Dedicated, always-on **Mac Mini** (basement).
* **Remote Access**: **RustDesk**.
* **LaunchAgent Services**:
  * `com.plexdebrid.daemon.plist`: Continuous background daemon.
  * `com.plexdebrid.daily-sync.plist`: Daily trigger at 4:00 AM Central.

### 2. Maintenance & Re-Authentication Runbook
* If Trakt tokens expire, run: `python3 /Users/markmatthews/plex_debrid/reauth_trakt.py`.
* Logs are viewable in `sync_maintenance.log` and `plex_debrid.log`.
