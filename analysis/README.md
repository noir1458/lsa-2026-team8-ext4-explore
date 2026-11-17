# 분석

모든 계산은 저장소의 raw results로 재현한다. Python 3, Matplotlib이 필요하다. 작성 당시 Python 3.12.14, Matplotlib 3.11.2를 사용했다. 설치 예: `python3 -m pip install matplotlib`.

- `analyze_leaf.py`: 질의별 CSV까지 검증하며 같은 round의 tail/baseline·shadow/baseline 비율을 집계한다. 개별 호출 평균의 hit/miss 조건부 통계는 전체 루프 지표와 구분한다.
- `compare_leaf_sessions.py`: B0·첫 실험·독립 확인의 절대 시간 중앙값/범위를 표시한다. 서로 다른 부팅을 paired trial로 계산하지 않는다.
- `analyze_es.py`, `plot_es.py`: SEEK_DATA 메타데이터 workload의 timing/profile을 분리한다. ES tree 전체 hit와 최근 포인터 hit는 다른 개념이다.
- `analyze_es_io.py`: 실제 4KiB pread 대조만 처리한다. buffered/direct의 함수 진입과 exact payload 검증을 확인한다.
- `analyze_inline.py`: 기존 inline_data feature 비교를 처리한다. 시간에는 마지막 sync를 포함한 총시간과 파일 작업시간을 따로 둔다. stat 보고량, 파일시스템 free-block 변화, 실제 inline flag를 분리한다.

도표의 범위는 관측 최소~최대이며 신뢰구간이 아니다. 프로파일링으로 느려진 시간을 성능 지표에 합치지 않는다. 오류나 누락 cell은 성공으로 집계하지 않는다. 1차와 확인 세션은 합치지 않으며 모든 이상치는 원자료에 남긴다.

실행 명령과 환경·측정 범위는 [재현 방법](../docs/reproduce.md), [측정 범위](../docs/measurement-notes.md)를 참고한다.
