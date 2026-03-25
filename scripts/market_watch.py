#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import os
import time
import ssl
import random
import socket
import urllib.request
from datetime import datetime, time as dtime
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(BASE_DIR, "config", "config.json")
LOG_DIR = os.path.join(BASE_DIR, "logs")
STATE_DIR = os.path.join(BASE_DIR, "state")
LOG_FILE = os.path.join(LOG_DIR, "market_watch.log")
STATE_FILE = os.path.join(STATE_DIR, "market_watch_state.json")

os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(STATE_DIR, exist_ok=True)

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X) monitor_system/market_watch_v2"


# ---------- Telegram: 冷却 + outbox（避免网络抖动时卡顿/刷失败） ----------

def _load_state_file(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _save_state_file(path: str, st: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def send_telegram(text: str, cfg: dict) -> bool:
    tg = cfg.get("telegram", {}) or {}
    token = tg.get("bot_token")
    chat_id = tg.get("chat_id")
    if not token or not chat_id:
        raise RuntimeError("telegram.bot_token/chat_id missing in config")

    import urllib.request, urllib.parse, json
    params = {
        "chat_id": str(chat_id),
        "text": text,
    }
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode(params).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        raw = r.read().decode("utf-8", "ignore")
    obj = json.loads(raw)
    if not obj.get("ok"):
        raise RuntimeError(f"telegram send failed: {raw}")
    return True

def tg_send_safe(text: str, cfg: dict) -> bool:
    """
    Telegram 发送增强版：
    - 失败冷却：失败后 cooldown_sec 内不再尝试（默认 600s），避免每次运行卡很久
    - outbox：失败时把消息写进 state.json，下次成功时先补发
    """
    cooldown_sec = int(cfg.get("telegram", {}).get("cooldown_sec", 600))
    now_ts = int(time.time())

    st = _load_state_file(STATE_FILE)
    tg = st.setdefault("telegram", {})
    last_fail = int(tg.get("last_fail_ts", 0) or 0)

    # 冷却期：不尝试发送，只更新 outbox
    if last_fail and (now_ts - last_fail) < cooldown_sec:
        log(f"[WARN] telegram cooldown active ({now_ts-last_fail}s<{cooldown_sec}s), skip send")
        tg["outbox_text"] = text
        tg["outbox_ts"] = now_ts
        _save_state_file(STATE_FILE, st)
        return False

    # 先补发 outbox
    outbox = tg.get("outbox_text")
    if outbox:
        try:
            send_telegram(outbox, cfg)
            tg.pop("outbox_text", None)
            tg.pop("outbox_ts", None)
            log("[INFO] telegram outbox delivered")
        except Exception as e:
            tg["last_fail_ts"] = now_ts
            _save_state_file(STATE_FILE, st)
            log(f"[WARN] telegram send failed (outbox): {e}")
            return False

    # 再发本次
    try:
        send_telegram(text, cfg)
        tg.pop("last_fail_ts", None)
        _save_state_file(STATE_FILE, st)
        return True
    except Exception as e:
        tg["last_fail_ts"] = now_ts
        tg["outbox_text"] = text
        tg["outbox_ts"] = now_ts
        _save_state_file(STATE_FILE, st)
        log(f"[WARN] telegram send failed: {e}")
        return False

# ----------------- utils -----------------

def log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"[{ts}] {msg}\n")

def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

def load_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        log(f"[WARN] state load failed: {e}")
        return {}

def save_state(st: dict) -> None:
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=2)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        log(f"[WARN] state save failed: {e}")

def arrow(pct: float) -> str:
    return "↑" if pct >= 0 else "↓"

def fmt_num(v, digits: int = 2) -> str:
    if v is None:
        return "--"
    try:
        return f"{float(v):.{digits}f}"
    except Exception:
        return "--"

def fmt_pct_or_dash(pct) -> str:
    if pct is None:
        return "--"
    try:
        pct = float(pct)
        return f"{arrow(pct)}{abs(pct):.2f}%"
    except Exception:
        return "--"

def in_time_range(now: dtime, start: dtime, end: dtime) -> bool:
    if start <= end:
        return start <= now < end
    return now >= start or now < end

def should_push(now: datetime, pct_values: dict, cfg: dict) -> bool:
    t = now.time()
    day_start = dtime.fromisoformat(cfg["schedule"]["day_start"])
    day_end = dtime.fromisoformat(cfg["schedule"]["day_end"])
    night_th = float(cfg["thresholds"]["night_major_pct"])

    if in_time_range(t, day_start, day_end):
        return True

    # 夜间：只有当“任一可用标的”超过夜间阈值才推
    for v in pct_values.values():
        if v is None:
            continue
        try:
            if abs(float(v)) >= night_th:
                return True
        except Exception:
            continue
    return False

# ----------------- http -----------------

def http_get(url: str, timeout: int, retries: int) -> bytes:
    ctx = ssl.create_default_context()
    last_err = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                return resp.read()
        except Exception as e:
            last_err = e
            # 退避+抖动
            time.sleep(0.4 + i * 0.6 + random.random() * 0.3)
    raise last_err

def http_get_json(url: str, timeout: int, retries: int):
    data = http_get(url, timeout=timeout, retries=retries)
    return json.loads(data.decode("utf-8", errors="replace"))

def http_get_text(url: str, timeout: int, retries: int) -> str:
    data = http_get(url, timeout=timeout, retries=retries)
    return data.decode("utf-8", errors="replace")

def safe_call(tag: str, fn, errors: list, default=None):
    try:
        return fn()
    except Exception as e:
        msg = f"{tag}: {e}"
        errors.append(msg)
        log(f"[WARN] {msg}")
        return default

# ----------------- telegram -----------------

def tg_send(text: str, cfg: dict) -> None:
    token = str(cfg["telegram"]["bot_token"]).strip()
    chat_id = str(cfg["telegram"]["chat_id"]).strip()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True
    }).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": UA},
        method="POST"
    )

    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=10, context=ctx) as resp:
        _ = resp.read()

# ----------------- data sources -----------------

def get_btc_eth_pct_binance(timeout, retries):
    btc = http_get_json("https://api.binance.com/api/v3/ticker/24hr?symbol=BTCUSDT", timeout, retries)
    eth = http_get_json("https://api.binance.com/api/v3/ticker/24hr?symbol=ETHUSDT", timeout, retries)
    return float(btc["lastPrice"]), float(btc["priceChangePercent"]), float(eth["lastPrice"]), float(eth["priceChangePercent"])

def get_btc_eth_pct_coingecko(timeout, retries):
    url = "https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd&ids=bitcoin,ethereum"
    arr = http_get_json(url, timeout, retries)
    mp = {x["id"]: x for x in arr}
    btc = mp["bitcoin"]
    eth = mp["ethereum"]
    return float(btc["current_price"]), float(btc.get("price_change_percentage_24h")), float(eth["current_price"]), float(eth.get("price_change_percentage_24h"))

def get_csv_last_row_stooq(symbol: str, timeout, retries):
    url = f"https://stooq.com/q/l/?s={symbol}&f=sd2t2ohlcv&h&e=csv"
    txt = http_get_text(url, timeout, retries)
    lines = [x.strip() for x in txt.splitlines() if x.strip()]
    if len(lines) < 2:
        raise RuntimeError(f"stooq csv empty: {symbol}")
    header = lines[0].split(",")
    vals = lines[-1].split(",")
    row = dict(zip(header, vals))
    o = float(row.get("Open") or "nan")
    c = float(row.get("Close") or "nan")
    return o, c

def get_gold_usd_oz_pct_stooq(timeout, retries):
    o, c = get_csv_last_row_stooq("xauusd", timeout, retries)
    if o != o or c != c:
        raise RuntimeError("stooq xauusd invalid o/c")
    pct = (c - o) / o * 100.0
    return c, pct

def get_stock_usd_pct_stooq(symbol: str, timeout, retries):
    o, c = get_csv_last_row_stooq(symbol, timeout, retries)
    if o != o or c != c:
        raise RuntimeError(f"stooq {symbol} invalid o/c")
    pct = (c - o) / o * 100.0
    return c, pct

def get_usdcny_primary(timeout, retries):
    j = http_get_json("https://open.er-api.com/v6/latest/USD", timeout, retries)
    if not isinstance(j, dict) or not isinstance(j.get("rates"), dict) or "CNY" not in j["rates"]:
        raise RuntimeError("fx primary: missing rates/CNY")
    return float(j["rates"]["CNY"])

def get_usdcny_backup(timeout, retries):
    j = http_get_json("https://api.frankfurter.app/latest?from=USD&to=CNY", timeout, retries)
    if not isinstance(j, dict) or not isinstance(j.get("rates"), dict) or "CNY" not in j["rates"]:
        raise RuntimeError("fx backup: missing rates/CNY")
    return float(j["rates"]["CNY"])

def usd_oz_to_cny_g(usd_per_oz: float, usdcny: float) -> float:
    return (usd_per_oz * usdcny) / 31.1034768

# ----------------- probes (VPS / Reality heuristic) -----------------

def tcp_probe(host: str, port: int, timeout_sec: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout_sec):
            return True
    except Exception:
        return False

def get_public_ip(timeout, retries):
    # 轻量：拿纯 IP
    txt = http_get_text("https://ipinfo.io/ip", timeout, retries).strip()
    if txt and len(txt) < 64:
        return txt
    raise RuntimeError("public ip empty")

# ----------------- alerts -----------------

def can_alert(st: dict, key: str, now_ts: int, cooldown_sec: int) -> bool:
    al = st.setdefault("alerts", {})
    last = int(al.get(key, 0) or 0)
    if now_ts - last >= cooldown_sec:
        al[key] = now_ts
        return True
    return False

# ----------------- main -----------------

def main():
    cfg = load_config()
    st = load_state()
    now = datetime.now()
    ts_hm = now.strftime("%H:%M")
    now_ts = int(time.time())

    http_cfg = cfg.get("http", {})
    timeout = int(http_cfg.get("timeout_sec", 8))
    retries = int(http_cfg.get("retries", 2))

    perf = cfg.get("perf", {})
    workers = int(perf.get("concurrent_workers", 6))

    # FX cache
    fx_cfg = cfg.get("fx_cache", {})
    fx_ttl = int(fx_cfg.get("ttl_sec", 12 * 3600))

    # alerts
    alert_cfg = cfg.get("alerts", {})
    major_th = float(alert_cfg.get("major_pct", 3.0))
    alert_cooldown = int(alert_cfg.get("cooldown_sec", 900))

    # probes
    net_cfg = cfg.get("network", {})
    expect_exit_ip = str(net_cfg.get("expected_exit_ip", "")).strip() or None
    vps_host = str(net_cfg.get("vps_host", "")).strip() or None
    vps_port = int(net_cfg.get("vps_port", 8443))
    vps_tcp_timeout = float(net_cfg.get("tcp_timeout_sec", 2.5))

    errors = []

    # --- 并发抓取：把“慢接口”并行化 ---
    results = {
        "btceth": None,
        "gold": None,
        "fx": None,
        "tsla": None,
        "nvda": None,
        "public_ip": None,
        "vps_tcp": None,
    }

    def task_btceth():
        r = safe_call("binance(btc/eth)", lambda: get_btc_eth_pct_binance(timeout, retries), errors, default=None)
        if r is None:
            r = safe_call("coingecko(btc/eth)", lambda: get_btc_eth_pct_coingecko(timeout, retries), errors, default=None)
        return r

    def task_gold():
        return safe_call("stooq(gold)", lambda: get_gold_usd_oz_pct_stooq(timeout, retries), errors, default=None)

    def task_fx():
        r = safe_call("fx(primary)", lambda: get_usdcny_primary(timeout, retries), errors, default=None)
        if r is None:
            r = safe_call("fx(backup)", lambda: get_usdcny_backup(timeout, retries), errors, default=None)
        return r

    def task_tsla():
        return safe_call("stooq(tsla)", lambda: get_stock_usd_pct_stooq("tsla.us", timeout, retries), errors, default=None)

    def task_nvda():
        return safe_call("stooq(nvda)", lambda: get_stock_usd_pct_stooq("nvda.us", timeout, retries), errors, default=None)

    def task_public_ip():
        return safe_call("probe(public_ip)", lambda: get_public_ip(timeout=timeout, retries=retries), errors, default=None)

    def task_vps_tcp():
        if not vps_host:
            return None
        ok = tcp_probe(vps_host, vps_port, vps_tcp_timeout)
        return bool(ok)

    futures = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures[ex.submit(task_btceth)] = "btceth"
        futures[ex.submit(task_gold)] = "gold"
        futures[ex.submit(task_fx)] = "fx"
        futures[ex.submit(task_tsla)] = "tsla"
        futures[ex.submit(task_nvda)] = "nvda"
        # probes
        futures[ex.submit(task_public_ip)] = "public_ip"
        futures[ex.submit(task_vps_tcp)] = "vps_tcp"

        for fu in as_completed(futures):
            k = futures[fu]
            try:
                results[k] = fu.result()
            except Exception as e:
                errors.append(f"task({k}): {e}")
                results[k] = None

    # --- 解包结果 ---
    btc_price = btc_pct = eth_price = eth_pct = None
    if results["btceth"] is not None:
        btc_price, btc_pct, eth_price, eth_pct = results["btceth"]

    gold_usd_oz = gold_pct = None
    if results["gold"] is not None:
        gold_usd_oz, gold_pct = results["gold"]

    tsla_price = tsla_pct = None
    if results["tsla"] is not None:
        tsla_price, tsla_pct = results["tsla"]

    nvda_price = nvda_pct = None
    if results["nvda"] is not None:
        nvda_price, nvda_pct = results["nvda"]

    usdcny = results["fx"] if results["fx"] is not None else None

    # --- FX 缓存：FX 两个源都挂了就用缓存（黄金¥/g尽量不断） ---
    fx_cache = st.setdefault("fx_cache", {})
    if usdcny is not None:
        fx_cache["rate"] = float(usdcny)
        fx_cache["ts"] = now_ts
    else:
        # 用缓存兜底
        cached_rate = fx_cache.get("rate")
        cached_ts = int(fx_cache.get("ts", 0) or 0)
        if cached_rate is not None and (now_ts - cached_ts) <= fx_ttl:
            usdcny = float(cached_rate)
            log(f"[INFO] fx cache used (age={now_ts-cached_ts}s)")
        else:
            usdcny = None

    gold_cny_g = None
    if gold_usd_oz is not None and usdcny is not None:
        try:
            gold_cny_g = usd_oz_to_cny_g(float(gold_usd_oz), float(usdcny))
        except Exception as e:
            errors.append(f"gold cny/g calc: {e}")
            gold_cny_g = None

    # --- 价格异常报警（>3%）---
    triggered = []
    for k, v in [("Gold", gold_pct), ("BTC", btc_pct), ("ETH", eth_pct), ("TSLA", tsla_pct), ("NVDA", nvda_pct)]:
        if v is None:
            continue
        try:
            if abs(float(v)) >= major_th:
                triggered.append((k, float(v)))
        except Exception:
            continue

    if triggered and can_alert(st, "major_move", now_ts, alert_cooldown):
        # 不影响主推送：单独发一条报警
        parts = [f"{k} {arrow(p)}{abs(p):.2f}%" for k, p in triggered]
        alert_text = "‼️ Market Alert\n" + " ".join(parts) + f" @{ts_hm}"
        try:
            tg_send_safe(alert_text, cfg)
            log("[INFO] alert sent: major_move")
        except Exception as e:
            log(f"[WARN] telegram send failed (alert): {e}")

    # --- VPS 是否被墙（可配置：期望出口IP + VPS端口探测） ---
    pub_ip = results["public_ip"]
    vps_tcp_ok = results["vps_tcp"]
# 
#     if expect_exit_ip and pub_ip and pub_ip != expect_exit_ip:
#         if can_alert(st, "exit_ip_mismatch", now_ts, 3600):
#             try:
#                 tg_send_safe(f"⚠️ 出口IP异常: {pub_ip} (期望 {expect_exit_ip}) @{ts_hm}", cfg)
#                 log("[WARN] alert sent: exit_ip_mismatch_disabled")
#             except Exception as e:
#                 log(f"[WARN] telegram send failed (exit_ip): {e}")

    if vps_host and vps_tcp_ok is False:
        if can_alert(st, "vps_port_down", now_ts, 1800):
            try:
                tg_send_safe(f"⚠️ VPS端口不可达: {vps_host}:{vps_port} @{ts_hm}", cfg)
                log("[WARN] alert sent: vps_port_down")
            except Exception as e:
                log(f"[WARN] telegram send failed (vps_tcp): {e}")

    # --- Reality 是否被干扰（启发式）：TCP可达但多站点TLS/EOF异常集中出现 ---
    # 说明：不做“真Reality握手”，只做“异常形态”告警（够用、低侵入）
    bad_keywords = ("EOF occurred in violation of protocol", "handshake operation timed out", "timed out")
    bad_hits = sum(1 for e in errors if any(k in e for k in bad_keywords))
    if vps_host and vps_tcp_ok is True and bad_hits >= 3:
        if can_alert(st, "possible_interference", now_ts, 1800):
            try:
                tg_send_safe(f"⚠️ 疑似网络干扰/Reality异常: TLS/EOF异常{bad_hits}次 @{ts_hm}", cfg)
                log("[WARN] alert sent: possible_interference")
            except Exception as e:
                log(f"[WARN] telegram send failed (interference): {e}")

    # --- 推送策略（样式A：固定5行，缺失用 --）---
    pct_values = {"Gold": gold_pct, "BTC": btc_pct, "ETH": eth_pct, "TSLA": tsla_pct, "NVDA": nvda_pct}

    any_ok = any(x is not None for x in [gold_usd_oz, btc_price, eth_price, tsla_price, nvda_price])
    # 全挂保护：若所有接口都失败，但 FX 有缓存，则仍推送固定 5 行（Gold 用缓存，其它用 --）
    all_down = all(v is None for v in pct_values.values())
    if all_down:
        # cached_rate 来自 fx_cache（上面已计算/读取），有它就不发“全挂”报警
        if 'cached_rate' in locals() and cached_rate is not None:
            log('[WARN] all sources down but fx_cache available; continue to push placeholders')
        else:
            msg = f"接口异常: 全部数据源不可用 @{ts_hm}"
            log(msg + (" | " + " | ".join(errors) if errors else ""))
            try:
                tg_send_safe(msg, cfg)
            except Exception as e:
                log(f"[WARN] telegram send failed (all down): {e}")
            return
    if not should_push(now, pct_values, cfg):
        log(f"[INFO] silent (night) @{ts_hm}")
        if errors:
            log("[INFO] partial errors: " + " | ".join(errors))
        save_state(st)
        return

    gold_usd = f"${fmt_num(gold_usd_oz, 2)}/oz"
    gold_cny = f"¥{fmt_num(gold_cny_g, 1)}/g"
    gold_line = f"Gold {gold_usd} {gold_cny} {fmt_pct_or_dash(gold_pct)}"

    btc_line = f"BTC {fmt_num(btc_price, 2)} {fmt_pct_or_dash(btc_pct)}"
    eth_line = f"ETH {fmt_num(eth_price, 2)} {fmt_pct_or_dash(eth_pct)}"
    tsla_line = f"TSLA {fmt_num(tsla_price, 2)} {fmt_pct_or_dash(tsla_pct)}"
    nvda_line = f"NVDA {fmt_num(nvda_price, 2)} {fmt_pct_or_dash(nvda_pct)} @{ts_hm}"

    text = "\n".join([gold_line, btc_line, eth_line, tsla_line, nvda_line])

    if errors:
        log("[INFO] partial errors: " + " | ".join(errors))

    try:
        send_telegram(text, cfg)
        log("[INFO] pushed ok")
    except Exception as e:
        log(f"[WARN] telegram send failed: {e}")

    save_state(st)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        try:
            log(f"[FATAL_GUARD] {e}")
        except:
            print("[FATAL_GUARD]",e)

