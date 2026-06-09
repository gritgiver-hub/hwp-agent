# hwp_agent — HWP(한글) 편집/변환 에이전트 (로컬 Windows + 한컴)

자연어 지시로 HWP 문서를 편집(텍스트 치환·표 셀·이미지)하고 PDF/Word와 변환하는 로컬 도구.
**Windows + 한컴오피스(한글) 설치 필수** (COM 자동화). Linux/서버에서는 동작하지 않음.

## 전제
- Windows 10/11 + **한컴오피스(한글)** 설치 (COM `HWPFrame.HwpObject`)
- Python 3.10+ , `pip install -r requirements-hwp.txt` (pyhwpx, pywin32)
- 자연어 편집은 `GEMINI_API_KEY` 환경변수 필요 (변환/검사는 불필요)

## 설치 확인
```
python run_hwp.py doctor
```

## 사용법
```
# 변환 (확장자로 방향 자동 인식)
python run_hwp.py convert 입력.hwp 출력.pdf
python run_hwp.py convert 입력.hwp 출력.docx
python run_hwp.py convert 입력.docx 출력.hwp

# 문서 구조 보기
python run_hwp.py inspect 문서.hwp

# 자연어 편집 (기본 dry-run, --apply로 실제 저장; 원본은 보존, *_edited.hwp 생성)
python run_hwp.py edit 문서.hwp "회사명 ㈜OOOO를 ㈜폴라펄스로 바꿔줘" --apply
python run_hwp.py edit 문서.hwp "둘째 표 3행 2열을 1,000,000으로" --out 결과.hwp --apply
```

## 동작/안전 원칙
- 모든 한컴 작업은 **시간제한 자식 프로세스**로 격리(어떤 COM 호출이든 hang 가능) — 멈추면 그 작업이 띄운 한컴만 종료, 사용자가 직접 연 한컴은 안 건드림.
- 편집은 **원본을 읽기 전용으로 열고 별도 파일로 저장** (원본 불변).
- `PathPolicy`로 지정 폴더 밖 파일 접근 차단.
- 편집 op는 적용 후 **자동 검증**(예: 셀 값 일치) — 검증 실패 시 저장하지 않음.

## v1 신뢰도 (정직하게)
- ✅ **확실**: 텍스트 찾기/바꾸기, 변환(HWP↔PDF/Word, PDF→HWP[저품질])
- ✅ **표 셀 편집**: 신청서/양식의 "항목명 | 값" 칸은 **라벨 기준(cell_by_label)** 으로 채움. **표 번호를 몰라도 라벨로 표를 자동 탐색**하고, **비어있는 인접 값칸을 자동 선택**(방향 추측 불필요). 주소 기반(row/col)도 goto_addr로 병합 인식. 모든 쓰기는 **적용 후 자동검증** — 실패 시 저장 안 함.
- ✅ **이미지 교체**: 원래 **크기+위치(geometry)를 보존**하며 교체(인라인/플로팅 모두), 교체 후 파일명으로 **자동검증**.
- ✅ **문서 생성**: 빈 문서에서 제목/문단/표(채움)/이미지 블록으로 새 HWP 생성(`executor.generate_document`).

## 구조
com_session(세션+경로정책) · convert(변환) · inspect(구조 인덱스) · edit_ops(리졸버+적용) ·
executor(bounded 실행기) · llm(NL→op, Gemini) · cli/run_hwp(엔트리)
