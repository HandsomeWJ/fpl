# FPL Copycat

Automatically mirrors the transfers and chip activations of a target elite manager
(from Fantasy Football Fix "Elite XI: Team Reveal") onto your own FPL team.

Runs every 15 minutes via GitHub Actions — works even when your computer is off.

## Behaviour
- **Chip first:** if the target manager activates a chip, it is activated on your team
  before any transfers (WC/FH are attached to the transfer request; BB/TC activated directly).
- Transfers that map cleanly onto your squad are applied automatically.
- Transfers that can't be mirrored (you don't own the outgoing player, can't afford the
  incoming one, 3-per-club limit, or it would need a -4 hit) are **skipped** and reported
  as a GitHub issue on this repo (which emails you).
- Nothing is submitted after the gameweek deadline passes.

## Secrets required (Settings -> Secrets and variables -> Actions)
- `FIX_COOKIE`  — Cookie header for fantasyfootballfix.com, e.g. `sessionid=XXXX`
- `FPL_COOKIE`  — Cookie header for fantasy.premierleague.com, e.g. `pl_profile=XXXX; datadome=XXXX`
- `FPL_ENTRY`   — your FPL team id

## Maintenance
Session cookies expire every few weeks/months. When a run fails with an auth error,
an issue is opened — refresh the corresponding secret with fresh cookie values from
your browser (DevTools -> Application -> Cookies).
