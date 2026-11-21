"""请求 / 响应模型。字符区间均为 [start, end)，按 Unicode 码位计。"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field, model_validator

MAX_CHARS = 300  # 每稿最多 300 个字符


class AnnotationIn(BaseModel):
    name: str = Field(min_length=1, max_length=64, description="人名")
    start: int = Field(ge=0, description="旧稿区间起点（含）")
    end: int = Field(gt=0, description="旧稿区间终点（不含）")


class PageCreate(BaseModel):
    page_ref: Optional[str] = Field(default=None, max_length=128,
                                    description="页码标识，如「卷一·頁三」")
    old_text: str = Field(min_length=1, max_length=MAX_CHARS,
                          description="旧稿全文（≤300 字）")
    new_text: str = Field(min_length=1, max_length=MAX_CHARS,
                          description="校订稿全文（≤300 字）")
    annotations: list[AnnotationIn] = Field(default_factory=list,
                                            max_length=500,
                                            description="旧稿上的人名标注")

    @model_validator(mode="after")
    def check_intervals(self) -> "PageCreate":
        n = len(self.old_text)
        for a in self.annotations:
            if not (0 <= a.start < a.end <= n):
                raise ValueError(
                    f"标注「{a.name}」的旧稿区间 [{a.start}, {a.end}) "
                    f"超出范围（旧稿长 {n}）"
                )
        return self


class ConfirmIn(BaseModel):
    new_start: int = Field(ge=0, description="编辑确认的新区间起点（含）")
    new_end: int = Field(gt=0, description="编辑确认的新区间终点（不含）")


class SupplementBody(BaseModel):
    name: str = Field(min_length=1, max_length=64, description="人名")
    new_start: int = Field(ge=0, description="校订稿区间起点（含）")
    new_end: int = Field(gt=0, description="校订稿区间终点（不含）")


class SupplementIn(SupplementBody):
    page_id: int = Field(ge=1, description="页编号")


class SupplementCorrectIn(BaseModel):
    new_start: int = Field(ge=0, description="更正后的校订稿区间起点（含）")
    new_end: int = Field(gt=0, description="更正后的校订稿区间终点（不含）")


class HitSourceOut(BaseModel):
    source_type: str = Field(description="annotation=旧标注 / supplement=编辑补录")
    source_id: int
    source_status: Optional[str] = Field(
        default=None, description="旧标注当前状态；补录为 supplement")


class SupplementOut(BaseModel):
    id: int
    page_id: int
    name: str
    new_start: int
    new_end: int
    excerpt: str
    hit_id: int = Field(description="该补录当前归属的命中编号")
    created_at: str
    updated_at: str


class HitOut(BaseModel):
    hit_id: int = Field(description="检索库命中编号（持久，可回查）")
    page_id: int
    page_ref: Optional[str]
    name: str
    start: int = Field(description="校订稿区间起点（含）")
    end: int = Field(description="校订稿区间终点（不含）")
    excerpt: str
    sources: list[HitSourceOut] = Field(
        description="合并进本命中的全部来源（类型 + 编号）")
    updated_at: str


class PageHitsOut(BaseModel):
    """按页供数：同名、同区间的补录与可用旧标注合并后的命中列表。"""
    page_id: int
    page_ref: Optional[str]
    items: list[HitOut]


class AnnotationOut(BaseModel):
    id: int
    name: str
    old_start: int
    old_end: int
    old_excerpt: str = Field(description="旧稿区间文字")
    status: str = Field(description="auto=自动可用 / pending=待核 / confirmed=编辑确认")
    new_start: Optional[int] = None
    new_end: Optional[int] = None
    new_excerpt: Optional[str] = Field(default=None, description="新稿区间文字")
    candidates: Optional[list[list[int]]] = Field(
        default=None,
        description="待核标注在当前最短对齐约束下仍可能的新区间列表 [start, end)")


class PageOut(BaseModel):
    id: int
    page_ref: Optional[str]
    old_text: str
    new_text: str
    edit_ops: int = Field(description="插入+删除总次数（最短对齐的代价）")
    version: int = Field(description="每次确认生效后 +1")
    usable_count: int = Field(description="可用标注数（auto + confirmed）")
    pending_count: int = Field(description="待核标注数")
    annotations: list[AnnotationOut]


class PageSummary(BaseModel):
    id: int
    page_ref: Optional[str]
    edit_ops: int
    version: int
    usable_count: int
    pending_count: int
    created_at: str


class IndexItem(BaseModel):
    hit_id: int = Field(description="检索库命中编号（持久，可回查）")
    annotation_id: Optional[int] = Field(
        default=None, description="兼容旧客户端：首个旧标注来源的编号")
    name: str
    start: int = Field(description="校订稿区间起点（含）")
    end: int = Field(description="校订稿区间终点（不含）")
    excerpt: str = Field(description="校订稿区间文字")
    source: str = Field(
        description="兼容旧客户端：首个来源状态（auto/confirmed/supplement）")
    sources: list[HitSourceOut] = Field(
        description="合并进本命中的来源；每项含类型与编号")


class PendingItem(BaseModel):
    annotation_id: int
    name: str
    old_start: int
    old_end: int
    candidates: list[list[int]]


class IndexOut(BaseModel):
    """检索库视图（兼容旧客户端）：保留 page/pending，命中并入 items。"""
    page_id: int
    page_ref: Optional[str]
    items: list[IndexItem]
    pending: list[PendingItem] = Field(description="待核标注（不进入检索库）")
