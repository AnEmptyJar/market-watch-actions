import time
import threading
import subprocess

INTERVAL_CHECK = 15
INTERVAL_TG = 2

def run_check_loop():
    while True:
        subprocess.run(["python3", "scripts/network_watch_pro.py"])
        time.sleep(INTERVAL_CHECK)

def tg_fast_loop():
    while True:
        subprocess.run([
            "python3",
            "scripts/network_watch_pro.py",
            "--tg-only"
        ])
        time.sleep(INTERVAL_TG)

if __name__ == "__main__":
    t1 = threading.Thread(target=run_check_loop, daemon=True)
    t2 = threading.Thread(target=tg_fast_loop, daemon=True)

    t1.start()
    t2.start()

    while True:
        time.sleep(60)
