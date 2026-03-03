import os
import time
import random
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

# Polite pacing
SLEEP_BETWEEN_CALLS = 0.6

LEAGUE_AVG_SA = 28.0


def today_str() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d")


TODAY = today_str()


# ---------------------------
# Robust API Fetcher
# ---------------------------
def get_json(url: str, max_retries: int = 12) -> dict:
    backoff = 0.8

    for _ in range(max_retries):
        try:
            r = SESSION.get(url, timeout=TIMEOUT)

            # Rate limit handling
            if r.status_code == 429:
                retry_after = r.headers.get("Retry-After")
                if retry_after and retry_after.strip().isdigit():
                    wait = float(retry_after)
                else:
                    wait = backoff

                wait = min(wait + random.uniform(0, 0.4), 30)
                time.sleep(wait)
                backoff = min(backoff * 1.8, 30)
                continue

            # Temporary server errors
            if 500 <= r.status_code < 600:
                time.sleep(min(backoff + random.uniform(0, 0.4), 30))
                backoff = min(backoff * 1.8, 30)
                continue

            r.raise_for_status()
            return r.json()

        except (requests.Timeout, requests.ConnectionError):
            time.sleep(min(backoff + random.uniform(0, 0.4), 30))
            backoff = min(backoff * 1.8, 30)
            continue

    raise RuntimeError(f"Failed after retries: {url}")


# ---------------------------
# Matchups
# ---------------------------
def get_matchups_today() -> dict[str, str]:
    data = get_json(f"{BASE}/score/{TODAY}")
    matchups: dict[str, str] = {}

    for g in data.get("games", []):
        home = (g.get("homeTeam") or {}).get("abbrev")
        away = (g.get("awayTeam") or {}).get("abbrev")
        if home and away:
            matchups[home] = away
            matchups[away] = home

    return matchups


# ---------------------------
# Roster
# ---------------------------
def roster_skaters(team_abbrev: str) -> list[tuple[int, str, str]]:
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


# ---------------------------
# Stats helpers
# ---------------------------
def stddev(vals: list[int]) -> float:
    m = sum(vals) / len(vals)
    var = sum((v - m) ** 2 for v in vals) / len(vals)
    return sqrt(var)


def last_n_from_game_log(player_id: int, n: int = 10) -> list[int] | None:
    url = f"{BASE}/player/{player_id}/game-log/{SEASON}/{GAME_TYPE}"
    data = get_json(url)

    games = data.get("gameLog") or data.get("games") or []
    if not isinstance(games, list) or not games:
        return None

    shots = []
    for g in games:
        s = g.get("shots")
        if isinstance(s, int):
            shots.append(s)

    if len(shots) < n:
        return None

    return shots[:n]


def club_shots_against_per_game(team_abbrev: str) -> float | None:
    data = get_json(f"{BASE}/club-stats/{team_abbrev}/{SEASON}/{GAME_TYPE}")

    skaters = data.get("skaters", [])
    goalies = data.get("goalies", [])

    if not skaters or not goalies:
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

    return sum(sa_vals) / team_gp


# ---------------------------
# Google Sheets
# ---------------------------
def post_to_google_sheets(rows: list[dict]) -> None:
    url = os.getenv("GS_WEBHOOK_URL", "").strip()
    secret = os.getenv("GS_SECRET", "").strip()

    if not url or not secret:
        print("GS webhook not configured.")
        return

    payload = {"secret": secret, "rows": rows}
    print(f"Posting {len(rows)} rows to Google Sheets...")
    r = SESSION.post(url, json=payload, timeout=TIMEOUT)
    print(f"Webhook status: {r.status_code}")
    print(f"Webhook response: {r.text[:200]}")


# ---------------------------
# Main
# ---------------------------
def main():
    print(f"\nNHL Shot Parlay Board - Last 10 SOG - {TODAY}\n")
    print(f"Boost baseline (league SA): {LEAGUE_AVG_SA:.1f}\n")

    matchups = get_matchups_today()
    if not matchups:
        print("No games found.")
        post_to_google_sheets([])
        return

    opp_sa_cache: dict[str, float] = {}
    all_rows: list[dict] = []

    for team in sorted(matchups.keys()):
        opp = matchups.get(team)
        if not opp:
            continue

        # Opponent SA
        if opp not in opp_sa_cache:
            try:
                time.sleep(SLEEP_BETWEEN_CALLS + random.uniform(0, 0.25))
                opp_sa = club_shots_against_per_game(opp)
                opp_sa_cache[opp] = opp_sa if opp_sa is not None else LEAGUE_AVG_SA
            except RuntimeError:
                print(f"⚠️ Failed opponent SA for {opp}, using league avg.")
                opp_sa_cache[opp] = LEAGUE_AVG_SA

        opp_sa = opp_sa_cache[opp]
        boost = opp_sa / LEAGUE_AVG_SA if LEAGUE_AVG_SA > 0 else 1.0

        try:
            skaters = roster_skaters(team)
        except RuntimeError as e:
            print(f"⚠️ Skipping {team} roster due to error: {e}")
            continue

        rows = []

        for pid, player_name, pos in skaters:
            try:
                time.sleep(SLEEP_BETWEEN_CALLS + random.uniform(0, 0.25))
                shots10 = last_n_from_game_log(pid, n=10)
            except RuntimeError:
                print(f"⚠️ Skipping {player_name} ({team}) due to game-log error.")
                continue

            if shots10 is None:
                continue

            s10 = sum(shots10) / 10.0
            hit2 = sum(1 for s in shots10 if s >= 2) / 10.0
            hit3 = sum(1 for s in shots10 if s >= 3) / 10.0
            sd10 = stddev(shots10)

            adj_sog = s10 * boost
            score2 = adj_sog + 0.6 * hit2 - 0.15 * sd10
            score3 = adj_sog + 0.6 * hit3 - 0.20 * sd10

            rows.append((player_name, pos, s10, hit2, hit3, opp_sa, boost, adj_sog, score2, score3))

        forwards = [r for r in rows if r[1] == "F"]
        defense = [r for r in rows if r[1] == "D"]

        forwards.sort(key=lambda x: x[8], reverse=True)
        defense.sort(key=lambda x: x[8], reverse=True)

        print(f"{team} vs {opp} | Opp SA: {opp_sa:.1f} | Boost: {boost:.2f}")

        picked = []

        for r in forwards[:4]:
            print(f"  {r[0]}  S10:{r[2]:.2f}  H2:{r[3]:.2f}  H3:{r[4]:.2f}")
            picked.append((r[0], team, opp, r[1], *r[2:]))

        if defense:
            r = defense[0]
            print(f"  {r[0]} (D)  S10:{r[2]:.2f}  H2:{r[3]:.2f}  H3:{r[4]:.2f}")
            picked.append((r[0], team, opp, r[1], *r[2:]))

        print("")

        for row in picked:
            player_name, t, o, pos, s10, hit2, hit3, opp_sa, boost, adj_sog, score2, score3 = row
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
    print("Done.")


if __name__ == "__main__":
    main()
