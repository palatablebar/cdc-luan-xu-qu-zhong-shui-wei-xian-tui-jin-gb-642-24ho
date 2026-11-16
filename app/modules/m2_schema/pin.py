"""批次口径：把 schema 版本钉在逻辑批次上，而不是按文件到达时点解释。

背景：上游切到新列名（v2）后，补片仍按旧列名（v1）发送。如果按文件到达
时点各自解释，同一逻辑批次会混成两套字段，重放历史快照还会改值。这里的
规则是：

- 一个批次只有一套口径；批内所有事件按钉住的口径归一（旧列名走别名映射）；
- auto：钉当前契约版本（config.DEFAULT_SCHEMA），与到达先后无关——新列名
  数据立即进入正式结果，旧列名补片无损归一后进入；
- manual：params["schema_pin"] 指定；参数进 batch_key，换口径就是新批次，
  已发布批次的结果与口径不受影响；
- 决策确定性：同样输入 + 同样参数 → 同样口径，重放历史快照不会改值；
- 跨口径到达与旧列名映射全部登记为待核对差异，随批次落库，可回读。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ... import config, errors

DIFF_SCHEMA = "schema_mismatch"      # 声明口径 ≠ 批次口径，已按批次口径归一
DIFF_ALIAS = "alias_mapped"          # 用了旧列名，已映射到规范列名
DIFF_AMBIGUOUS = "alias_ambiguous"   # 新旧列名同现且取值不同（已隔离）
DIFF_UNKNOWN = "unknown_field"       # 契约外字段（已隔离）

KIND_LABELS = {
    DIFF_SCHEMA: "跨口径到达",
    DIFF_ALIAS: "旧列名映射",
    DIFF_AMBIGUOUS: "新旧列名歧义",
    DIFF_UNKNOWN: "契约外字段",
}
KIND_LEVEL = {
    DIFF_SCHEMA: "warn",
    DIFF_ALIAS: "warn",
    DIFF_AMBIGUOUS: "error",
    DIFF_UNKNOWN: "error",
}


@dataclass
class Pin:
    """一批的口径决策：钉住版本、依据、声明分布与差异计数。"""
    pinned: str
    policy: str                       # auto / manual
    basis: str
    declared: dict = field(default_factory=dict)
    cross_events: int = 0             # 声明口径 ≠ 批次口径的事件数
    mapped_events: int = 0            # 用到旧列名映射的事件数

    def to_row(self) -> dict:
        return {"pinned": self.pinned, "policy": self.policy, "basis": self.basis,
                "declared": dict(self.declared), "cross_events": self.cross_events,
                "mapped_events": self.mapped_events}


def decide(norms, params: dict | None) -> Pin:
    """按批内声明分布与参数决定口径；同输入同参数必然得到同一口径。"""
    declared: dict[str, int] = {}
    for norm in norms:
        schema = getattr(norm, "schema", "") or ""
        if schema:
            declared[schema] = declared.get(schema, 0) + 1
    requested = str((params or {}).get("schema_pin") or "").strip()
    dist = "、".join(f"{key}={declared[key]}" for key in sorted(declared)) or "无"
    if requested:
        if requested not in config.SCHEMA_VERSIONS:
            raise errors.BadRequest(
                f"schema_pin 只接受 {' / '.join(config.SCHEMA_VERSIONS)}，"
                f"收到 {requested!r}", code="E_BAD_SCHEMA_PIN")
        pin = Pin(pinned=requested, policy="manual", declared=declared,
                  basis=f"按参数 schema_pin={requested} 钉住；批内声明 {dist}")
    else:
        pin = Pin(pinned=config.DEFAULT_SCHEMA, policy="auto", declared=declared,
                  basis=(f"钉当前契约版本 {config.DEFAULT_SCHEMA}（不按文件到达时点解释）；"
                         f"批内声明 {dist}"))
    pin.cross_events = sum(1 for norm in norms
                           if getattr(norm, "schema", "") and norm.schema != pin.pinned)
    pin.mapped_events = sum(1 for norm in norms if getattr(norm, "mapped", None))
    return pin


def diff_rows(norms, pin: Pin) -> list[dict]:
    """待核对差异：跨口径到达、旧列名映射、歧义与未知列，逐条带行号引用。"""
    rows: list[dict] = []
    for norm in norms:
        schema = getattr(norm, "schema", "") or ""
        if schema and schema != pin.pinned:
            rows.append(_row(norm, DIFF_SCHEMA, pin,
                             f"声明 {schema}，批次口径 {pin.pinned}，已按批次口径归一"))
        mapped = getattr(norm, "mapped", None) or []
        if mapped:
            text = "、".join(f"{m['alias']}→{m['target']}" for m in mapped)
            rows.append(_row(norm, DIFF_ALIAS, pin, f"旧列名已映射：{text}",
                             fields=mapped))
        errors_ = getattr(norm, "errors", []) or []
        if "E_ALIAS_AMBIGUOUS" in errors_:
            rows.append(_row(norm, DIFF_AMBIGUOUS, pin,
                             getattr(norm, "detail", "") or "新旧列名取值不同"))
        if "E_UNKNOWN_FIELD" in errors_:
            rows.append(_row(norm, DIFF_UNKNOWN, pin,
                             getattr(norm, "detail", "") or "契约外字段"))
    return rows


def _row(norm, kind: str, pin: Pin, detail: str,
         fields: list | None = None) -> dict:
    return {"row_ref": norm.row_ref, "kind": kind,
            "declared": getattr(norm, "schema", "") or None, "pinned": pin.pinned,
            "fields": list(fields or []), "detail": detail,
            "level": KIND_LEVEL[kind], "kind_label": KIND_LABELS[kind]}
