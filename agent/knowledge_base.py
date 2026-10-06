"""
Agrisky AI Agent — 轻量本地知识库检索（RAG）

把 agent/knowledge/*.md 按段落切块，用 TF-IDF(字符 n-gram，免中文分词)做相似检索。
仅给智能体提供"研判口径/规范参考"以降幻觉；业务数值仍以工具返回为准。
无 sklearn 时退化为关键字重叠打分，始终可用、无外部依赖。
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

KB_DIR = Path(__file__).resolve().parent / "knowledge"
LEGAL_DIR = Path(__file__).resolve().parent.parent / "legal_corpus" / "normalized"


@lru_cache(maxsize=1)
def _load_chunks() -> list[tuple[str, str]]:
    chunks: list[tuple[str, str]] = []
    if not KB_DIR.exists() and not LEGAL_DIR.exists():
        return chunks
    files = list(KB_DIR.glob("*.md")) if KB_DIR.exists() else []
    files.extend(LEGAL_DIR.glob("*.md") if LEGAL_DIR.exists() else [])
    for f in sorted(files):
        text = f.read_text(encoding="utf-8")
        for para in re.split(r"\n\s*\n", text):
            para = para.strip()
            if len(para) >= 10 and not para.startswith("# "):
                chunks.append((f.stem, para))
    return chunks


@lru_cache(maxsize=1)
def _tfidf():
    chunks = _load_chunks()
    if not chunks:
        return None
    try:
        from sklearn.feature_extraction.text import TfidfVectorizer
        vec = TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 3))
        mat = vec.fit_transform([c[1] for c in chunks])
        return vec, mat, chunks
    except Exception:  # noqa: BLE001
        return None


def retrieve(query: str, k: int = 3) -> list[dict]:
    """返回与 query 最相关的 k 个知识块：[{source, text, score}]。"""
    query = (query or "").strip()
    if not query:
        return []
    chunks = _load_chunks()
    if not chunks:
        return []

    idx = _tfidf()
    if idx is not None:
        try:
            import numpy as np
            vec, mat, chunks = idx
            sims = (mat @ vec.transform([query]).T).toarray().ravel()
            order = np.argsort(-sims)[:k]
            return [{"source": chunks[i][0], "text": chunks[i][1], "score": round(float(sims[i]), 4)}
                    for i in order if sims[i] > 0.02]
        except Exception:  # noqa: BLE001
            pass

    # 退化：字符 bigram 重叠打分
    def grams(s: str) -> set[str]:
        s = re.sub(r"\s+", "", s)
        return {s[i:i + 2] for i in range(len(s) - 1)}
    q = grams(query)
    scored = [(len(q & grams(t)) / (len(q) or 1), src, t) for src, t in chunks]
    scored.sort(reverse=True)
    return [{"source": s, "text": t, "score": round(sc, 4)} for sc, s, t in scored[:k] if sc > 0]
