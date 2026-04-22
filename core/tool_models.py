"""
Typed request models for the highest-traffic tool endpoints.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class StrictBaseModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EmptyArgs(StrictBaseModel):
    pass


class ListAssetsArgs(StrictBaseModel):
    site_id: Optional[str] = None
    status: Optional[str] = None
    asset_type: Optional[str] = None
    page_size: Optional[int] = Field(default=None, ge=1, le=200)
    page_num: int = Field(default=1, ge=1)


class GetAssetArgs(StrictBaseModel):
    asset_num: str
    site_id: str


class SearchAssetsArgs(StrictBaseModel):
    keyword: str
    site_id: Optional[str] = None
    page_size: Optional[int] = Field(default=None, ge=1, le=200)
    page_num: int = Field(default=1, ge=1)


class ListWorkordersArgs(StrictBaseModel):
    site_id: Optional[str] = None
    status: Optional[str] = None
    asset_num: Optional[str] = None
    priority: Optional[int] = Field(default=None, ge=1, le=5)
    date_from: Optional[str] = None
    date_to: Optional[str] = None
    page_size: Optional[int] = Field(default=None, ge=1, le=200)
    page_num: int = Field(default=1, ge=1)


class GetWorkorderArgs(StrictBaseModel):
    wonum: str
    site_id: str


class GetWorkorderKpisArgs(StrictBaseModel):
    site_id: str
    period_months: int = Field(default=3, ge=1, le=24)


class CheckStockLevelArgs(StrictBaseModel):
    item_num: str
    storeroom: str
    site_id: str


class ListLowStockItemsArgs(StrictBaseModel):
    site_id: str
    storeroom: Optional[str] = None
