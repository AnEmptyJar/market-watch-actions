#!/usr/bin/env python3
import json
import os
import platform
import re
import shutil
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional, List, Tuple

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "config" / "config.json"
STATE_PATH = BASE_DIR / "state" / "server_state.json"
LOG_PATH = BASE_DIR / "logs" / "server_watch.log"
ERR_PATH = BASE_DIR / "logs" / "server_watch.err"

for p in [BASE_DIR / "logs", BASE_DIR / "state", BASE_DIR / "scripts"]:
    p.mkdir(parents=True, exist_ok=True)


def log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    print(line)


def load_json(path: Path, default: dict) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path: Path, obj: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def run_cmd(cmd: List[str]) -> str:
    return subprocess.check_output(cmd, text=True, stderr=subprocess.STDOUT).strip()


def get_load_avg() -> list[float]:
    try:
        vals = os.getloadavg()
        return [round(vals[0], 2), round(vals[1], 2), round(vals[2], 2)]
    except Exception:
        return []


def get_disk_used_pct() -> Optional[int]:
    try:
        total, used, free = shutil.disk_usage("/")
        if total <= 0:
            return None
        return round(used * 100 / total)
    except Exception:
        return None


def get_linux_cpu_mem() -> Tuple[Optional[float], Optional[float]]:
    cpu_pct = None
    mem_pct = None

    try:
        out = run_cmd(["bash", "-lc", "top -bn1 | head -5"])
        m = re.search(r"(\d+\.\d+)\s*id", out)
        if m:
            idle = float(m.group(1))
            cpu_pct = round(100.0 - idle, 2)
    except Exception:
        pass

    try:
        out = run_cmd(["free", "-m"])
        for line in out.splitlines():
            if line.lower().startswith("mem:"):
                parts = re.split(r"\s+", line.strip())
                total = float(parts[1])
                used = float(parts[2])
                if total > 0:
                    mem_pct = round(used * 100.0 / total, 2)
                break
    except Exception:
        pass

    return cpu_pct, mem_pct


def get_macos_cpu_mem() -> Tuple[Optional[float], Optional[float]]:
    cpu_pct = None
    mem_pct = None

    try:
        out = run_cmd(["bash", "-lc", "top -l 1 | grep 'CPU usage'"])
        m = re.search(r"CPU usage:\s*([\d.]+)% user,\s*([\d.]+)% sys,\s*([\d.]+)% idle", out)
        if m:
            user = float(m.group(1))
            sysv = float(m.group(2))
            cpu_pct = round(user + sysv, 2)
    except Exception:
        pass

    try:
        out = run_cmd(["vm_stat"])
        page_size = 16384
        m = re.search(r"page size of (\d+) bytes", out)
        if m:
            page_size = int(m.group(1))

        pages = {}
        for line in out.splitlines():
            mm = re.match(r"([^:]+):\s+(\d+)\.", line)
            if mm:
                key = mm.group(1).strip()
                pages[key] = int(mm.group(2))

        pressure_used_pages = (
            pages.get("Pages active", 0)
            + pages.get("Pages wired down", 0)
            + pages.get("Pages occupied by compressor", 0)
        )
        reclaimable_pages = (
            pages.get("Pages inactive", 0)
            + pages.get("Pages speculative", 0)
            + pages.get("Pages free", 0)
        )
        total_pages = pressure_used_pages + reclaimable_pages
        if total_pages > 0:
            mem_pct = round(pressure_used_pages * 100.0 / total_pages, 2)
    except Exception:
        pass

    return cpu_pct, mem_pct


def get_metrics(cfg: dict = None) -> dict:
    system = platform.system().lower()
    if system == "darwin":
        cpu_pct, mem_pct = get_macos_cpu_mem()
    else:
        cpu_pct, mem_pct = get_linux_cpu_mem()

    return {
        "host": platform.node(),
        "platform": platform.platform(),
        "cpu_pct": cpu_pct,
        "mem_pct": mem_pct,
        "disk_pct": get_disk_used_pct(),
        "load_avg": get_load_avg(),
        "service_checks": _collect_service_checks(cfg or {}),
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def _check_process_alive(pattern: str) -> bool:
    try:
        if not pattern:
            return False
        out = run_cmd(["bash", "-lc", f"pgrep -f {pattern!r} >/dev/null && echo OK || echo NO"])
        return out.strip() == "OK"
    except Exception:
        return False


def _check_port_listening(port: int) -> bool:
    try:
        if shutil.which("lsof"):
            out = run_cmd(["bash", "-lc", f"lsof -nP -iTCP:{int(port)} -sTCP:LISTEN | tail -n +2 || true"])
            if out.strip():
                return True
        if shutil.which("ss"):
            out = run_cmd(["bash", "-lc", f"ss -lnt '( sport = :{int(port)} )' 2>/dev/null | tail -n +2 || true"])
            if out.strip():
                return True
        return False
    except Exception:
        return False


def _check_systemd_service(service: str):
    try:
        if not service:
            return None
        if not shutil.which("systemctl"):
            return None
        out = run_cmd(["systemctl", "is-active", service])
        return out.strip()
    except Exception:
        return "inactive"


def _collect_service_checks(cfg: dict) -> dict:
    checks_cfg = (((cfg or {}).get("server") or {}).get("checks") or {})
    proc_list = checks_cfg.get("processes") or []
    port_list = checks_cfg.get("ports") or []
    svc_list  = checks_cfg.get("systemd") or []

    processes = {}
    ports = {}
    systemd = {}

    for item in proc_list:
        if isinstance(item, dict):
            name = str(item.get("name") or item.get("pattern") or "")
            pattern = str(item.get("pattern") or item.get("name") or "")
        else:
            name = str(item)
            pattern = str(item)
        if name:
            processes[name] = _check_process_alive(pattern)

    for item in port_list:
        try:
            port = int(item)
            ports[str(port)] = _check_port_listening(port)
        except Exception:
            pass

    for item in svc_list:
        name = str(item).strip()
        if name:
            systemd[name] = _check_systemd_service(name)

    return {
        "processes": processes,
        "ports": ports,
        "systemd": systemd,
    }


def _service_status_text(service_checks: dict) -> str:
    sc = service_checks or {}
    procs = sc.get("processes") or {}
    ports = sc.get("ports") or {}
    svcs = sc.get("systemd") or {}

    if not procs and not ports and not svcs:
        return "未配置"

    bad = []

    bad_proc = [k for k, v in procs.items() if v is False]
    bad_port = [k for k, v in ports.items() if v is False]
    bad_svc = [k for k, v in svcs.items() if v not in (None, "active", "activating")]

    if bad_proc:
        bad.append("进程异常:" + ",".join(bad_proc[:3]))
    if bad_port:
        bad.append("端口异常:" + ",".join(bad_port[:3]))
    if bad_svc:
        bad.append("服务异常:" + ",".join(bad_svc[:3]))

    return "；".join(bad) if bad else "全部正常"



def classify_issues(m: dict) -> list[str]:
    issues: List[str] = []

    cpu = m.get("cpu_pct")
    mem = m.get("mem_pct")
    disk = m.get("disk_pct")
    load = m.get("load_avg") or []
    cpu_count = os.cpu_count() or 1
    service_checks = m.get("service_checks") or {}

    if isinstance(cpu, (int, float)) and cpu >= 85:
        issues.append(f"CPU_HIGH {cpu}%")
    if isinstance(mem, (int, float)) and mem >= 85:
        issues.append(f"MEM_HIGH {mem}%")
    if isinstance(disk, (int, float)) and disk >= 85:
        issues.append(f"DISK_HIGH {disk}%")
    if len(load) >= 1 and isinstance(load[0], (int, float)) and load[0] >= cpu_count * 1.5:
        issues.append(f"LOAD_HIGH {load[0]}")

    for name, ok in (service_checks.get("processes") or {}).items():
        if ok is False:
            issues.append(f"PROCESS_DOWN {name}")

    for port, ok in (service_checks.get("ports") or {}).items():
        if ok is False:
            issues.append(f"PORT_NOT_LISTENING {port}")

    for svc, state in (service_checks.get("systemd") or {}).items():
        if state not in (None, "active", "activating"):
            issues.append(f"SYSTEMD_INACTIVE {svc}={state}")

    return issues



def _host_label(cfg: dict, st: dict) -> str:
    try:
        server = (cfg or {}).get("server") or {}
        label = server.get("host_label")
        if label:
            return str(label)
    except Exception:
        pass
    try:
        return str((st.get("last_metrics") or {}).get("host") or platform.node() or "?")
    except Exception:
        return "?"


def _issue_text_cn(x: str) -> str:
    t = str(x)
    t = t.replace("CPU_HIGH", "CPU过高")
    t = t.replace("MEM_HIGH", "内存过高")
    t = t.replace("DISK_HIGH", "磁盘占用过高")
    t = t.replace("LOAD_HIGH", "负载过高")
    t = t.replace("PROCESS_DOWN", "进程未存活")
    t = t.replace("PORT_NOT_LISTENING", "端口未监听")
    t = t.replace("SYSTEMD_INACTIVE", "服务未激活")
    return t


def _incident_type_cn(issues: list) -> str:
    joined = " ".join(str(x) for x in (issues or []))
    if "PROCESS_DOWN" in joined or "PORT_NOT_LISTENING" in joined or "SYSTEMD_INACTIVE" in joined:
        return "关键服务异常"
    if "CPU_HIGH" in joined:
        return "CPU过高"
    if "MEM_HIGH" in joined:
        return "内存过高"
    if "DISK_HIGH" in joined:
        return "磁盘占用过高"
    if "LOAD_HIGH" in joined:
        return "负载过高"
    return "服务器异常"


def _can_alert(st: dict, key: str, now_ts: int, cooldown: int) -> bool:
    alert_ts = st.get("_alert_ts") or {}
    last = int(alert_ts.get(key, 0) or 0)
    return (now_ts - last) >= cooldown


def _mark_alert(st: dict, key: str, now_ts: int) -> None:
    st.setdefault("_alert_ts", {})[key] = now_ts



def _maybe_send_health_summary(cfg: dict, st: dict, m: dict, issues: list) -> None:
    try:
        if issues:
            return

        tg = (cfg or {}).get("telegram") or {}
        token = tg.get("bot_token")
        chat_id = tg.get("chat_id")
        if not token or not chat_id:
            log("[HEALTH] skip: missing telegram.bot_token or telegram.chat_id in config")
            return

        now_ts = int(time.time())
        # 健康摘要默认每30分钟推一次，避免刷屏
        interval = int((((cfg or {}).get("server") or {}).get("summary_interval_sec", 1800)) or 1800)
        key = "server_health_summary"

        if not _can_alert(st, key, now_ts, interval):
            return

        host_label = _host_label(cfg, st)
        load_text = " ".join(str(x) for x in (m.get("load_avg") or [])) or "?"
        service_text = _service_status_text(m.get("service_checks") or {})

        msg = (
            f"主机: {host_label}\n"
            f"CPU: {m.get('cpu_pct')}%\n"
            f"内存: {m.get('mem_pct')}%\n"
            f"磁盘: {m.get('disk_pct')}%\n"
            f"Load: {load_text}\n"
            f"服务: {service_text}\n"
            f"问题: 无\n"
            f"@{time.strftime('%H:%M')}"
        )

        ok = _tg_send_text(token, chat_id, msg)
        log(f"[HEALTH] sendMessage_ok={ok}")
        if ok:
            _mark_alert(st, key, now_ts)
    except Exception as e:
        try:
            log(f"[HEALTH] exception: {e}")
        except Exception:
            pass

def _maybe_send_alert(cfg: dict, st: dict, m: dict, issues: list) -> None:
    try:
        if not issues:
            return

        tg = (cfg or {}).get("telegram") or {}
        token = tg.get("bot_token")
        chat_id = tg.get("chat_id")
        if not token or not chat_id:
            log("[ALERT] skip: missing telegram.bot_token or telegram.chat_id in config")
            return

        now_ts = int(time.time())
        cooldown = int(((cfg or {}).get("server") or {}).get("alert_cooldown_sec", 900) or 900)
        key = "server_fail"

        if not _can_alert(st, key, now_ts, cooldown):
            log(f"[ALERT] suppressed by cooldown: {key}")
            return

        host_label = _host_label(cfg, st)
        issue_text = "；".join(_issue_text_cn(x) for x in issues[:6]) if issues else "无"
        incident = _incident_type_cn(issues)
        load_text = " ".join(str(x) for x in (m.get("load_avg") or [])) or "?"
        streak = int(st.get("fail_streak", 0) or 0)
        service_text = _service_status_text(m.get("service_checks") or {})

        msg = (
            f"主机: {host_label}\n"
            f"类型: {incident}\n"
            f"CPU: {m.get('cpu_pct')}%\n"
            f"内存: {m.get('mem_pct')}%\n"
            f"磁盘: {m.get('disk_pct')}%\n"
            f"Load: {load_text}\n"
            f"服务: {service_text}\n"
            f"问题: {issue_text}\n"
            f"连续: {streak}\n"
            f"@{time.strftime('%H:%M')}"
        )

        ok = _tg_send_text(token, chat_id, msg)
        log(f"[ALERT] sendMessage_ok={ok} type={incident}")
        if ok:
            _mark_alert(st, key, now_ts)
    except Exception as e:
        try:
            log(f"[ALERT] exception: {e}")
        except Exception:
            pass



def _tg_api_call(token: str, method: str, params: dict = None, timeout: int = 10):
    try:
        params = params or {}
        data = urllib.parse.urlencode(params).encode("utf-8")
        url = f"https://api.telegram.org/bot{token}/{method}"
        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", errors="ignore"))
    except Exception as e:
        try:
            log(f"[WARN] tg api failed: {method} err={e}")
        except Exception:
            pass
        return None


def _tg_send_text(token: str, chat_id: str, text: str) -> bool:
    resp = _tg_api_call(token, "sendMessage", {"chat_id": str(chat_id), "text": text}, timeout=10)
    return bool(resp and resp.get("ok"))


def _tg_poll_status_commands(cfg: dict, st: dict) -> None:
    try:
        tg = (cfg or {}).get("telegram") or {}
        token = tg.get("bot_token")
        chat_id = tg.get("chat_id")
        if not token or not chat_id:
            log("[TG_STATUS] skip: missing telegram.bot_token or telegram.chat_id in config")
            return

        offset = int(st.get("_tg_update_offset") or 0)
        resp = _tg_api_call(token, "getUpdates", {"timeout": 0, "limit": 10, "offset": offset}, timeout=8)
        if not resp or not resp.get("ok"):
            log("[TG_STATUS] getUpdates not ok")
            return

        updates = resp.get("result") or []
        if not updates:
            log(f"[TG_STATUS] no updates (offset={offset})")
            return

        max_id = max(u.get("update_id", 0) for u in updates)
        st["_tg_update_offset"] = max_id + 1

        handled = 0
        for u in updates:
            msg = u.get("message") or u.get("edited_message") or {}
            txt = (msg.get("text") or "").strip().lower()
            cid = str((msg.get("chat") or {}).get("id") or "")
            if cid != str(chat_id):
                continue
            if txt not in ("mac", "/mac"):
                continue

            m = st.get("last_metrics") or {}
            issues = st.get("last_issues") or []
            load_text = " ".join(str(x) for x in (m.get("load_avg") or [])) or "?"
            issues_text = "；".join(_issue_text_cn(x) for x in issues) if issues else "无"
            host_label = _host_label(cfg, st)
            service_text = _service_status_text(m.get("service_checks") or {})

            reply = (
                f"主机: {host_label}\n"
                f"CPU: {m.get('cpu_pct')}%\n"
                f"内存: {m.get('mem_pct')}%\n"
                f"磁盘: {m.get('disk_pct')}%\n"
                f"Load: {load_text}\n"
                f"服务: {service_text}\n"
                f"问题: {issues_text}\n"
                f"@{time.strftime('%H:%M:%S')}"
            )

            ok = _tg_send_text(token, chat_id, reply)
            log(f"[TG_STATUS] replied status (update_id={u.get('update_id')}) sendMessage_ok={ok}")
            handled += 1

        log(f"[TG_STATUS] processed {len(updates)} updates, handled={handled}, new_offset={st.get('_tg_update_offset')}")
    except Exception as e:
        try:
            log(f"[TG_STATUS] exception: {e}")
        except Exception:
            pass


def main() -> int:
    cfg = load_json(CONFIG_PATH, {})
    st = load_json(STATE_PATH, {})

    m = get_metrics(cfg)
    issues = classify_issues(m)
    issues.extend(_collect_security_issues(m))

    fs = int(st.get("fail_streak", 0) or 0)
    if issues:
        fs += 1
    else:
        fs = 0
    st["fail_streak"] = fs

    st["last_metrics"] = m
    st["last_issues"] = issues
    save_json(STATE_PATH, st)
    _tg_poll_status_commands(cfg, st)
    _maybe_send_alert(cfg, st, m, issues)
    _maybe_send_health_summary(cfg, st, m, issues)
    save_json(STATE_PATH, st)

    load_text = " ".join(str(x) for x in (m.get("load_avg") or [])) or "?"
    log(
        f"[SNAPSHOT] "
        f"cpu={m.get('cpu_pct')} mem={m.get('mem_pct')} disk={m.get('disk_pct')} "
        f"load={load_text} issues={issues if issues else '[]'}"
    )

    print("\nserver_watch v1 snapshot")
    print(f"CPU: {m.get('cpu_pct')}%")
    print(f"内存: {m.get('mem_pct')}%")
    print(f"磁盘: {m.get('disk_pct')}%")
    print(f"Load: {load_text}")

    if issues:
        print("问题: " + "；".join(issues))
    else:
        print("问题: 无")

    return 0


def _collect_security_issues(metrics: dict):
    issues = []

    sec = metrics.get("security_checks") or {}

    ssh_fail = sec.get("ssh_fail", 0)
    invalid = sec.get("invalid_user", 0)
    f2b = sec.get("fail2ban", "unknown")

    if ssh_fail >= 20:
        issues.append(f"SSH_FAIL_HIGH:{ssh_fail}")

    if invalid >= 20:
        issues.append(f"SSH_SCAN_HIGH:{invalid}")

    if f2b == "not_installed":
        issues.append("FAIL2BAN_NOT_INSTALLED")

    return issues

if __name__ == "__main__":
    raise SystemExit(main())

