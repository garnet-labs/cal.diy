#!/usr/bin/env python3
"""Read once; a later Garnet comment event retries a pending review."""

import json
import os
import re
import subprocess
import sys

BOT = "garnet-runtime-review[bot]"
SUMMARY = re.compile(r"<!--\s*garnet:summary\s+(\{.*?\})\s*-->", re.DOTALL)
COMMIT = re.compile(r"<!--\s*garnet:commit\s+([0-9a-f]{40})\s*-->")


def ready(pr, comments, repo, require_label=True):
    """Require an eligible PR and the last trusted comment's finalized head."""
    head = pr.get("head", {})
    author = pr.get("user", {})
    if (
        pr.get("state") != "open"
        or pr.get("draft") is not False
        or head.get("repo", {}).get("full_name") != repo
        or author.get("type") == "Bot"
        or "[bot]" in author.get("login", "")
        or (require_label and not any(label.get("name") == "stamphog" for label in pr.get("labels", [])))
    ):
        return False
    trusted = [c for c in comments if c.get("user", {}).get("login") == BOT]
    if not trusted:
        return False
    body = trusted[-1].get("body") or ""
    summaries = SUMMARY.findall(body)
    commits = COMMIT.findall(body)
    if len(summaries) != 1 or commits != [head.get("sha")]:
        return False
    try:
        summary = json.loads(summaries[0])
    except json.JSONDecodeError:
        return False
    return isinstance(summary, dict) and summary.get("status") == "finalized" and summary.get("commit") == head.get("sha")


def gh(path, *args):
    result = subprocess.run(["gh", "api", path, *args], check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


def main():
    repo, number = sys.argv[1:]
    pr = gh(f"repos/{repo}/pulls/{number}")
    pages = gh(f"repos/{repo}/issues/{number}/comments?per_page=100", "--paginate", "--slurp")
    comments = [comment for page in pages for comment in page]
    value = ready(pr, comments, repo, os.environ.get("REQUIRE_LABEL", "true") == "true")
    with open(os.environ["GITHUB_OUTPUT"], "a") as output:
        output.write(f"ready={str(value).lower()}\n")
    print("Finalized current-head evidence is ready." if value else "Review deferred: PR eligibility or finalized current-head evidence is missing; no polling.")


if __name__ == "__main__":
    main()
