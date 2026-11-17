# 재현 방법

Linux 5.4.214와 GCC 9.4.0의 전용 Ubuntu VM을 사용했다. 원자료를 덮어쓰지 않도록 새 작업 디렉터리에서 실행한다. root 권한이 필요한 단계는 실험 VM 안에서만 수행한다. 기존 디스크를 포맷하는 명령은 사용하지 않는다.

## 커널

공식 Linux 5.4.214 소스의 새 사본에 아래 순서로 패치를 적용한다. 커널 소스는 이 저장소에 포함하지 않는다. 설정은 `results/config-5.4.214-ext4ex1`을 참고하되 다른 장비에서는 해당 장비의 부팅 가능한 설정을 사용한다.

```sh
kernel_source=/path/to/fresh-linux-5.4.214
experiment_repo=/path/to/lsa-2026-team8-ext4-explore
cd "$kernel_source"
for patch_name in 0001-htree-common-instrumentation.patch 0002-dirent-tail-prefilter.patch 0003-es-lookup-refresh.patch; do
  patch -p1 < "$experiment_repo/patches/$patch_name"
done
cp "$experiment_repo/results/config-5.4.214-ext4ex1" .config
make olddefconfig
make -j2 bzImage modules
sudo make modules_install INSTALL_MOD_STRIP=1
sudo make install
```

기존 HTree 계측 코드는 공통 기반이다. 이번 실험에서 `htree_search=0`(binary), `htree_profile=0`을 유지한다. 같은 커널에서 leaf/ES 후보만 각각 켜고 끈다. ES의 기존 upstream READ_ONCE/WRITE_ONCE 보정도 두 정책에 공통 적용한다. 이 기준은 완전 무수정 커널과 같다고 주장하지 않는다.

GRUB에서 실제 항목 ID를 확인해 `grub-reboot`으로 새 커널을 한 번 부팅한다. 원래 기본 항목은 보존한다. `uname -r`로 `5.4.214-ext4ex1`인지 확인한다.

## 새 측정 디렉터리

```sh
cd "$experiment_repo"
make -C bench
python3 scripts/check_leaf.py
experiment_dir="$HOME/ext4-extra-new-run"
mkdir "$experiment_dir"
cp -R bench scripts patches "$experiment_dir/"
mkdir "$experiment_dir/results" "$experiment_dir/fixtures"
sudo bash "$experiment_dir/scripts/setup_leaf.sh" "$experiment_dir"
sudo bash "$experiment_dir/scripts/run_all.sh" "$experiment_dir"
```

`setup_leaf.sh`는 새 일반 파일 이미지 128 MiB만 포맷하며, 이름 패턴 네 종류에 각각 10,000개 파일을 만든다. `run_all.sh`는 extent 실험용 새 256 MiB 이미지, 정확성 검사, 부팅 안정화(240초 이상 및 부하 <0.6), 별도 계측, 시간 측정, inline_data 대조 실험, unmount 후 `e2fsck -fn` 검사를 수행한다. 완료 상태는 `results/run-all.exit`와 `current-phase`에 기록한다.

중간 실패 시 기존 디렉터리 위에 전체 스크립트를 재실행하지 않는다. 오류 로그와 완료 단계를 확인한 뒤 실패 단계부터 새 출력 디렉터리로 이어서 실행한다. 계측은 시간 측정에서 끄며 각 runner가 종료 시 정책을 복원한다.

## 지표

- leaf: 질의 전체 루프 `elapsed_ns / queries`. 이름 존재 여부와 checksum, 질의별 latency 원자료를 검증한다. warmdir는 guest dcache 정리 후 디렉터리 열거이며 물리 디스크 cold가 아니다.
- ES: 알려진 written extent 안의 `SEEK_DATA` 전체 루프 시간. 반환 offset이 요청 offset과 같은지 매번 확인한다. ES cache만 ioctl로 초기화하므로 es-cold는 페이지 캐시·디스크 cold와 다르다.
- inline: 파일 생성/읽기/확장 시간. 생성·확장은 마지막 directory fsync + syncfs를 포함한 시간과 파일 작업 시간을 따로 기록한다. 실제 inode inline flag와 파일별 st_blocks를 확인한다. st_blocks=0은 inode 저장 공간까지 0이라는 뜻이 아니다.

정책 효과는 같은 round의 변경/기준 비율을 먼저 구하고 중앙값·최소·최대·빠른 횟수를 집계한다. 서로 다른 세션을 합치지 않는다. shadow는 기준과 동일한 코드·설정으로 실행한 시간 변동 대조군이다.

## 분석

Python 3와 Matplotlib이 필요하다. 새 실험 자료에 대해:

```sh
python3 analysis/analyze_leaf.py --input "$experiment_dir/results/leaf-time" --profiles "$experiment_dir/results/leaf-profile" --output "$experiment_dir/analysis/leaf"
python3 analysis/analyze_es.py --input "$experiment_dir/results" --output "$experiment_dir/analysis/es"
python3 analysis/plot_es.py --input "$experiment_dir/analysis/es" --output "$experiment_dir/analysis/es"
python3 analysis/analyze_inline.py --results "$experiment_dir/results/inline" --output "$experiment_dir/analysis/inline"
```

완료한 실험은 일반 파일 이미지 두 개를 분리한 뒤 원래 커널로 다시 부팅한다. 이미지는 재검증을 위해 남겨둘 수 있으며, inline runner는 자기가 만든 개별 이미지에 대해 fsck가 성공한 경우만 정리한다.

## 원본 B0 및 독립 확인

B0는 패치를 적용하지 않은 별도 Linux 5.4.214 소스에서 같은 설정을 사용하되 CONFIG_LOCALVERSION만 -b0로 바꿔 빌드한다. 제공한 config-diff 기록에는 이름 한 줄만 다르다. B0로 한 번 부팅한 다음 기존 leaf 이미지를 다시 마운트하고 실행한다.

```sh
experiment_dir="$HOME/ext4-extra-new-run"
sudo mount -o loop,noatime "$experiment_dir/fixtures/leaf.img" "$experiment_dir/fixtures/leaf-mount"
sudo bash "$experiment_dir/scripts/run_stock.sh" "$experiment_dir"
```

이후 ext4ex1로 한 번 더 부팅해 확인 세션을 실행한다. 이 명령은 첫 실행 결과를 덮어쓰지 않고 별도 confirm/stock 출력에 기록한다. 동일한 image·이름 목록에서 새 실행/질의 순서 seed를 사용하며, inline 비교는 새 이미지를 만든다. 확인 세션이 끝날 때까지 원자료 전송이나 다른 실험을 함께 돌리지 않는다.

```sh
experiment_dir="$HOME/ext4-extra-new-run"
sudo bash "$experiment_dir/scripts/run_confirmation.sh" "$experiment_dir"
```

SEEK_DATA 반환값 외에 mapping 변경 후 실제 데이터를 검증하려고 page별 고유 payload를 넣은 별도 파일에 buffered·O_DIRECT 읽기를 모두 수행한다. 추가 pread 성능 대조는 이 정확성 검증과도 구분한다.

```sh
python3 analysis/analyze_leaf.py --input "$experiment_dir/results/leaf-stock" --output "$experiment_dir/analysis/leaf-stock"
python3 analysis/analyze_leaf.py --input "$experiment_dir/results/leaf-confirm" --profiles "$experiment_dir/results/leaf-profile" --output "$experiment_dir/analysis/leaf-confirm"
python3 analysis/analyze_es.py --input "$experiment_dir/results" --output "$experiment_dir/analysis/es"
python3 analysis/analyze_inline.py --results "$experiment_dir/results/inline-confirm" --output "$experiment_dir/analysis/inline-confirm"
python3 analysis/analyze_es_io.py --input "$experiment_dir/results" --output "$experiment_dir/analysis/es-io"
```
