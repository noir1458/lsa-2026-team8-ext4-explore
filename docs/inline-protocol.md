# 기존 inline_data 기능 비교

커널 자료구조를 새로 구현하는 실험이 아니다. Linux 5.4.214 계열의 ext4에서 `inline_data` 기능을 켰을 때 작은 파일의 저장 방식과 생성·조회·확장 비용이 어떻게 달라지는지 확인한다.

- 기본값: 크기별 1,000개 파일, 32/128/512/4096 B, 조건별 8회. 64 MiB 이미지, 4 KiB 블록, 256 B inode.
- 매 round마다 두 개의 새 regular-file 이미지를 만든다. 두 파일시스템은 UUID 외에 동일 geometry와 directory hash seed를 쓰고, feature 집합의 차이가 `inline_data` 한 개뿐인지 검사한다.
- 이미지 초기화의 지연 inode/journal 초기화를 끈다. mount는 `noatime,nodiratime,data=ordered`로 고정한다.
- 같은 round의 두 조건은 같은 크기 순서·파일 생성/읽기 순서·파일 내용이다. 조건 순서를 무작위로 정하고 다음 round에 역순으로 실행한다.
- 기본 결과는 240 trial이다. 생성 4개, cold-guest 읽기 4개, warm 읽기 4개, 32→4096 B 확장 및 이후 cold/warm 읽기 3개를 두 조건에서 8회 실행한다.

생성은 `open(O_EXCL) → pwrite → close`를 모든 파일에 적용하고 마지막에 `fsync(directory) + syncfs(filesystem)`를 수행한다. 파일 작업 시간과 마지막 동기화 시간을 따로 기록하며, 주 지표는 둘을 포함한 전체 시간이다. 파일마다 fsync를 호출하는 내구성 workload와 다르다.

읽기는 파일마다 `open → read → EOF 확인 → close`를 수행한다. 데이터 버퍼와 정답은 측정 전에 준비하고, 읽어 둔 결과의 정확한 바이트 비교는 측정 후에 한다. cold-guest 조건은 자식 프로세스 준비 완료 후 guest의 `drop_caches=3`을 요청한다. 호스트·VMM 캐시가 비었다는 뜻은 아니다. warm은 같은 파일을 별도로 예열하고 정확성을 확인한 뒤 측정한다.

확장은 32 B 파일을 먼저 모두 검증한 뒤 남은 바이트를 추가한다. 따라서 **warm 확장**이다. 4096 B 결과 전체가 정답과 일치해야 하며, 확장 전후의 `FS_INLINE_DATA_FL`, `st_blocks`, 논리 크기를 모두 기록한다.

128 B가 inline에 들어갈지는 가정하지 않는다. 실제 용량은 inode의 extra size와 xattr 여유 공간의 영향을 받는다. 모든 파일에 `FS_IOC_GETFLAGS`를 적용해 실제 저장 방식을 판정한다. 32 B에서도 inline이 관측되지 않으면 실험을 실패로 표시해 같은 기능 경로를 비교한 결과로 오해하지 않게 한다.

`st_blocks`는 512 B 단위이며 미리 확보한 inode table 공간을 포함하지 않는다. **Linux 5.4.214의 `ext4_file_getattr`는 inline 파일에 `ceil(size/512)`를 더해 보고한다.** tar/rsync 등이 이를 완전히 sparse한 파일로 오해하지 않도록 한 처리다. 따라서 32/128 B inline 파일에서 관측한 `st_blocks=1`은 별도 512 B 데이터 블록 할당을 의미하지 않는다. 분석에서는 이를 `stat_reported_bytes_per_file`로 부른다. 근거: [inode.c, 5826–5841행](https://github.com/gregkh/linux/blob/v5.4.214/fs/ext4/inode.c#L5826).

파일시스템 `statvfs`도 각 trial 전후에 보존한다. `filesystem_consumed_bytes_per_file`은 그 사이 free block이 감소한 양을 파일 수로 나눈 값이다. 파일 데이터뿐 아니라 디렉터리 확장을 포함하고, mkfs에서 이미 할당한 inode table을 제외하므로 파일의 전체 물리 점유량과 다르다. `/proc/self/io` 전후 원값과 차이는 측정 구간 주변의 프로세스 I/O이며, 작은 관측 오버헤드를 포함하고 실제 장치 I/O 전체와 같지 않다.

이 기능은 디렉터리 표현에도 영향을 줄 수 있다. 따라서 결과는 기존 feature 전체를 적용한 파일 workload 비교이며, regular-file 함수 하나의 인과 효과로 제한해서 설명하지 않는다. 디렉터리 생성 자체는 타이머 밖이지만 파일 추가 중 디렉터리 전환은 생성 시간에 포함될 수 있다.

## 실행

다른 디스크 실험을 멈춘 Linux guest에서 수행한다. 기존 경로나 block device는 받지 않고, 새 work/output 디렉터리만 허용한다.

```sh
gcc -std=c11 -O2 -Wall -Wextra -Werror bench/tinyfile_bench.c -o bench/tinyfile_bench
python3 scripts/run_inline.py --work-dir fixtures/inline-run --output results/inline --binary bench/tinyfile_bench --rounds 8
python3 analysis/analyze_inline.py --results results/inline --output analysis
```

`--plan-only`는 파일·mount·캐시 변경 없이 실행 순서를 출력한다. `--keep-images`가 없으면 각 이미지가 unmount와 읽기 전용 e2fsck를 통과한 뒤 해당 이미지 파일만 제거한다. 원자료·헤더·명령·환경·검증 결과는 보존한다.

근거: `source/fs/ext4/inline.c`의 `ext4_get_max_inline_size`, `ext4_try_to_write_inline_data`, `ext4_da_write_inline_data_begin`, `ext4_da_convert_inline_data_to_extent`. mke2fs v1.45.5는 `hash_seed` extended option을 지원한다: https://github.com/tytso/e2fsprogs/blob/v1.45.5/misc/mke2fs.c#L798
