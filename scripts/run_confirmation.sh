#!/usr/bin/env bash
set -euo pipefail
root=${1:?experiment directory required}
[[ $EUID = 0 && $(uname -r) = 5.4.214-ext4ex1 ]]
cd "$root"
param=/sys/module/ext4/parameters
finish() {
  code=$?
  for name in leaf_profile es_lookup_profile htree_profile leaf_prefilter es_lookup_refresh htree_search; do echo 0 > "$param/$name"; done
  echo "$code" > results/run-confirm.exit
}
trap finish EXIT
phase() { echo "$1" > results/confirm-phase; date -Is; echo "$1"; }
phase mount
mount -o loop,noatime fixtures/leaf.img fixtures/leaf-mount
mount -o loop,noatime fixtures/es.img fixtures/es-mount
phase data-correctness
python3 scripts/check_es_data.py --directory fixtures/es-mount/es-data-checks > results/es-data-correctness.json
phase settle
python3 scripts/wait_idle.py > results/settle-confirm.log
python3 scripts/capture_environment.py > results/environment-confirm.json
dmesg > results/dmesg-confirm-before.txt
phase leaf-confirm
python3 scripts/leaf_runner.py --bench bench/lookup_bench --fixtures fixtures/leaf-mount/leaf --names results/leaf-names --output results/leaf-confirm --rounds 20 --modes warmdir --seed 20261119 --drop-caches 2 --cpu 0 > results/leaf-confirm.log
phase es-confirm
python3 scripts/run_es_trials.py --bench bench/es_bench --file fixtures/es-mount/es.bin --output results/es-confirm --rounds 20 --seed 20261119 > results/es-confirm.log
phase inline-confirm
python3 scripts/run_inline.py --work-dir fixtures/inline-confirm --output results/inline-confirm --binary bench/tinyfile_bench --rounds 8 --seed 20261119 > results/inline-confirm.log
phase es-io-profile
python3 scripts/run_es_io.py --bench bench/es_io_bench --file fixtures/es-mount/es.bin --output results/es-io-profile --profile --rounds 1 > results/es-io-profile.log
phase es-io-time
python3 scripts/run_es_io.py --bench bench/es_io_bench --file fixtures/es-mount/es.bin --output results/es-io --rounds 8 > results/es-io.log
phase fsck
sync
umount fixtures/es-mount
umount fixtures/leaf-mount
e2fsck -fn fixtures/es.img > results/es-confirm-fsck.log 2>&1
e2fsck -fn fixtures/leaf.img > results/leaf-confirm-fsck.log 2>&1
dmesg > results/dmesg-confirm-after.txt
phase complete
