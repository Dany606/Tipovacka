"""
NHL Sync Script pro Tipovačku
- Načte plánované zápasy na příštích 7 dní
- Aktualizuje výsledky odehraných zápasů
- Běží automaticky přes GitHub Actions každý den
"""

import os
import json
import requests
from datetime import datetime, timedelta, timezone

# ── KONFIGURACE ──────────────────────────────────────────────────────
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]  # service_role key (ne anon!)
COMPETITION_ID = os.environ["COMPETITION_ID"]  # ID aktivní NHL soutěže

NHL_API = "https://api-web.nhle.com/v1"

HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=representation"
}

# Kurzy – odhadované pro playoff (lze ručně upravit)
# Pokud oba týmy neznáme, použijeme default
DEFAULT_ODDS = {"home": 1.85, "draw": 4.20, "away": 2.00}

# Základní kurzy podle kontextu playoff
TEAM_ODDS = {
    # Formát: "HomeTeam vs AwayTeam": (home_odds, draw_odds, away_odds)
    # Přidej ručně pokud chceš přesnější kurzy
}


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
    """Načte zápasy NHL pro daný týden (od zadaného data)."""
    url = f"{NHL_API}/schedule/{date_str}"
    r = requests.get(url, timeout=10)
    if r.status_code != 200:
        print(f"  Schedule API error {r.status_code} pro {date_str}")
        return []
    data = r.json()
    games = []
    for day in data.get("gameWeek", []):
        for g in day.get("games", []):
            games.append(g)
    return games


def get_nhl_game_result(game_id):
    """Načte výsledek konkrétního zápasu."""
    url = f"{NHL_API}/gamecenter/{game_id}/landing"
    r = requests.get(url, timeout=10)
    if r.status_code != 200:
        return None
    return r.json()


def format_team_name(team_obj):
    """Sestaví celé jméno týmu z NHL API objektu."""
    city = team_obj.get("placeName", {}).get("default", "")
    name = team_obj.get("commonName", {}).get("default", "")
    if city and name:
        return f"{city} {name}"
    return name or city or "Unknown"


def format_date_cz(game_time_utc):
    """Převede UTC čas na český formát DD.MM.YYYY HH:MM."""
    try:
        dt = datetime.fromisoformat(game_time_utc.replace("Z", "+00:00"))
        # Převod na CET/CEST (UTC+1/+2) - NHL hraje hlavně v noci
        cet = dt + timedelta(hours=2)
        return cet.strftime("%d.%m.%Y %H:%M")
    except Exception:
        return game_time_utc


def get_existing_matches():
    """Načte všechny existující zápasy z DB."""
    matches = supabase_get("matches", f"?competition_id=eq.{COMPETITION_ID}&select=*")
    return {f"{m['home']}|{m['away']}|{m['match_date'][:10]}": m for m in matches}


def sync_schedule():
    """Přidá nové nadcházející zápasy na příštích 14 dní."""
    print("\n📅 Synchronizuji program NHL...")
    today = datetime.now(timezone.utc).date()
    date_str = today.strftime("%Y-%m-%d")
    
    games = get_nhl_schedule(date_str)
    existing = get_existing_matches()
    
    added = 0
    for g in games:
        state = g.get("gameState", "")
        # Přidáme jen budoucí nebo dnešní zápasy
        if state in ("OFF", "FINAL", "OVER"):
            continue
        
        home_obj = g.get("homeTeam", {})
        away_obj = g.get("awayTeam", {})
        home = format_team_name(home_obj)
        away = format_team_name(away_obj)
        game_time = g.get("startTimeUTC", "")
        date_cz = format_date_cz(game_time)
        date_key = game_time[:10]
        
        key = f"{home}|{away}|{date_key}"
        if key in existing:
            continue  # Zápas už existuje
        
        # Odhadni kurzy
        pair_key = f"{home} vs {away}"
        if pair_key in TEAM_ODDS:
            oh, od, oa = TEAM_ODDS[pair_key]
        else:
            oh = DEFAULT_ODDS["home"]
            od = DEFAULT_ODDS["draw"]
            oa = DEFAULT_ODDS["away"]
        
        match_data = {
            "competition_id": COMPETITION_ID,
            "home": home,
            "away": away,
            "match_date": date_cz,
            "status": "upcoming",
            "odds_home": oh,
            "odds_draw": od,
            "odds_away": oa,
            "result_home": None,
            "result_away": None,
            "decision": None
        }
        
        try:
            supabase_insert("matches", match_data)
            print(f"  ✅ Přidán: {home} – {away} ({date_cz})")
            added += 1
        except Exception as e:
            print(f"  ❌ Chyba při přidávání {home} – {away}: {e}")
    
    print(f"  Celkem přidáno: {added} nových zápasů")


def sync_results():
    """Aktualizuje výsledky odehraných zápasů."""
    print("\n🏒 Aktualizuji výsledky...")
    
    # Načti zápasy které jsou 'upcoming' ale datum už prošlo
    now_cz = datetime.now(timezone.utc) + timedelta(hours=2)
    today = now_cz.strftime("%Y-%m-%d")
    
    # Načti všechny upcoming zápasy
    matches = supabase_get("matches", 
        f"?competition_id=eq.{COMPETITION_ID}&status=eq.upcoming&select=*")
    
    # Načti NHL výsledky za posledních 7 dní
    week_ago = (datetime.now(timezone.utc).date() - timedelta(days=7)).strftime("%Y-%m-%d")
    games = get_nhl_schedule(week_ago)
    
    # Indexuj podle jmen týmů
    results_by_teams = {}
    for g in games:
        state = g.get("gameState", "")
        if state not in ("OFF", "FINAL", "OVER"):
            continue
        home = format_team_name(g.get("homeTeam", {}))
        away = format_team_name(g.get("awayTeam", {}))
        home_score = g.get("homeTeam", {}).get("score")
        away_score = g.get("awayTeam", {}).get("score")
        
        # Zjisti způsob rozhodnutí
        period_descriptor = g.get("periodDescriptor", {})
        periods = g.get("periodDescriptor", {}).get("number", 3)
        decision = "reg"
        if periods == 4:
            decision = "ot"
        elif periods == 5:
            decision = "so"
        
        if home_score is not None and away_score is not None:
            results_by_teams[f"{home}|{away}"] = {
                "result_home": home_score,
                "result_away": away_score,
                "decision": decision
            }
    
    updated = 0
    for m in matches:
        key = f"{m['home']}|{m['away']}"
        if key not in results_by_teams:
            continue
        
        result = results_by_teams[key]
        try:
            supabase_update("matches", 
                {**result, "status": "done"},
                f"id=eq.{m['id']}"
            )
            dec_label = {"reg": "reg. čas", "ot": "prodloužení", "so": "nájezdy"}
            print(f"  ✅ Výsledek: {m['home']} {result['result_home']}:{result['result_away']} {m['away']} ({dec_label.get(result['decision'], '')})")
            updated += 1
        except Exception as e:
            print(f"  ❌ Chyba při aktualizaci {m['home']} – {m['away']}: {e}")
    
    print(f"  Celkem aktualizováno: {updated} výsledků")


if __name__ == "__main__":
    print("🏒 NHL Sync start:", datetime.now().strftime("%Y-%m-%d %H:%M"))
    print(f"   Competition ID: {COMPETITION_ID}")
    
    sync_schedule()
    sync_results()
    
    print("\n✅ Hotovo!")
