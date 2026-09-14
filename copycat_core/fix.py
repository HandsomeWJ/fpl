"""The target manager's reveal page on fantasyfootballfix.com (server-rendered HTML).

parse_reveal() is pure - HTML in, dict out - so it can be tested against saved pages.
"""

from __future__ import annotations

import re

import requests
from bs4 import BeautifulSoup

from .log import log

FIX_URL = "https://www.fantasyfootballfix.com/reveal/"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36")


def _find_section(soup: BeautifulSoup, name: str):
    for s in soup.select(".reveal-section"):
        if name.lower() in s.get_text(" ").lower():
            return s
    return None


def parse_reveal(html: str, name: str) -> dict:
    """Chips, transfer log, gameweek, full 15, XI/bench order and armbands for `name`.

    Two things about this page that are easy to get wrong:

    * The transfer list is a CHRONOLOGICAL LOG of everything the manager did this
      gameweek, not their net change. Never replay it - diff the squad instead.
    * The card has three faces and five pitches. Only `.flip-card-front` is the
      manager's real team; the back faces are manager-vs-consensus and
      manager-vs-my-squad comparisons. A bare `.fffPitch` selector silently reads
      the CONSENSUS XI.
    """
    soup = BeautifulSoup(html, "html.parser")
    section = _find_section(soup, name)
    if section is None:
        raise RuntimeError(
            f"Manager '{name}' not found on the reveal page - Fix session cookie may have "
            "expired, or the manager list changed.")

    chips = {}
    for chip in section.select(".rchip"):
        label = chip.select_one(".rchip__chip")
        status = chip.select_one(".rchip__status")
        if label and status:
            chips[label.get_text(strip=True)] = status.get_text(strip=True).lower()

    gw = None
    rt = section.select_one(".rtransfers")
    if rt:
        m = re.search(r"Gameweek\s+(\d+)", rt.get_text(" ", strip=True))
        if m:
            gw = int(m.group(1))

    transfers = []
    for li in section.select(".rtransfers__transfer"):
        players = li.select(".rtransfers__player")
        out_name = in_name = None
        for p in players:
            status = p.select_one(".rtransfers__status")
            txt = p.get_text(" ", strip=True)
            if not status:
                continue
            stat = status.get_text(strip=True).lower()
            pname = txt.replace(status.get_text(strip=True), "").strip()
            if stat == "out":
                out_name = pname
            elif stat == "in":
                in_name = pname
        if out_name and in_name:
            transfers.append({"out": out_name, "in": in_name})

    squad, starters, bench, captain, vice = [], [], [], None, None
    front = section.select_one(".flip-card-front")
    if front:
        bench_el = front.select_one(".fffBench")
        bench_nodes = set(id(d) for d in bench_el.find_all(True)) if bench_el else set()
        for el in front.select(".fffPitchElement"):
            t = el.select_one(".fffElementText")
            if not t:
                continue
            nm = t.get_text(" ", strip=True)
            if not nm:
                continue
            squad.append(nm)
            # DOM order is the lineup order: pitch rows GK->DEF->MID->FWD, then bench
            # with the reserve keeper first, which is exactly FPL's position ordering.
            (bench if id(el) in bench_nodes else starters).append(nm)
            # Armbands are title attributes, not classes.
            for d in el.find_all(True):
                tag = (d.get("title") or d.get("aria-label") or "").strip().lower()
                if tag == "captain":
                    captain = nm
                elif tag == "vice-captain":
                    vice = nm

    updated = None
    m = re.search(r"Last Updated:\s*([^F]+?)(?:FPL|$)", section.get_text(" ", strip=True))
    if m:
        updated = m.group(1).strip()
    return {"chips": chips, "transfers": transfers, "updated": updated, "gw": gw,
            "squad": squad, "starters": starters, "bench": bench,
            "captain": captain, "vice": vice}


def fetch_reveal_html(cookie: str, http=requests) -> str:
    r = http.get(FIX_URL, headers={"User-Agent": UA, "Cookie": cookie}, timeout=60)
    r.raise_for_status()
    return r.text


def fetch_fix_manager(name: str, cookie: str, http=requests) -> dict:
    return parse_reveal(fetch_reveal_html(cookie, http=http), name)


def dump_reveal_structure(name: str, cookie: str, http=requests,
                          max_lines: int = 250, max_depth: int = 6) -> bool:
    """Print the tag/class tree of the target's reveal section.

    Structure only - tags, classes, truncated text - because the repo is public and
    its workflow logs are public with it. Enough to write a parser for the full XI
    without republishing the page.
    """
    soup = BeautifulSoup(fetch_reveal_html(cookie, http=http), "html.parser")
    section = _find_section(soup, name)
    if section is None:
        log(f"[dump] no .reveal-section matched '{name}'")
        return False

    classes: dict[str, int] = {}
    for el in section.find_all(True):
        for c in el.get("class", []):
            classes[c] = classes.get(c, 0) + 1
    log("[dump] classes in section: "
        + ", ".join(f"{c}x{n}" for c, n in sorted(classes.items(), key=lambda kv: -kv[1])))

    n = [0]

    def walk(el, depth=0):
        if depth > max_depth or n[0] >= max_lines:
            return
        for child in el.find_all(recursive=False):
            if n[0] >= max_lines:
                log("[dump] ... truncated")
                return
            cls = ".".join(child.get("class", []))
            own = " ".join(t.strip() for t in child.find_all(string=True, recursive=False)
                           if t.strip())[:70]
            log(f"[dump] {'  ' * depth}<{child.name}{'.' + cls if cls else ''}>"
                + (f"  {own!r}" if own else ""))
            n[0] += 1
            walk(child, depth + 1)

    walk(section)

    front = section.select_one(".flip-card-front")
    log(f"[dump] flip-card-front found: {bool(front)}")
    if front:
        pitches = front.select(".fffPitch")
        benches = front.select(".fffBench")
        els = front.select(".fffPitchElement")
        log(f"[dump] front: {len(pitches)} pitch, {len(benches)} bench, "
            f"{len(els)} pitch elements")
        pitch = pitches[0] if pitches else None
        bench = benches[0] if benches else None
        for i, el in enumerate(els):
            t = el.select_one(".fffElementText")
            nm = t.get_text(" ", strip=True)[:20] if t else "?"
            where = "BENCH" if (bench and el in bench.find_all(True)) else \
                    ("PITCH" if (pitch and el in pitch.find_all(True)) else "?")
            inner = sorted({c for d in el.find_all(True) for c in d.get("class", [])})
            title_attrs = [d.get("title") or d.get("aria-label") for d in el.find_all(True)
                           if d.get("title") or d.get("aria-label")]
            log(f"[dump]   [{i:>2}] {where} {nm:<20} classes={inner} "
                f"titles={title_attrs}")
    return True
