#!/usr/bin/env bash
set -euo pipefail
root=${1:?}
cd "$root"
[[ $(uname -r) = 5.4.214-b0 ]]
trap 'echo $? > results/run-stock.exit' EXIT
python3 scripts/wait_idle.py > results/settle-stock.log
python3 scripts/capture_environment.py > results/environment-stock.json
python3 scripts/leaf_stock.py --bench bench/lookup_bench --fixtures fixtures/leaf-mount/leaf --names results/leaf-names --output results/leaf-stock --rounds 8 --modes warmdir --drop-caches 2 --cpu 0 > results/leaf-stock.log
sync
umount fixtures/leaf-mount
e2fsck -fn fixtures/leaf.img > results/leaf-stock-fsck.log 2>&1
