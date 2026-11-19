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
        season_type_all_star="Regular Season", timeout=60).get_data_frames()[0]
    return df.to_dict("records")


def fetch_sched():
    import requests
    return requests.get(SCHED, timeout=60, headers={"User-Agent": "Mozilla/5.0"}).json()


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
    try:
        rows = retry(lambda: fetch_log(season))
    except Exception as e:
        sys.exit(f"ERROR: NBA.com unreachable, data.json left unchanged ({e})")
    if not rows:
        sys.exit("ERROR: no game data yet for " + season + "; data.json left unchanged")
    try:
        sched = retry(fetch_sched)
    except Exception as e:
        print("WARNING: schedule unavailable, no next opponent:", e, file=sys.stderr)
        sched = {}
    out = build(rows, sched, season, now)
    with open("data.json.tmp", "w") as f:
        json.dump(out, f, separators=(",", ":"))
    os.replace("data.json.tmp", "data.json")
    print("OK", len(out["players"]), "players")


if __name__ == "__main__":
    main()
