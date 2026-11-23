"""
Alertes cotes boostées : Winamax, Betclic, Unibet.

Ouvre les pages de cotes boostées dans un vrai navigateur (Chromium),
repère les boosts, garde ceux dont la mise max est <= MISE_MAX,
et envoie une notification ntfy pour chaque nouveau boost.

Réglages : variables ci-dessous. Secret requis : NTFY_TOPIC.
"""

import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

# ------------------------------------------------------------------ réglages
MISE_MAX = 10.0              # on garde les boosts dont la mise max est <= 10 €
GARDER_SI_MISE_INCONNUE = True  # si le site n'affiche pas la mise max : envoyer quand même (signalé)
NTFY_SERVER = "https://ntfy.sh"
MEMOIRE = Path("seen.json")
OUBLI_APRES_JOURS = 3

BOOKMAKERS = {
    "Winamax": {
        "lien": "https://www.winamax.fr/paris-sportifs/sports/100000",
        # Winamax n'affiche pas la mise max sur la page : les boosts à 10 € max
        # sont ceux marqués « Grosse cote boostée ». On ne garde que ceux-là.
        "etiquette": r"grosse\s*cote",
        "mise_etiquette": 10.0,
        "seulement_etiquette": True,
        "pages": [
            ("https://www.winamax.fr/paris-sportifs/sports/100000", True),
        ],
    },
    "Betclic": {
        "lien": "https://www.betclic.fr/",
        "pages": [
            ("https://www.betclic.fr/", False),
        ],
    },
    "Unibet": {
        "lien": "https://www.unibet.fr/sport/cotes-boostees",
        "pages": [
            ("https://www.unibet.fr/sport/cotes-boostees", True),
        ],
    },
}
# (url, True) = page dédiée aux boosts ; (url, False) = page générale,
# où l'on ne garde que ce qui est marqué « boost » à proximité.

# ------------------------------------------------------------------ analyse du texte
COTE = re.compile(r"^\s*(\d{1,3}[.,]\d{1,2})\s*$")
MISE = re.compile(r"mise\s*(?:max(?:imale|imum)?|plafonn[ée]e)\D{0,15}?(\d{1,4}(?:[.,]\d{1,2})?)\s*€", re.I)
MOT_BOOST = re.compile(r"boost|super\s*cote|cote\s*major", re.I)
IGNORER = re.compile(r"^[\s→➜>»\-–—|·•]*$")
BLOCAGE = re.compile(
    r"access denied|just a moment|attention required|pas disponible dans votre pays|"
    r"not available in your (country|region)|request blocked|forbidden|captcha", re.I)


def nombre(txt):
    return float(txt.replace(",", "."))


def trouver_boosts(texte, page_boost):
    """Repère les boosts dans le texte visible d'une page.

    Un boost = une cote (souvent précédée de l'ancienne cote, plus basse),
    avec juste au-dessus la description du pari.
    """
    lignes = [l.strip() for l in texte.splitlines()]
    lignes = [l for l in lignes if l and not IGNORER.match(l)]
    boosts = []
    i = 0
    fin_prec = 0  # fin du groupe de cotes précédent = début de la « carte » actuelle
    while i < len(lignes):
        if not COTE.match(lignes[i]):
            i += 1
            continue
        # groupe de cotes consécutives
        debut = i
        cotes = []
        while i < len(lignes) and COTE.match(lignes[i]):
            cotes.append(nombre(COTE.match(lignes[i]).group(1)))
            i += 1
        haut_carte, fin_prec = fin_prec, i
        fenetre = " ".join(lignes[max(0, debut - 8): min(len(lignes), i + 6)])

        if len(cotes) == 2 and cotes[0] < cotes[1] <= cotes[0] * 12:
            ancienne, cote = cotes
        elif len(cotes) == 1 and page_boost:
            ancienne, cote = None, cotes[0]
        else:
            continue  # cotes 1N2 classiques, etc.
        if cote < 1.01:
            continue
        if not page_boost and not MOT_BOOST.search(fenetre):
            continue

        # description = jusqu'à 3 lignes au-dessus de la cote
        desc, mise_avant = [], None
        j = debut - 1
        while j >= 0 and len(desc) < 3 and not COTE.match(lignes[j]):
            l = lignes[j]
            m = MISE.search(l)
            if m:
                if mise_avant is not None:
                    break  # 2e « mise max » : on est remonté au boost précédent
                mise_avant = nombre(m.group(1))
            elif len(l) >= 3 and not (MOT_BOOST.search(l) and len(l) < 25):
                desc.insert(0, l)
            j -= 1
        if not desc:
            continue

        # mise max : d'abord juste après la cote (jusqu'à la cote suivante),
        # sinon au-dessus, dans la description
        apres = []
        for l in lignes[i:i + 4]:
            if COTE.match(l):
                break
            apres.append(l)
        m = MISE.search(" ".join(apres))
        mise = nombre(m.group(1)) if m else mise_avant
        carte = " ".join(lignes[max(haut_carte, debut - 12):debut])
        boosts.append({
            "carte": carte,
            "description": " · ".join(desc)[:200],
            "ancienne": ancienne,
            "cote": cote,
            "mise_max": mise,
        })
    return boosts


# ------------------------------------------------------------------ navigateur
def lire_page(navigateur, url):
    ctx = navigateur.new_context(
        locale="fr-FR",
        timezone_id="Europe/Paris",
        viewport={"width": 1280, "height": 2000},
    )
    page = ctx.new_page()
    statut, texte, erreur = None, "", None
    try:
        rep = page.goto(url, wait_until="domcontentloaded", timeout=45000)
        statut = rep.status if rep else None
        try:
            page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:
            pass
        for _ in range(4):  # fait défiler pour charger toute la liste
            page.mouse.wheel(0, 1500)
            page.wait_for_timeout(1200)
        texte = page.inner_text("body")
    except Exception as e:
        erreur = str(e).splitlines()[0][:200]
    finally:
        ctx.close()
    return statut, texte, erreur


# ------------------------------------------------------------------ mémoire + notif
def charger_memoire():
    if MEMOIRE.exists():
        try:
            return json.loads(MEMOIRE.read_text())
        except Exception:
            pass
    return {}


def sauver_memoire(vus):
    limite = time.time() - OUBLI_APRES_JOURS * 86400
    vus = {k: t for k, t in vus.items() if t > limite}
    MEMOIRE.write_text(json.dumps(vus, indent=1, sort_keys=True))


def cle(bk, b):
    desc = re.sub(r"\s+", " ", b["description"].lower())
    brut = f"{bk}|{desc}|{b['cote']}"
    return hashlib.sha1(brut.encode()).hexdigest()[:16]


def notifier(topic, bk, b, lien):
    import urllib.request
    cote = f"{b['cote']:.2f}"
    ligne_cote = f"{b['ancienne']:.2f} → {cote}" if b["ancienne"] else cote
    if b["mise_max"] is not None:
        ligne_mise = f"mise max {b['mise_max']:g} €"
    else:
        ligne_mise = "mise max non affichée, vérifie avant de miser"
    corps = {
        "topic": topic,
        "title": f"{bk} · cote boostée {cote}",
        "message": f"{b['description']}\n{ligne_cote} · {ligne_mise}",
        "click": lien,
        "tags": ["rocket"],
        "priority": 4,
        "actions": [{"action": "view", "label": f"Ouvrir {bk}", "url": lien}],
    }
    req = urllib.request.Request(
        NTFY_SERVER, data=json.dumps(corps).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    urllib.request.urlopen(req, timeout=15).read()


# ------------------------------------------------------------------ main
def main():
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    test = "--test" in sys.argv
    if not topic and not test:
        print("Secret NTFY_TOPIC manquant (étape 4 du guide).")
        sys.exit(1)

    vus = charger_memoire()
    resume = ["# Vérification des cotes boostées", ""]
    nouveaux = 0

    with sync_playwright() as p:
        nav = p.chromium.launch()
        for bk, conf in BOOKMAKERS.items():
            trouves = {}
            details = []
            for url, page_boost in conf["pages"]:
                statut, texte, erreur = lire_page(nav, url)
                bloque = bool((statut and statut >= 400) or BLOCAGE.search(texte[:3000]))
                boosts = trouver_boosts(texte, page_boost) if texte else []
                for b in boosts:
                    trouves.setdefault(cle(bk, b), b)
                etat = erreur or ("BLOQUÉ / erreur" if bloque else "ok")
                details.append(f"- `{url}` : HTTP {statut}, {etat}, {len(boosts)} boost(s) repéré(s)")
                if not boosts:
                    extrait = texte[:1500].replace("```", "'''")
                    details.append(f"<details><summary>Texte lu sur la page</summary>\n\n```\n{extrait}\n```\n</details>")

            gardes, ecartes = [], []
            etiq = re.compile(conf["etiquette"], re.I) if conf.get("etiquette") else None
            for k, b in trouves.items():
                if etiq:
                    if etiq.search(b["carte"]):
                        b["mise_max"] = b["mise_max"] or conf.get("mise_etiquette")
                    elif conf.get("seulement_etiquette"):
                        ecartes.append((b, "pas une « grosse cote boostée »"))
                        continue
                if b["mise_max"] is None and not GARDER_SI_MISE_INCONNUE:
                    ecartes.append((b, "mise max inconnue"))
                    continue
                if b["mise_max"] is not None and b["mise_max"] > MISE_MAX:
                    ecartes.append((b, f"mise max {b['mise_max']:g} €"))
                    continue
                gardes.append((k, b))

            resume.append(f"## {bk} : {len(trouves)} boost(s) trouvé(s), {len(gardes)} gardé(s)")
            resume.extend(details)
            for b, raison in ecartes:
                resume.append(f"  - ❌ {b['description']} : {b['cote']} (écarté : {raison})")
            if etiq and trouves and not any(etiq.search(b["carte"]) for b in trouves.values()):
                exemple = next(iter(trouves.values()))["carte"][:600].replace("```", "'''")
                resume.append(f"<details><summary>Exemple de texte autour d'un boost</summary>\n\n```\n{exemple}\n```\n</details>")
            for k, b in gardes:
                neuf = k not in vus
                resume.append(f"  - ✅ {'🆕 ' if neuf else ''}{b['description']} : {b['ancienne'] or '?'} → {b['cote']} (mise max {b['mise_max'] if b['mise_max'] is not None else '?'})")
                if neuf and not test:
                    try:
                        notifier(topic, bk, b, conf["lien"])
                        vus[k] = time.time()
                        nouveaux += 1
                    except Exception as e:
                        resume.append(f"    ⚠️ notification échouée : {e}")
            resume.append("")
        nav.close()

    resume.append(f"**{nouveaux} nouvelle(s) notification(s) envoyée(s).**")
    texte_resume = "\n".join(resume)
    print(texte_resume)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write(texte_resume + "\n")
    if not test:
        sauver_memoire(vus)


if __name__ == "__main__":
    main()
