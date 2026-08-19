"""synth 微型合成数据集（3 md + 2 golden）——零外部依赖的冒烟/单测数据。"""
from __future__ import annotations

import json
from pathlib import Path

from ..schema import CorpusDoc, RagCorpus, RagGolden

SYNTH_DIR = Path(__file__).resolve().parents[1] / "synth"


def load_synth(synth_dir: Path | None = None) -> tuple[RagCorpus, list[RagGolden]]:
    base = synth_dir or SYNTH_DIR
    corpus = RagCorpus(
        docs=[
            CorpusDoc(id=p.stem, text=p.read_text(encoding="utf-8"))
            for p in sorted(base.glob("*.md"))
        ]
    )
    goldens = [
        RagGolden(**g)
        for g in json.loads((base / "goldens.json").read_text(encoding="utf-8"))
    ]
    return corpus, goldens
