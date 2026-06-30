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

MATCHS DU JOUR :
{liste_foot}

{f'AUTRES SPORTS :{chr(10)}{liste_autres}' if liste_autres else ''}

Fais une presentation courte et percutante des matchs du jour. PAS DE PARIS, PAS DE COTES.
Juste :
- Le contexte de chaque match (enjeu, forme recente, rivalite)
- 1 info marquante ou actu sur les equipes (blessure, suspension, statistique)
- Ce qui rend ce match interessant a regarder

Format pour chaque match :
⚽ [Equipe A] vs [Equipe B] | [heure]
[2-3 phrases max : contexte + info cle + pourquoi c'est interessant]

Commence par : "🗞️ Programme BetMaster VIP - {aujourd_hui.strftime('%d/%m/%Y')}"
Termine par : "⚡ Les alertes paris arrivent 1h avant chaque match !"
Ecris en francais, sois dynamique et court."""

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

    # Resultats ESPN d'hier
    resultats = []
    for slug, ligue in COMPETITIONS_FOOT:
        try:
            r = requests.get(
                f"https://site.api.espn.com/apis/site/v2/sports/soccer/{slug}/scoreboard",
                params={"dates": hier.strftime("%Y%m%d")}, timeout=10)
            for e in r.json().get("events", []):
                comps = e.get("competitions", [{}])
                etat = e.get("status", {}).get("type", {}).get("state", "")
                if etat != "post":
                    continue
                competitors = comps[0].get("competitors", []) if comps else []
                scores = {c.get("homeAway"): (c.get("team", {}).get("displayName", ""), c.get("score", "0")) for c in competitors}
                home_name, home_score = scores.get("home", ("", "0"))
                away_name, away_score = scores.get("away", ("", "0"))
                if home_name and away_name:
                    resultats.append(f"{home_name} {home_score} - {away_score} {away_name} ({ligue})")
        except Exception:
            pass

    if not resultats:
        print("Pas de resultats hier.")
        return

    prompt = f"""Tu es un analyste sportif. Voici les resultats des matchs d'hier ({hier.strftime('%d/%m/%Y')}) :

{chr(10).join(resultats)}

Fais le recap des paris d'hier. Pour chaque match, evalue si les paris classiques ont fonctionne :
- Le pari SAFE (victoire du favori) : ✅ passe ou ❌ rate + la cote estimee
- Le Bet Builder (favori + over 1.5 buts) : ✅ passe ou ❌ rate + la cote estimee

Format pour chaque match :
⚽ [Equipe A] X-X [Equipe B]
✅ ou ❌ Safe — [Victoire X] @ ~[cote]
✅ ou ❌ Bet Builder — [Victoire X + Over 1.5 buts] @ ~[cote]

A la fin :
📈 Win rate du jour : X/X paris passes (XX%)
[1 phrase de conclusion + motivation pour aujourd'hui]

Commence EXACTEMENT par : "📊 RECAP DU {hier.strftime('%d/%m/%Y')}"
Termine par : "⚡ Nouvelles alertes ce soir, restez connectes !"
Ecris en francais, sois bref et percutant."""

    try:
        recap = groq_chat(prompt, max_tokens=600)
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

                prompt1 = f"""Match : {equipe1} vs {equipe2} ({nom_ligue}) dans {int(minutes_restantes)} min a {heure}.
Cotes reelles : {cotes if cotes else 'non disponibles'}

Reponds UNIQUEMENT avec ce bloc exact, remplace les crochets, ne rajoute RIEN d'autre :

🚨 {equipe1} vs {equipe2} | {heure}

🏆 Vainqueur : [equipe gagnante ou Match Nul]
💰 Cote : [cote 1X2 reelle si dispo, sinon ~estimee]
⚡ Confiance : [Elevee / Moyenne / Faible]

🔨 Bet Builder
🏆 [Vainqueur] + [CHOISIS 1 : "Plus de X.5 buts" OU "[Joueur cle] passeur" OU "[Joueur cle] buteur"]
💰 Cote estimee : ~[X.XX]
⚡ Confiance : [Elevee / Moyenne / Faible]

💼 Ne risquez jamais plus de 2-5% de votre bankroll sur un seul pari.
⚠️ Pari responsable. 18+"""

                try:
                    alerte = groq_chat(prompt1, max_tokens=200)
                    envoyer_telegram(alerte)
                    alertes[event_id] = aujourd_hui.isoformat()
                    json.dump(alertes, open(ALERTES_FILE, "w"))
                    print(f"Alerte envoyee : {equipe1} vs {equipe2}")
                except Exception as e:
                    print(f"Erreur alerte {equipe1} vs {equipe2}: {e}")

        except Exception as e:
            print(f"Erreur {slug}: {e}")


# ─── JOB 4 : RECAP SEMAINE (dimanche 20h Paris) ──────────────────────────────

def job_promo_propfirm():
    print(f"[{datetime.datetime.utcnow()}] Job promo propfirm...")
    message = """💼 TU VEUX PARIER AVEC UN GROS CAPITAL SANS RISQUER TON ARGENT ?

Prime Sports Funded te permet de gérer un compte allant de 5 000$ à 100 000$ sur les paris sportifs — sans mettre un seul euro de ta poche.

Comment ça marche ?
✅ Tu passes un challenge avec de l'argent virtuel
✅ Tu prouves que tu sais gérer un bankroll
✅ On te finance avec un vrai capital (5k$ à 100k$)
✅ Tu gardes jusqu'à 90% de tes profits
✅ Retraits rapides 24/7

C'est la plateforme pensée pour les parieurs sérieux et disciplinés. Si tu suis nos pronostics et que tu gères bien ton capital, tu as tout ce qu'il faut pour réussir le challenge.

🔗 Tente ta chance ici : https://primesportsfunded.com/?ref=PSF-2JEPFW

⚠️ Les paris sportifs comportent des risques. Jouez de manière responsable. 18+"""
    envoyer_telegram(message)
    print("Promo propfirm envoyee.")


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

    # Mercredi 12h00 Paris = 10h00 UTC
    scheduler.add_job(job_promo_propfirm, "cron", day_of_week="wed", hour=10, minute=0, id="promo_propfirm")

    scheduler.start()
    print("Scheduler demarre. Jobs actifs :")
    for job in scheduler.get_jobs():
        print(f"  - {job.id} : {job.next_run_time}")
    return scheduler
