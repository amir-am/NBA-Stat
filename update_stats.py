import json, math, os, sys, time, datetime as dt, statistics as st
from collections import defaultdict

SCHED = "https://cdn.nba.com/static/json/staticData/scheduleLeagueV2.json"
m = lambda v: round(sum(v) / len(v), 2) if v else None


def season_str(d):
    y = d.year if d.month >= 10 else d.year - 1
    return f"{y}-{str(y + 1)[2:]}"


def floor_pct(vals, p):
    s = sorted(vals, reverse=True)
    return s[math.ceil(p * len(s) / 100 - 1e-9) - 1]


def retry(fn, tries=4):
    for i in range(tries):
        try:
            return fn()
        except Exception as e:
            print(f"try {i + 1} failed: {e}", file=sys.stderr)
            if i == tries - 1:
                raise
            time.sleep(10 * 3 ** i)


def fetch_log(season):
    from nba_api.stats.endpoints import leaguegamelog
    df = leaguegamelog.LeagueGameLog(
        season=season, player_or_team_abbreviation="P",
        season_type_all_star="Regular Season", timeout=25).get_data_frames()[0]
    return df.to_dict("records")


def fetch_sched():
    import requests
    r = requests.get(SCHED, timeout=30, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.nba.com/",
                                                 "Origin": "https://www.nba.com", "Accept": "application/json"})
    print("NBA schedule:", r.status_code, r.text[:80].replace("\n", " "), file=sys.stderr)
    return r.json()


UA = {"User-Agent": "Mozilla/5.0"}
BOX = "https://cdn.nba.com/static/json/liveData/boxscore/boxscore_{}.json"


def parse_min(s):
    import re
    mm = re.match(r"PT(\d+)M([\d.]+)S", s or "")
    return int(mm[1]) + float(mm[2]) / 60 if mm else 0


def box_rows(j, gid, d):
    rows = []
    for side, oth in (("homeTeam", "awayTeam"), ("awayTeam", "homeTeam")):
        me, op = j[side]["teamTricode"], j[oth]["teamTricode"]
        mu = f"{me} vs. {op}" if side == "homeTeam" else f"{me} @ {op}"
        for p in j[side]["players"]:
            s = p["statistics"]
            rows.append(dict(PLAYER_ID=p["personId"], PLAYER_NAME=p["name"], TEAM_ABBREVIATION=me,
                             GAME_ID=gid, GAME_DATE=d, MATCHUP=mu, MIN=parse_min(s.get("minutes")),
                             PTS=s["points"], REB=s["reboundsTotal"], AST=s["assists"]))
    return rows


def fetch_cdn(sched, path="games_cache.json", budget=720):
    import requests
    from concurrent.futures import ThreadPoolExecutor
    cache = json.load(open(path)) if os.path.exists(path) else {}
    todo = [(str(g["gameId"]), g["gameDateEst"][:10]) for gd in sched["leagueSchedule"]["gameDates"]
            for g in gd["games"] if g["gameStatus"] == 3 and str(g["gameId"]).startswith("002")
            and str(g["gameId"]) not in cache]
    end = time.time() + budget

    def one(t):
        gid, d = t
        if time.time() > end:
            return gid, None
        try:
            j = retry(lambda: requests.get(BOX.format(gid), timeout=20, headers=UA).json(), 3)["game"]
            return gid, box_rows(j, gid, d)
        except Exception:
            return gid, None
    with ThreadPoolExecutor(8) as ex:
        for gid, rows in ex.map(one, todo):
            if rows:
                cache[gid] = rows
    print(f"cache: {len(cache)} games, {len(todo)} were new")
    with open(path + ".tmp", "w") as f:
        json.dump(cache, f, separators=(",", ":"))
    os.replace(path + ".tmp", path)
    return [r for rows in cache.values() for r in rows]


ESPN = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/"
FIX = {"GS": "GSW", "NY": "NYK", "SA": "SAS", "NO": "NOP", "WSH": "WAS", "UTAH": "UTA"}


def getj(url, tries=3):
    import requests

    def go():
        r = requests.get(url, timeout=30, headers=UA)
        print("GET", url[-70:], r.status_code, len(r.content), file=sys.stderr)
        r.raise_for_status()
        return r.json()
    return retry(go, tries)


def num(x):
    try:
        return float(x)
    except Exception:
        return 0.0


def fetch_espn(now, path="espn_cache.json", budget=720):
    from concurrent.futures import ThreadPoolExecutor
    y = int(season_str(now)[:4])
    d0, d1 = dt.date(y, 10, 1), now.date() + dt.timedelta(days=10)
    sb = getj(f"{ESPN}scoreboard?dates={d0:%Y%m%d}-{d1:%Y%m%d}&limit=1000")
    games, done = [], []
    for e in sb.get("events", []):
        if e.get("season", {}).get("type") != 2:
            continue
        c = e["competitions"][0]["competitors"]
        ab = lambda side: FIX.get(ab0 := next(x for x in c if x["homeAway"] == side)["team"]["abbreviation"], ab0)
        h, aw = ab("home"), ab("away")
        t = e["date"]
        ld = (dt.datetime.fromisoformat(t.replace("Z", "+00:00")) - dt.timedelta(hours=6)).date().isoformat()
        fin = e["status"]["type"]["completed"]
        games.append({"gameId": "002" + str(e["id"]), "gameStatus": 3 if fin else 1, "gameDateTimeUTC": t,
                      "homeTeam": {"teamTricode": h}, "awayTeam": {"teamTricode": aw}})
        if fin:
            done.append((str(e["id"]), ld, h, aw))
    sched = {"leagueSchedule": {"gameDates": [{"games": games}]}}
    cache = json.load(open(path)) if os.path.exists(path) else {}
    todo = [t for t in done if t[0] not in cache]
    end = time.time() + budget

    def one(t):
        eid, ld, h, aw = t
        if time.time() > end:
            return eid, None
        try:
            j = getj(f"{ESPN}summary?event={eid}", 3)
            rows = []
            for tm in j["boxscore"]["players"]:
                tab = tm["team"]["abbreviation"]
                tab = FIX.get(tab, tab)
                home = tab == h
                mu = f"{tab} vs. {aw}" if home else f"{tab} @ {h}"
                for grp in tm["statistics"]:
                    lab = grp["labels"]
                    ix = {k: lab.index(k) for k in ("MIN", "PTS", "REB", "AST")}
                    for at in grp["athletes"]:
                        st_ = at.get("stats") or []
                        if not st_ or at.get("didNotPlay"):
                            continue
                        rows.append(dict(PLAYER_ID=int(at["athlete"]["id"]), PLAYER_NAME=at["athlete"]["displayName"],
                                         TEAM_ABBREVIATION=tab, GAME_ID="002" + eid, GAME_DATE=ld, MATCHUP=mu,
                                         MIN=num(st_[ix["MIN"]]), PTS=int(num(st_[ix["PTS"]])),
                                         REB=int(num(st_[ix["REB"]])), AST=int(num(st_[ix["AST"]]))))
            return eid, rows
        except Exception as ex:
            print("game", eid, "failed:", ex, file=sys.stderr)
            return eid, None
    with ThreadPoolExecutor(6) as ex:
        for eid, rows in ex.map(one, todo):
            if rows:
                cache[eid] = rows
    print(f"espn cache: {len(cache)} games, {len(todo)} were new")
    with open(path + ".tmp", "w") as f:
        json.dump(cache, f, separators=(",", ":"))
    os.replace(path + ".tmp", path)
    return sched, [r for rows in cache.values() for r in rows]


def build(rows, sched, season, now):
    R = []
    for r in rows:
        mins = float(r["MIN"] or 0)
        if mins <= 0:
            continue
        R.append(dict(id=int(r["PLAYER_ID"]), name=r["PLAYER_NAME"], team=r["TEAM_ABBREVIATION"],
                      gid=r["GAME_ID"], date=str(r["GAME_DATE"])[:10], home=" vs. " in r["MATCHUP"],
                      opp=r["MATCHUP"].split()[-1], min=mins, pts=r["PTS"], reb=r["REB"], ast=r["AST"],
                      pra=r["PTS"] + r["REB"] + r["AST"]))
    tdates, allowed = defaultdict(set), defaultdict(float)
    for x in R:
        tdates[x["team"]].add(x["date"])
        allowed[(x["gid"], x["opp"])] += x["pra"]  # PRA the opp's defense gave up this game
    tl = defaultdict(list)
    for (_, o), v in allowed.items():
        tl[o].append(v)
    ta = {t: m(v) for t, v in tl.items()}
    order = sorted(ta, key=lambda t: -ta[t])
    teams = {t: {"allowed": ta[t], "rank": i + 1} for i, t in enumerate(order)}
    rest = {}
    for t, ds in tdates.items():
        ds = sorted(ds)
        for i, d in enumerate(ds):
            rest[(t, d)] = None if i == 0 else (dt.date.fromisoformat(d) - dt.date.fromisoformat(ds[i - 1])).days - 1
    nxt = {}
    for gd in (sched or {}).get("leagueSchedule", {}).get("gameDates", []):
        for g in gd["games"]:
            if g["gameStatus"] == 1 and str(g["gameId"]).startswith("002"):
                h, a, t = g["homeTeam"]["teamTricode"], g["awayTeam"]["teamTricode"], g["gameDateTimeUTC"]
                for me, op, ih in ((h, a, True), (a, h, False)):
                    if me and op and (me not in nxt or t < nxt[me][0]):
                        nxt[me] = (t, {"opp": op, "home": ih, "date": t[:10]})
    by = defaultdict(list)
    for x in sorted(R, key=lambda x: x["date"]):
        by[x["id"]].append(x)
    P = []
    for pid, g in by.items():
        if len(g) < 5:
            continue
        v = [x["pra"] for x in g]
        rs = [rest[(x["team"], x["date"])] for x in g]
        def sub(a):
            return {"avg": m(a), "n": len(a)} if len(a) >= 5 else None
        t = g[-1]["team"]
        P.append(dict(
            id=pid, name=g[0]["name"], team=t, gp=len(g), avg=m(v), med=st.median(v),
            f90=floor_pct(v, 90), f80=floor_pct(v, 80), f70=floor_pct(v, 70),
            l5=m(v[-5:]), l10=m(v[-10:]), min=m([x["min"] for x in g]), l5min=m([x["min"] for x in g[-5:]]),
            sd=round(st.pstdev(v), 2), mn=min(v), mx=max(v),
            home=m([x["pra"] for x in g if x["home"]]), away=m([x["pra"] for x in g if not x["home"]]),
            b2b=sub([x["pra"] for x, r in zip(g, rs) if r == 0]),
            r2=sub([x["pra"] for x, r in zip(g, rs) if r is not None and r >= 2]),
            next=nxt.get(t, (0, None))[1], pra=v,
            games=[dict(d=x["date"], o=x["opp"], h=x["home"], m=round(x["min"]), p=x["pts"], r=x["reb"],
                        a=x["ast"], pra=x["pra"]) for x in g[-15:]]))
    P.sort(key=lambda p: -p["avg"])
    return {"season": season, "updated": now.strftime("%Y-%m-%d %H:%M UTC"), "players": P[:200], "teams": teams}


def main():
    now = dt.datetime.now(dt.timezone.utc)
    season = season_str(now)
    sched, rows = {}, []
    try:
        sched = retry(fetch_sched, 2)
        rows = fetch_cdn(sched)
    except Exception as e:
        print("NBA CDN route failed:", e, file=sys.stderr)
    if not rows:
        try:
            sched, rows = fetch_espn(now)
        except Exception as e:
            print("ESPN route failed:", e, file=sys.stderr)
    if not rows:
        try:
            rows = retry(lambda: fetch_log(season), 2)
        except Exception as e:
            sys.exit(f"ERROR: no data source reachable, data.json left unchanged ({e})")
    if not rows:
        sys.exit("ERROR: no game data yet for " + season + "; data.json left unchanged")
    out = build(rows, sched, season, now)
    with open("data.json.tmp", "w") as f:
        json.dump(out, f, separators=(",", ":"))
    os.replace("data.json.tmp", "data.json")
    print("OK", len(out["players"]), "players")


if __name__ == "__main__":
    main()
