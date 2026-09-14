"""Run log.

Every line printed during a run is also kept in `report_lines`, which becomes the
body of the GitHub issue (or other notification) the run opens. Kept as a module-level
list on purpose: the runner, the planner and the notifier all contribute to one report.
"""

report_lines: list[str] = []


def log(msg: str) -> None:
    print(msg, flush=True)
    report_lines.append(msg)


def reset_report() -> None:
    report_lines.clear()
