"""Transcript ingestion: dedupe per response, incremental offsets, partial lines, rotation."""
import json
import os
import time
from datetime import datetime, timezone

import pytest

from cctripwire.config import Config
from cctripwire.quota import transcripts as T
from cctripwire.quota.estimator import weight
from cctripwire.store import Store


def asst(mid, rid, ts="2026-10-08T12:00:00.000Z", sid="s1", cwd="/home/u/proj",
         model="claude-opus-4-1", inp=10, out=20, cc=0, cr=0):
    return {"type": "assistant", "sessionId": sid, "cwd": cwd, "timestamp": ts, "requestId": rid,
            "uuid": f"u-{mid}-{rid}",
            "message": {"id": mid, "model": model,
                        "usage": {"input_tokens": inp, "output_tokens": out,
                                  "cache_creation_input_tokens": cc, "cache_read_input_tokens": cr}}}


def write_lines(path, objs, trailing_newline=True):
    text = "\n".join(o if isinstance(o, str) else json.dumps(o) for o in objs)
    path.write_text(text + ("\n" if trailing_newline else ""))


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "state" / "g.db")
    yield s
    s.close()


def token_rows(store):
    return store.conn.execute("SELECT * FROM token_events ORDER BY id").fetchall()


def test_one_response_counted_once(tmp_path, store):
    p = tmp_path / "t.jsonl"
    # Three content blocks of one response share message.id, requestId and usage.
    write_lines(p, [asst("m1", "r1"), asst("m1", "r1"), asst("m1", "r1"), asst("m2", "r2")])
    assert T.scan_file(store, p) == 2
    assert len(token_rows(store)) == 2


def test_skips_non_assistant_and_usageless_lines(tmp_path, store):
    p = tmp_path / "t.jsonl"
    no_usage = asst("m3", "r3")
    del no_usage["message"]["usage"]
    write_lines(p, [
        {"type": "user", "sessionId": "s1", "message": {"role": "user", "content": "hi"}},
        {"type": "attachment", "attachment": {}},
        {"type": "mode", "mode": "plan"},
        no_usage,
        "this is not json at all",
        asst("m4", "r4"),
    ])
    assert T.scan_file(store, p) == 1
    assert [r["msg_key"] for r in token_rows(store)] == ["m4:r4"]


def test_fields_are_extracted(tmp_path, store):
    p = tmp_path / "t.jsonl"
    write_lines(p, [asst("m1", "r1", ts="2026-10-08T12:00:00.500Z", sid="sess-9", cwd="/srv/myrepo",
                         model="claude-sonnet-4-5", inp=100, out=10, cc=40, cr=1000)])
    T.scan_file(store, p)
    (row,) = token_rows(store)
    want_ts = datetime(2026, 10, 8, 12, 0, 0, 500000, tzinfo=timezone.utc).timestamp()
    assert row["ts"] == pytest.approx(want_ts)
    assert row["session_id"] == "sess-9"
    assert row["project"] == "myrepo"
    assert (row["input"], row["output"], row["cache_create"], row["cache_read"]) == (100, 10, 40, 1000)
    assert row["weighted"] == pytest.approx(weight("claude-sonnet-4-5", {
        "input_tokens": 100, "output_tokens": 10, "cache_creation_input_tokens": 40,
        "cache_read_input_tokens": 1000}))


def test_explicit_project_agent_invocation(tmp_path, store):
    p = tmp_path / "t.jsonl"
    write_lines(p, [asst("m1", "r1")])
    T.scan_file(store, p, project="X", agent="A", invocation_id="inv1")
    (row,) = token_rows(store)
    assert (row["project"], row["agent"], row["invocation_id"]) == ("X", "A", "inv1")


def test_incremental_scan_only_reads_new_lines(tmp_path, store):
    p = tmp_path / "t.jsonl"
    write_lines(p, [asst("m1", "r1")])
    assert T.scan_file(store, p) == 1
    assert T.scan_file(store, p) == 0
    with open(p, "a") as f:
        f.write(json.dumps(asst("m2", "r2")) + "\n")
    assert T.scan_file(store, p) == 1
    assert len(token_rows(store)) == 2


def test_partial_last_line_waits_for_completion(tmp_path, store):
    p = tmp_path / "t.jsonl"
    full = json.dumps(asst("m2", "r2"))
    write_lines(p, [asst("m1", "r1")])
    with open(p, "a") as f:
        f.write(full[:40])                     # still being written
    assert T.scan_file(store, p) == 1          # m1 only; the offset stops before the partial line
    with open(p, "a") as f:
        f.write(full[40:] + "\n")
    assert T.scan_file(store, p) == 1          # m2 completed and counted exactly once
    assert T.scan_file(store, p) == 0
    assert len(token_rows(store)) == 2


def test_complete_json_without_trailing_newline_is_counted(tmp_path, store):
    p = tmp_path / "t.jsonl"
    write_lines(p, [asst("m1", "r1")], trailing_newline=False)
    assert T.scan_file(store, p) == 1
    assert T.scan_file(store, p) == 0


def test_truncation_restarts_from_zero(tmp_path, store):
    p = tmp_path / "t.jsonl"
    write_lines(p, [asst("m1", "r1"), asst("m2", "r2")])
    assert T.scan_file(store, p) == 2
    write_lines(p, [asst("m9", "r9")])         # smaller file, same inode
    assert T.scan_file(store, p) == 1
    assert len(token_rows(store)) == 3


def test_rotation_new_inode_restarts(tmp_path, store):
    p = tmp_path / "t.jsonl"
    write_lines(p, [asst("m1", "r1")])
    assert T.scan_file(store, p) == 1
    fresh = tmp_path / "fresh.jsonl"
    write_lines(fresh, [asst("m5", "r5")])     # a distinct inode, moved over the old file
    os.replace(fresh, p)
    assert T.scan_file(store, p) == 1


def test_project_defaults_from_cwd_and_agent_from_project(tmp_path, store):
    p = tmp_path / "t.jsonl"
    write_lines(p, [asst("m1", "r1", cwd="/a/b/worker-7")])
    T.scan_file(store, p)
    (row,) = token_rows(store)
    assert row["project"] == "worker-7"
    assert row["agent"] == "worker-7"


def test_missing_file_is_zero(tmp_path, store):
    assert T.scan_file(store, tmp_path / "nope.jsonl") == 0


def test_scan_all_respects_max_age_and_finds_subagents(tmp_path, store):
    cfg = Config({"general": {"claude_dir": str(tmp_path / "claude")}})
    proj = tmp_path / "claude" / "projects" / "-home-u-repo"
    (proj / "sess" / "subagents").mkdir(parents=True)
    fresh = proj / "s1.jsonl"
    sub = proj / "sess" / "subagents" / "agent-x.jsonl"
    old = proj / "old.jsonl"
    write_lines(fresh, [asst("m1", "r1")])
    write_lines(sub, [asst("m2", "r2")])
    write_lines(old, [asst("m3", "r3")])
    ten_days_ago = time.time() - 10 * 86400
    os.utime(old, (ten_days_ago, ten_days_ago))
    assert T.scan_all(store, cfg) == 2
    assert {r["msg_key"] for r in token_rows(store)} == {"m1:r1", "m2:r2"}


def test_scan_all_without_projects_dir(tmp_path, store):
    cfg = Config({"general": {"claude_dir": str(tmp_path / "empty")}})
    assert T.scan_all(store, cfg) == 0


def test_find_session_file(tmp_path):
    cfg = Config({"general": {"claude_dir": str(tmp_path / "claude")}})
    d = tmp_path / "claude" / "projects" / "p1"
    d.mkdir(parents=True)
    (d / "abc.jsonl").write_text("")
    assert T.find_session_file(cfg, "abc") == d / "abc.jsonl"
    assert T.find_session_file(cfg, "missing") is None
    assert T.find_session_file(cfg, "../abc") is None
    assert T.find_session_file(cfg, "") is None
