#!/usr/bin/env python3
"""Download DomainRAG (corpus + QA) to tests/rag_eval/data/domainrag/.

Two steps:
  1. gdown fetches the corpus archive (Google Drive) → extract into corpus/
  2. git clone --depth=1 of the official repository → _repo/ (holds the QA goldens in
     BCM/labeled_data/)

After the first run the loader discovers the corpus and basic_qa.jsonl automatically.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

TARGET = Path(__file__).resolve().parents[1] / "data" / "domainrag"
FILE_ID = "1NquEyPGwP0MpTGJwDUUYKU37snYN4Er4"  # DomainRAG corpus (Google Drive)
REPO_URL = "https://github.com/ShootingWong/DomainRAG.git"


def _download_corpus() -> None:
    corpus_dir = TARGET / "corpus"
    if corpus_dir.exists() and any(corpus_dir.rglob("*.json")):
        print(f"corpus 已存在，跳过下载：{corpus_dir}")
        return
    try:
        import gdown
    except ImportError:
        sys.exit("缺少 gdown，请先运行：make eval-install")
    archive = TARGET / "_download"
    archive.mkdir(exist_ok=True)
    out = archive / "domainrag"
    print(f"下载 corpus (file id={FILE_ID}) → {out}")
    gdown.download(id=FILE_ID, output=str(out), quiet=False)
    if tarfile.is_tarfile(out):
        with tarfile.open(out) as tar:
            tar.extractall(TARGET)
    else:
        try:
            with zipfile.ZipFile(out) as zf:
                zf.extractall(TARGET)
        except zipfile.BadZipFile:
            out.rename(TARGET / "domainrag.raw")


def _clone_qa() -> None:
    repo_dir = TARGET / "_repo"
    if repo_dir.exists() and (repo_dir / "BCM" / "labeled_data").exists():
        print(f"QA 仓库已存在，跳过克隆：{repo_dir}")
        return
    if repo_dir.exists():
        shutil.rmtree(repo_dir)
    print(f"克隆 DomainRAG 仓库（取 BCM/labeled_data）→ {repo_dir}")
    subprocess.run(
        ["git", "clone", "--depth=1", REPO_URL, str(repo_dir)], check=True
    )


def main() -> None:
    TARGET.mkdir(parents=True, exist_ok=True)
    _download_corpus()
    _clone_qa()
    print(f"完成，数据位于 {TARGET}")
    print(f"  corpus: {TARGET / 'corpus'}")
    print(f"  QA    : {TARGET / '_repo' / 'BCM' / 'labeled_data'}")


if __name__ == "__main__":
    main()

