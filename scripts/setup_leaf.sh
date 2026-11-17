#!/usr/bin/env bash
set -euo pipefail
root=${1:?experiment directory required}
[[ $EUID = 0 && -d $root/scripts ]]
image="$root/fixtures/leaf.img"
mount_dir="$root/fixtures/leaf-mount"
[[ ! -e $image && ! -e $mount_dir ]] || { echo 'Fresh destinations required' >&2; exit 2; }
mkdir "$mount_dir"
truncate -s 128M "$image"
mkfs.ext4 -q -F -b 4096 -I 256 -N 55000 -E lazy_itable_init=0,lazy_journal_init=0,hash_seed=11111111-2222-3333-4444-555555555555 "$image"
mount -o loop,noatime "$image" "$mount_dir"
python3 "$root/scripts/leaf_fixture.py" --parent "$mount_dir/leaf" --output "$root/results/leaf-names" --files 10000
for name in short13 long128 random128 varied; do
  lsattr -d "$mount_dir/leaf/$name"
done > "$root/results/leaf-flags.txt"
tune2fs -l "$image" > "$root/results/leaf-filesystem.txt"
sync
