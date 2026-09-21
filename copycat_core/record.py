"""What a run decided and did, as JSON the app can read.

Two outputs, both committed by the workflow's persist step:

  data/preview/latest.json   overwritten by EVERY run (live or dry): the plan the copycat
                             computed against the live squad and reveal page. With the
                             hourly forced dry run this is the "what will the next run
                             do" panel, never more than an hour stale.
  data/ledger/<ts>_gw<N>.json  one file per LIVE run that did or attempted something:
                             transfers applied, hits taken, chips played or held, lineup
                             changes, NEWLY reported skips, failures. The app's ledger.
                             A run that merely repeats an already-reported skip writes
                             no file (2026-09-21: 10 identical "1 skipped" files a day).

The record is built incrementally by runner.run(); nothing here talks to FPL.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Optional

from .log import log


@dataclass
class RunRecord:
    ts: str = ""
    kind: str = "preview"            # preview (dry) | run (live)
    event_id: Optional[int] = None
    deadline: Optional[str] = None
    dry: bool = True
    entry: Optional[int] = None
    target: str = ""
    fix: dict = field(default_factory=dict)      # updated, chips, transfers, captain, vice, squad
    team: dict = field(default_factory=dict)     # bank, free_transfers, made, squad names, chips
    plan_note: str = ""
    pairs: list = field(default_factory=list)    # net out/in name pairs
    to_apply: list = field(default_factory=list) # {out,in,out_id,in_id,sell,cost,role,enables}
    skipped: list = field(default_factory=list)  # {out,in,reason}
    # skips reported for the first time in this run (state["notified"] had not seen them);
    # a repeat of an already-reported skip is not a ledger event
    new_skips: list = field(default_factory=list)  # ["Out -> In: reason", ...]
    # downgrades still in force from earlier runs: we hold `have` where the target has
    # `held`, and the net diff deliberately does not buy `held` back
    held_downgrades: list = field(default_factory=list)  # {gw,held,have,enabled}
    # enabling downgrades the planner found for unaffordable transfers; `applied` is False
    # in the default recommend-only mode (owner approves with ALLOW_DOWNGRADE=1)
    recommendations: list = field(default_factory=list)  # {enables,out,in,sell,cost,drop,shortfall,applied}
    hits: dict = field(default_factory=lambda: {"count": 0, "points": 0})
    chips_activated: list = field(default_factory=list)
    transfer_chip: Optional[str] = None
    chip_held: Optional[dict] = None             # {chip, why}
    lineup: str = ""
    result: str = "nothing"                      # nothing | dry | applied | failed | early-exit
    early_exit: str = ""
    log: list = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=1, ensure_ascii=False)


def _write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def write_preview(rec: RunRecord, data_dir: str) -> str:
    path = os.path.join(data_dir, "preview", "latest.json")
    _write(path, rec.to_json())
    return path


def write_ledger(rec: RunRecord, data_dir: str) -> Optional[str]:
    """Only live runs with something to record become ledger entries."""
    if rec.dry:
        return None
    acted = (rec.to_apply or rec.chips_activated or rec.transfer_chip or rec.chip_held
             or rec.new_skips or rec.result == "failed" or rec.lineup.startswith("XI/bench"))
    if not acted:
        return None
    stamp = rec.ts.replace(":", "").replace("-", "")[:15]
    path = os.path.join(data_dir, "ledger", f"{stamp}_gw{rec.event_id or 0}.json")
    _write(path, rec.to_json())
    return path


def safe_write_records(rec: RunRecord, data_dir: str, report_lines: list) -> None:
    """Write both files; never let a recording problem fail the run."""
    try:
        rec.log = list(report_lines)
        rec.ts = rec.ts or datetime.now(timezone.utc).isoformat(timespec="seconds")
        p = write_preview(rec, data_dir)
        log(f"[record] preview -> {os.path.relpath(p, os.path.dirname(data_dir))}")
        lp = write_ledger(rec, data_dir)
        if lp:
            log(f"[record] ledger  -> {os.path.relpath(lp, os.path.dirname(data_dir))}")
    except Exception as e:
        log(f"[record] failed: {e}")
