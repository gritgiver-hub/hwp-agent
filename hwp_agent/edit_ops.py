"""Deterministic resolver + applier for structured edit ops.

The LLM emits structured ops (never code); this module resolves the target
against the inspect index (selector chain + scoring + min_confidence/ambiguity
gate), applies via pyhwpx, and verifies pre/postconditions. Op types:
  - text_find_replace   (most reliable; verified in spike)
  - table_set_cell      (verified: get_into_nth_table + cell nav + insert_text)
  - image_replace       (experimental; several Run actions are verify-on-machine)
"""
from __future__ import annotations

import difflib
import re
from typing import Any, Dict, List, Tuple


def _norm(s: Any) -> str:
    if s is None:
        return ""
    return re.sub(r"[ \t]+", " ", str(s).replace("\r\n", "\n").replace("\r", "\n")).strip()


def _sim(a: str, b: str) -> float:
    a, b = _norm(a), _norm(b)
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _ambiguous(cands: List[Tuple[float, Any]], margin: float = 0.08) -> bool:
    return len(cands) >= 2 and (cands[0][0] - cands[1][0]) < margin


def _run(hwp, action: str) -> bool:
    try:
        hwp.Run(action)
        return True
    except Exception:
        return False


def _cell_value(hwp, table_n: int, row1: int, col1: int) -> str:
    df = hwp.table_to_df(table_n)
    r0, c0 = row1 - 1, col1 - 1
    if r0 < 0 or c0 < 0 or r0 >= len(df.index) or c0 >= df.shape[1]:
        raise IndexError(f"cell out of range t={table_n} r={row1} c={col1}")
    return _norm(df.iloc[r0, c0])


def _cell_rc(hwp):
    """Current cell as (row, col) 1-based from get_cell_addr('A1'), or None."""
    try:
        a = hwp.get_cell_addr("str")
    except Exception:
        return None
    m = re.match(r"([A-Za-z]+)(\d+)", a or "")
    if not m:
        return None
    col = 0
    for ch in m.group(1).upper():
        col = col * 26 + (ord(ch) - 64)
    return (int(m.group(2)), col)


def _set_table_cell(hwp, table_n: int, row1: int, col1: int, value: str) -> None:
    """Navigate to (row1,col1) by walking cells in document order (Tab/TableRightCell
    wraps rows), matching get_cell_addr — robust to ambient cursor state. Merged
    cells have no distinct address for inner coords -> raises (caught upstream)."""
    hwp.get_into_nth_table(table_n, select_cell=True)  # enter table, first cell selected
    target = (row1, col1)
    seen, steps = set(), 0
    cur = _cell_rc(hwp)
    while cur != target:
        if cur is not None:
            if cur in seen:
                raise RuntimeError(f"cell {target} not reachable (cycled at {cur})")
            seen.add(cur)
        if not _run(hwp, "TableRightCell"):
            raise RuntimeError("TableRightCell failed")
        steps += 1
        if steps > 2000:
            raise RuntimeError(f"cell {target} unreachable after {steps} steps")
        cur = _cell_rc(hwp)
    # select this cell's content and replace it
    _run(hwp, "TableCellBlock")
    _run(hwp, "Delete")
    hwp.insert_text(value)


# ---- selector resolution ---------------------------------------------------

def _score_tables(index, selectors) -> List[Tuple[float, Dict[str, Any], List[str]]]:
    out = []
    for t in index.get("tables", []):
        score, reasons = 0.0, []
        for s in selectors:
            st = s.get("strategy", "")
            if st == "table_by_index" and t["table_n"] == s.get("table_index"):
                score += 0.70; reasons.append("index")
            elif st == "table_by_fingerprint" and t["fingerprint"] == s.get("table_fingerprint"):
                score += 0.95; reasons.append("fingerprint")
            elif st == "table_by_near_text":
                sim = _sim(t.get("near_text_before", ""), s.get("near_text_before", ""))
                score += 0.45 * sim
                if sim:
                    reasons.append(f"near={sim:.2f}")
        if score > 0:
            out.append((_clamp01(score), t, reasons))
    out.sort(key=lambda x: x[0], reverse=True)
    return out


def _resolve_cell(table, selector) -> Tuple[int, int]:
    st = selector.get("strategy")
    if st == "cell_by_row_col":
        return int(selector["row_index"]), int(selector["col_index"])
    if st == "cell_by_header":
        headers = [_norm(h) for h in table.get("headers", [])]
        name = _norm(selector.get("header_name", ""))
        try:
            col0 = next(i for i, h in enumerate(headers) if h == name)
        except StopIteration:
            raise ValueError(f"header not found: {name}")
        hri = int(selector.get("header_row_index", 1))
        row1 = hri + int(selector.get("row_index", 1)) if selector.get("row_mode", "data_row") != "physical" else int(selector.get("row_index", 1))
        return row1, col0 + 1
    raise ValueError(f"unsupported cell selector: {st}")


def _find_gso(hwp, ordinal1: int):
    i = 0
    ctrl = getattr(hwp, "HeadCtrl", None)
    while ctrl is not None:
        if (getattr(ctrl, "CtrlID", "") or "") == "gso":
            i += 1
            if i == ordinal1:
                return ctrl
        ctrl = getattr(ctrl, "Next", None)
    return None


def _check_conditions(hwp, conds, ctx) -> Tuple[bool, List[str]]:
    errs = []
    for c in conds or []:
        kind = c.get("kind")
        if kind == "cell_equals" and _norm(ctx.get("cell_after")) != _norm(c.get("value", "")):
            errs.append(f"cell_equals: {ctx.get('cell_after')!r} != {c.get('value')!r}")
        elif kind == "text_occurrence_count":
            text = _norm(hwp.GetTextFile("TEXT", "") or "")
            needle = _norm(c.get("value", ""))
            actual = text.count(needle) if needle else 0
            if actual != int(c.get("expected_count", -1)):
                errs.append(f"text_count {needle!r}: {actual} != {c.get('expected_count')}")
        elif kind == "target_exists" and not ctx.get("target_exists", False):
            errs.append("target_exists failed")
    return (not errs), errs


def _gate(best_score, cands, op, result):
    """Return error code if gate fails, else None."""
    result["confidence"] = best_score
    if best_score < float(op.get("min_confidence", 0.0)):
        return "LOW_CONFIDENCE"
    if _ambiguous(cands):
        result["ambiguous"] = True
        pol = op.get("ambiguity_policy", "fail")
        if pol == "ask_user":
            return "AMBIGUOUS_NEED_USER"
        if pol == "fail":
            return "AMBIGUOUS_TARGET"
    return None


def resolve_and_apply(hwp, index: Dict[str, Any], op: Dict[str, Any], dry_run: bool) -> Dict[str, Any]:
    typ = op.get("type")
    selectors = op.get("target", {}).get("selector_chain", [])
    action = op.get("action", {})
    result = {"op_id": op.get("op_id", "?"), "ok": False, "code": "", "applied": False,
              "confidence": 0.0, "ambiguous": False, "details": {}, "errors": []}

    if typ == "table_set_cell":
        table_selectors = [s for s in selectors if s.get("strategy", "").startswith("table_")]
        cands = _score_tables(index, table_selectors)
        result["details"]["candidates"] = [(c[1]["table_n"], round(c[0], 2), c[2]) for c in cands[:5]]
        if not cands:
            result["code"] = "TARGET_NOT_FOUND"; return result
        gate = _gate(cands[0][0], [(c[0], c[1]) for c in cands], op, result)
        if gate:
            result["code"] = gate; return result
        table = cands[0][1]
        cell_sel = next((s for s in selectors if s.get("strategy", "").startswith("cell_")), None)
        if not cell_sel:
            result["code"] = "BAD_SELECTOR"; result["errors"].append("missing cell selector"); return result
        try:
            row1, col1 = _resolve_cell(table, cell_sel)
            before = _cell_value(hwp, table["table_n"], row1, col1)
        except Exception as e:
            result["code"] = "RESOLVE_FAILED"; result["errors"].append(str(e)); return result
        expected = _norm(cell_sel.get("expected_current_value", ""))
        if expected and _norm(before) != expected:
            result["code"] = "EXPECTED_MISMATCH"
            result["errors"].append(f"{before!r} != {expected!r}"); return result
        new_value = _norm(action.get("new_value", ""))
        if dry_run or op.get("dry_run"):
            result.update(ok=True, code="DRY_RUN",
                          details={**result["details"], "table_n": table["table_n"], "row": row1, "col": col1,
                                   "before": before, "after": new_value})
            return result
        try:
            _set_table_cell(hwp, table["table_n"], row1, col1, new_value)
            after = _cell_value(hwp, table["table_n"], row1, col1)
        except Exception as e:
            result["code"] = "APPLY_FAILED"; result["errors"].append(str(e)); return result
        result["details"].update(table_n=table["table_n"], row=row1, col=col1, before=before, after=after)
        if _norm(after) != _norm(new_value):  # auto-verify the write landed in the right cell
            result["code"] = "VERIFY_FAILED"
            result["errors"].append(f"cell after write = {after!r} != {new_value!r}")
            return result
        ok, errs = _check_conditions(hwp, op.get("postconditions"), {"cell_after": after, "target_exists": True})
        if not ok:
            result["code"] = "POSTCONDITION_FAILED"; result["errors"] += errs; return result
        result.update(ok=True, applied=True, code="OK"); return result

    if typ == "text_find_replace":
        find = _norm(action.get("find_text", ""))
        repl = _norm(action.get("replace_text", ""))
        text = index.get("text", {}).get("full_text_norm", "")
        n_before = text.count(find) if find else 0
        if n_before == 0:
            result["code"] = "TARGET_NOT_FOUND"; result["confidence"] = 0.0; return result
        result["confidence"] = 0.9
        if dry_run or op.get("dry_run"):
            result.update(ok=True, code="DRY_RUN",
                          details={"find": find, "replace": repl, "occurrences": n_before})
            return result
        try:
            hwp.find_replace_all(find, repl)
        except Exception as e:
            result["code"] = "APPLY_FAILED"; result["errors"].append(str(e)); return result
        ok, errs = _check_conditions(hwp, op.get("postconditions"), {"target_exists": True})
        if not ok:
            result["code"] = "POSTCONDITION_FAILED"; result["errors"] += errs; return result
        result.update(ok=True, applied=True, code="OK", details={"find": find, "replace": repl,
                                                                 "occurrences": n_before})
        return result

    if typ == "image_replace":
        imgs = index.get("images", [])
        cands = []
        for im in imgs:
            score, reasons = 0.0, []
            for s in selectors:
                st = s.get("strategy", "")
                if st == "image_by_index" and im["image_ordinal"] == s.get("image_index"):
                    score += 0.70; reasons.append("index")
                elif st == "image_by_near_text":
                    sim = _sim(im.get("near_text_before", ""), s.get("near_text_before", ""))
                    score += 0.50 * sim
            if score > 0:
                cands.append((_clamp01(score), im, reasons))
        cands.sort(key=lambda x: x[0], reverse=True)
        if not cands:
            result["code"] = "TARGET_NOT_FOUND"; return result
        gate = _gate(cands[0][0], [(c[0], c[1]) for c in cands], op, result)
        if gate:
            result["code"] = gate; return result
        ordinal = int(cands[0][1]["image_ordinal"])
        path = action.get("new_image_path", "")
        if dry_run or op.get("dry_run"):
            result.update(ok=True, code="DRY_RUN", details={"image_ordinal": ordinal, "new_image_path": path})
            return result
        try:
            ctrl = _find_gso(hwp, ordinal)
            if ctrl is None:
                raise ValueError(f"image ordinal {ordinal} not found")
            # experimental: select+delete the target object, insert replacement
            _run(hwp, "SelectCtrlFront")
            _run(hwp, "Delete")
            hwp.insert_picture(path)
        except Exception as e:
            result["code"] = "APPLY_FAILED"; result["errors"].append(str(e)); return result
        result.update(ok=True, applied=True, code="OK",
                      details={"image_ordinal": ordinal, "note": "image geometry not preserved (v1)"})
        return result

    result["code"] = "UNSUPPORTED_OP_TYPE"
    return result
