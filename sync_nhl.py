"""
NHL Sync Script pro Tipovačku
- Načte plánované zápasy NHL na příštích 7 dní
- Aktualizuje výsledky pouze pro zápasy které jsou v naší DB jako 'upcoming'
"""

import os
import requests
from datetime import datetime, timedelta, timezone

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
COMPETITION_ID = os.environ["COMPETITION_ID"]

NHL_API = "https://api-web.nhle.com/v1"

HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=representation"
}

DEFAULT_ODDS = {"home": 1.85, "draw": 4.20, "away": 2.00}


def supabase_get(table, params=""):
    r = requests.get(f"{SUPABASE_URL}/rest/v1/{table}{params}", headers=HEADERS)
    r.raise_for_status()
    return r.json()


def supabase_insert(table, data):
    r = requests.post(f"{SUPABASE_URL}/rest/v1/{table}", headers=HEADERS, json=data)
    r.raise_for_status()
    return r.json()


def supabase_update(table, data, match_filter):
    r = requests.patch(
        f"{SUPABASE_URL}/rest/v1/{table}?{match_filter}",
        headers=HEADERS,
        json=data
    )
    r.raise_for_status()
    return r.json()


def get_nhl_schedule(date_str):
    url = f"{NHL_API}/schedule/{date_str}"
    r = requests.get(url, timeout=10)
    if r.status_code != 200:
        print(f"  Schedule API error {r.status_code}")
        return []
    data = r.json()
    games = []
    for day in data.get("gameWeek", []):
        for g in day.get("games", []):
            games.append(g)
    return games


def format_team_name(team_obj):
    city = team_obj.get("placeName", {}).get("default", "")
    name = team_obj.get("commonName", {}).get("default", "")
    if city and name:
        return f"{city} {name}"
    return name or city or "Unknown"


def format_date_cz(game_time_utc):
    try:
        dt = datetime.fromisoformat(game_time_utc.replace("Z", "+00:00"))
        cet = dt + timedelta(hours=2)
        return cet.strftime("%d.%m.%Y %H:%M")
    except Exception:
        return game_time_utc


def sync_schedule():
    """Přidá nové nadcházející zápasy na příštích 7 dní."""
    print("\n📅 Synchronizuji program NHL...")
    today = datetime.now(timezone.utc).date()
    date_str = today.strftime("%Y-%m-%d")

    games = get_nhl_schedule(date_str)

    # Načti existující zápasy
    existing = supabase_get("matches", f"?competition_id=eq.{COMPETITION_ID}&select=home,away,match_date")
    existing_keys = set()
    for m in existing:
        # Klíč podle jmen týmů
        existing_keys.add(f"{m['home']}|{m['away']}")

    added = 0
    for g in games:
        state = g.get("gameState", "")
        # Přidáme POUZE budoucí zápasy (FUT = future, PRE = pre-game)
        if state not in ("FUT", "PRE"):
            continue

        home_obj = g.get("homeTeam", {})
        away_obj = g.get("awayTeam", {})
        home = format_team_name(home_obj)
        away = format_team_name(away_obj)
        game_time = g.get("startTimeUTC", "")
        date_cz = format_date_cz(game_time)

        key = f"{home}|{away}"
        if key in existing_keys:
            print(f"  ⏭ Přeskočen (existuje): {home} – {away}")
            continue

        match_data = {
            "competition_id": COMPETITION_ID,
            "home": home,
            "away": away,
            "match_date": date_cz,
            "status": "upcoming",
            "odds_home": DEFAULT_ODDS["home"],
            "odds_draw": DEFAULT_ODDS["draw"],
            "odds_away": DEFAULT_ODDS["away"],
            "result_home": None,
            "result_away": None,
            "decision": None
        }

        try:
            supabase_insert("matches", match_data)
            print(f"  ✅ Přidán: {home} – {away} ({date_cz})")
            added += 1
        except Exception as e:
            print(f"  ❌ Chyba: {home} – {away}: {e}")

    print(f"  Přidáno: {added} nových zápasů")


def sync_results():
    """Aktualizuje výsledky POUZE pro zápasy které jsou v DB jako 'upcoming' a datum již prošlo."""
    print("\n🏒 Aktualizuji výsledky...")

    # Načti pouze upcoming zápasy z naší DB
    upcoming = supabase_get("matches",
        f"?competition_id=eq.{COMPETITION_ID}&status=eq.upcoming&select=*")

    if not upcoming:
        print("  Žádné upcoming zápasy.")
        return

    # Aktuální čas v CET
    now_cet = datetime.now(timezone.utc) + timedelta(hours=2)

    # Filtruj pouze zápasy jejichž čas již prošel (+ 3 hodiny buffer na dokončení)
    to_check = []
    for m in upcoming:
        try:
            date_str = m["match_date"]
            # Parsuj český formát DD.MM.YYYY HH:MM
            dt = datetime.strptime(date_str, "%d.%m.%Y %H:%M")
            if now_cet > dt.replace(tzinfo=None) + timedelta(hours=3):
                to_check.append(m)
        except Exception:
            pass

    if not to_check:
        print("  Žádné zápasy k vyhodnocení (ještě neskončily).")
        return

    print(f"  Kontroluji {len(to_check)} zápasů...")

    # Načti NHL výsledky pro dnešek a včerejšek
    today = datetime.now(timezone.utc).date()
    yesterday = (today - timedelta(days=1)).strftime("%Y-%m-%d")
    games_today = get_nhl_schedule(today.strftime("%Y-%m-%d"))
    games_yesterday = get_nhl_schedule(yesterday)
    all_games = games_today + games_yesterday

    # Indexuj dokončené zápasy podle jmen týmů
    results_map = {}
    for g in all_games:
        state = g.get("gameState", "")
        if state not in ("OFF", "FINAL", "OVER"):
            continue
        home = format_team_name(g.get("homeTeam", {}))
        away = format_team_name(g.get("awayTeam", {}))
        home_score = g.get("homeTeam", {}).get("score")
        away_score = g.get("awayTeam", {}).get("score")

        # Zjisti způsob rozhodnutí
        period_num = g.get("periodDescriptor", {}).get("number", 3)
        decision = "reg"
        if period_num == 4:
            decision = "ot"
        elif period_num >= 5:
            decision = "so"

        if home_score is not None and away_score is not None:
            results_map[f"{home}|{away}"] = {
                "result_home": home_score,
                "result_away": away_score,
                "decision": decision
            }

    updated = 0
    for m in to_check:
        key = f"{m['home']}|{m['away']}"
        if key not in results_map:
            print(f"  ⏳ Výsledek nenalezen: {m['home']} – {m['away']}")
            continue

        result = results_map[key]
        try:
            supabase_update("matches",
                {**result, "status": "done"},
                f"id=eq.{m['id']}"
            )
            dec = {"reg": "reg. čas", "ot": "prodl.", "so": "náj."}.get(result["decision"], "")
            print(f"  ✅ {m['home']} {result['result_home']}:{result['result_away']} {m['away']} ({dec})")
            updated += 1
        except Exception as e:
            print(f"  ❌ Chyba: {m['home']} – {m['away']}: {e}")

    print(f"  Aktualizováno: {updated} výsledků")


if __name__ == "__main__":
    print("🏒 NHL Sync:", datetime.now().strftime("%Y-%m-%d %H:%M"))
    sync_schedule()
    sync_results()
    print("\n✅ Hotovo!")
