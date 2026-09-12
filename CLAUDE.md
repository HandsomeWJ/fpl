# FPL Copycat — project context

## What this is
Automation that mirrors the FPL transfers and chip activations of elite manager
**"Tom Dollimore"** (as revealed on https://www.fantasyfootballfix.com/reveal/, Elite XI
Team Reveal) onto the owner's own FPL team. Runs on GitHub Actions
(repo `HandsomeWJ/fpl`, workflow `.github/workflows/copycat.yml`) so it works while the
owner's machine is off. The cron fires every 15 min but is gated — see
[Schedule](#schedule-changed-2026-08-25).

## Hard requirements (agreed with the owner)
1. **If Tom activates a chip, activate it on our team BEFORE any transfers.**
   WC/FH are attached to the transfer POST itself; BB/TC are activated first via the
   my-team endpoint. This ordering is critical.
2. Fully automatic — no confirmation before submitting.
3. Transfers that don't map cleanly (can't afford the in-player, 3-per-club violation,
   ambiguous player name) are SKIPPED and reported as a GitHub issue on this repo.
   **-4 hits ARE taken automatically** (`ALLOW_HITS` defaults to 1 in the workflow) —
   owner decision 2026-09-12: landing Tom's transfers before the daily price change
   outranks avoiding hits. This is set for the TEST account; decide again before
   switching `FPL_ENTRY` to the main account. Pass `allow_hits=0` to suppress.
4. Nothing is submitted after the GW deadline; Fix's "Gameweek N" transfer label must
   match the open FPL gameweek.

## Files
- `copycat.py` — the whole pipeline (fetch Fix reveal HTML → parse chips/transfers →
  read FPL team → chip first → map → two-phase POST → report). Well-commented.
- `test_local.py` — mocked end-to-end test (`python3 test_local.py`, needs
  requests + beautifulsoup4). Keep it passing.
- `state/state.json` — dedup state, committed back by the workflow after each run.
  The persist step **retries and then fails red**; it must never swallow a push
  failure, because losing this file makes a later run report an already-mirrored
  transfer as "I don't own X".

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

### The full 15 (this is the source of truth for mirroring)
`.flip-card-front .fffPitchElement` → `.fffElementText` gives the manager's squad,
15 names. **Anchor to `.flip-card-front`.** The card has three faces and five pitches
in total: the front is the manager's real team; the two back faces hold
manager-vs-consensus and manager-vs-my-squad comparisons. A bare `.fffPitch` selector
silently reads the CONSENSUS XI instead — wrong team, no error.

### The transfer list is a LOG, not a diff — do not replay it
`li.rtransfers__transfer` → two `.rtransfers__player` with `.rtransfers__status`
(Out/In). This is a **chronological log of everything the manager did this gameweek**,
not their net change. Under a Free Hit (unlimited transfers) it fills up with
reversals and repeats — real example, GW3 2026-09-04:

```
B.Fernandes -> Enzo      <- bought Enzo
Enzo -> B.Fernandes      <- changed their mind
B.Fernandes -> Cherki    <- B.Fernandes leaves AGAIN
```

**An FPL transfers batch is not a sequence.** Every entry is validated against the
CURRENT squad, not applied in order, so a payload holding both `A->B` and `B->A`
contradicts itself and the WHOLE batch is rejected:

```
400 {"transfers":[{},{"element_in":[{"code":"transfer_element_in_is_pick"}],
                     "element_out":[{"code":"transfer_element_out_not_pick"}]},{},...]}
```

This broke mirroring silently from 2026-09-02 to 2026-09-04: every live run 400'd and
applied nothing, while the runs still looked like ordinary reported failures.

**Fix: mirror by NET DIFF, never by replaying the log** (`plan_net_transfers`). Diff
the live squad against the parsed 15 and transfer straight to the destination,
ignoring the route. Pairs are matched within position, which is always possible
because both sides are valid FPL squads and so share positional shape. This is also
naturally idempotent and self-correcting: a partial application just produces a
smaller diff next run.

If the squad can't be parsed or resolved, the fallback replays the log — but
`net_out_transfer_log` cancels round trips first, so even the degraded path can't
submit a self-contradicting batch.

### A picks POST without a chip key CANCELS an active 3xc/bboost (found GW4, 2026-09-12)
`POST /api/my-team/{entry}/` with `{"picks": [...]}` and no `chip` key deactivates a
pending Triple Captain / Bench Boost. Transfer chips (FH/WC) live on the transfers
endpoint and are unaffected — which is why FH survived the lineup save in GW3 and TC
did not in GW4: activated at step 1, wiped by the lineup save at step 4, and the run
still reported success. `sync_lineup` now re-sends the active my-team chip
(`team_chip`), and chip activation checks **live** status instead of skipping on
`state["chips_done"]`, so an undone chip is redone rather than trusted as done.

## Status (2026-09-12) — mirroring verified live in GW3 and GW4; scheduler is the open risk
GW3: 12 net transfers applied unattended, squad matches Tom exactly, Free Hit played
automatically on convergence. Auth, token rotation and persistence green since
2026-08-25. Only remaining step: switch `FPL_REFRESH_TOKEN` + `FPL_ENTRY` to the
MAIN account (currently the test account, 7953181).

**Before switching to the main account, decide these test-account defaults again:**
- `ALLOW_HITS` defaults to **1** in the workflow (owner decision 2026-09-12, so Tom's
  transfers land before the daily price change even when they cost -4). On the main
  account that is real points — likely set back to 0, or add a per-GW cap.
- `HOLD_LATEST_TRANSFERS`, `ALLOW_TRANSFER_CHIP` are 0 by default and fine as-is.

### Scheduler — LIVE since 2026-09-12 12:45Z (cron-job.org → workflow_dispatch)
Job "FPL copycat dispatch (every 15 min)" on the owner's cron-job.org account, using a
dedicated fine-grained PAT `fpl-scheduler` (repo `HandsomeWJ/fpl` only, *Actions: read
and write*, **no expiration** — chosen deliberately so the clock cannot silently expire).
First firing landed 16s after schedule with `SCHEDULED: 1` and a `[gate]` line. If runs
stop arriving, check cron-job.org's job HISTORY first (it records the HTTP status of each
call; 204 = accepted), then the PAT.

#### Original decision record
**Finding (verified):** GitHub Actions `schedule` is best-effort and is dropping most
firings. GW4 deadline day, 2026-09-12: **3 of ~48** scheduled runs fired; **none between
09:11Z and the 12:30Z deadline** — a 3h blackout covering the moment Tom activated TC
and made his transfer (11:23Z). Nothing mirrored until a manual dispatch at 12:07Z.
Earlier data: `0 * * * *` ≈13% delivery, `*/15` ≈54%; off-the-hour minutes did not fix it.

**Contrast:** every `workflow_dispatch` this project has ever sent fired within
seconds. Dispatch is an API call, not a queued schedule item.

**Recommended fix:** an external scheduler (e.g. cron-job.org) POSTing every 15 min to
`https://api.github.com/repos/HandsomeWJ/fpl/actions/workflows/copycat.yml/dispatches`
with body `{"ref":"main"}` and a PAT holding *Actions: read and write*. Keep the cron
as a fallback only. The existing gate makes the extra firings free (one unauthenticated
GET when there is nothing to do).

**Setup (cron-job.org, free):** every 15 min, `POST` to
`https://api.github.com/repos/HandsomeWJ/fpl/actions/workflows/copycat.yml/dispatches`
with headers `Authorization: Bearer <fpl-scheduler PAT>`, `Accept: application/vnd.github+json`,
`Content-Type: application/json`, `User-Agent: fpl-copycat-scheduler`, and body
`{"ref":"main","inputs":{"scheduled":"1"}}`. Success is **204 No Content**. The PAT is a
dedicated fine-grained token, repo `HandsomeWJ/fpl` only, permission *Actions: read and
write* — kept separate from `GH_PAT` so neither scope leaks into the other.

**`inputs.scheduled=1` is essential.** It makes the dispatch go through
`should_run_now()` like cron. Without it every dispatch is treated as a human run and
bypasses the gate: 96 full runs a day, each an FPL auth and a Fix fetch. Verified
2026-09-12: a `scheduled=1` dispatch logs a `[gate]` line; a plain one does not.

**Top priority is copying Tom's transfers before the daily price change.** In SGT the
work window is 04:37–06:52 (summer) / 05:37–07:52 (winter), ~10 firings, last one
8 min before prices move. Prices change only at midnight UK, so a transfer Tom makes at
any time of day is bought at the same price he paid as long as one of those firings
runs — the scheduler's job is to make that certain.

**Verify after setup:** `gh run list -R HandsomeWJ/fpl --event workflow_dispatch` should
show a run every ~15 min whose log has `SCHEDULED: 1`. If the cron fallback ever
matters again, the ratio of `schedule` to `workflow_dispatch` runs shows it.

**If the scheduler is down:** on deadline day dispatch manually inside the last few
hours — `gh workflow run copycat.yml -R HandsomeWJ/fpl` — and do not assume anything ran.

### How automatic mirroring works (the whole point of the project)
Every gated run, with no human in the loop:
1. Parse Tom's **full 15** from the reveal page front face.
2. **Diff** it against the live squad → net transfers (never replay the log).
3. Apply what's affordable, retrying deferred ones as sales free up cash
   (`bank_left` is order-dependent — see below).
4. Play BB/TC immediately if Tom has them active; play **WC/FH only if the projected
   squad equals Tom's exactly** (`squad_converges`).
5. Submit, then verify against the live squad before believing any rejection.

**It is idempotent.** The diff is recomputed from real squads every run, so a partial
application simply yields a smaller diff next time, and a fully-mirrored squad yields
`[plan] already matching` and does nothing. There is no state to corrupt.

**What legitimately stops it**, all reported as issues rather than failing silently:
- (Only if `allow_hits=0`) no free transfers left and no chip → skipped rather than a -4.
- Genuinely unaffordable after all sales are applied.
- 3-per-club violation, unavailable player, ambiguous name.
- Squad won't converge → WC/FH held back with the exact difference named.

### Order-dependent affordability (fixed 2026-09-01)
Each swap is funded by its own sale plus the bank, so a cash-releasing transfer later
in the list can pay for tight ones earlier in it. Evaluating once, in list order,
discarded transfers the squad could afford: selling Mbeumo (8.0) funds both Wissa
(6.1) and Gvardiol (5.6), but those were checked first against 6.0 and 5.0 and
dropped — 1 of 3 applied when all 3 fitted with 1.2 to spare. Affordability failures
are now **deferred and retried until a pass applies nothing**.

### Two-phase submit: `confirmed:false` COMMITS (fixed 2026-08-29)
FPL's submit is not validate-then-commit. The `confirmed:false` call already applies
the transfer, so the `confirmed:true` confirm is a duplicate and is rejected with
`element in is already picked` / `element out is not a current pick` — errors that
describe the state the FIRST call created. Three separate successful runs were
reported as FAILED before this was understood. `verify_transfers_applied` now re-reads
the squad before believing any rejection, and fails safe if it can't.

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

- GW2 (deadline 2026-08-28T17:30Z) passed with no Tom transfers to mirror, so nothing
  was missed — but see the scheduling note below: the deadline window had zero runs.

### Schedule (changed 2026-08-25)
The cron fires **every 15 min at :07/:22/:37/:52**, but `should_run_now()` in
`copycat.py` makes almost every run a no-op. Real work happens only:
- in the **150 min before the price change** (`PRICE_LEAD_MIN`), and
- in the **6h before the open gameweek's deadline** (`DEADLINE_WINDOW_H`).

`workflow_dispatch` always bypasses the gate.

**In the owner's clock (SGT), which is how they think about this:**

| | Summer (BST) | Winter (GMT) |
|---|---|---|
| Price change | 07:00 SGT | 08:00 SGT |
| Work window | 04:37 - 06:52 SGT | 05:37 - 07:52 SGT |

The last firing lands ~8 min before the change.

**GitHub's cron is not reliable enough for a deadline-driven tool — full stop.**
Off-the-hour helped but did not fix it. GW4 deadline day (2026-09-12, deadline
12:30Z): **3 scheduled firings all day out of ~48**, and **zero between 09:11Z and
the deadline** — the exact 3h in which Tom activated TC and made a transfer (11:23Z).
Nothing mirrored until a manual dispatch at 12:07Z. `workflow_dispatch` events are
API calls, not best-effort queue items, and have never been dropped here; the fix is
an external scheduler (e.g. cron-job.org) POSTing
`/repos/HandsomeWJ/fpl/actions/workflows/copycat.yml/dispatches` every 15 min with
a PAT that has Actions: read/write. Keep the cron as a fallback only.

**Never schedule this on the hour.** GitHub delays scheduled runs under load and
:00 is peak congestion. Measured here: `0 * * * *` delivered **13%** of its firings
and dropped **every** slot in the GW2 deadline window (2026-08-28) — the copycat did
no work at all inside the 6h before that deadline, and only escaped missing a
transfer because Tom happened to make none. `*/15` delivered ~54%. Fire often, at
off-peak minutes, and let the gate keep it cheap — a skipped run is one
unauthenticated GET and spends no token rotation.

**FPL price changes moved to MIDNIGHT UK time for 2026/27** - the old 01:30 GMT /
02:30 BST rule is gone. `minutes_to_price_change()` computes it from
`Europe/London`, so the SGT times shift by themselves at the DST switch instead of
drifting an hour twice a year. It falls back to fixed 21:00/22:00 UTC slots if
tzdata is missing on the runner.

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
