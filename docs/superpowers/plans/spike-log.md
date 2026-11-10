# Session spike log

Decision rule: 7/7 daily headless token fetches succeed → goal 5 (rare re-auth) holds as designed.
Any `SessionExpired` within 7 days → add the "release day tomorrow, run `opatchbot login`" reminder path before go-live.

| date (local) | action | result |
|---|---|---|
| 2026-11-09 22:51 | `scripts/login_once.py` unattended (username+password+TOTP seed) | login ok; session.json written; token expires 23:27 |
| 2026-11-09 22:53 | live test: saved session → fetch_token (no login) | token returned, no sign-in; same JWT as login (Oracle returns current token until expiry) |
| 2026-11-09 23:0x | unattended login via TOTP (after fixing: SPA waits, decoy JWT, Generate-token click) | login ok; token exp 23:55; search+download HTTP 200 |
| 2026-11-09 23:0x | live test: saved session → fetch_token (no login) | passed |
