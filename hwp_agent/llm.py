"""Natural-language (Korean) edit instruction -> validated structured op plan.

The LLM never writes code; it emits JSON ops that edit_ops applies deterministically.
Reuses the repo's robust JSON parse (mail_todo.extractor.parse_llm_json) and the
google-genai pattern.
"""
from __future__ import annotations

import os
from typing import Any, Callable, Dict, Optional

from hwp_agent._jsonparse import parse_llm_json
from hwp_agent.inspect import summary_for_llm

_PROMPT = """당신은 HWP 문서 편집 지시를 '구조화된 편집 op JSON'으로 변환합니다.
코드를 쓰지 말고 JSON만 출력하세요. 표/이미지는 아래 문서 구조의 번호(table_n, image_ordinal)를 우선 사용하세요.

[문서 구조]
{summary}

[사용자 지시]
{instruction}

다음 형식의 JSON만 출력:
{{"operations": [ ... ]}}

op 종류:
1) 텍스트 치환
{{"type":"text_find_replace","min_confidence":0.8,"ambiguity_policy":"fail",
  "target":{{"selector_chain":[{{"strategy":"text_by_match_context","match_text":"<찾을문구>"}}]}},
  "action":{{"kind":"find_replace","find_text":"<찾을문구>","replace_text":"<바꿀문구>","replace_all":true}},
  "postconditions":[{{"kind":"text_occurrence_count","value":"<찾을문구>","expected_count":0}}]}}
2) 표 셀 값
{{"type":"table_set_cell","min_confidence":0.7,"ambiguity_policy":"ask_user",
  "target":{{"selector_chain":[{{"strategy":"table_by_index","table_index":<N>}},
     {{"strategy":"cell_by_row_col","row_index":<행>,"col_index":<열>}}]}},
  "action":{{"kind":"set_cell_text","new_value":"<새 값>"}}}}
   (헤더로 지정 시 cell_by_header: {{"strategy":"cell_by_header","header_name":"<헤더>","row_index":1,"row_mode":"data_row"}})
3) 이미지 교체
{{"type":"image_replace","min_confidence":0.8,"ambiguity_policy":"fail",
  "target":{{"selector_chain":[{{"strategy":"image_by_index","image_index":<N>}}]}},
  "action":{{"kind":"replace_image","new_image_path":"<경로>"}}}}

규칙: 지시가 모호하면 ambiguity_policy를 "ask_user"로. 행/열·표번호는 1부터.
"""


def gemini_llm(prompt: str, model: Optional[str] = None) -> str:
    from google import genai
    model = model or os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    return getattr(client.models.generate_content(model=model, contents=prompt), "text", "") or ""


def build_plan(instruction: str, index: Dict[str, Any],
               llm: Optional[Callable[[str], str]] = None, dry_run: bool = True) -> Dict[str, Any]:
    llm = llm or gemini_llm
    prompt = _PROMPT.format(summary=summary_for_llm(index), instruction=instruction)
    parsed = parse_llm_json(llm(prompt))
    ops = parsed.get("operations") if isinstance(parsed, dict) else None
    ops = ops or []
    for i, op in enumerate(ops):
        op.setdefault("op_id", f"op-{i + 1}")
        op.setdefault("ambiguity_policy", "fail")
        op.setdefault("min_confidence", 0.7)
    return {
        "schema_version": "1.0.0",
        "dry_run": dry_run,
        "document": {"path": "", "fingerprint": index.get("document_fingerprint", "")},
        "operations": ops,
    }
