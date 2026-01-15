import os
import sys
import time
import requests
from math import sqrt
from datetime import datetime
from zoneinfo import ZoneInfo

# ----------------------------
# Config
# ----------------------------
BASE = "https://api-web.nhle.com/v1"
TZ = ZoneInfo("America/Toronto")

SEASON = "20252026"
GAME_TYPE = "2"  # regular season

SESSION = requests.Session()
TIMEOUT = 20

# Polite pacing to avoid 429s
SLEEP_BETWEEN_CALLS = 0.18

# League baseline shots against per game (all situations) used for boost
# Keep as constant to reduce extra calls and rate limiting.
LEAGUE_AVG_SA = float(os.getenv("LEAGUE_AVG_SA", "28.0"))

# Sample size
N_GAMES = int(os.getenv("N_GAMES", "10"))

# Google Sheets webhook (Apps Script Web App)
GS_WEBHOOK_URL = os.getenv("GS_WEBHOOK_URL", "").strip()
GS_SECRET = os.getenv("GS_SECRET", "").strip()

# ----------------------------
# Helpers
# ----------------------------
def today_str() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d")


TODAY = today_str()


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


def stddev(vals: list[int]) -> float:
    m = sum(vals) / len(vals)
    var = sum((v - m) ** 2 for v in vals) / len(vals)
    return sqrt(var)


def post_rows_to_sheets(rows: list[dict]) -> None:
    """
    Posts rows to your Google Sheets webhook.
    Expects Apps Script to accept:
      { "secret": "...", "rows": [ {...}, {...} ] }
    If your Apps Script also supports "tab", you can add it here.
    """
    if not GS_WEBHOOK_URL or not GS_SECRET:
        print("ℹ️ GS webhook not configured (missing GS_WEBHOOK_URL or GS_SECRET). Skipping post.")
        return

    payload = {
        "secret": GS_SECRET,
        "rows": rows,
    }
    r = SESSION.post(GS_WEBHOOK_URL, json=payload, timeout=TIMEOUT)
    r.raise_for_status()


# ----------------------------
# NHL data fetch
# ----------------------------
def get_matchups_today() -> dict[str, str]:
    """
    Returns mapping like: {"NJD": "SEA", "SEA": "NJD", ...}
    """
    data = get_json(f"{BASE}/score/{TODAY}")
    matchups: dict[str, str] = {}

    for g in data.get("games", []):
        home = (g.get("homeTeam") or {}).get("abbrev")
        away = (g.get("awayTeam") or {}).get("abbrev")
        if home and away:
            matchups[home] = away
            matchups[away] = home

    return matchups


def roster_skaters(team_abbrev: str) -> list[tuple[int, str, str]]:
    """
    Returns (player_id, name, pos_code) for skaters only (F and D).
    """
    data = get_json(f"{BASE}/roster/{team_abbrev}/current")
    out: list[tuple[int, str, str]] = []

    def full_name(p: dict) -> str:
        first = (p.get("firstName") or {}).get("default") or p.get("firstName")
        last = (p.get("lastName") or {}).get("default") or p.get("lastName")
        return (" ".join([x for x in [first, last] if x]) or p.get("fullName") or "Unknown").strip()

    for group_key, pos in [("forwards", "F"), ("defensemen", "D")]:
        for p in data.get(group_key, []):
            pid = p.get("id")
            if isinstance(pid, int):
                out.append((pid, full_name(p), pos))

    return out


def lastN_shots_from_game_log(player_id: int, n: int = 10) -> list[int] | None:
    """
    Pull last N game shots from the NHL game-log endpoint.
    Returns list of ints length N, or None if not enough data.
    """
    url = f"{BASE}/player/{player_id}/game-log/{SEASON}/{GAME_TYPE}"
    data = get_json(url)

    game_log = data.get("gameLog")
    if not isinstance(game_log, list) or not game_log:
        return None

    shots: list[int] = []
    for g in game_log:
        s = g.get("shots")
        if s is None and isinstance(g.get("skaterStats"), dict):
            s = g["skaterStats"].get("shots")

        if isinstance(s, int):
            shots.append(s)

        if len(shots) >= n:
            break

    if len(shots) < n:
        return None

    return shots[:n]


def club_shots_against_per_game(team_abbrev: str) -> float | None:
    """
    Compute opponent shots against per game (all situations) from club-stats endpoint.
    Endpoint provides goalie shotsAgainst totals and skater gamesPlayed.
    We compute:
      shotsAgainstPerGame = (sum goalies.shotsAgainst) / (max skaters.gamesPlayed)
    """
    data = get_json(f"{BASE}/club-stats/{team_abbrev}/{SEASON}/{GAME_TYPE}")

    skaters = data.get("skaters", [])
    goalies = data.get("goalies", [])

    if not isinstance(skaters, list) or not isinstance(goalies, list) or not skaters or not goalies:
        return None

    gp_vals = [s.get("gamesPlayed") for s in skaters if isinstance(s.get("gamesPlayed"), int)]
    if not gp_vals:
        return None

    team_gp = max(gp_vals)
    if team_gp <= 0:
        return None

    sa_vals = [g.get("shotsAgainst") for g in goalies if isinstance(g.get("shotsAgainst"), int)]
    if not sa_vals:
        return None

    total_sa = sum(sa_vals)
    return total_sa / team_gp


# ----------------------------
# Main
# ----------------------------
def main() -> None:
    print(f"NHL Shot Parlay Board - Last {N_GAMES} SOG - {TODAY}")
    print(f"Boost baseline (league SA): {LEAGUE_AVG_SA:.1f}\n")

    matchups = get_matchups_today()
    if not matchups:
        print("No games found for today.")
        return

    # Cache opponent SA so we only call club-stats once per opponent team
    opp_sa_cache: dict[str, float] = {}

    # We'll compute all rows, then print + post Top 5 per team.
    all_rows: list[dict] = []

    # iterate by team (includes both sides, like NJD and SEA)
    teams = sorted(matchups.keys())

    for team in teams:
        opp = matchups.get(team)
        if not opp:
            continue

        # Opponent SA
        if opp not in opp_sa_cache:
            time.sleep(SLEEP_BETWEEN_CALLS)
            opp_sa = club_shots_against_per_game(opp)
            opp_sa_cache[opp] = opp_sa if opp_sa is not None else LEAGUE_AVG_SA

        opp_sa = opp_sa_cache[opp]
        boost = (opp_sa / LEAGUE_AVG_SA) if LEAGUE_AVG_SA > 0 else 1.0

        skaters = roster_skaters(team)

        for pid, name, pos in skaters:
            time.sleep(SLEEP_BETWEEN_CALLS)

            shotsN = lastN_shots_from_game_log(pid, n=N_GAMES)
            if shotsN is None:
                continue

            sN = sum(shotsN) / float(N_GAMES)
            hit2 = sum(1 for s in shotsN if s >= 2) / float(N_GAMES)
            hit3 = sum(1 for s in shotsN if s >= 3) / float(N_GAMES)
            sdN = stddev(shotsN)

            adj_sog = sN * boost

            # Your scoring formula (same as before)
            score2 = adj_sog + 0.6 * hit2 - 0.15 * sdN
            score3 = adj_sog + 0.6 * hit3 - 0.20 * sdN

            all_rows.append({
                "Player": name,
                "PlayerId": pid,
                "Team": team,
                "Opp": opp,
                "Pos": pos,
                f"S{N_GAMES}": round(sN, 4),
                "Hit2": round(hit2, 4),
                "Hit3": round(hit3, 4),
                "OppSA": round(opp_sa, 4),
                "Boost": round(boost, 6),
                "AdjSOG": round(adj_sog, 4),
                "Score2": round(score2, 4),
                "Score3": round(score3, 4),
                "Date": TODAY,
            })

    # Print + post "Top 5 per team" (4F + 1D) based on Score2
    output_rows: list[dict] = []
    for team in sorted(set(r["Team"] for r in all_rows)):
        team_rows = [r for r in all_rows if r["Team"] == team]
        if not team_rows:
            continue

        opp = team_rows[0]["Opp"]

        forwards = [r for r in team_rows if r["Pos"] == "F"]
        defense = [r for r in team_rows if r["Pos"] == "D"]

        forwards.sort(key=lambda x: x["Score2"], reverse=True)
        defense.sort(key=lambda x: x["Score2"], reverse=True)

        top_f = forwards[:4]
        top_d = defense[:1]

        print(f"{team} vs {opp} | Opp SA: {team_rows[0]['OppSA']:.1f} | Boost: {team_rows[0]['Boost']:.2f}")
        for r in top_f:
            print(
                f"  {r['Player']}  "
                f"S{N_GAMES}:{r[f'S{N_GAMES}']:.2f}  "
                f"H2:{r['Hit2']:.2f}  "
                f"H3:{r['Hit3']:.2f}  "
                f"Adj:{r['AdjSOG']:.2f}  "
                f"Sc2:{r['Score2']:.2f}  "
                f"Sc3:{r['Score3']:.2f}"
            )
            output_rows.append(r)

        if top_d:
            r = top_d[0]
            print(
                f"  {r['Player']} (D)  "
                f"S{N_GAMES}:{r[f'S{N_GAMES}']:.2f}  "
                f"H2:{r['Hit2']:.2f}  "
                f"H3:{r['Hit3']:.2f}  "
                f"Adj:{r['AdjSOG']:.2f}  "
                f"Sc2:{r['Score2']:.2f}  "
                f"Sc3:{r['Score3']:.2f}"
            )
            output_rows.append(r)

        print("")

    # Post to Sheets
    try:
        post_rows_to_sheets(output_rows)
        print("✅ Posted Top 5 per team to Google Sheets")
    except Exception as e:
        print(f"❌ Failed to post to Google Sheets: {e}")


if __name__ == "__main__":
    # optional future modes: python nhl_shots.py candidates / results
    # for now, keep it simple:
    main()
