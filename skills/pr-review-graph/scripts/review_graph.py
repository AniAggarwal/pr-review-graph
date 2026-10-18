#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Review graph: an interactive Artifact page for a set of GitHub PRs and issues under review.

One config file drives everything. Subcommands:

  fetch       pull title, head, base, CI and URL for each node from `gh`
  build       config + fetched data -> page HTML + rows.json (the db rows)
  check       consistency checks on config + fetched data
  screenshot  render a built page in headless Chrome/Chromium, dark and light, 1280 and 390 wide
  seed        ArtifactData batch payloads for the nodes and edges rows
  smoke       run the page scripts with browser-like globals (catches the `top` and SVG-hidden traps)
  inbox       turn the reviews and feedback rows into a list of actions
  patchid     patch-id of one base..head diff (to backfill an approval's patch-id)

Run `review_graph.py <subcommand> --help` for the options. Python 3.11+, standard library only.
"""

from __future__ import annotations

import argparse
import datetime as dt
import glob
import html
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILL = HERE.parent
TEMPLATE = SKILL / "assets" / "template.html"
GROUPS = ("base", "root", "chain1", "chain2", "siblings", "bar", "last", "other")
ID_RE = re.compile(r"^[A-Za-z0-9_\-.~:@+]{1,200}$")

DEFAULT_STATUSES = {
    "needs_review": {"label": "Needs review", "color": "needs"},
    "rereview": {"label": "Re-read", "color": "needs"},
    "fixups": {"label": "Fixups in progress", "color": "fixups"},
    "changes": {"label": "Changes requested", "color": "fixups"},
    "decision": {"label": "Decision needed", "color": "fixups"},
    "recheck": {"label": "Re-check", "color": "fixups"},
    "approved": {"label": "Approved", "color": "approved"},
    "merge_now": {"label": "Approved, merge now", "color": "approved"},
    "merged": {"label": "Merged", "color": "merged"},
    "closed": {"label": "Closed", "color": "merged"},
    "hold": {"label": "On hold", "color": "merged"},
    "branch": {"label": "Branch", "color": "branch"},
}
DEFAULT_VERDICTS = [
    ["approved", "Reviewed + approved"],
    ["merge_now", "Approved, merge now"],
    ["changes", "Changes requested"],
    ["hold", "Hold"],
]
DEFAULT_DECISION_VERDICTS = [["approved", "Approve"], ["changes", "Change the plan"], ["hold", "Hold"]]
DEFAULT_ROWS = {
    "chain1": "FIRST CHAIN",
    "chain2": "SECOND CHAIN",
    "siblings": "SIBLINGS, EACH DIRECTLY ON THE ROOT PR",
    "last": "LAST",
    "other": "OTHER REVIEWS",
}
DEFAULT_LEGEND = {
    "needs": "needs your review",
    "fixups": "fixups in progress / re-check",
    "approved": "reviewed / approved",
    "merged": "merged or on hold",
    "step": "your review order",
    "stacked": "stacked on",
    "waits": "waits for",
}
SKELETON = (
    '<!doctype html><html{attr}><head><meta charset=utf8><meta name=viewport content="width=device-width,'
    'initial-scale=1,viewport-fit=cover"><style>:root{{color-scheme:light;box-sizing:border-box;padding-top:'
    "env(safe-area-inset-top,0px);padding-bottom:env(safe-area-inset-bottom,0px)}}html{{scroll-padding-top:"
    "env(safe-area-inset-top,0px)}}body{{margin:0;padding:0;font:14px -apple-system,BlinkMacSystemFont,"
    "sans-serif;background:#fff;color:#000}}img{{max-width:100%}}[hidden]:not([hidden=until-found i])"
    "{{display:none!important}}</style></head><body>"
)


def die(msg: str, code: int = 1) -> None:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


def load_json(path: str | Path):
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        die(f"{path}: not found")
    except json.JSONDecodeError as e:
        die(f"{path}: not valid JSON ({e})")


def node_id(n: dict) -> str:
    if n.get("id"):
        return n["id"]
    if not n.get("repo") or not n.get("number"):
        die(f"node needs an id, or a repo and a number: {n}")
    return f"{n['repo'].split('/')[-1].lower()}-{n['number']}"


def key(n: dict) -> str:
    return f"{n['repo']}#{n['number']}"


# ---------------------------------------------------------------- fetch


def ci_summary(rollup: list | None) -> str:
    states = [(c.get("conclusion") or c.get("state") or c.get("status") or "").upper() for c in rollup or []]
    states = [s for s in states if s not in ("SKIPPED", "NEUTRAL", "")]
    if not states:
        return "none"
    if any(s in ("FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE") for s in states):
        return "failing"
    if any(s in ("PENDING", "QUEUED", "IN_PROGRESS", "WAITING", "REQUESTED", "EXPECTED") for s in states):
        return "running"
    return "green"


def gh_json(args: list[str], tries: int = 3) -> dict:
    """Run `gh` and parse its JSON, retrying transient network failures (TLS timeouts and the like)."""
    import time

    for i in range(tries):
        out = subprocess.run(["gh", *args], capture_output=True, text=True)
        if not out.returncode:
            return json.loads(out.stdout)
        if i + 1 < tries:
            time.sleep(2 * (i + 1))
    raise RuntimeError(out.stderr.strip() or f"gh {' '.join(args)} failed")


def patch_id(diff: str) -> str:
    """`git patch-id --stable` of a diff (works outside a repository); '' if git is missing or the diff is empty."""
    if not diff.strip() or not shutil.which("git"):
        return ""
    r = subprocess.run(["git", "patch-id", "--stable"], input=diff, capture_output=True, text=True)
    return (r.stdout.split() or [""])[0][:12]


def compare_diff(repo: str, base: str, head: str) -> str:
    r = subprocess.run(
        ["gh", "api", "-H", "Accept: application/vnd.github.diff", f"repos/{repo}/compare/{base}...{head}"], capture_output=True, text=True
    )
    if r.returncode:
        raise RuntimeError(r.stderr.strip())
    return r.stdout


def load_history(path: Path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}


def cmd_fetch(a) -> None:
    cfg = load_json(a.config)
    if not shutil.which("gh"):
        die("`gh` is not on PATH; install the GitHub CLI or write a fetched JSON by hand (see examples/)")
    items, branches, errors = {}, {}, []
    hist_path = Path(a.history or Path(a.out).with_name("patch-history.json"))
    history = load_history(hist_path)
    for n in cfg.get("nodes", []):
        if not n.get("repo") or not n.get("number"):
            continue
        k = key(n)
        try:
            if n.get("kind", "pr") == "issue":
                d = gh_json(["issue", "view", str(n["number"]), "-R", n["repo"], "--json", "number,title,state,url"])
            else:
                d = gh_json(
                    [
                        "pr",
                        "view",
                        str(n["number"]),
                        "-R",
                        n["repo"],
                        "--json",
                        "number,title,state,isDraft,url,headRefName,headRefOid,baseRefName,statusCheckRollup",
                    ]
                )
                d["ci"] = ci_summary(d.pop("statusCheckRollup", None))
                if not a.no_patch_ids:
                    diff = subprocess.run(["gh", "pr", "diff", str(n["number"]), "-R", n["repo"]], capture_output=True, text=True).stdout
                    d["patch_id"] = patch_id(diff)
                    if d["patch_id"]:
                        history.setdefault(k, {})[d["headRefOid"][:8]] = d["patch_id"]
            items[k] = d
        except Exception as e:  # keep going; report all failures together
            errors.append(f"{k}: {e}")
    for b in (cfg.get("trunk"), cfg.get("target"), cfg.get("bar")):
        if b and b.get("repo") and b.get("branch"):
            try:
                d = gh_json(["api", f"repos/{b['repo']}/branches/{b['branch']}"])
                branches[f"{b['repo']}:{b['branch']}"] = d["commit"]["sha"]
            except Exception as e:
                errors.append(f"branch {b['repo']}:{b['branch']}: {e}")
    out = {
        "fetched_at": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "items": items,
        "branches": branches,
        "patch_history": history,
    }
    Path(a.out).write_text(json.dumps(out, indent=1))
    if history:
        hist_path.write_text(json.dumps(history, indent=1, sort_keys=True))
    print(f"fetched {len(items)} items, {len(branches)} branches -> {a.out}")
    for e in errors:
        print(f"warning: {e}", file=sys.stderr)
    if errors and a.strict:
        sys.exit(1)


# ---------------------------------------------------------------- rows


def build_rows(cfg: dict, fetched: dict, reviews: dict | None = None) -> tuple[list[dict], list[dict]]:
    items, branches = fetched.get("items", {}), fetched.get("branches", {})
    reviews = reviews or {}
    statuses = {**DEFAULT_STATUSES, **cfg.get("statuses", {})}
    nodes: list[dict] = []
    trunk, target, bar = cfg.get("trunk"), cfg.get("target"), cfg.get("bar")

    def sha8(b):
        return (branches.get(f"{b.get('repo')}:{b.get('branch')}") or b.get("sha") or "")[:8]

    if target:
        nodes.append(
            dict(
                id=target.get("id", "main"),
                kind="branch",
                status="branch",
                title=target.get("name", target.get("branch", "main")),
                label=target.get("label", ""),
                group="base",
                order=0,
                repo=target.get("repo", ""),
                notes=target.get("notes", ""),
                head=sha8(target),
            )
        )
    if trunk:
        nodes.append(
            dict(
                id=trunk.get("id", "trunk"),
                kind="branch",
                status="branch",
                title=trunk.get("name", trunk.get("branch", "trunk")),
                label=trunk.get("label", "trunk"),
                group="base",
                order=1,
                repo=trunk.get("repo", ""),
                notes=trunk.get("notes", ""),
                head=sha8(trunk),
            )
        )
    if bar:
        nodes.append(
            dict(
                id=bar.get("id", "bar"),
                kind="branch",
                status="branch",
                title=bar.get("name", "All merged"),
                label=bar.get("label", "after every PR above is in").replace("{sha}", sha8(bar) or "?"),
                group="bar",
                order=0,
                repo=bar.get("repo", ""),
                notes=bar.get("notes", "").replace("{sha}", sha8(bar) or "?"),
                head=sha8(bar),
            )
        )

    head_branch: dict[str, str] = {}  # "repo:branch" -> node id
    for n in cfg.get("nodes", []):
        f = items.get(key(n), {}) if n.get("repo") and n.get("number") else {}
        nid = node_id(n)
        state = (f.get("state") or "").upper()
        # GitHub's merged/closed state beats the config's status (and, below, any verdict).
        status = (
            {"MERGED": "merged", "CLOSED": "closed"}.get(state)
            or n.get("status")
            or ("decision" if n.get("kind") == "decision" else "needs_review")
        )
        if status not in statuses:
            die(f"{nid}: status {status!r} is not in the status vocabulary")
        head = (f.get("headRefOid") or n.get("head") or "")[:8]
        row: dict = dict(
            id=nid,
            repo=n.get("repo", ""),
            number=n.get("number", 0),
            kind=n.get("kind", "pr"),
            title=n.get("title") or f.get("title") or nid,
            label=n.get("label", ""),
            url=n.get("url") or f.get("url", ""),
            head=head,
            head_branch=f.get("headRefName", ""),
            base=f.get("baseRefName", n.get("base", "")),
            ci=n.get("ci") or f.get("ci", ""),
            draft=bool(f.get("isDraft")),
            state=state,
            status=status,
            review_step=n.get("review_step"),
            owner=n.get("owner", ""),
            notes=n.get("notes", ""),
            compare_url=n.get("compare_url", ""),
            compare_label=n.get("compare_label", ""),
            reviewed_head=(n.get("reviewed_head") or "")[:8],
            group=n.get("group", "other"),
            order=n.get("order", 0),
        )
        if n.get("top"):
            row["top"] = n["top"]
        # Patch-ids: the current one, plus every head seen before, so the page can tell a re-parent from a real change.
        hist = fetched.get("patch_history", {}).get(key(n), {}) if n.get("repo") and n.get("number") else {}
        row["patch_id"] = f.get("patch_id", "") or hist.get(head, "")
        row["patch_ids"] = dict(hist)
        if n.get("reviewed_head") and n.get("reviewed_patch_id"):
            row["patch_ids"][row["reviewed_head"]] = n["reviewed_patch_id"][:12]
        # The LATEST approval decides: the user's newest verdict row if there is one, else the config's reviewed_head.
        # It stands at an older head only if the PR's own patch-id is unchanged (a re-parent); otherwise re-check.
        rv = reviews.get(nid) if status not in ("merged", "closed") else None  # GitHub state beats any verdict
        if rv and rv.get("verdict") in ("approved", "merge_now"):
            appr_head, appr_pid, appr_status = (rv.get("head") or "")[:8], rv.get("patch_id") or "", rv["verdict"]
        elif rv and rv.get("verdict"):
            appr_head = appr_pid = appr_status = ""  # a later non-approval verdict replaces any older approval
        else:
            appr_head, appr_pid, appr_status = row["reviewed_head"], "", status if status in ("approved", "merge_now") else ""
        if appr_status and appr_head and head and appr_head != head:
            was = appr_pid or row["patch_ids"].get(appr_head, "")
            if was and row["patch_id"] and was == row["patch_id"]:
                row["reparented"] = True
                row["status"] = appr_status
            else:
                row["status"] = "recheck"
                row["approved_status"] = appr_status
                row["approved_head"] = appr_head
        elif appr_status and appr_head == head:
            row["status"] = appr_status
        if row["compare_url"] == "" and row["reviewed_head"] and head and row["reviewed_head"] != head and row["repo"]:
            row["compare_url"] = f"https://github.com/{row['repo']}/compare/{row['reviewed_head']}..{head}"
            row["compare_label"] = row["compare_label"] or f"Diff since your last review ({row['reviewed_head']}..{head})"
        if row["head_branch"]:
            head_branch[f"{row['repo']}:{row['head_branch']}"] = nid
        nodes.append(row)

    # Show bases as "#N" / "trunk" instead of raw branch names where we know them.
    by_id = {n["id"]: n for n in nodes}
    edges: list[dict] = []
    spec = cfg.get("edges", "infer")
    if spec == "infer":
        for n in nodes:
            if n["kind"] == "branch" or not n.get("base"):
                continue
            src = None
            b = f"{n['repo']}:{n['base']}"
            if b in head_branch:
                src = head_branch[b]
            elif trunk and b == f"{trunk.get('repo')}:{trunk.get('branch')}":
                src = trunk.get("id", "trunk")
            elif bar and b == f"{bar.get('repo')}:{bar.get('branch')}":
                src = bar.get("id", "bar")
            # Once PRs merge into the trunk, the PRs above them get retargeted to the trunk. Keep the picture: a chain
            # member still hangs off the previous box in its chain, and a chain head or sibling off the root.
            # The last row works the same way, with the bar in place of the root.
            if src and trunk and src == trunk.get("id", "trunk") and n["group"] in ("chain1", "chain2", "siblings", "last"):
                prev = [m for m in nodes if m["group"] == n["group"] and m["order"] < n["order"]] if n["group"] != "siblings" else []
                anchor = [m for m in nodes if m["group"] == ("bar" if n["group"] == "last" else "root")]
                src = max(prev, key=lambda m: m["order"])["id"] if prev else (anchor[0]["id"] if anchor else src)
            if src:
                edges.append({"from": src, "to": n["id"], "kind": "stacked", "label": ""})
                s = by_id[src]
                n["base"] = f"#{s['number']}" if s["kind"] != "branch" else f"{n['base']} ({s['title']})"
    elif isinstance(spec, list):
        edges += [{"label": "", "kind": "stacked", **e} for e in spec]
    else:
        die('config "edges" must be "infer" or a list of {from, to, kind}')
    edges += [{"label": "", "kind": "stacked", **e} for e in cfg.get("extra_edges", [])]
    if bar and cfg.get("waits_for", "auto") == "auto":
        bid = bar.get("id", "bar")
        for g in ("chain1", "chain2"):
            members = sorted((n for n in nodes if n["group"] == g), key=lambda n: n["order"])
            if members:
                edges.append({"from": members[-1]["id"], "to": bid, "kind": "waits_for", "label": ""})
        edges += [{"from": n["id"], "to": bid, "kind": "waits_for", "label": ""} for n in nodes if n["group"] == "siblings"]
    if trunk and target:
        edges.append(
            {
                "from": trunk.get("id", "trunk"),
                "to": target.get("id", "main"),
                "kind": "final",
                "label": cfg.get("final_label", "one PR once|everything is in"),
            }
        )
    seen, uniq = set(), []
    for e in edges:
        if "from" not in e or "to" not in e:
            die(f"edge needs both from and to: {e}")
        e["id"] = f"{e['from']}__{e['to']}"
        if e["id"] not in seen:
            seen.add(e["id"])
            uniq.append(e)
    return nodes, uniq


def check_rows(cfg: dict, nodes: list[dict], edges: list[dict]) -> tuple[list[str], list[str]]:
    err, warn = [], []
    ids = [n["id"] for n in nodes]
    for i in sorted({i for i in ids if ids.count(i) > 1}):
        err.append(f"duplicate node id {i!r}")
    for i in ids:
        if not ID_RE.match(i):
            err.append(f"node id {i!r} is not path-safe (letters, digits, _ - . ~ : @ + only)")
    known = set(ids)
    for e in edges:
        for end in ("from", "to"):
            if e[end] not in known:
                err.append(f"edge {e['id']}: {end} {e[end]!r} is not a node")
        if not ID_RE.match(e["id"]):
            err.append(f"edge id {e['id']!r} is not path-safe")
        if e["kind"] not in ("stacked", "waits_for", "final"):
            err.append(f"edge {e['id']}: unknown kind {e['kind']!r}")
    slots = {}
    for n in nodes:
        if n["group"] not in GROUPS:
            err.append(f"{n['id']}: unknown group {n['group']!r} (use one of {', '.join(GROUPS)})")
        s = (n["group"], n["order"])
        if s in slots:
            err.append(f"{n['id']} and {slots[s]} share a slot: group {s[0]}, order {s[1]}")
        slots[s] = n["id"]
    if sum(n["group"] == "root" for n in nodes) > 1:
        err.append("more than one node in group root")
    if sum(n["group"] == "bar" for n in nodes) > 1:
        err.append("more than one bar")
    done = {"approved", "merge_now", "merged", "closed", "hold", "branch"}
    for n in nodes:
        st = n.get("review_step")
        if st is not None and (isinstance(st, bool) or not isinstance(st, int) or st < 1):
            err.append(f"{n['id']}: review_step must be a whole number from 1, got {st!r}")
        if n["kind"] in ("pr", "issue") and n["status"] not in done and not n.get("review_step") and not n.get("approved_status"):
            warn.append(f"{n['id']}: open but has no review_step")
        if n.get("status") == "recheck":
            warn.append(
                f"{n['id']}: approved at {n.get('approved_head') or n['reviewed_head']} but the patch changed (or its old patch-id is unknown): shown as re-check"
            )
        if n["kind"] == "pr" and not n.get("head"):
            warn.append(f"{n['id']}: no head SHA (not fetched?)")
        if n.get("status") in ("needs_review", "rereview") and n.get("state") == "MERGED":
            warn.append(f"{n['id']}: merged on GitHub but status is {n['status']}")
    step = {n["id"]: n.get("review_step") for n in nodes if isinstance(n.get("review_step"), int)}
    for e in edges:
        a, b = step.get(e["from"]), step.get(e["to"])
        if e["kind"] == "stacked" and a and b and b < a:
            err.append(f"review order: {e['to']} (step {b}) stacks on {e['from']} (step {a}); review the base first")
    for g in ("chain1", "chain2", "siblings", "last", "other"):
        members = sorted((n for n in nodes if n["group"] == g), key=lambda n: n["order"])
        if len(members) > 12:
            warn.append(f"group {g} has {len(members)} boxes; the graph gets very wide")
    return err, warn


def report_check(err, warn) -> bool:
    for w in warn:
        print(f"warning: {w}")
    for e in err:
        print(f"ERROR: {e}")

    print("check OK" if not err else f"check FAILED ({len(err)} errors)")
    return not err


# ---------------------------------------------------------------- build


def code_spans(text: str) -> str:
    """Escape text; `backticks` become <code>."""
    parts = text.split("`")
    return "".join(html.escape(p) if i % 2 == 0 else f"<code>{html.escape(p)}</code>" for i, p in enumerate(parts))


def static_queue(nodes, statuses, steps) -> str:
    def top(n):
        return n.get("top") or (n["title"] if n["kind"] == "branch" else f"#{n['number']}")

    def row(n):
        st = n["status"]
        color = statuses.get(st, {}).get("color", "needs")
        return (
            f'<a class="row" href="{html.escape(n.get("url") or "#")}" target="_blank" rel="noopener" '
            f'style="--b:var(--{color}-b)"><span class="txt"><b>{html.escape(top(n))}</b> {html.escape(n["title"])}'
            f'</span><span class="stt">{html.escape(statuses.get(st, {}).get("label", st))}</span></a>'
        )

    by_step: dict[int, list] = {}
    for n in nodes:
        if n.get("review_step") and n["kind"] != "branch":
            by_step.setdefault(n["review_step"], []).append(n)
    done = [n for n in nodes if not n.get("review_step") and n["kind"] != "branch"]
    out = [
        f'<li class="step"><b>{s}. {html.escape(steps.get(str(s), f"Step {s}"))}</b>'
        + "".join(row(n) for n in sorted(by_step[s], key=lambda n: n["order"]))
        + "</li>"
        for s in sorted(by_step)
    ]
    if done:
        out.append('<li class="step"><b>Already decided</b>' + "".join(row(n) for n in done) + "</li>")
    return "".join(out)


def legend_html(legend) -> str:
    sw = lambda c, t: f'<span><i class="sw" style="--b:var(--{c}-b);--f:var(--{c}-f)"></i>{html.escape(t)}</span>'
    return "".join(
        [
            sw("needs", legend["needs"]),
            sw("fixups", legend["fixups"]),
            sw("approved", legend["approved"]),
            sw("merged", legend["merged"]),
            f'<span><i class="st">1</i>{html.escape(legend["step"])}</span>',
            f'<span><i class="ln"></i>{html.escape(legend["stacked"])}</span>',
            f'<span><i class="ln d"></i>{html.escape(legend["waits"])}</span>',
        ]
    )


def page_config(cfg) -> dict:
    return {
        "statuses": {**DEFAULT_STATUSES, **cfg.get("statuses", {})},
        "verdicts": cfg.get("verdicts", DEFAULT_VERDICTS),
        "decision_verdicts": cfg.get("decision_verdicts", DEFAULT_DECISION_VERDICTS),
        "steps": {str(k): v for k, v in cfg.get("steps", {}).items()},
        "rows": {**DEFAULT_ROWS, **cfg.get("rows", {})},
    }


def render_page(cfg, nodes, edges, snapshot_at: str, reviews: list | None = None) -> str:
    t = TEMPLATE.read_text()
    pc = page_config(cfg)
    # Inside <script type="application/json">: "</" could close the tag and "<!--" could start an escaped-script state
    # (PR titles come from GitHub, so any text can show up here). Both stay valid JSON.
    js = lambda o: json.dumps(o, ensure_ascii=False).replace("</", "<\\/").replace("<!--", "<\\u0021--")
    title = cfg.get("title", "Review Graph")
    subs = {
        "{{TITLE}}": html.escape(title),
        "{{SUBTITLE}}": code_spans(
            cfg.get(
                "subtitle",
                "Every PR and issue waiting on your review. Click a box to see its diff, "
                "leave feedback or record your verdict. Claude reads what you save here.",
            )
        ),
        "{{CAPTION}}": code_spans(
            cfg.get(
                "caption",
                "Everything merges into the trunk, and one PR lands the trunk on its target at the end. "
                "Click a box to open its panel. Circled numbers are your review order.",
            )
        ),
        "{{LEGEND}}": legend_html({**DEFAULT_LEGEND, **cfg.get("legend", {})}),
        "/*PAGECONFIG*/": js(pc),
        "/*SNAPSHOT*/": js({"at": snapshot_at, "nodes": nodes, "edges": edges, "reviews": reviews or []}),
        "<!--STATIC_QUEUE-->": static_queue(nodes, pc["statuses"], pc["steps"]),
    }
    for k in subs:
        if k not in t:
            die(f"template is missing placeholder {k}")
    # One pass, so text inserted for one placeholder (a PR title, say) is never rescanned for the others.
    return re.sub("|".join(re.escape(k) for k in subs), lambda m: subs[m.group(0)], t)


def load_inputs(a):
    cfg = load_json(a.config)
    fetched = load_json(a.fetched) if a.fetched else {"items": {}, "branches": {}}
    reviews = {}
    for r in read_rows(a.reviews) if getattr(a, "reviews", None) else []:
        nid = r.get("node_id") or r.get("_id")
        reviews[nid] = {k: r.get(k) for k in ("node_id", "verdict", "head", "patch_id", "at", "sent_at") if r.get(k) is not None}
        reviews[nid]["node_id"] = nid
    return cfg, fetched, reviews


def cmd_build(a) -> None:
    cfg, fetched, reviews = load_inputs(a)
    nodes, edges = build_rows(cfg, fetched, reviews)
    err, warn = check_rows(cfg, nodes, edges)
    if not report_check(err, warn) and not a.force:
        sys.exit(1)
    at = a.snapshot_at or (fetched.get("fetched_at") or dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%MZ")).replace("T", " ")[:16] + "Z"
    out = Path(a.out)
    out.write_text(render_page(cfg, nodes, edges, at.rstrip("Z") + "Z", list(reviews.values())))
    rows = Path(a.rows or out.with_suffix(".rows.json"))
    rows.write_text(json.dumps({"nodes": nodes, "edges": edges}, indent=1, ensure_ascii=False))
    print(f"built {out} ({out.stat().st_size} bytes) and {rows}: {len(nodes)} nodes, {len(edges)} edges")


def cmd_check(a) -> None:
    cfg, fetched, reviews = load_inputs(a)
    nodes, edges = build_rows(cfg, fetched, reviews)
    sys.exit(0 if report_check(*check_rows(cfg, nodes, edges)) else 1)


# ---------------------------------------------------------------- screenshot


def find_browser(explicit: str | None) -> str | None:
    if explicit:
        return explicit if (Path(explicit).exists() or shutil.which(explicit)) else None
    if os.environ.get("CHROME"):
        return os.environ["CHROME"]
    for name in (
        "chromium",
        "chromium-browser",
        "google-chrome",
        "google-chrome-stable",
        "chrome",
        "microsoft-edge",
        "brave",
        "brave-browser",
    ):
        if shutil.which(name):
            return shutil.which(name)
    home = Path.home()
    pats = [
        home / ".cache/puppeteer/chrome-headless-shell/*/*/chrome-headless-shell",
        home / ".cache/puppeteer/chrome/*/*/chrome",
        home / ".cache/ms-playwright/chromium*/chrome-linux*/chrome",
        home / ".cache/ms-playwright/chromium_headless_shell*/*/chrome-headless-shell",
        Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
    ]
    for p in pats:
        hits = sorted(glob.glob(str(p)))
        if hits:
            return hits[-1]
    return None


def cmd_screenshot(a) -> None:
    browser = find_browser(a.browser)
    if not browser:
        print("warning: no Chrome/Chromium found; skipping screenshots. Pass --browser PATH or set CHROME=.", file=sys.stderr)
        return
    out = Path(a.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    body = Path(a.page).read_text()
    headless = [] if "headless-shell" in browser else ["--headless=new"]
    shots = []
    for theme in ("dark", "light"):
        wrapped = out / f"page-{theme}.html"
        wrapped.write_text(SKELETON.format(attr=f' data-theme="{theme}"') + body + "</body></html>")
        for w in a.widths:
            png = out / f"{theme}-{w}.png"
            r = subprocess.run(
                [
                    browser,
                    *headless,
                    "--no-sandbox",
                    "--hide-scrollbars",
                    "--disable-gpu",
                    f"--virtual-time-budget={a.wait_ms}",
                    f"--window-size={w},{a.height}",
                    f"--screenshot={png}",
                    wrapped.resolve().as_uri(),
                ],
                capture_output=True,
                text=True,
                timeout=120,
            )
            if r.returncode or not png.exists():
                print(f"warning: {theme} {w}px failed: {(r.stderr or '').strip()[-300:]}", file=sys.stderr)
            else:
                shots.append(png)
                print(png)
    if shots:
        print("Look at every screenshot: the graph must be visible in both themes, text readable, nothing clipped.")


def cmd_smoke(a) -> None:
    node = shutil.which("node")
    if not node:
        print("warning: node not found; skipping the smoke test (screenshots still apply)", file=sys.stderr)
        return
    r = subprocess.run([node, str(SKILL / "assets" / "smoke.js"), a.page])
    sys.exit(r.returncode)


# ---------------------------------------------------------------- seed

LIST_HEADER = re.compile(r'from collection "([^"]+)"')
LIST_ROW = re.compile(r'^- "([^"]+)".*?version (\d+)', re.MULTILINE)  # older list output: - "<id>" ... version N


def parse_listing(text: str) -> dict[str, int]:
    """Versions from ArtifactData list output text: {'nodes/<id>': version}.

    The output is a `N documents from collection "<name>":` header followed by one JSON line per row,
    `{"id": ..., "data": {...}, "version": N}`. The older `- "<id>" ... version N` row form is read too.
    """
    versions, coll = {}, None
    for line in text.splitlines():
        line = line.strip()
        m = LIST_HEADER.search(line)
        if m:
            coll = m.group(1)
            continue
        if not coll:
            continue
        if line.startswith("{"):
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict) and row.get("id") and isinstance(row.get("version"), int):
                versions[f"{coll}/{row['id']}"] = row["version"]
            continue
        m = LIST_ROW.match(line)
        if m:
            versions[f"{coll}/{m.group(1)}"] = int(m.group(2))
    return versions


def cmd_seed(a) -> None:
    rows = load_json(a.rows)
    versions: dict[str, int] = {}
    for f in a.listing or []:
        versions.update(parse_listing(Path(f).read_text()))
    docs_dir = Path(a.out_dir)
    docs_dir.mkdir(parents=True, exist_ok=True)
    writes, wanted = [], set()
    for coll in ("nodes", "edges"):
        for r in rows[coll]:
            did = r["id"]
            wanted.add(f"{coll}/{did}")
            w = {"op": "set", "collection": coll, "doc_id": did}
            if a.inline:
                w["data"] = r
            else:
                p = docs_dir / f"{coll}__{did}.json"
                p.write_text(json.dumps(r, ensure_ascii=False))
                w["file_path"] = str(p.resolve())
            if f"{coll}/{did}" in versions:
                w["if_version"] = versions[f"{coll}/{did}"]
            writes.append(w)
    if a.prune:
        for k, v in versions.items():
            coll, did = k.split("/", 1)
            if coll in ("nodes", "edges") and k not in wanted:
                writes.append({"op": "delete", "collection": coll, "doc_id": did, "if_version": v})
    batches = [writes[i : i + 50] for i in range(0, len(writes), 50)]
    for i, b in enumerate(batches, 1):
        p = docs_dir / f"batch-{i}.json"
        p.write_text(json.dumps(b, ensure_ascii=False))
        print(f"{p}: {len(b)} writes")
    missing = [w for w in writes if w["op"] == "set" and "if_version" not in w]
    if versions and missing:
        print(f"note: {len(missing)} rows have no version in the listing, so they are created (fine for new rows)")
    print("Pass each batch file's contents as `writes` to ArtifactData action=batch. Reviews and feedback are never touched.")


# ---------------------------------------------------------------- inbox


def read_rows(path: str) -> list[dict]:
    """Rows from an ArtifactData out_dir, a JSON array, or pasted list/query output text."""
    p = Path(path)
    if p.is_dir():
        out = []
        for f in sorted(p.glob("*.json")):
            d = json.loads(f.read_text())
            d.setdefault("_id", f.stem)
            out.append(d)
        return out
    text = p.read_text()
    try:
        d = json.loads(text)
        return [{"_id": x.get("id"), "_version": x.get("version"), **x.get("data", x)} for x in (d if isinstance(d, list) else [d])]
    except json.JSONDecodeError:
        pass
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("{") and '"data"' in line:
            x = json.loads(line)
            rows.append({"_id": x.get("id"), "_version": x.get("version"), **x["data"]})
    return rows


def cmd_inbox(a) -> None:
    rows = load_json(a.rows)
    nodes = {n["id"]: n for n in rows["nodes"]}
    name = lambda i: (
        f"{nodes[i].get('top') or '#' + str(nodes[i].get('number'))} {nodes[i].get('label') or nodes[i]['title']}" if i in nodes else i
    )
    actions = []
    for r in read_rows(a.reviews) if a.reviews else []:
        nid = r.get("node_id") or r.get("_id")
        n = nodes.get(nid, {})
        cur, seen, v = n.get("head", ""), r.get("head", ""), r.get("verdict")
        stale = bool(cur and seen and cur != seen)
        was = r.get("patch_id") or n.get("patch_ids", {}).get(seen, "")
        same_patch = bool(stale and was and n.get("patch_id") and was == n["patch_id"])
        if stale and same_patch:
            act = f"head moved {seen} -> {cur} but the patch-id is unchanged ({was}): the verdict stands (re-parented)"
        elif stale:
            act = (
                f"RE-CHECK: verdict {v!r} was given at {seen}, head is now {cur} and the patch changed "
                f"({was or 'old patch-id unknown'} -> {n.get('patch_id') or '?'}). Ask for a re-review with the delta link."
            )
        elif v == "merge_now":
            act = f"merge at {cur} once CI is green ({n.get('ci', '?')}); retarget the next PR in its chain first"
        elif v == "approved":
            act = "record the approval; it merges later, in merge order (not a merge instruction)"
        elif v == "changes":
            act = f"relay to {n.get('owner') or 'the owning agent'}; set the node to fixups"
        elif v == "hold":
            act = "keep it out of every merge; note the hold on the node"
        else:
            act = f"unknown verdict {v!r}"
        sent = " (already sent to Claude)" if r.get("sent_at") else ""
        actions.append(f"- verdict {name(nid)}: {v} at {seen or '?'}{sent}\n    -> {act}")
    for f in read_rows(a.feedback) if a.feedback else []:
        if f.get("resolved"):
            continue
        nid = f.get("node_id")
        n = nodes.get(nid, {})
        stale = f.get("head") and n.get("head") and f["head"] != n["head"]
        ver = f" (version {f['_version']})" if f.get("_version") else ""
        actions.append(
            f"- feedback {name(nid)} [feedback/{f.get('_id')}{ver}]{' on an older head ' + f['head'] if stale else ''}:\n"
            f'    "{f.get("text", "").strip()}"\n'
            f"    -> relay to {n.get('owner') or 'the owning agent'}; when it lands, update feedback/{f.get('_id')} "
            f"with reply + resolved=true (pin if_version)"
        )
    print("\n".join(actions) if actions else "Inbox empty: no verdicts and no open feedback.")


def cmd_patchid(a) -> None:
    pid = patch_id(compare_diff(a.repo, a.base, a.head))
    if not pid:
        die("empty diff or git missing")
    print(pid)
    if a.record:
        hp = Path(a.record)
        h = load_history(hp)
        h.setdefault(a.key, {})[a.head[:8]] = pid
        hp.write_text(json.dumps(h, indent=1, sort_keys=True))
        print(f"recorded {a.key} {a.head[:8]} -> {pid} in {hp}")


# ---------------------------------------------------------------- cli


def main() -> None:
    p = argparse.ArgumentParser(prog="review_graph.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser(
        "fetch",
        help="pull PR/issue data from GitHub with gh",
        description="For every node with repo+number, run `gh pr view` (or `gh issue view` for kind=issue) and record title, state, draft, URL, head branch/SHA, base branch and a CI summary (green/running/failing/none). Also records the SHAs of the trunk, target and bar branches. Needs `gh auth login`.",
    )
    f.add_argument("config")
    f.add_argument("--out", default="fetched.json", help="where to write the fetched data (default: fetched.json)")
    f.add_argument("--strict", action="store_true", help="exit 1 if any item failed to fetch")
    f.add_argument("--history", help="patch-id history file to read and extend (default: patch-history.json next to --out)")
    f.add_argument("--no-patch-ids", action="store_true", help="skip `gh pr diff` + `git patch-id` (faster; disables re-check detection)")
    f.set_defaults(fn=cmd_fetch)

    for name, fn, h, d in (
        (
            "build",
            cmd_build,
            "config + fetched data -> page HTML and rows JSON",
            "Merge the config with fetched data, infer stacking edges from base branches (unless edges are listed), run the consistency check, then write the page HTML (template + embedded snapshot + static queue) and the rows JSON that `seed` and `inbox` read.",
        ),
        (
            "check",
            cmd_check,
            "run the consistency checks only",
            "Checks: unique, path-safe ids; edges point at existing nodes; no two boxes share a (group, order) slot; known groups; one root and at most one bar; a PR never has an earlier review_step than the PR it stacks on. Warns on open nodes without a review_step and PRs without a head.",
        ),
    ):
        c = sub.add_parser(name, help=h, description=d)
        c.add_argument("config")
        c.add_argument("--fetched", help="output of `fetch` (or a hand-written fixture); omit to build from the config alone")
        c.add_argument(
            "--reviews",
            help="the reviews rows (ArtifactData list out_dir, JSON, or pasted output). The latest verdict per node then decides approved vs re-check, and the snapshot shows it",
        )
        if name == "build":
            c.add_argument("--out", default="review-graph.html", help="page to write (default: review-graph.html)")
            c.add_argument("--rows", help="rows JSON to write (default: <out>.rows.json)")
            c.add_argument("--snapshot-at", help='label for the embedded snapshot, e.g. "10-15 18:30Z" (default: fetch time)')
            c.add_argument("--force", action="store_true", help="write even if the check fails")
        c.set_defaults(fn=fn)

    s = sub.add_parser(
        "screenshot",
        help="headless Chrome screenshots, dark and light",
        description="Wrap a built page in the Artifact publish skeleton (which hides [hidden] elements, like the real viewer) and screenshot it in dark and light at each width. Without window.claude the page shows its embedded snapshot, which uses the same render code as live data. Skips with a warning when no browser is found.",
    )
    s.add_argument("page")
    s.add_argument("--out-dir", default="shots")
    s.add_argument("--browser", help="path to chrome / chromium / chrome-headless-shell (default: auto-detect, or $CHROME)")
    s.add_argument("--widths", type=int, nargs="+", default=[1280, 390])
    s.add_argument("--height", type=int, default=1300)
    s.add_argument("--wait-ms", type=int, default=3000, help="virtual time to let scripts run (default 3000)")
    s.set_defaults(fn=cmd_screenshot)

    k = sub.add_parser(
        "smoke",
        help="run the page scripts with browser-like globals (needs node)",
        description="Run the built page's scripts in node with a non-configurable `top` global and a stub DOM, in three modes: no window.claude, claude.use throwing, and a fake live db + comments. Fails if a script throws, the SVG keeps its hidden attribute, the snapshot is not shown, a verdict does not recolor its box, or feedback and the batch button do not reach the fake sendToClaude. Complements, not replaces, `screenshot`.",
    )
    k.add_argument("page")
    k.set_defaults(fn=cmd_smoke)

    e = sub.add_parser(
        "seed",
        help="ArtifactData batch payloads for nodes and edges",
        description="Write one JSON file per row plus batch-N.json files (at most 50 writes each) of ArtifactData writes. Pass a batch file's contents as `writes` to ArtifactData action=batch. To update rows that already exist, save the ArtifactData `list` output for nodes and edges to text files and pass them with --listing, so each write is pinned with if_version. Only nodes and edges are written; reviews and feedback are never touched.",
    )
    e.add_argument("rows", help="rows JSON from `build`")
    e.add_argument("--out-dir", default="seed")
    e.add_argument("--listing", nargs="*", help="files holding ArtifactData list output for nodes/edges (for if_version pins)")
    e.add_argument("--prune", action="store_true", help="also delete listed nodes/edges that are no longer in the rows")
    e.add_argument("--inline", action="store_true", help="put row data inline in the batch instead of file_path entries")
    e.set_defaults(fn=cmd_seed)

    i = sub.add_parser(
        "inbox",
        help="reviews + open feedback -> action list",
        description="Read the reviews and feedback rows (an ArtifactData out_dir, a JSON array, or the pasted text of a list/query result) and print one action per verdict and per open feedback row, flagging verdicts and feedback given on an older head than the node's current one.",
    )
    i.add_argument("rows", help="rows JSON from `build` (current heads, owners)")
    i.add_argument("--reviews", help="reviews rows (dir, JSON, or pasted ArtifactData output)")
    i.add_argument("--feedback", help="feedback rows (dir, JSON, or pasted ArtifactData output)")
    i.set_defaults(fn=cmd_inbox)

    q = sub.add_parser(
        "patchid",
        help="patch-id of a base..head diff",
        description="Compute `git patch-id --stable` of the GitHub compare diff base...head (via `gh api`). Use it to backfill the patch-id of an approval given before patch-ids were recorded: pass the PR's base and head as they were when it was approved, and --record to add it to the patch-id history that `fetch` and `build` use.",
    )
    q.add_argument("--repo", required=True)
    q.add_argument("--base", required=True)
    q.add_argument("--head", required=True)
    q.add_argument("--record", help="patch-history.json to add the result to")
    q.add_argument("--key", help="history key, owner/repo#number (needed with --record)")
    q.set_defaults(fn=cmd_patchid)

    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
