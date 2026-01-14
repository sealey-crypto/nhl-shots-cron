import time
import requests
from datetime import datetime
from zoneinfo import ZoneInfo

BASE = "https://api-web.nhle.com/v1"
TZ = ZoneInfo("America/Toronto")
TODAY = datetime.now(TZ).strftime("%Y-%m-%d")

SESSION = requests.Session()
TIMEOUT = 20

# Polite pacing (prevents 429s)
SLEEP_BETWEEN_CALLS = 0.15

def get_json(url: str, max_retries: int = 6) -> dict:
    backoff = 0.6
    for _ in range(max_retries):
        r = SESSION.get(url, timeout=TIMEOUT)

        # Rate limited
        if r.status_code == 429:
            retry_after = r.headers.get("Retry-After")
            wait = float(retry_after) if (retry_after and retry_after.isdigit()) else backoff
            time.sleep(wait)
            backoff = min(backoff * 2, 12)
            continue

        r.raise_for_status()
        return r.json()

    raise RuntimeError(f"Failed after retries: {url}")

def teams_playing_today() -> list[str]:
    """
    Uses daily score endpoint so we ONLY get today's games.
    Endpoint documented as /v1/score/{date}.
    """
    data = get_json(f"{BASE}/score/{TODAY}")
    teams = set()

    for g in data.get("games", []):
        home = (g.get("homeTeam") or {}).get("abbrev")
        away = (g.get("awayTeam") or {}).get("abbrev")
        if home: teams.add(home)
        if away: teams.add(away)

    return sorted(teams)

def roster_skater_ids(team_abbrev: str) -> list[tuple[int, str, str]]:
    """
    Current roster endpoint: /v1/roster/{team}/current
    Returns (player_id, name, pos_code) for F and D only (no goalies).
    """
    data = get_json(f"{BASE}/roster/{team_abbrev}/current")
    out = []

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

def last5_avg_shots_from_landing(player_id: int) -> float | None:
    """
    Player landing endpoint: /v1/player/{player}/landing
    Often includes last 5 games info (varies by season/data availability),
    so we try a few common shapes and compute average shots.
    """
    data = get_json(f"{BASE}/player/{player_id}/landing")

    # Common candidates where last-5 might live
    candidates = []
    for key in ["last5Games", "lastFiveGames", "last5", "recentGames"]:
        v = data.get(key)
        if isinstance(v, list):
            candidates = v
            break

    # If the API doesn't give last-5 in landing, return None
    if not candidates:
        return None

    shots = []
    for g in candidates[:5]:
        s = g.get("shots")
        if s is None and isinstance(g.get("skaterStats"), dict):
            s = g["skaterStats"].get("shots")
        if isinstance(s, int):
            shots.append(s)

    if len(shots) < 5:
        return None

    return sum(shots) / 5.0

def main():
    print(f"\nNHL Shot Leaders (Last 5 Games) — {TODAY}\n")
    print("VERSION: v4 (score endpoint + landing last5)\n")

    teams = teams_playing_today()
    if not teams:
        print("No games found for today.")
        return

    for team in teams:
        skaters = roster_skater_ids(team)

        results = []
        for pid, name, pos in skaters:
            time.sleep(SLEEP_BETWEEN_CALLS)
            avg = last5_avg_shots_from_landing(pid)
            if avg is None:
                continue
            results.append((name, pos, avg))

        # Sort and print: 4 forwards + 1 defense if available
        forwards = [r for r in results if r[1] == "F"]
        defense = [r for r in results if r[1] == "D"]

        forwards.sort(key=lambda x: x[2], reverse=True)
        defense.sort(key=lambda x: x[2], reverse=True)

        print(team)
        for name, pos, avg in forwards[:4]:
            print(f"  {name}  {avg:.2f}")

        if defense:
            name, pos, avg = defense[0]
            print(f"  {name} (D)  {avg:.2f}")
        print("")

if __name__ == "__main__":
    main()
