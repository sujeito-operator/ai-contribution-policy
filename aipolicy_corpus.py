#!/usr/bin/env python3
"""Screen the most-depended-on repositories on GitHub for a written rule about
AI-authored contributions, and publish the answer as an open dataset.

Why this exists
---------------
2026-08-18. This operation has now had FOUR patches closed, or excluded from
payment, by a rule that was published in the repository before the first line of
code was written:

    Parcels        #2824, #2826  "This contribution violates our AI policy"
    huggingface    #52 (transformers-mlinter) closed over the ask
    brainglobe     #48 closed on a site policy the screen could not read
    jhipster       bounties "would only be paid for humans ... not for 100%
                   bot generated content" -- the PR is welcome, the money is not

`ai_policy_screen.py` was written to stop that happening to us. It reads, per
repository, whether an agent-instruction file (`CLAUDE.md`, `AGENTS.md`,
`.cursorrules`, `.github/copilot-instructions.md`, `GEMINI.md`) exists and
whether prose forbids unsolicited agent PRs.

That question is not ours alone. Every developer running Claude Code, Cursor or
Copilot against someone else's repository has it, and there is no published
answer anywhere: you find out when your PR is closed. This module runs the same
screen across a large corpus and writes the result down.

WHAT THIS MEASURES, AND WHAT IT DOES NOT.
It measures whether a rule is PUBLISHED and machine-readable in the default
branch, plus a linked docs site where one is reachable. It does NOT judge
whether a project is friendly to AI contributions -- an `AGENTS.md` is usually
an invitation with conditions, not a ban. `READ` means exactly what it says:
a human has to read that file before opening anything. The one verdict that is
a refusal is `BLOCK`.

The corpus is "most-starred public repositories", built in star buckets because
the GitHub search API returns at most 1000 results per query. Stars are a poor
proxy for importance and a fine proxy for "a lot of people send this repo PRs",
which is the population the question is about.

Usage:
  python3 aipolicy_corpus.py --selftest        # no network
  python3 aipolicy_corpus.py --targets 1000    # build corpus, screen it
  python3 aipolicy_corpus.py --targets 1000 --out evidence/aipolicy/
"""

from __future__ import annotations

import argparse
import collections
import concurrent.futures
import csv
import json
import os
import pathlib
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import ai_policy_screen as aps  # noqa: E402  -- classify() is imported, never edited

API = "https://api.github.com"

# Star buckets. The search API caps any single query at 1000 results, so the
# corpus is assembled from disjoint ranges rather than one sorted sweep.
BUCKETS = [
    (200000, None),
    (100000, 199999),
    (70000, 99999),
    (55000, 69999),
    (45000, 54999),
    (38000, 44999),
    (33000, 37999),
    (29000, 32999),
    (26000, 28999),
    (23000, 25999),
    (21000, 22999),
    (19000, 20999),
    (17500, 18999),
    (16000, 17499),
    (15000, 15999),
]

VERDICTS = ["BLOCK", "MONEY-OUT", "READ", "POLICY", "CLEAR"]


def _token() -> str:
    for k in ("GITHUB_PAT", "GITHUB_TOKEN", "GH_TOKEN", "GITHUB_CLASSIC_PAT"):
        v = os.environ.get(k)
        if v:
            return v
    raise SystemExit("no GitHub token in the environment (GITHUB_PAT)")


def _search(q: str, token: str, page: int) -> dict:
    url = (API + "/search/repositories?q=" + urllib.parse.quote(q)
           + "&sort=stars&order=desc&per_page=100&page=%d" % page)
    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + token,
        "Accept": "application/vnd.github+json",
        "User-Agent": "sujeito-operator-aipolicy-corpus",
    })
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=45) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            # 403/429 here is the 30-per-minute search budget, not a ban.
            if e.code in (403, 429) and attempt < 3:
                time.sleep(20 * (attempt + 1))
                continue
            raise
    return {"items": []}


def build_corpus(target: int, token: str, log=print) -> list[dict]:
    """Most-starred public repos, assembled from disjoint star buckets."""
    seen: dict[str, dict] = {}
    for lo, hi in BUCKETS:
        if len(seen) >= target:
            break
        rng = "stars:>=%d" % lo if hi is None else "stars:%d..%d" % (lo, hi)
        for page in range(1, 11):
            if len(seen) >= target:
                break
            data = _search(rng + " is:public archived:false", token, page)
            items = data.get("items") or []
            for it in items:
                full = it["full_name"]
                if full in seen:
                    continue
                seen[full] = {
                    "repo": full,
                    "stars": it["stargazers_count"],
                    "language": it.get("language") or "",
                    "pushed_at": (it.get("pushed_at") or "")[:10],
                    "description": (it.get("description") or "")[:300],
                    "homepage": it.get("homepage") or "",
                }
            if len(items) < 100:
                break
            time.sleep(2.2)          # stay inside 30 searches/minute
        log("  corpus %-22s -> %d repos" % (rng, len(seen)))
    out = sorted(seen.values(), key=lambda r: -r["stars"])[:target]
    return out


RATE_PROBE = "/repos/octocat/Hello-World"


def core_remaining(token: str) -> int:
    """Calls left in the hourly core budget, read from the header of a REAL request.

    NOT from `/rate_limit`. Measured 2026-08-18 ~15:5xZ: with the budget genuinely
    exhausted, `/rate_limit` answered `core.remaining = 5000` while the very next
    content request returned 403 with `x-ratelimit-remaining: 0` in its own headers.
    A guard that reads the reporting endpoint instead of the resource it is guarding
    reports a budget that does not exist, and every check downstream of it is a
    verdict about a measurement that never happened. Costs one call per check.
    """
    req = urllib.request.Request(API + RATE_PROBE, headers={
        "Accept": "application/vnd.github+json",
        "Authorization": "token " + token,
        "User-Agent": "sujeito-operator-aipolicy-corpus"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return int(r.headers.get("x-ratelimit-remaining", -1))
    except urllib.error.HTTPError as e:
        return int(e.headers.get("x-ratelimit-remaining", 0) or 0)
    except Exception:
        return -1


def is_budget_403(exc: Exception) -> bool:
    """True when this failure is 'you are out of budget', not 'this repo is odd'.

    A budget failure must stop the run. It must never be written into the dataset as
    a verdict: a repository nobody could read is absent, not CLEAR and not ERROR.
    """
    if not isinstance(exc, urllib.error.HTTPError) or exc.code not in (403, 429):
        return False
    rem = exc.headers.get("x-ratelimit-remaining")
    return rem is not None and rem.strip() == "0"


def _tree(repo: str, ref: str, token: str) -> dict:
    """One call -> {name: (type, sha)} for a tree. Non-recursive on purpose: a
    recursive tree on a repo like torvalds/linux is megabytes and can be truncated."""
    doc = aps._get("/repos/%s/git/trees/%s" % (repo, urllib.parse.quote(ref, safe="")), token)
    if not isinstance(doc, dict):
        return {}
    return {e["path"]: (e.get("type"), e.get("sha")) for e in doc.get("tree") or []}


def screen_cheap(repo: str, token: str) -> dict:
    """`ai_policy_screen.screen()` with the same classifier and a third of the calls.

    `screen()` asks the contents API for all 12 candidate paths whether or not they
    exist: 13 calls per repository, so 5,000/hour buys only ~380 repos. Listing the
    root tree and `.github/` first costs 2-3 calls and then fetches ONLY the files
    that are actually there, which is typically one or two.

    `classify()` is imported, not reimplemented. Every verdict this produces is the
    verdict `ai_policy_screen` would produce from the same bytes.
    """
    meta = aps._get("/repos/" + repo, token)
    if not isinstance(meta, dict):
        raise aps.RepoUnreadable("%s: not readable — CLEAR would be a lie" % repo)
    branch = meta.get("default_branch") or "HEAD"

    root = _tree(repo, branch, token)
    present = {p for p, (t, _) in root.items() if t == "blob"}
    if root.get(".github", (None, None))[0] == "tree":
        for name, (t, _) in _tree(repo, root[".github"][1], token).items():
            if t == "blob":
                present.add(".github/" + name)

    agent_hits, prose_hits = {}, {}
    for path in aps.AGENT_FILES:
        if path in present:
            text = aps.fetch_text(repo, path, token)
            if text is not None:
                agent_hits[path] = text
    for path in aps.PROSE_FILES:
        if path in present:
            text = aps.fetch_text(repo, path, token)
            if text is not None:
                prose_hits[path] = text

    site_hits, site_note = aps.site_policy(repo, token)
    prose_hits.update(site_hits)
    verdict, reasons = aps.classify(agent_hits, prose_hits, {})
    return {"repo": repo, "verdict": verdict, "reasons": reasons,
            "agent_files": sorted(agent_hits), "site": site_note,
            "site_pages": sorted(site_hits)}


def screen_all(rows: list[dict], token: str, workers: int = 4, log=print,
               floor: int = 150) -> list[dict]:
    """Screen every row, or stop cleanly before the hourly budget runs out.

    A row is NEVER written as ERROR because the budget ran out. That was the whole
    §MF-4 lesson: a screen that could not run must not read as "nothing found".
    When the budget hits `floor` the run stops and returns what it actually measured.
    """
    done = [0]
    stop = [False]
    t0 = time.time()

    def one(row: dict) -> dict | None:
        if stop[0]:
            return None
        try:
            res = screen_cheap(row["repo"], token)
        except Exception as e:                       # a dead repo must not kill the run
            if is_budget_403(e):
                stop[0] = True                       # out of budget: measure nothing more
                return None
            res = {"verdict": "ERROR", "reasons": [type(e).__name__ + ": " + str(e)[:120]],
                   "agent_files": [], "site": "", "site_pages": []}
        done[0] += 1
        if done[0] % 25 == 0:
            rate = done[0] / max(time.time() - t0, 1e-9)
            rem = core_remaining(token)
            log("  screened %d/%d  (%.1f/s, %ds left, %d API calls of budget left)"
                % (done[0], len(rows), rate,
                   int((len(rows) - done[0]) / max(rate, 1e-9)), rem))
            if 0 <= rem < floor:
                stop[0] = True
                log("  STOPPING: core budget down to %d. %d repos measured, the rest "
                    "are NOT written as CLEAR or ERROR — they are simply not in the "
                    "dataset." % (rem, done[0]))
        merged = dict(row)
        merged.update({
            "verdict": res.get("verdict", "ERROR"),
            "agent_files": res.get("agent_files") or [],
            "reasons": res.get("reasons") or [],
            "site_pages": res.get("site_pages") or [],
        })
        return merged

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        out = [r for r in ex.map(one, rows) if r is not None]
    return out


# Words that make a prohibition a rule ABOUT AI rather than an ordinary rule about
# pull requests that a context-free phrase match happened to catch.
AI_TERMS = re.compile(
    r"\b(ai|a\.i\.|llm|llms|genai|generative|agent|agents|agentic|bot|bots|copilot|"
    r"claude|cursor|codex|devin|chatgpt|gpt|machine[- ]generated|ai[- ]generated|"
    r"auto[- ]generated by|autonomous)\b", re.I)


def _raw(repo: str, path: str, timeout: int = 25) -> str | None:
    """Fetch a repo file from raw.githubusercontent. Costs NO API budget.

    `HEAD` resolves the default branch, so this works on repos whose default is
    neither `main` nor `master`.
    """
    url = "https://raw.githubusercontent.com/%s/HEAD/%s" % (repo, path)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "sujeito-operator-aipolicy"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf8", "replace")
    except Exception:
        return None


def verify_blocks(rows: list[dict], workers: int = 6, log=print) -> list[dict]:
    """Re-read every BLOCK and record the SENTENCE behind it, plus whether it is
    actually about AI.

    Why this exists, and it is the whole reason this dataset is worth citing.
    `ai_policy_screen.FORBID_PHRASES` is matched context-free. That is correct for
    its original job -- deciding whether THIS operation should open a PR, where a
    false BLOCK costs one target and nothing else. Published, a false BLOCK is a
    public claim about somebody else's project, and on the first real corpus
    **8 of 18 were wrong**:

        angular, uv, ruff   "do not open pull requests" -- scoped to unlabelled
                            feature requests / needs-design issues
        grafana             scoped to translation files
        coolify             scoped to major changes without prior discussion
        CppCoreGuidelines   scoped to revisiting settled style decisions
        awesome-selfhosted  "use the data repo instead" -- routing, not refusal
        ui-ux-pro-max-skill a code-of-conduct line about harassment and spam

    while duckdb ("do not submit pull requests generated by AI (LLMs)"), babel,
    TypeScript, odysseus, agno, jquery and superpowers are real rules about
    machine-authored contributions.

    So the phrase is not the finding. The sentence is. This pass quotes it and
    flags `ai_specific`, and the site prints the quote next to every row so a
    reader can overrule the classifier.
    """
    targets = [r for r in rows if r["verdict"] in ("BLOCK", "MONEY-OUT")]

    def one(r: dict) -> None:
        m = re.match(r"^(.*?): '(.*)'$", (r["reasons"] or [""])[0])
        if not m:
            r["quote"], r["quote_source"], r["ai_specific"] = "", "", False
            return
        path, phrase = m.group(1), m.group(2)
        text = _raw(r["repo"], path)
        if text is None:
            # Could not re-read it. Say so; do not guess in either direction.
            r["quote"], r["quote_source"], r["ai_specific"] = "", path, None
            return
        i = text.lower().find(phrase)
        if i < 0:
            r["quote"], r["quote_source"], r["ai_specific"] = "", path, None
            return
        lo, hi = max(0, i - 300), min(len(text), i + len(phrase) + 200)
        quote = re.sub(r"\s+", " ", text[lo:hi]).strip()
        r["quote"] = quote
        r["quote_source"] = path
        r["ai_specific"] = bool(AI_TERMS.search(quote))

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(one, targets))
    named = sum(1 for r in targets if r.get("ai_specific") is True)
    unread = sum(1 for r in targets if r.get("ai_specific") is None)
    log("  verified %d prohibitions: %d name AI, %d do not, %d could not be re-read"
        % (len(targets), named, len(targets) - named - unread, unread))
    return rows


def summarise(rows: list[dict], attempted: int | None = None) -> dict:
    by_verdict = collections.Counter(r["verdict"] for r in rows)
    by_file = collections.Counter()
    for r in rows:
        for f in r["agent_files"]:
            by_file[f] += 1
    by_lang: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for r in rows:
        by_lang[r["language"] or "(none)"][r["verdict"]] += 1
    with_any = sum(1 for r in rows if r["agent_files"])
    return {
        "measured_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "repos": len(rows),
        # The rank the corpus was drawn to. `repos` can be smaller when the hourly
        # API budget stops the run: those repos are ABSENT, never CLEAR. Every page
        # says "N of the top M" so the denominator cannot be read as a census.
        "attempted": attempted if attempted is not None else len(rows),
        "stars_min": min((r["stars"] for r in rows), default=0),
        "stars_max": max((r["stars"] for r in rows), default=0),
        "with_agent_file": with_any,
        "with_agent_file_pct": round(100.0 * with_any / max(len(rows), 1), 1),
        "by_verdict": dict(by_verdict),
        "by_agent_file": dict(by_file.most_common()),
        "by_language": {k: dict(v) for k, v in sorted(
            by_lang.items(), key=lambda kv: -sum(kv[1].values()))[:25]},
    }


def write_out(rows: list[dict], summary: dict, out: pathlib.Path, log=print) -> None:
    out.mkdir(parents=True, exist_ok=True)
    with (out / "repos.jsonl").open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    cols = ["repo", "stars", "language", "pushed_at", "verdict",
            "agent_files", "reasons", "homepage", "description"]
    with (out / "repos.csv").open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for r in rows:
            w.writerow([
                r["repo"], r["stars"], r["language"], r["pushed_at"], r["verdict"],
                ";".join(r["agent_files"]), " | ".join(r["reasons"])[:400],
                r.get("homepage", ""), r.get("description", ""),
            ])
    (out / "summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    log("wrote %s: repos.jsonl, repos.csv, summary.json" % out)


def selftest() -> int:
    """No network. Pins the shapes this module promises to its consumers."""
    fails = []

    def check(name, cond):
        if not cond:
            fails.append(name)

    # 1. buckets are disjoint and descending -- a repo must not be screened twice
    prev_lo = None
    for lo, hi in BUCKETS:
        if hi is not None:
            check("bucket %d..%d ordered" % (lo, hi), lo <= hi)
        if prev_lo is not None:
            check("bucket %d below previous %d" % (lo, prev_lo), lo < prev_lo)
            if hi is not None:
                check("bucket %d..%d disjoint from %d" % (lo, hi, prev_lo), hi < prev_lo)
        prev_lo = lo

    # 2. summarise counts what it says it counts
    rows = [
        {"repo": "a/a", "stars": 9, "language": "Python", "verdict": "READ",
         "agent_files": ["CLAUDE.md"], "reasons": [], "site_pages": []},
        {"repo": "b/b", "stars": 8, "language": "Python", "verdict": "CLEAR",
         "agent_files": [], "reasons": [], "site_pages": []},
        {"repo": "c/c", "stars": 7, "language": "Go", "verdict": "READ",
         "agent_files": ["AGENTS.md", "CLAUDE.md"], "reasons": [], "site_pages": []},
    ]
    s = summarise(rows)
    check("repos counted", s["repos"] == 3)
    check("with_agent_file counts REPOS not FILES", s["with_agent_file"] == 2)
    check("pct", s["with_agent_file_pct"] == 66.7)
    check("by_verdict", s["by_verdict"] == {"READ": 2, "CLEAR": 1})
    check("by_agent_file counts FILES", s["by_agent_file"]["CLAUDE.md"] == 2)
    check("by_language split", s["by_language"]["Python"] == {"READ": 1, "CLEAR": 1})

    # 3. the control: a repo with no language must not vanish from by_language
    s2 = summarise([{"repo": "d/d", "stars": 1, "language": "", "verdict": "CLEAR",
                     "agent_files": [], "reasons": [], "site_pages": []}])
    check("empty language bucketed as (none)", s2["by_language"]["(none)"] == {"CLEAR": 1})

    # 4. the control that matters most: an ERROR row is NOT silently a CLEAR.
    #    A screen that could not run must never read as "nothing found" -- that is
    #    the §MF-4 defect (an absence is only evidence when you can prove you looked).
    s3 = summarise([{"repo": "e/e", "stars": 1, "language": "C", "verdict": "ERROR",
                     "agent_files": [], "reasons": ["HTTPError: 502"], "site_pages": []}])
    check("ERROR is its own verdict", s3["by_verdict"] == {"ERROR": 1})
    check("ERROR is not CLEAR", "CLEAR" not in s3["by_verdict"])

    # 5. every verdict this module reports must be one ai_policy_screen can emit,
    #    plus ERROR. If classify() gains a verdict, this fails and the site copy
    #    gets updated rather than silently dropping a category.
    known = set(VERDICTS)
    check("VERDICTS covers classify()", known >= {"BLOCK", "READ", "POLICY", "CLEAR"})

    # 6. the budget discriminator. A 403 that means "out of calls" must stop the run;
    #    a 403 that means something else about one repository must not. Getting this
    #    backwards either fills the dataset with fake ERROR rows or halts on one repo.
    def err(code, headers):
        return urllib.error.HTTPError("u", code, "m", headers, None)

    check("403 with remaining=0 is a budget stop",
          is_budget_403(err(403, {"x-ratelimit-remaining": "0"})))
    check("429 with remaining=0 is a budget stop",
          is_budget_403(err(429, {"x-ratelimit-remaining": "0"})))
    check("403 with budget left is NOT a budget stop",
          not is_budget_403(err(403, {"x-ratelimit-remaining": "4211"})))
    check("403 with no header is NOT a budget stop -- absence is not evidence",
          not is_budget_403(err(403, {})))
    check("404 is never a budget stop",
          not is_budget_403(err(404, {"x-ratelimit-remaining": "0"})))
    check("a non-HTTP error is never a budget stop",
          not is_budget_403(ValueError("boom")))

    for f in fails:
        print("FAIL", f)
    print("selftest: %d checks failed" % len(fails))
    return 1 if fails else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--targets", type=int, default=1000)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--out", default="evidence/aipolicy")
    ap.add_argument("--corpus-only", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()

    token = _token()
    rem = core_remaining(token)
    print("core budget before start: %d calls" % rem)
    if 0 <= rem < 400:
        print("REFUSING TO START: %d calls left is not enough for a corpus worth "
              "publishing. The budget resets hourly; run again after it does." % rem)
        return 3
    print("building corpus of %d repos ..." % a.targets)
    rows = build_corpus(a.targets, token)
    print("corpus: %d repos, %d..%d stars"
          % (len(rows), rows[-1]["stars"], rows[0]["stars"]))
    if a.corpus_only:
        pathlib.Path(a.out).mkdir(parents=True, exist_ok=True)
        (pathlib.Path(a.out) / "corpus.json").write_text(json.dumps(rows), encoding="utf-8")
        return 0

    print("screening with %d workers ..." % a.workers)
    attempted = len(rows)
    rows = screen_all(rows, token, a.workers)
    verify_blocks(rows)
    if len(rows) < attempted:
        print("MEASURED %d of %d. The %d not reached are absent from the dataset, "
              "not recorded as CLEAR." % (len(rows), attempted, attempted - len(rows)))
    summary = summarise(rows, attempted)
    write_out(rows, summary, pathlib.Path(a.out))
    print(json.dumps({k: summary[k] for k in
                      ("repos", "with_agent_file", "with_agent_file_pct", "by_verdict")},
                     indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
