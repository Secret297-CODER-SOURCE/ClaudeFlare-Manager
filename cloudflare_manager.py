"""
██████╗ ███████╗██╗  ██╗████████╗███████╗██████╗     ██████╗ ███╗   ██╗███████╗
██╔══██╗██╔════╝╚██╗██╔╝╚══██╔══╝██╔════╝██╔══██╗    ██╔══██╗████╗  ██║██╔════╝
██║  ██║█████╗   ╚███╔╝    ██║   █████╗  ██████╔╝    ██║  ██║██╔██╗ ██║███████╗
██║  ██║██╔══╝   ██╔██╗    ██║   ██╔══╝  ██╔══██╗    ██║  ██║██║╚██╗██║╚════██║
██████╔╝███████╗██╔╝ ██╗   ██║   ███████╗██║  ██║    ██████╔╝██║ ╚████║███████║
╚═════╝ ╚══════╝╚═╝  ╚═╝   ╚═╝   ╚══════╝╚═╝  ╚═╝    ╚═════╝ ╚═╝  ╚═══╝╚══════╝
                              Cloudflare DNS Manager
"""

import asyncio
import curses
import json
import logging
import os
import random
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any

import aiohttp
import requests

# ─────────────────────────────────────────────
#  ФАЙЛЫ
# ─────────────────────────────────────────────
ACCOUNTS_FILE = "dex_accounts.json"
CNAME_FILE    = "dex_cnames.json"
FAILED_FILE   = "failed_domains.txt"
BASE_URL      = "https://api.cloudflare.com/client/v4"

DNS_DELAY           = 0.4
ZONE_DELAY          = 1.5
RETRY_DELAY         = 5
MAX_RETRIES         = 5
DRY_RUN             = False
ACCOUNT_CONCURRENCY = 3
REQUEST_TIMEOUT     = 120

# ─────────────────────────────────────────────
#  ЦВЕТА (ANSI)
# ─────────────────────────────────────────────
R  = "\033[0m"       # reset
B  = "\033[1m"       # bold
CY = "\033[96m"      # cyan
GR = "\033[92m"      # green
YL = "\033[93m"      # yellow
RD = "\033[91m"      # red
DM = "\033[2m"       # dim
MG = "\033[95m"      # magenta


def c(text, color):
    return f"{color}{text}{R}"


# ─────────────────────────────────────────────
#  ЛОГИРОВАНИЕ
# ─────────────────────────────────────────────
os.makedirs("logs", exist_ok=True)
log_file = f"logs/dexter_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.FileHandler(log_file, encoding="utf-8"),
    ]
)
log = logging.getLogger("dexter")

failed_lock = threading.Lock()
failed_domains: list[str] = []
ns_collected: list[tuple] = []   # (domain, [ns1, ns2], acc_label)
ns_collected_lock = threading.Lock()
log_lock = asyncio.Lock()

# Кэш реальных зон Cloudflare для live-поиска (TUI и текстовый режим)
_domain_cache: dict[str, Any] = {"zones": [], "ts": 0.0}

# ─────────────────────────────────────────────
#  ПРОГРЕСС-БАР
# ─────────────────────────────────────────────
class Progress:
    def __init__(self, total: int, label: str = ""):
        self.total   = total
        self.done    = 0
        self.ok      = 0
        self.fail    = 0
        self.label   = label
        self._lock   = threading.Lock()
        self._start  = time.time()

    def update(self, success: bool):
        with self._lock:
            self.done += 1
            if success:
                self.ok += 1
            else:
                self.fail += 1
            self._draw()

    def _draw(self):
        pct   = self.done / self.total
        width = 30
        filled = int(width * pct)
        bar   = c("█" * filled, GR) + c("░" * (width - filled), DM)
        elapsed = time.time() - self._start
        eta   = (elapsed / self.done * (self.total - self.done)) if self.done else 0
        print(
            f"\r  {bar}  "
            f"{c(str(self.done), CY)}/{c(str(self.total), CY)}  "
            f"{c('✓'+str(self.ok), GR)}  {c('✗'+str(self.fail), RD)}  "
            f"{c(f'ETA {int(eta)}s', DM)}   ",
            end="", flush=True
        )

    def finish(self):
        self._draw()
        print()


# ─────────────────────────────────────────────
#  ХРАНИЛИЩЕ
# ─────────────────────────────────────────────
def load_json(path, default):
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_accounts() -> list[dict]:
    return load_json(ACCOUNTS_FILE, [])


def save_accounts(a):
    save_json(ACCOUNTS_FILE, a)


def load_cnames() -> list[dict]:
    return load_json(CNAME_FILE, [])


def save_cnames(c_):
    save_json(CNAME_FILE, c_)


# ─────────────────────────────────────────────
#  ОБЩИЕ UI УТИЛИТЫ
# ─────────────────────────────────────────────
def sep(char="─", n=64):
    print(c(char * n, DM))


def header(title: str):
    sep("═")
    print(f"  {c(B + title, CY)}")
    sep("─")


def ok(msg):  print(f"  {c('✓', GR)} {msg}")
def err(msg): print(f"  {c('✗', RD)} {msg}"); log.error(msg)
def warn(msg): print(f"  {c('!', YL)} {msg}"); log.warning(msg)
def info(msg): print(f"  {c('·', DM)} {msg}"); log.info(msg)


def prompt(label: str, default: str = "") -> str:
    hint = f" [{c(default, DM)}]" if default else ""
    val  = input(f"  {c('›', CY)} {label}{hint}: ").strip()
    return val if val else default


def confirm(question: str) -> bool:
    ans = input(f"  {c('?', YL)} {question} {c('[да/нет]', DM)}: ").strip().lower()
    return ans in ("да", "y", "yes", "д", "1")


def choose(items: list[str], label: str) -> int | None:
    """Показывает нумерованный список, возвращает 0-based index или None."""
    for i, item in enumerate(items, 1):
        print(f"  {c(f'[{i}]', CY)} {item}")
    raw = input(f"  {c('›', CY)} {label}: ").strip()
    try:
        idx = int(raw) - 1
        if 0 <= idx < len(items):
            return idx
    except ValueError:
        pass
    return None


def choose_multi(items: list[str], label: str) -> list[int]:
    """Выбор нескольких: '1,3', '1-4', '*'."""
    for i, item in enumerate(items, 1):
        print(f"  {c(f'[{i}]', CY)} {item}")
    print(f"  {c('[*]', CY)} Все")
    raw = input(f"  {c('›', CY)} {label} (1,2 / 1-3 / *): ").strip()
    if raw == "*":
        return list(range(len(items)))
    indices = []
    for part in raw.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            try:
                indices.extend(range(int(a) - 1, int(b)))
            except ValueError:
                pass
        else:
            try:
                indices.append(int(part) - 1)
            except ValueError:
                pass
    return [i for i in indices if 0 <= i < len(items)]


# ─────────────────────────────────────────────
#  ВЫБОР АККАУНТА / CNAME
# ─────────────────────────────────────────────
def select_account(label="Аккаунт Cloudflare") -> dict | None:
    accounts = load_accounts()
    header(label)
    items = [f"{c(a.get('label','—'), B)}  {c('<'+a['email']+'>', DM)}" for a in accounts]
    items.append(c("+ Новый аккаунт", YL))
    idx = choose(items, "Выбор")
    if idx is None:
        err("Неверный выбор")
        return None
    if idx == len(accounts):
        return _ask_new_account()
    return accounts[idx]


def select_accounts_multi(label="Аккаунты") -> list[dict]:
    accounts = load_accounts()
    if not accounts:
        warn("Нет сохранённых аккаунтов — добавьте в Настройках")
        return []
    header(label)
    items = [f"{c(a.get('label','—'), B)}  {c('<'+a['email']+'>', DM)}" for a in accounts]
    idxs  = choose_multi(items, "Выбор")
    return [accounts[i] for i in idxs]


def select_cname(label="CNAME-таргет") -> str | None:
    cnames = load_cnames()
    header(label)
    items = [f"{c(c_['name'], B)}  →  {c(c_['target'], GR)}" for c_ in cnames]
    items.append(c("+ Новый таргет", YL))
    idx = choose(items, "Выбор")
    if idx is None:
        err("Неверный выбор")
        return None
    if idx == len(cnames):
        return _ask_new_cname()
    return cnames[idx]["target"]


def _ask_new_account() -> dict | None:
    label   = prompt("Метка (например: FB3, Wawada)")
    email   = prompt("Email")
    api_key = prompt("API Key")
    if not email or not api_key:
        err("Email и API Key обязательны")
        return None
    acc = {"label": label or email, "email": email, "api_key": api_key}
    if confirm("Сохранить аккаунт для следующих сессий?"):
        accs = load_accounts()
        accs.append(acc)
        save_accounts(accs)
        ok(f"Аккаунт «{acc['label']}» сохранён")
    return acc


def _ask_new_cname() -> str | None:
    name   = prompt("Метка таргета (например: Ghost, Main)")
    target = prompt("CNAME target (example.ghost.io)")
    if not target:
        err("Таргет обязателен")
        return None
    if confirm("Сохранить таргет?"):
        cns = load_cnames()
        cns.append({"name": name or target, "target": target})
        save_cnames(cns)
        ok(f"Таргет «{name or target}» сохранён")
    return target


def load_domains_file(path: str) -> list[str]:
    if not os.path.isfile(path):
        err(f"Файл не найден: {path}")
        return []
    with open(path, "r", encoding="utf-8") as f:
        domains = [l.strip().lower() for l in f if l.strip() and not l.startswith("#")]
    domains = list(dict.fromkeys(domains))
    ok(f"Загружено доменов: {c(str(len(domains)), CY)}")
    log.info(f"Loaded {len(domains)} domains from {path}")
    return domains


def ask_domains_file() -> list[str]:
    path = prompt("Файл с доменами", "domains.txt")
    return load_domains_file(path)


def save_failed(domains: list[str]):
    with open(FAILED_FILE, "w", encoding="utf-8") as f:
        for d in domains:
            f.write(d + "\n")
    warn(f"Незалитые домены → {FAILED_FILE} ({len(domains)} шт.)")


# ─────────────────────────────────────────────
#  CF HELPERS (sync)
# ─────────────────────────────────────────────
def cf_hdr(acc: dict) -> dict:
    return {
        "X-Auth-Email": acc["email"],
        "X-Auth-Key":   acc["api_key"],
        "Content-Type": "application/json",
    }


def _add_zone(domain: str, hdr: dict) -> str | None:
    r = requests.post(f"{BASE_URL}/zones",
                      json={"name": domain, "jump_start": True},
                      headers=hdr, timeout=30)
    if r.status_code in (200, 201):
        zid = r.json()["result"]["id"]
        log.info(f"[ADD] {domain} → zone_id={zid}")
        return zid
    log.warning(f"[ADD FAIL] {domain}: {r.json().get('errors', r.text)}")
    return None


def _get_zone_id(domain: str, hdr: dict) -> str | None:
    r = requests.get(f"{BASE_URL}/zones", headers=hdr,
                     params={"name": domain}, timeout=30)
    res = r.json().get("result", [])
    return res[0]["id"] if res else None


def _set_ssl(zone_id: str, domain: str, hdr: dict):
    r = requests.patch(f"{BASE_URL}/zones/{zone_id}/settings/ssl",
                       json={"value": "flexible"}, headers=hdr, timeout=30)
    if r.status_code in (200, 201):
        log.info(f"[SSL] {domain} → flexible")
    else:
        log.warning(f"[SSL FAIL] {domain}: {r.json()}")


def _delete_dns(zone_id: str, domain: str, hdr: dict) -> bool:
    url = f"{BASE_URL}/zones/{zone_id}/dns_records"
    r   = requests.get(url, headers=hdr, timeout=30)
    data = r.json()
    if not data.get("success"):
        log.error(f"[DNS GET FAIL] {domain}: {data.get('errors')}")
        return False
    for rec in data.get("result", []):
        dr = requests.delete(f"{url}/{rec['id']}", headers=hdr, timeout=30)
        if dr.json().get("success"):
            log.info(f"[DNS DEL] {domain} {rec['type']} {rec['name']}")
        else:
            log.warning(f"[DNS DEL FAIL] {domain} {rec['type']}")
    return True


def _add_cname(zone_id: str, domain: str, target: str, hdr: dict) -> bool:
    r = requests.post(
        f"{BASE_URL}/zones/{zone_id}/dns_records",
        json={"type": "CNAME", "name": "@", "content": target, "ttl": 1, "proxied": True},
        headers=hdr, timeout=30
    )
    if r.json().get("success"):
        log.info(f"[CNAME] {domain} → {target}")
        return True
    log.warning(f"[CNAME FAIL] {domain}: {r.json().get('errors')}")
    return False


def _get_ns(zone_id: str, domain: str, hdr: dict) -> list[str]:
    r = requests.get(f"{BASE_URL}/zones/{zone_id}", headers=hdr, timeout=30)
    ns = r.json().get("result", {}).get("name_servers", [])
    if ns:
        log.info(f"[NS] {domain}: {', '.join(ns)}")
    return ns


def _cf_list_zones(hdr: dict) -> list[dict]:
    """Забирает ВСЕ зоны (домены) аккаунта прямо из Cloudflare API, с пагинацией."""
    zones, page = [], 1
    while True:
        try:
            r = requests.get(f"{BASE_URL}/zones", headers=hdr,
                              params={"per_page": 50, "page": page}, timeout=30)
            data = r.json()
        except Exception as e:
            log.error(f"[ZONES LIST ERR] page={page}: {e}")
            break
        if not data.get("success"):
            log.error(f"[ZONES LIST FAIL] {data.get('errors')}")
            break
        result = data.get("result", [])
        zones.extend(result)
        pages = data.get("result_info", {}).get("total_pages", 1)
        if page >= pages or not result:
            break
        page += 1
    return zones


_ns_file_lock = threading.Lock()

def _save_ns(domain: str, ns: list[str], acc_label: str):
    """Дописывает NS в logs/ns_results.txt, привязывая к аккаунту."""
    if not ns:
        return
    line = f"{domain:<40} {', '.join(ns):<60} [{acc_label}]"
    with _ns_file_lock:
        with open("logs/ns_results.txt", "a", encoding="utf-8") as f:
            f.write(line + "\n")
    with ns_collected_lock:
        ns_collected.append((domain, ns, acc_label))
    log.info(f"[NS SAVED] {domain}: {', '.join(ns)}")


def _process_one_add(domain: str, hdr: dict, cname: str, prog: Progress, acc_label: str = "") -> bool:
    zid = _add_zone(domain, hdr) or _get_zone_id(domain, hdr)
    if not zid:
        log.error(f"[SKIP] {domain}: нет зоны")
        prog.update(False)
        with failed_lock:
            failed_domains.append(domain)
        return False
    _set_ssl(zid, domain, hdr)
    if not _delete_dns(zid, domain, hdr):
        prog.update(False)
        with failed_lock:
            failed_domains.append(domain)
        return False
    if not _add_cname(zid, domain, cname, hdr):
        prog.update(False)
        with failed_lock:
            failed_domains.append(domain)
        return False
    ns = _get_ns(zid, domain, hdr)
    _save_ns(domain, ns, acc_label)
    prog.update(True)
    return True


def _print_ns_table():
    """Выводит итоговую таблицу NS после заливки."""
    with ns_collected_lock:
        rows = list(ns_collected)
    if not rows:
        return
    sep()
    print(f"  {c(B + 'NS-серверы по доменам', CY)}")
    sep()
    # Группируем уникальные пары NS для наглядности
    from collections import defaultdict
    by_ns = defaultdict(list)
    for domain, ns, label in rows:
        key = ", ".join(ns)
        by_ns[key].append(domain)

    for ns_pair, doms in sorted(by_ns.items()):
        print(f"  {c(ns_pair, GR)}")
        for d in doms:
            print(f"    {c('·', DM)} {d}")
        print()

    sep()
    print(f"  {c(f'Всего доменов с NS: {len(rows)}', DM)}  "
          f"{c(f'Уникальных NS-пар: {len(by_ns)}', DM)}")
    sep()


def run_add_batch(domains: list[str], hdr: dict, cname: str, workers: int = 10, acc_label: str = ""):
    prog = Progress(len(domains), "AddZone")
    with ns_collected_lock:
        ns_collected.clear()
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _ns_file_lock:
        with open("logs/ns_results.txt", "a", encoding="utf-8") as f:
            f.write(f"\n# ── {ts}  [{acc_label}] ──────────────────────────────\n")
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(_process_one_add, d, hdr, cname, prog, acc_label): d for d in domains}
        for f in as_completed(futures):
            try:
                f.result()
            except Exception as e:
                domain = futures[f]
                log.error(f"[THREAD ERR] {domain}: {e}")
                with failed_lock:
                    if domain not in failed_domains:
                        failed_domains.append(domain)
                prog.update(False)
    prog.finish()
    _print_ns_table()


# ─────────────────────────────────────────────
#  CF HELPERS (async — DNS Cleaner)
# ─────────────────────────────────────────────
async def _alog(path: str, line: str):
    def w():
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    async with log_lock:
        await asyncio.to_thread(w)


async def _retry_req(session, method, url, headers, json_body=None):
    last = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            kw = {"headers": headers}
            if json_body:
                kw["json"] = json_body
            async with session.request(method, url, **kw) as resp:
                if resp.status == 429:
                    d = RETRY_DELAY + random.uniform(0, 1.5)
                    await asyncio.sleep(d)
                    continue
                data = await resp.json(content_type=None)
                if resp.status >= 400 or not data.get("success", False):
                    raise Exception(f"HTTP {resp.status} {data}")
                return data
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            last = e
            await asyncio.sleep(RETRY_DELAY + random.uniform(0, 1.5))
        except Exception as e:
            raise e
    raise Exception(f"Failed {MAX_RETRIES} retries: {last}")


async def _a_get_zone(session, hdr, domain) -> str | None:
    try:
        d = await _retry_req(session, "GET", f"{BASE_URL}/zones?name={domain}", hdr)
        r = d.get("result", [])
        return r[0]["id"] if r else None
    except Exception:
        return None


async def _a_get_dns(session, hdr, zone_id) -> list:
    records, page = [], 1
    while True:
        d = await _retry_req(session, "GET",
                              f"{BASE_URL}/zones/{zone_id}/dns_records?per_page=100&page={page}", hdr)
        r = d.get("result", [])
        records.extend(r)
        if len(r) < 100:
            break
        page += 1
    return records


async def _a_del_dns(session, hdr, zone_id, rid):
    if DRY_RUN:
        return
    await _retry_req(session, "DELETE", f"{BASE_URL}/zones/{zone_id}/dns_records/{rid}", hdr)


async def _a_del_zone(session, hdr, zone_id):
    if DRY_RUN:
        return
    await _retry_req(session, "DELETE", f"{BASE_URL}/zones/{zone_id}", hdr)


async def _a_process_domain(session, acc, hdr, domain, del_zone: bool, prog_list: list, prog_lock):
    email = acc["email"]
    log.info(f"[CLEAN] {domain} account={email}")
    zone_id = await _a_get_zone(session, hdr, domain)
    if not zone_id:
        log.warning(f"[CLEAN] {domain}: зона не найдена")
        async with prog_lock:
            prog_list[0] += 1; prog_list[2] += 1
        return False

    records = await _a_get_dns(session, hdr, zone_id)
    log.info(f"[CLEAN] {domain}: {len(records)} DNS-записей")

    for rec in records:
        rid = rec.get("id")
        if not rid:
            continue
        try:
            await _a_del_dns(session, hdr, zone_id, rid)
            log.info(f"[DNS DEL] {domain} {rec.get('type')} {rec.get('name')}")
        except Exception as e:
            log.error(f"[DNS DEL ERR] {domain}: {e}")
            await _alog("logs/dns_errors.log", f"{domain} | {email} | {e}")
        if not DRY_RUN:
            await asyncio.sleep(DNS_DELAY)

    if del_zone:
        await _a_del_zone(session, hdr, zone_id)
        log.info(f"[ZONE DEL] {domain}")
        if not DRY_RUN:
            await asyncio.sleep(ZONE_DELAY)

    await _alog("logs/deleted.log", f"{'DRY' if DRY_RUN else 'OK'} | {domain} | {email}")
    async with prog_lock:
        prog_list[0] += 1; prog_list[1] += 1
    return True


async def _a_process_account(acc, domains, del_zone, prog_list, prog_lock):
    hdr     = {"X-Auth-Email": acc["email"], "X-Auth-Key": acc["api_key"],
               "Content-Type": "application/json"}
    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
    sem     = asyncio.Semaphore(ACCOUNT_CONCURRENCY)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        tasks = []
        for domain in domains:
            async with sem:
                t = asyncio.create_task(
                    _a_process_domain(session, acc, hdr, domain, del_zone, prog_list, prog_lock)
                )
                tasks.append(t)
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for domain, res in zip(domains, results):
            if isinstance(res, Exception):
                log.error(f"[ERR] {domain}: {res}")
                await _alog("logs/errors.log", f"{domain} | {acc['email']} | {res}")


async def run_cleaner_async(accounts, domains, del_zone):
    prog_list = [0, 0, 0]   # [done, ok, fail]
    prog_lock = asyncio.Lock()
    total     = len(domains) * len(accounts)

    async def _draw_loop():
        while True:
            done, ok_, fail = prog_list
            pct   = done / total if total else 1
            width = 30
            filled = int(width * pct)
            bar   = c("█" * filled, GR) + c("░" * (width - filled), DM)
            print(
                f"\r  {bar}  {c(str(done), CY)}/{c(str(total), CY)}  "
                f"{c('✓'+str(ok_), GR)}  {c('✗'+str(fail), RD)}   ",
                end="", flush=True
            )
            if done >= total:
                print()
                break
            await asyncio.sleep(0.5)

    draw_task = asyncio.create_task(_draw_loop())
    tasks     = [asyncio.create_task(_a_process_account(acc, domains, del_zone, prog_list, prog_lock))
                 for acc in accounts]
    await asyncio.gather(*tasks)
    await draw_task


# ═════════════════════════════════════════════
#  СЦЕНАРИЙ 1: AddZone + SSL + CNAME
# ═════════════════════════════════════════════
def scenario_add_zone():
    header("Залить домены  [AddZone + SSL + CNAME]")

    domains = ask_domains_file()
    if not domains:
        return

    acc = select_account("Аккаунт для заливки")
    if not acc:
        return

    cname = select_cname("CNAME-таргет")
    if not cname:
        return

    try:
        workers = int(prompt("Потоков", "10"))
    except ValueError:
        workers = 10

    sep()
    print(f"  Доменов : {c(str(len(domains)), CY)}")
    print(f"  Аккаунт : {c(acc.get('label', acc['email']), GR)}")
    print(f"  CNAME   : {c(cname, GR)}")
    print(f"  Потоков : {c(str(workers), CY)}")
    sep()
    if not confirm("Запустить?"):
        return

    hdr = cf_hdr(acc)
    failed_domains.clear()

    print()
    run_add_batch(domains, hdr, cname, workers=workers, acc_label=acc.get('label', acc['email']))

    sep()
    ok(f"Успешно: {c(str(len(domains) - len(failed_domains)), GR)} / {len(domains)}")
    if failed_domains:
        err(f"Не залито: {len(failed_domains)}")
        save_failed(list(failed_domains))
        _retry_loop(cname, workers)


def _retry_loop(cname: str, workers: int):
    while failed_domains:
        sep()
        if not confirm(f"Повторить {len(failed_domains)} доменов с другим аккаунтом?"):
            break
        acc2 = select_account("Новый аккаунт")
        if not acc2:
            break
        hdr2      = cf_hdr(acc2)
        retry_lst = list(failed_domains)
        failed_domains.clear()
        run_add_batch(retry_lst, hdr2, cname, workers=workers, acc_label=acc2.get('label', acc2['email']))
        ok(f"В этом раунде: {len(retry_lst) - len(failed_domains)}")
        if not failed_domains:
            ok("Все домены обработаны!")
            break
        save_failed(list(failed_domains))


# ═════════════════════════════════════════════
#  СЦЕНАРИЙ 2: DNS Cleaner
# ═════════════════════════════════════════════
def scenario_dns_cleaner():
    header("DNS Cleaner")

    print(f"  {c('[1]', CY)} Только DNS-записи  {c('(зона остаётся)', DM)}")
    print(f"  {c('[2]', CY)} DNS + вся зона      {c('(удалить полностью)', DM)}")
    sep()
    mode = prompt("Режим", "1")
    del_zone = mode == "2"

    accounts = select_accounts_multi("Аккаунты для очистки")
    if not accounts:
        return

    domains = ask_domains_file()
    if not domains:
        return

    sep()
    print(f"  Аккаунтов : {c(str(len(accounts)), CY)}")
    print(f"  Доменов   : {c(str(len(domains)), CY)}")
    print(f"  Режим     : {c('DNS + Зона' if del_zone else 'Только DNS', YL)}")
    print(f"  DRY_RUN   : {c(str(DRY_RUN), MG)}")
    sep()
    if not confirm("Подтвердить?"):
        print(c("  Отменено", RD))
        return

    print()
    asyncio.run(run_cleaner_async(accounts, domains, del_zone))
    ok(f"Готово. Логи в папке logs/")


# ═════════════════════════════════════════════
#  НАСТРОЙКИ: аккаунты
# ═════════════════════════════════════════════
def settings_accounts():
    while True:
        header("Аккаунты Cloudflare")
        accounts = load_accounts()
        if accounts:
            for i, a in enumerate(accounts, 1):
                print(f"  {c(f'[{i}]', CY)} {c(a.get('label','—'), B)}  "
                      f"{c('<'+a['email']+'>', DM)}")
        else:
            print(f"  {c('(нет аккаунтов)', DM)}")
        sep()
        print(f"  {c('[A]', GR)} Добавить    {c('[D]', RD)} Удалить    {c('[0]', DM)} Назад")
        sep()
        cmd = input(f"  {c('›', CY)} ").strip().lower()
        if cmd == "0":
            break
        elif cmd == "a":
            acc = _ask_new_account()
            if acc and not any(a["email"] == acc["email"] for a in load_accounts()):
                accs = load_accounts()
                accs.append(acc)
                save_accounts(accs)
        elif cmd == "d":
            if not accounts:
                continue
            idx = choose([f"{a.get('label','—')} <{a['email']}>" for a in accounts], "Удалить №")
            if idx is not None:
                removed = accounts.pop(idx)
                save_accounts(accounts)
                ok(f"Удалён: {removed.get('label', removed['email'])}")


# ═════════════════════════════════════════════
#  НАСТРОЙКИ: CNAME
# ═════════════════════════════════════════════
def settings_cnames():
    while True:
        header("CNAME-таргеты")
        cnames = load_cnames()
        if cnames:
            for i, c_ in enumerate(cnames, 1):
                print(f"  {c(f'[{i}]', CY)} {c(c_['name'], B)}  →  {c(c_['target'], GR)}")
        else:
            print(f"  {c('(нет таргетов)', DM)}")
        sep()
        print(f"  {c('[A]', GR)} Добавить    {c('[D]', RD)} Удалить    {c('[0]', DM)} Назад")
        sep()
        cmd = input(f"  {c('›', CY)} ").strip().lower()
        if cmd == "0":
            break
        elif cmd == "a":
            _ask_new_cname()
        elif cmd == "d":
            if not cnames:
                continue
            idx = choose([f"{c_['name']} → {c_['target']}" for c_ in cnames], "Удалить №")
            if idx is not None:
                removed = cnames.pop(idx)
                save_cnames(cnames)
                ok(f"Удалён: {removed['name']}")


# ═════════════════════════════════════════════
#  ГЛАВНОЕ МЕНЮ
# ═════════════════════════════════════════════
LOGO = f"""
{c('██████╗ ███████╗██╗  ██╗████████╗███████╗██████╗     ██████╗ ███╗   ██╗███████╗', CY)}
{c('██╔══██╗██╔════╝╚██╗██╔╝╚══██╔══╝██╔════╝██╔══██╗    ██╔══██╗████╗  ██║██╔════╝', CY)}
{c('██║  ██║█████╗   ╚███╔╝    ██║   █████╗  ██████╔╝    ██║  ██║██╔██╗ ██║███████╗', MG)}
{c('██║  ██║██╔══╝   ██╔██╗    ██║   ██╔══╝  ██╔══██╗    ██║  ██║██║╚██╗██║╚════██║', MG)}
{c('██████╔╝███████╗██╔╝ ██╗   ██║   ███████╗██║  ██║    ██████╔╝██║ ╚████║███████║', CY)}
{c('╚═════╝ ╚══════╝╚═╝  ╚═╝   ╚═╝   ╚══════╝╚═╝  ╚═╝    ╚═════╝ ╚═╝  ╚═══╝╚══════╝', CY)}
{c('                         Cloudflare DNS Manager', DM)}
"""



# ═════════════════════════════════════════════
#  ПУНКТ 5 — Редактор файла доменов
# ═════════════════════════════════════════════
def scenario_domain_editor():
    header("Редактор файла доменов")

    path = prompt("Файл с доменами", "domains.txt")

    def _read():
        if not os.path.isfile(path):
            return []
        with open(path, "r", encoding="utf-8") as f:
            return [l.rstrip("\n") for l in f.readlines()]

    def _write(lines):
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + ("\n" if lines else ""))

    while True:
        lines = _read()
        domains_only = [l for l in lines if l.strip() and not l.startswith("#")]
        sep("═")
        print(f"  {c(path, YL)}  {c(f'({len(domains_only)} доменов)', DM)}")
        sep("─")
        print(f"  {c('[S]', CY)} Показать список")
        print(f"  {c('[A]', CY)} Добавить домены")
        print(f"  {c('[D]', RD)} Удалить домены")
        print(f"  {c('[C]', RD)} Очистить файл")
        print(f"  {c('[I]', YL)} Импорт из другого файла")
        print(f"  {c('[0]', DM)} Назад")
        sep("═")
        cmd = input(f"  {c('›', CY)} ").strip().lower()

        if cmd == "0":
            break

        elif cmd == "s":
            sep()
            if not domains_only:
                warn("Файл пуст")
            else:
                for i, d in enumerate(domains_only, 1):
                    print(f"  {c(str(i).rjust(4), DM)}  {d}")
                sep()
                print(f"  Итого: {c(str(len(domains_only)), CY)} доменов")
            input(f"  {c('Enter — продолжить', DM)}")

        elif cmd == "a":
            print(f"  {c('Вводи домены по одному. Пустая строка — конец.', DM)}")
            added = 0
            existing = set(l.strip().lower() for l in lines if l.strip())
            new_lines = list(lines)
            while True:
                d = input(f"  {c('+', GR)} ").strip().lower()
                if not d:
                    break
                if d in existing:
                    warn(f"{d} уже есть")
                else:
                    new_lines.append(d)
                    existing.add(d)
                    added += 1
            if added:
                _write(new_lines)
                ok(f"Добавлено: {added}")

        elif cmd == "d":
            if not domains_only:
                warn("Файл пуст")
                continue
            print(f"  {c('Введи номера через запятую или диапазон (1-5, * = все):', DM)}")
            for i, d in enumerate(domains_only, 1):
                print(f"  {c(str(i).rjust(4), DM)}  {d}")
            sep()
            raw = input(f"  {c('›', CY)} ").strip()
            if raw == "*":
                to_del = set(domains_only)
            else:
                to_del = set()
                for part in raw.split(","):
                    part = part.strip()
                    if "-" in part:
                        a, b = part.split("-", 1)
                        try:
                            for i in range(int(a)-1, int(b)):
                                to_del.add(domains_only[i])
                        except (ValueError, IndexError):
                            pass
                    else:
                        try:
                            to_del.add(domains_only[int(part)-1])
                        except (ValueError, IndexError):
                            pass
            if not to_del:
                warn("Ничего не выбрано")
                continue
            new_lines = [l for l in lines if l.strip() not in to_del]
            _write(new_lines)
            ok(f"Удалено: {len(to_del)}")

        elif cmd == "c":
            if confirm(f"Очистить {path} полностью?"):
                _write([])
                ok("Файл очищен")

        elif cmd == "i":
            src = prompt("Путь к исходному файлу")
            if not os.path.isfile(src):
                err(f"Файл не найден: {src}")
                continue
            with open(src, "r", encoding="utf-8") as f:
                new_domains = [l.strip().lower() for l in f if l.strip() and not l.startswith("#")]
            existing = set(l.strip().lower() for l in lines if l.strip())
            to_add = [d for d in new_domains if d not in existing]
            if not to_add:
                warn("Все домены уже есть в файле")
                continue
            _write(lines + to_add)
            ok(f"Импортировано: {len(to_add)} доменов (пропущено дублей: {len(new_domains)-len(to_add)})")


# ═════════════════════════════════════════════
#  ПУНКТ 6 — Домены по аккаунтам (из ns_results.txt)
# ═════════════════════════════════════════════
def scenario_ns_viewer():
    header("Домены и NS по аккаунтам")

    ns_file = "logs/ns_results.txt"
    if not os.path.isfile(ns_file):
        warn("Файл logs/ns_results.txt не найден — сначала залейте домены")
        input(f"  {c('Enter — назад', DM)}")
        return

    # Парсим файл: строки вида  domain   ns1, ns2   [label]
    # Заголовки: # ── 2026-...  [label] ───
    from collections import defaultdict, OrderedDict
    acc_data: dict[str, list[dict]] = OrderedDict()  # label -> [{domain, ns, ts}]
    current_label = "?"
    current_ts    = ""

    with open(ns_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.rstrip()
            if line.startswith("# ──"):
                # # ── 2026-06-22 11:52:00  [FB3] ──...
                import re
                m = re.search(r"([\d\-]+ [\d:]+)\s+\[(.+?)\]", line)
                if m:
                    current_ts    = m.group(1)
                    current_label = m.group(2)
                if current_label not in acc_data:
                    acc_data[current_label] = []
            elif line.strip() and not line.startswith("#"):
                # domain   ns1, ns2   [label]
                import re
                m = re.match(r"^(\S+)\s+(.+?)\s+\[(.+?)\]$", line.strip())
                if m:
                    domain = m.group(1)
                    ns     = m.group(2).strip()
                    label  = m.group(3)
                    if label not in acc_data:
                        acc_data[label] = []
                    acc_data[label].append({"domain": domain, "ns": ns, "ts": current_ts})

    if not acc_data:
        warn("Данных нет")
        input(f"  {c('Enter — назад', DM)}")
        return

    while True:
        sep("═")
        print(f"  {c(B + 'Аккаунты с залитыми доменами', CY)}")
        sep("─")
        labels = list(acc_data.keys())
        for i, lbl in enumerate(labels, 1):
            cnt = len(acc_data[lbl])
            print(f"  {c(f'[{i}]', CY)} {c(lbl, B)}  {c(f'{cnt} доменов', DM)}")
        print(f"  {c('[A]', YL)} Все аккаунты сразу")
        print(f"  {c('[E]', YL)} Экспорт в текстовый файл")
        print(f"  {c('[0]', DM)} Назад")
        sep("═")
        cmd = input(f"  {c('›', CY)} ").strip().lower()

        if cmd == "0":
            break

        elif cmd == "a":
            _show_acc_domains(acc_data, labels)

        elif cmd == "e":
            out = prompt("Имя файла для экспорта", "ns_export.txt")
            with open(out, "w", encoding="utf-8") as f:
                for lbl in labels:
                    f.write(f"\n{'='*60}\n")
                    f.write(f"  Аккаунт: {lbl}  ({len(acc_data[lbl])} доменов)\n")
                    f.write(f"{'='*60}\n")
                    # группируем по NS
                    by_ns = defaultdict(list)
                    for row in acc_data[lbl]:
                        by_ns[row["ns"]].append(row["domain"])
                    for ns_pair, doms in sorted(by_ns.items()):
                        f.write(f"  NS: {ns_pair}\n")
                        for d in doms:
                            f.write(f"    {d}\n")
                        f.write("\n")
            ok(f"Сохранено в {out}")

        else:
            try:
                idx = int(cmd) - 1
                if 0 <= idx < len(labels):
                    lbl = labels[idx]
                    _show_acc_domains(acc_data, [lbl])
                else:
                    warn("Неверный номер")
            except ValueError:
                warn("Неверный ввод")


def _show_acc_domains(acc_data: dict, labels: list):
    from collections import defaultdict
    for lbl in labels:
        rows = acc_data[lbl]
        sep("═")
        print(f"  {c(B + lbl, MG)}  {c(f'({len(rows)} доменов)', DM)}")
        sep("─")
        by_ns = defaultdict(list)
        for row in rows:
            by_ns[row["ns"]].append(row["domain"])
        for ns_pair, doms in sorted(by_ns.items()):
            print(f"  {c('NS:', DM)} {c(ns_pair, GR)}")
            for d in doms:
                print(f"       {c('·', DM)} {d}")
            print()
        print(f"  {c(f'Итого: {len(rows)}', DM)}")
    sep("─")
    input(f"  {c('Enter — продолжить', DM)}")

# ═════════════════════════════════════════════
#  ПУНКТ 7 — Домены на Cloudflare (реальный список через API)
# ═════════════════════════════════════════════
def scenario_cf_domains():
    header("Домены на Cloudflare (реальный список через API)")

    accounts = select_accounts_multi("Аккаунты для просмотра")
    if not accounts:
        return

    all_zones: list[dict] = []
    for acc in accounts:
        label = acc.get("label", acc["email"])
        info(f"Запрашиваю зоны для «{label}»...")
        zones = _cf_list_zones(cf_hdr(acc))
        for z in zones:
            z["_account"] = label
        all_zones.extend(zones)
        ok(f"{label}: {c(str(len(zones)), CY)} доменов")

    if not all_zones:
        warn("Доменов не найдено (или ошибка авторизации)")
        input(f"  {c('Enter — назад', DM)}")
        return

    _display_cf_zones(all_zones)


STATUS_COLOR = {"active": GR, "pending": YL, "moved": RD, "deleted": RD, "initializing": YL}


def _display_cf_zones(zones: list[dict]):
    from collections import defaultdict
    by_acc = defaultdict(list)
    for z in zones:
        by_acc[z.get("_account", "?")].append(z)

    for acc_label, zs in by_acc.items():
        sep("═")
        print(f"  {c(B + acc_label, MG)}  {c(f'({len(zs)} доменов)', DM)}")
        sep("─")
        for z in sorted(zs, key=lambda x: x.get("name", "")):
            name   = z.get("name", "?")
            status = z.get("status", "?")
            plan   = (z.get("plan") or {}).get("name", "")
            ns     = ", ".join(z.get("name_servers", []) or [])
            col    = STATUS_COLOR.get(status, DM)
            print(f"  {c(name.ljust(35), B)} {c(status.ljust(14), col)} {c(plan, DM)}")
            if ns:
                print(f"       {c('NS:', DM)} {ns}")
        print()

    sep("─")
    print(f"  {c(f'Всего доменов: {len(zones)}', CY)}")
    sep("═")

    if confirm("Экспортировать список в текстовый файл?"):
        out = prompt("Имя файла", "cf_domains_export.txt")
        with open(out, "w", encoding="utf-8") as f:
            for acc_label, zs in by_acc.items():
                f.write(f"\n{'='*60}\n")
                f.write(f"Аккаунт: {acc_label}  ({len(zs)} доменов)\n")
                f.write(f"{'='*60}\n")
                for z in sorted(zs, key=lambda x: x.get("name", "")):
                    ns = ", ".join(z.get("name_servers", []) or [])
                    f.write(f"{z.get('name','?'):<35} {z.get('status','?'):<14} {ns}\n")
        ok(f"Сохранено в {out}")

    input(f"  {c('Enter — назад', DM)}")


# ═════════════════════════════════════════════
#  ПУНКТ 8 — Поиск доменов (текстовый режим)
# ═════════════════════════════════════════════
def _fetch_all_zones_flat(accounts: list[dict]) -> list[dict]:
    all_zones: list[dict] = []
    for acc in accounts:
        label = acc.get("label", acc["email"])
        zones = _cf_list_zones(cf_hdr(acc))
        for z in zones:
            z["_account"] = label
        all_zones.extend(zones)
        ok(f"{label}: {c(str(len(zones)), CY)} доменов")
    return all_zones


def scenario_domain_search():
    header("Поиск доменов на Cloudflare")

    accounts = load_accounts()
    if not accounts:
        warn("Нет сохранённых аккаунтов — добавьте в Настройках")
        return

    if not _domain_cache["zones"]:
        info("Загружаю домены со всех аккаунтов...")
        _domain_cache["zones"] = _fetch_all_zones_flat(accounts)
        _domain_cache["ts"] = time.time()

    ok(f"В кэше: {c(str(len(_domain_cache['zones'])), CY)} доменов "
       f"({c(str(len(accounts)), CY)} аккаунтов)")

    while True:
        sep()
        q = prompt("Домен для поиска (пусто — назад, 'r' — обновить кэш)")
        if not q:
            break
        if q.lower() == "r":
            info("Обновляю кэш...")
            _domain_cache["zones"] = _fetch_all_zones_flat(accounts)
            _domain_cache["ts"] = time.time()
            continue

        ql = q.lower()
        matches = [z for z in _domain_cache["zones"] if ql in z.get("name", "").lower()]
        sep()
        if not matches:
            warn("Ничего не найдено")
            continue
        for z in sorted(matches, key=lambda x: x.get("name", "")):
            status = z.get("status", "?")
            col    = STATUS_COLOR.get(status, DM)
            ns     = ", ".join(z.get("name_servers", []) or [])
            print(f"  {c(z.get('name','?').ljust(35), B)} "
                  f"{c(z.get('_account','?').ljust(16), MG)} {c(status, col)}")
            if ns:
                print(f"       {c('NS:', DM)} {ns}")
        print(f"\n  Найдено: {c(str(len(matches)), CY)} / {len(_domain_cache['zones'])}")


def main_classic():
    os.system("clear" if os.name == "posix" else "cls")
    print(LOGO)

    while True:
        sep("═")
        accounts = load_accounts()
        cnames   = load_cnames()
        print(f"  {c('Аккаунтов:', DM)} {c(str(len(accounts)), CY)}   "
              f"{c('CNAME-таргетов:', DM)} {c(str(len(cnames)), CY)}   "
              f"{c('Лог:', DM)} {c(log_file, DM)}")
        sep("─")
        print(f"  {c('[1]', CY)}  Залить домены          {c('AddZone + SSL + CNAME', DM)}")
        print(f"  {c('[2]', CY)}  DNS Cleaner            {c('очистить / удалить зоны', DM)}")
        sep("─")
        print(f"  {c('[3]', YL)}  Аккаунты               {c('добавить / удалить', DM)}")
        print(f"  {c('[4]', YL)}  CNAME-таргеты          {c('добавить / удалить', DM)}")
        print(f"  {c('[5]', YL)}  Редактор доменов       {c('добавить / удалить / импорт', DM)}")
        print(f"  {c('[6]', MG)}  Домены по аккаунтам    {c('NS / история заливок (локально)', DM)}")
        print(f"  {c('[7]', MG)}  Домены на Cloudflare   {c('реальный список через API', DM)}")
        print(f"  {c('[8]', MG)}  🔍 Поиск доменов       {c('по всем сохранённым аккаунтам', DM)}")
        sep("─")
        print(f"  {c('[0]', DM)}  Выход")
        sep("═")
        cmd = input(f"  {c('›', CY)} ").strip()

        if   cmd == "1": scenario_add_zone()
        elif cmd == "2": scenario_dns_cleaner()
        elif cmd == "3": settings_accounts()
        elif cmd == "4": settings_cnames()
        elif cmd == "5": scenario_domain_editor()
        elif cmd == "6": scenario_ns_viewer()
        elif cmd == "7": scenario_cf_domains()
        elif cmd == "8": scenario_domain_search()
        elif cmd == "0": print(c("\n  Bye! 👋\n", DM)); break
        else:            warn("Неверный пункт меню")


# ═════════════════════════════════════════════
#  ИНТЕРАКТИВНОЕ TUI (curses): анимации + live-поиск
# ═════════════════════════════════════════════
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
CPAIR: dict[str, int] = {}

TUI_MENU = [
    ("1", "Залить домены",       "AddZone + SSL + CNAME",           scenario_add_zone),
    ("2", "DNS Cleaner",         "очистить / удалить зоны",         scenario_dns_cleaner),
    ("3", "Аккаунты",            "добавить / удалить",              settings_accounts),
    ("4", "CNAME-таргеты",       "добавить / удалить",              settings_cnames),
    ("5", "Редактор доменов",    "domains.txt",                     scenario_domain_editor),
    ("6", "Домены по аккаунтам", "NS / история заливок (локально)", scenario_ns_viewer),
    ("7", "Домены на Cloudflare","реальный список через API",       scenario_cf_domains),
    ("8", "🔍 Поиск доменов",    "live-фильтр по всем аккаунтам",   "SEARCH"),
    ("0", "Выход",               "",                                 "EXIT"),
]


def _init_colors():
    curses.start_color()
    try:
        curses.use_default_colors()
        bg = -1
    except curses.error:
        bg = curses.COLOR_BLACK
    for i, (name, col) in enumerate((
        ("cy", curses.COLOR_CYAN), ("gr", curses.COLOR_GREEN),
        ("yl", curses.COLOR_YELLOW), ("rd", curses.COLOR_RED),
        ("mg", curses.COLOR_MAGENTA), ("wh", curses.COLOR_WHITE),
    ), start=1):
        curses.init_pair(i, col, bg)
        CPAIR[name] = curses.color_pair(i)


def _addstr(win, y, x, text, attr=0):
    h, w = win.getmaxyx()
    if y < 0 or y >= h or x >= w or x < 0:
        return
    try:
        win.addstr(y, x, text[:max(0, w - x - 1)], attr)
    except curses.error:
        pass


def _box(win, y0, x0, h, w, attr=0):
    try:
        win.attron(attr)
        win.hline(y0, x0 + 1, curses.ACS_HLINE, w - 2)
        win.hline(y0 + h - 1, x0 + 1, curses.ACS_HLINE, w - 2)
        win.vline(y0 + 1, x0, curses.ACS_VLINE, h - 2)
        win.vline(y0 + 1, x0 + w - 1, curses.ACS_VLINE, h - 2)
        win.addch(y0, x0, curses.ACS_ULCORNER)
        win.addch(y0, x0 + w - 1, curses.ACS_URCORNER)
        win.addch(y0 + h - 1, x0, curses.ACS_LLCORNER)
        win.addch(y0 + h - 1, x0 + w - 1, curses.ACS_LRCORNER)
        win.attroff(attr)
    except curses.error:
        pass


def _suspend_and_run(stdscr, func):
    curses.def_prog_mode()
    curses.endwin()
    os.system("clear" if os.name == "posix" else "cls")
    try:
        func()
    except Exception as e:
        err(f"Ошибка: {e}")
    input(f"\n  {c('Enter — вернуться в меню', DM)}")
    curses.reset_prog_mode()
    stdscr.clear()
    stdscr.refresh()


def _draw_menu(stdscr, selected: int, frame: int, accounts_n: int, cnames_n: int):
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    spin = SPINNER[frame % len(SPINNER)]

    _addstr(stdscr, 1, 2, "CLOUDFLARE DNS MANAGER", CPAIR["cy"] | curses.A_BOLD)
    _addstr(stdscr, 1, w - 4, spin, CPAIR["mg"])
    _addstr(stdscr, 2, 2,
            f"Аккаунтов: {accounts_n}   CNAME-таргетов: {cnames_n}", CPAIR["wh"] | curses.A_DIM)

    box_y, box_x = 4, 1
    box_h = len(TUI_MENU) + 2
    box_w = min(w - 2, 70)
    _box(stdscr, box_y, box_x, box_h, box_w, CPAIR["cy"])

    pulse = (frame // 5) % 2  # мягкое мигание выбранной строки
    for i, (key, title, desc, _) in enumerate(TUI_MENU):
        y = box_y + 1 + i
        is_sel = (i == selected)
        marker = "❯ " if is_sel else "  "
        m_attr = (CPAIR["gr"] | curses.A_BOLD) if (is_sel and pulse) else \
                 (CPAIR["gr"]) if is_sel else 0
        row_attr = curses.A_BOLD if is_sel else 0
        _addstr(stdscr, y, box_x + 2, marker, m_attr)
        _addstr(stdscr, y, box_x + 4, f"[{key}] {title}", (CPAIR["yl"] if is_sel else CPAIR["wh"]) | row_attr)
        desc_x = box_x + 4 + len(f"[{key}] {title}") + 2
        if desc:
            _addstr(stdscr, y, desc_x, desc, curses.A_DIM)

    hint_y = box_y + box_h + 1
    _addstr(stdscr, hint_y, 2,
            "↑↓ навигация   Enter выбрать   1-8/0 быстрый выбор   Q выход", curses.A_DIM)
    stdscr.refresh()


def curses_main(stdscr):
    curses.curs_set(0)
    _init_colors()
    stdscr.nodelay(True)
    stdscr.timeout(120)

    selected = 0
    frame = 0
    accounts_n = len(load_accounts())
    cnames_n = len(load_cnames())

    while True:
        _draw_menu(stdscr, selected, frame, accounts_n, cnames_n)
        frame += 1
        try:
            key = stdscr.getch()
        except curses.error:
            key = -1

        if key == -1:
            continue
        elif key in (curses.KEY_UP, ord('k')):
            selected = (selected - 1) % len(TUI_MENU)
        elif key in (curses.KEY_DOWN, ord('j')):
            selected = (selected + 1) % len(TUI_MENU)
        elif key in (curses.KEY_ENTER, 10, 13):
            action = TUI_MENU[selected][3]
            if action == "EXIT":
                break
            elif action == "SEARCH":
                screen_domain_search(stdscr)
            else:
                _suspend_and_run(stdscr, action)
                accounts_n = len(load_accounts())
                cnames_n = len(load_cnames())
        elif key in (ord('q'), ord('Q')):
            break
        elif ord('0') <= key <= ord('9'):
            idx = next((i for i, item in enumerate(TUI_MENU) if item[0] == chr(key)), None)
            if idx is not None:
                selected = idx
                action = TUI_MENU[idx][3]
                if action == "EXIT":
                    break
                elif action == "SEARCH":
                    screen_domain_search(stdscr)
                else:
                    _suspend_and_run(stdscr, action)
                    accounts_n = len(load_accounts())
                    cnames_n = len(load_cnames())


# ─────────────────────────────────────────────
#  Live-поиск доменов (curses)
# ─────────────────────────────────────────────
def _draw_loading(stdscr, done: int, total: int, frame: int, label: str):
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    spin = SPINNER[frame % len(SPINNER)]
    msg = f"{spin}  {label}  ({done}/{total})"
    _addstr(stdscr, h // 2, max(0, (w - len(msg)) // 2), msg, CPAIR["cy"] | curses.A_BOLD)
    stdscr.refresh()


def _fetch_zones_with_spinner(stdscr, accounts: list[dict]) -> list[dict]:
    state = {"count": 0, "total": len(accounts), "done": False, "zones": [], "lock": threading.Lock()}

    def one(acc):
        try:
            zs = _cf_list_zones(cf_hdr(acc))
            label = acc.get("label", acc["email"])
            for z in zs:
                z["_account"] = label
            with state["lock"]:
                state["zones"].extend(zs)
        except Exception as e:
            log.error(f"[SEARCH FETCH ERR] {acc.get('label', '?')}: {e}")
        with state["lock"]:
            state["count"] += 1

    def worker():
        with ThreadPoolExecutor(max_workers=5) as ex:
            list(ex.map(one, accounts))
        state["done"] = True

    t = threading.Thread(target=worker, daemon=True)
    t.start()

    stdscr.nodelay(True)
    stdscr.timeout(80)
    frame = 0
    while not state["done"]:
        _draw_loading(stdscr, state["count"], state["total"], frame, "Загружаю домены из Cloudflare")
        frame += 1
        stdscr.getch()
    t.join()
    stdscr.timeout(120)
    return state["zones"]


def _draw_search(stdscr, query: str, filtered: list[dict], selected: int,
                  total_zones: int, total_accounts: int, frame: int):
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    spin = SPINNER[frame % len(SPINNER)]

    _addstr(stdscr, 1, 2, f"🔍 Поиск доменов:", CPAIR["cy"] | curses.A_BOLD)
    _addstr(stdscr, 1, 20, query + "▏", CPAIR["yl"] | curses.A_BOLD)
    _addstr(stdscr, 1, w - 4, spin, CPAIR["mg"])

    box_y, box_x = 3, 1
    box_h = max(5, h - box_y - 3)
    box_w = min(w - 2, 100)
    _box(stdscr, box_y, box_x, box_h, box_w, CPAIR["cy"])

    visible = box_h - 2
    scroll = 0
    if selected >= visible:
        scroll = selected - visible + 1

    for row, z in enumerate(filtered[scroll:scroll + visible]):
        i = row + scroll
        y = box_y + 1 + row
        is_sel = (i == selected)
        name   = z.get("name", "?")
        acc    = z.get("_account", "?")
        status = z.get("status", "?")
        col    = CPAIR.get({"active": "gr", "pending": "yl"}.get(status, "wh"), CPAIR["wh"])
        marker = "❯ " if is_sel else "  "
        line_attr = curses.A_REVERSE if is_sel else 0
        _addstr(stdscr, y, box_x + 2, marker, CPAIR["gr"] if is_sel else 0)
        _addstr(stdscr, y, box_x + 4, name.ljust(36), (CPAIR["yl"] | curses.A_BOLD) if is_sel else curses.A_BOLD)
        _addstr(stdscr, y, box_x + 42, acc.ljust(18), CPAIR["mg"])
        _addstr(stdscr, y, box_x + 61, status, col)

    if not filtered:
        _addstr(stdscr, box_y + 1, box_x + 2, "Ничего не найдено" if query else "Начните вводить домен...", curses.A_DIM)

    footer_y = box_y + box_h + 1
    _addstr(stdscr, footer_y, 2,
            f"Найдено: {len(filtered)} / {total_zones} доменов, {total_accounts} аккаунтов", curses.A_DIM)
    _addstr(stdscr, footer_y + 1, 2,
            "Ввод — фильтр   ↑↓ навигация   Enter — детали   Ctrl+R обновить   Esc назад", curses.A_DIM)
    stdscr.refresh()


def _show_domain_detail(stdscr, z: dict):
    curses.def_prog_mode()
    stdscr.nodelay(False)
    h, w = stdscr.getmaxyx()
    box_h, box_w = 10, min(w - 4, 70)
    y0, x0 = max(1, (h - box_h) // 2), max(1, (w - box_w) // 2)
    win = curses.newwin(box_h, box_w, y0, x0)
    win.erase()
    win.box()
    lines = [
        (f" {z.get('name', '?')}", CPAIR["cy"] | curses.A_BOLD),
        (f" Аккаунт : {z.get('_account', '?')}", CPAIR["mg"]),
        (f" Статус  : {z.get('status', '?')}", 0),
        (f" Тариф   : {(z.get('plan') or {}).get('name', '—')}", 0),
        (f" Zone ID : {z.get('id', '?')}", curses.A_DIM),
        (" NS:", curses.A_BOLD),
    ]
    for ns in (z.get("name_servers") or []):
        lines.append((f"   · {ns}", CPAIR["gr"]))
    for i, (text, attr) in enumerate(lines[:box_h - 3], start=1):
        _addstr(win, i, 1, text, attr)
    _addstr(win, box_h - 2, 1, "Любая клавиша — закрыть", curses.A_DIM)
    win.refresh()
    win.getch()
    stdscr.nodelay(True)
    curses.reset_prog_mode()
    stdscr.clear()
    stdscr.refresh()


def screen_domain_search(stdscr):
    accounts = load_accounts()
    if not accounts:
        _suspend_and_run(stdscr, lambda: warn("Нет сохранённых аккаунтов — добавьте в п. [3] Аккаунты"))
        return

    if not _domain_cache["zones"]:
        zones = _fetch_zones_with_spinner(stdscr, accounts)
        _domain_cache["zones"] = zones
        _domain_cache["ts"] = time.time()

    query = ""
    selected = 0
    frame = 0
    curses.curs_set(1)
    stdscr.nodelay(True)
    stdscr.timeout(100)

    while True:
        zones = _domain_cache["zones"]
        ql = query.lower()
        filtered = sorted(
            (z for z in zones if ql in z.get("name", "").lower()),
            key=lambda z: z.get("name", "")
        ) if query else sorted(zones, key=lambda z: z.get("name", ""))

        _draw_search(stdscr, query, filtered, selected, len(zones), len(accounts), frame)
        frame += 1
        try:
            key = stdscr.getch()
        except curses.error:
            key = -1

        if key == -1:
            continue
        elif key == 27:  # Esc
            break
        elif key in (curses.KEY_UP,):
            selected = max(0, selected - 1)
        elif key in (curses.KEY_DOWN,):
            selected = min(max(0, len(filtered) - 1), selected + 1)
        elif key in (curses.KEY_ENTER, 10, 13):
            if filtered:
                _show_domain_detail(stdscr, filtered[selected])
        elif key in (curses.KEY_BACKSPACE, 127, 8):
            query = query[:-1]
            selected = 0
        elif key == 18:  # Ctrl+R
            zones = _fetch_zones_with_spinner(stdscr, accounts)
            _domain_cache["zones"] = zones
            _domain_cache["ts"] = time.time()
        elif 32 <= key <= 126:
            query += chr(key)
            selected = 0

    curses.curs_set(0)
    stdscr.nodelay(True)
    stdscr.timeout(120)


def main():
    """Запускает новый Textual TUI или совместимый классический режим."""
    if "--classic" in sys.argv or not sys.stdout.isatty():
        main_classic()
        return
    try:
        from cf_manager.tui import run_tui

        run_tui(os.path.dirname(os.path.abspath(__file__)))
    except ImportError as exc:
        print(f"Textual TUI недоступен: {exc}")
        print("Установите зависимости: python3 -m pip install -r requirements.txt")
        print("Запускается классический режим.\n")
        main_classic()


if __name__ == "__main__":
    main()
