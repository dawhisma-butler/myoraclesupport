# Acceptance run — 2026-10-CPU (executed 2026-11-09 on the exploratory Mac)

## 1. Login and token (criterion 1)
`scripts/login_once.py` / `opatchbot login`: unattended with `MOS_TOTP_SEED` (no human input); session saved;
token minted. Live test `tests/integration/test_token_live.py` (saved session → token, no login): **passed**.

## 2. Resolve (criterion 2)
`opatchbot --config ~/.mos/local-config.yaml --today 2026-10-21 resolve 2026-10-CPU` with the full example inventory:

| item | status | patch | description | released | files | reason (abridged) |
|---|---|---|---|---|---|---|
| pt-8.61 | resolved | 40014041 | PT 8.61.24 PRODUCT PATCH DPK | 2026-10-19 | 4 (6.5 GB) | newest PT 8.61 product DPK |
| pt-8.62 | resolved | 40014036 | PT 8.62.14 PRODUCT PATCH DPK | 2026-10-19 | 4 (6.5 GB) | newest PT 8.62 product DPK |
| ptmw-8.61 | resolved | 40136983 | PT-MW-DPK | 2026-10-26 | 1 (3.4 GB) | newest middleware DPK |
| ptmw-8.62 | resolved | 40136978 | PT-MW-DPK | 2026-10-26 | 1 (5.4 GB) | newest middleware DPK |
| hcm-9.2 | resolved | 40016683 | PEOPLESOFT RELEASE PATCHSET: HCM - 10282026 | 2026-10-28 | 1 | newest in window; also matched 40016823 |
| fscm-9.2 | resolved | 40219247 | PEOPLESOFT RELEASE PATCHSET: FSCM - 10292026 | 2026-10-28 | 1 | newest in window; also matched 40189810, 40157090 |
| cs-9.2 | resolved | 39715828 | PEOPLESOFT CS UPDATE IMAGE 9.2.039 - NATIVE OS | 2026-09-28 | 11 (22.4 GB) | newest image (released before window) |
| db19-ru | resolved | 39915792 | DATABASE RELEASE UPDATE 19.33.0.0.0 | 2026-10-10 | 1 (2.4 GB) | newest (released before window) |
| db19-ojvm | resolved | 39803650 | OJVM RELEASE UPDATE 19.33.0.0.0 | 2026-09-17 | 1 | newest (released before window) |
| db19-jdk | resolved | 40192005 | JDK BUNDLE PATCH 19.0.0.0.261023 | 2026-11-03 | 1 | newest |
| db19-opatch | resolved | 6880880 | OPatch 12.2.0.1.55 for DB 19.0.0.0.0 (Oct 2026) | — | 1 | current OPatch for 19 |
| db21-ru | resolved | 40017330 | DATABASE RELEASE UPDATE 21.24.0.0.0 | 2026-10-08 | 1 (1.6 GB) | newest (released before window) |
| db21-ojvm | **missing** | | | | | no `^OJVM RELEASE UPDATE 21\.` among the 150 newest 21c Linux patches |
| db21-jdk | resolved | 40192133 | JDK BUNDLE PATCH 21.0.0.0.261023 | 2026-10-25 | 1 | newest |
| db21-opatch | resolved | 6880880 | OPatch 12.2.0.1.55 for DB 21.0.0.0.0 (Oct 2026) | — | 1 | current OPatch for 21 |

Operator to confirm: cs-9.2 as the update image; whether 21c OJVM still ships (if not, drop the item).

## 3. Fetch (criterion 3)
Full set ≈ 49 GB; the Mac had 13 GB free, so the fetch acceptance used the inventory subset
pt-8.61 (hardlink reuse from a seeded prior-cycle folder holding the files verified earlier today),
hcm-9.2, fscm-9.2, db19-ru/ojvm/jdk/opatch, db21-ru/ojvm/jdk/opatch (≈ 4.9 GB of downloads).
The excluded items (pt-8.62, ptmw-*, cs-9.2) use the same resolve/fetch code path and resolved correctly above.

Fetch log: appended below after the run (`~/.mos/accept-run1.log`, `~/.mos/accept-run2.log`).

## 4. Spike (session longevity)
`deploy/com.mocs.opatchbot-spike.plist` runs `opatchbot token` daily at 09:00 and appends
`date | action | exit + expiry` to `spike-log.md`. The operator loads it (one line in the plist header);
the agent that built this was not permitted to install scheduled jobs. Decision rule is at the top of `spike-log.md`.

## Fetch log (2026-11-09 23:14–23:17 local)
Run 1 (`--today 2026-10-21`): email `[opatchbot] 2026-10-CPU: partial` — "Verified now: 13; already present: 0; failed: 0";
not found: db21-ojvm. 9 files downloaded (≈ 4.9 GB) and SHA-256 verified; the 4 PeopleTools 8.61 files were
hardlinked from the seeded prior cycle (same inode confirmed) with no download. Exit 1 (partial: one expected item missing).
Run 2 (`--today 2026-10-22`): "0 verified, 13 present, 0 failed", no email (nothing changed), exit 1. `opatchbot status`
shows the cycle `partial` with that last result. Criterion 3 met for the subset; re-run did no network transfer beyond the searches.
