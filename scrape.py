#!/usr/bin/env python3
"""Fetch Fencing Singapore women's sabre rankings (Cadet/Junior/Senior) -> data.json.

Runs daily on GitHub Actions. It fails loudly (non-zero exit) if the page layout
changes or the numbers don't add up, rather than publishing wrong data.
"""
import datetime as dt
import hashlib
import json
import os
import re
import time

import requests
from bs4 import BeautifulSoup

URL = "https://my.fencingsingapore.org.sg/showranks"
CATS = ["Cadet", "Junior", "Senior"]
HEADERS = {"User-Agent": "sabre-dashboard/1.0 (personal project; 3 requests per day)"}
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def season_start(today=None):
    """Seasons start 1 June: Oct 2026 -> 2026, Mar 2027 -> 2026."""
    t = today or dt.date.today()
    return t.year if t.month >= 6 else t.year - 1


def fetch(cat, season):
    params = {
        "rankingmethod[category]": cat,
        "rankingmethod[gender]": "Female",
        "rankingmethod[weapon]": "Sabre",
        "rankingmethod[windowlength]": season,
        "commit": "Search",
    }
    for attempt in range(3):
        try:
            r = requests.get(URL, params=params, headers=HEADERS, timeout=30)
            r.raise_for_status()
            return r.text
        except requests.RequestException:
            if attempt == 2:
                raise
            time.sleep(5 * (attempt + 1))


def points(text):
    m = re.search(r"/\s*(\d+)", text)  # "2 / 1251" -> 1251 ; "-" -> 0
    return int(m.group(1)) if m else 0


def num(text):
    digits = re.sub(r"[^\d]", "", text)
    return int(digits) if digits else 0


def comp_id(a):
    return int(re.search(r"/competitions/(\d+)", a["href"]).group(1))


def parse(html):
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table")
    if table is None:
        raise ValueError("no ranking table on page")
    rows = table.find_all("tr")
    hi = None
    for i, r in enumerate(rows):
        c = r.find(["th", "td"])
        if c is not None and c.get_text(strip=True) == "Ranking":
            hi = i
            break
    if hi is None:
        raise ValueError("header row not found")

    # competition id -> date, taken from the date links above the header row
    dates = {}
    for r in rows[:hi]:
        for a in r.find_all("a", href=re.compile(r"/competitions/\d+")):
            if DATE.fullmatch(a.get_text(strip=True)):
                dates[comp_id(a)] = a.get_text(strip=True)

    head = rows[hi].find_all(["th", "td"])
    events = []  # (column index, date, event name)
    for i, c in enumerate(head):
        a = c.find("a", href=re.compile(r"/competitions/\d+"))
        if a:
            cid = comp_id(a)
            if cid not in dates:
                raise ValueError(f"no date found for competition {cid}")
            events.append((i, dates[cid], a.get_text(strip=True)))
    events.sort(key=lambda e: (e[1], e[0]))

    fencers = []
    for r in rows[hi + 1:]:
        tds = r.find_all("td")
        if len(tds) != len(head):
            continue
        link = tds[1].find("a", href=re.compile(r"/profiles/\d+"))
        if link is None:
            continue
        fencers.append({
            "id": int(re.search(r"/profiles/(\d+)", link["href"]).group(1)),
            "name": link.get_text(strip=True),
            "club": tds[2].get_text(strip=True),
            "school": tds[3].get_text(strip=True),
            "base": num(tds[-2].get_text()),
            "total": num(tds[-1].get_text()),
            "pts": [points(tds[i].get_text(" ", strip=True)) for i, _, _ in events],
        })
    return events, fencers


def check(cat, fencers):
    """Total must equal best-4 event scores + base points (true for every row checked by hand)."""
    if len(fencers) < 5:
        raise ValueError(f"{cat}: only {len(fencers)} fencers parsed")
    bad = [f["name"] for f in fencers
           if sum(sorted(f["pts"], reverse=True)[:4]) + f["base"] != f["total"]]
    if len(bad) > 0.1 * len(fencers):
        raise ValueError(f"{cat}: {len(bad)} rows fail the best-4 + base check, e.g. {bad[:3]}")
    return bad


def label(name, words):
    name = re.sub(r"\(.*?\)", "", name)
    sur, _, given = name.partition(",")
    return " ".join((sur.split() + given.split()[:words])).title()


def build(cat, season):
    events, fencers = parse(fetch(cat, season))
    bad = check(cat, fencers)
    if bad:
        print(f"WARNING {cat}: total mismatch for {bad}")
    keep = sorted((f for f in fencers if f["total"] > 0), key=lambda f: -f["total"])
    short = [label(f["name"], 1) for f in keep]
    out = [[f["id"], label(f["name"], 2 if short.count(s) > 1 else 1), f["club"], f["base"], f["pts"], label(f["name"], 99), f["school"]]
           for f, s in zip(keep, short)]
    return {"ev": [[d, n] for _, d, n in events], "f": out}


def profile_events(html):
    """Competition history from a profile page. Only the results table is read:
    the birth year, school and citizenship shown on the page are deliberately ignored."""
    soup = BeautifulSoup(html, "html.parser")
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        heads = [c.get_text(strip=True) for c in rows[0].find_all(["th", "td"])] if rows else []
        if "Finalrank" not in heads or "Competition" not in heads:
            continue
        ix = {h: i for i, h in enumerate(heads) if h}
        out = []
        for r in rows[1:]:
            tds = r.find_all("td")
            if len(tds) != len(heads):
                continue
            g = lambda h: tds[ix[h]].get_text(strip=True)
            out.append([g("Event Date"), g("Competition"), g("Location"), g("Category"),
                        g("Points For"), num(g("Finalrank")), num(g("Points"))])
        if not out:
            raise ValueError("competition table had no readable rows")
        return sorted(out, key=lambda e: e[0], reverse=True)
    raise ValueError("no competition table on profile")


def update_profiles(data, now):
    """Cache each ranked fencer's full competition history in profiles.json.
    A profile is re-fetched only when her results changed or the copy is over 7 days old.
    Failures here never block the rankings update."""
    old = {}
    if os.path.exists("profiles.json"):
        with open("profiles.json", encoding="utf-8") as fh:
            old = json.load(fh).get("p", {})
    sigs = {}
    for cat in CATS:
        for f in data[cat.lower()]["f"]:
            sigs.setdefault(str(f[0]), []).append([cat, f[4]])
    today = now.date()
    new, fetched, failed = {}, 0, 0
    for fid, sig in sigs.items():
        s = hashlib.md5(json.dumps(sig).encode()).hexdigest()[:10]
        cur = old.get(fid)
        if cur and cur["s"] == s and (today - dt.date.fromisoformat(cur["t"])).days < 7:
            new[fid] = cur
            continue
        try:
            time.sleep(2)
            r = requests.get(f"https://my.fencingsingapore.org.sg/profiles/{fid}", headers=HEADERS, timeout=30)
            r.raise_for_status()
            new[fid] = {"s": s, "t": today.isoformat(), "e": profile_events(r.text)}
            fetched += 1
        except Exception as e:  # keep the old copy rather than lose history
            failed += 1
            print(f"WARNING profile {fid}: {e}")
            if cur:
                new[fid] = cur
    if failed:
        print(f"::warning::{failed} of {len(sigs)} profile pages could not be read")
    if not os.path.exists("profiles.json") or json.dumps({"p": new}, sort_keys=True) != json.dumps({"p": old}, sort_keys=True):
        with open("profiles.json", "w", encoding="utf-8") as fh:
            json.dump({"p": new}, fh, ensure_ascii=False, separators=(",", ":"))
    print(f"profiles.json: {fetched} fetched, {failed} failed, {len(new)} total")


def save_snapshot(data, season, now):
    """One real snapshot per day: [id, base, total]. Base points shrink over time,
    so this is the only accurate record of what the site showed on each day."""
    snaps = []
    if os.path.exists("history.json"):
        with open("history.json", encoding="utf-8") as fh:
            snaps = json.load(fh).get("snaps", [])
    today = now.strftime("%Y-%m-%d")
    snap = {"d": today, "s": season}
    for cat in CATS:
        snap[cat.lower()] = [[f[0], f[3], f[3] + sum(sorted(f[4], reverse=True)[:4])]
                             for f in data[cat.lower()]["f"]]
    snaps = [x for x in snaps if x.get("d") != today] + [snap]
    with open("history.json", "w", encoding="utf-8") as fh:
        json.dump({"snaps": snaps}, fh, separators=(",", ":"))
    print("history.json:", len(snaps), "snapshots")


def main():
    season = int(os.environ.get("SEASON") or season_start())
    data = {}
    for i, cat in enumerate(CATS):
        if i:
            time.sleep(3)  # be gentle with the server
        data[cat.lower()] = build(cat, season)
    data["season"] = season
    now = dt.datetime.now(dt.timezone(dt.timedelta(hours=8)))

    old = None
    if os.path.exists("data.json"):
        with open("data.json", encoding="utf-8") as fh:
            old = json.load(fh)
    if old and {k: v for k, v in old.items() if k != "generated"} == data:
        print("data.json: no change")
    else:
        data["generated"] = now.strftime("%Y-%m-%d %H:%M")
        with open("data.json", "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, separators=(",", ":"))
        print("Updated data.json")
    save_snapshot(data, season, now)
    update_profiles(data, now)


if __name__ == "__main__":
    main()
