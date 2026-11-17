# 구현과 해석 근거

- Linux 5.4 ext4 공식 문서: https://www.kernel.org/doc/html/v5.4/admin-guide/ext4.html
- ext4 저장 구조: https://www.kernel.org/doc/html/v5.4/filesystems/ext4/overview.html
- inline data: https://www.kernel.org/doc/html/v5.4/filesystems/ext4/dynamic.html#inline-data
- extents_status.c 원본: https://git.kernel.org/pub/scm/linux/kernel/git/stable/linux.git/tree/fs/ext4/extents_status.c?h=v5.4.214
- namei.c 원본: https://git.kernel.org/pub/scm/linux/kernel/git/stable/linux.git/tree/fs/ext4/namei.c?h=v5.4.214
- ES cache concurrent read 보정(upstream): https://github.com/torvalds/linux/commit/492888df0c7b42fc0843631168b0021bc4caee84

이번 ES 실험의 READ_ONCE/WRITE_ONCE 보정은 기존 upstream 수정에 근거한다. 우리 비교 후보는 조회 성공 후 최근 extent 힌트를 갱신하는 정책이며, 이를 발견된 correctness 버그나 새로운 자료구조 발명이라고 주장하지 않는다.
