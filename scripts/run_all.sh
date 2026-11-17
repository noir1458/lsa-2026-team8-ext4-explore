#!/usr/bin/env bash
set -euo pipefail
root=${1:?experiment directory required}
[[ $EUID = 0 && $(uname -r) = 5.4.214-ext4ex1 ]]
cd "$root"
param=/sys/module/ext4/parameters
finish() {
  code=$?
  for name in leaf_profile es_lookup_profile htree_profile leaf_prefilter es_lookup_refresh htree_search; do echo 0 > "$param/$name"; done
  echo "$code" > results/run-all.exit
}
trap finish EXIT
phase() { echo "$1" > results/current-phase; date -Is; echo "$1"; }
phase prepare-es
[[ ! -e fixtures/es.img ]]
truncate -s 256M fixtures/es.img
mkfs.ext4 -q -F -b 4096 -I 256 -E lazy_itable_init=0,lazy_journal_init=0 fixtures/es.img
mkdir fixtures/es-mount
mount -o loop,noatime fixtures/es.img fixtures/es-mount
python3 scripts/create_es_fixture.py fixtures/es-mount/es.bin > results/es-fixture.json
filefrag -v fixtures/es-mount/es.bin > results/es-filefrag.txt
phase correctness
python3 scripts/check_leaf_mutations.py --root fixtures/leaf-mount --require-index --drop-caches 2 --output results/leaf-mutations.json > results/leaf-mutations.log
python3 scripts/check_es_correctness.py --bench bench/es_bench --directory fixtures/es-mount/checks > results/es-correctness.json
phase settle
python3 scripts/wait_idle.py > results/settle.log
python3 scripts/capture_environment.py > results/environment.json
dmesg > results/dmesg-before.txt
phase leaf-profile
python3 scripts/leaf_runner.py --bench bench/lookup_bench --fixtures fixtures/leaf-mount/leaf --names results/leaf-names --output results/leaf-profile --profile --rounds 1 --drop-caches 2 --cpu 0 > results/leaf-profile.log
phase es-profile
python3 scripts/run_es_trials.py --bench bench/es_bench --file fixtures/es-mount/es.bin --output results/es-profile --profile --rounds 1 --labels baseline,refresh > results/es-profile.log
phase leaf-time
python3 scripts/leaf_runner.py --bench bench/lookup_bench --fixtures fixtures/leaf-mount/leaf --names results/leaf-names --output results/leaf-time --rounds 10 --drop-caches 2 --cpu 0 > results/leaf-time.log
phase es-time
python3 scripts/run_es_trials.py --bench bench/es_bench --file fixtures/es-mount/es.bin --output results/es-time --rounds 8 > results/es-time.log
phase inline
python3 scripts/run_inline.py --work-dir fixtures/inline-run --output results/inline --binary bench/tinyfile_bench --rounds 8 > results/inline.log
phase fsck
sync
umount fixtures/es-mount
umount fixtures/leaf-mount
e2fsck -f -n fixtures/es.img > results/es-fsck.log 2>&1
e2fsck -f -n fixtures/leaf.img > results/leaf-fsck.log 2>&1
dmesg > results/dmesg-after.txt
phase complete
