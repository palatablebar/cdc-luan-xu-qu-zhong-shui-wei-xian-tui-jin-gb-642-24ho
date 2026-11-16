"""M2 入口：契约视图、单条试归一、批次口径与待核对差异回读。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from ... import errors
from ...web import Ctx, get_ctx, render
from ..m3_dedup import service as dedup_service
from . import alias, contract, pin as pin_mod, service

router = APIRouter(tags=["M2 契约兼容"])


@router.get("/api/schema")
def api_schema(ctx: Ctx = Depends(get_ctx)) -> dict:
    view = contract.contract_view()
    view["aliases"] = alias.alias_rows()
    batch = ctx.current_batch()
    if batch:
        view["batch_id"] = batch["batch_id"]
        view["batch_pin"] = ctx.store.load_pin(batch["batch_id"])
    return view


@router.get("/api/batches/{batch_id}/schema")
def api_batch_schema(batch_id: str, ctx: Ctx = Depends(get_ctx)) -> dict:
    """回读一个批次钉住的口径与待核对差异（跨口径到达 / 旧列名映射 / 歧义 / 未知列）。"""
    batch = ctx.store.get_batch(batch_id)
    if batch is None:
        raise errors.NotFound(f"批次 {batch_id} 不存在", code="E_NOT_FOUND")
    diffs = ctx.store.load_diffs(batch_id)
    return {
        "batch_id": batch_id,
        "batch_status": batch["status"],
        "pin": ctx.store.load_pin(batch_id),
        "diffs": diffs,
        "diff_count": len(diffs),
        "kinds": [{"kind": kind, "label": pin_mod.KIND_LABELS[kind],
                   "level": pin_mod.KIND_LEVEL[kind]}
                  for kind in (pin_mod.DIFF_SCHEMA, pin_mod.DIFF_ALIAS,
                               pin_mod.DIFF_AMBIGUOUS, pin_mod.DIFF_UNKNOWN)],
    }


@router.post("/api/schema/preview")
def api_preview(payload: dict | None = None) -> dict:
    body = payload or {}
    if not isinstance(body, dict) or not body:
        raise errors.BadRequest("需要一段事件 JSON", code="E_BAD_REQUEST")
    event = body.get("event") if isinstance(body.get("event"), dict) else body
    return service.preview(event)


@router.get("/schema")
def page_schema(request: Request, ctx: Ctx = Depends(get_ctx)):
    batch = ctx.current_batch()
    layers = ctx.layers(batch["batch_id"]) if batch else {
        "inputs": [], "events": [], "snapshot": [], "watermarks": [],
        "stages": [], "totals": {}}
    view = contract.contract_view()
    preview_sample = {
        "table": "orders", "pk": "1101", "version": 3, "op": "upsert",
        "event_time": "2026-11-15T08:10:00Z", "schema": "v1",
        "payload": {"ref_no": "1101", "amt": "1,200.50", "st": "PAID"},
    }
    return render(request, "schema.html", batch=batch, page="schema",
                  contract=view, field_rows=contract.field_rows(),
                  alias_rows=alias.alias_rows(),
                  pin=(ctx.store.load_pin(batch["batch_id"]) if batch else None),
                  diffs=(ctx.store.load_diffs(batch["batch_id"]) if batch else []),
                  kind_labels=pin_mod.KIND_LABELS,
                  distribution=dedup_service.distribution_from_rows(layers["events"]),
                  events=layers["events"][:1],
                  preview_sample=preview_sample,
                  preview_result=service.preview(preview_sample),
                  totals=layers["totals"], inputs=layers["inputs"],
                  page_title="契约兼容")
