# hwp-agent — 한글(HWP) 문서 편집·변환 에이전트

자연어 지시로 HWP 문서를 **편집**(텍스트 치환·표 셀·이미지)하고 **PDF/Word와 변환**하는 로컬 도구.
한컴오피스(한글) COM 자동화를 사용하므로 **Windows + 한컴오피스 설치가 반드시 필요**합니다(Linux/서버 불가).

## 전제 (Prerequisites)
- Windows 10/11
- **한컴오피스(한글) 설치** — COM `HWPFrame.HwpObject`
- Python 3.10+
- 자연어 편집을 쓰려면 `GEMINI_API_KEY` 환경변수 (변환·검사만 쓰면 불필요)

## 설치
```powershell
git clone https://github.com/<owner>/hwp-agent.git
cd hwp-agent
pip install -r requirements.txt
python run_hwp.py doctor      # 한컴/COM 연결 확인
```

## 사용법
```powershell
# 변환 (확장자로 방향 자동 인식)
python run_hwp.py convert in.hwp out.pdf
python run_hwp.py convert in.hwp out.docx
python run_hwp.py convert in.docx out.hwp

# 문서 구조 보기 (표/이미지/텍스트 인덱스)
python run_hwp.py inspect doc.hwp

# 자연어 편집 (기본 dry-run, --apply로 실제 저장; 원본 보존 → 별도 파일 생성)
$env:GEMINI_API_KEY="..."
python run_hwp.py edit doc.hwp "회사명 ㈜OOOO를 ㈜폴라펄스로 바꿔줘" --apply
python run_hwp.py edit doc.hwp "둘째 표 3행 2열을 1,000,000으로" --out result.hwp --apply
```

## 안전 원칙
- 모든 한컴 작업을 **시간제한 자식 프로세스**로 격리 — 멈춰도 그 작업이 띄운 한컴만 종료(사용자가 연 한컴은 불간섭).
- 편집은 **원본을 읽기전용으로 열고 별도 파일로 저장**(원본 불변).
- `PathPolicy`로 지정 폴더 밖 접근 차단.
- 각 편집 op는 적용 후 **자동 검증**(예: 셀 값 일치) — 실패 시 저장하지 않음.

## v1 신뢰도 (정직하게)
- ✅ **확실**: 텍스트 찾기/바꾸기, 변환(HWP↔PDF/Word, PDF→HWP[저품질])
- ✅ **표 셀 편집**: 신청서/양식의 "항목명 | 값" 칸은 **라벨 기준(cell_by_label)** 으로 견고하게 채움(병합 값셀 포함). 주소 기반(row/col)도 goto_addr로 병합 인식. 모든 쓰기는 **적용 후 자동검증** — 실패 시 저장 안 함. (병합으로 덮인 셀을 직접 주소 지정하면 안전하게 거부)
- ⚠️ **best-effort**: 이미지 교체(실험적, geometry 미보존)

## 테스트
```powershell
pip install pytest
pytest        # COM 비의존 단위테스트 9개 (PathPolicy/포맷코드/리졸버/LLM 파싱)
```

## 구조
`com_session`(세션+경로정책) · `convert`(변환) · `inspect`(구조 인덱스) ·
`edit_ops`(셀렉터 리졸버+적용+검증) · `executor`(bounded 실행기) ·
`llm`(자연어→op, Gemini) · `cli`/`run_hwp`(엔트리)

> LLM은 코드를 생성하지 않습니다. 자연어 지시 + 문서 구조 요약 → **구조화된 JSON 편집 op** → 파이썬이 검증된 COM 호출로 결정적 적용.
