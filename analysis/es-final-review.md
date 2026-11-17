# Independent ES review

All numbers below were cross-checked by recomputing same-round ratios directly
from the raw `summary.csv` files, separately from the analysis report. A
negative percentage means less elapsed time per query, not that throughput
increased by the same percentage. Discovery and confirmation remain separate.

## Validation and scope

- ES metadata: 144 discovery timing trials + 360 confirmation timing trials,
  and 12 separate profile trials; all planned cells present, all PASS.
- Data-read control: 96 timing + 8 separate profile trials; all planned cells
  present, all PASS.
- Timing profiling gates were off and counters were zero. Profile times were
  excluded from the performance comparisons.
- Unique-page-payload correctness: all 12 stages passed, including buffered
  reads after `fsync`/`FADV_DONTNEED`, aligned `O_DIRECT` reads, holes, punch and
  rewrite, truncate, extend, and restoration. The earlier two-reader and seek
  geometry checks also passed. These are scoped functional checks, not a proof
  for all concurrent filesystem operations.

The measured policy comparison is refresh off versus on in the same modified
kernel. Common instrumentation gates and the upstream `READ_ONCE`/`WRITE_ONCE`
safety correction exist in both conditions. This is not an exact overhead
comparison against an unmodified kernel.

## Metadata confirmation

| Pattern | ES initial state | Refresh vs baseline: median time change | Faster pairs | Refresh vs shadow: median time change | Faster pairs |
|---|---|---:|---:|---:|---:|
| hot | warm | −5.984% | 20/20 | −5.944% | 20/20 |
| sequential | warm | −5.453% | 20/20 | −5.973% | 20/20 |
| random | warm | +0.890% | 3/20 | +1.054% | 3/20 |
| hot | cold | +0.021% | 10/20 | +0.007% | 10/20 |
| sequential | cold | −5.161% | 20/20 | −5.347% | 20/20 |
| random | cold | +0.170% | 8/20 | +0.256% | 8/20 |

The approximately 5–6% time reductions for locality-rich metadata queries
repeat in another boot and survive changing the identical-policy baseline
label. The two references share trials and are not independent studies.
Random queries do not improve; warm random queries show a small adverse
direction, consistent with frequent hint writes without a better hit rate.

Separate profiling explains the pattern. In warm hot queries, baseline visits
9 RB nodes per query and has no last-extent pointer hits; refresh reaches
99.9995% pointer hits. Warm sequential hits reach 93.75%, exactly matching
15 reused pages out of each 16-page written span. Random pointer hits stay
near 0.1%. Cold hot is a useful negative control: the first miss inserts the
needed extent and already leaves the baseline hint at the hot extent, so both
policies subsequently take the shortcut.

“ES cold” means the extent-status entries were explicitly cleared; file page
cache, metadata buffers, host cache and physical storage were not made cold.

## File-data read control

| 4 KiB pread condition | Refresh vs baseline: median time change | Refresh vs shadow: median time change |
|---|---:|---:|
| buffered, hot | +1.134% | +1.209% |
| buffered, random | −0.652% | +0.128% |
| O_DIRECT, hot | −0.377% | −0.022% |
| O_DIRECT, random | +0.433% | −0.477% |

This short control does not establish an end-to-end file-read improvement.
Buffered warm reads make zero ES lookups, making their policy-label differences
a negative control. Direct reads make one ES lookup per query. In direct hot
reads, refresh reduces RB-node visits from 9 to 0.00225 per query, but the
complete pread-loop timing has no stable advantage over the same-policy
shadow. Device/host caches are uncontrolled; this is not a physical cold-disk
throughput experiment.

The performance fixture repeats one known payload on every page. Its full-byte
checks detect short reads, zeros and corruption, but cannot distinguish two
wrongly swapped written pages. That limitation is covered separately by the
unique-page-payload correctness fixture, not by claiming that the performance
fixture itself verifies every physical mapping.

## Supported conclusion

Updating the existing one-entry ES hint after successful tree lookup is a
demonstrated, workload-dependent improvement for these synthetic `SEEK_DATA`
metadata queries. It provides a concrete locality and cache-policy project,
including a clear regression/no-benefit workload. The measured result should
not be expanded to “all ext4 operations” or “file reading is 6% faster.”

Figures regenerated from the final data and visually inspected:

- `analysis/es/fig2_es_timing_es-confirm.png`
- `analysis/es/fig3_es_profile_es-profile.png`
- `analysis/es-io/fig4_es_io_es-io.png`
