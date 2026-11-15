"""Publish only qualified, current-day NHL shot research legs to the public dashboard.

Requires GOOGLE_CREDENTIALS_JSON, MODEL_B_SHEET_ID, MODEL_C_SHEET_ID and
GH_PUBLISH_TOKEN. Never publish credentials, entire sheets, or historical rows.
"""
import base64
import datetime as dt
import json
import os
from zoneinfo import ZoneInfo
import requests
import gspread
from google.oauth2.service_account import Credentials

ZONE = ZoneInfo("America/Toronto")
REPO = "sealey-crypto/nhl-shots-cron"
TARGET = "dashboard/picks.json"
API = f"https://api.github.com/repos/{REPO}/contents/{TARGET}"

def number(value):
    try: return float(str(value).replace("%","").replace(",","").strip())
    except (ValueError,TypeError): return None

def percent(value):
    n = number(value)
    return n / 100 if n is not None and "%" in str(value) else n

def american_matches_decimal(american, decimal):
    """Reject missing, malformed or contradictory sportsbook price pairs."""
    try:
        raw = str(american).strip().replace(",", "")
        if not raw or "." in raw:
            return False
        price = int(raw)
        if -100 < price < 100:
            return False
        expected = 1 + (price / 100 if price > 0 else 100 / abs(price))
        return abs(expected - decimal) <= 0.012
    except (ValueError, TypeError, ZeroDivisionError):
        return False

def read(book, title):
    try: return book.worksheet(title).get_all_values()
    except gspread.WorksheetNotFound: return []

def sheet_slate_date(value):
    """Normalize only unambiguous Sheets date representations to ISO day."""
    raw = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%m/%d/%Y", "%m/%d/%Y %H:%M:%S"):
        try:
            return dt.datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            pass
    return None

def model_b(book, today):
    rows = read(book, "Model B Auto Parlays")
    if len(rows) < 11 or len(rows[1]) < 2 or sheet_slate_date(rows[1][1]) != today:
        print(f"Model B source gate: rows={len(rows)}, source_date={rows[1][1] if len(rows)>1 and len(rows[1])>1 else 'missing'}, slate={today}")
        return []
    out = []
    for cells in rows[10:]:
        cells += [""] * max(0, 15-len(cells))
        player, team, opp, market, american, dec, _, prob, _, ev, start, game, eligibility = cells[:13]
        if eligibility != "ELIGIBLE" or not market.startswith(("2+ SOG","3+ SOG")): continue
        probability, edge, decimal = percent(prob), percent(ev), number(dec)
        if not all((player, game, start, probability is not None, edge is not None, decimal)):
            continue
        try: game_day = dt.datetime.fromisoformat(start.replace("Z","+00:00")).astimezone(ZONE).date().isoformat()
        except ValueError: continue
        if game_day != today or edge <= 0 or not 0 < probability < 1 or decimal <= 1: continue
        # A newly published snapshot must not revive an already-started game's
        # pregame quote. The source start field is an ISO timestamp with offset.
        try:
            game_start = dt.datetime.fromisoformat(start.replace("Z", "+00:00"))
            if game_start.tzinfo is None or game_start <= dt.datetime.now(dt.timezone.utc):
                continue
        except (ValueError, TypeError):
            continue
        # Fail closed on inconsistent source odds/probability/EV.
        if abs((probability * decimal - 1) - edge) > 0.015: continue
        if not american_matches_decimal(american, decimal): continue
        out.append(dict(model="B",player=player,team=team,opp=opp,market=market,
                        american_odds=american,decimal_odds=decimal,probability=probability,
                        ev=edge,game_id=str(game),game_start_utc=game_start.astimezone(dt.timezone.utc).isoformat(),status="ELIGIBLE"))
    return out

def model_c(book, today):
    forecasts = read(book, "Daily Forecasts")
    picks = read(book, "Paper Picks")
    if len(forecasts) < 2 or len(picks) < 2:
        print(f"Model C source gate: forecasts={max(0,len(forecasts)-1)}, paper_picks={max(0,len(picks)-1)}")
        return []
    fheaders = {k:i for i,k in enumerate(forecasts[0])}
    pheaders = {k:i for i,k in enumerate(picks[0])}
    if not {"game_date","player","team","opp"}.issubset(fheaders): return []
    if not {"forecast_utc","player","team","opp","line","side","book","american_odds","decimal_odds","model_probability","estimated_ev","game_id"}.issubset(pheaders): return []
    known = set()
    for r in forecasts[1:]:
        def fv(k): return r[fheaders[k]] if len(r)>fheaders[k] else ""
        if sheet_slate_date(fv("game_date")) == today: known.add((fv("player"),fv("team"),fv("opp"),fv("game_id") if "game_id" in fheaders else None))
    out=[]
    for r in picks[1:]:
        def v(k): return r[pheaders[k]] if len(r)>pheaders[k] else ""
        if (v("player"),v("team"),v("opp"),v("game_id") if "game_id" in fheaders else None) not in known: continue
        if v("side")!="OVER" or v("book").lower()!="fanduel": continue
        line=number(v("line"))
        # Sportsbook total 1.5 corresponds to 2+; total 2.5 to 3+.
        if line not in (1.5,2.5): continue
        # A forecast can be generated the evening before its game. Its
        # timestamp is NOT the slate date; the verified game start below
        # determines the Toronto slate date. Still reject stale/future runs.
        stamp=v("forecast_utc")
        try:
            forecast_time = dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            now_utc = dt.datetime.now(dt.timezone.utc)
            if forecast_time.tzinfo is None or not (
                dt.timedelta(0) <= now_utc - forecast_time <= dt.timedelta(hours=36)
            ):
                continue
        except (ValueError, TypeError, AttributeError):
            continue
        # Publication time is not quote time. Fail closed until upstream
        # supplies both a verified game start and actual odds capture time.
        # This prevents recycling old pregame prices as live research.
        start_col = next((k for k in ("game_start_utc", "start_time_utc", "game_start") if k in pheaders), None)
        quote_col = next((k for k in ("odds_captured_at_utc", "quote_captured_at_utc", "odds_snapshot_utc") if k in pheaders), None)
        if not start_col or not quote_col: continue
        try:
            game_start = dt.datetime.fromisoformat(v(start_col).replace("Z", "+00:00"))
            captured = dt.datetime.fromisoformat(v(quote_col).replace("Z", "+00:00"))
            now = dt.datetime.now(dt.timezone.utc)
            if not game_start.tzinfo or not captured.tzinfo: continue
            if not (captured <= now < game_start): continue
            # The slate date must be the actual NHL game date in Toronto,
            # not merely the forecast date or UTC calendar date.
            if game_start.astimezone(ZONE).date().isoformat() != today: continue
            if now - captured > dt.timedelta(hours=4): continue
            if game_start - captured > dt.timedelta(days=2): continue
        except (ValueError, TypeError, AttributeError): continue
        prob,ev,decimal=number(v("model_probability")),number(v("estimated_ev")),number(v("decimal_odds"))
        if prob is None or ev is None or decimal is None or not 0<prob<1 or ev<=0 or decimal<=1:continue
        # Reject mismatched or malformed price/probability/EV combinations.
        if abs((prob * decimal - 1) - ev) > 0.015: continue
        if not american_matches_decimal(v("american_odds"), decimal): continue
        out.append(dict(model="C",player=v("player"),team=v("team"),opp=v("opp"),
                        market=f"{int(line+.5)}+ SOG",american_odds=v("american_odds"),
                        decimal_odds=decimal,probability=prob,ev=ev,game_id=str(v("game_id")),
                        game_start_utc=game_start.astimezone(dt.timezone.utc).isoformat(),
                        odds_captured_at_utc=captured.astimezone(dt.timezone.utc).isoformat(),
                        status="ELIGIBLE"))
    # Keep one record per player/market/game; do not maximize EV across reruns,
    # since that would cherry-pick potentially stale historical quotes.
    dedup={}
    for leg in out:
        key=(leg["model"],leg["player"],leg["team"],leg["opp"],leg["market"],leg["game_id"])
        # Prefer the most recently captured verified quote, not the
        # largest EV. Earlier rows can represent superseded prices.
        previous = dedup.get(key)
        if previous is None or leg["odds_captured_at_utc"] > previous["odds_captured_at_utc"]:
            dedup[key] = leg
    return list(dedup.values())

def verify_roster_and_add_photos(legs):
    """Verify current NHL team membership and attach official NHL headshot IDs.
    Fail closed on team mismatches; if NHL API is unavailable, omit unverified legs.
    """
    cache = {}
    verified = []
    for leg in legs:
        team = leg["team"]
        if team not in cache:
            try:
                response = requests.get(f"https://api-web.nhle.com/v1/roster/{team}/current", timeout=12)
                response.raise_for_status()
                data = response.json()
                roster = {}
                for group in ("forwards", "defensemen", "goalies"):
                    for p in data.get(group, []):
                        first = p.get("firstName", {}).get("default", "")
                        last = p.get("lastName", {}).get("default", "")
                        if first and last and p.get("id"):
                            roster[(first + " " + last).casefold()] = str(p["id"])
                cache[team] = roster
            except (requests.RequestException, ValueError) as exc:
                print(f"Roster verification unavailable for {team}: {exc}")
                cache[team] = None
        roster = cache[team]
        if roster is None:
            continue  # No unverified team/player combinations in public feed.
        player_id = roster.get(leg["player"].casefold())
        if not player_id:
            print(f"Quarantined mismatched player/team: {leg['player']} / {team}")
            continue
        leg["player_id"] = player_id
        verified.append(leg)
    return verified

def main():
    creds = Credentials.from_service_account_info(json.loads(os.environ["GOOGLE_CREDENTIALS_JSON"]),
                 scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"])
    client = gspread.authorize(creds)
    today=dt.datetime.now(ZONE).date().isoformat()
    b=model_b(client.open_by_key(os.environ["MODEL_B_SHEET_ID"]),today)
    c=model_c(client.open_by_key(os.environ["MODEL_C_SHEET_ID"]),today)
    print(f"Pre-roster qualified: Model B={len(b)} Model C={len(c)}")
    legs=verify_roster_and_add_photos(b+c)
    print(f"Post-roster qualified: total={len(legs)}")
    b=[x for x in legs if x['model']=='B']
    c=[x for x in legs if x['model']=='C']
    payload=dict(schema_version=1,snapshot_date=today,
        generated_at_utc=dt.datetime.now(dt.timezone.utc).isoformat(),
        legs=legs,model_b_count=len(b),model_c_count=len(c),
        note="Research only; model EV is unvalidated. No combined parlay prices implied.")
    token=os.environ["GH_PUBLISH_TOKEN"]
    headers={"Authorization":f"Bearer {token}","Accept":"application/vnd.github+json"}
    existing=requests.get(API,headers=headers,timeout=30)
    existing.raise_for_status()
    content=base64.b64encode((json.dumps(payload,indent=2)+"\n").encode()).decode()
    result=requests.put(API,headers=headers,timeout=30,json={
        "message":f"Publish sanitized NHL research snapshot {today}",
        "content":content,"sha":existing.json()["sha"],"branch":"main"})
    result.raise_for_status()
    print(f"Published snapshot {today}: Model B={len(b)} Model C={len(c)}")
if __name__=="__main__": main()
