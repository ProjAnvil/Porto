"""Profile-aware collection name resolution.

Physical collection names include the profile revision so that migrated
data in a new profile never collides with the old profile's collections.
Legacy installations (revision 1, generation 1) keep unsuffixed names.
"""
from __future__ import annotations


def resolve_kb_collection(base: str, revision: int, generation: int) -> str:
    """Resolve the physical KB collection name.

    * revision=1, generation=1 → unsuffixed ``base`` (legacy bootstrap)
    * revision=1, generation>1 → ``base__g<gen>``
    * revision>1, any generation → ``base__r<rev>_g<gen>``
    """
    if revision <= 1 and generation <= 1:
        return base
    if revision <= 1:
        return f"{base}__g{generation}"
    return f"{base}__r{revision}_g{generation}"


def resolve_memory_collection(base: str, revision: int) -> str:
    """Resolve the physical Memory collection name.

    * revision=1 → unsuffixed ``base``
    * revision>1 → ``base__r<rev>``
    """
    if revision <= 1:
        return base
    return f"{base}__r{revision}"
