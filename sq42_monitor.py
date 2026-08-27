#!/usr/bin/env python3
"""
SQ42 Monitor — Surveille squadron42.com / RSI et ne poste sur Discord QUE
lorsqu'un signal réellement significatif apparaît (pas les rebuilds techniques).

Signaux qui déclenchent un post (sinon : silence, mémoire mise à jour) :
  1. Le hash SHA-256 du CONTENU de SQ42_thumbnail.jpg change.
  2. Un nouveau namespace de contenu (./en/*.json) apparaît (total > 36 ou nouveau artemis*).
  3. La route https://robertsspaceindustries.com/en/artemis passe de 404 à 200.
  4. Un composant au préfixe entièrement nouveau apparaît dans le bundle.

Bruit ignoré silencieusement : nom de main-XXXX.js qui change à contenu identique,
version RSI qui monte, thumbnail ré-uploadée à contenu identique, rehash de chunks.

Conçu pour tourner via GitHub Actions (single-run, cron). État mémorisé dans
sq42_state.json (committé par le workflow).
"""

import requests
import json
import re
import os
import hashlib
from datetime import datetime

# ============================================================
# CONFIGURATION
# ============================================================
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
STATE_FILE = "sq42_state.json"
SCHEMA_VERSION = 2

# Référence de contenu connue de la thumbnail (sert de base si mémoire absente)
THUMBNAIL_REFERENCE_SHA = "b1a9c8020d8ade73b5356ce8070f7e727d696fe05716cc7bd23168585b412edb"
NAMESPACE_BASELINE_COUNT = 36  # nombre de namespaces ./en/*.json de référence

ARTEMIS_ROUTE = "https://robertsspaceindustries.com/en/artemis"

# Messages-types (aucune mention @here / @everyone)
MSG_THUMBNAIL = (
    "🛰️ L'image de partage officielle de Squadron 42 vient de changer pour la "
    "première fois depuis des mois. C'est souvent le genre de détail qui précède "
    "une annonce. On surveille."
)
MSG_NAMESPACE = (
    "📄 Du nouveau contenu vient d'apparaître dans le code du site Squadron 42 "
    "(un élément qui n'existait pas avant). L'infrastructure de la page bouge. "
    "À suivre de près."
)
MSG_ARTEMIS_ROUTE = (
    "🚨 La page \"Artemis\" du site — restée inaccessible (erreur 404) depuis des "
    "mois — vient de répondre pour la première fois. C'est l'interrupteur qu'on "
    "attendait. Quelque chose se prépare, maintenant."
)
MSG_NEW_COMPONENT = (
    "🔧 Un nouvel élément inconnu vient d'être ajouté à la structure du site "
    "Squadron 42. Pas encore de contenu visible, mais c'est un mouvement "
    "inhabituel. On garde un œil dessus."
)
# ============================================================

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}


# ------------------------------------------------------------
# Collecte
# ------------------------------------------------------------
def get_build_name():
    """Relit toujours plt-client.es.js d'abord pour connaître le build réellement en ligne."""
    try:
        r = requests.get(
            "https://static.squadron42.com/plt-client/plt-client.es.js",
            headers=HEADERS, timeout=10
        )
        match = re.search(r'main-[a-zA-Z0-9_-]+\.js', r.text)
        if not match:
            print(f"  [build] Regex introuvable. Contenu: {r.text[:200]!r}")
            return None
        print(f"  [build] {match.group(0)}")
        return match.group(0)
    except Exception as e:
        print(f"  [build] Erreur: {e}")
        return None


def get_bundle(build):
    """Télécharge le bundle applicatif principal (main-XXXX.js)."""
    if not build:
        return None
    try:
        r = requests.get(
            f"https://static.squadron42.com/plt-client/assets/{build}",
            headers=HEADERS, timeout=20
        )
        print(f"  [bundle] HTTP {r.status_code} — {len(r.text)} chars")
        return r.text if r.status_code == 200 else None
    except Exception as e:
        print(f"  [bundle] Erreur: {e}")
        return None


def extract_namespaces(bundle):
    """Namespaces de contenu ./en/*.json (camelCase minuscule uniquement)."""
    ns = set(re.findall(r'en/([a-z][a-zA-Z0-9]*)\.json', bundle))
    print(f"  [namespaces] {len(ns)} trouvés")
    return sorted(ns)


def extract_prefixes(bundle):
    """Préfixe (premier mot CamelCase) de chaque composant chunk."""
    chunks = re.findall(r'chunks/[A-Z][a-zA-Z]+-[a-zA-Z0-9_-]+\.js', bundle)
    names = {c.split("/")[-1].split("-", 1)[0] for c in chunks}
    prefixes = set()
    for name in names:
        m = re.match(r'[A-Z][a-z0-9]*', name)
        prefixes.add(m.group(0) if m else name)
    print(f"  [prefixes] {len(prefixes)} préfixes / {len(names)} composants")
    return sorted(prefixes)


def get_thumbnail_sha256():
    """Hash SHA-256 du CONTENU de la thumbnail (pas sa date ni son nom)."""
    try:
        r = requests.get(
            "https://cdn.robertsspaceindustries.com/static/images/SQ42_thumbnail.jpg",
            headers=HEADERS, timeout=10
        )
        if r.status_code != 200 or not r.content:
            return None
        h = hashlib.sha256(r.content).hexdigest()
        print(f"  [thumbnail] {h[:16]}…")
        return h
    except Exception as e:
        print(f"  [thumbnail] Erreur: {e}")
        return None


def get_artemis_route_status():
    """Statut HTTP de la route Artemis (404 aujourd'hui, on guette le 200)."""
    try:
        r = requests.get(ARTEMIS_ROUTE, headers=HEADERS, timeout=15, allow_redirects=True)
        print(f"  [route artemis] HTTP {r.status_code}")
        return r.status_code
    except Exception as e:
        print(f"  [route artemis] Erreur: {e}")
        return None


# ------------------------------------------------------------
# État
# ------------------------------------------------------------
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)


def send_discord(message):
    """Poste un simple message texte (aucune mention)."""
    if not DISCORD_WEBHOOK_URL:
        print("DISCORD_WEBHOOK_URL non configuré — post ignoré.")
        return
    try:
        requests.post(DISCORD_WEBHOOK_URL, json={"content": message}, timeout=10)
    except Exception as e:
        print(f"Erreur Discord: {e}")


# ------------------------------------------------------------
# Logique principale
# ------------------------------------------------------------
def collect(previous):
    """Collecte l'état courant. En cas d'échec réseau sur un champ, on reporte
    la valeur précédente pour ne pas corrompre la base (évite les faux positifs)."""
    build = get_build_name()
    bundle = get_bundle(build)

    if bundle is not None:
        main_sha = hashlib.sha256(bundle.encode("utf-8", "ignore")).hexdigest()
        namespaces = extract_namespaces(bundle)
        prefixes = extract_prefixes(bundle)
    else:
        print("  [collect] Bundle indisponible — champs bundle reportés depuis la mémoire.")
        main_sha = previous.get("main_sha256")
        namespaces = previous.get("namespaces", [])
        prefixes = previous.get("prefixes", [])

    thumb = get_thumbnail_sha256() or previous.get("thumbnail_sha256")
    artemis_status = get_artemis_route_status()
    if artemis_status is None:
        artemis_status = previous.get("artemis_route_status")

    return {
        "schema": SCHEMA_VERSION,
        "build_name": build or previous.get("build_name"),
        "main_sha256": main_sha,
        "thumbnail_sha256": thumb,
        "namespaces": namespaces,
        "prefixes": prefixes,
        "artemis_route_status": artemis_status,
        "checked_at": datetime.now().isoformat(),
    }


def detect_signals(previous, current):
    """Retourne la liste des messages à poster (vide = silence)."""
    signals = []

    # --- Cas 1 : thumbnail (contenu) ---
    base_thumb = previous.get("thumbnail_sha256") or THUMBNAIL_REFERENCE_SHA
    if current["thumbnail_sha256"] and current["thumbnail_sha256"] != base_thumb:
        signals.append(MSG_THUMBNAIL)

    # --- Cas 2 : nouveau namespace de contenu ---
    # (garde-fou : ne compare que si une base non vide existe)
    prev_ns = set(previous.get("namespaces", []))
    curr_ns = set(current["namespaces"])
    if prev_ns:
        added_ns = curr_ns - prev_ns
        new_artemis = {n for n in added_ns if n.lower().startswith("artemis")}
        if added_ns and (len(curr_ns) > NAMESPACE_BASELINE_COUNT or new_artemis):
            detail = ", ".join(sorted(added_ns))
            signals.append(f"{MSG_NAMESPACE}\n`+ namespace : {detail} (total {len(curr_ns)})`")

    # --- Cas 3 : route Artemis 404 → 200 ---
    # (ne déclenche que depuis un statut de base réel non-200, ex. 404)
    prev_status = previous.get("artemis_route_status")
    if current["artemis_route_status"] == 200 and prev_status not in (None, 200):
        signals.append(MSG_ARTEMIS_ROUTE)

    # --- Cas 4 : préfixe de composant entièrement nouveau ---
    prev_prefixes = set(previous.get("prefixes", []))
    if prev_prefixes:
        new_prefixes = set(current["prefixes"]) - prev_prefixes
        if new_prefixes:
            detail = ", ".join(sorted(new_prefixes))
            signals.append(f"{MSG_NEW_COMPONENT}\n`+ préfixe inédit : {detail}`")

    return signals


def check():
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Vérification…")
    previous = load_state()
    current = collect(previous)

    # Migration / premier run : établir la base en silence (une seule confirmation).
    if previous.get("schema") != SCHEMA_VERSION:
        save_state(current)
        send_discord(
            "✅ Surveillance Squadron 42 mise à jour — nouvelle logique anti-bruit active.\n"
            f"Référence : thumbnail `{(current['thumbnail_sha256'] or '?')[:12]}…`, "
            f"{len(current['namespaces'])} namespaces de contenu, "
            f"route /en/artemis en `{current['artemis_route_status']}`.\n"
            "Le bot ne postera désormais que pour un vrai signal."
        )
        print("Base v2 établie — silence.")
        return

    signals = detect_signals(previous, current)

    if signals:
        for msg in signals:
            send_discord(msg)
        print(f"SIGNAL(S) détecté(s) : {len(signals)} — post(s) envoyé(s).")
    else:
        print("  Aucun signal significatif — bruit technique ignoré, mémoire à jour.")

    # On met toujours la base à jour (un signal ne se re-poste pas à chaque run).
    save_state(current)


if __name__ == "__main__":
    print("=" * 50)
    print("SQ42 Monitor — détection de signaux significatifs")
    print(f"Webhook Discord: {'✅ Configuré' if DISCORD_WEBHOOK_URL else '❌ MANQUANT'}")
    print("=" * 50)
    check()
