import os
import time
import requests
from datetime import datetime
from zoneinfo import ZoneInfo
from math import sqrt

BASE = "https://api-web.nhle.com/v1"
TZ = ZoneInfo("America/Toronto")

SEASON = "20252026"
GAME_TYPE = "2"  # Regular season

LEAGUE_AVG_SA = 28.0
SLEEP = 0.18
TIMEOUT = 20

SESSION = requests.Session()
TODAY = datetime.now(TZ).strftime("%Y-%m-%d")


def get_json(url, retries=6):
    backoff = 0.6
    for _ in range(retries):
        r = SESSION.get(url, timeout=TIMEOUT)
        if r.status_code == 429:
            time.sleep(backoff)
            backoff = min(backoff * 2, 10)
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError(f"Failed: {url}")


def get_matchups():
    data = get_json(f"{BASE}/score/{TODAY}")
    out = {}
    for g in data.get("games", []):
        h = (g.get("homeTeam") or {}).get("abbrev")
        a = (g.get("awayTeam") or {}).get("abbrev")
        if h and a:
            out[h] = a
            out[a] = h
    return out


def roster(team):
    data = get_json(f"{BASE}/roster/{team}/current")
    out = []

    def nm(p):
        fn = (p.get("firstName") or {}).get("default") or ""
        ln = (p.get("lastName") or {}).get("default") or ""
        name = (fn + " " + ln).strip()
        return name if name else "Unknown"

    for p in data.get("forwards", []):
        out.append((p["id"], nm(p), "F"))
    for p in data.get("defensemen", []):
        out.append((p["id"], nm(p), "D"))

    return out


def last10(player_id):
    data = get_json(f"{BASE}/player/{player_id}/landing")

    # Try a few possible keys (NHL occasionally changes these)
    games = None
    for key in ["last10Games", "lastTenGames", "last10", "recentGames", "last5Games", "lastFiveGames"]:
        v = data.get(key)
        if isinstance(v, list) and len(v) > 0:
            games = v
            break
    if not games:
        return None

    shots = []
    for g in games[:10]:
        s = g.get("shots")
        if s is None and isinstance(g.get("skaterStats"), dict):
            s = g["skaterStats"].get("shots")
        if isinstance(s, int):
            shots.append(s)

    # Need at least 5 games to be meaningful
    return shots if len(shots) >= 5 else None

def stddev(vals):
    m = sum(vals) / len(vals)
    return sqrt(sum((v - m) ** 2 for v in vals) / len(vals))


def club_sa(team):
    data = get_json(f"{BASE}/club-stats/{team}/{SEASON}/{GAME_TYPE}")

    sk = data.get("skaters", [])
    gl = data.get("goalies", [])

    if not isinstance(sk, list) or not isinstance(gl, list) or not sk or not gl:
        return None

    gp_vals = [s.get("gamesPlayed") for s in sk if isinstance(s.get("gamesPlayed"), int)]
    if not gp_vals:
        return None
    gp = max(gp_vals)
    if gp <= 0:
        return None

    sa_vals = [g.get("shotsAgainst") for g in gl if isinstance(g.get("shotsAgainst"), int)]
    if not sa_vals:
        return None
    sa = sum(sa_vals)

    return sa / gp


def post_to_sheets(rows):
    url = os.environ.get("GS_WEBHOOK_URL")
    secret = os.environ.get("GS_SECRET")

    if not url or not secret:
        print("❌ GS webhook not configured (missing GS_WEBHOOK_URL or GS_SECRET)")
        return

    # Debug without exposing secret
    print("🔐 Using GS_SECRET length:", len(secret))
    print("🔐 GS_SECRET preview:", secret[:4] + "..." + secret[-4:])

    payload = {"secret": secret, "rows": rows}

    try:
        print(f"📡 Posting {len(rows)} rows to Google Sheets...")
        r = requests.post(url, json=payload, timeout=15)
        print("📬 Webhook status:", r.status_code)
        print("📬 Webhook response:", r.text)
    except Exception as e:
        print("❌ Webhook post failed:", e)


def main():
    print(f"\nNHL Shot Parlay Board – {TODAY}\n")

    matchups = get_matchups()
    if not matchups:
        print("No games found today.")
        return

    opp_cache = {}
    all_rows = []

    for team in sorted(matchups.keys()):
        opp = matchups[team]

        if opp not in opp_cache:
            time.sleep(SLEEP)
            sa = club_sa(opp)
            opp_cache[opp] = sa if sa is not None else LEAGUE_AVG_SA

        opp_sa = opp_cache[opp]
        boost = opp_sa / LEAGUE_AVG_SA if LEAGUE_AVG_SA else 1.0

        print(f"{team} vs {opp} | Opp SA: {opp_sa:.1f} | Boost {boost:.2f}")

        players = roster(team)
        rows = []

        for pid, name, pos in players:
            time.sleep(SLEEP)
            shots = last10(pid)
            if not shots:
                continue

            s10 = sum(shots) / len(shots)
            h2 = sum(1 for s in shots if s >= 2) / len(shots)
            h3 = sum(1 for s in shots if s >= 3) / len(shots)
            sd = stddev(shots)

            adj = s10 * boost
            sc2 = adj + 0.6 * h2 - 0.15 * sd
            sc3 = adj + 0.6 * h3 - 0.20 * sd

            rows.append((name, pos, s10, h2, h3, opp_sa, boost, adj, sc2, sc3, sd))

        rows.sort(key=lambda x: x[8], reverse=True)

        top = rows[:5]
        for r in top:
            name, pos, s10, h2, h3, opp_sa, boost, adj, sc2, sc3, sd = r
            print(f"  {name} {pos}  S10:{s10:.2f}  H2:{h2:.2f}  H3:{h3:.2f}  Adj:{adj:.2f}  Sc2:{sc2:.2f}  Sc3:{sc3:.2f}")

            all_rows.append({
                "Player": name,
                "Team": team,
                "Opp": opp,
                "Pos": pos,
                "S10": round(s10, 2),
                "Hit2": round(h2, 2),
                "Hit3": round(h3, 2),
                "OppSA": round(opp_sa, 2),
                "Boost": round(boost, 3),
                "AdjSOG": round(adj, 2),
                "Score2": round(sc2, 2),
                "Score3": round(sc3, 2),
                "Date": TODAY
            })

        print("")

    print("✅ Collected rows:", len(all_rows))
    post_to_sheets(all_rows)


if __name__ == "__main__":
    main()
