"""批次口径：钉住与回读、待核对差异、重放稳定、失败保留旧版、不丢已采信记录。

对应场景：上游切到新列名（v2）后补片仍发旧列名（v1）。口径钉在逻辑批次上，
不按文件到达时点解释；每批口径与待核对差异落库，可回读。
"""
from __future__ import annotations

import pathlib
from dataclasses import replace

import pytest

from app import errors
from app.modules.m2_schema import pin as pin_mod
from app.modules.m6_batches import service
from fixtures import expected as X

SAMPLES = pathlib.Path(__file__).resolve().parent.parent / "sample_data"


def test_pin_recorded_and_re_readable(settings, store):
    """每批采用的口径落库：钉住版本、决策方式、依据、批内声明分布都可回读。"""
    run = service.run(settings=settings, store=store, source="all")
    batch_id = run["batch"]["batch_id"]
    pin = store.load_pin(batch_id)
    assert pin["pinned"] == "v2"                 # 混合批钉当前契约版本
    assert pin["policy"] == "auto"
    assert "不按文件到达时点" in pin["basis"]
    assert pin["declared"].get("v1", 0) >= 1     # 批内确实有 v1 补片
    assert pin["declared"].get("v2", 0) >= 1
    assert pin["diff_count"] == len(store.load_diffs(batch_id))
    assert run["schema_pin"]["pinned"] == "v2"
    assert run["totals"]["schema_pin"] == "v2"


def test_mixed_batch_v1_rows_registered_as_diffs(settings, store):
    """同一逻辑批次混发两套列名：v1 行按批次口径归一，并逐条登记待核对。"""
    run = service.run(settings=settings, store=store, source="all")
    batch_id = run["batch"]["batch_id"]
    diffs = store.load_diffs(batch_id)
    mismatch = [d for d in diffs if d["kind"] == pin_mod.DIFF_SCHEMA]
    assert mismatch, "v1 补片必须登记为跨口径差异"
    assert all(d["pinned"] == "v2" for d in mismatch)
    assert any(d["row_ref"].startswith("events_v1_gb.csv:") for d in mismatch)
    mapped = [d for d in diffs if d["kind"] == pin_mod.DIFF_ALIAS]
    assert any(f["alias"] == "amt" and f["target"] == "amount"
               for d in mapped for f in d["fields"])
    # 歧义与未知列也进差异表，级别为 error
    serious = [d for d in diffs if d["level"] == "error"]
    assert {d["kind"] for d in serious} == {pin_mod.DIFF_AMBIGUOUS, pin_mod.DIFF_UNKNOWN}
    # 差异只登记，不改变既有计数与金额账
    assert run["totals"]["applied"] == X.APPLIED
    assert run["totals"]["amount_applied"] == X.AMOUNT_APPLIED


def test_pin_stable_on_idempotent_replay(settings, store):
    """同一来源重跑：命中幂等重放，口径与差异不新增、不改值。"""
    first = service.run(settings=settings, store=store, source="all")
    batch_id = first["batch"]["batch_id"]
    pin_before = store.load_pin(batch_id)
    diffs_before = store.load_diffs(batch_id)
    rows_before = store.count_rows(batch_id)

    second = service.run(settings=settings, store=store, source="all")
    assert second["idempotent_replay"] is True
    assert store.load_pin(batch_id) == pin_before
    assert store.load_diffs(batch_id) == diffs_before
    assert store.count_rows(batch_id) == rows_before
    assert second["schema_pin"]["pinned"] == pin_before["pinned"]
    assert len(store.list_batches()) == 1


def test_manual_pin_override_creates_new_batch(settings, store):
    """手工换口径 = 新批次键 = 新批次；旧批次的口径与结果不动。"""
    base = service.run(settings=settings, store=store, source="all")
    overridden = service.run(settings=settings, store=store, source="all",
                             params={"schema_pin": "v1"})
    assert overridden["idempotent_replay"] is False
    assert overridden["batch"]["batch_id"] != base["batch"]["batch_id"]
    pin = store.load_pin(overridden["batch"]["batch_id"])
    assert pin["pinned"] == "v1" and pin["policy"] == "manual"
    # v1 口径下，v2 事件反过来成为跨口径差异
    diffs = store.load_diffs(overridden["batch"]["batch_id"])
    assert any(d["kind"] == pin_mod.DIFF_SCHEMA and d["declared"] == "v2"
               for d in diffs)
    # 已发布批次保持原口径
    assert store.load_pin(base["batch"]["batch_id"])["pinned"] == "v2"
    assert len(store.list_batches()) == 2


def test_bad_pin_param_rejected_without_writing(settings, store):
    with pytest.raises(errors.BadRequest) as info:
        service.run(settings=settings, store=store, source="all",
                    params={"schema_pin": "v9"})
    assert info.value.code == "E_BAD_SCHEMA_PIN"
    assert store.list_batches() == []


def test_failed_run_keeps_previous_results(settings, store, tmp_path):
    """补片重跑失败（陈旧输入被 409 拒绝）：旧版结果、口径、水位全部原样。"""
    first = service.run(settings=settings, store=store, source="all")
    batch_id = first["batch"]["batch_id"]
    rows_before = store.count_rows(batch_id)
    diffs_before = store.load_diffs(batch_id)

    old_dir = tmp_path / "old"
    old_dir.mkdir()
    (old_dir / "events_old.jsonl").write_text(
        "# cdc-export v2 rows=1 charset=utf-8\n"
        '{"table":"orders","pk":"7001","version":9,"op":"upsert",'
        '"event_time":"2026-11-15T06:00:00Z","schema":"v2",'
        '"payload":{"status":"新建","amount":"1.00"}}\n',
        encoding="utf-8")
    with pytest.raises(errors.StaleBatch):
        service.run(settings=replace(settings, sample_dir=str(old_dir)),
                    store=store, source="events_old.jsonl")
    assert store.count_rows(batch_id) == rows_before
    assert store.load_pin(batch_id)["pinned"] == "v2"
    assert store.load_diffs(batch_id) == diffs_before
    assert store.watermark_high() == X.WATERMARK


def test_save_attempt_never_demotes_done_batch(settings, store):
    """失败尝试只登记，不把已完成批次降级、不动其结果行。"""
    run = service.run(settings=settings, store=store, source="all")
    batch_id = run["batch"]["batch_id"]
    rows_before = store.count_rows(batch_id)
    store.save_attempt(batch={
        "batch_id": batch_id, "batch_key": run["batch_key"], "run_id": "CDC-retry",
        "source": "all", "status": "failed", "created_at": "2026-11-15T10:00:00Z",
        "input_digest": run["batch"]["input_digest"], "params": {},
    }, inputs=[], note="重试失败")
    batch = store.get_batch(batch_id)
    assert batch["status"] == "done"
    assert store.count_rows(batch_id) == rows_before
    assert store.load_pin(batch_id)["pinned"] == "v2"


def test_accepted_records_survive_later_batches(settings, store):
    """后续批次（新口径、新输入）不丢、不改已采信记录。"""
    first = service.run(settings=settings, store=store, source="all")
    batch_id = first["batch"]["batch_id"]
    rows_before = store.count_rows(batch_id)
    later = service.run(settings=settings, store=store, source="main")
    assert later["batch"]["batch_id"] != batch_id
    assert store.count_rows(batch_id) == rows_before
    assert len(store.load_events(batch_id)) == rows_before["events"]
    assert store.load_pin(batch_id)["pinned"] == "v2"


def test_batch_schema_api_and_pages(client):
    """回读入口：API 给出每批口径与待核对差异，批次列表带口径列。"""
    listed = client.get("/api/batches").json()
    row = listed["batches"][0]
    assert row["schema_pin"] == "v2"
    assert row["schema_diffs"] >= 1

    payload = client.get(f"/api/batches/{row['batch_id']}/schema").json()
    assert payload["pin"]["pinned"] == "v2"
    assert payload["pin"]["policy"] == "auto"
    assert payload["diff_count"] == len(payload["diffs"]) == row["schema_diffs"]
    assert any(d["kind"] == pin_mod.DIFF_SCHEMA for d in payload["diffs"])

    schema = client.get("/api/schema").json()
    assert schema["batch_pin"]["pinned"] == "v2"

    imported = client.post("/api/events/import", json={"source": "all"}).json()
    assert imported["schema_pin"]["pinned"] == "v2"

    missing = client.get("/api/batches/B-nope0000/schema")
    assert missing.status_code == 404

    page = client.get("/schema")
    assert page.status_code == 200
    assert "批次口径" in page.text and "待核对差异" in page.text
    batches_page = client.get("/batches")
    assert batches_page.status_code == 200
