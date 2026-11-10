# ALS map embed / vector-index budget (Raider)

Measured on host `austen-loren-strine-Raider-GE68HX-13VF` at **2026-11-10T05:48:12Z**.

Index target path (preferred): `/home/austen-loren-strine/Development/als_graph/data/`  
Filesystem: `/` on `/dev/nvme0n1p4` (ext4).

## Disk

| Field | Value |
| --- | --- |
| `free_bytes_now` | **93519347712** (~87.10 GiB) |
| Disk hard limit `floor(0.80 * free_bytes_now)` | **74815478169** (~69.68 GiB) |

Do not grow the vector index (or related embed artifacts under this filesystem) past **74815478169** bytes without warning Austen first.

## RAM (guidance only)

| Field | Value |
| --- | --- |
| `MemAvailable` | **55320520** kB = **56648212480** bytes (~52.76 GiB) |
| RAM guidance `floor(0.50 * MemAvailable_bytes)` | **28324106240** (~26.38 GiB) |

This RAM figure is guidance for the embedding process working set. It is **not** a pre-allocation target.

## Operating rules

- **Warn Austen before exceeding** the disk hard limit above.
- **Do not pre-claim** the full disk or RAM budget (no huge sparse files, no forced memory lock to the cap).
- Re-measure before a large embed run if free space or `MemAvailable` may have changed.
