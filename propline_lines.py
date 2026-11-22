"""Collect NBA player PRA (points + rebounds + assists) lines from PropLine.

Source:  https://api.prop-line.com/v1  (free key: 1,000 requests/day, player props included)
Auth:    API key from the environment variable PROPLINE_API_KEY (never put a key in code).
Market:  player_points_rebounds_assists
Cost:    1 request for the whole slate (bulk endpoint); per-event fallback only if bulk has no props.

CLI:     PROPLINE_API_KEY=... python propline_lines.py --out lines_full.json
Module:  fetch_all(key) -> (records, notes);  primary_lines(records) -> {normalized name: {...}}
"""
import argparse, datetime as dt, json, os, re, sys, time, unicodedata

BASE = "https://api.prop-line.com/v1"
SPORT = "basketball_nba"
MARKET = "player_points_rebounds_assists"
PREFERRED = ["draftkings", "fanduel"]   # the only books used, in this order of preference
ALLOWED_BOOKS = set(PREFERRED)
DFS_BOOKS = {"prizepicks", "underdog", "sleeper", "dabble", "pick6"}  # pick'em apps: not two-way sportsbook prices


def norm_name(n):
    n = unicodedata.normalize("NFKD", str(n or "")).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z ]", "", n.replace("-", " ")).strip()


def api_get(path, key, params=None, tries=4):
    """GET with retries. 429/5xx honour Retry-After; 401/403/404 fail immediately with a clear message."""
    import requests
    last = None
    for i in range(tries):
        try:
            r = requests.get(BASE + path, params=params, timeout=30,
                             headers={"X-API-Key": key, "Accept": "application/json", "User-Agent": "pra-lines/1.0"})
        except requests.RequestException as e:
            last = str(e)[:120]
            time.sleep(2 ** i * 2)
            continue
        meta = {k: r.headers.get(k) for k in ("X-Daily-Limit", "X-Daily-Used", "X-Daily-Remaining")}
        print(f"GET {path} -> {r.status_code} {meta}", file=sys.stderr)
        if r.status_code == 200:
            return r.json(), meta
        if r.status_code in (429, 502, 503, 504):
            last = f"HTTP {r.status_code} {r.text[:80]!r}"
            try:
                wait = float(r.headers.get("Retry-After") or 2 ** i * 2)
            except ValueError:
                wait = 2 ** i * 2
            time.sleep(min(wait, 60))
            continue
        raise RuntimeError(f"PropLine HTTP {r.status_code}: {r.text[:160]!r}")
    raise RuntimeError(f"PropLine unreachable after {tries} tries ({last})")


def events_from(j):
    if isinstance(j, list):
        return j
    if isinstance(j, dict):
        for k in ("events", "data"):
            if isinstance(j.get(k), list):
                return j[k]
        if "bookmakers" in j:
            return [j]
    return []


def parse_events(events, retrieved_at):
    """Event objects (the-odds-api style) -> one record per event/book/player/line with Over and Under prices."""
    recs = {}
    for ev in events:
        eid = str(ev.get("id") or "")
        for bk in ev.get("bookmakers") or []:
            bkey = str(bk.get("key") or "").lower()
            if bkey not in ALLOWED_BOOKS:
                continue
            for m in bk.get("markets") or []:
                if m.get("key") != MARKET or m.get("line_type") == "milestone":
                    continue
                group = {}
                for o in m.get("outcomes") or []:
                    side = str(o.get("name") or "").lower()
                    player = o.get("description")
                    if side not in ("over", "under") or not player or o.get("point") is None:
                        continue
                    try:
                        pt = float(o["point"])
                    except (TypeError, ValueError):
                        continue
                    g = group.setdefault((norm_name(player), pt), {
                        "player": player, "player_id": o.get("player_id"), "team": o.get("player_team"),
                        "line": pt, "over_price": None, "under_price": None})
                    g[side + "_price"] = o.get("price")
                for g in group.values():
                    recs[(eid, bkey, norm_name(g["player"]), g["line"])] = {
                        "event_id": eid, "home_team": ev.get("home_team"), "away_team": ev.get("away_team"),
                        "commence_time": ev.get("commence_time"), "book": bkey, "book_title": bk.get("title") or bkey,
                        "dfs": bkey in DFS_BOOKS, "market": MARKET, "line_type": m.get("line_type") or "unknown",
                        "last_update": m.get("last_update"), "retrieved_at": retrieved_at, **g}
    return list(recs.values())


def _implied(price):
    try:
        p = float(price)
    except (TypeError, ValueError):
        return None
    return 100 / (p + 100) if p > 0 else -p / (-p + 100)


def _balance(rec):
    a, b = _implied(rec.get("over_price")), _implied(rec.get("under_price"))
    return abs((a if a is not None else b if b is not None else 0) - 0.5) if (a is not None or b is not None) else 9


def main_lines(recs):
    """One main line per event/book/player: the book's own 'main' row, else the most balanced two-way price."""
    groups = {}
    for r in recs:
        groups.setdefault((r["event_id"], r["book"], norm_name(r["player"])), []).append(r)
    out = []
    for rows in groups.values():
        mains = [r for r in rows if r["line_type"] == "main"] or rows
        out.append(min(mains, key=_balance))
    return out


def _et_date(t):
    try:
        from zoneinfo import ZoneInfo
        return dt.datetime.fromisoformat(str(t).replace("Z", "+00:00")).astimezone(ZoneInfo("America/New_York")).date().isoformat()
    except Exception:
        return str(t)[:10]


def _rank(r):
    if r["book"] in PREFERRED:
        return PREFERRED.index(r["book"])
    return 50 if not r["dfs"] else 99


def primary_lines(recs):
    """Per player: the line from the first available book in PREFERRED order, for each game date (US Eastern).
    Returns {normalized name: {"line","book","books","over_price","under_price" (earliest date),
                               "by_date": {date: {"line","book","books","over_price","under_price"}}}}"""
    per = {}
    for r in main_lines(recs):
        per.setdefault(norm_name(r["player"]), {}).setdefault(_et_date(r.get("commence_time")), []).append(r)
    out = {}
    for k, days in per.items():
        by = {}
        for d, rows in days.items():
            r = min(rows, key=_rank)
            by[d] = {"line": r["line"], "book": r["book_title"], "books": len({x["book"] for x in rows}),
                     "over_price": r["over_price"], "under_price": r["under_price"]}
        out[k] = dict(by[min(by)], by_date=by)
    return out


def fetch_all(key, now=None):
    """Returns (records, notes). Bulk call first (1 request); per-event fallback if the bulk reply has no PRA rows."""
    now = now or dt.datetime.now(dt.timezone.utc)
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    notes = []
    j, meta = api_get(f"/sports/{SPORT}/odds", key, {"markets": MARKET})
    recs = parse_events(events_from(j), stamp)
    if meta.get("X-Daily-Remaining"):
        notes.append(f"requests left today: {meta['X-Daily-Remaining']}")
    if recs:
        return recs, notes
    notes.append("bulk reply had no PRA rows; trying per-event")
    evs, _ = api_get(f"/sports/{SPORT}/events", key)
    n = 0
    for e in events_from(evs):
        try:
            t = dt.datetime.fromisoformat(str(e["commence_time"]).replace("Z", "+00:00"))
        except (KeyError, ValueError):
            continue
        if not -2 * 3600 <= (t - now).total_seconds() <= 36 * 3600 or n >= 16:
            continue
        n += 1
        try:
            ej, _ = api_get(f"/sports/{SPORT}/events/{e['id']}/odds", key, {"markets": MARKET})
        except RuntimeError as ex:
            notes.append(f"event {e.get('id')}: {ex}")
            continue
        recs += parse_events(events_from(ej) or [ej], stamp)
    if not recs:
        notes.append("no PRA lines posted yet (books usually post a day ahead)")
    return recs, notes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", help="write all lines from all books to this JSON file")
    a = ap.parse_args()
    key = os.environ.get("PROPLINE_API_KEY", "").strip()
    if not key:
        sys.exit("Set PROPLINE_API_KEY first (free key at https://prop-line.com).")
    recs, notes = fetch_all(key)
    books = sorted({r["book_title"] for r in recs})
    print(f"{len(recs)} lines, {len({norm_name(r['player']) for r in recs})} players, {len(books)} books: {', '.join(books)}")
    for n in notes:
        print("note:", n)
    if a.out:
        with open(a.out, "w") as f:
            json.dump({"source": "PropLine", "market": MARKET, "count": len(recs), "books": books, "lines": recs}, f, indent=1)
        print("wrote", a.out)


if __name__ == "__main__":
    main()
