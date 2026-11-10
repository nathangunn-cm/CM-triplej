# Roster radio spin tracker

Counts spins of Community Music roster tracks on triple j, Double J and triple j Unearthed, using ABC's play-history feed (the same log behind the "recently played" lists on the triple j site).

- **Daily** (around 6am Melbourne): collects the last two days of plays and updates the dashboard.
- **Mondays** (around 7:30am Melbourne): also emails the 7-day summary to the team.
- **Dashboard**: open any time; filter by 7 / 28 / 90 days, by station, or by artist; click a track to see every spin with date and time.

## One-time setup (about 20 minutes)

1. **Create a GitHub repo** (public is fine) and upload everything in this folder, including the hidden `.github` folder.
2. **Turn on the dashboard**: repo **Settings → Pages → Build and deployment**, set Source to *Deploy from a branch*, branch `main`, folder `/docs`. GitHub shows the URL (something like `https://<account>.github.io/<repo>/`).
3. **Add the dashboard URL for the email**: **Settings → Secrets and variables → Actions → Variables**, add `DASHBOARD_URL`.
4. **Add the email settings** under **Secrets** (same page):

   | Secret | Example |
   |---|---|
   | `SMTP_HOST` | `smtp.office365.com` or `smtp.gmail.com` |
   | `SMTP_PORT` | `587` |
   | `SMTP_USER` | the sending mailbox |
   | `SMTP_PASSWORD` | its password or app password |
   | `EMAIL_FROM` | usually the same as `SMTP_USER` |
   | `EMAIL_TO` | comma-separated list, e.g. `nathan@…, james@…` |

   If you send from a Microsoft 365 mailbox, IT may need to enable "Authenticated SMTP" for it. A dedicated Gmail account with an [app password](https://support.google.com/accounts/answer/185833) is the quickest alternative.
5. **Backfill**: **Actions → Radio spin sweep → Run workflow**, set days to `90`, tick *Send the summary email* to test delivery.

## Day to day

- **Add or remove tracks**: edit `roster.csv` in GitHub (pencil icon). Two columns, `artist,track`, spelled as they appear on the release. Small differences (capitals, "feat.", "(Radio Edit)", "&" vs "and") are handled automatically; remixes are counted separately unless you list the remix title.
- **Change recipients**: update the `EMAIL_TO` secret.
- **Run a sweep now**: Actions → Run workflow.
- **Change stations**: edit `stations` in `config.json` (known IDs: `triplej`, `doublej`, `unearthed`).

## Notes

- The dashboard page carries a `noindex` tag so search engines won't list it. Anyone with the link can still open it, and the repo itself is visible on GitHub if public.
- The ABC feed isn't an official public API and could change. If a sweep fails, the Actions tab shows the error and nothing is overwritten.
- Plays aired off vinyl or CD (mostly late-night specialist shows) may not be logged by ABC, so treat counts as a floor.
- History is kept in `data/plays.json` from the day you start, so trends build up over time.
