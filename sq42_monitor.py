#!/usr/bin/env python3
"""
SQ42 Monitor — Surveille squadron42.com et envoie un résumé sur Discord
Conçu pour tourner via GitHub Actions (single-run, cron toutes les 5 min)
"""

import requests
import json
import re
import os
import time
from datetime import datetime

# ============================================================
# CONFIGURATION
# ============================================================
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
STATE_FILE = "sq42_state.json"
# ============================================================

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}


def get_build_hash():
    try:
        r = requests.get(
            "https://static.squadron42.com/plt-client/plt-client.es.js",
            headers=HEADERS, timeout=10
        )
        print(f"  [build] HTTP {r.status_code} — {len(r.text)} chars")
        match = re.search(r'main-[a-zA-Z0-9_-]+\.js', r.text)
        if not match:
            print(f"  [build] Regex introuvable. Début du contenu: {r.text[:200]!r}")
        return match.group(0) if match else None
    except Exception as e:
        print(f"  [build] Erreur: {e}")
        return None


def get_navigation():
    try:
        r = requests.get(
            "https://static.robertsspaceindustries.com/nav/en/sq42.mrcwfzg488ep3-1772467486.json",
            headers=HEADERS, timeout=10
        )
        data = r.json()
        return {
            "root": data.get("root"),
            "button": data.get("tools", {}).get("enlist-now-link", {}).get("title"),
            "children": data.get("nodes", {}).get("squadron-42-game-page-simple", {}).get("children", [])
        }
    except:
        return None


def get_thumbnail_date():
    try:
        r = requests.head(
            "https://cdn.robertsspaceindustries.com/static/images/SQ42_thumbnail.jpg",
            headers=HEADERS, timeout=10
        )
        return r.headers.get("last-modified")
    except:
        return None


def get_page_date():
    try:
        r = requests.head(
            "https://squadron42.com/en/",
            headers=HEADERS, timeout=10
        )
        return r.headers.get("last-modified")
    except:
        return None


def get_chunks(build=None):
    try:
        if not build:
            build = get_build_hash()
        if not build:
            print("  [chunks] Pas de build hash, chunks ignorés.")
            return set()
        r = requests.get(
            f"https://static.squadron42.com/plt-client/assets/{build}",
            headers=HEADERS, timeout=15
        )
        print(f"  [chunks] HTTP {r.status_code} — {len(r.text)} chars")
        chunks = re.findall(r'chunks/[A-Z][a-zA-Z]+-[a-zA-Z0-9_-]+\.js', r.text)
        print(f"  [chunks] {len(chunks)} chunks trouvés")
        return set(chunks)
    except Exception as e:
        print(f"  [chunks] Erreur: {e}")
        return set()


def chunk_component_name(chunk_path):
    """'chunks/ArtemisFeatures-xgBWSBhY.js' → 'ArtemisFeatures' (clé de comparaison sans hash)"""
    name = chunk_path.split("/")[-1]
    return name.rsplit("-", 1)[0]


def format_chunk_name(chunk_path):
    """'chunks/ArtemisFeatures-xgBWSBhY.js' → 'Artemis Features'"""
    name = chunk_component_name(chunk_path)
    name = re.sub(r'([A-Z])', r' \1', name).strip()
    return name


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {}


def save_state(state):
    # Ne persiste que les champs utiles à la comparaison (pas checked_at)
    persistent = {k: v for k, v in state.items() if k != "checked_at"}
    with open(STATE_FILE, "w") as f:
        json.dump(persistent, f, indent=2, sort_keys=True)


def _split_text(text, max_len=4000):
    lines = text.split("\n")
    parts = []
    current = []
    current_len = 0
    for line in lines:
        if current_len + len(line) + 1 > max_len and current:
            parts.append("\n".join(current))
            current = []
            current_len = 0
        current.append(line)
        current_len += len(line) + 1
    if current:
        parts.append("\n".join(current))
    return parts


def send_discord(title, description, color=0x00ff00, urgent=False):
    if not DISCORD_WEBHOOK_URL:
        print("DISCORD_WEBHOOK_URL non configuré — notification ignorée.")
        return

    content = "@everyone 🚨 CHANGEMENT DÉTECTÉ SUR SQUADRON42.COM 🚨" if urgent else ""
    parts = _split_text(description) if len(description) > 4000 else [description]

    for i, part in enumerate(parts):
        payload = {
            "content": content if i == 0 else "",
            "embeds": [{
                "title": title if i == 0 else f"{title} (suite {i + 1})",
                "description": part,
                "color": color,
                "footer": {"text": f"sq42-monitor • {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}"}
            }]
        }
        try:
            requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
        except Exception as e:
            print(f"Erreur Discord: {e}")
        if i < len(parts) - 1:
            time.sleep(0.5)


def check_and_compare():
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Vérification en cours...")

    build = get_build_hash()
    current = {
        "build": build,
        "navigation": get_navigation(),
        "thumbnail_date": get_thumbnail_date(),
        "page_date": get_page_date(),
        "chunks": sorted(get_chunks(build)),
        "checked_at": datetime.now().isoformat()
    }

    previous = load_state()

    if not previous:
        save_state(current)
        nav = current["navigation"] or {}
        send_discord(
            "🟢 SQ42 Monitor démarré",
            f"**Build:** `{current['build']}`\n"
            f"**Navigation root:** `{nav.get('root', 'N/A')}`\n"
            f"**Bouton:** `{nav.get('button', 'N/A')}`\n"
            f"**Thumbnail:** {current['thumbnail_date']}\n"
            f"**Chunks:** {len(current['chunks'])} composants surveillés",
            color=0x3498db
        )
        print("Premier état sauvegardé — monitoring actif !")
        return

    changes = []
    urgent = False

    if current["build"] != previous.get("build"):
        changes.append(
            f"🔨 **Nouveau build détecté !**\n"
            f"  Avant: `{previous.get('build')}`\n"
            f"  Après: `{current['build']}`"
        )

    prev_nav = previous.get("navigation") or {}
    curr_nav = current["navigation"] or {}

    if curr_nav.get("root") != prev_nav.get("root"):
        urgent = True
        changes.append(
            f"🚨 **ROOT DE NAVIGATION CHANGÉ !**\n"
            f"  Avant: `{prev_nav.get('root')}`\n"
            f"  Après: `{curr_nav.get('root')}`"
        )

    if curr_nav.get("button") != prev_nav.get("button"):
        urgent = True
        changes.append(
            f"🚨 **BOUTON CHANGÉ !**\n"
            f"  Avant: `{prev_nav.get('button')}`\n"
            f"  Après: `{curr_nav.get('button')}`"
        )

    if curr_nav.get("children") != prev_nav.get("children"):
        changes.append(
            f"📋 **Sections de navigation changées !**\n"
            f"  Avant: `{prev_nav.get('children')}`\n"
            f"  Après: `{curr_nav.get('children')}`"
        )

    if current["thumbnail_date"] != previous.get("thumbnail_date"):
        changes.append(
            f"🖼️ **Thumbnail mise à jour !**\n"
            f"  Avant: `{previous.get('thumbnail_date')}`\n"
            f"  Après: `{current['thumbnail_date']}`"
        )

    if current["page_date"] != previous.get("page_date"):
        changes.append(
            f"📄 **Page principale modifiée !**\n"
            f"  Avant: `{previous.get('page_date')}`\n"
            f"  Après: `{current['page_date']}`"
        )

    prev_names = {chunk_component_name(c) for c in previous.get("chunks", [])}
    curr_names = {chunk_component_name(c) for c in current["chunks"]}
    truly_new = curr_names - prev_names
    truly_removed = prev_names - curr_names
    rehashed = len(curr_names) - len(truly_new)

    if truly_new:
        lines = [f"✨ **{len(truly_new)} nouveau(x) composant(s)**\n"]
        for name in sorted(truly_new):
            lines.append(f"• {re.sub(r'([A-Z])', r' \\1', name).strip()}")
        changes.append("\n".join(lines))

    if truly_removed:
        lines = [f"🗑️ **{len(truly_removed)} composant(s) supprimé(s)**\n"]
        for name in sorted(truly_removed):
            lines.append(f"• {re.sub(r'([A-Z])', r' \\1', name).strip()}")
        changes.append("\n".join(lines))

    if current["build"] != previous.get("build") and rehashed > 0 and not truly_new and not truly_removed:
        changes.append(f"_({rehashed} composants rehashés — même contenu)_")

    if changes:
        description = "\n\n".join(changes)
        color = 0xff0000 if urgent else 0xf39c12
        title = "🚨 ANNONCE IMMINENTE ?" if urgent else "🔔 Changement détecté sur SQ42"
        send_discord(title, description, color=color, urgent=urgent)
        save_state(current)
        print(f"CHANGEMENT DÉTECTÉ — {len(changes)} modification(s) !")
    else:
        print(f"  Aucun changement. Build: {current['build']} | Root: {curr_nav.get('root')} | Bouton: {curr_nav.get('button')}")


if __name__ == "__main__":
    print("=" * 50)
    print("SQ42 Monitor — GitHub Actions Edition")
    print(f"Webhook Discord: {'✅ Configuré' if DISCORD_WEBHOOK_URL else '❌ MANQUANT (variable DISCORD_WEBHOOK_URL)'}")
    print("=" * 50)
    check_and_compare()
