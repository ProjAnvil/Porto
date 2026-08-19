"""数据集注册表——按名加载，``experiment.py --dataset <name>`` 消费。

``domainrag`` 默认 extractive 子集（保持既有行为）；DomainRAG 其余子集
以 ``domainrag:<subset>`` 或裸子集名引用。
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
    # 默认数据集：DomainRAG extractive（与历史行为一致）
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
    """按名加载数据集；未知名称报错并列出可用项。"""
    if name not in DATASETS:
        raise KeyError(f"未知数据集 '{name}'，可用: {available_datasets()}")
    return DATASETS[name](data_dir)
