# Apify integration — 2026-10-24

## Implementation

- Provider: official `apify/instagram-scraper`, Actor ID `shu8hvrXbJbY3Eb9W`, live build `0.0.830`.
- Credentials: local `APIFY_API_TOKEN` or Streamlit Secrets, sent only in the Authorization header to `api.apify.com`. No Instagram session cookies are forwarded; provider redirects are refused. The integration uses the standard library without adding dependencies.
- CLI and Streamlit share the compatible export/analyzer pipeline. The configured token selects Apify in `auto` mode. Explicit `apify` mode requires the token and cannot silently fall back to Instagram credentials. The existing model key, web password, queue and Contributor-training consent remain in effect.
- Collection: one posts run (limit up to 100), one inexpensive profile-details run for biography/counts, optional one batch comments run for incomplete threads. Carousels preserve their available children; videos retain the provided video source. Post dates determine returned-post ordering. Speaker roles, comment dates and available reply context are preserved.
- Persistent run IDs and response/media caches avoid starting paid work again on replay. Status-poll interruptions can resume the existing run; an uncertain run-start response is blocked from automatic retry. Saved comments survive a later preview-mode replay. Web cancellation requests remote abort before terminating the local worker.
- Each export's Actor-run budget defaults to $0.50. Runs are sequential and receive only the remaining cap. Live observation confirmed `usageTotalUsd` and charged result counters can lag behind `SUCCEEDED`; billing settlement is checked before allocating another run. Unknown costs block additional paid runs instead of being treated as free. Optional collection errors stay visible in the manifest.

## Live collection result

Target: the same public profile used in the earlier provider tests. Requested up to 100 posts; profile metadata advertised 65.

| Field | Observed result |
| --- | --- |
| Returned unique posts | 37 |
| Posts with source publication dates | 37 |
| Saved media | 65 files: 57 images and 8 videos |
| Saved media size | 35,004,053 bytes |
| Download errors | 0 |
| Comment texts collected | 52 |
| Threads meeting the reported comment count | 19 |
| Threads with limited comments | 18 |
| Full advertised timeline verified | No — 37 returned versus 65 advertised |

All 57 photos passed Pillow verification. FFprobe confirmed a video stream in all eight saved videos. All twelve posts from the previous provider's first batch were present in the Apify result. The provider did not return the remaining advertised posts; a successful Actor run is not treated as proof of complete collection.

The additional comments run returned 44 records, all already present in the original previews. Deduplication kept the export at 52 comments. The reported comment counts sum to 96; this difference remains a coverage limitation, not a claim that the other comments do not exist. Full mode did not improve this test and remains optional; preview mode is the economical default.

## Observed Actor costs

| Run | Results | Settled `usageTotalUsd` |
| --- | ---: | ---: |
| Posts | 37 | $0.0999 |
| Profile | 1 | $0.0027 |
| Additional comments | 44 | $0.1188 |
| Total including the comments experiment | | **$0.2214** |
| Posts + profile with bundled comment previews | | **$0.1026** |

These are authenticated run charges reported by Apify, not a projection for a full 100-post profile. Early reads showed $0.0837 for the posts run before final charges reached $0.0999; the integration now waits for settlement. Separate dataset access or other account usage is not inferred from these run totals. No model requests were made for this integration test.

The posts Actor took 30.617 seconds. A separate export pass reused its dataset, collected profile metadata and downloaded all media in 55.025 seconds. The optional comments pass with cached media took 59.293 seconds. These separately observed phases are not a fresh full-pipeline benchmark. A fully cached replay took 0.011 seconds in-process, with the API method patched to reject any request: zero network calls and zero new Actor runs.

Local outputs: ignored `.runtime/apify/bohdankryuk/posts.json`, `media/`, `.apify/runs.json`, and `verification.json`.

## Validation

The complete local workspace suite passed **101 tests in 2.249 seconds**. Syntax compilation and `git diff --check` passed. Tests cover Actor/dataset reuse, restart/resume behavior, uncertain start protection, delayed billing, budgets, owner/reply context, carousel/video conversion, media reuse, preservation of earlier comments, provider-specific configuration, combined costs, invalid-setting UI rendering, and the web pipeline with a mocked model. These automated tests use fictional records and mocks; the separate live collection and media checks are described above.

Primary references:

- [Actor README](https://apify.com/apify/instagram-scraper)
- [Actor input schema](https://apify.com/apify/instagram-scraper/input-schema)
- [Run Actor and spending caps](https://docs.apify.com/api/v2/actors-runs-post)
- [Run cost fields](https://docs.apify.com/api/client/js/reference/next/interface/Run)
