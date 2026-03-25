#!/usr/bin/env python3
import os, json, ssl, urllib.request

BASE_DIR = os.path.expanduser("~/LAB/MONITOR_SYSTEM/monitor_system")
CONFIG_PATH = os.path.join(BASE_DIR, "config", "config.json")
STATE_FILE  = os.path.join(BASE_DIR, "state", "network_state.json")

def load_json(path: str, default: dict) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def tg_send(text: str, cfg: dict) -> None:
    tg = (cfg.get("telegram") or {})
    token = str(tg.get("bot_token", "")).strip()
    chat_id = str(tg.get("chat_id", "")).strip()
    if not token or not chat_id:
        raise RuntimeError("missing telegram.bot_token or telegram.chat_id")

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = json.dumps({
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True
    }).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10, context=ssl.create_default_context()) as r:
        _ = r.read()

def mask_ip(ip: str) -> str:
    if not ip:
        return "null"
    parts = ip.split(".")
    if len(parts) == 4:
        return f"{parts[0]}.{parts[1]}.xx.xx"
    return ip[:6] + "…"

def ok_str(v) -> str:
    return "OK" if bool(v) else "FAIL"

def main():
    cfg = load_json(CONFIG_PATH, {})
    st  = load_json(STATE_FILE, {})

    ip = mask_ip(st.get("public_ip") or "")
    reality = ok_str(st.get("reality_ok", True))
    google  = ok_str(st.get("google_ok", True))
    youtube = ok_str(st.get("youtube_ok", True))
    port = st.get("port_ms", None)
    rlat = st.get("reality_lat_ms", None)
    port_s = f"{int(port)}ms" if isinstance(port, int) else "null"
    rlat_s = f"{int(rlat)}ms" if isinstance(rlat, int) else "null"

    msg = (
        "【网络健康】\n"
        f"IP: {ip}\n"
        f"Reality: {reality}\n"
        f"Google: {google}\n"
        f"YouTube: {youtube}\n"
        f"Port: {port_s}\n"
        f"RealityLat: {rlat_s}"
    )
    tg_send(msg, cfg)

if __name__ == "__main__":
    main()
