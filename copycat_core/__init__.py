"""copycat_core — mirror a target FPL manager's squad, lineup and chips onto your team.

Extracted from the single-file copycat.py in Phase 0 of the app roadmap. The logic
here is the asset: every module encodes something the season taught the hard way
(single-use refresh tokens, FPL's confirmed:false committing, the reveal page's
transfer LOG vs the squad, my-team chip cancellation). See CLAUDE.md.

Layout:
  settings.py  env -> Settings
  ports.py     StateStore / TokenStore / Notifier + GitHub and in-memory adapters
  fix.py       reveal-page parsing (pure) + fetch
  fpl.py       FPL auth (token rotation) and API reads
  plan.py      name matching, net diff, affordability, chip decision, hit policy
  schedule.py  price-change clock and the run gate
  execute.py   chip activation, two-phase submit + verify, lineup sync
  runner.py    orchestration (the old main()) and the CLI entry
"""

__version__ = "0.1.0"
