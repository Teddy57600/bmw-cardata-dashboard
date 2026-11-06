"""Page de démonstration avec des données FICTIVES, pour les captures d'écran du README.

    python outils/captures_demo.py              # génère demo/demo.html
    python outils/captures_demo.py --captures   # et refait les captures de docs/captures/ (Microsoft Edge)

Aucune donnée personnelle : VIN factice, position publique (BMW Welt, Munich), historique inventé
sur 60 jours. Seule la photo du véhicule (voiture.png, si elle existe) est reprise telle quelle.
Le dossier demo/ est ignoré par Git.
"""
import json, random, shutil, subprocess, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
OUT = PROJ / "demo"
CAPTURES = PROJ / "docs" / "captures"
# onglet → hauteur de la capture (largeur 1440 px)
SHOTS = {"apercu": 845, "entretien": 1180, "conduite": 1100, "historique": 1960, "flux": 1250}
EDGE = [Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
        Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe")]

sys.path.insert(0, str(PROJ))
import bmw_cardata as b  # noqa: E402


def build_page():
    OUT.mkdir(exist_ok=True)
    for old in OUT.glob("voiture.*"):
        old.unlink()
    for photo in PROJ.glob("voiture.*"):
        shutil.copy(photo, OUT / photo.name)
    b.HERE = OUT
    b.PROFILE_FILE = OUT / "profil.json"
    b.STREAM_LOG = OUT / "historique.jsonl"
    b.API_LOG = OUT / "lectures.jsonl"
    b.PROFILE_FILE.write_text(json.dumps({
        "brand": "BMW", "modelName": "120d", "series": "1", "seriesDevt": "F70", "bodyType": "Sports Hatch",
        "numberOfDoors": 5, "driveTrain": "MHEV", "propulsionType": "DI", "engine": "B47L",
        "colourDescription": "SKYSCRAPER GRAU METALLIC", "constructionDate": "2025-06-01T00:00:00.000+0000",
        "hasNavi": True, "hasSunRoof": True, "saList": ",".join(f"0{i:03d}" for i in range(42)),
    }), encoding="utf-8")

    now = datetime.now(timezone.utc).replace(microsecond=0)
    iso = lambda d: d.strftime("%Y-%m-%dT%H:%M:%SZ")
    ts, old = iso(now - timedelta(minutes=2)), iso(now - timedelta(days=17))

    # ---- historique fictif sur 60 jours (toujours le même grâce à la graine) ----
    random.seed(7)
    t, end = now - timedelta(days=60), now - timedelta(minutes=2)
    odo, fuel, volt = 10370.0, 82.0, 12.55
    p = [252, 248, 232, 232]
    hist = []
    while True:
        t = min(t + timedelta(hours=random.uniform(6, 30)), end)
        km = random.choice([0, 0, 14, 22, 35, 48, 90, 180])
        odo += km
        fuel -= km * 0.075
        if fuel < 14:
            fuel = random.choice([92, 96, 100])
        volt = min(12.9, max(12.35, volt + random.uniform(-.05, .05) + (.02 if km > 40 else -.015)))
        if random.random() < .10: p[0] -= 1
        if random.random() < .16: p[1] -= 1
        if random.random() < .08: p[2] -= 1
        if random.random() < .30: p[3] -= 1  # l'arrière droit perd doucement de la pression
        hist.append((iso(t), round(odo), round(fuel), round(fuel * 9.6), round(volt, 2), list(p)))
        if t == end:
            break
    shift = 12480 - hist[-1][1]  # le compteur finit à 12 480 km
    hist = [(h[0], h[1] + shift, *h[2:]) for h in hist]
    last = hist[-1]

    def e(v, u=None, t=ts):
        return {"value": v, "unit": u, "timestamp": t}

    cbs = [{"date": "2027-09", "status": "OK", "title": "Engine oil", "unitOfLengthRemaining": "11000"},
           {"date": "2028-06", "status": "OK", "title": "Brake fluid", "unitOfLengthRemaining": "-"},
           {"date": "2029-06", "status": "OK", "title": "Vehicle check", "unitOfLengthRemaining": "37000"},
           {"date": "2027-03", "status": "PENDING", "title": "Microfilter", "unitOfLengthRemaining": "-"}]
    T = {
        "vehicle.vehicle.travelledDistance": e(str(last[1]), "km"),
        "vehicle.drivetrain.fuelSystem.level": e(str(last[2]), "%"),
        "vehicle.drivetrain.lastRemainingRange": e(str(last[3]), "km"),
        "vehicle.status.serviceDistance.next": e("11000", None, old),
        "vehicle.status.conditionBasedServices": e(json.dumps(cbs)),
        "vehicle.status.conditionBasedServicesCount": e("6"),
        "vehicle.status.checkControlMessages": e("[]"),
        "vehicle.drivetrain.fuelSystem.consumptionOverLifeTime.overall.fuel": e("611", "l"),
        "vehicle.drivetrain.fuelSystem.consumptionOverLifeTime.overall.referenceDistance": e("12400", "km"),
        "vehicle.cabin.door.lock.status": e("SECURED"),
        "vehicle.vehicle.antiTheftAlarmSystem.alarm.armStatus": e("doorsTiltCabin"),
        "vehicle.vehicle.antiTheftAlarmSystem.alarm.isOn": e("false"),
        "vehicle.body.hood.isOpen": e("false"), "vehicle.body.trunk.isOpen": e("CLOSED"),
        "vehicle.cabin.sunroof.overallStatus": e("CLOSED"),
        "vehicle.electricalSystem.battery.voltage": e(str(last[4]), "V"),
        "vehicle.electricalSystem.battery.serviceDemand.replace": e("200"),
        "vehicle.electricalSystem.battery.serviceDemand.recharge": e("0"),
        "vehicle.vehicle.averageWeeklyDistanceShortTerm": e("310"),
        "vehicle.cabin.infotainment.navigation.currentLocation.heading": e("212", "degrees"),
        "vehicle.cabin.infotainment.navigation.currentLocation.altitude": e("519", "m"),
        # position fictive : un lieu public (BMW Welt, Munich)
        "vehicle.cabin.infotainment.navigation.currentLocation.latitude": e("48.17680", "degrees"),
        "vehicle.cabin.infotainment.navigation.currentLocation.longitude": e("11.55600", "degrees"),
        "vehicle.cabin.climate.timers.weekdaysTimer1.action": e("ACTIVATE"),
        "vehicle.cabin.climate.timers.weekdaysTimer1.hour": e("7"),
        "vehicle.cabin.climate.timers.weekdaysTimer1.minute": e("30"),
        "vehicle.cabin.climate.timers.weekdaysTimer2.action": e("deactive"),
    }
    wheels = ["row1.wheel.left", "row1.wheel.right", "row2.wheel.left", "row2.wheel.right"]
    for i, w in enumerate(wheels):
        T[f"vehicle.chassis.axle.{w}.tire.pressure"] = e(str(last[5][i]), "kPa")
        T[f"vehicle.chassis.axle.{w}.tire.pressureTarget"] = e("250" if w.startswith("row1") else "230", "kPa")
    for d in ("row1.driver", "row1.passenger", "row2.driver", "row2.passenger"):
        T[f"vehicle.cabin.door.{d}.isOpen"] = e("CLOSED")
        T[f"vehicle.cabin.window.{d}.status"] = e("CLOSED")

    with b.API_LOG.open("w", encoding="utf-8") as f:  # relevés passés (graphiques)
        for h in hist[:-1]:
            d = {"vehicle.vehicle.travelledDistance": e(h[1], "km", h[0]),
                 "vehicle.drivetrain.fuelSystem.level": e(h[2], "percent", h[0]),
                 "vehicle.drivetrain.lastRemainingRange": e(h[3], "km", h[0]),
                 "vehicle.electricalSystem.battery.voltage": e(h[4], "V", h[0])}
            for i, w in enumerate(wheels):
                d[f"vehicle.chassis.axle.{w}.tire.pressure"] = e(h[5][i], "kpa", h[0])
            f.write(json.dumps({"received": h[0], "source": "api", "data": d}) + "\n")

    with b.STREAM_LOG.open("w", encoding="utf-8") as f:  # derniers messages (onglet Flux)
        r1 = now - timedelta(minutes=21)
        for i, (k, v) in enumerate(list(T.items())[:24]):
            f.write(json.dumps({"received": (r1 + timedelta(seconds=i // 3)).isoformat(), "data": {k: v}}) + "\n")
        extra = {"vehicle.cabin.infotainment.isMobilePhoneConnected": e("true"),
                 "vehicle.vehicle.avgSpeed": e("47.3", "km/h"),
                 "vehicle.cabin.hvac.statusAirPurification": e("INACTIVE")}
        for k, v in extra.items():
            f.write(json.dumps({"received": (now - timedelta(minutes=2)).isoformat(), "data": {k: v}}) + "\n")

    page = OUT / "demo.html"
    b.render_dashboard("WBA00000000DEMO00", T, now.astimezone().strftime("%d/%m/%Y %H:%M"), iso(now), path=page)
    return page


def capture(page):
    edge = next((p for p in EDGE if p.exists()), None)
    if not edge:
        sys.exit("Microsoft Edge introuvable : ouvrez demo/demo.html et faites les captures à la main.")
    CAPTURES.mkdir(parents=True, exist_ok=True)
    for tab, height in SHOTS.items():
        # profil Edge dédié : sans lui, Edge déjà ouvert ignore la demande et aucun fichier n'est écrit
        subprocess.run([str(edge), "--headless=new", "--disable-gpu", "--hide-scrollbars",
                        "--force-device-scale-factor=1", f"--window-size=1440,{height}",
                        "--virtual-time-budget=5000", f"--user-data-dir={OUT / 'edge-profil'}",
                        f"--screenshot={CAPTURES / (tab + '.png')}", f"{page.as_uri()}?still#{tab}"],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print("Capture :", (CAPTURES / f"{tab}.png").relative_to(PROJ))
    print("Capture mobile (mobile.jpg, 750 × 1624) : à faire à la main dans un navigateur à 375 px de large,\n"
          "Edge en mode headless ne descend pas sous ~500 px.")


if __name__ == "__main__":
    page = build_page()
    print("Page de démonstration :", page.relative_to(PROJ))
    if "--captures" in sys.argv:
        capture(page)
