# -*- coding: utf-8 -*-
"""Decode the stackable item lists of a Last Epoch STASH save.

``STASH_*`` documents use the same ``EPOCH`` + JSON container as character saves.
Their item lists are ``savedItems`` (full items), ``materialsList`` (crafting
materials), ``wovenEchoesList`` and ``keysList``.  The latter two use compact
stack formats; entries the item decoder cannot interpret are reported with their
raw bytes instead of being dropped.
"""
from __future__ import annotations

from . import items as _items

LISTS = ("savedItems", "materialsList", "wovenEchoesList", "keysList")


def decrypt_stash_lists(stash_document: dict) -> dict:
    """Decode every stackable list of a STASH save (best effort, never raises)."""
    out: dict = {}
    for key in LISTS:
        entries = stash_document.get(key) or []
        decoded = []
        for index, entry in enumerate(entries):
            data = entry.get("data") or []
            try:
                item = _items.decode_item(data)
            except Exception as exc:  # noqa: BLE001 - keep the raw record visible
                item = {"error": f"{type(exc).__name__}: {exc}", "data_len": len(data)}
            item["index"] = index
            item["container_id"] = entry.get("containerID")
            item["quantity"] = entry.get("quantity")
            item["inventory_position"] = entry.get("inventoryPosition")
            item["data_bytes"] = [int(b) & 0xFF for b in data]
            decoded.append(item)
        out[key] = decoded
    return out
