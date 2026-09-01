# Median/rankfit row-tiling results - 2026-09-01

## Purpose

The production median/rank-fit path previously required a `float32` cube for
all accepted frames and the full output footprint. This validation checks the
new two-stage, full-width row-tile implementation for exact output equivalence,
bounded memory, fallback safety, and the expected speed/RAM trade-off.

The real-data runs used the 220P/McNaught RGB Seestar sequence at 1920x1080.
Both the star-aligned and Metcalf-aligned products were built at the same time.

## Exact five-frame comparison

| Tile rows | Tiles | Stack wall | Peak RSS | Result |
| ---: | ---: | ---: | ---: | --- |
| 128 | 15 | 19.862 s | 775,741,440 bytes (740 MiB) | success |
| 1920 | 1 | 8.565 s | 895,336,448 bytes (854 MiB) | success |

The star and Metcalf FITS from both runs were bit-for-bit identical:

- maximum absolute pixel difference: `0 ADU`
- changed pixels: `0`

Smaller tiles reduce peak RAM but reread each FITS more often, so the slower
128-row result is expected.

## 247-frame automatic plan

`--median-tile-rows auto` observed 9,406,525,440 available bytes (8.76 GiB) and
selected 720 rows, producing three tiles. Its plan was:

- accepted frames: 247
- channels: 3
- simultaneous cubes: 2
- bytes per output row: 6,428,160
- cube budget: 4,703,262,720 bytes
- selected working cubes: 4,628,275,200 bytes (4.31 GiB)
- tile-allocation fallbacks: 0
- worker-allocation fallbacks: 0

The run completed with:

| Measurement | Result |
| --- | ---: |
| Stack wall time | 215.547 s |
| End-to-end pipeline wall time | 268.475 s |
| Python peak RSS | 5,351,010,304 bytes (4.98 GiB) |
| Full-screen two-cube requirement avoided | about 11.5 GiB |

The working-cube budget deliberately covers only order-statistic cubes and their
sample-count maps. Output arrays, workers, Python, and FITS buffers account for
the difference between 4.31 GiB of cubes and 4.98 GiB peak RSS, while roughly
half of the RAM reported as available at planning time remains outside the cube
budget for the OS and filesystem cache.

## Correctness and failure tests

Automated tests cover:

- in-memory median and rank-fit against the former disk-backed reference;
- RGB affine registration, Metcalf shift, background correction, coverage, and
  saturation masks across multiple row tiles for both median and rank-fit;
- rank-fit central-sample matrix products in a separately bounded 16 MiB
  pixel workspace rather than one tile-wide advanced-index temporary;
- exact source bottom/right edges for an identity transform;
- an injected tile allocation failure, proving the uncommitted tile is discarded
  before the same rows are retried at half height;
- automatic and explicit tile-row parsing and RAM planning.

The implementation keeps registration coordinates separate from
`StackCanvas(shape, origin_x, origin_y)`. The current output remains the reference
footprint, but the row-tiling algorithm does not require reference and output
shapes or origins to be identical.
