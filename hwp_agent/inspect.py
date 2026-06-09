"""Build a structured index of an open HWP doc (for LLM targeting + resolvers).

Runs inside a Hancom COM session (pyhwpx Hwp). Verified primitives: GetTextFile,
table_to_df, HeadCtrl/Next/CtrlID, get_into_nth_table. `near_text_before` for
tables/images is best-effort (true paragraph/table interleaving is hard via the
high-level API) and flagged in `notes`.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Dict, List


def _norm(s: Any) -> str:
    if s is None:
        return ""
    t = str(s).replace("\r\n", "\n").replace("\r", "\n")
    t = re.sub(r"[ \t]+", " ", t)
    return re.sub(r"\n{3,}", "\n\n", t).strip()


def _sha(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _full_text(hwp) -> str:
    try:
        return hwp.GetTextFile("TEXT", "") or ""
    except Exception:
        return ""


def _count_ctrls(hwp) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    ctrl = getattr(hwp, "HeadCtrl", None)
    while ctrl is not None:
        cid = getattr(ctrl, "CtrlID", "") or ""
        counts[cid] = counts.get(cid, 0) + 1
        ctrl = getattr(ctrl, "Next", None)
    return counts


def _table_info(hwp, n: int) -> Dict[str, Any]:
    df = hwp.table_to_df(n)
    rows = [[_norm(x) for x in df.iloc[i].tolist()] for i in range(len(df))]
    headers = rows[0] if rows else []
    first_data = rows[1] if len(rows) > 1 else []
    fp = "tbl:" + _sha({"shape": [len(df), df.shape[1]], "headers": headers, "first": first_data})[:20]
    return {
        "table_n": n,
        "shape": {"rows": int(df.shape[0]), "cols": int(df.shape[1])},
        "headers": headers,
        "fingerprint": fp,
        "sample_rows": rows[:3],
        "merged_cells_detected": False,  # v1 best-effort flag
    }


def _occurrence_map(text: str, max_terms: int = 300, max_hits: int = 15) -> Dict[str, Any]:
    """term -> {count, hits:[{offset, context_before, context_after}]} for targeting."""
    terms = re.findall(r"[0-9A-Za-z가-힣]{2,}", text)
    freq: Dict[str, int] = {}
    for t in terms:
        freq[t] = freq.get(t, 0) + 1
    out: Dict[str, Any] = {}
    for term, _ in sorted(freq.items(), key=lambda x: x[1], reverse=True)[:max_terms]:
        hits, start = [], 0
        while len(hits) < max_hits:
            i = text.find(term, start)
            if i < 0:
                break
            a, b = max(0, i - 24), min(len(text), i + len(term) + 24)
            hits.append({"offset": i, "context_before": text[a:i], "context_after": text[i + len(term):b]})
            start = i + len(term)
        out[term] = {"count": freq[term], "hits": hits}
    return out


def build_index(hwp) -> Dict[str, Any]:
    text = _norm(_full_text(hwp))
    ctrl_counts = _count_ctrls(hwp)

    tables: List[Dict[str, Any]] = []
    n = 1
    while True:
        try:
            tables.append(_table_info(hwp, n))
        except Exception:
            break
        n += 1
        if n > 500:  # safety cap
            break

    images: List[Dict[str, Any]] = []
    ordinal = 0
    ctrl = getattr(hwp, "HeadCtrl", None)
    while ctrl is not None:
        if (getattr(ctrl, "CtrlID", "") or "") == "gso":
            ordinal += 1
            images.append({"image_ordinal": ordinal, "ctrl_id": "gso", "near_text_before": "", "page": None})
        ctrl = getattr(ctrl, "Next", None)

    doc_fp = "doc:" + _sha({
        "text_head": text[:4000],
        "table_fps": [t["fingerprint"] for t in tables],
        "image_count": len(images),
    })[:24]

    return {
        "schema_version": "inspect-1.0.0",
        "document_fingerprint": doc_fp,
        "ctrl_counts": ctrl_counts,
        "text": {"full_text_norm": text, "occurrence_map": _occurrence_map(text)},
        "tables": tables,
        "images": images,
        "notes": [
            "near_text_before for tables/images is best-effort and currently empty in v1",
            "page fields are null (no cheap reliable page API wired yet)",
        ],
    }


def summary_for_llm(index: Dict[str, Any], max_text: int = 3000) -> str:
    """Compact human/LLM-readable summary the model uses to target edits."""
    lines = [f"문서지문: {index['document_fingerprint']}",
             f"표 {len(index['tables'])}개, 이미지 {len(index['images'])}개", ""]
    for t in index["tables"]:
        lines.append(f"[표 {t['table_n']}] {t['shape']['rows']}행x{t['shape']['cols']}열 "
                     f"헤더={t['headers'][:6]} fp={t['fingerprint']}")
    lines.append("")
    lines.append("본문(앞부분):")
    lines.append(index["text"]["full_text_norm"][:max_text])
    return "\n".join(lines)
