import time
import requests
from datetime import datetime
from zoneinfo import ZoneInfo
from math import sqrt

BASE = "https://api-web.nhle.com/v1"
TZ = ZoneInfo("America/Toronto")
TODAY = datetime.now(TZ).strftime("%Y-%m-%d")

SEASON = "20252026"
GAME_TYPE = "2"  # regular season

SESSION = requests.Session()
TIMEOUT = 20

# Polite pacing to avoid 429s
SLEEP_BETWEEN_CALLS = 0.18

# Baseline league shots against per game (all situations) used for boost
# Kept constant to avoid extra API calls and rate limits.
LEAGUE_AVG_SA = 30.0


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

    def name(p: dict) -> str:
        first = (p.get("firstName") or {}).get("default") or p.get("firstName")
        last = (p.get("lastName") or {}).get("default") or p.get("lastName")
        return (" ".join([x for x in [first, last] if x]) or p.get("fullName") or "Unknown").strip()

    for group_key, pos in [("forwards", "F"), ("defensemen", "D")]:
        for p in data.get(group_key, []):
            pid = p.get("id")
            if isinstance(pid, int):
                out.append((pid, name(p), pos))

    return out


def last5_shots_from_landing(player_id: int) -> list[int] | None:
    """
    Uses landing endpoint and tries to extract last 5 games shots as a list of ints.
    If last-5 is not available for a player, returns None.
    """
    data = get_json(f"{BASE}/player/{player_id}/landing")

    candidates = []
    for key in ["last5Games", "lastFiveGames", "last5", "recentGames"]:
        v = data.get(key)
        if isinstance(v, list):
            candidates = v
            break

    if not candidates:
        return None

    shots: list[int] = []
    for g in candidates[:5]:
        s = g.get("shots")
        if s is None and isinstance(g.get("skaterStats"), dict):
            s = g["skaterStats"].get("shots")
        if isinstance(s, int):
            shots.append(s)

    if len(shots) < 5:
        return None

    return shots[:5]


def stddev(vals: list[int]) -> float:
    m = sum(vals) / len(vals)
    var = sum((v - m) ** 2 for v in vals) / len(vals)
    return sqrt(var)


def club_shots_against_per_game(team_abbrev: str) -> float | None:
    """
    Compute opponent shots against per game (all situations) from club-stats endpoint.

    club-stats provides:
      - goalies[].shotsAgainst  (raw totals)
      - skaters[].gamesPlayed   (we use max skater GP as team games played)

    We compute:
      shotsAgainstPerGame = sum(goalies.shotsAgainst) / max(skaters.gamesPlayed)
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


def main():
    print(f"\nNHL Shot Parlay Board - Last 5 SOG - {TODAY}\n")
    print(f"Boost baseline (league SA): {LEAGUE_AVG_SA:.1f}\n")

    matchups = get_matchups_today()
    if not matchups:
        print("No games found for today.")
        return

    opp_sa_cache: dict[str, float] = {}
    teams = sorted(matchups.keys())

    for team in teams:
        opp = matchups.get(team)
        if not opp:
            continue

        # Pull opponent shots against per game (cached)
        if opp not in opp_sa_cache:
            time.sleep(SLEEP_BETWEEN_CALLS)
            opp_sa = club_shots_against_per_game(opp)
            opp_sa_cache[opp] = opp_sa if opp_sa is not None else LEAGUE_AVG_SA

        opp_sa = opp_sa_cache[opp]
        boost = opp_sa / LEAGUE_AVG_SA if LEAGUE_AVG_SA > 0 else 1.0

        if opp_sa == LEAGUE_AVG_SA:
            print(f"(note) Opp SA fallback used for {opp}")

        skaters = roster_skaters(team)

        rows = []
        for pid, name, pos in skaters:
            time.sleep(SLEEP_BETWEEN_CALLS)

            shots5 = last5_shots_from_landing(pid)
            if shots5 is None:
                continue

            s5 = sum(shots5) / 5.0
            hit2 = sum(1 for s in shots5 if s >= 2) / 5.0
            hit3 = sum(1 for s in shots5 if s >= 3) / 5.0
            sd5 = stddev(shots5)

            adj_sog = s5 * boost

            # Ranking scores (parlay-friendly)
            score2 = adj_sog + 0.6 * hit2 - 0.15 * sd5
            score3 = adj_sog + 0.6 * hit3 - 0.20 * sd5

            rows.append((name, pos, s5, hit2, hit3, sd5, opp_sa, boost, adj_sog, score2, score3))

        forwards = [r for r in rows if r[1] == "F"]
        defense = [r for r in rows if r[1] == "D"]

        forwards.sort(key=lambda x: x[9], reverse=True)  # Score2
        defense.sort(key=lambda x: x[9], reverse=True)

        print(f"{team} vs {opp} | Opp SA: {opp_sa:.1f} | Boost: {boost:.2f}\n")

        for r in forwards[:4]:
            name, pos, s5, hit2, hit3, sd5, opp_sa, boost, adj_sog, score2, score3 = r
            print(
                f"  {name}  "
                f"S5:{s5:.2f}  H2:{hit2:.2f}  H3:{hit3:.2f}  "
                f"Adj:{adj_sog:.2f}  Sc2:{score2:.2f}  Sc3:{score3:.2f}"
            )

        if defense:
            r = defense[0]
            name, pos, s5, hit2, hit3, sd5, opp_sa, boost, adj_sog, score2, score3 = r
            print(
                f"  {name} (D)  "
                f"S5:{s5:.2f}  H2:{hit2:.2f}  H3:{hit3:.2f}  "
                f"Adj:{adj_sog:.2f}  Sc2:{score2:.2f}  Sc3:{score3:.2f}"
            )

        print("")


if __name__ == "__main__":
    main()
