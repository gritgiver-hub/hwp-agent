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
import os
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
    """Cell value in *physical-row* coordinates: row 1 == the table's first row
    (header). This MUST match goto_addr / _set_table_cell / _find_label_rc, which
    all use physical addresses -- table_to_df promotes row 0 to df.columns, so
    df.iloc would be off by one row for the header. Use the full grid instead."""
    g = _grid(hwp, table_n)
    r0, c0 = row1 - 1, col1 - 1
    if r0 < 0 or c0 < 0 or not g or r0 >= len(g) or c0 >= len(g[0]):
        raise IndexError(f"cell out of range t={table_n} r={row1} c={col1}")
    return _norm(g[r0][c0])


def _excel_addr(row1: int, col1: int) -> str:
    """1-based (row, col) -> Excel-style address, e.g. (2, 2) -> 'B2'."""
    s, c = "", col1
    while c > 0:
        c, r = divmod(c - 1, 26)
        s = chr(65 + r) + s
    return f"{s}{row1}"


def _set_table_cell(hwp, table_n: int, row1: int, col1: int, value: str) -> None:
    """Write a cell value reliably (verified against merge-free and merged tables).

    Two pyhwpx facts make this work where the naive approach fails:
      1) goto_addr() is merge-aware (expands merged spans internally) and lands on
         the exact addressed cell -- unlike a manual TableRightCell walk, which
         miscounts across merged cells.
      2) On a *cell-block* selection (select_cell=True), Run("Delete") does NOT
         clear the cell text (it leaves residue, new text gets prepended). So we
         instead put the caret inside the cell, select its text content with
         MoveListBegin + MoveSelListEnd, Delete that, then insert.
    A merged/out-of-range address makes goto_addr return False -> raise (the caller
    records APPLY_FAILED; the post-write auto-verify is an additional guard)."""
    hwp.get_into_nth_table(table_n)  # caret into table 1st cell, deselected (is_cell True)
    if not hwp.goto_addr(_excel_addr(row1, col1), select_cell=False):
        raise RuntimeError(f"goto_addr {_excel_addr(row1, col1)} failed "
                           f"(cell merged or out of range in table {table_n})")
    _clear_cell_and_type(hwp, value)


def _clear_cell_and_type(hwp, value: str) -> None:
    """Caret is inside the target cell: select its text content and replace.
    (Run('Delete') on a *cell-block* selection leaves residue, so we select the
    cell's text list instead -- MoveListBegin..MoveSelListEnd -- then Delete.)"""
    _run(hwp, "MoveListBegin")
    _run(hwp, "MoveSelListEnd")
    _run(hwp, "Delete")
    hwp.insert_text(value)
    _run(hwp, "Cancel")


def _grid(hwp, table_n: int) -> List[List[str]]:
    """Full cell grid (header row + data rows) as normalized strings. Merged cells
    appear as their repeated value (pyhwpx fills the span), which is what label
    search wants."""
    df = hwp.table_to_df(table_n)
    return [[_norm(x) for x in df.columns.tolist()]] + \
           [[_norm(x) for x in df.iloc[i].tolist()] for i in range(len(df))]


def _find_label_rc(grid: List[List[str]], label: str, contains: bool = True):
    """First (row1, col1) 1-based whose cell text matches label, or None."""
    lab = _norm(label)
    for ri, row in enumerate(grid):
        for ci, val in enumerate(row):
            if (lab in val) if contains else (val == lab):
                return (ri + 1, ci + 1)
    return None


def _table_has_label(table: Dict[str, Any], label: str) -> bool:
    lab = _norm(label)
    cells = list(table.get("headers", []))
    for row in table.get("sample_rows", []):
        cells += list(row)
    return any(lab in _norm(x) for x in cells)


def _table_with_label(index: Dict[str, Any], label: str, hint_n=None):
    """Pick the table to fill by *label presence* (across the whole index), so the
    LLM doesn't have to guess the right table_index among many. Prefer the hinted
    table if it contains the label; else the first table that does; else the hint."""
    tables = index.get("tables", [])
    by_n = {t["table_n"]: t for t in tables}
    if hint_n is not None and hint_n in by_n and _table_has_label(by_n[hint_n], label):
        return by_n[hint_n]
    for t in tables:
        if _table_has_label(t, label):
            return t
    return by_n.get(hint_n)


def _neighbor(r1: int, c1: int, direction: str):
    return {"right": (r1, c1 + 1), "below": (r1 + 1, c1),
            "left": (r1, c1 - 1), "above": (r1 - 1, c1)}.get(direction, (r1, c1 + 1))


def _smart_label_target(hwp, index, label, hint_n, llm_dir):
    """Resolve (table, target cell, direction) for a label-anchored fill by reading
    candidate tables' grids and preferring a *fillable* (empty) adjacent cell. This
    fixes the two hard cases of LLM form-filling: a wrong direction guess (value is
    usually in the empty neighbour) and the same label appearing in several tables
    (pick the one with a blank slot). Returns
    (score, table_n, label_rc, target_rc, direction, before) or None."""
    tables = index.get("tables", [])
    cand_tn = [t["table_n"] for t in tables if _table_has_label(t, label)]
    if not cand_tn:
        cand_tn = [hint_n] if hint_n is not None else [t["table_n"] for t in tables]
    dirs = []
    for d in (llm_dir, "right", "below"):
        if d in ("right", "below", "left", "above") and d not in dirs:
            dirs.append(d)
    best = None
    for tn in cand_tn:
        if tn is None:
            continue
        try:
            grid = _grid(hwp, tn)
        except Exception:
            continue
        rc = _find_label_rc(grid, label)
        if rc is None:
            continue
        r, c = rc
        for d in dirs:
            tr, tc = _neighbor(r, c, d)
            if not (1 <= tr <= len(grid) and grid and 1 <= tc <= len(grid[0])):
                continue
            val = _norm(grid[tr - 1][tc - 1])
            score = (3 if val == "" else 0) + (2 if d == llm_dir else 0) \
                + (1 if tn == hint_n else 0) + (0.5 if d == "right" else 0)
            if best is None or score > best[0]:
                best = (score, tn, (r, c), (tr, tc), d, val)
    return best


def _set_cell_by_label(hwp, table_n: int, label: str, direction: str, value: str):
    """Fill the cell adjacent to a label cell (robust for merged 'label | value'
    forms): find the (non-merged) label cell, goto its address, step into the
    neighbour, then replace. Returns (label_rc, target_rc, landed_addr)."""
    grid = _grid(hwp, table_n)
    rc = _find_label_rc(grid, label)
    if rc is None:
        raise RuntimeError(f"label {label!r} not found in table {table_n}")
    lr, lc = rc
    hwp.get_into_nth_table(table_n)
    if not hwp.goto_addr(_excel_addr(lr, lc), select_cell=False):
        raise RuntimeError(f"goto label cell {_excel_addr(lr, lc)} failed (label cell merged?)")
    act = {"right": "TableRightCell", "below": "TableLowerCell",
           "left": "TableLeftCell", "above": "TableUpperCell"}.get(direction, "TableRightCell")
    if not _run(hwp, act):
        raise RuntimeError(f"move {act} from label failed")
    landed = None
    try:
        landed = hwp.get_cell_addr("str")
    except Exception:
        pass
    _clear_cell_and_type(hwp, value)
    target_rc = {"right": (lr, lc + 1), "below": (lr + 1, lc),
                 "left": (lr, lc - 1), "above": (lr - 1, lc)}.get(direction, (lr, lc + 1))
    return rc, target_rc, landed


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


def _gso_list(hwp) -> List[Any]:
    """All picture/drawing object ctrls (CtrlID == 'gso') in document order."""
    out, ctrl = [], getattr(hwp, "HeadCtrl", None)
    while ctrl is not None:
        if (getattr(ctrl, "CtrlID", "") or "") == "gso":
            out.append(ctrl)
        ctrl = getattr(ctrl, "Next", None)
    return out


def _find_gso(hwp, ordinal1: int):
    gsos = _gso_list(hwp)
    return gsos[ordinal1 - 1] if 1 <= ordinal1 <= len(gsos) else None


def _set_ctrl_props(ctrl, **kv) -> None:
    p = ctrl.Properties
    for k, v in kv.items():
        p.SetItem(k, v)
    ctrl.Properties = p


def _image_name(hwp, ctrl) -> str:
    try:
        return (hwp.get_image_info(ctrl) or {}).get("name", "")
    except Exception:
        return ""


# Size + placement properties to carry over when swapping a picture, so both
# inline (TreatAsChar=1) and floating images keep their geometry/anchor.
_GEOM_PROPS = (
    "Width", "Height", "WidthRelTo", "HeightRelTo", "TreatAsChar",
    "HorzAlign", "HorzRelTo", "HorzOffset", "VertAlign", "VertRelTo", "VertOffset",
    "TextWrap", "FlowWithText", "AllowOverlap", "NumberingType",
    "OutsideMarginLeft", "OutsideMarginRight", "OutsideMarginTop", "OutsideMarginBottom",
)


def _replace_image(hwp, ordinal1: int, path: str):
    """Swap a picture's content while preserving geometry + placement. Capture the
    target gso's size/anchor properties, delete it, re-insert the new image at the
    same caret position, then restore every captured property (so floating images
    keep their absolute position, not just size). Returns (new_ctrl, geom_dict).
    get_image_info(name) lets the caller verify the swap landed."""
    gsos = _gso_list(hwp)
    if not (1 <= ordinal1 <= len(gsos)):
        raise RuntimeError(f"image ordinal {ordinal1} out of range (have {len(gsos)})")
    target = gsos[ordinal1 - 1]
    props = target.Properties
    geom = {}
    for k in _GEOM_PROPS:
        try:
            v = props.Item(k)
            if v is not None:
                geom[k] = v
        except Exception:
            pass
    tac = geom.get("TreatAsChar", 1)
    try:
        pos = hwp.get_ctrl_pos(target)
    except Exception:
        pos = None
    hwp.move_to_ctrl(target)
    hwp.delete_ctrl(target)
    if pos is not None:
        try:
            hwp.set_pos(*pos)
        except Exception:
            pass
    newc = hwp.insert_picture(os.path.abspath(path), treat_as_char=bool(tac))
    try:
        _set_ctrl_props(newc, **geom)
    except Exception:
        # fall back to size-only if the full set is rejected
        try:
            _set_ctrl_props(newc, Width=geom.get("Width"), Height=geom.get("Height"))
        except Exception:
            pass
    return newc, geom


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
        cell_sel = next((s for s in selectors if s.get("strategy", "").startswith("cell_")), None)
        if not cell_sel:
            result["code"] = "BAD_SELECTOR"; result["errors"].append("missing cell selector"); return result
        table_selectors = [s for s in selectors if s.get("strategy", "").startswith("table_")]
        new_value = _norm(action.get("new_value", ""))

        # --- label-anchored cell (robust for merged 'label | value' forms).
        #     Resolve the table by LABEL across the whole index, so a wrong/absent
        #     table_index from the LLM doesn't break it. ---
        if cell_sel.get("strategy") == "cell_by_label":
            label = cell_sel.get("label_text", "") or cell_sel.get("header_name", "")
            llm_dir = cell_sel.get("direction", "right")
            hint = next((s.get("table_index") for s in table_selectors
                         if s.get("strategy") == "table_by_index"), None)
            try:
                best = _smart_label_target(hwp, index, label, hint, llm_dir)
            except Exception as e:
                result["code"] = "RESOLVE_FAILED"; result["errors"].append(str(e)); return result
            if best is None:
                result["code"] = "TARGET_NOT_FOUND"
                result["errors"].append(f"label not found in any table: {label!r}"); return result
            _, tn, label_rc, target_rc, direction, before = best
            tr, tc = target_rc
            result["details"]["candidates"] = [(tn, "by_label", [direction])]
            result["confidence"] = 0.85
            if dry_run or op.get("dry_run"):
                result.update(ok=True, code="DRY_RUN",
                              details={**result["details"], "table_n": tn, "label": label,
                                       "direction": direction, "target": [tr, tc], "before": before, "after": new_value})
                return result
            try:
                _, target_rc, landed = _set_cell_by_label(hwp, tn, label, direction, new_value)
                after = _cell_value(hwp, tn, target_rc[0], target_rc[1])
            except Exception as e:
                result["code"] = "APPLY_FAILED"; result["errors"].append(str(e)); return result
            result["details"].update(table_n=tn, label=label, direction=direction,
                                     target=list(target_rc), landed=landed, before=before, after=after)
            if _norm(after) != _norm(new_value):
                result["code"] = "VERIFY_FAILED"
                result["errors"].append(f"cell after write = {after!r} != {new_value!r}"); return result
            ok, errs = _check_conditions(hwp, op.get("postconditions"), {"cell_after": after, "target_exists": True})
            if not ok:
                result["code"] = "POSTCONDITION_FAILED"; result["errors"] += errs; return result
            result.update(ok=True, applied=True, code="OK"); return result

        # --- positional cell (row/col or header): score+gate the table by selectors ---
        cands = _score_tables(index, table_selectors)
        result["details"]["candidates"] = [(c[1]["table_n"], round(c[0], 2), c[2]) for c in cands[:5]]
        if not cands:
            result["code"] = "TARGET_NOT_FOUND"; return result
        gate = _gate(cands[0][0], [(c[0], c[1]) for c in cands], op, result)
        if gate:
            result["code"] = gate; return result
        table = cands[0][1]
        tn = table["table_n"]
        try:
            row1, col1 = _resolve_cell(table, cell_sel)
            before = _cell_value(hwp, tn, row1, col1)
        except Exception as e:
            result["code"] = "RESOLVE_FAILED"; result["errors"].append(str(e)); return result
        expected = _norm(cell_sel.get("expected_current_value", ""))
        if expected and _norm(before) != expected:
            result["code"] = "EXPECTED_MISMATCH"
            result["errors"].append(f"{before!r} != {expected!r}"); return result
        if dry_run or op.get("dry_run"):
            result.update(ok=True, code="DRY_RUN",
                          details={**result["details"], "table_n": tn, "row": row1, "col": col1,
                                   "before": before, "after": new_value})
            return result
        try:
            _set_table_cell(hwp, tn, row1, col1, new_value)
            after = _cell_value(hwp, tn, row1, col1)
        except Exception as e:
            result["code"] = "APPLY_FAILED"; result["errors"].append(str(e)); return result
        result["details"].update(table_n=tn, row=row1, col=col1, before=before, after=after)
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
        if not path or not os.path.exists(path):
            result["code"] = "RESOLVE_FAILED"
            result["errors"].append(f"new_image_path not found: {path!r}"); return result
        try:
            _, geom = _replace_image(hwp, ordinal, path)
        except Exception as e:
            result["code"] = "APPLY_FAILED"; result["errors"].append(str(e)); return result
        # auto-verify: the addressed slot now holds the new file
        after_name = _image_name(hwp, _find_gso(hwp, ordinal))
        expect = os.path.basename(path)
        result["details"].update(image_ordinal=ordinal, new_image=expect,
                                 width=geom.get("Width"), height=geom.get("Height"),
                                 treat_as_char=geom.get("TreatAsChar"), after_name=after_name)
        if after_name != expect:
            result["code"] = "VERIFY_FAILED"
            result["errors"].append(f"image after replace = {after_name!r} != {expect!r}"); return result
        result.update(ok=True, applied=True, code="OK"); return result

    result["code"] = "UNSUPPORTED_OP_TYPE"
    return result
