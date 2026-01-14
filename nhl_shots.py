import time
import requests
from datetime import datetime, timezone

BASE = "https://api-web.nhle.com/v1"
TODAY = datetime.now(timezone.utc).strftime("%Y-%m-%d")

SEASON = "20252026"   # update each season
GAME_TYPE = "2"       # 2 = regular season

SESSION = requests.Session()
TIMEOUT = 20

# Throttle to avoid bursts (tune if needed)
PER_REQUEST_SLEEP_SEC = 0.12

def safe_get_json(url: str, max_retries: int = 6) -> dict:
    """
    GET JSON with retry/backoff for 429 and transient errors.
    """
    backoff = 0.5
    for attempt in range(max_retries):
        r = SESSION.get(url, timeout=TIMEOUT)

        # Rate limited
        if r.status_code == 429:
            retry_after = r.headers.get("Retry-After")
            if retry_after and retry_after.isdigit():
                wait = float(retry_after)
            else:
                wait = backoff
                backoff = min(backoff * 2, 10)

            time.sleep(wait)
            continue

        # Other errors
        if r.status_code >= 400:
            r.raise_for_status()

        return r.json()

    raise RuntimeError(f"Failed after retries (rate limited or unavailable): {url}")

def get_teams_playing_today() -> list[str]:
    """
    Returns team abbreviations (e.g., 'NJD', 'SEA') ONLY for TODAY.
    IMPORTANT: schedule/{date} may return a week object; we filter to the day that matches TODAY.
    """
    url = f"{BASE}/schedule/{TODAY}"
    data = safe_get_json(url)

    teams = set()

    # Most common shape: data["gameWeek"] -> list of days -> day["date"] and day["games"]
    for day in data.get("gameWeek", []):
        # day["date"] is typically "YYYY-MM-DD"
        if (day.get("date") or "") != TODAY:
            continue
        for g in day.get("games", []):
            home = (g.get("homeTeam") or {}).get("abbrev")
            away = (g.get("awayTeam") or {}).get("abbrev")
            if home: teams.add(home)
            if away: teams.add(away)

    # Fallback: if API ever returns just "games"
    if not teams and "games" in data:
        for g in data.get("games", []):
            # Some variants include gameDate; filter if present
            game_date = (g.get("gameDate") or "")[:10]
            if game_date and game_date != TODAY:
                continue
            home = (g.get("homeTeam") or {}).get("abbrev")
            away = (g.get("awayTeam") or {}).get("abbrev")
            if home: teams.add(home)
            if away: teams.add(away)

    return sorted(teams)

def get_roster(team_abbrev: str) -> list[tuple[int, str, str]]:
    """
    Returns list of (player_id, full_name, position_code) for skaters only.
    Endpoint: /v1/roster/{team}/current
    """
    url = f"{BASE}/roster/{team_abbrev}/current"
    data = safe_get_json(url)

    players: list[tuple[int, str, str]] = []

    def _name(p: dict) -> str:
        first = (p.get("firstName") or {}).get("default") or p.get("firstName")
        last = (p.get("lastName") or {}).get("default") or p.get("lastName")
        return (" ".join([x for x in [first, last] if x]) or p.get("fullName") or "Unknown").strip()

    for group_key, pos_code in [("forwards", "F"), ("defensemen", "D")]:
        for p in data.get(group_key, []):
            pid = p.get("id")
            if isinstance(pid, int):
                players.append((pid, _name(p), pos_code))

    return players

def last5_avg_shots(player_id: int) -> float | None:
    """
    Endpoint: /v1/player/{player}/game-log/{season}/{game-type}
    We take the most recent 5 games that contain 'shots'.
    """
    url = f"{BASE}/player/{player_id}/game-log/{SEASON}/{GAME_TYPE}"
    data = safe_get_json(url)

    game_log = data.get("gameLog") or data.get("gamelog") or data.get("games") or []
    if not isinstance(game_log, list) or not game_log:
        return None

    shots: list[int] = []
    for g in game_log:
        s = g.get("shots")
        if s is None:
            s = (g.get("skaterStats") or {}).get("shots")
        if isinstance(s, int):
            shots.append(s)
        if len(shots) == 5:
            break

    if len(shots) < 5:
        return None

    return sum(shots) / 5.0

def main():
    print(f"\nNHL Shot Leaders (Last 5 Games) — {TODAY}\n")

    teams = get_teams_playing_today()
    if not teams:
        print("No teams found for today (schedule parse returned empty).")
        return

    for team in teams:
        roster = get_roster(team)

        results: list[tuple[str, str, float]] = []
        for pid, name, pos in roster:
            # gentle throttle to reduce 429s
            time.sleep(PER_REQUEST_SLEEP_SEC)

            avg = last5_avg_shots(pid)
            if avg is None:
                continue
            results.append((name, pos, avg))

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
