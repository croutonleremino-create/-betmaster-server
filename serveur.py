"""
Serveur BetMaster VIP
- Nouveau paiement Stripe -> envoie le lien Telegram automatiquement
- Resiliation Stripe -> retire l'abonne du canal Telegram
"""

import os
import json
import requests
import stripe
from flask import Flask, request, jsonify

app = Flask(__name__)

# Demarre le scheduler en arriere-plan (pronostics, alertes, recaps)
from scheduler import start_scheduler
start_scheduler()

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHANNEL_ID = os.environ["TELEGRAM_CHANNEL_ID"]
STRIPE_SECRET_KEY = os.environ["STRIPE_SECRET_KEY"]
STRIPE_WEBHOOK_SECRET = os.environ["STRIPE_WEBHOOK_SECRET"]

stripe.api_key = STRIPE_SECRET_KEY
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
DATA_FILE = "subscribers.json"


def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE) as f:
            return json.load(f)
    return {"pending": {}, "active": {}}


def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)


def tg(method, **kwargs):
    r = requests.post(f"{TELEGRAM_API}/{method}", json=kwargs, timeout=10)
    return r.json()


@app.route("/join")
def join():
    """Page affichee apres le paiement Stripe."""
    session_id = request.args.get("session", "")
    if not session_id:
        return "Lien invalide.", 400

    try:
        session = stripe.checkout.Session.retrieve(session_id)
        customer_id = str(session.customer)
    except Exception:
        return "Session invalide.", 400

    data = load_data()
    data["pending"][session_id] = customer_id
    save_data(data)

    bot_info = tg("getMe")
    bot_username = bot_info["result"]["username"]

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>BetMaster VIP</title></head>
<body style="font-family:sans-serif;text-align:center;padding:40px;background:#1a1a2e;color:white">
  <h1>Paiement confirme !</h1>
  <p style="font-size:18px">Clique ci-dessous pour rejoindre le canal prive BetMaster VIP :</p><br>
  <a href="https://t.me/{bot_username}?start={session_id}"
     style="background:#229ED9;color:white;padding:18px 36px;border-radius:10px;
            text-decoration:none;font-size:20px;font-weight:bold">
    Rejoindre le canal VIP
  </a>
  <br><br>
  <p style="color:#aaa;font-size:14px">Tu seras redirige vers Telegram.</p>
</body></html>"""


@app.route("/bot", methods=["POST"])
def bot():
    """Recoit les messages envoyes au bot Telegram."""
    update = request.get_json(silent=True) or {}
    message = update.get("message", {})
    text = message.get("text", "")
    user_id = message.get("from", {}).get("id")
    chat_id = message.get("chat", {}).get("id")

    if not text.startswith("/start ") or not user_id:
        return jsonify({"ok": True})

    session_id = text.split(" ", 1)[1].strip()
    data = load_data()

    if session_id not in data["pending"]:
        tg("sendMessage", chat_id=chat_id,
           text="Lien invalide ou deja utilise. Contacte le support si tu viens de payer.")
        return jsonify({"ok": True})

    customer_id = data["pending"].pop(session_id)
    data["active"][customer_id] = user_id
    save_data(data)

    result = tg("createChatInviteLink", chat_id=TELEGRAM_CHANNEL_ID, member_limit=1)
    invite_link = result.get("result", {}).get("invite_link", "")

    if invite_link:
        tg("sendMessage", chat_id=chat_id,
           text=f"✅ Abonnement verifie !\n\nVoici ton lien d'acces au canal prive (usage unique) :\n{invite_link}")

        MESSAGE_BIENVENUE = """🎯 *Bienvenue sur BetMaster VIP !* 🎯

Si tu es ici, ce n'est pas par hasard.

Ce canal a un seul objectif : t'aider à adopter le bon état d'esprit pour parier intelligemment et gérer ton bankroll comme un pro. ⚽🏀🎾

Tu y trouveras :
✅ Des analyses de matchs et des pronostics
✅ Des stratégies de gestion de bankroll
✅ Un mindset de parieur discipliné
✅ De la motivation au quotidien
✅ Des conseils pour passer à l'action sans tilt

Le succès dans les paris ne tombe pas du ciel. Il se construit avec de la *discipline*, de la *patience* et les *bonnes décisions*. 📈

Mon objectif est de partager avec toi des analyses concrètes, des opportunités value et des conseils pour progresser, un pari après l'autre.

📲 *Rejoins-moi aussi sur Instagram :* @Odessy\\_Bet pour encore plus de contenu, de stories et de pronos exclusifs !

Le meilleur moment pour commencer, c'est maintenant. ⏳

Ensemble, avançons vers une approche plus rentable et maîtrisée. 💪

🔞 *Les paris comportent des risques. Ne mise que ce que tu peux te permettre de perdre. Jouer comporte des risques : endettement, isolement, dépendance. Appelez le 09 74 75 13 13 (appel non surtaxé).*"""

        tg("sendMessage", chat_id=chat_id, text=MESSAGE_BIENVENUE, parse_mode="Markdown")
    else:
        tg("sendMessage", chat_id=chat_id,
           text="Une erreur est survenue. Contacte le support.")

    return jsonify({"ok": True})


@app.route("/webhook", methods=["POST"])
def stripe_webhook():
    """Recoit les evenements Stripe."""
    payload = request.get_data()
    sig = request.headers.get("Stripe-Signature", "")

    try:
        event = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
    except Exception:
        return "Signature invalide", 400

    if event["type"] == "customer.subscription.deleted":
        customer_id = str(event["data"]["object"]["customer"])
        data = load_data()
        user_id = data["active"].get(customer_id)
        if user_id:
            tg("banChatMember", chat_id=TELEGRAM_CHANNEL_ID, user_id=user_id)
            tg("unbanChatMember", chat_id=TELEGRAM_CHANNEL_ID, user_id=user_id)
            del data["active"][customer_id]
            save_data(data)

    return jsonify({"ok": True})


@app.route("/")
def index():
    return "BetMaster VIP server OK", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
