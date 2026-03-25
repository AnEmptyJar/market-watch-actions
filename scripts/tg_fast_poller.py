import time
import sys
from pathlib import Path

BASE_DIR = Path("/Users/jimmy-pro/LAB/MONITOR_SYSTEM/monitor_system")
sys.path.insert(0, str(BASE_DIR / "scripts"))

import network_watch_pro as nw

STATE_FILE = nw.STATE_FILE
CONFIG_PATH = nw.CONFIG_PATH

def main():
    while True:
        try:
            cfg = nw.load_json(CONFIG_PATH, {})
            st = nw.load_json(STATE_FILE, {})
            nw._tg_poll_status_commands_v4(cfg, st)
            nw.save_json(STATE_FILE, st)
        except Exception as e:
            try:
                nw.log(f"[TG_FAST] exception: {e}")
            except Exception:
                pass
        time.sleep(2)

if __name__ == "__main__":
    main()
