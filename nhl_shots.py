import requests
from datetime import datetime

API = "https://statsapi.web.nhl.com/api/v1"

TODAY = datetime.utcnow().strftime("%Y-%m-%d")

def get_today_games():
    url = f"{API}/schedule?date={TODAY}"
    data = requests.get(url).json()
    games = []

    for date in data.get("dates", []):
        for g in date.get("games", []):
            games.append({
                "home": g["teams"]["home"]["team"],
                "away": g["teams"]["away"]["team"]
            })
    return games

def get_roster(team_id):
    url = f"{API}/teams/{team_id}/roster"
    return requests.get(url).json()["roster"]

def get_last5_sog(player_id):
    season = "20252026"
    url = f"{API}/people/{player_id}/stats?stats=gameLog&season={season}"
    data = requests.get(url).json()

    splits = data["stats"][0]["splits"]
    if len(splits) < 5:
        return None

    last5 = splits[:5]
    shots = [int(g["stat"]["shots"]) for g in last5]
    return sum(shots) / 5.0

def main():
    print(f"\nNHL Shot Leaders (Last 5 Games) — {TODAY}\n")

    games = get_today_games()
    seen = set()

    for g in games:
        for team in [g["home"], g["away"]]:
            if team["id"] in seen:
                continue
            seen.add(team["id"])

            roster = get_roster(team["id"])
            players = []

            for p in roster:
                pid = p["person"]["id"]
                name = p["person"]["fullName"]
                pos = p["position"]["code"]

                avg = get_last5_sog(pid)
                if avg is None:
                    continue

                players.append((name, pos, avg))

            forwards = [p for p in players if p[1] != "D"]
            defense = [p for p in players if p[1] == "D"]

            forwards.sort(key=lambda x: x[2], reverse=True)
            defense.sort(key=lambda x: x[2], reverse=True)

            print(f"{team['name'].upper()}")
            for p in forwards[:4]:
                print(f"  {p[0]}  {p[2]:.2f}")

            if defense:
                d = defense[0]
                print(f"  {d[0]} (D)  {d[2]:.2f}")
            print("")

if __name__ == "__main__":
    main()
