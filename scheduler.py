"""
Scheduler APScheduler — tourne en arriere-plan sur Render 24h/24.
Remplace tous les cron jobs Mac.

Horaires UTC (France = UTC+2 en ete) :
  06:45 UTC = 08:45 France -> pronostics du jour
  09:00 UTC = 11:00 France -> recap quotidien
  15:00 UTC = 17:00 France -> alerte pre-match
  18:00 UTC dimanche = 20:00 France -> recap semaine
  toutes les 15 min -> check compos
"""

import os
import json
import datetime
import time
import requests
from groq import Groq

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHANNEL_ID", "")
ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "")

COMPETITIONS_FOOT = [
    ("fifa.world",     "Coupe du Monde 2026"),
    ("uefa.champions", "Champions League"),
    ("fra.1",          "Ligue 1"),
    ("eng.1",          "Premier League"),
    ("esp.1",          "La Liga"),
    ("ger.1",          "Bundesliga"),
    ("ita.1",          "Serie A"),
]

ALERTES_FILE = "/tmp/alertes_compos.json"
HISTORIQUE_FILE = "/tmp/historique.json"


# ─── UTILITAIRES ──────────────────────────────────────────────────────────────

def envoyer_telegram(message: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram non configure")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    for morceau in [message[i:i+4000] for i in range(0, len(message), 4000)]:
        try:
            requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": morceau}, timeout=30)
            time.sleep(1)
        except Exception as e:
            print(f"Erreur Telegram: {e}")


def groq_chat(prompt: str, max_tokens: int = 3000) -> str:
    client = Groq(api_key=GROQ_API_KEY)
    r = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=max_tokens,
        temperature=0.7,
    )
    return r.choices[0].message.content.strip()


def utc_to_paris(dt_utc: datetime.datetime) -> datetime.datetime:
    """Convertit UTC en heure de Paris (UTC+2 en ete, UTC+1 en hiver)."""
    # Simple : +2h en ete (mars-octobre), +1h en hiver
    month = dt_utc.month
    offset = 2 if 3 <= month <= 10 else 1
    return dt_utc + datetime.timedelta(hours=offset)


def aujourd_hui_paris() -> datetime.date:
    return utc_to_paris(datetime.datetime.utcnow()).date()


def recuperer_cotes_match(equipe1: str, equipe2: str) -> str:
    sports = ["soccer_fifa_world_cup", "soccer_uefa_champs_league",
              "soccer_france_ligue1", "soccer_epl", "soccer_spain_la_liga",
              "soccer_germany_bundesliga", "soccer_italy_serie_a"]
    for sport in sports:
        try:
            r = requests.get(f"https://api.the-odds-api.com/v4/sports/{sport}/odds/",
                params={"apiKey": ODDS_API_KEY, "regions": "eu",
                        "markets": "h2h,totals", "oddsFormat": "decimal"}, timeout=10)
            for game in r.json():
                home = game.get("home_team", "")
                away = game.get("away_team", "")
                if equipe1.lower() in home.lower() or equipe2.lower() in away.lower() or \
                   equipe1.lower() in away.lower() or equipe2.lower() in home.lower():
                    for bm in game.get("bookmakers", []):
                        h2h, cote_over = {}, ""
                        for market in bm.get("markets", []):
                            if market["key"] == "h2h":
                                h2h = {o["name"]: o["price"] for o in market["outcomes"]}
                            if market["key"] == "totals":
                                for o in market["outcomes"]:
                                    if "Over" in o["name"]:
                                        cote_over = f" | +2.5 buts: {o['price']}"
                        if h2h:
                            return (f"1:{h2h.get(home,'?')} X:{h2h.get('Draw','?')} "
                                    f"2:{h2h.get(away,'?')}{cote_over}")
        except Exception:
            pass
    return ""


# ─── JOB 1 : PRONOSTICS DU JOUR (08h45 Paris) ────────────────────────────────

def job_pronostics():
    print(f"[{datetime.datetime.utcnow()}] Job pronostics...")
    aujourd_hui = aujourd_hui_paris()

    # Matchs via ESPN
    matchs_foot = []
    cdm = _matchs_espn("fifa.world", "Coupe du Monde 2026", aujourd_hui)
    if cdm:
        matchs_foot = cdm[:12]
    else:
        for slug, ligue in COMPETITIONS_FOOT[1:]:
            matchs_foot.extend(_matchs_espn(slug, ligue, aujourd_hui))
        if not matchs_foot:
            matchs_foot = _matchs_odds_api(aujourd_hui)

    autres = _autres_sports()
    cotes = _toutes_cotes(aujourd_hui)

    liste_foot = "\n".join(f"  - {m}" for m in matchs_foot) if matchs_foot else "Aucun match ESPN"
    liste_autres = "\n".join(f"  - {e}" for e in autres) if autres else ""

    prompt = f"""Tu es un analyste sportif expert. Nous sommes le {aujourd_hui.strftime('%d/%m/%Y')}.

MATCHS DE FOOTBALL AUJOURD'HUI :
{liste_foot}

{f'AUTRES SPORTS :{chr(10)}{liste_autres}' if liste_autres else ''}

{cotes}

Pour chaque match de football, utilise ce format :

⚽ [Equipe A] vs [Equipe B] | [heure] | [Ligue]
📋 [1-2 phrases : forme recente, stats cles, contexte]
🟢 Safe — [pari simple]
Cote : [vraie cote si dispo, sinon ~estimee] | ✅ Confiance Elevee
🟡 Combine — [resultat + tir cadre ou Over 1.5 buts]
Cote : ~[estimee] | ⚡ Confiance Moyenne
🔴 Boost — [resultat + buteur + Over 2.5 buts ou corners]
Cote : ~[estimee] | 🎯 Confiance Faible
⚽ Paris joueurs : [Joueur 1] buteur ~X.XX | [Joueur 2] but/passe ~X.XX | Over 2.5 buts [cote] | Over 9.5 corners ~X.XX

Commence par : "🏆 Pronostics BetMaster VIP - {aujourd_hui.strftime('%d/%m/%Y')}"
Termine par :
"📊 Cotes 1X2 et totaux : bookmakers en temps reel. Cotes joueurs/corners : estimees.
⚠️ Pari responsable. 18+"
Ecris en francais."""

    try:
        analyse = groq_chat(prompt, max_tokens=4000)
        envoyer_telegram(analyse)
        # Sauvegarde historique
        try:
            hist = json.load(open(HISTORIQUE_FILE)) if os.path.exists(HISTORIQUE_FILE) else {}
            hist[aujourd_hui.isoformat()] = {"analyse": analyse}
            json.dump(hist, open(HISTORIQUE_FILE, "w"), ensure_ascii=False, indent=2)
        except Exception:
            pass
        print("Pronostics envoyes.")
    except Exception as e:
        print(f"Erreur job_pronostics: {e}")


# ─── JOB 2 : RECAP QUOTIDIEN (11h00 Paris) ───────────────────────────────────

def job_recap_quotidien():
    print(f"[{datetime.datetime.utcnow()}] Job recap quotidien...")
    hier = (aujourd_hui_paris() - datetime.timedelta(days=1))

    try:
        hist = json.load(open(HISTORIQUE_FILE)) if os.path.exists(HISTORIQUE_FILE) else {}
        analyse_hier = hist.get(hier.isoformat(), {}).get("analyse", "")
    except Exception:
        analyse_hier = ""

    # Resultats ESPN d'hier
    resultats = []
    for slug, ligue in COMPETITIONS_FOOT:
        try:
            r = requests.get(
                f"https://site.api.espn.com/apis/site/v2/sports/soccer/{slug}/scoreboard",
                params={"dates": hier.strftime("%Y%m%d")}, timeout=10)
            for e in r.json().get("events", []):
                if e.get("status", {}).get("type", {}).get("state") == "post":
                    nom = e.get("name", "")
                    score = e.get("competitions", [{}])[0].get("score", "")
                    resultats.append(f"{nom} : {score}")
        except Exception:
            pass

    if not resultats and not analyse_hier:
        print("Rien a recapituler.")
        return

    prompt = f"""Tu es un analyste sportif. Recap des resultats d'hier ({hier.strftime('%d/%m/%Y')}).

Pronostics d'hier :
{analyse_hier[:2000] if analyse_hier else 'Non disponibles'}

Resultats reels :
{chr(10).join(resultats) if resultats else 'Non disponibles'}

Fais un recap court :
- Pour chaque match : ✅ ou ❌ selon le pronostic
- Win rate du jour (ex: 3/5 = 60%)
- 1 phrase de conclusion

Commence par : "📊 RECAP DU {hier.strftime('%d/%m/%Y')}"
Ecris en francais, sois bref."""

    try:
        recap = groq_chat(prompt, max_tokens=800)
        envoyer_telegram(recap)
        print("Recap quotidien envoye.")
    except Exception as e:
        print(f"Erreur job_recap_quotidien: {e}")


# ─── JOB 3 : CHECK COMPOS toutes les 15 min ──────────────────────────────────

def job_check_compos():
    print(f"[{datetime.datetime.utcnow()}] Job check compos...")
    maintenant_utc = datetime.datetime.utcnow()
    aujourd_hui = aujourd_hui_paris()

    try:
        alertes = json.load(open(ALERTES_FILE)) if os.path.exists(ALERTES_FILE) else {}
        alertes_aujourd_hui = set(k for k, v in alertes.items() if v >= aujourd_hui.isoformat())
    except Exception:
        alertes = {}
        alertes_aujourd_hui = set()

    for slug, nom_ligue in COMPETITIONS_FOOT:
        try:
            r = requests.get(
                f"https://site.api.espn.com/apis/site/v2/sports/soccer/{slug}/scoreboard",
                timeout=10)
            for event in r.json().get("events", []):
                if event.get("status", {}).get("type", {}).get("state") != "pre":
                    continue

                event_id = event.get("id", "")
                if event_id in alertes_aujourd_hui:
                    continue

                date_str = event.get("date", "")
                try:
                    dt_utc = datetime.datetime.strptime(date_str, "%Y-%m-%dT%H:%MZ")
                    dt_paris = utc_to_paris(dt_utc)
                except Exception:
                    continue

                if dt_paris.date() != aujourd_hui:
                    continue

                minutes_restantes = (dt_utc - maintenant_utc).total_seconds() / 60
                if not (0 < minutes_restantes <= 60):
                    continue

                comps = event.get("competitions", [{}])
                competitors = comps[0].get("competitors", []) if comps else []
                equipe1 = competitors[0].get("team", {}).get("displayName", "") if len(competitors) > 0 else ""
                equipe2 = competitors[1].get("team", {}).get("displayName", "") if len(competitors) > 1 else ""
                heure = dt_paris.strftime("%H:%M")
                cotes = recuperer_cotes_match(equipe1, equipe2)

                prompt = f"""Le match {equipe1} vs {equipe2} ({nom_ligue}) commence dans {int(minutes_restantes)} minutes ({heure}).
Cotes bookmakers : {cotes if cotes else 'non disponibles'}

Tu es un analyste sportif expert. Base-toi sur la forme recente, les stats et les confrontations directes.

Utilise ce format :

🚨 ALERTE PRE-MATCH
{equipe1} vs {equipe2} | {heure} | {nom_ligue}

👥 Compos probables
{equipe1} : [11 joueurs cles probables]
{equipe2} : [11 joueurs cles probables]

📋 Analyse rapide
[2 phrases max]

🟢 Safe — [pari simple]
Cote : [vraie cote si dispo, sinon ~estimee] | ✅ Confiance Elevee
🟡 Combine — [resultat + tir cadre ou Over 1.5 buts]
Cote : ~[estimee] | ⚡ Confiance Moyenne
🔴 Boost — [resultat + buteur + Over 2.5 buts ou corners]
Cote : ~[estimee] | 🎯 Confiance Faible
⚽ Paris joueurs : [Joueur 1] buteur ~X.XX | [Joueur 2] but/passe ~X.XX | Over 2.5 buts [cote] | Over 9.5 corners ~X.XX

📊 Cotes 1X2 et totaux : bookmakers en temps reel. Cotes joueurs/corners : estimees.
⚠️ Pari responsable. 18+

Ecris en francais, reste court et impactant."""

                try:
                    analyse = groq_chat(prompt, max_tokens=1200)
                    envoyer_telegram(analyse)
                    alertes[event_id] = aujourd_hui.isoformat()
                    json.dump(alertes, open(ALERTES_FILE, "w"))
                    print(f"Alerte envoyee : {equipe1} vs {equipe2}")
                except Exception as e:
                    print(f"Erreur alerte {equipe1} vs {equipe2}: {e}")

        except Exception as e:
            print(f"Erreur {slug}: {e}")


# ─── JOB 4 : RECAP SEMAINE (dimanche 20h Paris) ──────────────────────────────

def job_recap_semaine():
    print(f"[{datetime.datetime.utcnow()}] Job recap semaine...")
    try:
        hist = json.load(open(HISTORIQUE_FILE)) if os.path.exists(HISTORIQUE_FILE) else {}
    except Exception:
        hist = {}

    if not hist:
        print("Pas d'historique.")
        return

    contenu = "\n\n".join(
        f"=== {date} ===\n{data.get('analyse','')[:500]}"
        for date, data in sorted(hist.items())[-7:]
    )

    prompt = f"""Tu es un analyste sportif. Voici les pronostics de la semaine :

{contenu}

Fais un bilan hebdomadaire :
- Win rate global estime de la semaine
- Meilleur pari de la semaine
- Moins bon pari
- Note globale /10
- 1 phrase de motivation pour la semaine prochaine

Commence par : "📅 BILAN DE LA SEMAINE"
Ecris en francais, sois bref et percutant."""

    try:
        bilan = groq_chat(prompt, max_tokens=600)
        envoyer_telegram(bilan)
        print("Recap semaine envoye.")
    except Exception as e:
        print(f"Erreur job_recap_semaine: {e}")


# ─── HELPERS ESPN / ODDS API ──────────────────────────────────────────────────

def _matchs_espn(slug: str, nom_ligue: str, today: datetime.date) -> list[str]:
    matchs = []
    try:
        r = requests.get(
            f"https://site.api.espn.com/apis/site/v2/sports/soccer/{slug}/scoreboard",
            timeout=10)
        for event in r.json().get("events", []):
            if event.get("status", {}).get("type", {}).get("state") != "pre":
                continue
            date_str = event.get("date", "")
            try:
                dt_utc = datetime.datetime.strptime(date_str, "%Y-%m-%dT%H:%MZ")
                dt_paris = utc_to_paris(dt_utc)
                if dt_paris.date() != today:
                    continue
                heure = dt_paris.strftime("%H:%M")
            except Exception:
                continue
            matchs.append(f"⚽ {event.get('name','')} ({nom_ligue}) - {heure}")
    except Exception:
        pass
    return matchs


def _matchs_odds_api(today: datetime.date) -> list[str]:
    sports = [
        ("soccer_fifa_world_cup", "Coupe du Monde 2026"),
        ("soccer_uefa_champs_league", "Champions League"),
        ("soccer_france_ligue1", "Ligue 1"),
        ("soccer_epl", "Premier League"),
        ("soccer_spain_la_liga", "La Liga"),
        ("soccer_germany_bundesliga", "Bundesliga"),
        ("soccer_italy_serie_a", "Serie A"),
    ]
    matchs = []
    for sport_key, nom_ligue in sports:
        try:
            r = requests.get(
                f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds/",
                params={"apiKey": ODDS_API_KEY, "regions": "eu",
                        "markets": "h2h", "oddsFormat": "decimal"}, timeout=10)
            for game in r.json():
                commence = game.get("commence_time", "")
                if not commence:
                    continue
                dt_utc = datetime.datetime.strptime(commence[:19], "%Y-%m-%dT%H:%M:%S")
                dt_paris = utc_to_paris(dt_utc)
                if dt_paris.date() != today:
                    continue
                home = game.get("home_team", "")
                away = game.get("away_team", "")
                matchs.append(f"⚽ {home} vs {away} ({nom_ligue}) - {dt_paris.strftime('%H:%M')}")
        except Exception:
            pass
    return matchs[:12]


def _autres_sports() -> list[str]:
    evenements = []
    try:
        r = requests.get(
            "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard",
            timeout=10)
        for e in r.json().get("events", [])[:4]:
            if e.get("status", {}).get("type", {}).get("state") == "pre":
                evenements.append(f"🏀 {e.get('name','')} (NBA)")
    except Exception:
        pass
    try:
        r = requests.get(
            "https://site.api.espn.com/apis/site/v2/sports/racing/f1/scoreboard",
            timeout=10)
        for e in r.json().get("events", [])[:2]:
            if e.get("status", {}).get("type", {}).get("state") == "pre":
                evenements.append(f"🏎️ {e.get('name','')} (F1)")
    except Exception:
        pass
    return evenements


def _toutes_cotes(today: datetime.date) -> str:
    sports = ["soccer_fifa_world_cup", "soccer_uefa_champs_league",
              "soccer_france_ligue1", "soccer_epl", "soccer_spain_la_liga",
              "soccer_germany_bundesliga", "soccer_italy_serie_a"]
    cotes = []
    for sport in sports:
        try:
            r = requests.get(f"https://api.the-odds-api.com/v4/sports/{sport}/odds/",
                params={"apiKey": ODDS_API_KEY, "regions": "eu",
                        "markets": "h2h,totals", "oddsFormat": "decimal"}, timeout=10)
            for game in r.json():
                commence = game.get("commence_time", "")
                if not commence:
                    continue
                dt_utc = datetime.datetime.strptime(commence[:19], "%Y-%m-%dT%H:%M:%S")
                if utc_to_paris(dt_utc).date() != today:
                    continue
                home, away = game.get("home_team", ""), game.get("away_team", "")
                for bm in game.get("bookmakers", []):
                    for market in bm.get("markets", []):
                        if market["key"] == "h2h":
                            odds = {o["name"]: o["price"] for o in market["outcomes"]}
                            cotes.append(f"⚽ {home} vs {away} | 1:{odds.get(home,'?')} X:{odds.get('Draw','?')} 2:{odds.get(away,'?')}")
                            break
                    break
        except Exception:
            pass
    return ("COTES BOOKMAKERS DU JOUR :\n" + "\n".join(cotes)) if cotes else ""


def start_scheduler():
    from apscheduler.schedulers.background import BackgroundScheduler

    scheduler = BackgroundScheduler(timezone="UTC")

    # 08h45 Paris = 06h45 UTC
    scheduler.add_job(job_pronostics, "cron", hour=6, minute=45, id="pronostics")

    # 11h00 Paris = 09h00 UTC
    scheduler.add_job(job_recap_quotidien, "cron", hour=9, minute=0, id="recap_quotidien")

    # Toutes les 15 minutes
    scheduler.add_job(job_check_compos, "interval", minutes=15, id="check_compos")

    # Dimanche 20h00 Paris = 18h00 UTC
    scheduler.add_job(job_recap_semaine, "cron", day_of_week="sun", hour=18, minute=0, id="recap_semaine")

    scheduler.start()
    print("Scheduler demarre. Jobs actifs :")
    for job in scheduler.get_jobs():
        print(f"  - {job.id} : {job.next_run_time}")
    return scheduler
