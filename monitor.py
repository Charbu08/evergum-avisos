#!/usr/bin/env python3
"""
Avisos a Discord cuando alguien publica en redes.
Lee feeds RSS/Atom (YouTube nativo; TikTok, Instagram y Facebook vía RSS.app)
y envía cada publicación nueva a un webhook de Discord.

Solo usa la librería estándar de Python 3.8+.

Uso:
  python monitor.py config_tiktok.json --once   (una revisión y termina; lo usa GitHub Actions)
  python monitor.py config_tiktok.json          (revisa en bucle cada interval_minutes)

El webhook se toma de la variable de entorno indicada en "webhook_env"
(en GitHub: Settings > Secrets). Si no existe, se usa "webhook_url" del config.
"""
import json
import os
import sys
import time
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET

BASE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE, "seen.json")
MAX_SEEN = 500

COLORS = {
    "youtube": 0xFF0000,
    "tiktok": 0x000000,
    "instagram": 0xE1306C,
    "facebook": 0x1877F2,
}
EMOJIS = {
    "youtube": "▶️",
    "tiktok": "🎵",
    "instagram": "📸",
    "facebook": "👍",
}

UA = {"User-Agent": "Mozilla/5.0 (compatible; DiscordFeedBot/1.0)"}


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def strip_ns(tag):
    return tag.split("}", 1)[-1]


def fetch_entries(url):
    """Devuelve lista de dicts {id, title, link} de un feed RSS o Atom."""
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        root = ET.fromstring(r.read())

    entries = []
    for el in root.iter():
        if strip_ns(el.tag) not in ("item", "entry"):
            continue
        data = {"id": None, "title": "", "link": ""}
        for child in el:
            name = strip_ns(child.tag)
            if name in ("id", "guid"):
                data["id"] = (child.text or "").strip()
            elif name == "title":
                data["title"] = (child.text or "").strip()
            elif name == "link":
                data["link"] = (child.get("href") or child.text or "").strip()
        data["id"] = data["id"] or data["link"] or data["title"]
        if data["id"]:
            entries.append(data)
    return entries


def send_discord(webhook, feed, entry):
    platform = feed.get("platform", "").lower()
    emoji = EMOJIS.get(platform, "🔔")
    payload = {
        "content": f"<@&1490661130413281372> {emoji} **{feed['name']}** publicó algo nuevo en {platform.capitalize() or 'redes'}\n{entry['link']}",
        "embeds": [
            {
                "title": (entry["title"] or "Nueva publicación")[:250],
                "url": entry["link"],
                "color": COLORS.get(platform, 0x5865F2),
            }
        ],
    }
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json", **UA}
    req = urllib.request.Request(webhook, data=body, headers=headers, method="POST")
    try:
        urllib.request.urlopen(req, timeout=30)
    except urllib.error.HTTPError as e:
        if e.code == 429:  # límite de Discord: esperar y reintentar una vez
            time.sleep(float(e.headers.get("Retry-After", "2")) + 1)
            urllib.request.urlopen(
                urllib.request.Request(webhook, data=body, headers=headers, method="POST"),
                timeout=30,
            )
        else:
            raise
    time.sleep(1)  # evitar rate limit


def check_once(webhook, config, state):
    for feed in config["feeds"]:
        key = feed["url"]
        try:
            entries = fetch_entries(feed["url"])
        except Exception as e:
            print(f"[!] No pude leer {feed['name']}: {e}")
            continue

        first_run = key not in state

        if first_run:
            if not entries:
                # Feed vacío o con fallo temporal: no registrar, para no avisar de todo después
                print(f"[i] {feed['name']}: feed sin publicaciones por ahora, reintento luego.")
                continue
            # Primera vez: guardar lo existente sin avisar, para no inundar el canal
            state[key] = [e["id"] for e in entries]
            print(f"[i] {feed['name']}: {len(entries)} publicaciones registradas (sin aviso).")
            continue

        old = state[key]
        seen = set(old)
        nuevas = [e for e in entries if e["id"] not in seen]
        for e in reversed(nuevas):  # de la más vieja a la más nueva
            try:
                send_discord(webhook, feed, e)
                print(f"[+] Aviso enviado: {feed['name']} - {e['title'][:60]}")
                seen.add(e["id"])
            except Exception as ex:
                print(f"[!] Error enviando a Discord ({feed['name']}): {ex}")

        # Conservar primero lo que sigue en el feed y después lo más antiguo, con tope de tamaño
        current = [e["id"] for e in entries if e["id"] in seen]
        current_set = set(current)
        older = [i for i in old if i not in current_set]
        state[key] = (current + older)[:MAX_SEEN]


def main():
    global STATE_FILE
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    once = "--once" in sys.argv

    if not args:
        raise SystemExit("Uso: python monitor.py config_xxx.json [--once]")
    config_path = args[0]
    if not os.path.isabs(config_path):
        config_path = os.path.join(BASE, config_path)
    # Cada config tiene su propio archivo de "ya visto"
    STATE_FILE = os.path.splitext(config_path)[0] + ".seen.json"

    config = load_json(config_path, None)
    if not config:
        raise SystemExit(f"No encuentro o está vacío: {config_path}")

    webhook = ""
    env_name = config.get("webhook_env")
    if env_name:
        webhook = os.environ.get(env_name, "").strip()
    if not webhook:
        webhook = config.get("webhook_url", "").strip()
    if not webhook or "PEGA_AQUI" in webhook:
        raise SystemExit(
            f"Falta el webhook: define el secret/variable '{env_name}' o 'webhook_url' en {os.path.basename(config_path)}."
        )

    state = load_json(STATE_FILE, {})

    if once:
        check_once(webhook, config, state)
        save_json(STATE_FILE, state)
        return

    interval = int(config.get("interval_minutes", 10)) * 60
    print(f"Monitoreando {len(config['feeds'])} cuentas cada {interval // 60} min. Ctrl+C para salir.")
    while True:
        check_once(webhook, config, state)
        save_json(STATE_FILE, state)
        time.sleep(interval)


if __name__ == "__main__":
    main()
