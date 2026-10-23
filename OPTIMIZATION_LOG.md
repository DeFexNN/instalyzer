# Optimization log

## Analyzer media settings

Selected 768 px maximum image edge, up to three evenly spaced carousel images, and a 1024-token per-post output cap. This keeps several visual samples for carousels while materially reducing request payload.

The five-post benchmark compared 25 requests across five settings. At 768 px with three media samples, aggregate input tokens fell 57.3%, encoded image payload fell 58%, measured request wall time fell 41.8%, and estimated cost fell 43.3% versus the 1280 px baseline. Estimated cost was about $0.000973 for that five-post scenario. This is a small sample, so timing is directional and analysis quality was not formally scored. The 512-token output cap did not show a dependable speed improvement and saved little estimated cost; the default remains 1024.

Detailed measurements are in [the benchmark report](exports/instagram-yaroslav_kazo-20261021-220341/optimization_benchmark_20261021T205220Z.md).

## Scraper media concurrency

Added bounded processing across posts: four download workers by default, configurable from one to six with `IG_MEDIA_DOWNLOAD_WORKERS`. The returned manifest retains profile order. Video compression runs under a separate two-job limit so parallel downloads do not start an unbounded number of FFmpeg encodes.

The local unit test uses artificial download delays to verify stable output order and enforce the worker ceiling. It measures concurrency correctness, not Instagram throughput. A real profile rerun is needed before claiming a wall-time improvement because CDN throttling, network conditions, and video mix affect the result.

### Local checks on 2026-10-22

- `python -m unittest discover -s tests -v`: 4 tests passed in 0.073 s. They confirmed profile-order preservation, the six-worker ceiling, one retry for transient timeouts, and no retry for permanent HTTP 403.
- `python -m py_compile scrape_instagram.py analyze_instagram_profile.py benchmark_optimizations.py media_processing.py compact_instagram_export.py`: passed.
- Caption-length inspection of the existing 50-post export: median 221 characters, 90th percentile 487, maximum 1,005; no caption exceeded 1,500 characters. A truncation cap would not help this sample, so none was added.
- Re-encoding the 41 existing photos at JPEG quality 80 and 768 px would reduce their stored size from 6.72 MB to about 2.83 MB (57.8%). Those photos are only about 5% of this export's 130.8 MB of media, while downscaling would permanently reduce archive detail. The analyzer already uploads 768 px versions, so the scraper keeps its 1280 px archive setting.
- A real 50-post scrape with four download workers completed in about 108 s and collected 82 media entries. It saved 81 media files; one CDN image read timed out. Across the manifest, final media was 129.1 MB versus 368.1 MB before successful compression (64.9% smaller). The run exposed transient timeout handling, so the scraper now retries a timeout/rate-limit/server error once. A second full 50-post run was not made just to re-fetch the same 129 MB; retry behavior is verified with local tests.

### Retry verification after the live run

- The retry tests and syntax check pass. The one failed CDN image was observed before adding this retry and was not re-fetched in a second full run.

## Next candidates, not yet adopted

- Compare 4 vs. 6 analyzer requests per second-independent run on randomized posts; current five-post benchmark used concurrency five and does not establish a safe best concurrency.
- Compare two posts per model request. It may lower request overhead, but needs a quality check for mixing evidence between posts and a paid benchmark.
- Measure a low-detail image input option against the current 768 px images. Provider support and quality impact need verification before changing defaults.
- Consider caching prepared video frames and normalized image bytes only if repeat/no-resume runs show local media preparation is a meaningful portion of wall time; model requests currently dominate recorded timings.
