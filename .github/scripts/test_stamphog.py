"""Offline tests for the workflow's head/finalization guard and sticky publisher."""

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest


def load(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(f"{name}.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


publisher = load("stamphog-publish")
guard = load("stamphog-ready")
SHA = "a" * 40
REPO = "garnet-labs/cal.diy"


@pytest.fixture
def pr():
    return {
        "state": "open", "draft": False, "user": {"type": "User", "login": "alice"},
        "head": {"sha": SHA, "repo": {"full_name": REPO}}, "labels": [{"name": "stamphog"}],
    }


def comment(sha=SHA, status="finalized", login=guard.BOT):
    summary = json.dumps({"status": status, "commit": sha})
    return {"user": {"login": login}, "body": f"<!-- garnet:commit {sha} -->\n<!-- garnet:summary {summary} -->"}


def test_guard_ready(pr):
    assert guard.ready(pr, [comment()], REPO)


@pytest.mark.parametrize("comments", [
    [], [comment(status="pending")], [comment(sha="b" * 40)],
    [comment(login="untrusted")], [comment(), comment(status="pending")],
    [{"user": {"login": guard.BOT}, "body": "<!-- garnet:summary {bad} -->"}],
    [{"user": {"login": guard.BOT}, "body": f"<!-- garnet:commit {SHA} -->\n<!-- garnet:summary {{bad}} -->"}],
    [{"user": {"login": guard.BOT}, "body": "<!-- garnet:summary null -->"}],
    [{"user": {"login": guard.BOT}, "body": None}],
    [comment(), {"user": {"login": guard.BOT}, "body": "Recording runtime behavior"}],
])
def test_guard_rejects_missing_stale_untrusted_pending_or_malformed(pr, comments):
    assert not guard.ready(pr, comments, REPO)


@pytest.mark.parametrize("field,value", [
    ("draft", True), ("state", "closed"), ("labels", []),
    ("head", {"sha": SHA, "repo": {"full_name": "external/fork"}}),
    ("user", {"type": "Bot", "login": "automation"}),
    ("user", {"type": "User", "login": "automation[bot]"}),
])
def test_guard_rejects_ineligible_pr(pr, field, value):
    pr[field] = value
    assert not guard.ready(pr, [comment()], REPO)


def test_manual_dispatch_can_omit_label_but_not_evidence(pr):
    pr["labels"] = []
    assert guard.ready(pr, [comment()], REPO, require_label=False)
    assert not guard.ready(pr, [], REPO, require_label=False)


def test_guard_requires_both_marker_heads(pr):
    c = comment()
    c["body"] = c["body"].replace(f'"commit": "{SHA}"', f'"commit": "{"b" * 40}"')
    assert not guard.ready(pr, [c], REPO)
    assert not guard.ready(pr, [comment() | {"body": comment()["body"] * 2}], REPO)


def test_guard_main_reads_pages_once_and_emits_output(pr, monkeypatch, tmp_path):
    calls = []
    def gh(path, *args):
        calls.append((path, args))
        return pr if "/pulls/" in path else [[], [comment()]]
    output = tmp_path / "outputs"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setenv("REQUIRE_LABEL", "true")
    monkeypatch.setattr(sys, "argv", ["ready", REPO, "1"])
    monkeypatch.setattr(guard, "gh", gh)
    guard.main()
    assert output.read_text() == "ready=true\n"
    assert len(calls) == 2
    assert calls[-1][1] == ("--paginate", "--slurp")


def test_guard_api_failure_propagates(pr, monkeypatch):
    def gh(*args):
        raise subprocess.CalledProcessError(1, "gh")
    monkeypatch.setattr(guard, "gh", gh)
    monkeypatch.setattr(sys, "argv", ["ready", REPO, "1"])
    with pytest.raises(subprocess.CalledProcessError):
        guard.main()


@pytest.mark.parametrize("verdict", ["APPROVED", "REFUSED", "ESCALATE", "ERROR", "WAIT", "DRY-RUN", "AUTO-APPROVED"])
def test_publisher_cohort_envelope(verdict):
    data = {"head_sha": SHA, "final_verdict": verdict, "review_body": "Engine judgment."}
    body = publisher.render_comment(data)
    assert body.startswith("<!-- stamphog-sticky-review -->")
    match = re.search(r"reviewed `([0-9a-f]{40})` — verdict: \*\*([A-Z_]+)\*\*", body)
    assert match.groups() == (SHA, verdict.replace("-", "_"))
    assert body.endswith("Engine judgment.")


@pytest.mark.parametrize("data", [{}, {"head_sha": "abc", "final_verdict": "APPROVED"}, {"head_sha": SHA}, {"head_sha": SHA, "final_verdict": "bad ** APPROVED"}])
def test_publisher_rejects_missing_or_invalid_identity(data):
    with pytest.raises(ValueError):
        publisher.render_comment(data)


def test_gate_only_body():
    body = publisher.render_comment({"head_sha": SHA, "final_verdict": "REFUSED", "gates": [{"gate": "deny", "passed": False, "message": "Lifecycle changed."}]})
    assert "Lifecycle changed." in body
    assert "Gate mechanics and policy version" in body


@pytest.mark.parametrize("existing", [False, True])
def test_publisher_upserts_only_own_comments_and_removes_own_duplicates(existing, monkeypatch, tmp_path):
    verdict = tmp_path / "verdict.json"
    verdict.write_text(json.dumps({"head_sha": SHA, "final_verdict": "APPROVED"}))
    comments = [
        {"id": 1, "user": {"login": "alice"}, "body": publisher.MARKER},
        {"id": 2, "user": {"login": publisher.BOT}, "body": publisher.LEGACY_MARKERS[0]},
    ]
    if existing:
        comments.extend({"id": n, "user": {"login": publisher.BOT}, "body": publisher.MARKER} for n in (3, 4))
    calls = []
    def gh(args):
        calls.append(args)
        if "--paginate" in args:
            return json.dumps([comments[:1], comments[1:]])
        if "-F" in args:
            body_file = args[-1].split("@", 1)[1]
            assert f"reviewed `{SHA}`" in Path(body_file).read_text()
        return "{}"
    monkeypatch.setattr(publisher, "gh", gh)
    monkeypatch.setattr(sys, "argv", ["publish", str(verdict), REPO, "1"])
    publisher.main()
    assert calls[1][2] == ("PATCH" if existing else "POST")
    assert not any(args[-1].endswith("comments/1") for args in calls)
    deleted = [int(args[-1].rsplit("/", 1)[1]) for args in calls if "DELETE" in args]
    assert deleted == ([2, 3] if existing else [2])


def test_publisher_api_failure_propagates(monkeypatch, tmp_path):
    verdict = tmp_path / "verdict.json"
    verdict.write_text(json.dumps({"head_sha": SHA, "final_verdict": "APPROVED"}))
    def gh(args):
        raise subprocess.CalledProcessError(1, "gh")
    monkeypatch.setattr(publisher, "gh", gh)
    monkeypatch.setattr(sys, "argv", ["publish", str(verdict), REPO, "1"])
    with pytest.raises(subprocess.CalledProcessError):
        publisher.main()
