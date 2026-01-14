import time
import requests
import os
from zoneinfo import ZoneInfo
from math import sqrt
from datetime import datetime

BASE = "https://api-web.nhle.com/v1"
TZ = ZoneInfo("America/Toronto")
TODAY = datetime.now(TZ).strftime("%Y-%m-%d")

SEASON = "20252026"
GAME_TYPE = "2"  # regular season

SESSION = requests.Session()
TIMEOUT = 20

# Rate limit control
SLEEP_BETWEEN_CALLS = 0.18

# League average shots against per game (baseline)
LEAGUE_AVG_SA = 28.0


# -------------------- HTTP HELPERS --------------------

def get_json(url: str, max_retries: int = 7) -> dict:
    backoff = 0.6
    for _ in range(max_retries):
        r = SESSION.get(url, timeout=TIMEOUT)

        if r.status_code == 429:
            retry_after = r.headers.get("Retry-After")
            wait = float(retry_after) if (retry_after and retry_after.isdigit()) else backoff
            time.sleep(wait)
            backoff = min(backoff * 2, 12)
            continue

        r.raise_for_status()
        return r.json()

    raise RuntimeError(f"Failed after retries: {url}")


# -------------------- NHL DATA --------------------

def get_matchups_today():
    data = get_json(f"{BASE}/score/{TODAY}")
    matchups = {}
    for g in data.get("games", []):
        home = (g.get("homeTeam") or {}).get("abbrev")
        away = (g.get("awayTeam") or {}).get("abbrev")
        if home and away:
            matchups[home] = away
            matchups[away] = home
    return matchups


def roster_skaters(team):
    data = get_json(f"{BASE}/roster/{team}/current")
    out = []

    def pname(p):
        first = (p.get("firstName") or {}).get("default")
        last = (p.get("lastName") or {}).get("default")
        return f"{first} {last}".strip()

    for p in data.get("forwards", []):
        out.append((p["id"], pname(p), "F"))
    for p in data.get("defensemen", []):
        out.append((p["id"], pname(p), "D"))

    return out


def last5_shots(pid):
    data = get_json(f"{BASE}/player/{pid}/landing")

    games = None
    for key in ["last5Games", "lastFiveGames", "recentGames"]:
        if key in data:
            games = data[key]
            break

    if not games:
        return None

    shots = []
    for g in games[:5]:
        s = g.get("shots")
        if s is None and g.get("skaterStats"):
            s = g["skaterStats"].get("shots")
        if isinstance(s, int):
            shots.append(s)

    return shots if len(shots) == 5 else None


def stddev(vals):
    m = sum(vals) / len(vals)
    return sqrt(sum((v - m) ** 2 for v in vals) / len(vals))


def club_shots_against(team):
    data = get_json(f"{BASE}/club-stats/{team}/{SEASON}/{GAME_TYPE}")

    skaters = data.get("skaters", [])
    goalies = data.get("goalies", [])

    if not skaters or not goalies:
        return None

    gp = max(s["gamesPlayed"] for s in skaters if "gamesPlayed" in s)
    sa = sum(g["shotsAgainst"] for g in goalies if "shotsAgainst" in g)

    return sa / gp if gp > 0 else None


# -------------------- GOOGLE SHEETS WEBHOOK --------------------

def post_to_google_sheets(rows, date_str):
    url = os.getenv("SHEETS_WEBHOOK_URL", "").strip()
    token = os.getenv("SHEETS_TOKEN", "").strip()

    if not url or not token:
        print("⚠️ Sheets webhook not configured")
        return

    payload = {
        "token": token,
        "date": date_str,
        "rows": rows
    }

    try:
        SESSION.post(url, json=payload, timeout=TIMEOUT).raise_for_status()
        print("✅ Posted Top 5 per team to Google Sheets")
    except Exception as e:
        print("⚠️ Sheets webhook failed:", e)


# -------------------- MAIN --------------------

def main():
    print(f"\nNHL Shot Parlay Board - Last 5 SOG - {TODAY}\n")
    print(f"Boost baseline (league SA): {LEAGUE_AVG_SA:.1f}\n")

    matchups = get_matchups_today()
    if not matchups:
        print("No games today.")
        return

    opp_sa_cache = {}
    all_top_rows = []

    for team in sorted(matchups.keys()):
        opp = matchups[team]

        if opp not in opp_sa_cache:
            time.sleep(SLEEP_BETWEEN_CALLS)
            sa = club_shots_against(opp)
            opp_sa_cache[opp] = sa if sa else LEAGUE_AVG_SA

        opp_sa = opp_sa_cache[opp]
        boost = opp_sa / LEAGUE_AVG_SA

        rows = []

        for pid, name, pos in roster_skaters(team):
            time.sleep(SLEEP_BETWEEN_CALLS)
            shots = last5_shots(pid)
            if not shots:
                continue

            s5 = sum(shots) / 5
            h2 = sum(1 for s in shots if s >= 2) / 5
            h3 = sum(1 for s in shots if s >= 3) / 5
            sd = stddev(shots)
            adj = s5 * boost

            sc2 = adj + 0.6 * h2 - 0.15 * sd
            sc3 = adj + 0.6 * h3 - 0.20 * sd

            rows.append((name, pos, s5, h2, h3, opp_sa, boost, adj, sc2, sc3))

        forwards = [r for r in rows if r[1] == "F"]
        defense = [r for r in rows if r[1] == "D"]

        forwards.sort(key=lambda x: x[8], reverse=True)
        defense.sort(key=lambda x: x[8], reverse=True)

        print(f"{team} vs {opp} | Opp SA: {opp_sa:.1f} | Boost: {boost:.2f}\n")

        team_rows = []

        for r in forwards[:4]:
            name, pos, s5, h2, h3, oppsa, boost, adj, sc2, sc3 = r
            print(f"  {name}  S5:{s5:.2f}  H2:{h2:.2f}  H3:{h3:.2f}  Adj:{adj:.2f}  Sc2:{sc2:.2f}  Sc3:{sc3:.2f}")
            team_rows.append(dict(player=name, team=team, opp=opp, pos="F",
                                  s5=s5, hit2=h2, hit3=h3, oppSA=oppsa, boost=boost,
                                  adjSOG=adj, score2=sc2, score3=sc3))

        if defense:
            r = defense[0]
            name, pos, s5, h2, h3, oppsa, boost, adj, sc2, sc3 = r
            print(f"  {name} (D)  S5:{s5:.2f}  H2:{h2:.2f}  H3:{h3:.2f}  Adj:{adj:.2f}  Sc2:{sc2:.2f}  Sc3:{sc3:.2f}")
            team_rows.append(dict(player=name, team=team, opp=opp, pos="D",
                                  s5=s5, hit2=h2, hit3=h3, oppSA=oppsa, boost=boost,
                                  adjSOG=adj, score2=sc2, score3=sc3))

        print("")
        all_top_rows.extend(team_rows)

    post_to_google_sheets(all_top_rows, TODAY)


if __name__ == "__main__":
    main()
