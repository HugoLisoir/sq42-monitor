#!/usr/bin/env python3
"""
SQ42 Monitor — Surveille squadron42.com / RSI et ne poste sur Discord QUE
lorsqu'un signal réellement significatif apparaît (pas les rebuilds techniques).

Signaux qui déclenchent un post :
  1. Le hash SHA-256 du CONTENU de SQ42_thumbnail.jpg change.
  2. Un nouveau namespace de contenu (./en/*.json) apparaît.
  3. La route /en/artemis passe de 404 à 200.
  4. Un composant au préfixe entièrement nouveau apparaît dans le bundle.
  5. Un nouvel identifiant Tycoon (tycoon_* / Ty*) apparaît — e-commerce interne.
  6. Une mention plateforme tierce (steam/epic/psn/xbox/sony/nintendo) apparaît
     HORS du bloc de détection d'appareil (ua-parser), qui est du bruit permanent.

Tracé silencieux (aucun Discord) : historique des builds, pour répondre a
posteriori à « combien de builds cette semaine » / « quel hash le jour J ».
Résumé via : python sq42_monitor.py --digest [jours]

Conçu pour GitHub Actions (single-run, cron). État dans sq42_state.json.
"""

import requests
import json
import re
import os
import sys
import hashlib
from datetime import datetime, timedelta

# ============================================================
# CONFIGURATION
# ============================================================
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
STATE_FILE = "sq42_state.json"
SCHEMA_VERSION = 3

THUMBNAIL_REFERENCE_SHA = "b1a9c8020d8ade73b5356ce8070f7e727d696fe05716cc7bd23168585b412edb"
NAMESPACE_BASELINE_COUNT = 36

ARTEMIS_ROUTE = "https://robertsspaceindustries.com/en/artemis"

# --- Tycoon (système e-commerce interne RSI) ---
TYCOON_PATTERN = r'tycoon_[a-zA-Z_]+|Ty[A-Z][a-zA-Z]+'

# --- Plateformes tierces ---
PLATFORM_KEYWORDS = ["steam", "epic", "playstation", "psn", "xbox", "sony", "nintendo"]

# Marqueurs de la librairie de device-detection (ua-parser) : bruit permanent.
# On délimite dynamiquement sa zone au lieu d'un rayon fixe, car elle fait ~35 Ko.
VENDOR_MARKERS = [
    r'\(ouya\)',
    r'\(nintendo\|playstation\)',
    r'NINTENDO:"Nintendo"',
    r'PLAYSTATION:"PlayStation"',
    r'XBOX:"Xbox"',
    r'="Sony"',
    r'lbbrowser\|luakit\|rekonq',
    r'droid\.\+; \(\(shield',
]
VENDOR_PAD = 3000          # marge autour des marqueurs extrêmes
PLATFORM_CTX = 150         # ±150 => 300 caractères de contexte dans l'alerte

BUILD_HISTORY_MAX = 500

# --- Messages (aucune mention @here / @everyone) ---
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
    """Relit toujours plt-client.es.js d'abord : le nom du build peut etre
    remplace sur le CDN avant l'aspiration (source des anciens 404)."""
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
    if not build:
        return None
    try:
        r = requests.get(
            f"https://static.squadron42.com/plt-client/assets/{build}",
            headers=HEADERS, timeout=25
        )
        print(f"  [bundle] HTTP {r.status_code} — {len(r.text)} chars")
        return r.text if r.status_code == 200 else None
    except Exception as e:
        print(f"  [bundle] Erreur: {e}")
        return None


def extract_namespaces(bundle):
    ns = set(re.findall(r'en/([a-z][a-zA-Z0-9]*)\.json', bundle))
    print(f"  [namespaces] {len(ns)} trouvés")
    return sorted(ns)


def extract_prefixes(bundle):
    chunks = re.findall(r'chunks/[A-Z][a-zA-Z]+-[a-zA-Z0-9_-]+\.js', bundle)
    names = {c.split("/")[-1].split("-", 1)[0] for c in chunks}
    prefixes = set()
    for name in names:
        m = re.match(r'[A-Z][a-z0-9]*', name)
        prefixes.add(m.group(0) if m else name)
    print(f"  [prefixes] {len(prefixes)} préfixes / {len(names)} composants")
    return sorted(prefixes)


def extract_tycoon(bundle):
    names = sorted(set(re.findall(TYCOON_PATTERN, bundle)))
    print(f"  [tycoon] {len(names)} identifiants")
    return names


def _vendor_zone(bundle):
    """Délimite dynamiquement la zone de la lib de device-detection (bruit)."""
    pos = []
    for pat in VENDOR_MARKERS:
        pos.extend(m.start() for m in re.finditer(pat, bundle))
    if not pos:
        print("  [platform] zone vendor introuvable — alertes suspendues ce run")
        return None
    zone = (min(pos) - VENDOR_PAD, max(pos) + VENDOR_PAD)
    print(f"  [platform] zone vendor {zone[0]}..{zone[1]} ({len(pos)} marqueurs)")
    return zone


def _fingerprint(keyword, context):
    """Empreinte stable malgré la minification : on ne garde que les mots de
    3+ lettres (les identifiants minifiés font 1-2 caractères)."""
    toks = [t.lower() for t in re.findall(r'[A-Za-z]{3,}', context)]
    h = hashlib.sha1(" ".join(toks).encode("utf-8")).hexdigest()[:16]
    return f"{keyword}:{h}"


def extract_platform(bundle):
    """Retourne (toutes_les_empreintes, hits_hors_zone_vendor).

    La garde (?![a-z]) évite les faux positifs de sous-chaîne (ex. 'epic' pris
    dans un mot plus long) tout en gardant les formes camelCase (steamAppId)."""
    zone = _vendor_zone(bundle)
    all_fps, outside = set(), []
    for kw in PLATFORM_KEYWORDS:
        pattern = re.compile(r'(?i:' + re.escape(kw) + r')(?![a-z])')
        for m in pattern.finditer(bundle):
            p = m.start()
            ctx = bundle[max(0, p - PLATFORM_CTX): p + PLATFORM_CTX]
            fp = _fingerprint(kw, bundle[max(0, p - 200): p + 200])
            all_fps.add(fp)
            in_vendor = zone is not None and zone[0] <= p <= zone[1]
            if zone is not None and not in_vendor:
                outside.append({"fp": fp, "keyword": kw, "context": ctx})
    print(f"  [platform] {len(all_fps)} empreintes, {len(outside)} hors zone vendor")
    return sorted(all_fps), outside


def get_thumbnail_sha256():
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
        print(f"DISCORD_WEBHOOK_URL non configuré — post ignoré:\n{message[:300]}")
        return
    try:
        requests.post(DISCORD_WEBHOOK_URL, json={"content": message[:1990]}, timeout=10)
    except Exception as e:
        print(f"Erreur Discord: {e}")


# ------------------------------------------------------------
# Logique
# ------------------------------------------------------------
def collect(previous):
    """Collecte l'état courant. Sur échec réseau, on reporte la valeur mémorisée
    pour ne pas corrompre la base (et donc éviter les faux positifs)."""
    build = get_build_name()
    bundle = get_bundle(build)

    if bundle is not None:
        main_sha = hashlib.sha256(bundle.encode("utf-8", "ignore")).hexdigest()
        namespaces = extract_namespaces(bundle)
        prefixes = extract_prefixes(bundle)
        tycoon = extract_tycoon(bundle)
        platform_fps, platform_outside = extract_platform(bundle)
    else:
        print("  [collect] Bundle indisponible — champs reportés depuis la mémoire.")
        main_sha = previous.get("main_sha256")
        namespaces = previous.get("namespaces", [])
        prefixes = previous.get("prefixes", [])
        tycoon = previous.get("tycoon", [])
        platform_fps = previous.get("platform_fingerprints", [])
        platform_outside = []

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
        "tycoon": tycoon,
        "platform_fingerprints": platform_fps,
        "checked_at": datetime.now().isoformat(),
        "_platform_outside": platform_outside,   # transitoire, non persisté
    }


def update_build_history(previous, current):
    """Trace silencieuse : une entrée par build DISTINCT (avec sa date de
    première apparition). Suffit pour compter les builds d'une période et
    retrouver le hash en ligne à une date donnée, sans commit à chaque scan."""
    hist = list(previous.get("build_history", []))
    build = current.get("build_name")
    if not build:
        return hist
    if not hist or hist[-1].get("build") != build:
        hist.append({
            "build": build,
            "sha256": current.get("main_sha256"),
            "first_seen": current["checked_at"],
        })
        print(f"  [history] nouveau build enregistré ({len(hist)} au total)")
    return hist[-BUILD_HISTORY_MAX:]


def detect_signals(previous, current):
    """Retourne la liste des messages à poster (vide = silence)."""
    signals = []

    # --- 1. thumbnail (contenu) ---
    base_thumb = previous.get("thumbnail_sha256") or THUMBNAIL_REFERENCE_SHA
    if current["thumbnail_sha256"] and current["thumbnail_sha256"] != base_thumb:
        signals.append(MSG_THUMBNAIL)

    # --- 2. nouveau namespace de contenu ---
    prev_ns = set(previous.get("namespaces", []))
    curr_ns = set(current["namespaces"])
    if prev_ns:
        added = curr_ns - prev_ns
        new_artemis = {n for n in added if n.lower().startswith("artemis")}
        if added and (len(curr_ns) > NAMESPACE_BASELINE_COUNT or new_artemis):
            signals.append(
                f"{MSG_NAMESPACE}\n`+ namespace : {', '.join(sorted(added))} "
                f"(total {len(curr_ns)})`"
            )

    # --- 3. route Artemis 404 -> 200 ---
    prev_status = previous.get("artemis_route_status")
    if current["artemis_route_status"] == 200 and prev_status not in (None, 200):
        signals.append(MSG_ARTEMIS_ROUTE)

    # --- 4. préfixe de composant entièrement nouveau ---
    prev_prefixes = set(previous.get("prefixes", []))
    if prev_prefixes:
        new_prefixes = set(current["prefixes"]) - prev_prefixes
        if new_prefixes:
            signals.append(
                f"{MSG_NEW_COMPONENT}\n`+ préfixe inédit : {', '.join(sorted(new_prefixes))}`"
            )

    # --- 5. nouvel identifiant Tycoon ---
    prev_ty = set(previous.get("tycoon", []))
    curr_ty = set(current["tycoon"])
    if prev_ty:
        new_ty = sorted(curr_ty - prev_ty)
        if new_ty:
            shown = ", ".join(f"`{n}`" for n in new_ty[:15])
            if len(new_ty) > 15:
                shown += f" … (+{len(new_ty) - 15})"
            signals.append(
                f"🛒 Nouveau champ Tycoon détecté : {shown} "
                f"(total : {len(prev_ty)} → {len(curr_ty)})"
            )

    # --- 6. mention plateforme tierce hors bloc device-detection ---
    prev_fps = set(previous.get("platform_fingerprints", []))
    if prev_fps:
        for hit in current.get("_platform_outside", []):
            if hit["fp"] in prev_fps:
                continue
            ctx = hit["context"].replace("```", "'''").replace("\n", " ")
            signals.append(
                f"🎮 Mention plateforme tierce **hors** du bloc de détection "
                f"d'appareil : `{hit['keyword']}`\n"
                f"Contexte pour analyse manuelle :\n```\n{ctx}\n```"
            )

    return signals


def check():
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Vérification…")
    previous = load_state()
    current = collect(previous)

    first_run = not previous
    signals = [] if first_run else detect_signals(previous, current)

    # historique de builds : silencieux, jamais de Discord
    current["build_history"] = update_build_history(previous, current)
    current.pop("_platform_outside", None)

    if first_run:
        send_discord(
            "✅ Surveillance Squadron 42 active — logique anti-bruit.\n"
            f"Référence : thumbnail `{(current['thumbnail_sha256'] or '?')[:12]}…`, "
            f"{len(current['namespaces'])} namespaces, "
            f"{len(current['tycoon'])} identifiants Tycoon, "
            f"route /en/artemis en `{current['artemis_route_status']}`."
        )
        print("Base initiale établie — silence.")
    elif signals:
        for msg in signals:
            send_discord(msg)
        print(f"SIGNAL(S) : {len(signals)} — post(s) envoyé(s).")
    else:
        print("  Aucun signal significatif — bruit technique ignoré, mémoire à jour.")

    save_state(current)


def post_digest(days=7):
    """Résumé d'activité de build sur une période (jamais automatique par scan)."""
    state = load_state()
    hist = state.get("build_history", [])
    cutoff = datetime.now() - timedelta(days=days)
    recent = []
    for h in hist:
        try:
            if datetime.fromisoformat(h["first_seen"]) >= cutoff:
                recent.append(h)
        except (ValueError, KeyError, TypeError):
            continue

    lines = [
        f"📊 **Activité de build Squadron 42** — {days} derniers jours",
        f"Builds distincts détectés : **{len(recent)}**",
    ]
    if recent:
        for h in recent[-12:]:
            when = str(h.get("first_seen", ""))[:16].replace("T", " ")
            lines.append(f"• `{h.get('build')}` — {when}")
        if len(recent) > 12:
            lines.append(f"… et {len(recent) - 12} autre(s)")
    else:
        lines.append("Aucun nouveau build sur la période.")
    lines.append(f"En ligne actuellement : `{state.get('build_name')}`")
    lines.append(f"_Historique total conservé : {len(hist)} build(s)._")

    print("\n".join(lines))
    send_discord("\n".join(lines))


if __name__ == "__main__":
    if "--digest" in sys.argv:
        idx = sys.argv.index("--digest")
        days = 7
        if len(sys.argv) > idx + 1:
            try:
                days = int(sys.argv[idx + 1])
            except ValueError:
                pass
        print(f"=== Digest build ({days} jours) ===")
        post_digest(days)
    else:
        print("=" * 50)
        print("SQ42 Monitor — détection de signaux significatifs")
        print(f"Webhook Discord: {'✅ Configuré' if DISCORD_WEBHOOK_URL else '❌ MANQUANT'}")
        print("=" * 50)
        check()
