import os
import sys
import time
import requests
from datetime import datetime

TOKEN = os.getenv("GITHUB_TOKEN")
if not TOKEN:
    print("Missing GITHUB_TOKEN")
    sys.exit(1)

LABEL_NAME = os.getenv("LABEL_NAME", "backport-pending")
TARGET_BRANCH = os.getenv("TARGET_BRANCH", "master")
AFTER_DAYS = int(os.getenv("REMIND_AFTER_DAYS", "7"))
EVERY_DAYS = int(os.getenv("REMIND_EVERY_DAYS", "7"))
MARKER = os.getenv("MARKER", "[backport-pending-reminder]")

GITHUB_REPOSITORY = os.getenv("GITHUB_REPOSITORY")
if not GITHUB_REPOSITORY or "/" not in GITHUB_REPOSITORY:
    print("Cannot parse OWNER/REPO from GITHUB_REPOSITORY")
    sys.exit(1)
OWNER, REPO = GITHUB_REPOSITORY.split("/")

HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Accept": "application/vnd.github+json",
    "User-Agent": "backport-pending-reminder-script",
}

def api(path, method="GET", data=None):
    url = f"https://api.github.com{path}"
    resp = requests.request(method, url, headers=HEADERS, json=data)
    if resp.status_code == 403 and resp.headers.get("x-ratelimit-remaining") == "0":
        reset = int(resp.headers.get("x-ratelimit-reset", "0"))
        wait_sec = max(0, reset - int(time.time())) + 1
        print(f"Rate-limited. Sleeping {wait_sec}s...")
        time.sleep(wait_sec)
        return api(path, method, data)
    if not resp.ok:
        print(f"{method} {path} failed: {resp.status_code} {resp.text}")
        sys.exit(1)
    return resp.json()

def paginate(path, params=None):
    results = []
    page = 1
    params = params or {}
    while True:
        params.update({"per_page": 100, "page": page})
        res = api(f"{path}?{'&'.join(f'{k}={v}' for k, v in params.items())}")
        if not isinstance(res, list) or not res:
            break
        results.extend(res)
        if len(res) < 100:
            break
        page += 1
    return results

def days_between(a, b):
    # 5 minutes as "1 day" for testing
    return int((a - b).total_seconds() // (60 * 5))

def list_prs_with_label(label):
    # Get all PRs (any state) with the label
    prs = []
    for state in ["open", "closed"]:
        prs.extend(paginate(f"/repos/{OWNER}/{REPO}/issues", {"state": state, "labels": label}))
    # Filter only PRs (not issues)
    return [pr for pr in prs if "pull_request" in pr]

def get_pull(pull_number):
    return api(f"/repos/{OWNER}/{REPO}/pulls/{pull_number}")

def list_issue_events(number):
    return paginate(f"/repos/{OWNER}/{REPO}/issues/{number}/events")

def list_comments(number):
    return paginate(f"/repos/{OWNER}/{REPO}/issues/{number}/comments")

def create_comment(number, body):
    return api(f"/repos/{OWNER}/{REPO}/issues/{number}/comments", method="POST", data={"body": body})

def run():
    now = datetime.utcnow()
    print(f"Repo: {OWNER}/{REPO}")
    print(f"Target branch: {TARGET_BRANCH}")
    print(f"Threshold: {AFTER_DAYS}d | Re-reminder: {EVERY_DAYS}d")
    prs = list_prs_with_label(LABEL_NAME)

    for pr_issue in prs:
        number = pr_issue["number"]

        pr = get_pull(number)
        base_ref = pr.get("base", {}).get("ref")
        if base_ref != TARGET_BRANCH:
            continue

        events = list_issue_events(number)
        labeled_events = sorted(
            [e for e in events if e.get("event") == "labeled" and e.get("label", {}).get("name") == LABEL_NAME],
            key=lambda e: e["created_at"], reverse=True
        )
        if not labeled_events:
            print(f"#{number}: no labeled event found (label may be pre-existing), skipping.")
            continue

        labeled_at = datetime.strptime(labeled_events[0]["created_at"], "%Y-%m-%dT%H:%M:%SZ")
        age_days = days_between(now, labeled_at)
        if age_days < AFTER_DAYS:
            print(f"#{number}: label age {age_days}d < {AFTER_DAYS}d, skipping.")
            continue

        comments = list_comments(number)
        recent_reminder_comments = sorted(
            [c for c in comments if (c.get("user", {}).get("type") == "Bot" or "github-actions" in c.get("user", {}).get("login", "")) and MARKER in c.get("body", "")],
            key=lambda c: c["created_at"], reverse=True
        )
        if recent_reminder_comments:
            last_at = datetime.strptime(recent_reminder_comments[0]["created_at"], "%Y-%m-%dT%H:%M:%SZ")
            since = days_between(now, last_at)
            if since < EVERY_DAYS:
                print(f"#{number}: reminded {since}d ago (< {EVERY_DAYS}d), skipping.")
                continue

        author = f"@{pr.get('user', {}).get('login', '')}" if pr.get('user', {}).get('login') else ""
        requested_users = [f"@{u['login']}" for u in pr.get("requested_reviewers", [])]
        requested_teams = [f"@{pr['base']['repo']['owner']['login']}/{t['slug']}" for t in pr.get("requested_teams", [])]
        mentions = " ".join(filter(None, [author] + requested_users + requested_teams))

        body = f"""{MARKER}
{mentions}

This pull request targets `{TARGET_BRANCH}` and has the `{LABEL_NAME}` label for **{age_days} days**.
Please review next steps for backporting (or remove the label if no longer needed).

- Threshold: `{AFTER_DAYS}d`
- Re-reminder interval: `{EVERY_DAYS}d`
"""
        create_comment(number, body)
        print(f"#{number}: posted reminder (age {age_days}d).")
        time.sleep(0.2)

    print("Done.")

if __name__ == "__main__":
    try:
        run()
    except Exception as e:
        print(e)