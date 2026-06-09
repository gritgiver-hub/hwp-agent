"""Unit tests for hwp_agent pure logic (no Hancom COM required)."""
import pandas as pd
import pytest

from hwp_agent.com_session import PathPolicy, fmt_code
from hwp_agent import edit_ops, inspect as hi, llm


# ---- PathPolicy ----

def test_pathpolicy_denies_and_allows(tmp_path):
    allowed = tmp_path / "ok"
    allowed.mkdir()
    pol = PathPolicy(allow_roots=[allowed], deny_roots=["G:\\"])
    assert pol.resolve(str(allowed / "a.hwp")).endswith("a.hwp")
    with pytest.raises(PermissionError):
        pol.resolve(str(tmp_path / "outside.hwp"))


def test_pathpolicy_deny_takes_precedence(tmp_path):
    pol = PathPolicy(allow_roots=[tmp_path], deny_roots=[tmp_path / "secret"])
    (tmp_path / "secret").mkdir()
    with pytest.raises(PermissionError):
        pol.resolve(str(tmp_path / "secret" / "x.hwp"))


def test_fmt_code():
    assert fmt_code("docx") == "OOXML"
    assert fmt_code("pdf") == "PDF"
    assert fmt_code(None, "a.hwp") == "HWP"
    assert fmt_code(None, "a.docx") == "OOXML"


# ---- inspect pure logic ----

def test_occurrence_map_finds_context():
    text = "시행일 2025년 부터 적용. 그리고 2025년 다시."
    m = hi._occurrence_map(text)
    assert "2025년" in m and m["2025년"]["count"] == 2
    assert "시행일" in m["2025년"]["hits"][0]["context_before"]


# ---- edit_ops resolver (pure / dry-run) ----

def _index_one_table():
    return {
        "tables": [{"table_n": 1, "shape": {"rows": 3, "cols": 2},
                    "headers": ["항목", "금액"], "fingerprint": "tbl:abc",
                    "near_text_before": "청구 내역", "sample_rows": []}],
        "images": [{"image_ordinal": 1, "near_text_before": "회사 로고"}],
        "text": {"full_text_norm": "금액은 120,000 입니다."},
    }


class _FakeHwp:
    def __init__(self, df):
        self._df = df

    def table_to_df(self, n):
        return self._df


def test_text_find_replace_dry_run_and_not_found():
    idx = _index_one_table()
    op = {"op_id": "t1", "type": "text_find_replace",
          "action": {"find_text": "120,000", "replace_text": "130,000"}}
    r = edit_ops.resolve_and_apply(None, idx, op, dry_run=True)
    assert r["ok"] and r["code"] == "DRY_RUN" and r["details"]["occurrences"] == 1

    op2 = {"op_id": "t2", "type": "text_find_replace",
           "action": {"find_text": "없는문구", "replace_text": "x"}}
    r2 = edit_ops.resolve_and_apply(None, idx, op2, dry_run=True)
    assert not r2["ok"] and r2["code"] == "TARGET_NOT_FOUND"


def test_table_set_cell_dry_run_by_header():
    idx = _index_one_table()
    # table_to_df promotes the first table row to df.columns, so model that here.
    df = pd.DataFrame([["수수료", "120,000"], ["세금", "0"]], columns=["항목", "금액"])
    op = {"op_id": "c1", "type": "table_set_cell", "min_confidence": 0.5,
          "target": {"selector_chain": [
              {"strategy": "table_by_index", "table_index": 1},
              {"strategy": "cell_by_header", "header_name": "금액", "row_index": 1,
               "row_mode": "data_row", "expected_current_value": "120,000"}]},
          "action": {"new_value": "130,000"}}
    r = edit_ops.resolve_and_apply(_FakeHwp(df), idx, op, dry_run=True)
    assert r["ok"] and r["code"] == "DRY_RUN"
    assert r["details"]["row"] == 2 and r["details"]["col"] == 2
    assert r["details"]["before"] == "120,000" and r["details"]["after"] == "130,000"


def test_table_set_cell_expected_mismatch():
    idx = _index_one_table()
    df = pd.DataFrame([["수수료", "999"]], columns=["항목", "금액"])
    op = {"op_id": "c2", "type": "table_set_cell", "min_confidence": 0.5,
          "target": {"selector_chain": [
              {"strategy": "table_by_index", "table_index": 1},
              {"strategy": "cell_by_row_col", "row_index": 2, "col_index": 2,
               "expected_current_value": "120,000"}]},
          "action": {"new_value": "130,000"}}
    r = edit_ops.resolve_and_apply(_FakeHwp(df), idx, op, dry_run=True)
    assert not r["ok"] and r["code"] == "EXPECTED_MISMATCH"


def test_table_low_confidence_gate():
    idx = _index_one_table()
    op = {"op_id": "c3", "type": "table_set_cell", "min_confidence": 0.99,
          "target": {"selector_chain": [
              {"strategy": "table_by_index", "table_index": 1},
              {"strategy": "cell_by_row_col", "row_index": 1, "col_index": 1}]},
          "action": {"new_value": "x"}}
    r = edit_ops.resolve_and_apply(_FakeHwp(pd.DataFrame([["a"]])), idx, op, dry_run=True)
    assert not r["ok"] and r["code"] == "LOW_CONFIDENCE"  # index match scores 0.70 < 0.99


# ---- table cell address + label resolver (pure) ----

def test_excel_addr():
    assert edit_ops._excel_addr(1, 1) == "A1"
    assert edit_ops._excel_addr(2, 2) == "B2"
    assert edit_ops._excel_addr(3, 27) == "AA3"


def test_find_label_rc():
    grid = [["과제명", "기존값"], ["신청기업", "회사"]]
    assert edit_ops._find_label_rc(grid, "과제명") == (1, 1)
    assert edit_ops._find_label_rc(grid, "신청기업") == (2, 1)
    assert edit_ops._find_label_rc(grid, "없음") is None


def test_table_set_cell_by_label_dry_run():
    idx = _index_one_table()
    df = pd.DataFrame([["dummy1", "dummy2"]], columns=["과제명", "기존값"])
    op = {"op_id": "L1", "type": "table_set_cell", "min_confidence": 0.5,
          "target": {"selector_chain": [
              {"strategy": "table_by_index", "table_index": 1},
              {"strategy": "cell_by_label", "label_text": "과제명", "direction": "right"}]},
          "action": {"new_value": "AX 디바이스 사업"}}
    r = edit_ops.resolve_and_apply(_FakeHwp(df), idx, op, dry_run=True)
    assert r["ok"] and r["code"] == "DRY_RUN"
    assert r["details"]["target"] == [1, 2]
    assert r["details"]["before"] == "기존값" and r["details"]["after"] == "AX 디바이스 사업"


# ---- llm plan building (fake llm) ----

def test_build_plan_with_fake_llm():
    idx = _index_one_table()
    idx["document_fingerprint"] = "doc:xyz"

    def fake_llm(prompt):
        assert "사용자 지시" in prompt
        return ('{"operations": [{"type":"text_find_replace",'
                '"action":{"find_text":"2025년","replace_text":"2026년"}}]}')

    plan = llm.build_plan("연도 바꿔줘", idx, llm=fake_llm, dry_run=True)
    assert plan["dry_run"] is True
    assert plan["document"]["fingerprint"] == "doc:xyz"
    op = plan["operations"][0]
    assert op["op_id"] == "op-1" and op["type"] == "text_find_replace"
    assert op["ambiguity_policy"] == "fail" and op["min_confidence"] == 0.7
