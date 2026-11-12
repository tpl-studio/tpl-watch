"""TPL watcher — runs every 5 minutes, 24 hours a day, on GitHub Actions (public repo, standard library only).

1. Reads public football RSS feeds (watch.toml).
2. Keeps only big-club headlines that look like a Romano "Here we go", a Romano exit report or a club announcement (rules.py).
3. When a NEW story appears, it asks the private workflow to run now (workflow_dispatch). That workflow does all
   fact checks, de-duplication and sending — this watcher only says "look now".

Follow-up headlines about a story it already reported (same big club + same proper noun) are skipped for
story_hours: the private repo's 30-minute schedule picks those up as status updates.
Budget guard: before each dispatch it reads the private repo's run history for this month and only dispatches while
used + days_left × base_daily_min + dispatch_cost_min ≤ monthly_free_min − monthly_reserve_min (free plan protection).
No secrets live in this repository: the token and the target repository name come from GitHub Secrets.
"""
from __future__ import annotations

import calendar
import json
import math
import os
import re
import sys
import tomllib
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import rules as R  # noqa: E402

STATE = Path(os.environ.get("WATCH_STATE") or HERE / ".state" / "watch.json")

# Words that never identify a story on their own (headline vocabulary). Club names are added from watch.toml.
STOP = set("""
official officially confirmed confirm confirms confirmation breaking exclusive update news report reports sources source
romano fabrizio here there after before with without from into their they them this that have been will would just
sacked sack sacks sacking fired dismissed relieved duties parts parted ways resigns resigned resignation steps stepped down
appointed appoint appoints appointment named manager managers coach coaches head boss bosses interim caretaker
signs signed signing signings sign transfer transfers contract contracts extension renewal renews complete completes
completed completion joins joined join move moves loan loans free permanent deal deals agreement agreed agree terms
done medical medicals unveiled unveils unveil welcome welcomes arrival arrives arrived departure leaves leaving left
premier league laliga liga serie bundesliga ligue champions europa football soccer club clubs team side squad season
first final last year years until summer winter january window immediate effect million euros pounds fee
striker winger midfielder defender goalkeeper forward player players star talent young academy women
united city real madrid hotspur saint paris germain munich milan inter
""".split())


def load_conf() -> dict:
    with open(HERE / "watch.toml", "rb") as f:
        return tomllib.load(f)


def load_state() -> dict:
    try:
        s = json.loads(STATE.read_text(encoding="utf-8"))
        return s if isinstance(s, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(s: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(s, ensure_ascii=False, indent=0), encoding="utf-8")


def club_words(conf: dict) -> set[str]:
    return {w for c in conf.get("big_clubs", []) for n in c.get("names", []) for w in R.norm(n).split()}


def names_in(title: str, conf: dict) -> list[str]:
    """Proper-noun-looking words (Capitalised, 4+ letters) that are not headline vocabulary or club names."""
    skip = STOP | club_words(conf)
    out = []
    for w in re.findall(r"[A-ZÀ-ÖØ-Þ][\w'’-]{3,}", title or ""):
        n = R.norm(w).replace(" ", "")
        if len(n) >= 4 and n not in skip and n not in out:
            out.append(n)
    return out


def covered(item: dict, stories: list[dict], conf: dict) -> bool:
    """Same big club + a shared proper noun as a story already reported → follow-up (left to the 30-minute schedule)."""
    nm = set(names_in(item["title"], conf))
    return any(set(item["clubs"]) & set(s.get("clubs", [])) and nm & set(s.get("names", [])) for s in stories)


def prune(state: dict, now: datetime, conf: dict) -> dict:
    w = conf.get("watch", {})
    lim_seen = now - timedelta(days=float(w.get("seen_days", 3)))
    lim_story = now - timedelta(hours=float(w.get("story_hours", 24)))

    def keep(at: str, lim: datetime) -> bool:
        try:
            return datetime.fromisoformat(at) >= lim
        except (TypeError, ValueError):
            return False
    return {"seen": {k: v for k, v in state.get("seen", {}).items() if keep(v, lim_seen)},
            "stories": [s for s in state.get("stories", []) if keep(s.get("at", ""), lim_story)],
            "fired": [x for x in state.get("fired", []) if keep(x, now - timedelta(days=2))],
            "fail_day": state.get("fail_day", "")}


def plan(state: dict, rel: list[dict], now: datetime, conf: dict) -> dict:
    """Which relevant headlines are new stories (→ dispatch) and which are follow-ups/repeats (→ just remember)."""
    seen = state["seen"]
    fresh = [r for r in rel if r["id"] not in seen]
    new, follow = [], []
    stories = list(state["stories"])
    for r in fresh:
        if covered(r, stories, conf):
            follow.append(r)
        else:
            new.append(r)
            stories.append({"clubs": r["clubs"], "names": names_in(r["title"], conf), "at": now.isoformat()})
    return {"new": new, "follow": follow, "stories": stories}


def fired_today(state: dict, now: datetime, conf: dict) -> int:
    day = R.window_day(now, conf)
    n = 0
    for x in state.get("fired", []):
        try:
            n += R.window_day(datetime.fromisoformat(x), conf) == day
        except ValueError:
            pass
    return n


def dispatch(token: str, repo: str, conf: dict, opener=urllib.request.urlopen) -> tuple[bool, str]:
    w = conf.get("watch", {})
    url = f"https://api.github.com/repos/{repo}/actions/workflows/{w.get('workflow', 'transfer.yml')}/dispatches"
    body = json.dumps({"ref": w.get("ref", "main"), "inputs": {"trigger": "watch"}}).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28", "Content-Type": "application/json", "User-Agent": "tpl-watch"})
    try:
        with opener(req, timeout=20) as r:
            return r.status in (200, 204), f"HTTP {r.status}"
    except urllib.error.HTTPError as e:
        hint = {401: "token invalid or expired", 403: "token lacks Actions write", 404: "repo/workflow not found or no access",
                422: "workflow input/ref rejected"}.get(e.code, "")
        return False, f"HTTP {e.code} {hint}".strip()
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return False, type(e).__name__


def _get(url: str, token: str, opener) -> dict:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                                               "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "tpl-watch"})
    with opener(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def month_used(token: str, repo: str, now: datetime, opener=urllib.request.urlopen) -> int | None:
    """Minutes the private repo used this month (UTC month, like GitHub billing): every run rounded up to a minute.
    Skipped runs cost nothing. None if it cannot be read."""
    start = now.astimezone(timezone.utc).strftime("%Y-%m-01")
    total = 0
    try:
        for page in range(1, 11):
            d = _get(f"https://api.github.com/repos/{repo}/actions/runs?per_page=100&page={page}&created=%3E%3D{start}",
                     token, opener)
            runs = d.get("workflow_runs", [])
            for r in runs:
                if r.get("conclusion") == "skipped" or not r.get("run_started_at"):
                    continue
                a = datetime.fromisoformat(r["run_started_at"].replace("Z", "+00:00"))
                b = datetime.fromisoformat((r.get("updated_at") or r["run_started_at"]).replace("Z", "+00:00"))
                total += max(1, math.ceil((b - a).total_seconds() / 60))
            if len(runs) < 100:
                return total
        return total
    except Exception:  # noqa: BLE001 — unreadable → caller falls back to the small daily cap
        return None


def budget_ok(used: int, now: datetime, conf: dict) -> tuple[bool, str]:
    w = conf.get("watch", {})
    u = now.astimezone(timezone.utc)
    days_left = calendar.monthrange(u.year, u.month)[1] - u.day + 1
    proj = used + days_left * float(w.get("base_daily_min", 58)) + float(w.get("dispatch_cost_min", 3))
    limit = float(w.get("monthly_free_min", 2000)) - float(w.get("monthly_reserve_min", 100))
    return proj <= limit, f"month used {used} min, projected {proj:.0f} / limit {limit:.0f}"


def output(**kv) -> None:
    p = os.environ.get("GITHUB_OUTPUT")
    if p:
        with open(p, "a", encoding="utf-8") as f:
            for k, v in kv.items():
                f.write(f"{k}={v}\n")


def run(now: datetime | None = None, fetcher=R.fetch, opener=urllib.request.urlopen, env=os.environ) -> dict:
    conf = load_conf()
    now = now or R.now_kst()
    if not conf.get("watch", {}).get("all_day") and not R.in_window(now, conf):
        return {"why": "outside 15:00-03:00 KST", "changed": False}
    before = load_state()
    state = prune(before, now, conf)
    items = []
    for f in conf.get("feeds", []):
        try:
            items += R.parse_rss(fetcher(f["url"]), f)
        except Exception as e:  # noqa: BLE001 — one blocked feed must not stop the rest
            print(f"feed failed: {f.get('name')} ({type(e).__name__})")
    rel = R.relevant(items, conf, now)
    p = plan(state, rel, now, conf)
    for r in p["follow"]:
        state["seen"][r["id"]] = now.isoformat()
    res = {"items": len(items), "relevant": len(rel), "new": len(p["new"]), "follow": len(p["follow"]),
           "dispatched": False}
    if p["new"]:
        for r in p["new"][:8]:
            print(f"  new [{r['kind']}] {r['title'][:100]} — {r['publisher']}")
        token, repo = env.get("DISPATCH_TOKEN", "").strip(), env.get("DISPATCH_REPO", "").strip()
        w = conf.get("watch", {})
        cap = int(w.get("max_dispatch_per_day", 10))
        n_today = fired_today(state, now, conf)
        guard = ""
        if token and repo and n_today < cap:
            used = month_used(token, repo, now, opener)
            if used is None:
                cap = min(cap, int(w.get("fallback_cap", 3)))
                res["budget"] = "usage unreadable — small daily cap"
            else:
                ok_b, res["budget"] = budget_ok(used, now, conf)
                guard = "" if ok_b else "budget"
        if not (token and repo):
            res["why"] = "secrets TPL_DISPATCH_TOKEN / TPL_DISPATCH_REPO not set — detected only"
        elif n_today >= cap or guard:
            res["why"] = (("monthly free-minute guard" if guard else f"daily dispatch cap {cap} reached")
                          + " — left to the private 30-minute schedule (15:00-03:00)")
            for r in p["new"]:
                state["seen"][r["id"]] = now.isoformat()
        else:
            ok, why = dispatch(token, repo, conf, opener)
            res["dispatched"], res["why"] = ok, ("dispatched" if ok else f"dispatch failed: {why}")
            if not ok and state["fail_day"] != R.window_day(now, conf):
                state["fail_day"] = R.window_day(now, conf)
                res["alert"] = True     # fail the run once a day (GitHub e-mails the owner) — not every 5 minutes
            if ok:                      # failed → not marked, tried again next run
                for r in p["new"]:
                    state["seen"][r["id"]] = now.isoformat()
                state["stories"] = p["stories"]
                state["fired"].append(now.isoformat())
    else:
        res["why"] = "no new story" + (f" ({len(p['follow'])} follow-up headline(s) left to the schedule)" if p["follow"] else "")
    res["changed"] = state != before
    if res["changed"]:
        save_state(state)
    return res


def ping(env=os.environ, opener=urllib.request.urlopen) -> dict:
    """Manual connection test: send one dispatch now (no feeds, no memory change)."""
    token, repo = env.get("DISPATCH_TOKEN", "").strip(), env.get("DISPATCH_REPO", "").strip()
    if not (token and repo):
        return {"why": "secrets TPL_DISPATCH_TOKEN / TPL_DISPATCH_REPO not set", "alert": True, "changed": False}
    ok, why = dispatch(token, repo, load_conf(), opener)
    return {"ping": "ok" if ok else "failed", "why": why, "alert": not ok, "changed": False}


if __name__ == "__main__":
    r = ping() if os.environ.get("WATCH_PING") == "true" else run()
    print(" · ".join(f"{k}={v}" for k, v in r.items()))
    output(changed=1 if r.get("changed") else 0)
    sys.exit(1 if r.get("alert") else 0)
