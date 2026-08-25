# FPL Copycat — project context

## What this is
Automation that mirrors the FPL transfers and chip activations of elite manager
**"Tom Dollimore"** (as revealed on https://www.fantasyfootballfix.com/reveal/, Elite XI
Team Reveal) onto the owner's own FPL team. Runs on GitHub Actions
(repo `HandsomeWJ/fpl`, workflow `.github/workflows/copycat.yml`) so it works while the
owner's machine is off. The cron fires hourly but is gated — see [Schedule](#schedule-changed-2026-08-25).

## Hard requirements (agreed with the owner)
1. **If Tom activates a chip, activate it on our team BEFORE any transfers.**
   WC/FH are attached to the transfer POST itself; BB/TC are activated first via the
   my-team endpoint. This ordering is critical.
2. Fully automatic — no confirmation before submitting.
3. Transfers that don't map cleanly (we don't own the out-player, can't afford the
   in-player, 3-per-club violation, would need a -4 hit, ambiguous player name) are
   SKIPPED and reported as a GitHub issue on this repo. Never take -4 hits automatically.
4. Nothing is submitted after the GW deadline; Fix's "Gameweek N" transfer label must
   match the open FPL gameweek.

## Files
- `copycat.py` — the whole pipeline (fetch Fix reveal HTML → parse chips/transfers →
  read FPL team → chip first → map → two-phase POST → report). Well-commented.
- `test_local.py` — mocked end-to-end test (`python3 test_local.py`, needs
  requests + beautifulsoup4). Keep it passing.
- `state/state.json` — dedup state, committed back by the workflow after each run.

## Auth (2026/27 FPL site — cookies pl_profile/datadome are gone)
- FPL uses OIDC: authority `https://account.premierleague.com/as`, public client id
  `bfcbaf69-aade-4c1b-8f00-c1cb8a193030`, token endpoint
  `https://account.premierleague.com/as/token`, refresh_token grant.
  API auth = `Authorization: Bearer <access_token>` (no cookies needed; verified).
  The script refreshes tokens itself and persists rotation in the repo Actions
  variable `FPL_TOKENS` — which needs the `GH_PAT` secret, NOT `GITHUB_TOKEN`; see
  [Token rotation](#token-rotation--the-thing-that-broke-and-why-read-before-touching-auth).
- Repo secrets: `FIX_COOKIE` (full Cookie header for fantasyfootballfix.com, contains
  Django `sessionid`), `FPL_REFRESH_TOKEN` (seed, from localStorage key
  `oidc.user:https://account.premierleague.com/as:<client_id>` on
  fantasy.premierleague.com), `FPL_COOKIE` (legacy fallback, may be stale),
  `FPL_ENTRY` (currently `7953181` = the owner's TEST account).
- Transfer payload (validated against community libs + owner's old code):
  POST `/api/transfers/` with `confirmed:false` (validation) then `confirmed:true`,
  body `{entry, event, transfers:[{element_in, element_out, purchase_price,
  selling_price}], wildcard, freehit, bench_boost, triple_captain, chip}`.

## Fix reveal parsing (server-rendered HTML, no API)
Section = `.reveal-section` containing the manager's name. Chips: `.rchip` →
`.rchip__chip` (WC1/WC2/TC/FH/BB) + `.rchip__status` (available/active/…).
Transfers: `li.rtransfers__transfer` → two `.rtransfers__player`, each with
`.rtransfers__status` (Out/In); the player name is the element text minus the status
word. NOTE: transfer rows were only ever observed empty (GW1 had no transfers) — the
name-extraction should be sanity-checked against the first real transfer that appears.

## Status (2026-08-25) — WORKING, end-to-end verified
Auth, token rotation and persistence all confirmed green. Runs 32851077765 /
32851200296 / 32851411701 proved the full loop: refresh → rotate → persist to
`FPL_TOKENS` → next run reads it. All 191 stale failure issues closed; 0 open.
Still to do: let GW's first real Tom transfer land on the test account, then switch
`FPL_REFRESH_TOKEN` + `FPL_ENTRY` to the main account.

### Token rotation — the thing that broke, and why (read before touching auth)
**FPL's OIDC issues one-time-use refresh tokens.** Each refresh returns a NEW
refresh_token and invalidates the old one immediately, so the rotation MUST be
persisted or the automation dies after exactly one run.

`save_tokens()` writes the rotation to the repo Actions variable `FPL_TOKENS`.
**`GITHUB_TOKEN` cannot write Actions variables**, regardless of `permissions:` —
it fails with `403 "Resource not accessible by integration"`. That API needs a PAT
with *Variables: read and write*, supplied as the `GH_PAT` secret. This failure was
silent for four days: every run logged the 403 and still exited 0.

Now: `_gh_headers(admin=True)` uses `GH_PAT`; a failed persist opens an issue AND
exits non-zero. Run `workflow_dispatch` with `check_pat=1` to probe whether `GH_PAT`
can write variables **without spending an FPL token** — use this when the PAT expires
(fine-grained PATs cap at 1 year; this one was issued 2026-08-25).

**Seed from a private/incognito window.** The browser session and the automation are
competing consumers of the same rotating chain — whichever refreshes last invalidates
the other. A private window gets its own independent chain. Log in there, copy the
refresh token, set the secret, then close the window **without logging out** (logout
revokes it server-side). Seeding from the everyday session means normal browsing will
silently kill the automation's token.

**Never run `gh variable list` on this repo.** It prints `FPL_TOKENS` in full,
including a live refresh token. Variables are not masked the way secrets are. Use
`gh variable list --json name,updatedAt` instead. (This happened on 2026-08-25; the
token was invalidated by forcing a rotation — set `expires_at` to 0 via
`gh api … --jq .value | jq -c '.expires_at = 0' | gh variable set FPL_TOKENS`, then
dispatch a run, which spends and replaces the exposed token.) Storing rotation state
in a *secret* would be masked, but needs libsodium/PyNaCl to write.

- GW2 deadline is 2026-08-28T17:30Z. Tom has no pending transfers and his BB was GW1
  (spent), so nothing has been missed.

### Schedule (changed 2026-08-25)
The cron fires **hourly, plus :45 past 22 and 23 UTC**, but `should_run_now()` in
`copycat.py` makes almost every run a no-op. Real work happens only:
- in the **150 min before the price change** (`PRICE_LEAD_MIN`), and
- in the **6h before the open gameweek's deadline** (`DEADLINE_WINDOW_H`).

`workflow_dispatch` always bypasses the gate.

**In the owner's clock (SGT), which is how they think about this:**

| | Summer (BST) | Winter (GMT) |
|---|---|---|
| Price change | 07:00 SGT | 08:00 SGT |
| Runs before it | 05:00, 06:00, 06:45 | 06:00, 06:45, 07:00, 07:45 |

So 3-4 runs on a quiet day, plus ~6 on a deadline day.

**FPL price changes moved to MIDNIGHT UK time for 2026/27** - the old 01:30 GMT /
02:30 BST rule is gone. `minutes_to_price_change()` computes it from
`Europe/London`, so the SGT times shift by themselves at the DST switch instead of
drifting an hour twice a year. It falls back to fixed 21:00/22:00 UTC slots if
tzdata is missing on the runner. Several slots rather than one because GitHub
routinely delays scheduled runs and sometimes drops them entirely; the last slot is
~15 min before the change, which closes the gap where a transfer by Tom could
otherwise be mirrored after prices moved.

The gate reads the deadline from the **unauthenticated** bootstrap endpoint, so a
skipped run spends no refresh-token rotation. It **fails open**: if the deadline
can't be read the run proceeds, because a wasted run is much cheaper than a missed
deadline. Do not make it fail closed.

### Test account
7953181 HAS a saved 15-player squad (verified 2026-08-25 via public API: 15 picks in
GW1, 67 pts). Tom's GW1 BB was never mirrored because auth was already broken —
expected, no action needed.

## Conventions
- Never log or commit token/cookie values; secrets stay in GitHub Actions secrets.
- Keep failures loud: any auth/parse failure must open a GitHub issue (notify()).
- Owner's timezone is Asia/Singapore; FPL deadlines are UTC in the API.
