#!/usr/bin/env python3
"""
Lecture de l'état de votre BMW via l'API officielle BMW CarData (lecture seule).

Commandes :
    python bmw_cardata.py login     -> connexion à votre compte BMW (à faire une fois, puis tous les 14 jours max)
    python bmw_cardata.py status    -> récupère l'état de la voiture, l'affiche et génère tableau_de_bord.html
    python bmw_cardata.py stream    -> reste à l'écoute en direct (streaming MQTT) et met la page à jour
    python bmw_cardata.py show      -> réaffiche la dernière lecture sans appeler BMW (gratuit)
    python bmw_cardata.py profile   -> récupère le modèle et la photo de la voiture (une seule fois)
    python bmw_cardata.py reset     -> oublie le VIN / le conteneur mémorisés (les jetons sont conservés)

Options : --open (ouvre le tableau de bord dans le navigateur)

Python 3.9+ suffit. Seul « stream » demande une bibliothèque en plus :  pip install paho-mqtt

Attention : BMW limite l'API à ~50 appels par jour et par compte.
Une commande "status" coûte 1 appel (jusqu'à 3 quand la liste des données change).
"""

import base64
import hashlib
import json
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from collections import deque
from contextlib import closing
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# --- Points d'accès BMW CarData -------------------------------------------------
AUTH_HOST = "https://customer.bmwgroup.com"
DEVICE_CODE_URL = AUTH_HOST + "/gcdm/oauth/device/code"
TOKEN_URL = AUTH_HOST + "/gcdm/oauth/token"
API_HOST = "https://api-cardata.bmwgroup.com"
API_VERSION = "v1"
SCOPES_API = "authenticate_user openid cardata:api:read"
SCOPES_FULL = SCOPES_API + " cardata:streaming:read"
STREAM_HOST = "customer.streaming-cardata.bmwgroup.com"
STREAM_PORT = 9000
# Page « en direct » : http://127.0.0.1:8765 par défaut. Dans Docker, BMW_LIVE_HOST=0.0.0.0.
LIVE_HOST = os.environ.get("BMW_LIVE_HOST") or "127.0.0.1"
LIVE_PORT = int(os.environ.get("BMW_LIVE_PORT") or 8765)

# --- Fichiers locaux ------------------------------------------------------------
# Le code est à côté de ce script ; les données personnelles aussi, sauf si BMW_DATA_DIR
# désigne un autre dossier (volume Docker, par exemple).
HERE = Path(__file__).resolve().parent
DATA = Path(os.environ.get("BMW_DATA_DIR") or HERE).resolve()
CONFIG_FILE = DATA / "config.json"            # client_id, VIN, id du conteneur
TOKENS_FILE = DATA / "tokens.json"            # jetons d'accès : NE PAS PARTAGER
LAST_FILE = DATA / "derniere_lecture.json"    # dernières données connues
DASHBOARD_FILE = DATA / "tableau_de_bord.html"
PROFILE_FILE = DATA / "profil_vehicule.json"  # modèle, motorisation... (récupéré une fois)
TEMPLATE_FILE = HERE / "dashboard_template.html"
STREAM_LOG = DATA / "historique_stream.jsonl"  # chaque message reçu en direct
CATALOGUE_FILE = HERE / "catalogue_bmw.json"   # noms officiels (en français) de toutes les données BMW
CATALOGUE_URL = "https://www.bmw.fr/fr-fr/utilities/bmw/api/cd/catalogue?streamable=false&q=&category=&offset="
STREAM_LOG_IN_PAGE = 800                       # nombre de messages repris dans la page
HISTORY_DB = DATA / "historique.db"            # base SQLite : toutes les valeurs datées (streaming + status)
API_LOG = DATA / "historique_lectures.jsonl"   # ancien journal des lectures « status », importé dans la base
HISTORY_MAX_POINTS = 1500                      # points gardés par courbe dans la page

# Données suivies dans le temps (onglet « Historique »)
HISTORY_KEYS = [
    "vehicle.drivetrain.fuelSystem.level",
    "vehicle.drivetrain.lastRemainingRange",
    "vehicle.vehicle.travelledDistance",
    "vehicle.chassis.axle.row1.wheel.left.tire.pressure",
    "vehicle.chassis.axle.row1.wheel.right.tire.pressure",
    "vehicle.chassis.axle.row2.wheel.left.tire.pressure",
    "vehicle.chassis.axle.row2.wheel.right.tire.pressure",
    "vehicle.electricalSystem.battery.voltage",
    "vehicle.electricalSystem.battery.stateOfCharge",
]

CONTAINER_NAME = "pc-tableau-de-bord"

# Données demandées à BMW : (clé technique, libellé, groupe)
DESCRIPTORS = [
    # Général / entretien
    ("vehicle.vehicle.travelledDistance", "Kilométrage", "Général"),
    ("vehicle.status.serviceDistance.next", "Prochain entretien dans", "Général"),
    ("vehicle.status.serviceTime.inspectionDateLegal", "Prochaine inspection", "Général"),
    ("vehicle.status.conditionBasedServicesCount", "Messages d'entretien", "Général"),
    ("vehicle.status.conditionBasedServices", "Entretien détaillé", "Général"),
    ("vehicle.status.checkControlMessages", "Alertes Check Control", "Général"),
    ("vehicle.electronicControlUnit.diagnosticTroubleCodes.raw", "Mémoire des défauts", "Général"),
    # Énergie
    ("vehicle.drivetrain.fuelSystem.level", "Niveau de carburant", "Énergie"),
    ("vehicle.drivetrain.fuelSystem.remainingFuel", "Carburant restant", "Énergie"),
    ("vehicle.drivetrain.lastRemainingRange", "Autonomie totale", "Énergie"),
    ("vehicle.drivetrain.fuelSystem.consumptionOverLifeTime.overall.fuel", "Carburant consommé (total)", "Énergie"),
    ("vehicle.drivetrain.fuelSystem.consumptionOverLifeTime.overall.referenceDistance", "Distance de référence conso", "Énergie"),
    ("vehicle.powertrain.electric.battery.stateOfCharge.displayed", "Batterie haute tension", "Énergie"),
    ("vehicle.drivetrain.electricEngine.kombiRemainingElectricRange", "Autonomie électrique", "Énergie"),
    ("vehicle.drivetrain.electricEngine.charging.status", "État de la charge", "Énergie"),
    # Batteries
    ("vehicle.electricalSystem.battery.stateOfCharge", "Batterie 12 V (charge)", "Batteries"),
    ("vehicle.electricalSystem.battery.voltage", "Batterie 12 V (tension)", "Batteries"),
    ("vehicle.electricalSystem.battery48V.stateOfHealth.displayed", "Santé batterie 48 V", "Batteries"),
    ("vehicle.electricalSystem.battery.serviceDemand.recharge", "Batterie 12 V à recharger", "Batteries"),
    ("vehicle.electricalSystem.battery.serviceDemand.replace", "Santé batterie 12 V", "Batteries"),
    # Ouvrants
    ("vehicle.cabin.door.lock.status", "Verrouillage", "Ouvrants"),
    ("vehicle.cabin.door.row1.driver.isOpen", "Porte avant gauche", "Ouvrants"),
    ("vehicle.cabin.door.row1.passenger.isOpen", "Porte avant droite", "Ouvrants"),
    ("vehicle.cabin.door.row2.driver.isOpen", "Porte arrière gauche", "Ouvrants"),
    ("vehicle.cabin.door.row2.passenger.isOpen", "Porte arrière droite", "Ouvrants"),
    ("vehicle.body.trunk.isOpen", "Coffre", "Ouvrants"),
    ("vehicle.body.trunk.isLocked", "Coffre verrouillé", "Ouvrants"),
    ("vehicle.body.hood.isOpen", "Capot", "Ouvrants"),
    ("vehicle.cabin.window.row1.driver.status", "Vitre avant gauche", "Ouvrants"),
    ("vehicle.cabin.window.row1.passenger.status", "Vitre avant droite", "Ouvrants"),
    ("vehicle.cabin.window.row2.driver.status", "Vitre arrière gauche", "Ouvrants"),
    ("vehicle.cabin.window.row2.passenger.status", "Vitre arrière droite", "Ouvrants"),
    ("vehicle.cabin.sunroof.overallStatus", "Toit ouvrant", "Ouvrants"),
    ("vehicle.cabin.sunroof.relativePosition", "Ouverture du toit", "Ouvrants"),
    # Sécurité
    ("vehicle.vehicle.antiTheftAlarmSystem.alarm.armStatus", "Alarme", "Sécurité"),
    ("vehicle.vehicle.antiTheftAlarmSystem.alarm.isOn", "Alarme déclenchée", "Sécurité"),
    ("vehicle.body.lights.isRunningOn", "Phares allumés", "Sécurité"),
    # Conduite
    ("vehicle.drivetrain.engine.isActive", "Moteur en marche", "Conduite"),
    ("vehicle.drivetrain.engine.isIgnitionOn", "Contact mis", "Conduite"),
    ("vehicle.isMoving", "En mouvement", "Conduite"),
    ("vehicle.drivetrain.internalCombustionEngine.engine.ect", "Température liquide de refroidissement", "Conduite"),
    ("vehicle.status.conditionBasedServicesAverageDistancePerDay", "Distance moyenne par jour", "Conduite"),
    ("vehicle.vehicle.averageWeeklyDistanceShortTerm", "Distance moyenne par semaine", "Conduite"),
    ("vehicle.vehicle.averageWeeklyDistanceLongTerm", "Distance moyenne par semaine (long terme)", "Conduite"),
    ("vehicle.vehicle.speedRange.lowerBound", "Plage de vitesse (bas)", "Conduite"),
    ("vehicle.vehicle.speedRange.upperBound", "Plage de vitesse (haut)", "Conduite"),
    # Pneus
    ("vehicle.chassis.axle.row1.wheel.left.tire.pressure", "Pneu avant gauche", "Pneus"),
    ("vehicle.chassis.axle.row1.wheel.right.tire.pressure", "Pneu avant droit", "Pneus"),
    ("vehicle.chassis.axle.row2.wheel.left.tire.pressure", "Pneu arrière gauche", "Pneus"),
    ("vehicle.chassis.axle.row2.wheel.right.tire.pressure", "Pneu arrière droit", "Pneus"),
    ("vehicle.chassis.axle.row1.wheel.left.tire.pressureTarget", "Consigne avant gauche", "Pneus"),
    ("vehicle.chassis.axle.row1.wheel.right.tire.pressureTarget", "Consigne avant droit", "Pneus"),
    ("vehicle.chassis.axle.row2.wheel.left.tire.pressureTarget", "Consigne arrière gauche", "Pneus"),
    ("vehicle.chassis.axle.row2.wheel.right.tire.pressureTarget", "Consigne arrière droit", "Pneus"),
    ("vehicle.chassis.axle.row1.wheel.left.tire.temperature", "Température pneu AV G", "Pneus"),
    ("vehicle.chassis.axle.row1.wheel.right.tire.temperature", "Température pneu AV D", "Pneus"),
    ("vehicle.chassis.axle.row2.wheel.left.tire.temperature", "Température pneu AR G", "Pneus"),
    ("vehicle.chassis.axle.row2.wheel.right.tire.temperature", "Température pneu AR D", "Pneus"),
    # Navigation / position
    ("vehicle.cabin.infotainment.navigation.currentLocation.latitude", "Latitude", "Position"),
    ("vehicle.cabin.infotainment.navigation.currentLocation.longitude", "Longitude", "Position"),
    ("vehicle.cabin.infotainment.navigation.currentLocation.heading", "Orientation", "Position"),
    ("vehicle.cabin.infotainment.navigation.currentLocation.altitude", "Altitude", "Position"),
    ("vehicle.cabin.infotainment.navigation.destinationSet.distance", "Distance jusqu'à destination", "Position"),
    ("vehicle.cabin.infotainment.navigation.destinationSet.arrivalTime", "Temps jusqu'à destination", "Position"),
    # Confort
    ("vehicle.cabin.hvac.preconditioning.status.comfortState", "Climatisation programmée", "Confort"),
    ("vehicle.cabin.hvac.preconditioning.configuration.defaultSettings.targetTemperature", "Température de consigne clim", "Confort"),
    ("vehicle.cabin.climate.timers.weekdaysTimer1.action", "Programmateur clim 1", "Confort"),
    ("vehicle.cabin.climate.timers.weekdaysTimer1.hour", "Programmateur 1 (heure)", "Confort"),
    ("vehicle.cabin.climate.timers.weekdaysTimer1.minute", "Programmateur 1 (minute)", "Confort"),
    ("vehicle.cabin.climate.timers.weekdaysTimer2.action", "Programmateur clim 2", "Confort"),
    ("vehicle.cabin.climate.timers.weekdaysTimer2.hour", "Programmateur 2 (heure)", "Confort"),
    ("vehicle.cabin.climate.timers.weekdaysTimer2.minute", "Programmateur 2 (minute)", "Confort"),
]
LABELS = {k: label for k, label, _ in DESCRIPTORS}

# Traduction de quelques valeurs fréquentes (les autres sont affichées telles quelles)
VALUE_FR = {
    "LOCKED": "Verrouillée", "SECURED": "Verrouillée (sécurisée)", "UNLOCKED": "Déverrouillée",
    "SELECTIVE_LOCKED": "Partiellement verrouillée",
    "CLOSED": "Fermé(e)", "OPEN": "Ouvert(e)", "INTERMEDIATE": "Entrouvert(e)",
    "CHARGING": "En charge", "NOCHARGING": "Pas en charge", "NOT_CHARGING": "Pas en charge",
    "FULLY_CHARGED": "Chargée", "COMPLETE": "Chargée", "ERROR": "Erreur",
    "ARMED": "Activée", "UNARMED": "Désactivée", "DISARMED": "Désactivée",
}

# Unités à utiliser quand BMW n'en fournit pas
DEFAULT_UNITS = {
    "vehicle.status.serviceDistance.next": "km",
    "vehicle.drivetrain.fuelSystem.remainingFuel": "l",
    "vehicle.vehicle.averageWeeklyDistanceShortTerm": "km",
    "vehicle.vehicle.averageWeeklyDistanceLongTerm": "km",
    "vehicle.status.conditionBasedServicesAverageDistancePerDay": "km",
    "vehicle.cabin.infotainment.navigation.currentLocation.heading": "°",
    "vehicle.cabin.infotainment.navigation.currentLocation.altitude": "m",
}

# Valeurs codées, d'après le catalogue officiel BMW CarData
SPECIAL_VALUES = {
    "vehicle.electricalSystem.battery.serviceDemand.replace": {
        "200": "Bonne", "140": "Limitée (remplacement conseillé)",
        "110": "Insuffisante (à remplacer)", "80": "Dégradée (remplacement urgent)"},
    "vehicle.electricalSystem.battery.serviceDemand.recharge": {"0": "Non", "1": "Oui"},
    "vehicle.vehicle.antiTheftAlarmSystem.alarm.armStatus": {
        "unarmed": "Désactivée", "doorsOnly": "Activée (portes)",
        "doorsTiltCabin": "Activée (portes, inclinaison, habitacle)"},
}

# Au-delà de ce nombre de jours, une valeur est signalée comme ancienne
STALE_DAYS = 7

_lock = threading.Lock()  # protège les écritures de fichiers en mode streaming


# =============================================================================
# Utilitaires
# =============================================================================
def load_json(path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, data):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)  # lisible par vous seul (sans effet notable sous Windows)
    except OSError:
        pass
    tmp.replace(path)


def die(msg):
    print(f"\n[ERREUR] {msg}")
    sys.exit(1)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def http(method, url, *, form=None, body=None, headers=None):
    """Requête HTTP simple. Renvoie (code, contenu décodé en JSON ou texte)."""
    headers = dict(headers or {})
    data = None
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            code, raw = resp.status, resp.read()
    except urllib.error.HTTPError as e:
        code, raw = e.code, e.read()
    except urllib.error.URLError as e:
        die(f"Impossible de joindre {url} : {e.reason}")
    text = raw.decode("utf-8", errors="replace")
    try:
        return code, json.loads(text) if text else {}
    except json.JSONDecodeError:
        return code, text


# =============================================================================
# Authentification (OAuth "Device Code Flow" + PKCE)
# =============================================================================
def get_client_id(config):
    cid = config.get("client_id")
    if not cid:
        print("Collez le Client ID créé dans le portail BMW CarData")
        print("(My BMW sur le web > votre véhicule > BMW CarData > Create CarData Client) :")
        cid = input("> ").strip()
        if not cid:
            die("Client ID vide.")
        config["client_id"] = cid
        save_json(CONFIG_FILE, config)
    return cid


def store_tokens(tok, previous=None):
    previous = previous or {}
    for k in ("refresh_token", "gcid", "id_token"):  # on garde ce que BMW ne renvoie pas toujours
        if not tok.get(k) and previous.get(k):
            tok[k] = previous[k]
    tok["obtained_at"] = int(time.time())
    save_json(TOKENS_FILE, tok)
    return tok


def request_device_code(client_id, scope, challenge):
    return http("POST", DEVICE_CODE_URL, form={
        "client_id": client_id,
        "response_type": "device_code",
        "scope": scope,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })


def login():
    config = load_json(CONFIG_FILE, {})
    client_id = get_client_id(config)

    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()

    # On demande aussi le droit de streaming ; si le client n'y est pas abonné, on se rabat sur l'API seule.
    scope = SCOPES_FULL
    code, dc = request_device_code(client_id, scope, challenge)
    if code != 200 or not isinstance(dc, dict) or "device_code" not in dc:
        scope = SCOPES_API
        code, dc = request_device_code(client_id, scope, challenge)
        if code == 200:
            print("Note : le streaming n'est pas activé pour ce client (connexion API seule).")
            print("       Pour l'activer : portail BMW CarData > interrupteur « CarData Stream », puis refaites login.")
    if code != 200 or not isinstance(dc, dict) or "device_code" not in dc:
        die(f"Demande de code refusée (HTTP {code}) : {dc}\n"
            "Vérifiez le Client ID et qu'il est bien abonné à « CarData API » dans le portail.")

    url = dc.get("verification_uri_complete") or dc["verification_uri"]
    print("\n1. Ouvrez cette page :", url)
    print("2. Connectez-vous avec votre compte BMW et saisissez ce code :", dc["user_code"])
    print("\n(En attente de votre validation... Ctrl+C pour annuler)")
    try:
        webbrowser.open(url)
    except Exception:
        pass

    interval = int(dc.get("interval", 5))
    deadline = time.time() + int(dc.get("expires_in", 300))
    while time.time() < deadline:
        time.sleep(interval)
        code, tok = http("POST", TOKEN_URL, form={
            "client_id": client_id,
            "device_code": dc["device_code"],
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "code_verifier": verifier,
        })
        if code == 200 and isinstance(tok, dict) and "access_token" in tok:
            store_tokens(tok)
            config["streaming_scope"] = "cardata:streaming:read" in (tok.get("scope") or scope)
            save_json(CONFIG_FILE, config)
            print("\nConnexion réussie. Jetons enregistrés dans tokens.json (ne partagez pas ce fichier).")
            print("Streaming :", "autorisé" if config["streaming_scope"] else "non autorisé")
            print("Lancez maintenant :  python bmw_cardata.py status")
            return
        err = tok.get("error") if isinstance(tok, dict) else None
        if err == "authorization_pending":
            continue
        if err == "slow_down":
            interval += 5
            continue
        die(f"Connexion échouée (HTTP {code}) : {tok}")
    die("Le code a expiré avant validation. Relancez :  python bmw_cardata.py login")


def get_tokens(margin=120, force=False):
    """Renvoie les jetons (access_token, id_token, gcid...) valides, en les rafraîchissant si besoin."""
    tok = load_json(TOKENS_FILE)
    if not tok:
        die("Pas encore connecté. Lancez d'abord :  python bmw_cardata.py login")
    age = time.time() - tok.get("obtained_at", 0)
    if not force and age < int(tok.get("expires_in", 3600)) - margin:
        return tok

    config = load_json(CONFIG_FILE, {})
    code, new = http("POST", TOKEN_URL, form={
        "client_id": config.get("client_id", ""),
        "grant_type": "refresh_token",
        "refresh_token": tok.get("refresh_token", ""),
    })
    if code != 200 or not isinstance(new, dict) or "access_token" not in new:
        die("La session a expiré (plus de 14 jours sans utilisation ?).\n"
            "Reconnectez-vous :  python bmw_cardata.py login")
    return store_tokens(new, tok)


def get_access_token():
    return get_tokens()["access_token"]


# =============================================================================
# Appels à l'API CarData
# =============================================================================
def api(method, path, token, body=None, fatal=True):
    code, data = http(method, API_HOST + path, body=body, headers={
        "Authorization": f"Bearer {token}",
        "x-version": API_VERSION,
        "Accept": "application/json",
    })
    if code == 403 and "429" in str(data):
        die("Limite quotidienne de l'API BMW atteinte (~50 appels/jour). Réessayez demain.")
    if code == 401:
        die("Accès refusé (401). Essayez :  python bmw_cardata.py login")
    if code >= 400:
        if not fatal:
            return None
        die(f"Erreur API {method} {path} (HTTP {code}) : {data}")
    return data


def ensure_vin(config, token):
    if config.get("vin"):
        return config["vin"]
    data = api("GET", "/customers/vehicles/mappings", token)
    vehicles = data if isinstance(data, list) else data.get("mappings", data.get("vehicles", []))
    primary = [v for v in vehicles if v.get("mappingType") == "PRIMARY"] or vehicles
    if not primary:
        die("Aucun véhicule associé à ce compte BMW.")
    if len(primary) > 1:
        print("Plusieurs véhicules trouvés :")
        for i, v in enumerate(primary, 1):
            print(f"  {i}. {v['vin']}")
        choice = input("Numéro du véhicule à suivre > ").strip()
        vin = primary[int(choice) - 1]["vin"]
    else:
        vin = primary[0]["vin"]
    config["vin"] = vin
    save_json(CONFIG_FILE, config)
    print(f"Véhicule mémorisé : {vin}")
    return vin


def descriptors_signature():
    keys = sorted(d[0] for d in DESCRIPTORS)
    return hashlib.sha1("\n".join(keys).encode()).hexdigest()[:12]


def ensure_container(config, token):
    """Crée (ou recrée si la liste des données a changé) le conteneur de données chez BMW."""
    sig = descriptors_signature()
    if config.get("container_id") and config.get("container_sig") == sig:
        return config["container_id"]

    old_id = config.get("container_id")
    name = f"{CONTAINER_NAME}-{sig}"
    created = api("POST", "/customers/containers", token, body={
        "name": name,
        "purpose": "Tableau de bord personnel sur PC",
        "technicalDescriptors": [d[0] for d in DESCRIPTORS],
    })
    config["container_id"] = created.get("containerId") or created.get("id")
    config["container_sig"] = sig
    save_json(CONFIG_FILE, config)
    print(f"Conteneur de données {'mis à jour' if old_id else 'créé'} chez BMW ({len(DESCRIPTORS)} données).")
    if old_id:  # on fait le ménage dans l'ancien conteneur (sans bloquer en cas d'échec)
        api("DELETE", f"/customers/containers/{urllib.parse.quote(old_id)}", token, fatal=False)
    return config["container_id"]


# =============================================================================
# Données : fusion API + streaming
# =============================================================================
def merge_telematic(current, incoming):
    """Fusionne de nouvelles valeurs en gardant, pour chaque donnée, la plus récente."""
    changed = []
    for key, entry in (incoming or {}).items():
        if not isinstance(entry, dict):
            entry = {"value": entry}
        old = current.get(key) or {}
        new_ts, old_ts = entry.get("timestamp") or "", old.get("timestamp") or ""
        if entry.get("value") is None and old.get("value") is not None:
            continue  # on n'écrase pas une vraie valeur par « rien »
        if not old or new_ts >= old_ts:
            if old.get("value") != entry.get("value") or old_ts != new_ts:
                changed.append(key)
            current[key] = {"value": entry.get("value"), "unit": entry.get("unit"),
                            "timestamp": entry.get("timestamp")}
    return changed


def load_last():
    return load_json(LAST_FILE, {}) or {}


# =============================================================================
# Mise en forme (terminal)
# =============================================================================
def fmt_value(entry, key=""):
    if not entry or entry.get("value") in (None, ""):
        return None
    value = str(entry["value"])
    unit = entry.get("unit") or DEFAULT_UNITS.get(key, "")
    if unit.lower() == "kpa":  # le streaming écrit « kpa », l'API « kPa »
        unit = "kPa"
    if value in SPECIAL_VALUES.get(key, {}):
        return SPECIAL_VALUES[key][value]
    if value.startswith("<"):  # mémoire des défauts (XML)
        return f"{value.count('<dtc ')} code(s) enregistré(s)"
    if key.endswith("Timer1.action") or key.endswith("Timer2.action"):
        return "Activé" if value.upper().startswith("ACTIV") else "Désactivé"
    if value[:1] in "[{":  # listes (entretien, alertes...) : on résume
        try:
            parsed = json.loads(value)
            n = len(parsed) if isinstance(parsed, (list, dict)) else 1
            return f"{n} élément(s)"
        except ValueError:
            return value[:40] + ("…" if len(value) > 40 else "")
    if value.lower() in ("true", "false"):
        yes = value.lower() == "true"
        if key.endswith("isOpen"):
            return "Ouvert(e)" if yes else "Fermé(e)"
        return "Oui" if yes else "Non"
    if value in VALUE_FR:
        return VALUE_FR[value]
    if unit == "degrees":  # coordonnées GPS : on garde toute la précision
        return f"{value}°" if key.endswith("heading") else value
    if unit == "kPa":      # pression des pneus : affichée en bar
        try:
            return f"{float(value) / 100:.1f} bar"
        except ValueError:
            pass
    try:
        num = float(value)
        value = f"{num:,.0f}".replace(",", " ") if num.is_integer() or abs(num) >= 100 else f"{num:.1f}"
    except ValueError:
        pass
    unit = {"percent": "%", "km": "km", "l": "L", "V": "V", "celsius": "°C", "Celsius": "°C"}.get(unit, unit)
    return f"{value} {unit}".strip()


def parse_time(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
    except (ValueError, AttributeError):
        return None


def build_rows(telematic):
    rows = []
    for key, label, group in DESCRIPTORS:
        entry = telematic.get(key) or {}
        dt = parse_time(entry.get("timestamp"))
        stale = bool(dt and (datetime.now(timezone.utc) - dt).days >= STALE_DAYS)
        rows.append({
            "group": group, "label": label,
            "value": fmt_value(entry, key),
            "time": (dt.strftime("%d/%m/%Y %H:%M") if dt else entry.get("timestamp") or "")
                    + (" (ancienne donnée)" if stale else ""),
            "stale": stale,
        })
    return rows


def print_table(rows):
    current = None
    for r in rows:
        if r["value"] is None:
            continue
        if r["group"] != current:
            current = r["group"]
            print(f"\n== {current} ==")
        print(f"  {r['label']:<40} {r['value']:<24} {r['time']}")
    missing = sum(1 for r in rows if r["value"] is None)
    if missing:
        print(f"\n({missing} donnée(s) non disponible(s) pour ce véhicule ou pas encore transmise(s).)")


# =============================================================================
# Catalogue BMW et historique du streaming
# =============================================================================
def ensure_catalogue():
    """Télécharge une fois le catalogue public BMW CarData (noms français des ~300 données)."""
    if CATALOGUE_FILE.exists():
        return
    names, offset = {}, 0
    try:
        while offset < 1000:
            req = urllib.request.Request(CATALOGUE_URL + str(offset),
                                         headers={"Accept": "application/json", "User-Agent": "ma-bmw-tableau-de-bord"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                items = json.loads(resp.read().decode("utf-8")).get("data", {}).get("items", [])
            if not items:
                break
            for it in items:
                names[it["id"]] = {"name": it.get("name"), "unit": it.get("unit"), "category": it.get("category")}
            offset += len(items)
    except (urllib.error.URLError, ValueError, KeyError, OSError):
        return  # pas grave : la page affichera les clés techniques
    if names:
        save_json(CATALOGUE_FILE, names)


def read_stream_history(limit=STREAM_LOG_IN_PAGE):
    """Les derniers messages reçus en direct (les plus récents à la fin)."""
    if not STREAM_LOG.exists():
        return []
    with STREAM_LOG.open("rb") as f:  # on ne lit que la fin du fichier
        f.seek(0, 2)
        size = f.seek(max(0, f.tell() - 600_000))
        lines = f.read().decode("utf-8", errors="replace").splitlines()
    if size:
        lines = lines[1:]  # première ligne probablement coupée
    out = []
    for line in lines[-limit:]:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return out


# =============================================================================
# Historique (base SQLite)
# =============================================================================
DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS mesures (
    cle        TEXT NOT NULL,  -- clé technique BMW (ex. vehicle.vehicle.travelledDistance)
    horodatage TEXT NOT NULL,  -- date de la valeur donnée par la voiture (ISO 8601)
    t          REAL NOT NULL,  -- la même date en secondes Unix (tri et filtres)
    valeur,                    -- nombre si possible, sinon texte
    unite      TEXT,
    source     TEXT,           -- stream, api ou derniere_lecture
    recu       TEXT,           -- date de réception sur le PC
    PRIMARY KEY (cle, horodatage)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS mesures_cle_t ON mesures (cle, t);
"""


def _db_value(v):
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, str):
        try:
            return float(v) if v.strip() else v
        except ValueError:
            return v
    return None if v is None else json.dumps(v, ensure_ascii=False)


def _db_record(db, data, source, received=None):
    rows = []
    for k, e in (data or {}).items():
        if not isinstance(e, dict):
            continue
        dt = parse_time(e.get("timestamp"))
        if dt:
            rows.append((k, e["timestamp"], dt.timestamp(), _db_value(e.get("value")), e.get("unit"),
                         source, received))
    db.executemany("INSERT OR IGNORE INTO mesures VALUES (?, ?, ?, ?, ?, ?, ?)", rows)


def open_db():
    """Ouvre la base de l'historique. À la première ouverture, elle est créée puis remplie
    avec les anciens journaux (.jsonl) et les dernières valeurs connues."""
    db = sqlite3.connect(HISTORY_DB, timeout=10)
    db.executescript(DB_SCHEMA)
    if db.execute("PRAGMA user_version").fetchone()[0] == 0:
        with db:
            for path, source in ((STREAM_LOG, "stream"), (API_LOG, "api")):
                if not path.exists():
                    continue
                with path.open(encoding="utf-8", errors="replace") as f:
                    for line in f:
                        try:
                            entry = json.loads(line)
                            _db_record(db, entry.get("data"), entry.get("source", source), entry.get("received"))
                        except (json.JSONDecodeError, AttributeError):
                            pass
            last = load_json(LAST_FILE, {}) or {}
            _db_record(db, last.get("telematicData"), "derniere_lecture", last.get("fetched_iso"))
            db.execute("PRAGMA user_version = 1")
    return db


def save_history(data, source, received=None):
    """Ajoute des valeurs reçues à la base (une valeur déjà connue n'est pas dupliquée)."""
    try:
        with closing(open_db()) as db, db:
            _db_record(db, data, source, received or now_iso())
    except sqlite3.Error as e:
        print(f"Base {HISTORY_DB.name} : écriture impossible ({e}).")


REFUEL_MIN_RISE = 10  # hausse du niveau (points de %) qui signale un plein


def since_refuel(telematic, tank_liters):
    """Consommation estimée depuis le dernier plein, d'après le niveau du réservoir (en %) et le
    compteur. BMW ne transmet pas cette donnée : le niveau n'a qu'une précision de 1 %, donc
    l'estimation s'affine avec les kilomètres. Renvoie None s'il n'y a pas de quoi calculer."""
    lvl_key, odo_key = "vehicle.drivetrain.fuelSystem.level", "vehicle.vehicle.travelledDistance"
    series = {lvl_key: {}, odo_key: {}}
    try:
        with closing(open_db()) as db:
            for k, t, v in db.execute("SELECT cle, t, valeur FROM mesures WHERE cle IN (?, ?)", (lvl_key, odo_key)):
                if isinstance(v, (int, float)):
                    series[k][t] = v
    except sqlite3.Error as e:
        print(f"Base {HISTORY_DB.name} : lecture impossible ({e}).")
    for k in series:  # dernière valeur connue, au cas où elle ne serait pas encore dans la base
        e = (telematic or {}).get(k)
        dt = parse_time(e.get("timestamp")) if isinstance(e, dict) else None
        if dt:
            try:
                series[k][dt.timestamp()] = float(e.get("value"))
            except (TypeError, ValueError):
                pass
    lvl, odo = sorted(series[lvl_key].items()), sorted(series[odo_key].items())
    if len(lvl) < 2 or not odo:
        return None
    start, refuel = 0, False
    for i in range(1, len(lvl)):
        if lvl[i][1] - lvl[i - 1][1] >= REFUEL_MIN_RISE:
            start, refuel = i, True
    t0, l0 = lvl[start]
    # le niveau remonte parfois d'un point après le plein (voiture à plat) : on part du plus haut
    l0 = max(v for t, v in lvl[start:] if t - t0 < 3600)
    t1, l1 = lvl[-1]
    before = [v for t, v in odo if t <= t0]
    km0 = before[-1] if before else next((v for t, v in odo if t - t0 < 86400), None)
    if km0 is None:
        return None
    km = max(0, odo[-1][1] - km0)
    used_pct = max(0, l0 - l1)
    out = {"since": datetime.fromtimestamp(t0, timezone.utc).isoformat(), "refuel": refuel,
           "startLevel": l0, "level": l1, "km": km, "usedPct": used_pct, "tank": tank_liters}
    if tank_liters:
        out["liters"] = round(used_pct / 100 * tank_liters, 1)
        if km > 0:
            out["l100"] = round(out["liters"] / km * 100, 1)
            # ±1 point de % à chaque bout : marge de l'estimation
            out["margin"] = round(2 / 100 * tank_liters / km * 100, 1)
    return out


HISTORY_ONE_MAX = 5000  # valeurs renvoyées au plus pour une donnée (onglet « Toutes les données »)


def key_history(key):
    """Toutes les valeurs connues d'une donnée, de la plus récente à la plus ancienne :
    [[horodatage, valeur, unité, source], ...]."""
    if not re.fullmatch(r"vehicle(\.[A-Za-z0-9_]+)+", key or ""):
        return []
    try:
        with closing(open_db()) as db:
            return [list(r) for r in db.execute(
                "SELECT horodatage, valeur, unite, source FROM mesures WHERE cle = ? ORDER BY t DESC LIMIT ?",
                (key, HISTORY_ONE_MAX))]
    except sqlite3.Error as e:
        print(f"Base {HISTORY_DB.name} : lecture impossible ({e}).")
        return []


def build_history(telematic):
    """Séries temporelles {clé: [[horodatage ISO, valeur], ...]} lues dans la base, complétées
    par les dernières valeurs connues. Une valeur est datée par la voiture (timestamp), pas par
    sa réception."""
    points = {k: {} for k in HISTORY_KEYS}

    def add(k, ts, t, v):
        try:
            points[k][ts] = (t, float(v))
        except (TypeError, ValueError):
            pass

    try:
        with closing(open_db()) as db:
            marks = ",".join("?" * len(HISTORY_KEYS))
            for k, ts, t, v in db.execute(
                    f"SELECT cle, horodatage, t, valeur FROM mesures WHERE cle IN ({marks})", HISTORY_KEYS):
                add(k, ts, t, v)
    except sqlite3.Error as e:
        print(f"Base {HISTORY_DB.name} : lecture impossible ({e}).")
    for k, e in (telematic or {}).items():
        dt = parse_time(e.get("timestamp")) if k in points and isinstance(e, dict) else None
        if dt:
            add(k, e["timestamp"], dt.timestamp(), e.get("value"))

    out = {}
    for k, by_ts in points.items():
        serie = []
        for t, (_, v) in sorted(by_ts.items(), key=lambda kv: kv[1][0]):
            # valeur inchangée : on ne garde que le premier et le dernier point du palier
            if len(serie) >= 2 and serie[-1][1] == v and serie[-2][1] == v:
                serie[-1] = [t, v]
            else:
                serie.append([t, v])
        if len(serie) > HISTORY_MAX_POINTS:
            step = len(serie) / HISTORY_MAX_POINTS
            serie = [serie[int(i * step)] for i in range(HISTORY_MAX_POINTS - 1)] + [serie[-1]]
        if serie:
            out[k] = serie
    return out


# =============================================================================
# Tableau de bord (HTML)
# =============================================================================
def render_dashboard(vin, telematic, fetched_at, fetched_iso=None, live=False, path=DASHBOARD_FILE):
    if not TEMPLATE_FILE.exists():
        die(f"Modèle de page introuvable : {TEMPLATE_FILE.name} (gardez-le à côté du script).")
    image = next((p.name for p in sorted(DATA.glob("voiture.*"))), None)
    payload = {
        "vin": vin,
        "fetchedLabel": fetched_at,
        "fetchedIso": fetched_iso,
        "telematic": telematic,
        "profile": load_json(PROFILE_FILE, {}),
        "image": image,
        "labels": LABELS,
        "catalogue": {k: v.get("name") for k, v in (load_json(CATALOGUE_FILE, {}) or {}).items()},
        "streamLog": list(LiveState.log)[-STREAM_LOG_IN_PAGE:] if live else read_stream_history(),
        "history": build_history(telematic),
        "sinceRefuel": since_refuel(telematic, (load_json(CONFIG_FILE, {}) or {}).get("tank_liters")),
        "live": live,
    }
    blob = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    page = TEMPLATE_FILE.read_text(encoding="utf-8").replace("/*__DATA__*/{}", blob, 1)
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(page, encoding="utf-8")
    tmp.replace(path)


def display(vin, telematic, fetched_at, fetched_iso=None):
    print(f"\nBMW {vin} — lu le {fetched_at}")
    print_table(build_rows(telematic))
    render_dashboard(vin, telematic, fetched_at, fetched_iso)
    print(f"\nTableau de bord : {DASHBOARD_FILE}")
    if "--open" in sys.argv:
        webbrowser.open(DASHBOARD_FILE.as_uri())


# =============================================================================
# Commandes
# =============================================================================
def status():
    config = load_json(CONFIG_FILE, {})
    if not config.get("client_id"):
        die("Pas encore configuré. Lancez d'abord :  python bmw_cardata.py login")
    token = get_access_token()
    vin = ensure_vin(config, token)
    container_id = ensure_container(config, token)
    ensure_catalogue()

    q = urllib.parse.urlencode({"containerId": container_id})
    data = api("GET", f"/customers/vehicles/{urllib.parse.quote(vin)}/telematicData?{q}", token)
    incoming = data.get("telematicData", {}) if isinstance(data, dict) else {}
    save_history(incoming, "api")

    last = load_last()
    telematic = last.get("telematicData", {}) if last.get("vin") == vin else {}
    merge_telematic(telematic, incoming)

    fetched_at = datetime.now().strftime("%d/%m/%Y %H:%M")
    fetched_iso = now_iso()
    save_json(LAST_FILE, {"vin": vin, "fetched_at": fetched_at, "fetched_iso": fetched_iso,
                          "telematicData": telematic})
    display(vin, telematic, fetched_at, fetched_iso)


def profile():
    """Récupère une fois pour toutes le modèle et la photo de la voiture (2 appels)."""
    config = load_json(CONFIG_FILE, {})
    if not config.get("client_id"):
        die("Pas encore configuré. Lancez d'abord :  python bmw_cardata.py login")
    token = get_access_token()
    vin = ensure_vin(config, token)
    v = urllib.parse.quote(vin)

    basic = api("GET", f"/customers/vehicles/{v}/basicData", token)
    save_json(PROFILE_FILE, basic)
    print("Fiche du véhicule enregistrée :", PROFILE_FILE.name)

    req = urllib.request.Request(API_HOST + f"/customers/vehicles/{v}/image", headers={
        "Authorization": f"Bearer {token}",
        "x-version": API_VERSION,
        "Accept": "image/png,image/jpeg",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            ctype, raw = resp.headers.get("Content-Type", ""), resp.read()
        ext = ".jpg" if "jpeg" in ctype else ".png"
        for old in DATA.glob("voiture.*"):
            old.unlink()
        (DATA / f"voiture{ext}").write_bytes(raw)
        print("Photo du véhicule enregistrée :", f"voiture{ext}")
    except urllib.error.HTTPError as e:
        print(f"Photo indisponible (HTTP {e.code}). Le tableau de bord utilisera un dessin à la place.")

    for k in ("brand", "modelName", "series", "bodyType", "driveTrain", "constructionDate"):
        if basic.get(k):
            print(f"  {k:<18} {basic[k]}")


def show():
    """Réaffiche la dernière lecture sans appeler BMW (0 appel consommé)."""
    last = load_last()
    if not last:
        die("Aucune lecture enregistrée. Lancez d'abord :  python bmw_cardata.py status")
    fetched_at = last.get("fetched_at", "?")
    fetched_iso = last.get("fetched_iso")
    if not fetched_iso:  # anciennes lectures : on reconstruit la date à partir du libellé
        try:
            fetched_iso = datetime.strptime(fetched_at, "%d/%m/%Y %H:%M").astimezone().isoformat()
        except ValueError:
            fetched_iso = None
    display(last["vin"], last.get("telematicData", {}), fetched_at, fetched_iso)


def reset():
    config = load_json(CONFIG_FILE, {})
    for k in ("vin", "container_id", "container_sig"):
        config.pop(k, None)
    save_json(CONFIG_FILE, config)
    print("VIN et conteneur oubliés. Le prochain « status » les redemandera.")


# =============================================================================
# Streaming en direct (MQTT)
# =============================================================================
class LiveState:
    version = 0
    connected = False
    last_message = None
    messages = 0
    seq = 0
    log = deque(maxlen=2000)  # derniers messages reçus, servis à l'onglet « Flux en direct »


def start_live_server():
    """Petit serveur local qui sert UNIQUEMENT la page, la photo, l'état « en direct » et
    l'historique d'une donnée (jamais les jetons ni la configuration)."""
    allowed = {"/": DASHBOARD_FILE, "/tableau_de_bord.html": DASHBOARD_FILE}
    for p in DATA.glob("voiture.*"):
        allowed["/" + p.name] = p

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.split("?")[0]
            if path == "/live.json":
                body = json.dumps({"version": LiveState.version, "connected": LiveState.connected,
                                   "lastMessage": LiveState.last_message,
                                   "messages": LiveState.messages}).encode()
                ctype = "application/json"
            elif path == "/stream.json":  # messages reçus après le n° « after »
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                try:
                    after = int(q.get("after", ["0"])[0])
                except ValueError:
                    after = 0
                with _lock:
                    msgs = [m for m in LiveState.log if m["seq"] > after]
                body = json.dumps({"connected": LiveState.connected, "messages": msgs},
                                  ensure_ascii=False).encode()
                ctype = "application/json"
            elif path == "/history.json":  # toutes les valeurs d'une donnée (clé « key »)
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                body = json.dumps({"values": key_history(q.get("key", [""])[0])}, ensure_ascii=False).encode()
                ctype = "application/json"
            elif path in allowed and allowed[path].exists():
                with _lock:
                    body = allowed[path].read_bytes()
                ctype = "image/png" if path.endswith(".png") else \
                        "image/jpeg" if path.endswith(".jpg") else "text/html; charset=utf-8"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    for port in range(LIVE_PORT, LIVE_PORT + 10):
        try:
            server = ThreadingHTTPServer((LIVE_HOST, port), Handler)
        except OSError:
            continue
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return f"http://{LIVE_HOST}:{port}/"
    return None


def stream():
    try:
        import paho.mqtt.client as mqtt
        from paho.mqtt.enums import CallbackAPIVersion
    except ImportError:
        die("Le streaming a besoin de la bibliothèque paho-mqtt (version 2). Installez-la avec :\n"
            "    python -m pip install paho-mqtt")

    config = load_json(CONFIG_FILE, {})
    if not config.get("client_id"):
        die("Pas encore configuré. Lancez d'abord :  python bmw_cardata.py login")
    if not config.get("streaming_scope"):
        die("Le streaming n'est pas autorisé pour votre connexion.\n"
            "1. Portail BMW CarData : activez « CarData Stream » et créez un flux (choix des données).\n"
            "2. Refaites :  python bmw_cardata.py login")
    tok = get_tokens(margin=300)
    if not tok.get("gcid") or not tok.get("id_token"):
        die("Jetons incomplets (gcid / id_token manquants). Refaites :  python bmw_cardata.py login")
    vin = config.get("vin") or ensure_vin(config, tok["access_token"])
    # Les paramètres affichés sur le portail BMW se déduisent de la connexion ; on peut les forcer
    # dans config.json (stream_host, stream_port, stream_username, stream_topic) si BMW les change.
    gcid = config.get("stream_username") or tok["gcid"]
    topic = config.get("stream_topic") or f"{gcid}/{vin}"
    host = config.get("stream_host") or STREAM_HOST
    port = int(config.get("stream_port") or STREAM_PORT)

    ensure_catalogue()
    for m in read_stream_history(LiveState.log.maxlen):  # historique déjà reçu, numéroté dans l'ordre
        LiveState.seq += 1
        m["seq"] = LiveState.seq
        LiveState.log.append(m)

    # Page de départ avec les dernières données connues
    last = load_last()
    telematic = last.get("telematicData", {}) if last.get("vin") == vin else {}
    fetched_at = last.get("fetched_at") or datetime.now().strftime("%d/%m/%Y %H:%M")
    with _lock:
        render_dashboard(vin, telematic, fetched_at, last.get("fetched_iso") or now_iso(), live=True)
    url = start_live_server()

    state = {"need_reconnect": False}

    def on_connect(client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            LiveState.connected = False
            print(f"[{datetime.now():%H:%M:%S}] Connexion refusée par BMW : {reason_code}")
            state["need_reconnect"] = True
            return
        LiveState.connected = True
        print(f"[{datetime.now():%H:%M:%S}] Connecté au flux BMW. En attente de données…")
        client.subscribe(topic, qos=1)

    def on_disconnect(client, userdata, flags, reason_code, properties):
        LiveState.connected = False
        print(f"[{datetime.now():%H:%M:%S}] Déconnecté ({reason_code}, code {getattr(reason_code, 'value', reason_code)}).")
        if reason_code.is_failure:
            state["need_reconnect"] = True

    def on_message(client, userdata, msg):
        nonlocal telematic, fetched_at
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            print("Message illisible ignoré.")
            return
        if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
            incoming = payload["data"]
        elif isinstance(payload, dict) and "value" in payload and msg.topic.count("/") > 1:
            incoming = {msg.topic.split("/", 2)[2].replace("/", "."): payload}  # un sujet par donnée
        else:
            print("Format de message inattendu, ignoré.")
            return

        with _lock:
            entry = {"received": now_iso(), "topic": msg.topic, "data": incoming}
            with STREAM_LOG.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            save_history(incoming, "stream", entry["received"])
            LiveState.seq += 1
            LiveState.log.append(dict(entry, seq=LiveState.seq))
            changed = merge_telematic(telematic, incoming)
            fetched_at = datetime.now().strftime("%d/%m/%Y %H:%M")
            save_json(LAST_FILE, {"vin": vin, "fetched_at": fetched_at, "fetched_iso": now_iso(),
                                  "telematicData": telematic})
            render_dashboard(vin, telematic, fetched_at, now_iso(), live=True)
        LiveState.version += 1
        LiveState.messages += 1
        LiveState.last_message = now_iso()

        print(f"[{datetime.now():%H:%M:%S}] {len(incoming)} donnée(s) reçue(s) :")
        for key in (changed or list(incoming))[:12]:
            shown = fmt_value(telematic.get(key) or incoming.get(key), key)
            print(f"    {LABELS.get(key, key):<40} {shown}")

    client = mqtt.Client(callback_api_version=CallbackAPIVersion.VERSION2,
                         client_id=gcid, protocol=mqtt.MQTTv5)
    client.tls_set()
    client.username_pw_set(gcid, tok["id_token"])
    client.reconnect_delay_set(min_delay=5, max_delay=120)
    client.on_connect, client.on_disconnect, client.on_message = on_connect, on_disconnect, on_message

    print(f"Streaming BMW pour {vin}")
    print("Paramètres utilisés (comparez-les à ceux du portail BMW CarData) :")
    print(f"    Hôte            {host}")
    print(f"    Port            {port}")
    print(f"    Nom d'utilisateur {gcid}")
    print(f"    Sujet           {topic}")
    print("    Mot de passe    jeton de connexion (id_token), renouvelé automatiquement\n")
    if url:
        print(f"Tableau de bord en direct : {url}  (se met à jour tout seul)")
        if "--open" in sys.argv:
            webbrowser.open(url)
    print("Laissez cette fenêtre ouverte. Ctrl+C pour arrêter.\n")

    try:
        client.connect(host, port, keepalive=30)
    except OSError as e:
        die(f"Impossible de joindre le serveur de streaming BMW : {e}")
    client.loop_start()

    try:
        while True:
            time.sleep(20)
            tok_age = time.time() - load_json(TOKENS_FILE, {}).get("obtained_at", 0)
            expiring = tok_age > int(tok.get("expires_in", 3600)) - 300
            if not (expiring or state["need_reconnect"]):
                continue
            # Le mot de passe MQTT (id_token) expire au bout d'une heure : on le renouvelle et on se reconnecte.
            try:
                tok = get_tokens(margin=300, force=True)
            except SystemExit:
                print("Renouvellement du jeton impossible pour l'instant, nouvel essai dans 1 minute…")
                time.sleep(60)
                continue
            state["need_reconnect"] = False
            print(f"[{datetime.now():%H:%M:%S}] Jeton renouvelé, reconnexion…")
            client.loop_stop()
            try:
                client.disconnect()
            except Exception:
                pass
            client.username_pw_set(gcid, tok["id_token"])
            try:
                client.connect(host, port, keepalive=30)
            except OSError as e:
                print(f"Reconnexion impossible ({e}), nouvel essai bientôt…")
                state["need_reconnect"] = True
            client.loop_start()
    finally:
        client.loop_stop()
        try:
            client.disconnect()
        except Exception:
            pass
        with _lock:
            render_dashboard(vin, telematic, fetched_at, now_iso(), live=False)
        print("\nStreaming arrêté. Le tableau de bord garde les dernières données reçues.")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    commands = {"login": login, "status": status, "stream": stream, "profile": profile,
                "show": show, "reset": reset}
    if cmd in commands:
        commands[cmd]()
    else:
        print(__doc__)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nAnnulé.")
