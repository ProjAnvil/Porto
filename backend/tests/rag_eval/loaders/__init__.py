"""Dataset registry: load by name, consumed by ``experiment.py --dataset <name>``.

``domainrag`` is the extractive subset by default (existing behavior kept); the other
DomainRAG subsets are referenced as ``domainrag:<subset>`` or by the bare subset name.
"""
from __future__ import annotations

from pathlib import Path

from ..schema import RagCorpus, RagGolden
from .domainrag import (
    DATA_DIR,
    DEFAULT_SUBSET,
    SUBSET_FILES,
    load_domainrag,
)
from .domainrag import (
    available_subsets as available_subsets,
)
from .refusal import load_refusal
from .synth import load_synth


def _load_domainrag_named(subset: str):
    def _load(data_dir: Path | None = None) -> tuple[RagCorpus, list[RagGolden]]:
        return load_domainrag(data_dir or DATA_DIR, subset=subset)

    return _load


DATASETS: dict[str, object] = {
    "synth": lambda _=None: load_synth(),
    "refusal": lambda _=None: load_refusal(),
    # Default dataset: DomainRAG extractive (same as the historical behavior)
    "domainrag": _load_domainrag_named(DEFAULT_SUBSET),
}
for _subset in SUBSET_FILES:
    DATASETS[f"domainrag:{_subset}"] = _load_domainrag_named(_subset)
    DATASETS.setdefault(_subset, _load_domainrag_named(_subset))


def available_datasets() -> list[str]:
    return sorted(DATASETS)


def load_dataset(
    name: str, data_dir: Path | None = None
) -> tuple[RagCorpus, list[RagGolden]]:
    """Load a dataset by name; an unknown name raises and lists the available ones."""
    if name not in DATASETS:
        raise KeyError(f"未知数据集 '{name}'，可用: {available_datasets()}")
    return DATASETS[name](data_dir)
