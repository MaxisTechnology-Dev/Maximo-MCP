"""
core/object_registry.py — Registry of Maximo logical entities.

Maps entity names (e.g. "workorder") to one or more candidate object
structure names, field aliases, and default select fields.

Design goals
------------
* Environment-agnostic: any Maximo site may expose different object
  structure names (mxwo vs mxapiwodetail vs a site-specific zwoapi).
* Alias mapping: callers use human-friendly names (site_id, priority);
  the registry translates them to the real Maximo column names.
* Extensible: adding a new entity is a single dict entry.
"""

from __future__ import annotations

from typing import Dict, List, Optional


class ObjectDefinition:
    """
    Describes a logical Maximo entity with candidate object structures,
    field aliases, and default select fields.

    Attributes:
        candidates:     Ordered list of object structure names to try,
                        from most-preferred to least-preferred.
        aliases:        Mapping of caller-friendly names to real Maximo
                        column names.  Keys are lower-cased for lookup.
        default_select: Maximo column names returned when the caller
                        does not supply an explicit select list.
    """

    __slots__ = ("candidates", "aliases", "default_select")

    def __init__(
        self,
        candidates: List[str],
        aliases: Optional[Dict[str, str]] = None,
        default_select: Optional[List[str]] = None,
    ) -> None:
        self.candidates: List[str] = candidates
        self.aliases: Dict[str, str] = {k.lower(): v for k, v in (aliases or {}).items()}
        self.default_select: List[str] = default_select or []

    def resolve_alias(self, user_field: str) -> str:
        """Return the Maximo column name for a caller alias, or the original name."""
        return self.aliases.get(user_field.lower(), user_field)

    def resolve_select(self, select: Optional[List[str]]) -> List[str]:
        """
        Translate a caller-supplied select list (or fall back to default_select)
        by running each name through resolve_alias().
        """
        source = select if select else self.default_select
        return [self.resolve_alias(f) for f in source]


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

OBJECT_REGISTRY: Dict[str, ObjectDefinition] = {
    "workorder": ObjectDefinition(
        candidates=["mxwo", "mxapiwodetail", "zwoapi"],
        aliases={
            "priority":     "wopriority",
            "site_id":      "siteid",
            "asset_num":    "assetnum",
            "work_type":    "worktype",
            "reported_by":  "reportedby",
            "failure_code": "failurecode",
        },
        default_select=[
            "wonum", "description", "status", "wopriority", "assetnum",
            "siteid", "worktype", "reportdate", "schedstart", "actfinish",
            "actlabhrs", "reportedby", "location", "failurecode",
        ],
    ),

    "asset": ObjectDefinition(
        candidates=["mxasset", "mxapiasset"],
        aliases={
            "site_id":        "siteid",
            "asset_type":     "assettype",
            "serial_num":     "serialnum",
            "purchase_price": "purchaseprice",
        },
        default_select=[
            "assetnum", "description", "siteid", "status", "assettype",
            "serialnum", "location", "purchaseprice", "installdate",
            "changedate", "manufacturer", "vendor", "parent",
        ],
    ),

    "pm": ObjectDefinition(
        candidates=["mxpm", "mxapipm"],
        aliases={
            "site_id":   "siteid",
            "asset_num": "assetnum",
        },
        default_select=[
            "pmnum", "description", "siteid", "assetnum",
            "frequency", "frequnit", "nextduedate", "status",
        ],
    ),

    "inventory": ObjectDefinition(
        candidates=["mxinvbal", "mxapiinvbal"],
        aliases={
            "site_id":  "siteid",
            "item_num": "itemnum",
        },
        default_select=[
            "itemnum", "siteid", "storeloc", "curbal",
            "orderpoint", "minlevel", "maxlevel",
        ],
    ),

    "labor": ObjectDefinition(
        candidates=["mxlabor", "mxapilabor"],
        aliases={
            "site_id":    "siteid",
            "labor_code": "laborcode",
        },
        default_select=[
            "laborcode", "personid", "siteid", "craft", "status",
        ],
    ),

    "location": ObjectDefinition(
        candidates=["mxoperloc", "mxapioperlocdetail"],
        aliases={
            "site_id": "siteid",
        },
        default_select=[
            "location", "description", "siteid", "type", "parent", "status",
        ],
    ),

    "purchase_order": ObjectDefinition(
        candidates=["mxpo", "mxapipo"],
        aliases={
            "site_id":  "siteid",
            "vendor_id": "vendor",
        },
        default_select=[
            "ponum", "siteid", "vendor", "status",
            "totalcost", "orderdate", "requireddate",
        ],
    ),
}


def get_object_definition(entity: str) -> Optional[ObjectDefinition]:
    """
    Return the ObjectDefinition for a logical entity name (case-insensitive).
    Returns None when the entity is not registered.
    """
    return OBJECT_REGISTRY.get(entity.lower())


def list_entities() -> List[str]:
    """Return all registered entity names in alphabetical order."""
    return sorted(OBJECT_REGISTRY.keys())
