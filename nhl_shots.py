import os
import time
import requests
from datetime import datetime
from zoneinfo import ZoneInfo
from math import sqrt

# ============================
# CONFIG
# ============================

BASE = "https://api-web.nhle.com/v1"
TZ = ZoneInfo("America/Toronto")

SEASON = "20252026"
GAME_TYPE = "2"  # Regular season

LEAGUE_AVG_SA = 28.0
SLEEP = 0.18
TIMEOUT = 20

SESSION = requests.Session()

TODAY = datetime.now(TZ).strftime("%Y-%m-%d")

# ============================
# NETWORK HELPERS
# ============================

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

# ============================
# NHL DATA
# ============================

def get_matchups():
    data = get_json(f"{BASE}/score/{TODAY}")
    out = {}
    for g in data.get("games", []):
        h = g["homeTeam"]["abbrev"]
        a = g["awayTeam"]["abbrev"]
        out[h] = a
        out[a] = h
    return out

def roster(team):
    data = get_json(f"{BASE}/roster/{team}/current")
    out = []

    def nm(p):
        return f"{p['firstName']['default']} {p['lastName']['default']}"

    for p in data.get("forwards", []):
        out.append((p["id"], nm(p), "F"))
    for p in data.get("defensemen", []):
        out.append((p["id"], nm(p), "D"))

    return out

def last10(player_id):
    data = get_json(f"{BASE}/player/{player_id}/landing")

    for key in ["last10Games", "lastTenGames", "last10", "recentGames"]:
        if key in data:
            games = data[key]
            break
    else:
        return None

    shots = []
    for g in games[:10]:
        s = g.get("shots") or g.get("skaterStats", {}).get("shots")
        if isinstance(s, int):
            shots.append(s)

    return shots if len(shots) >= 5 else None

def stddev(vals):
    m = sum(vals) / len(vals)
    return sqrt(sum((v - m) ** 2 for v in vals) / len(vals))

def club_sa(team):
    data = get_json(f"{BASE}/club-stats/{team}/{SEASON}/{GAME_TYPE}")

    sk = data.get("skaters", [])
    gl = data.get("goalies", [])

    if not sk or not gl:
        return None

    gp = max(s["gamesPlayed"] for s in sk if "gamesPlayed" in s)
    sa = sum(g["shotsAgainst"] for g in gl if "shotsAgainst" in g)

    return sa / gp if gp else None

# ============================
# GOOGLE SHEETS WEBHOOK
# ============================

def post_to_sheets(rows):
    url = os.environ.get("GS_WEBHOOK_URL")
    secret = os.environ.get("GS_SECRET")

    if not url or not secret:
        print("❌ GS webhook not configured")
        return

    payload = {
        "secret": secret,
        "rows": rows
    }

    try:
        print(f"📡 Posting {len(rows)} rows to Google Sheets...")
        r = requests.post(url, json=payload, timeout=15)
        print("📬 Webhook status:", r.status_code)
        print("📬 Webhook response:", r.text)
    except Exception as e:
        print("❌ Webhook post failed:", e)

# ============================
# MAIN
# ============================

def main():
    print(f"\nNHL Shot Parlay Board – {TODAY}\n")

    matchups = get_matchups()
    opp_cache = {}

    all_rows = []

    for team, opp in matchups.items():
        if opp not in opp_cache:
            time.sleep(SLEEP)
            sa = club_sa(opp)
            opp_cache[opp] = sa if sa else LEAGUE_AVG_SA

        opp_sa = opp_cache[opp]
        boost = opp_sa / LEAGUE_AVG_SA

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

            rows.append((name, pos, s10, h2, h3, opp_sa, boost, adj, sc2, sc3))

        rows.sort(key=lambda x: x[8], reverse=True)

        for r in rows[:5]:
            print(f"  {r[0]} {r[1]}  S10:{r[2]:.2f}  H2:{r[3]:.2f}  H3:{r[4]:.2f}  Adj:{r[7]:.2f}  Sc2:{r[8]:.2f}  Sc3:{r[9]:.2f}")

            all_rows.append({
                "Player": r[0],
                "Team": team,
                "Opp": opp,
                "Pos": r[1],
                "S10": round(r[2], 2),
                "Hit2": round(r[3], 2),
                "Hit3": round(r[4], 2),
                "OppSA": round(opp_sa, 2),
                "Boost": round(boost, 3),
                "AdjSOG": round(r[7], 2),
                "Score2": round(r[8], 2),
                "Score3": round(r[9], 2),
                "Date": TODAY
            })

        print("")

    post_to_sheets(all_rows)

if __name__ == "__main__":
    main()
