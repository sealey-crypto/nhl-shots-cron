import requests
from datetime import datetime, timezone

BASE = "https://api-web.nhle.com/v1"
TODAY = datetime.now(timezone.utc).strftime("%Y-%m-%d")
SEASON = "20252026"   # change each season
GAME_TYPE = "2"       # 2 = regular season per common usage in this API reference

SESSION = requests.Session()
TIMEOUT = 20

def safe_get_json(url: str) -> dict:
    r = SESSION.get(url, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()

def get_teams_playing_today() -> list[str]:
    """
    Returns a de-duplicated list of 3-letter team abbreviations (e.g., 'NJD', 'SEA').
    The schedule response shape is different than the legacy statsapi, so we parse defensively.
    """
    url = f"{BASE}/schedule/{TODAY}"
    data = safe_get_json(url)

    teams = set()

    # Common shape: data["gameWeek"] -> list of days -> day["games"] -> game["homeTeam"/"awayTeam"]["abbrev"]
    if "gameWeek" in data:
        for day in data.get("gameWeek", []):
            for g in day.get("games", []):
                home = (g.get("homeTeam") or {}).get("abbrev")
                away = (g.get("awayTeam") or {}).get("abbrev")
                if home: teams.add(home)
                if away: teams.add(away)

    # Fallback: sometimes APIs return a direct "games" list
    if not teams and "games" in data:
        for g in data.get("games", []):
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

    players = []

    # Common shape: forwards/defensemen/goalies arrays
    for group_key, pos_code in [("forwards", "F"), ("defensemen", "D")]:
        for p in data.get(group_key, []):
            pid = p.get("id")
            first = (p.get("firstName") or {}).get("default") or p.get("firstName")
            last = (p.get("lastName") or {}).get("default") or p.get("lastName")
            name = " ".join([x for x in [first, last] if x]) or p.get("fullName") or "Unknown"

            if isinstance(pid, int):
                players.append((pid, name, pos_code))

    return players

def last5_avg_shots(player_id: int) -> float | None:
    """
    Endpoint: /v1/player/{player}/game-log/{season}/{game-type}
    We take the most recent 5 games that have a 'shots' field.
    """
    url = f"{BASE}/player/{player_id}/game-log/{SEASON}/{GAME_TYPE}"
    data = safe_get_json(url)

    game_log = data.get("gameLog") or data.get("gamelog") or data.get("games") or []
    if not isinstance(game_log, list) or not game_log:
        return None

    shots = []
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
        print("No teams found for today. (Schedule parse returned empty list.)")
        return

    for team in teams:
        roster = get_roster(team)

        results = []
        for pid, name, pos in roster:
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
