#!/usr/bin/env python3
"""
FSTT - Freshservice Time Tracking assistant for TAMs.

Just run it:   python fstt.py          (the first run walks you through setup)

Inside the wizard you can always type:  b = back,  q = quit,  ? = help

Commands used by scheduled tasks:
  python fstt.py create-week [CUSTOMER ...]   create this week's recurring tickets (no prompts)
  python fstt.py resolve-previous             resolve earlier weeks that are still open (no prompts)
  python fstt.py remind                       interactive: log this week's time (Friday reminder)
  python fstt.py setup                        re-run the full setup
  python fstt.py auto                         legacy all-in-one task
Options: --date YYYY-MM-DD (act as if today is that date), --dry-run (show, change nothing)
"""
from __future__ import annotations

import argparse
import datetime as dt
import getpass
import html
import json
import os
import platform
import re
import subprocess
import sys
import time
import tomllib
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit(f"FSTT needs the 'requests' package. Install it with:\n  \"{sys.executable}\" -m pip install requests")

CONFIG_VERSION = 2
HERE = Path(__file__).resolve().parent
CFG_PATH = Path(os.environ.get("FSTT_CONFIG", HERE / "config.toml"))
STATE_PATH = Path(os.environ.get("FSTT_STATE", HERE / "state.json"))
ENV_KEY = "FRESHSERVICE_API_KEY"
KEYCHAIN_SERVICE = "fstt-freshservice"
LINUX_KEY_FILE = Path.home() / ".config" / "fstt" / "api_key"
DEFAULT_DOMAIN = "cbportal.freshservice.com"
SM_CATEGORY_ID = 23000088050          # Service Management
SM_WORKSPACE_ID = 2
STATUS_RESOLVED = 4
STATUS_NAMES = {2: "Open", 3: "Pending", 4: "Resolved", 5: "Closed"}
PLACEHOLDER_DESC = {"", "no description available"}
TOP_LEVEL_TICKET_FIELDS = {"group_id", "responder_id", "priority", "status", "source", "department_id",
                           "category", "sub_category", "item_category", "urgency", "impact", "due_by",
                           "fr_due_by", "description", "subject", "type", "tags", "workspace_id"}

# Service Management catalog - used when the API key is not allowed to list the catalog
BUILTIN_CATALOG = [
    (78, "3rd Party Issues Coordination"), (71, "Access Management"), (77, "Business Prioritization"),
    (91, "Change Request"), (62, "Customer Documentation"), (90, "Customer Onboarding"),
    (66, "Maintenance Planning"), (74, "Monthly Operational Review"), (72, "New Feature Recommendation"),
    (64, "New Vendor Product Launch"), (70, "Oriented Product Roadmap"), (65, "Platform Assistance (How-to)"),
    (76, "Platform Audit"), (69, "Problem Management"), (89, "Ticket Follow Up"), (73, "Upgrade Review"),
    (67, "Vendor Catalog Update"), (68, "Vendor Prices Update"), (75, "Weekly Operational Review"),
]
ITEM_KEYS = {75: "wor", 65: "howto", 89: "followup"}
ITEM_PROMPTS = {
    65: [{"name": "question_topic", "label": "How-to type", "required": True,
          "choices": ["Commerce", "Connect", "APIs", "Custom Developed Software"]}],
    89: [{"name": "related_ticket", "label": "Related ticket ID", "required": False}],
    73: [{"name": "current_version", "label": "Current version", "required": False},
         {"name": "new_version", "label": "New version", "required": True},
         {"name": "related_ticket", "label": "Related ticket ID", "required": False}],
}
PLATFORM_VERSIONS = (["SaaS", "Connect", "CASA"] + [f"21.{n}" for n in range(29, -1, -1) if n != 13]
                     + ["End of life versions", "Automation error"])
BUILTIN_CLOSURE_FIELDS = [
    {"name": "ticket_type", "label": "Ticket type", "choices": None, "default": "TAM Request"},
    {"name": "environment", "label": "Environment", "choices": ["Production", "Lab", "Connect"], "default": "Production"},
    {"name": "platform_version", "label": "Platform version", "choices": PLATFORM_VERSIONS, "default": "SaaS"},
]
INTERNAL_NOTE_CHOICES = ["Team sync / TAM weekly meeting", "1-1 with manager", "Monthly status update",
                         "Roadmap session", "PI planning", "Coaching", "Training"]

HELP_TEXT = """
================================ FSTT help ================================
What it does
  Creates your Freshservice service requests (one per customer and ticket
  type, per week), logs time on them and resolves them at the end of week.

Weeks
  Weeks are ISO weeks (Mon-Sun) and appear in subjects as "Week #48".
  When asked "Which week?" you can answer:
     Enter = this week   p = previous   n = next
     48 = week number    2026-11-12 = any date inside the week

Main menu
  Create tickets            weekly set for all/selected customers, or one
                            custom ticket of any catalog type (also for a
                            customer that is not in your list)
  Log time                  for one customer, or one by one for all open tickets
  Tickets with no time      lists a week's tickets with 0h and lets you
                            fill them in
  Status of a week          tickets, status and hours
  Resolve tickets           a whole week, one customer, or one ticket
  Settings                  customers, field values, ticket types, internal
                            company, API key, scheduled tasks

Hours can be typed as 1.5, 1,5, 1:30, 90m or 2h.

If Freshservice rejects a ticket because a mandatory field is missing
(e.g. Platform version), FSTT asks for the value and can save it so you are
not asked again.

Files next to fstt.py: config.toml (settings), state.json (ticket ids),
fstt.log (output of scheduled runs).
At any prompt: b = back, q = quit, ? = this help.
==========================================================================
"""


# ============================================================ small utils
def deep_merge(a, b):
    out = dict(a or {})
    for k, v in (b or {}).items():
        out[k] = deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def hhmm(minutes):
    return f"{minutes // 60}:{minutes % 60:02d}"


def parse_hhmm(s):
    try:
        h, m = (s or "0:0").split(":")[:2]
        return int(h) * 60 + int(m)
    except ValueError:
        return 0


def parse_hours(s):
    """1.5 | 1,5 | 1:30 | 90m | 2h -> minutes"""
    s = s.strip().lower().replace(",", ".")
    if m := re.fullmatch(r"(\d+):(\d{1,2})", s):
        return int(m[1]) * 60 + int(m[2])
    if m := re.fullmatch(r"(\d+(?:\.\d+)?)\s*m(in)?", s):
        return round(float(m[1]))
    if m := re.fullmatch(r"(\d+(?:\.\d+)?)\s*h?", s):
        return round(float(m[1]) * 60)
    raise ValueError(s)


def html_p(text):
    return "<p>" + html.escape(text).replace("\n", "<br>") + "</p>"


def parse_choices(message):
    m = re.search(r"one of these values:\s*'([^']*)'", message or "")
    return [c.strip() for c in m[1].split(",") if c.strip()] if m else []


def week_info(d):
    y, w, _ = d.isocalendar()
    mon = d - dt.timedelta(days=d.weekday())
    fri = mon + dt.timedelta(days=4)
    return {"key": f"{y}-W{w:02d}", "year": y, "week": w, "monday": mon, "friday": fri,
            "start": mon.isoformat(), "end": fri.isoformat(),
            "label": f"Week #{w} ({mon:%b %d} - {fri:%b %d})"}


def week_monday(key):
    y, w = key.split("-W")
    return dt.date.fromisocalendar(int(y), int(w), 1)


# ============================================================ TOML writer (FSTT rewrites its own config)
def _tkey(k):
    return k if re.fullmatch(r"[A-Za-z0-9_-]+", k) else json.dumps(k)


def _tval(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, list):
        return "[" + ", ".join(_tval(x) for x in v) + "]"
    if isinstance(v, dict):
        body = ", ".join(f"{_tkey(k)} = {_tval(x)}" for k, x in v.items() if x is not None)
        return "{ " + body + " }" if body else "{}"
    raise TypeError(type(v))


def dump_toml(cfg):
    out = ["# FSTT configuration - maintained by FSTT (Settings menu). Manual edits are fine; comments are not kept.", ""]
    is_tables = lambda v: isinstance(v, list) and v and all(isinstance(x, dict) for x in v)
    for k, v in cfg.items():
        if v is not None and not isinstance(v, dict) and not is_tables(v):
            out.append(f"{_tkey(k)} = {_tval(v)}")
    for k, v in cfg.items():
        if isinstance(v, dict):
            out += ["", f"[{_tkey(k)}]"] + [f"{_tkey(a)} = {_tval(b)}" for a, b in v.items() if b is not None]
    for k, v in cfg.items():
        if is_tables(v):
            for row in v:
                out += ["", f"[[{_tkey(k)}]]"] + [f"{_tkey(a)} = {_tval(b)}" for a, b in row.items() if b is not None]
    return "\n".join(out) + "\n"


# ============================================================ terminal UI
class Back(Exception):
    pass


class QuitApp(Exception):
    pass


def say(msg=""):
    print(msg)


def title(text):
    say()
    say(f"--- {text} " + "-" * max(3, 66 - len(text)))


def read(prompt):
    try:
        s = input(prompt).strip()
    except EOFError:
        raise QuitApp
    low = s.lower()
    if low in ("?", "help"):
        say(HELP_TEXT)
        return read(prompt)
    if low in ("b", "back"):
        raise Back
    if low in ("q", "quit", "exit"):
        raise QuitApp
    return s


def choose(heading, options, default=None):
    """Single choice -> index."""
    if heading:
        say(heading)
    for i, o in enumerate(options, 1):
        say(f"  {i:>2}) {o}")
    while True:
        d = f" [Enter = {default + 1}]" if default is not None else ""
        s = read(f"Choose 1-{len(options)}{d}: ")
        if not s and default is not None:
            return default
        if s.isdigit() and 1 <= int(s) <= len(options):
            return int(s) - 1
        low = s.lower()
        hits = [i for i, o in enumerate(options) if o.lower() == low] or \
               [i for i, o in enumerate(options) if low and o.lower().startswith(low)]
        if len(hits) == 1:
            return hits[0]
        say("  Please type a number from the list.")


def choose_many(heading, options, preselected=None):
    """Multiple choice -> sorted list of indexes."""
    pre = set(preselected or [])
    say(heading)
    for i, o in enumerate(options):
        say(f"  {i + 1:>2}) [{'x' if i in pre else ' '}] {o}")
    hint = "e.g. 1,3,5-7 | all | none" + (" | Enter = keep [x]" if preselected is not None else "")
    while True:
        s = read(f"Select ({hint}): ")
        if not s and preselected is not None:
            return sorted(pre)
        if s.lower() == "all":
            return list(range(len(options)))
        if s.lower() == "none":
            return []
        try:
            picked = set()
            for part in s.replace(" ", "").split(","):
                if "-" in part:
                    a, b = map(int, part.split("-", 1))
                    picked.update(range(a - 1, b))
                elif part:
                    picked.add(int(part) - 1)
            if picked and all(0 <= i < len(options) for i in picked):
                return sorted(picked)
        except ValueError:
            pass
        say("  Please use numbers from the list, e.g. 1,3 or 2-4.")


def ask_text(prompt, default=None, required=True):
    while True:
        d = f" [Enter = {default}]" if default not in (None, "") else ""
        s = read(f"{prompt}{d}: ")
        if not s and default not in (None, ""):
            return str(default)
        if s or not required:
            return s
        say("  A value is needed (b = back).")


def ask_yes(prompt, default=True):
    while True:
        s = read(f"{prompt} [{'Y/n' if default else 'y/N'}]: ").lower()
        if not s:
            return default
        if s in ("y", "yes"):
            return True
        if s in ("n", "no"):
            return False


def ask_minutes(prompt, required=False):
    while True:
        s = read(f"{prompt}: ")
        if not s and not required:
            return None
        try:
            m = parse_hours(s)
            if m > 0:
                return m
        except ValueError:
            pass
        say("  Hours like 1.5, 1:30, 90m or 2h" + ("" if required else " (Enter = skip)") + ".")


def ask_date(prompt, default):
    while True:
        s = read(f"{prompt} (YYYY-MM-DD) [Enter = {default:%a %Y-%m-%d}]: ")
        if not s:
            return default
        try:
            return dt.date.fromisoformat(s)
        except ValueError:
            say("  Use the format YYYY-MM-DD, e.g. 2026-11-20.")


# ============================================================ API
class ApiError(RuntimeError):
    def __init__(self, status, msg, errors=None):
        super().__init__(msg)
        self.status = status
        self.errors = errors or []


class FS:
    def __init__(self, domain, key, dry_run=False):
        self.base = f"https://{domain}/api/v2"
        self.dry = dry_run
        self.s = requests.Session()
        self.s.auth = (key or "missing", "X")
        self.s.headers["Content-Type"] = "application/json"

    def req(self, method, path, **kw):
        if self.dry and method != "GET":
            say(f"  [dry-run] {method} {path} {json.dumps(kw.get('json', {}))[:400]}")
            return {}
        for attempt in range(5):
            try:
                r = self.s.request(method, self.base + path, timeout=40, **kw)
            except requests.RequestException as e:
                if attempt == 4:
                    raise ApiError(0, f"{method} {path}: network error: {e}")
                time.sleep(3)
                continue
            if r.status_code == 429:
                time.sleep(int(r.headers.get("Retry-After", 30)))
                continue
            if r.status_code >= 400:
                errors = []
                try:
                    errors = r.json().get("errors", []) or []
                except ValueError:
                    pass
                raise ApiError(r.status_code, f"{method} {path} -> {r.status_code}: {r.text[:600]}", errors)
            return r.json() if r.content else {}
        raise ApiError(429, f"{method} {path}: still rate-limited")

    def paged(self, path, key, params=None, max_pages=200):
        for page in range(1, max_pages + 1):
            batch = self.req("GET", path, params={**(params or {}), "per_page": 100, "page": page}).get(key, [])
            yield from batch
            if len(batch) < 100:
                return

    def add_time(self, tid, entry):
        try:
            return self.req("POST", f"/tickets/{tid}/time_entries", json={"time_entry": entry})
        except ApiError as e:
            if e.status == 400 and not e.errors:
                return self.req("POST", f"/tickets/{tid}/time_entries", json=entry)
            raise


# ============================================================ API key storage
def get_api_key():
    k = os.environ.get(ENV_KEY, "").strip()
    if k:
        return k
    if sys.platform == "win32":          # saved with [Environment]::SetEnvironmentVariable(..., "User")
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as h:
                return str(winreg.QueryValueEx(h, ENV_KEY)[0]).strip() or None
        except OSError:
            return None
    if sys.platform == "darwin":         # macOS Keychain
        try:
            r = subprocess.run(["security", "find-generic-password", "-a", getpass.getuser(),
                                "-s", KEYCHAIN_SERVICE, "-w"], capture_output=True, text=True, timeout=15)
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    for f in (LINUX_KEY_FILE, Path.home() / ".config" / "fsw" / "api_key"):
        if f.exists():
            return f.read_text().strip() or None
    return None


def detect_os():
    return {"Windows": 0, "Darwin": 1}.get(platform.system(), 2)


OS_NAMES = ["Windows (PowerShell)", "macOS (Terminal)", "Linux (bash)"]

KEY_WHERE = """Where to find your Freshservice API key:
  1. Open Freshservice in the browser.
  2. Click your profile picture (top right) -> Profile Settings.
  3. On the right: "Your API Key" -> reveal it and copy it."""


def key_instructions(os_idx):
    if os_idx == 0:
        return """Open a NEW PowerShell window and run these two lines. The first one asks for the key
with hidden input, so the key never shows on screen or in your command history:

  $s = Read-Host "Freshservice API key" -AsSecureString
  [Environment]::SetEnvironmentVariable("FRESHSERVICE_API_KEY", [Net.NetworkCredential]::new("", $s).Password, "User")

This stores the key in your Windows user profile (not in any file in this folder).
Scheduled tasks that run as you can read it too."""
    if os_idx == 1:
        return f"""Open Terminal and run this (it asks for the key; input is hidden):

  security add-generic-password -a "$USER" -s {KEYCHAIN_SERVICE} -w

The key goes into your macOS Keychain. If macOS later asks whether python may read it,
choose "Always Allow"."""
    return """Open a terminal and run this (input is hidden; the key goes to a file only you can read):

  mkdir -p ~/.config/fstt && (umask 077; read -rsp "Freshservice API key: " k; printf '%s' "$k" > ~/.config/fstt/api_key); echo"""


# ============================================================ config
def builtin_items():
    return [{"key": ITEM_KEYS.get(did, slug(name)), "name": name, "display_id": did,
             "recurring": False, "resolve_with_week": False,
             "fields": {"question_topic": "Commerce"} if did == 65 else {},
             "prompt": ITEM_PROMPTS.get(did, [])}
            for did, name in sorted(BUILTIN_CATALOG, key=lambda x: x[1].lower())]


def new_config():
    return {
        "version": CONFIG_VERSION,
        "setup_done": False,
        "user": {"name": "", "email": "", "agent_id": 0},
        "freshservice": {"domain": DEFAULT_DOMAIN, "workspace_id": SM_WORKSPACE_ID,
                         "catalog_category_id": SM_CATEGORY_ID},
        "defaults": {"assign_to_me": True, "group_id": 0, "custom_fields": {}, "fields": {}},
        "resolve_update": {},
        "templates": {"recurring_subject": "[{customer}] Week #{week}",
                      "recurring_description": "{item} - {customer}, week #{week} ({start} to {end}).",
                      "adhoc_subject": "[{customer}] {subject}",
                      "default_note": "TAM activities - {customer}"},
        "schedule": {"log_time_local": "17:00", "billable": True},
        "items": builtin_items(),
        "customers": [],
    }


def migrate_v1(old):
    cfg = new_config()
    cfg["user"]["email"] = old.get("me", {}).get("email", "")
    cfg["user"]["agent_id"] = old.get("me", {}).get("agent_id", 0)
    cfg["freshservice"]["domain"] = old.get("freshservice", {}).get("domain", DEFAULT_DOMAIN)
    tu = old.get("ticket_update", {})
    cfg["defaults"]["group_id"] = tu.get("group_id", 0)
    cfg["defaults"]["custom_fields"] = dict(tu.get("custom_fields", {}))
    cfg["resolve_update"] = old.get("resolve_update", {}) or {}
    sch = old.get("schedule", {})
    cfg["schedule"]["log_time_local"] = sch.get("log_time_local", "17:00")
    cfg["schedule"]["billable"] = sch.get("billable", True)
    recurring = old.get("recurring", [])
    if recurring:
        cfg["templates"]["recurring_subject"] = recurring[0].get("subject_template", "[{account}] Week #{week}") \
            .replace("{account}", "{customer}")
    by_did = {i["display_id"]: i for i in cfg["items"]}
    for key, it in old.get("items", {}).items():
        row = by_did.get(it.get("display_id"))
        if row is None:
            row = {"display_id": it.get("display_id"), "name": it.get("name", key)}
            cfg["items"].append(row)
        row.update(key=key, name=it.get("name", row["name"]),
                   fields=it.get("fields", row.get("fields", {})),
                   prompt=it.get("prompt", row.get("prompt", [])),
                   resolve_with_week=it.get("resolve_with_week", False),
                   recurring=any(r.get("item") == key and r.get("accounts") for r in recurring))
    for a in old.get("accounts", []):
        name = a["name"]
        c = {"name": name}
        dep = a.get("department_id", a.get("department"))
        if isinstance(dep, int) or str(dep or "").isdigit():
            c["department_id"] = int(dep)
        cf = (a.get("ticket_update") or {}).get("custom_fields") or {}
        if cf:
            c["custom_fields"] = dict(cf)
        if a.get("note_choices"):
            c["note_choices"] = a["note_choices"]
        auto = [r["item"] for r in recurring if name in r.get("accounts", [])]
        manual = [r["item"] for r in recurring if name in r.get("manual_accounts", [])]
        if manual and not auto:
            c["manual_only"] = True
            if "internal" in name.lower():
                c["internal"] = True
                c["billable"] = False
            c["recurring_items"] = manual
        else:
            c["recurring_items"] = auto
        cfg["customers"].append(c)
    cfg["setup_done"] = True
    return cfg


def load_config():
    if not CFG_PATH.exists():
        return new_config()
    with open(CFG_PATH, "rb") as f:
        raw = tomllib.load(f)
    if raw.get("version") == CONFIG_VERSION:
        base = new_config()
        base.update(raw)
        for k in ("user", "freshservice", "defaults", "templates", "schedule"):
            base[k] = deep_merge(new_config()[k], raw.get(k, {}))
        return base
    cfg = migrate_v1(raw)
    backup = CFG_PATH.with_name(CFG_PATH.stem + ".v1-backup.toml")
    backup.write_bytes(CFG_PATH.read_bytes())
    save_config(cfg)
    say(f"(Your config was converted to the new format; the old one is saved as {backup.name})")
    return cfg


def save_config(cfg):
    tmp = CFG_PATH.with_suffix(".tmp")
    tmp.write_text(dump_toml(cfg), encoding="utf-8")
    tmp.replace(CFG_PATH)


def load_state():
    st = json.loads(STATE_PATH.read_text()) if STATE_PATH.exists() else {}
    st.setdefault("weeks", {})
    st.setdefault("adhoc", {})
    return st


def save_state(state):
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    tmp.replace(STATE_PATH)


# ============================================================ the app
class App:
    def __init__(self, cfg, state, today, dry_run=False, interactive=True):
        self.cfg, self.state, self.today = cfg, state, today
        self.dry, self.interactive = dry_run, interactive
        self._fs = None
        self._depts = None
        self.asked_previous = False

    # ---------------------------------------------------- plumbing
    @property
    def fs(self):
        if self._fs is None:
            key = get_api_key()
            if not key:
                raise ApiError(401, "No Freshservice API key found. Open Settings -> API key.")
            self._fs = FS(self.cfg["freshservice"]["domain"], key, self.dry)
        return self._fs

    def save_cfg(self):
        save_config(self.cfg)

    def save_state(self):
        if not self.dry:
            save_state(self.state)

    def item(self, key):
        return next((i for i in self.cfg["items"] if i["key"] == key), None)

    def customer(self, name):
        if not name:
            return None
        return next((c for c in self.cfg["customers"] if c["name"].lower() == name.lower()), None)

    def customer_by_dept(self, dep_id):
        if not dep_id:
            return None
        return next((c for c in self.cfg["customers"] if c.get("department_id") == dep_id), None)

    def recurring_items(self, cust):
        keys = cust.get("recurring_items")
        if keys is None:
            keys = [i["key"] for i in self.cfg["items"] if i.get("recurring")]
        return [self.item(k) for k in keys if self.item(k)]

    def fmt(self, tpl, **kw):
        kw.setdefault("account", kw.get("customer"))
        return tpl.format(**kw)

    # ---------------------------------------------------- departments (= customers in Freshservice)
    def departments(self):
        if self._depts is None:
            say("  (loading the customer list from Freshservice ...)")
            self._depts = list(self.fs.paged("/departments", "departments"))
        return self._depts

    def search_departments(self, text):
        t = text.lower()
        return [d for d in self.departments() if t in d["name"].lower()]

    def pick_department(self, prompt="Customer name (or part of it) or Freshservice ID", query=None):
        while True:
            q = query or ask_text(prompt)
            query = None
            if q.isdigit():
                try:
                    d = self.fs.req("GET", f"/departments/{q}")["department"]
                except ApiError:
                    say("  No customer with that ID.")
                    continue
                if ask_yes(f"  Found: {d['name']} - is that right?"):
                    return d
                continue
            hits = self.search_departments(q)
            if not hits:
                say(f"  No customer matches '{q}'. Try another part of the name.")
                continue
            shown = hits[:20]
            if len(hits) > 20:
                say(f"  {len(hits)} matches - showing the first 20. Type a longer name to narrow it down.")
            i = choose("Which one?", [f"{d['name']}  (id {d['id']})" for d in shown] + ["None of these - search again"])
            if i < len(shown):
                return shown[i]

    # ---------------------------------------------------- ticket fields + learning from validation errors
    def ticket_fields(self, cust):
        d = self.cfg["defaults"]
        body = {}
        if d.get("assign_to_me", True) and self.cfg["user"].get("agent_id"):
            body["responder_id"] = self.cfg["user"]["agent_id"]
        if d.get("group_id"):
            body["group_id"] = d["group_id"]
        body.update(d.get("fields", {}))
        body.update((cust or {}).get("fields", {}))
        cf = deep_merge(d.get("custom_fields", {}), (cust or {}).get("custom_fields", {}))
        if cf:
            body["custom_fields"] = cf
        if (cust or {}).get("department_id"):
            body["department_id"] = cust["department_id"]
        return body

    def learn(self, errors, body, cust=None, item=None):
        """Ask for values Freshservice says are missing/invalid; optionally save them. True if anything learned."""
        if not self.interactive:
            return False
        learned = False
        for err in errors:
            f = err.get("field") or ""
            msg = err.get("message", "")
            if not f or f == "description":
                continue
            say(f"\n  Freshservice needs a valid value for '{f}': {msg[:160]}")
            choices = parse_choices(msg)
            if choices:
                val = choices[choose(f"  Value for '{f}':", choices)]
            else:
                val = ask_text(f"  Value for '{f}'")
            if f.endswith("_id") and str(val).isdigit():
                val = int(val)
            if item is not None:                          # field of the catalog form
                body.setdefault("custom_fields", {})[f] = val
                if ask_yes(f"  Always use '{val}' for '{f}' on '{item['name']}' requests?"):
                    item.setdefault("fields", {})[f] = val
                    self.save_cfg()
            else:                                         # field of the ticket
                custom = f not in TOP_LEVEL_TICKET_FIELDS
                (body.setdefault("custom_fields", {}) if custom else body)[f] = val
                opts = ["Save as default for all customers"]
                saved_cust = self.customer((cust or {}).get("name"))
                if saved_cust:
                    opts.append(f"Save for {saved_cust['name']} only")
                opts.append("Use for this ticket only")
                pick = opts[choose("  Remember this value?", opts, default=0)]
                target = self.cfg["defaults"] if pick.startswith("Save as default") else \
                    saved_cust if pick.startswith("Save for") else None
                if target is not None:
                    target.setdefault("custom_fields" if custom else "fields", {})[f] = val
                    self.save_cfg()
                    say("  Saved for next time.")
            learned = True
        return learned

    def put_ticket(self, tid, body, cust):
        for _ in range(6):
            try:
                return self.fs.req("PUT", f"/tickets/{tid}", json=body)
            except ApiError as e:
                if e.status == 400 and e.errors and self.learn(e.errors, body, cust=cust):
                    continue
                raise
        raise ApiError(400, f"#{tid}: still rejected after several attempts")

    # ---------------------------------------------------- create
    def place(self, item, cust, subject, description, extra_fields=None):
        body = {"email": self.cfg["user"]["email"],
                "custom_fields": {"subject": subject, "description": description,
                                  **item.get("fields", {}), **(extra_fields or {})}}
        res = None
        for _ in range(6):
            try:
                res = self.fs.req("POST", f"/service_catalog/items/{item['display_id']}/place_request", json=body)
                break
            except ApiError as e:
                if e.status == 400 and e.errors and self.learn(e.errors, body, item=item):
                    continue
                raise
        if self.dry:
            self.fs.req("PUT", "/tickets/<new>", json={**self.ticket_fields(cust), "description": "..."})
            return None
        tid = ((res or {}).get("service_request") or {}).get("id")
        if not tid:
            raise ApiError(0, f"Unexpected response from Freshservice: {res}")
        upd = self.ticket_fields(cust)
        upd["description"] = html_p(description)
        try:
            self.put_ticket(tid, upd, cust)
        except ApiError as e:
            say(f"  ! #{tid} was created, but setting its fields failed: {e}")
        return tid

    def create_week(self, day, customers=None):
        """customers=None -> all non-manual customers."""
        wi = week_info(day)
        wk = self.state["weeks"].setdefault(wi["key"], {"tickets": {}})
        targets = customers if customers is not None else \
            [c for c in self.cfg["customers"] if not c.get("manual_only")]
        existing = None
        say(f"  {wi['label']}")
        for cust in targets:
            for item in self.recurring_items(cust):
                key = f"{item['key']}:{cust['name']}"
                if key in wk["tickets"]:
                    say(f"  exists   #{wk['tickets'][key]:<8} {cust['name']:<14} {item['name']}")
                    continue
                ctx = dict(customer=cust["name"], item=item["name"], week=wi["week"], year=wi["year"],
                           start=wi["start"], end=wi["end"])
                subj = self.fmt(self.cfg["templates"]["recurring_subject"], **ctx)
                if existing is None and not self.dry:
                    existing = list(self.fs.paged("/tickets", "tickets", {
                        "requester_id": self.cfg["user"]["agent_id"],
                        "updated_since": (wi["monday"] - dt.timedelta(days=3)).strftime("%Y-%m-%dT00:00:00Z")},
                        max_pages=10))
                want = f"{item['name']} : {subj}"
                hit = [t for t in (existing or []) if t.get("subject", "").endswith(want)]
                if hit:
                    wk["tickets"][key] = hit[0]["id"]
                    self.save_state()
                    say(f"  found    #{hit[0]['id']:<8} {cust['name']:<14} {item['name']}")
                    continue
                desc = self.fmt(self.cfg["templates"]["recurring_description"], **ctx)
                try:
                    tid = self.place(item, cust, subj, desc)
                except ApiError as e:
                    say(f"  FAILED   {cust['name']:<14} {item['name']}: {e}")
                    continue
                if tid:
                    wk["tickets"][key] = tid
                    self.save_state()
                    say(f"  created  #{tid:<8} {cust['name']:<14} {item['name']}  ({subj})")
                else:
                    say(f"  would create  {cust['name']:<14} {item['name']}  ({subj})")

    def create_adhoc(self, item, cust, subject_text, description, fields):
        prefix = f"[{cust['name']}]"
        if subject_text.lower().startswith(prefix.lower()):
            subject_text = subject_text[len(prefix):].strip()
        subj = self.fmt(self.cfg["templates"]["adhoc_subject"], customer=cust["name"], subject=subject_text)
        tid = self.place(item, cust, subj, description or subject_text, fields)
        if tid:
            self.state["adhoc"][str(tid)] = {"item": item["key"], "account": cust["name"], "subject": subj,
                                             "department_id": cust.get("department_id"),
                                             "created": self.today.isoformat(), "resolved": False}
            self.save_state()
            say(f"  created #{tid}  {subj}")
        return tid

    # ---------------------------------------------------- tracked tickets
    def ticket_meta(self, tid):
        tid = int(tid)
        for wk in self.state["weeks"].values():
            for key, t in wk.get("tickets", {}).items():
                if t == tid:
                    return key.split(":", 1)
        m = self.state["adhoc"].get(str(tid))
        return (m["item"], m["account"]) if m else (None, None)

    def week_tickets(self, day, customer=None, include_adhoc=True):
        """[(tid, item_key, customer, label)] for the week's set plus open one-off tickets."""
        wk = self.state["weeks"].get(week_info(day)["key"], {}).get("tickets", {})
        rows = []
        order = {c["name"].lower(): n for n, c in enumerate(self.cfg["customers"])}
        names = {i["key"]: i["name"] for i in self.cfg["items"]}
        for key, tid in sorted(wk.items(), key=lambda kv: (order.get(kv[0].split(":", 1)[1].lower(), 99),
                                                           names.get(kv[0].split(":", 1)[0], kv[0]))):
            ik, cname = key.split(":", 1)
            if customer and cname.lower() != customer.lower():
                continue
            it = self.item(ik)
            rows.append((tid, ik, cname, f"#{tid:<8} {cname:<14} {it['name'] if it else ik}"))
        if include_adhoc:
            for t, m in self.state["adhoc"].items():
                if m.get("resolved") or (customer and m["account"].lower() != customer.lower()):
                    continue
                rows.append((int(t), m["item"], m["account"], f"#{t:<8} {m['account']:<14} {m['subject']}  (one-off)"))
        return rows

    # ---------------------------------------------------- time
    def executed_at(self, day):
        hh, mm = map(int, self.cfg["schedule"].get("log_time_local", "17:00").split(":"))
        return dt.datetime.combine(day, dt.time(hh, mm)).astimezone().astimezone(dt.timezone.utc) \
            .strftime("%Y-%m-%dT%H:%M:%SZ")

    def log(self, tid, customer, day, minutes, note):
        cust = self.customer(customer) or {}
        billable = cust.get("billable", self.cfg["schedule"].get("billable", True))
        entry = {"agent_id": self.cfg["user"]["agent_id"], "time_spent": f"{minutes // 60:02d}:{minutes % 60:02d}",
                 "executed_at": self.executed_at(day), "billable": billable, "timer_running": False,
                 "note": note or self.fmt(self.cfg["templates"]["default_note"], customer=customer)}
        self.fs.add_time(tid, entry)
        say(f"  logged {hhmm(minutes)}h on #{tid} ({customer}) for {day:%a %Y-%m-%d}")

    def logged_minutes(self, tid):
        entries = self.fs.req("GET", f"/tickets/{tid}/time_entries").get("time_entries", [])
        return sum(parse_hhmm(e.get("time_spent")) for e in entries)

    # ---------------------------------------------------- resolve
    def resolve_ticket(self, tid):
        t = self.fs.req("GET", f"/tickets/{tid}")["ticket"]
        _, cname = self.ticket_meta(tid)
        if t.get("status") in (4, 5):
            say(f"  already  #{tid} {cname or ''} ({STATUS_NAMES.get(t['status'])})")
            self._mark_adhoc_resolved(tid)
            return True
        cust = self.customer(cname) or self.customer_by_dept(t.get("department_id"))
        desc = (t.get("description_text") or "").strip()
        if desc.lower() in PLACEHOLDER_DESC:
            desc = t.get("subject", "")
        body = self.ticket_fields(cust)
        body["description"] = html_p(desc)
        body["status"] = STATUS_RESOLVED
        body = deep_merge(body, self.cfg.get("resolve_update", {}))
        try:
            self.put_ticket(tid, body, cust)
        except ApiError as e:
            hint = "" if self.interactive else "  -> run FSTT (no arguments) and resolve this week to fill in the value"
            say(f"  FAILED   #{tid} {cname or ''}: {e}{hint}")
            return False
        say(f"  resolved #{tid} {cname or ''}")
        self._mark_adhoc_resolved(tid)
        return True

    def _mark_adhoc_resolved(self, tid):
        if str(tid) in self.state["adhoc"]:
            self.state["adhoc"][str(tid)]["resolved"] = True
            self.save_state()

    def resolve_week(self, wkey, customer=None):
        wk = self.state["weeks"].get(wkey, {})
        ids = [r[0] for r in self.week_tickets(week_monday(wkey), customer, include_adhoc=False)]
        if not customer:
            weekly = {i["key"] for i in self.cfg["items"] if i.get("resolve_with_week")}
            ids += [int(t) for t, m in self.state["adhoc"].items() if not m.get("resolved") and m["item"] in weekly]
        if not ids:
            say(f"  {wkey}: nothing to resolve")
            return True
        ok = all([self.resolve_ticket(t) for t in ids])
        if ok and not customer and wk:
            wk["resolved"] = True
            self.save_state()
        return ok

    def open_previous_weeks(self):
        this = week_info(self.today)["key"]
        return sorted(k for k, v in self.state["weeks"].items()
                      if k < this and not v.get("resolved") and v.get("tickets"))


# ============================================================ wizard helpers
def ask_week(app, prompt="Which week?", default=None):
    dw = week_info(default or app.today)
    while True:
        s = read(f"{prompt} [Enter = {dw['label']} | p = previous | n = next | week no. | a date]: ").lower()
        if not s:
            return dw["monday"]
        if s in ("p", "prev", "previous", "last"):
            return dw["monday"] - dt.timedelta(days=7)
        if s in ("n", "next"):
            return dw["monday"] + dt.timedelta(days=7)
        m = re.fullmatch(r"w?#?(\d{1,2})", s)
        if m and 1 <= int(m[1]) <= 53:
            try:
                return dt.date.fromisocalendar(app.today.isocalendar()[0], int(m[1]), 1)
            except ValueError:
                pass
        try:
            d = dt.date.fromisoformat(s)
            return d - dt.timedelta(days=d.weekday())
        except ValueError:
            say("  Examples: Enter, p, n, 47, 2026-11-12")


def short_name(dept_name):
    n = re.sub(r"^\[[^\]]*\]\s*", "", dept_name).strip()
    n = re.sub(r",?\s+(inc\.?|llc|ltd\.?|corp\.?|gmbh)\b.*$", "", n, flags=re.I).strip()
    return n or dept_name


def pick_customer(app, heading="Which customer?", allow_other=False):
    custs = app.cfg["customers"]
    opts = [c["name"] + ("  (internal)" if c.get("internal") else "") for c in custs]
    if allow_other:
        opts.append("Other customer (search Freshservice)")
    if not opts:
        say("  No customers configured yet (Settings -> Customers).")
        raise Back
    i = choose(heading, opts)
    if i < len(custs):
        return custs[i]
    d = app.pick_department()
    name = ask_text("  Short name for ticket subjects", default=short_name(d["name"]))
    cust = {"name": name, "department_id": d["id"], "department_name": d["name"]}
    if ask_yes(f"  Add {name} to your customer list for next time?", default=False):
        cust.update(recurring_items=[], manual_only=True)
        app.cfg["customers"].append(cust)
        app.save_cfg()
    return cust


def ask_note(app, customer):
    choices = (app.customer(customer) or {}).get("note_choices")
    default = app.fmt(app.cfg["templates"]["default_note"], customer=customer)
    if choices:
        opts = choices + ["Other (type it)", f"Default note ({default})"]
        i = choose("  What was it?", opts, default=len(opts) - 1)
        if opts[i].startswith("Default note"):
            return None
        note = ask_text("  Note") if opts[i] == "Other (type it)" else opts[i]
        extra = ask_text("  Details (Enter = none)", required=False)
        return f"{note} - {extra}" if extra else note
    return ask_text(f"  Note (Enter = \"{default}\")", required=False) or None


# ============================================================ main menu
def main_menu(app):
    while True:
        wi = week_info(app.today)
        title(f"Hi {app.cfg['user'].get('name') or 'there'}! {app.today:%a %Y-%m-%d} - {wi['label']}")
        if not app.asked_previous:
            app.asked_previous = True
            prev = app.open_previous_weeks()
            if prev:
                say(f"  Not resolved yet: {', '.join(prev)}")
                try:
                    if ask_yes("  Resolve them now?"):
                        for k in prev:
                            say(f"  {k}:")
                            app.resolve_week(k)
                except Back:
                    pass
                except ApiError as e:
                    say(f"  Freshservice error: {e}")
        actions = [("Create tickets", menu_create), ("Log time", menu_log),
                   ("Tickets with no time logged yet", menu_missing_time), ("Status of a week", menu_status),
                   ("Resolve tickets", menu_resolve), ("Settings", menu_settings), ("Help", None), ("Quit", None)]
        i = choose("What would you like to do?  (b = back, q = quit, ? = help)", [a for a, _ in actions])
        label, fn = actions[i]
        if label == "Quit":
            raise QuitApp
        if label == "Help":
            say(HELP_TEXT)
            continue
        try:
            fn(app)
        except Back:
            continue
        except ApiError as e:
            say(f"\n  Freshservice error: {e}")


# ---------------------------------------------------------------- create
def menu_create(app):
    title("Create tickets")
    i = choose("What do you want to create?", [
        "This week's ticket set for all customers",
        "Ticket set for selected customers and/or another week",
        "One custom ticket (any ticket type, any customer)"])
    if i == 0:
        preview_and_create(app, app.today, None)
    elif i == 1:
        day = ask_week(app)
        custs = app.cfg["customers"]
        sel = choose_many("Which customers?", [c["name"] + ("  (manual only)" if c.get("manual_only") else "")
                                              for c in custs],
                          preselected=[n for n, c in enumerate(custs) if not c.get("manual_only")])
        if sel:
            preview_and_create(app, day, [custs[n] for n in sel])
    else:
        create_custom(app)


def preview_and_create(app, day, customers):
    wi = week_info(day)
    targets = customers if customers is not None else [c for c in app.cfg["customers"] if not c.get("manual_only")]
    plan = [(c["name"], it["name"]) for c in targets for it in app.recurring_items(c)]
    if not plan:
        say("  No recurring ticket types are set for these customers (Settings -> Recurring ticket types).")
        return
    say(f"\n{wi['label']}: {len(plan)} ticket(s) - ones that already exist are skipped:")
    for c, it in plan:
        say(f"   - {c:<14} {it}")
    if ask_yes("Go ahead?"):
        app.create_week(day, customers)


def create_custom(app):
    cust = pick_customer(app, allow_other=True)
    items = sorted(app.cfg["items"], key=lambda x: (not x.get("recurring"), x["name"].lower()))
    item = items[choose("Which ticket type?", [it["name"] for it in items])]
    subject = ask_text("Subject")
    description = ask_text("Description", default=subject)
    fields = {}
    for f in item.get("prompt", []):
        req = f.get("required", False)
        label = f.get("label", f["name"]) + ("" if req else " (optional)")
        default = item.get("fields", {}).get(f["name"])
        if f.get("choices"):
            opts = list(f["choices"]) + ([] if req else ["(skip)"])
            val = opts[choose(label, opts, default=opts.index(default) if default in opts else None)]
            if val != "(skip)":
                fields[f["name"]] = val
        else:
            val = ask_text(label, default=default, required=req)
            if val:
                fields[f["name"]] = val
    say(f"\n  {item['name']} for {cust['name']}: [{cust['name']}] {subject}")
    if not ask_yes("Create it?"):
        return
    tid = app.create_adhoc(item, cust, subject, description, fields)
    if tid and ask_yes("Log time on it now?", default=False):
        day = ask_date("  Date", app.today)
        minutes = ask_minutes("  Hours", required=True)
        app.log(tid, cust["name"], day, minutes, ask_note(app, cust["name"]))


# ---------------------------------------------------------------- log time
def menu_log(app):
    title("Log time")
    i = choose("Log time for ...", ["One customer", "All open tickets of a week (one by one)"])
    day = ask_week(app)
    wi = week_info(day)
    if i == 0:
        log_one_customer(app, day)
        return
    rows = app.week_tickets(day)
    if not app.state["weeks"].get(wi["key"], {}).get("tickets"):
        say(f"  The ticket set for {wi['label']} has not been created yet.")
        if ask_yes("  Create it now?"):
            app.create_week(day)
            rows = app.week_tickets(day)
    if not rows:
        return
    log_date = ask_date("Log the time on which date?", wi["friday"])
    say("Enter hours for each ticket (Enter = skip).")
    for tid, _, cname, label in rows:
        minutes = ask_minutes(f"  {label}  hours")
        if minutes:
            app.log(tid, cname, log_date, minutes, ask_note(app, cname))


def log_one_customer(app, day):
    wi = week_info(day)
    cust = pick_customer(app)
    while True:
        rows = app.week_tickets(day, cust["name"])
        if not rows:
            if app.recurring_items(cust) and ask_yes(f"  {cust['name']} has no tickets in {wi['label']}. Create them now?"):
                app.create_week(day, [cust])
                rows = app.week_tickets(day, cust["name"])
            if not rows:
                say("  Nothing to log on. Create a custom ticket first (Create tickets).")
                return
        if len(rows) == 1:
            tid, _, cname, label = rows[0]
            say(f"  Ticket: {label}")
        else:
            tid, _, cname, label = rows[choose("Which ticket?", [r[3] for r in rows])]
        log_date = ask_date("  Date", wi["friday"])
        minutes = ask_minutes("  Hours", required=True)
        app.log(tid, cname, log_date, minutes, ask_note(app, cname))
        if not ask_yes(f"Log more for {cust['name']}?", default=False):
            return


# ---------------------------------------------------------------- missing time
def menu_missing_time(app):
    title("Tickets with no time logged yet")
    day = ask_week(app)
    wi = week_info(day)
    rows = app.week_tickets(day)
    if not rows:
        say(f"  No tickets tracked for {wi['label']}.")
        return
    say("  Checking time entries ...")
    empty = [r for r in rows if app.logged_minutes(r[0]) == 0]
    if not empty:
        say(f"  All {len(rows)} ticket(s) of {wi['label']} have time logged.")
        return
    say(f"\n{len(empty)} of {len(rows)} ticket(s) have no time yet:")
    for r in empty:
        say(f"   {r[3]}")
    if not ask_yes("Log time for them now?"):
        return
    log_date = ask_date("Log the time on which date?", wi["friday"])
    say("Enter hours (Enter = skip).")
    for tid, _, cname, label in empty:
        minutes = ask_minutes(f"  {label}  hours")
        if minutes:
            app.log(tid, cname, log_date, minutes, ask_note(app, cname))


# ---------------------------------------------------------------- status
def menu_status(app):
    title("Status")
    day = ask_week(app)
    wi = week_info(day)
    rows = app.week_tickets(day)
    if not rows:
        say(f"  No tickets tracked for {wi['label']}.")
        return
    say(f"\n{wi['label']}")
    total = 0
    for tid, _, _, label in rows:
        t = app.fs.req("GET", f"/tickets/{tid}")["ticket"]
        mins = app.logged_minutes(tid)
        total += mins
        say(f"  {STATUS_NAMES.get(t['status'], t['status']):<9} {hhmm(mins):>6}h  {label}")
    say(f"  {'':<9} {hhmm(total):>6}h  total")


# ---------------------------------------------------------------- resolve
def menu_resolve(app):
    title("Resolve tickets")
    prev = app.open_previous_weeks()
    default_day = week_monday(prev[0]) if prev else app.today
    i = choose("What do you want to resolve?", ["All tickets of a week", "One customer's tickets in a week",
                                                "One ticket"])
    if i == 0:
        wi = week_info(ask_week(app, default=default_day))
        if ask_yes(f"Resolve all tickets of {wi['label']}?"):
            app.resolve_week(wi["key"])
    elif i == 1:
        cust = pick_customer(app)
        app.resolve_week(week_info(ask_week(app, default=default_day))["key"], cust["name"])
    else:
        rows = app.week_tickets(app.today)
        for k in prev:
            rows = app.week_tickets(week_monday(k), include_adhoc=False) + rows
        opts = [r[3] for r in rows] + ["Type a ticket number"]
        j = choose("Which ticket?", opts)
        if j < len(rows):
            tid = rows[j][0]
        else:
            raw = ask_text("Ticket number")
            if not raw.lstrip("#").isdigit():
                say("  Not a ticket number.")
                return
            tid = int(raw.lstrip("#"))
        app.resolve_ticket(tid)


# ============================================================ settings / setup
def menu_settings(app):
    steps = [("Your name and email", step_name), ("API key - check or replace", step_api),
             ("Customers - add / remove / rename", step_customers),
             ("Ticket field values (environment, platform version, ...)", step_fields),
             ("Recurring ticket types", step_items),
             ("CloudBlue Internal Company (internal meetings)", step_internal),
             ("Scheduled tasks - setup instructions", step_schedule), ("Where are my files?", show_files),
             ("Back to main menu", None)]
    while True:
        title("Settings")
        i = choose(None, [s for s, _ in steps])
        if steps[i][1] is None:
            return
        try:
            steps[i][1](app)
        except Back:
            pass


def show_files(app):
    say(f"  config: {CFG_PATH}\n  state:  {STATE_PATH}\n  log:    {HERE / 'fstt.log'}")


def run_setup(app, first=True):
    title("Welcome to FSTT - let's set things up (about 5 minutes)" if first else "Setup")
    say("Each step is saved as you go; you can stop with q and continue later.")
    for step in (step_name, step_api, step_customers, step_fields, step_items, step_internal, step_schedule):
        while True:
            try:
                step(app)
                break
            except Back:
                say("  (going back is not possible during setup - repeating this step)")
    app.cfg["setup_done"] = True
    app.save_cfg()
    say("\nSetup complete. Everything can be changed later under Settings.")


def step_name(app):
    title("1. About you")
    u = app.cfg["user"]
    u["name"] = ask_text("Your first name (used to greet you)", default=u.get("name") or None)
    u["email"] = ask_text("Your Freshservice login email", default=u.get("email") or None)
    app.save_cfg()


def normalize_domain(raw):
    d = re.sub(r"^https?://", "", raw.strip()).split("/")[0].strip().lower()
    return d


def check_key(domain, key):
    """-> 'ok' | 'bad_key' | 'bad_address' | 'error:<text>'"""
    try:
        FS(domain, key).req("GET", "/tickets", params={"per_page": 1})
        return "ok"
    except ApiError as e:
        if e.status in (401, 403):
            return "bad_key"
        if e.status in (0, 404):
            return "bad_address"
        return f"error:{e}"


def ask_domain(app):
    fs_cfg = app.cfg["freshservice"]
    say("Freshservice API address: the name ending in .freshservice.com (not the customer portal address).")
    while True:
        d = normalize_domain(ask_text("Freshservice API address", default=fs_cfg.get("domain") or DEFAULT_DOMAIN))
        if not d.endswith(".freshservice.com") and not ask_yes(
                f"  '{d}' does not end in .freshservice.com - portal addresses usually don't serve the API. "
                f"Use it anyway?", default=False):
            continue
        fs_cfg["domain"] = d
        app.save_cfg()
        return d


def step_api(app):
    title("2. Freshservice API key")
    domain = ask_domain(app)
    while True:
        key = get_api_key()
        if key:
            say(f"  Found a saved API key - checking it against {domain} ...")
            result = check_key(domain, key)
            if result == "ok":
                say("  The key works.")
                if ask_yes("  Keep using it?", default=True):
                    app._fs = FS(domain, key, app.dry)
                    break
            elif result == "bad_address":
                say(f"  {domain} does not answer API calls (not found / unreachable). The key was not checked.")
                say(f"  Use your *.freshservice.com address, e.g. {DEFAULT_DOMAIN}.")
                domain = ask_domain(app)
                continue
            elif result == "bad_key":
                say("  Freshservice rejected the key (401/403). Please save a new one.")
            else:
                say(f"  Could not check the key: {result[6:]}")
                if ask_yes("  Try again?", default=True):
                    continue
        say()
        say(KEY_WHERE)
        say()
        os_idx = choose("Which system are you on?", OS_NAMES, default=detect_os())
        say()
        say(key_instructions(os_idx))
        say("\nDo NOT paste the key here - FSTT reads it from where you saved it.")
        read("Press Enter when you have saved it (q = quit): ")
        app._fs = None
    find_me(app)


def find_me(app):
    u = app.cfg["user"]
    agent = None
    for path, key in (("/agents", "agents"), ("/requesters", "requesters")):
        try:
            rows = app.fs.req("GET", path, params={"email": u["email"]}).get(key, [])
            if rows:
                agent = rows[0]
                break
        except ApiError:
            continue
    if agent:
        u["agent_id"] = agent["id"]
        say(f"  Found you in Freshservice: {agent.get('first_name', '')} {agent.get('last_name', '')} (id {agent['id']})")
    else:
        say("  Could not find you by email. Open your profile in Freshservice; the number at the end of the URL is your id.")
        while True:
            raw = ask_text("  Your Freshservice user id", default=u.get("agent_id") or None)
            if raw.isdigit():
                u["agent_id"] = int(raw)
                break
    app.save_cfg()
    learn_from_recent(app)


def learn_from_recent(app):
    """Propose group / field values from one of your recent tickets."""
    d = app.cfg["defaults"]
    if d.get("group_id") and d.get("custom_fields"):
        return
    try:
        since = (app.today - dt.timedelta(days=120)).strftime("%Y-%m-%dT00:00:00Z")
        rows = list(app.fs.paged("/tickets", "tickets", {"requester_id": app.cfg["user"]["agent_id"],
                                                         "updated_since": since}, max_pages=2))
        mine = [t for t in rows if t.get("responder_id") == app.cfg["user"]["agent_id"] and t.get("group_id")]
        if not mine:
            return
        t = app.fs.req("GET", f"/tickets/{mine[0]['id']}")["ticket"]
    except ApiError:
        return
    if not d.get("group_id"):
        d["group_id"] = t.get("group_id") or 0
    known = {f["name"] for f in BUILTIN_CLOSURE_FIELDS}
    for k, v in (t.get("custom_fields") or {}).items():
        if k in known and v and k not in d.setdefault("custom_fields", {}):
            d["custom_fields"][k] = v
    app.save_cfg()
    say(f"  Took group and field suggestions from your recent ticket #{t['id']} (you confirm them in step 4).")


def closure_fields(app):
    try:
        fields = app.fs.req("GET", "/ticket_form_fields").get("ticket_fields", [])
    except ApiError:
        return BUILTIN_CLOSURE_FIELDS
    out = []
    for f in fields:
        if f.get("default_field") or not (f.get("required_for_closure") or f.get("required_for_agents")):
            continue
        choices = []
        for c in f.get("choices") or []:
            choices.append(c.get("value") if isinstance(c, dict) else (c[0] if isinstance(c, list) else str(c)))
        name = re.sub(r"^cf_", "", f.get("name", ""))
        builtin = next((b for b in BUILTIN_CLOSURE_FIELDS if b["name"] == name), {})
        out.append({"name": name, "label": f.get("label", name),
                    "choices": [c for c in choices if c] or builtin.get("choices"), "default": builtin.get("default")})
    names = {f["name"] for f in out}
    return out + [b for b in BUILTIN_CLOSURE_FIELDS if b["name"] not in names]


def step_fields(app):
    title("4. Ticket field values")
    say("These are filled in on every ticket FSTT creates; Freshservice needs them to resolve tickets.")
    d = app.cfg["defaults"]
    g = ask_text("Group id to assign your tickets to (0 = none)", default=str(d.get("group_id") or 0))
    d["group_id"] = int(g) if g.isdigit() else 0
    d["assign_to_me"] = ask_yes("Assign the tickets to you?", default=d.get("assign_to_me", True))
    cf = d.setdefault("custom_fields", {})
    fields = closure_fields(app)
    for f in fields:
        cur = cf.get(f["name"], f.get("default"))
        if f.get("choices"):
            opts = list(f["choices"])
            cf[f["name"]] = opts[choose(f"{f['label']} (default for all customers):", opts,
                                        default=opts.index(cur) if cur in opts else None)]
        else:
            cf[f["name"]] = ask_text(f"{f['label']} (default for all customers)", default=cur)
    app.save_cfg()
    custs = app.cfg["customers"]
    while custs and ask_yes("Does a customer need a different value (e.g. its own platform version)?", default=False):
        c = custs[choose("Which customer?", [x["name"] for x in custs])]
        f = fields[choose("Which field?", [x["label"] for x in fields])]
        cur = c.get("custom_fields", {}).get(f["name"], cf.get(f["name"]))
        if f.get("choices"):
            opts = list(f["choices"])
            val = opts[choose(f"{f['label']} for {c['name']}:", opts, default=opts.index(cur) if cur in opts else None)]
        else:
            val = ask_text(f"{f['label']} for {c['name']}", default=cur)
        c.setdefault("custom_fields", {})[f["name"]] = val
        app.save_cfg()
        say(f"  {c['name']}: {f['label']} = {val}")


def step_customers(app):
    title("3. Customers you track time for")
    custs = app.cfg["customers"]
    while True:
        if custs:
            say("Your customers:")
            for c in custs:
                extra = ", ".join(f"{k}={v}" for k, v in c.get("custom_fields", {}).items())
                flag = " (internal)" if c.get("internal") else (" (manual only)" if c.get("manual_only") else "")
                say(f"   - {c['name']:<16} id {c.get('department_id', '?')}{flag}{'   ' + extra if extra else ''}")
        else:
            say("No customers yet.")
        opts = ["Add a customer", "Remove a customer", "Rename a customer (short name in subjects)", "Done"]
        i = choose("", opts, default=3 if custs else 0)
        if i == 3:
            app.save_cfg()
            return
        if i == 0:
            d = app.pick_department("Customer name (or part of it) or Freshservice ID")
            if app.customer_by_dept(d["id"]):
                say("  Already in your list.")
                continue
            name = ask_text("  Short name used in ticket subjects", default=short_name(d["name"]))
            custs.append({"name": name, "department_id": d["id"], "department_name": d["name"]})
            say(f"  Added {name}.")
        elif i == 1 and custs:
            n = choose("Remove which?", [c["name"] for c in custs])
            if ask_yes(f"  Remove {custs[n]['name']}?", default=False):
                custs.pop(n)
        elif i == 2 and custs:
            n = choose("Rename which?", [c["name"] for c in custs])
            say("  Tickets created before keep their old subject.")
            custs[n]["name"] = ask_text("  New short name", default=custs[n]["name"])
        app.save_cfg()


def fetch_catalog(app):
    try:
        cat = app.cfg["freshservice"]["catalog_category_id"]
        rows = [r for r in app.fs.paged("/service_catalog/items", "service_items", {"category_id": cat}, max_pages=5)
                if r.get("category_id") == cat]
        if rows:
            return [(r["display_id"], r["name"]) for r in rows]
    except ApiError:
        pass
    return BUILTIN_CATALOG


def sync_items(app):
    have = {i["display_id"] for i in app.cfg["items"]}
    for did, name in fetch_catalog(app):
        if did not in have:
            app.cfg["items"].append({"key": ITEM_KEYS.get(did, slug(name)), "name": name, "display_id": did,
                                     "recurring": False, "resolve_with_week": False, "fields": {},
                                     "prompt": ITEM_PROMPTS.get(did, [])})
    app.cfg["items"].sort(key=lambda i: i["name"].lower())


def step_items(app):
    title("5. Recurring ticket types")
    say("Which Service Management ticket types should be created every week for each customer?")
    sync_items(app)
    items = app.cfg["items"]
    sel = set(choose_many("Ticket types:", [i["name"] for i in items],
                          preselected=[n for n, i in enumerate(items) if i.get("recurring")]))
    for n, it in enumerate(items):
        it["recurring"] = n in sel
    for it in items:
        if not it["recurring"]:
            continue
        for p in it.get("prompt", []):
            if p.get("required") and p.get("choices"):
                cur = it.setdefault("fields", {}).get(p["name"])
                it["fields"][p["name"]] = p["choices"][choose(
                    f"'{it['name']}' needs '{p['label']}' - value for the weekly tickets:", p["choices"],
                    default=p["choices"].index(cur) if cur in p["choices"] else None)]
            elif p.get("required") and not it.get("fields", {}).get(p["name"]):
                it.setdefault("fields", {})[p["name"]] = ask_text(
                    f"'{it['name']}' needs '{p['label']}' - value for the weekly tickets")
    regular = [c for c in app.cfg["customers"] if not c.get("manual_only")]
    rec = [i for i in items if i.get("recurring")]
    if regular and rec:
        if ask_yes("Create the same ticket types for every customer?", default=True):
            for c in regular:
                c.pop("recurring_items", None)
        else:
            for c in regular:
                cur = c.get("recurring_items")
                pre = [n for n, i in enumerate(rec) if cur is None or i["key"] in cur]
                pick = choose_many(f"Ticket types for {c['name']}:", [i["name"] for i in rec], preselected=pre)
                c["recurring_items"] = [rec[n]["key"] for n in pick]
    app.save_cfg()


def step_internal(app):
    title("6. CloudBlue Internal Company")
    say("Track internal work (syncs, monthly updates, roadmaps, PI planning, team meetings, 1-1s, ...)")
    say("on one weekly ticket under 'CloudBlue Internal Company'. It is created only when you ask for it")
    say("or log time on it - never by the automatic weekly run.")
    existing = next((c for c in app.cfg["customers"] if c.get("internal")), None)
    q = f"Keep tracking internal time ({existing['name']})?" if existing else "Track internal time?"
    if not ask_yes(q, default=True):
        if existing:
            app.cfg["customers"].remove(existing)
            app.save_cfg()
        return
    cust = existing
    if not cust:
        hits = app.search_departments("CloudBlue Internal Company")
        if len(hits) == 1 and ask_yes(f"  Use '{hits[0]['name']}' (id {hits[0]['id']})?"):
            d = hits[0]
        else:
            d = app.pick_department("Internal company name or ID", query="CloudBlue Internal")
        cust = {"name": "Internal", "department_id": d["id"], "department_name": d["name"],
                "internal": True, "manual_only": True}
        app.cfg["customers"].append(cust)
    cust["name"] = ask_text("Short name for its ticket subjects", default=cust.get("name", "Internal"))
    items = app.cfg["items"]
    cur = (cust.get("recurring_items") or ["wor"])[0]
    idx = next((n for n, i in enumerate(items) if i["key"] == cur), None)
    cust["recurring_items"] = [items[choose("Ticket type for the internal weekly ticket:",
                                            [i["name"] for i in items], default=idx)]["key"]]
    current = cust.get("note_choices") or INTERNAL_NOTE_CHOICES
    say("Call types offered when you log internal time:")
    for c in current:
        say(f"   - {c}")
    if ask_yes("Use these? (n = type your own list)", default=True):
        cust["note_choices"] = list(current)
    else:
        raw = ask_text("Call types, separated by ;")
        cust["note_choices"] = [x.strip() for x in raw.split(";") if x.strip()]
    cust["billable"] = ask_yes("Should internal time be billable?", default=cust.get("billable", False))
    app.save_cfg()


# ---------------------------------------------------------------- scheduled tasks
def step_schedule(app):
    title("7. Scheduled tasks")
    say("Suggested tasks (each one runs at the next start-up if the computer was off at that time):")
    say("  - Friday 3:00 PM : opens FSTT so you can log the week's time")
    say("  - Monday 9:00 AM : resolves last week's tickets")
    say("  - Monday 9:05 AM : creates this week's ticket set")
    if not ask_yes("Do you want to set these up?", default=True):
        return
    os_idx = choose("Which system are you on?", OS_NAMES, default=detect_os())
    text = schedule_instructions(os_idx)
    out = HERE / ["fstt-schedule-windows.ps1", "fstt-schedule-macos.sh", "fstt-schedule-linux.sh"][os_idx]
    out.write_text(text, encoding="utf-8")
    say("\nOpen a separate " + ["PowerShell", "Terminal", "terminal"][os_idx] + " window and paste the following:\n")
    say(text)
    say(f"(Also saved to {out})")
    read("Press Enter to continue: ")


def schedule_instructions(os_idx):
    py, d = sys.executable, str(HERE)
    if os_idx == 0:
        q = lambda s: s.replace("'", "''")
        return f"""# FSTT scheduled tasks - run in PowerShell as yourself (no admin needed)
$py  = '{q(py)}'
$dir = '{q(d)}'
$set = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
# remove the tasks of the old fsw version
Unregister-ScheduledTask -TaskName 'fsw-auto','fsw-reminder','fsw-log-time','fsw-resolve-previous','fsw-create-week' -Confirm:$false -ErrorAction SilentlyContinue

# Friday 3 PM - opens FSTT to log the week's time
Register-ScheduledTask -TaskName 'fstt-log-time' -Settings $set -Force `
  -Trigger (New-ScheduledTaskTrigger -Weekly -DaysOfWeek Friday -At 3pm) `
  -Action (New-ScheduledTaskAction -Execute 'powershell.exe' -WorkingDirectory $dir `
           -Argument "-NoExit -Command & '$py' fstt.py remind")

# Monday 9:00 AM - resolve previous week(s)
Register-ScheduledTask -TaskName 'fstt-resolve-previous' -Settings $set -Force `
  -Trigger (New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday -At 9am) `
  -Action (New-ScheduledTaskAction -Execute 'cmd.exe' -WorkingDirectory $dir `
           -Argument "/c `"`"$py`" fstt.py resolve-previous >> fstt.log 2>&1`"")

# Monday 9:05 AM - create this week's ticket set
Register-ScheduledTask -TaskName 'fstt-create-week' -Settings $set -Force `
  -Trigger (New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday -At 9:05am) `
  -Action (New-ScheduledTaskAction -Execute 'cmd.exe' -WorkingDirectory $dir `
           -Argument "/c `"`"$py`" fstt.py create-week >> fstt.log 2>&1`"")

# Test one now:  Start-ScheduledTask fstt-create-week ; Get-Content "$dir\\fstt.log" -Tail 20
"""
    if os_idx == 1:
        la = "~/Library/LaunchAgents"
        e = html.escape

        def plist(label, weekday, hour, minute, args):
            a = "".join(f"<string>{e(x)}</string>" for x in args)
            return f"""cat > {la}/{label}.plist <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key><array>{a}</array>
  <key>WorkingDirectory</key><string>{e(d)}</string>
  <key>StartCalendarInterval</key><dict><key>Weekday</key><integer>{weekday}</integer><key>Hour</key><integer>{hour}</integer><key>Minute</key><integer>{minute}</integer></dict>
  <key>StandardOutPath</key><string>{e(d)}/fstt.log</string>
  <key>StandardErrorPath</key><string>{e(d)}/fstt.log</string>
</dict></plist>
EOF
launchctl unload {la}/{label}.plist 2>/dev/null; launchctl load {la}/{label}.plist
"""
        remind = f"tell application \"Terminal\" to do script \"cd '{d}' && '{py}' fstt.py remind\""
        return ("# FSTT scheduled tasks for macOS (launchd). A run missed while the Mac sleeps runs on wake.\n"
                f"mkdir -p {la}\n"
                + plist("com.fstt.log-time", 5, 15, 0, ["/usr/bin/osascript", "-e", remind])
                + plist("com.fstt.resolve-previous", 1, 9, 0, [py, "fstt.py", "resolve-previous"])
                + plist("com.fstt.create-week", 1, 9, 5, [py, "fstt.py", "create-week"])
                + "# Test one now: launchctl start com.fstt.create-week ; tail fstt.log\n")
    unit = "~/.config/systemd/user"

    def svc(name, cal, cmd):
        return f"""cat > {unit}/{name}.service <<'EOF'
[Unit]
Description={name}
[Service]
Type=oneshot
WorkingDirectory={d}
ExecStart={cmd}
EOF
cat > {unit}/{name}.timer <<'EOF'
[Timer]
OnCalendar={cal}
Persistent=true
[Install]
WantedBy=timers.target
EOF
"""
    return ("# FSTT scheduled tasks for Linux (systemd user timers; Persistent=true catches up after boot)\n"
            f"mkdir -p {unit}\n"
            + svc("fstt-log-time", "Fri 15:00",
                  f"/usr/bin/notify-send \"FSTT\" \"Time to log your hours: cd {d} && {py} fstt.py remind\"")
            + svc("fstt-resolve-previous", "Mon 09:00", f"/bin/sh -c '{py} fstt.py resolve-previous >> fstt.log 2>&1'")
            + svc("fstt-create-week", "Mon 09:05", f"/bin/sh -c '{py} fstt.py create-week >> fstt.log 2>&1'")
            + "systemctl --user daemon-reload\n"
            + "systemctl --user enable --now fstt-log-time.timer fstt-resolve-previous.timer fstt-create-week.timer\n"
            + "# Test one now: systemctl --user start fstt-create-week ; tail fstt.log\n")


# ============================================================ entry point
def main():
    p = argparse.ArgumentParser(prog="fstt", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", nargs="?", choices=["create-week", "create", "resolve-previous", "remind", "setup",
                                                  "auto", "help"])
    p.add_argument("customers", nargs="*", help="create-week: only these customers")
    p.add_argument("--date", help="act as if today is YYYY-MM-DD")
    p.add_argument("--dry-run", action="store_true", help="show what would change, change nothing")
    args = p.parse_args()
    if args.command == "help":
        p.print_help()
        say(HELP_TEXT)
        return

    today = dt.date.fromisoformat(args.date) if args.date else dt.date.today()
    cfg = load_config()
    state = load_state()
    interactive = args.command in (None, "remind", "setup") and sys.stdin.isatty()
    app = App(cfg, state, today, args.dry_run, interactive)

    try:
        if args.command in (None, "setup", "remind"):
            if not sys.stdin.isatty():
                sys.exit("This mode needs an interactive terminal.")
            if args.command == "setup" or not cfg.get("setup_done"):
                run_setup(app, first=not cfg.get("setup_done"))
            elif not cfg["user"].get("name"):
                say("One quick question first.")
                cfg["user"]["name"] = ask_text("What's your first name (used to greet you)?")
                app.save_cfg()
            if args.command == "remind":
                title(f"Hi {cfg['user']['name']}! Time to log {week_info(today)['label']}")
                try:
                    menu_missing_time(app)
                except Back:
                    pass
                except ApiError as e:
                    say(f"  Freshservice error: {e}")
            main_menu(app)
        elif args.command in ("create-week", "create"):
            say(f"\n=== {dt.datetime.now():%Y-%m-%d %H:%M} FSTT {args.command} ===")
            only = None
            if args.customers:
                only = [app.customer(n) for n in args.customers]
                if None in only:
                    sys.exit(f"Unknown customer(s). Known: {', '.join(c['name'] for c in cfg['customers'])}")
            app.create_week(today, only)
        elif args.command == "resolve-previous":
            say(f"\n=== {dt.datetime.now():%Y-%m-%d %H:%M} FSTT resolve-previous ===")
            weeks = app.open_previous_weeks()
            if not weeks:
                say("  nothing to resolve")
            for k in weeks:
                say(f"  {k}:")
                app.resolve_week(k)
        elif args.command == "auto":                     # legacy single task
            say(f"\n=== {dt.datetime.now():%Y-%m-%d %H:%M} FSTT auto ===")
            for k in app.open_previous_weeks():
                app.resolve_week(k)
            if today.weekday() <= 4:
                app.create_week(today)
            if today.weekday() >= 4 and not state["weeks"].get(week_info(today)["key"], {}).get("resolved"):
                app.resolve_week(week_info(today)["key"])
    except QuitApp:
        say("Bye!")
    except KeyboardInterrupt:
        say("\nBye!")
    except ApiError as e:
        sys.exit(f"Freshservice error: {e}")


if __name__ == "__main__":
    main()
