from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import List

VERSION_LABEL_RE = re.compile(r"^v\d{1,2}$|(^v\d{1,2}\.\d{1,2}$)")
PENDING_LABEL = "Backport Pending"


@dataclass
class PRInfo:
    number: int
    labels: List[str]
    merged: bool

def load_event() -> dict:
    path = os.environ.get("GITHUB_EVENT_PATH")
    if not path or not os.path.exists(path):
        print("::warning::GITHUB_EVENT_PATH not set or file missing; nothing to do", file=sys.stderr)
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def extract_pr(event: dict) -> PRInfo | None:
    pr = event.get("pull_request")
    if not pr:
        return None
    labels = [lbl.get("name", "") for lbl in pr.get("labels", [])]
    return PRInfo(number=pr["number"], labels=labels, merged=pr.get("merged", False))

def needs_pending_label(info: PRInfo) -> bool:
    has_version_label = any(VERSION_LABEL_RE.match(l) for l in info.labels)
    has_pending = PENDING_LABEL in info.labels
    return info.merged and (not has_version_label) and (not has_pending)

def add_label(pr_number: int, label: str) -> None:
    repo = os.environ.get("GITHUB_REPOSITORY")
    token = os.environ.get("GITHUB_TOKEN")
    if not repo or not token:
        print("::error::Missing GITHUB_REPOSITORY or GITHUB_TOKEN", file=sys.stderr)
        sys.exit(1)
    owner, repo_name = repo.split("/", 1)
    url = f"https://api.github.com/repos/{owner}/{repo_name}/issues/{pr_number}/labels"
    body = json.dumps({"labels": [label]}).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    try:
        with urllib.request.urlopen(req) as resp:
            if resp.status not in (200, 201):
                print(f"::error::Failed to add label: HTTP {resp.status}", file=sys.stderr)
                sys.exit(1)
            print(f"Added label '{label}' to PR #{pr_number}")
    except urllib.error.HTTPError as e:
        if e.code == 422:
            # Possibly already labeled concurrently; treat as success
            print(f"Label '{label}' already present or cannot add (422)")
        else:
            print(f"::error::HTTP error adding label: {e.code} {e.reason}", file=sys.stderr)
            sys.exit(1)
    except Exception as e:
        print(f"::error::Unexpected error adding label: {e}", file=sys.stderr)
        sys.exit(1)

"""
Label a merged PR with 'Backport pending' if it has no version label.

Expected environment:
  GITHUB_EVENT_PATH: Path to the event JSON (GitHub sets this automatically)
  GITHUB_REPOSITORY: owner/repo
  GITHUB_TOKEN: token with repo:issues scope (use GITHUB_TOKEN or a PAT)

This script is idempotent: if the PR already has a version label (vX.Y) or already
has the 'Backport Pending' label, it exits without error.
"""
def main() -> int:
    event = load_event()
    if not event:
        return 0
    info = extract_pr(event)
    if not info:
        print("No pull_request object in event; skipping")
        return 0
    if needs_pending_label(info):
        add_label(info.number, PENDING_LABEL)
    else:
        print("No label needed (either merged has version label or already pending)")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
