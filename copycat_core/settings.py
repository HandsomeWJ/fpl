"""Environment -> Settings. The only place that reads os.environ."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping, Optional


def _flag(env: Mapping[str, str], key: str) -> bool:
    return env.get(key) == "1"


@dataclass
class Settings:
    entry: Optional[int]
    target: str
    dry: bool
    debug: bool
    # Owner decision 2026-09-12: copying Tom's transfers before the daily price change
    # outranks avoiding -4s, so the workflow defaults ALLOW_HITS=1 on the TEST account.
    # Revisit before pointing this at the main account.
    allow_hits: bool
    # WC/FH are whole-squad chips; normally played only when the mirror converges
    # exactly. This overrides that rule.
    allow_transfer_chip: bool
    # Exclude the target's N most recent transfers from the target squad, so their
    # latest move can be tested on its own instead of arriving with a catch-up batch.
    hold_latest: int
    # Set by the external scheduler's dispatch: behave like cron (gated).
    scheduled: bool
    event_name: str
    fix_cookie: Optional[str]
    fpl_refresh_seed: Optional[str]
    fpl_cookie: Optional[str]
    gh_repo: Optional[str]
    gh_token: Optional[str]
    gh_pat: Optional[str]
    state_path: str
    check_pat: bool
    dump_reveal: bool
    # data/ next to state/: daily price snapshots (see snapshot.py)
    data_dir: str = ""
    snapshot_force: bool = False
    # When a clock tick is gated out, still compute a dry-run plan once an hour so the
    # app's preview stays fresh. PREVIEW_HOURLY=0 turns it off.
    preview_hourly: bool = True

    @property
    def is_clock(self) -> bool:
        """True when this run was started by a clock (cron or the external scheduler)
        rather than a human, and must therefore go through the gate."""
        return self.event_name == "schedule" or self.scheduled

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ,
                 state_path: Optional[str] = None) -> "Settings":
        entry = env.get("FPL_ENTRY")
        return cls(
            entry=int(entry) if entry else None,
            target=env.get("TARGET_MANAGER", "Tom Dollimore"),
            dry=_flag(env, "DRY_RUN"),
            debug=_flag(env, "DEBUG"),
            allow_hits=_flag(env, "ALLOW_HITS"),
            allow_transfer_chip=_flag(env, "ALLOW_TRANSFER_CHIP"),
            hold_latest=int(env.get("HOLD_LATEST_TRANSFERS") or 0),
            scheduled=_flag(env, "SCHEDULED"),
            event_name=env.get("GITHUB_EVENT_NAME", ""),
            fix_cookie=env.get("FIX_COOKIE"),
            fpl_refresh_seed=env.get("FPL_REFRESH_TOKEN"),
            fpl_cookie=env.get("FPL_COOKIE"),
            gh_repo=env.get("GITHUB_REPOSITORY"),
            gh_token=env.get("GITHUB_TOKEN"),
            gh_pat=env.get("GH_PAT"),
            state_path=state_path or os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "state", "state.json"),
            check_pat=_flag(env, "CHECK_PAT"),
            dump_reveal=_flag(env, "DUMP_REVEAL"),
            data_dir=os.path.join(os.path.dirname(os.path.dirname(
                state_path or os.path.join(os.path.dirname(os.path.dirname(
                    os.path.abspath(__file__))), "state", "state.json"))), "data"),
            snapshot_force=_flag(env, "SNAPSHOT_FORCE"),
            preview_hourly=env.get("PREVIEW_HOURLY", "1") != "0",
        )
