"""pipeline/models.py — Pydantic v2 轻量校验模型.

仅用于 raw 输入的早期校验和最终 result 的 sanity check.
不替换 save_scan_result 内部的 dict 结构, 不影响 json.dump 路径.
"""
from __future__ import annotations

from typing import Any, Literal, Optional, Union
from pydantic import BaseModel, ConfigDict, Field, field_validator


class RangeEstimate(BaseModel):
    model_config = ConfigDict(extra='ignore')
    lower: int = 0
    upper: int = 0


class NormalizedAd(BaseModel):
    """Validates the shape of a raw ad entry after normalize_ad()."""
    model_config = ConfigDict(extra='ignore', str_strip_whitespace=False)

    id: str = ''
    page_name: str = 'Unknown Brand'
    page_id: str = ''
    page_profile_uri: str = ''
    page_likes: int = 0
    page_categories: list[str] = Field(default_factory=list)
    start_date: Union[int, str] = 0
    end_date: str = ''
    is_active: bool = True
    format: str = ''
    cta: str = ''
    link_url: str = ''
    link_description: str = ''
    link_caption: str = ''
    title: str = ''
    body: str = ''
    ad_snapshot_url: str = ''
    variants: int = 1
    platforms: list[str] = Field(default_factory=list)
    spend: Optional[RangeEstimate] = None
    impressions: Optional[RangeEstimate] = None
    currency: str = ''
    target_locations: list[str] = Field(default_factory=list)
    target_ages: str = ''
    target_genders: str = ''
    languages: list[str] = Field(default_factory=list)
    image_url: str = ''
    video_url: str = ''
    disclaimer_label: str = ''

    @field_validator('page_likes', 'variants', mode='before')
    @classmethod
    def _coerce_int(cls, v: Any) -> int:
        if v is None:
            return 0
        try:
            return int(v)
        except (TypeError, ValueError):
            return 0


class ScanResult(BaseModel):
    """v7.1 _corrected.json 顶层. extra='allow' 保留未声明字段."""
    model_config = ConfigDict(extra='allow')

    keyword: str
    time: str
    count: int
    ads: int
    brands: int
    scan_version: str
    top_competitors: list[dict]
    market_analysis: dict
    extracted_keywords: dict
    product_opportunities: Optional[dict] = None
    rabbit_hole: Optional[dict] = None
    metadata: Optional[dict] = None


# ── API 请求/响应 ──────────────────────────────────────────────────────────

class RawScanRequest(BaseModel):
    keyword: str
    raw_ads: list[dict]
    source_raw: Optional[dict] = None


class V2ScrapeFastRequest(BaseModel):
    model_config = ConfigDict(extra='ignore', str_strip_whitespace=True)

    keywords: Optional[list[str]] = Field(default=None, max_length=300)
    auto_pull_limit: int = Field(default=0, ge=0, le=300)
    workers: int = Field(default=5, ge=1, le=16)
    country: Optional[str] = Field(default=None, max_length=8)
    headless: bool = True
    dry_run: bool = False
    strategy: Literal['auto', 'quick', 'deep', 'playwright'] = 'auto'

    @field_validator('keywords', mode='before')
    @classmethod
    def _normalize_keywords(cls, v: Any) -> Optional[list[str]]:
        if v is None or v == '':
            return None
        if not isinstance(v, list):
            raise ValueError('keywords must be list[str]')
        out: list[str] = []
        seen: set[str] = set()
        for item in v:
            if not isinstance(item, str):
                continue
            kw = item.strip()
            key = kw.lower()
            if not kw or key in seen:
                continue
            if len(kw) > 120:
                kw = kw[:120].strip()
                key = kw.lower()
            seen.add(key)
            out.append(kw)
        return out or None

    @field_validator('country', mode='before')
    @classmethod
    def _normalize_country(cls, v: Any) -> Optional[str]:
        if v is None or v == '':
            return None
        return str(v).strip().upper()[:8]


class BrandResponse(BaseModel):
    domain: str
    canonical_name: str
    orbit_score: int
    appearance_count: int
    highest_likes: int = 0
    total_ads_across_scans: int = 0
    keywords: list[str] = Field(default_factory=list)
    page_names: list[str] = Field(default_factory=list)


class BrandSummary(BaseModel):
    domain: str
    canonical_name: str
    orbit_score: int
    appearance_count: int


class LearnedKeyword(BaseModel):
    keyword: str
    kw_type: str
    subtype: str = ''
    source_kw: str
    learned_date: str


class ScanSummary(BaseModel):
    keyword: str
    scan_date: str
    ads_count: int = 0
    brands_count: int = 0
    corrected_file: str = ''


class HealthResponse(BaseModel):
    status: str
    db_file: str
    pipeline_version: str = '8.0.0'
