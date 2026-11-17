# 측정 범위와 해석

## 비교 대상

leaf/ES의 주 비교는 같은 ext4ex1 커널 안의 정책 off/on이다. 정책과 무관한 공통 계측 코드·READ_ONCE 보정은 양쪽에 존재한다. leaf는 별도 B0 원본 커널도 측정하되, 다른 부팅의 값을 같은 회차 짝으로 계산하지 않는다. inline_data는 ext4의 기존 feature 설정 비교이며 새 커널 기능을 만들었다고 주장하지 않는다.

## 캐시 이름

- leaf warmdir: guest drop_caches=2 다음 readdir. 각 이름을 한 번씩 조회하며 경로 dcache가 준비되지 않은 조건. 디렉터리 데이터와 VM/host 캐시가 모두 비었다는 뜻은 아니다.
- leaf warm: 같은 질의 목록을 사전 실행. 별도 profile에서 tail 후보 비교 횟수0을 확인한다.
- ES es-warm/es-cold: EXT4_IOC_CLEAR_ES_CACHE로 ES tree만 비운 뒤 written span마다 사전 SEEK_DATA를 할지의 차이. 페이지 캐시와 디스크 캐시 상태 이름이 아니다.
- inline cold_guest: 프로세스와 버퍼 준비 뒤 guest drop_caches=3. host/VirtualBox 캐시와 실제 하드웨어 cold를 통제하지 않았다.
- pread 대조: 데이터는 공통 buffered warm 후 ES cache만 재설정한다. buffered는 페이지 캐시, O_DIRECT는 guest 페이지 캐시를 우회하지만 host/storage cache를 우회한다고 주장하지 않는다.

## 계측과 시간

시간 측정에서는 모든 profile을 끈다. 별도 계측은 수정 코드가 실제 실행됐는지와 비교/방문 수를 보여준다. 이 수치를 CPU 시간 비중, 물리 I/O 수, 전체 프로그램 속도 향상으로 바꾸지 않는다. 이름 비교 hit/miss별 값은 50:50 혼합 작업 안의 조건부 통계이며 순수 hit-only/miss-only 작업 부하 결과가 아니다.

## inline 저장량

실제 inline flag를 모든 파일에서 확인한다. st_blocks는 이 커널에서 inline 파일에 ceil(size/512)를 더해 보고하므로, stat에 나타난 512바이트를 독립 데이터 블록의 실제 할당으로 해석하지 않는다. 파일시스템 free-block 변화는 디렉터리 확장을 포함하고 이미 확보된 inode table을 제외한다. 두 지표를 구분한다.

## 선별과 확인

1차 실험의 끝난 leaf 자료를 압축·전송하는 작업이 ES 후반/inline 일부와 겹쳤다. 선별 자료는 삭제하지 않고 그대로 보존한다. 최종 확인은 새 부팅·질의 순서 seed에서 수행하며 측정 중 원자료 전송·압축·컴파일을 하지 않는다. 각 벤치마크 자체가 시간 구간 후 저장하는 원자료는 동일하게 유지한다.

범위는 단일 VM, Linux 5.4.214, GCC 9.4.0, 주 성능시험 단일 프로세스·단일 CPU 고정이다. 암호화 및 casefold 이름은 선행 필터에서 제외하지만 기능별 실파일 시스템 실험을 별도로 수행한 것은 아니다. 다중 reader는 ES 정확성에서만 검사한다. 모든 ext4 작업에 일반화하거나 최신 커널 개선의 신규성·상류 반영 가능성을 입증했다고 주장하지 않는다.
