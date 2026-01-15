import os
import time
import requests
from zoneinfo import ZoneInfo
from math import sqrt
from datetime import datetime

BASE = "https://api-web.nhle.com/v1"
TZ = ZoneInfo("America/Toronto")

SEASON = "20252026"
GAME_TYPE = "2"  # regular season

SESSION = requests.Session()
TIMEOUT = 20

# Polite pacing to avoid 429s
SLEEP_BETWEEN_CALLS = 0.20

# Baseline league shots against per game (all situations) used for boost
LEAGUE_AVG_SA = 28.0  # you set this recently; keep as-is or adjust later


def today_str() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d")

TODAY = today_str()


def get_json(url: str, max_retries: int = 7) -> dict:
    backoff = 0.7
    for _ in range(max_retries):
        r = SESSION.get(url, timeout=TIMEOUT)

        if r.status_code == 429:
            retry_after = r.headers.get("Retry-After")
            wait = float(retry_after) if (retry_after and retry_after.isdigit()) else backoff
            time.sleep(wait)
            backoff = min(backoff * 2, 15)
            continue

        r.raise_for_status()
        return r.json()

    raise RuntimeError(f"Failed after retries: {url}")


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

    def pname(p: dict) -> str:
        first = (p.get("firstName") or {}).get("default") or p.get("firstName")
        last = (p.get("lastName") or {}).get("default") or p.get("lastName")
        return (" ".join([x for x in [first, last] if x]) or p.get("fullName") or "Unknown").strip()

    for group_key, pos in [("forwards", "F"), ("defensemen", "D")]:
        for p in data.get(group_key, []):
            pid = p.get("id")
            if isinstance(pid, int):
                out.append((pid, pname(p), pos))

    return out


def stddev(vals: list[int]) -> float:
    m = sum(vals) / len(vals)
    var = sum((v - m) ** 2 for v in vals) / len(vals)
    return sqrt(var)


def last_n_from_game_log(player_id: int, n: int = 10) -> list[int] | None:
    """
    Pull true game log and extract the last N games shots.
    This fixes the 'landing endpoint sometimes only behaves like last-5' issue.
    """
    url = f"{BASE}/player/{player_id}/game-log/{SEASON}/{GAME_TYPE}"
    data = get_json(url)

    games = data.get("gameLog") or data.get("games") or []
    if not isinstance(games, list) or not games:
        return None

    shots: list[int] = []
    for g in games:
        s = g.get("shots")
        if isinstance(s, int):
            shots.append(s)

    # game-log is typically most recent first, but if not, still safe:
    # If it had dates, we could sort; however shots-only extraction usually preserves order.
    if len(shots) < n:
        return None

    return shots[:n]


def club_shots_against_per_game(team_abbrev: str) -> float | None:
    """
    Compute opponent shots against per game (all situations) from club-stats endpoint.
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


def post_to_google_sheets(rows: list[dict]) -> None:
    url = os.getenv("GS_WEBHOOK_URL", "").strip()
    secret = os.getenv("GS_SECRET", "").strip()

    if not url or not secret:
        print("ℹ️ GS webhook not configured (missing GS_WEBHOOK_URL or GS_SECRET). Skipping post.")
        return

    payload = {"secret": secret, "rows": rows}
    print(f"📡 Posting {len(rows)} rows to Google Sheets...")
    r = SESSION.post(url, json=payload, timeout=TIMEOUT)
    print(f"📬 Webhook status: {r.status_code}")
    print(f"📬 Webhook response: {r.text[:200]}")


def main():
    print(f"\nNHL Shot Parlay Board - Last 10 SOG - {TODAY}\n")
    print(f"Boost baseline (league SA): {LEAGUE_AVG_SA:.1f}\n")

    matchups = get_matchups_today()
    if not matchups:
        print("No games found for today.")
        post_to_google_sheets([])
        return

    opp_sa_cache: dict[str, float] = {}
    teams = sorted(matchups.keys())

    all_rows: list[dict] = []

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
        boost = opp_sa / LEAGUE_AVG_SA if LEAGUE_AVG_SA > 0 else 1.0

        skaters = roster_skaters(team)

        rows = []
        for pid, player_name, pos in skaters:
            time.sleep(SLEEP_BETWEEN_CALLS)

            shots10 = last_n_from_game_log(pid, n=10)
            if shots10 is None:
                continue

            s10 = sum(shots10) / 10.0
            hit2 = sum(1 for s in shots10 if s >= 2) / 10.0
            hit3 = sum(1 for s in shots10 if s >= 3) / 10.0
            sd10 = stddev(shots10)

            adj_sog = s10 * boost

            # same scoring logic, just using sd10 now
            score2 = adj_sog + 0.6 * hit2 - 0.15 * sd10
            score3 = adj_sog + 0.6 * hit3 - 0.20 * sd10

            rows.append((player_name, pos, s10, hit2, hit3, opp_sa, boost, adj_sog, score2, score3))

        forwards = [r for r in rows if r[1] == "F"]
        defense = [r for r in rows if r[1] == "D"]

        forwards.sort(key=lambda x: x[8], reverse=True)  # score2
        defense.sort(key=lambda x: x[8], reverse=True)

        print(f"{team} vs {opp} | Opp SA: {opp_sa:.1f} | Boost: {boost:.2f}")

        # Top 4 forwards + top 1 D
        picked = []

        for r in forwards[:4]:
            player_name, pos, s10, hit2, hit3, opp_sa, boost, adj_sog, score2, score3 = r
            print(f"  {player_name}  S10:{s10:.2f}  H2:{hit2:.2f}  H3:{hit3:.2f}  Adj:{adj_sog:.2f}  Sc2:{score2:.2f}  Sc3:{score3:.2f}")
            picked.append((player_name, team, opp, pos, s10, hit2, hit3, opp_sa, boost, adj_sog, score2, score3))

        if defense:
            r = defense[0]
            player_name, pos, s10, hit2, hit3, opp_sa, boost, adj_sog, score2, score3 = r
            print(f"  {player_name} (D)  S10:{s10:.2f}  H2:{hit2:.2f}  H3:{hit3:.2f}  Adj:{adj_sog:.2f}  Sc2:{score2:.2f}  Sc3:{score3:.2f}")
            picked.append((player_name, team, opp, "D", s10, hit2, hit3, opp_sa, boost, adj_sog, score2, score3))

        print("")

        # add to outbound rows
        for (player_name, t, o, pos, s10, hit2, hit3, opp_sa, boost, adj_sog, score2, score3) in picked:
            all_rows.append({
                "Player": player_name,
                "Team": t,
                "Opp": o,
                "Pos": pos,
                "S10": round(s10, 2),
                "Hit2": round(hit2, 2),
                "Hit3": round(hit3, 2),
                "OppSA": round(opp_sa, 2),
                "Boost": round(boost, 3),
                "AdjSOG": round(adj_sog, 2),
                "Score2": round(score2, 2),
                "Score3": round(score3, 2),
                "Date": TODAY
            })

    post_to_google_sheets(all_rows)
    print("✅ Posted Top 5 per team to Google Sheets")


if __name__ == "__main__":
    main()
