from __future__ import annotations
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
from urllib.parse import urlencode
from typing import Iterable, Dict, Any, List

ISO_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
VERSION_LABEL_RE = re.compile(r"^v\d{1,2}(\.\d{1,2})?$")
PENDING_LABEL_CANONICAL = "Backport Pending"
COMMENT_MARKER = f"<!-- backport-pending-reminder every {os.environ.get('PENDING_LABEL_AGE_DAYS', '7')} days -->"
REMINDER_BODY = f"A backport still appears to be pending for this merged PR. Please either:\n\n- Add an appropriate version label (e.g. v9.2) once backported, or\n- Remove the `{PENDING_LABEL_CANONICAL}` label if no backport will be performed.\n\nThank you!"

GITHUB_API = "https://api.github.com"


def gh_request(path: str, method: str = "GET", body: Dict[str, Any] | None = None, params: Dict[str, str] | None = None) -> Any:
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        print("::error::Missing GITHUB_TOKEN", file=sys.stderr)
        sys.exit(1)
    if params:
        path = f"{path}?{urlencode(params)}"
    url = f"{GITHUB_API}{path}"
    data = None
    if body is not None:
        data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    try:
        with urllib.request.urlopen(req) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            txt = resp.read().decode(charset)
            if resp.status >= 300:
                raise RuntimeError(f"HTTP {resp.status}: {txt}")
            if not txt.strip():
                return None
            return json.loads(txt)
    except urllib.error.HTTPError as e:
        err = e.read().decode()
        print(f"::error::HTTP {e.code} {e.reason} {err}", file=sys.stderr)
        raise
    except Exception as ex:
        print(f"::error::Unexpected error: {ex}", file=sys.stderr)
        raise


def list_prs(filter:str, since: dt.datetime) -> Iterable[Dict[str, Any]]:
    # Filter merged PRs updated since timeframe.
    # Format: repo:owner/name is:pr is:merged updated:>=YYYY-MM-DD
    q_date = since.strftime("%Y-%m-%d")
    page = 1
    while True:
        result = gh_request("/search/issues", params={
            "q": f"{filter} updated:>={q_date}",
            "per_page": "100",
            "page": str(page)
        })
        items = result.get("items", [])
        if not items:
            break
        for it in items:
            yield it
        if len(items) < 100:
            break
        page += 1
        time.sleep(0.25)


def get_pr(owner: str, repo: str, number: int) -> Dict[str, Any]:
    return gh_request(f"/repos/{owner}/{repo}/pulls/{number}")


def get_issue_comments(owner: str, repo: str, number: int) -> List[Dict[str, Any]]:
    comments: List[Dict[str, Any]] = []
    page = 1
    while True:
        data = gh_request(f"/repos/{owner}/{repo}/issues/{number}/comments", params={"per_page": "100", "page": str(page)})
        if not data:
            break
        comments.extend(data)
        if len(data) < 100:
            break
        page += 1
    return comments


def post_comment(owner: str, repo: str, number: int, body: str) -> None:
    gh_request(f"/repos/{owner}/{repo}/issues/{number}/comments", method="POST", body={"body": body})
    print(f"Posted reminder comment on PR #{number}")


def has_version_label(labels: List[Dict[str, Any]]) -> bool:
    return any(VERSION_LABEL_RE.match(lbl.get("name", "")) for lbl in labels)


def has_pending_label(labels: List[Dict[str, Any]]) -> bool:
    names_lower = {lbl.get("name", "").lower() for lbl in labels}
    return PENDING_LABEL_CANONICAL.lower() in names_lower


def last_reminder_time(comments: List[Dict[str, Any]]) -> dt.datetime | None:
    """Return timestamp of the newest reminder comment (first match in descending order)."""
    def comment_ts(c: Dict[str, Any]) -> dt.datetime:
        ts_raw = c.get("created_at") or c.get("updated_at")
        if not ts_raw:
            raise RuntimeError(f"Comment {c.get('id')}, {c.get('body')} missing both created_at and updated_at timestamps")
        return dt.datetime.strptime(ts_raw, ISO_FORMAT).replace(tzinfo=dt.timezone.utc)

    for c in sorted(comments, key=comment_ts, reverse=True):
        if COMMENT_MARKER in (c.get("body") or ""):
            return comment_ts(c)
    return None


"""Scan recently merged PRs that still have the Backport Pending label after a
configured number of days and post a reminder comment if they have not yet
received a version label.

Runs under a scheduled workflow or manual dispatch.
Environment:
    GITHUB_REPOSITORY       (owner/repo)
    GITHUB_TOKEN            (token with repo scope)
    LOOKBACK_DAYS           (optional, default 7) - how far back to search merged PRs
    PENDING_LABEL_AGE_DAYS  (optional, default 7) - minimum merge age before reminding (in days)

Logic:
  - List merged PRs updated within LOOKBACK_DAYS.
  - For each PR: if it has canonical Backport Pending label (case-insensitive), lacks any vX.Y label, and merged_at older than threshold -> check if we already reminded in last run (presence of a comment marker) else add comment.

We add a hidden marker in the comment body so we don't duplicate.
"""


def main() -> int:
    repo_full = os.environ.get("GITHUB_REPOSITORY")
    if not repo_full or "/" not in repo_full:
        print("::error::Invalid GITHUB_REPOSITORY", file=sys.stderr)
        return 1
    owner, repo = repo_full.split("/", 1)

    lookback_days = int(os.environ.get("LOOKBACK_DAYS", "7"))
    age_days = int(os.environ.get("PENDING_LABEL_AGE_DAYS", "7"))
    now = dt.datetime.now(dt.timezone.utc)
    since = now - dt.timedelta(days=lookback_days)
    threshold = now - dt.timedelta(days=age_days)

    reminders: List[Dict[str, Any]] = []
    
    for item in list_prs(f"repo:{owner}/{repo} is:pr is:merged", since):
        number = item.get("number") or int(item.get("url", "/").rstrip("/").split("/")[-1])
        pr = get_pr(owner, repo, number)

        labels = pr.get("labels", [])
        if has_pending_label(labels) or has_version_label(labels):
            continue

        comments = get_issue_comments(owner, repo, number)
        prev_time = last_reminder_time(comments)
        if not prev_time or prev_time > threshold:
            continue

        author = pr.get("user", {}).get("login", "PR author")
        post_comment(owner, repo, number, f"{COMMENT_MARKER}\n@{author}\n{REMINDER_BODY}")
        reminders.append({"pr": number, "author": author, "time_since_last_reminder": str(prev_time - threshold)})

    print(f"Reminders posted: {', '.join(f'#{r['pr']} (to @{r['author']})\nTime since last reminder: {r['time_since_last_reminder']}' for r in reminders)}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
