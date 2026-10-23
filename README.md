# Instalyzer

Streamlit-застосунок для аналізу тем, інтересів і видимих занять у Instagram-профілі. Український інтерфейс, реальний прогрес обробки, історія сесії, перегляд матеріалів і завантаження підсумкового Markdown-звіту. У публічному демо є позначений приклад вигаданого профілю; реальні запуски захищені кодом доступу.

**Streamlit Cloud:** гілка `main`, головний файл `streamlit_app.py`. Детальна інструкція та Secrets: [DEPLOY.md](DEPLOY.md).

```powershell
python -m pip install -r requirements.txt
python -m streamlit run streamlit_app.py
```

Нижче — документація окремих CLI-скриптів, які використовує вебінтерфейс.

## Instagram export

Exports up to 100 posts from the selected Instagram profile, including captions, dates, links, and local copies of photos/videos. Carousel posts are saved as multiple media files. `IG_USERNAME` identifies the account represented by the local session cookies; the requested profile can be another profile visible to that signed-in account.

## Configure cookies

Put only each cookie's value (the text after `=`), without the cookie name or semicolon, into the local `.env` file. `IG_SESSIONID`, `IG_CSRFTOKEN`, and `IG_DS_USER_ID` are required. `csrftoken` and `datr` alone do not sign in an account. Other fields are optional and may be left blank if they are not present:

- `IG_CSRFTOKEN`
- `IG_DS_USER_ID`
- `IG_MID`
- `IG_IG_DID`
- `IG_RUR`
- `IG_DATR`
- `IG_DPR`
- `IG_IG_NRCB`
- `IG_PS_L`
- `IG_PS_N`
- `IG_RD_CHALLENGE`
- `IG_WD`

These are browser session credentials. Keep `.env` on your computer and never paste the values into chat or screenshots. The file is ignored by Git. Playwright imports them into a headless Chromium context for `instagram.com`; media downloads use separate requests without cookies.

Comments are enabled by default (the web pipeline always enables them). Up to 100 comments per post are collected, including available preview replies; full reply threads are not fetched. Set `IG_MAX_COMMENTS_PER_POST` from 1 to 500 to change collection limits. CLI users can opt out with `IG_INCLUDE_COMMENTS=false`. Collection uses the active Playwright session before its browser closes. Restricted threads are recorded as unavailable; a rate limit stops further comment requests without discarding the posts.

The web pipeline checks comment collection before sending material to the model. Resuming an older export without comments automatically collects its missing threads while preserving downloaded media. Empty/restricted threads that were already checked are not fetched again. For an existing CLI export, run `python scrape_instagram.py --collect-comments exports/EXPORT_DIRECTORY`, then run the analyzer normally (not `--synthesize-only`) to include the new evidence. Completed old reports remain unchanged.

This local exporter uses headless Playwright to load the profile and collect its GraphQL responses. These web endpoints are not a documented public scraping API and may change. Session cookies can expire or be revoked.

Downloaded photos are normalized to JPEG quality 80 and at most 1280 px on the long edge when that makes the file smaller. Video variants are selected at up to 720 px, then optionally transcoded with FFmpeg to H.264 CRF 28; the transcoded file replaces the download only when it is smaller. Install FFmpeg and make sure it is on `PATH` to enable video compression. If FFmpeg is unavailable, the selected source video is kept.

The web app defaults to `APP_LOW_CPU_MODE=true`: it disables full video transcoding, limits FFmpeg to one thread and media preparation to one file at a time, and caps download workers at two. The selected source videos are kept, so disk use can increase; the analyzer still extracts one frame per selected video. API requests remain independently concurrent. CLI video compression is enabled by default and can be disabled with `IG_COMPRESS_VIDEOS=false`; `FFMPEG_THREADS` (1–4) and `OPENCODE_MEDIA_PREPARATION_WORKERS` (1–4) control its CPU parallelism.

Media for different posts downloads concurrently (4 workers by default), while output order remains the same as the profile. Set `IG_MEDIA_DOWNLOAD_WORKERS` to 1–6 to tune concurrency; video transcoding is capped at two simultaneous FFmpeg jobs to limit CPU and memory pressure. A transient timeout, rate limit, or server error gets one retry. Set the worker count to `1` to disable parallel downloading.

## Run

From PowerShell in `C:\instalyzer`:

```powershell
python -m pip install -r requirements.txt
python -m playwright install chromium
python .\scrape_instagram.py 'https://www.instagram.com/your_username/'
```

To collect another profile visible to the signed-in account, pass that profile URL in the same way:

```powershell
python .\scrape_instagram.py 'https://www.instagram.com/example_profile/'
```

You can also pass a username (`your_username` or `@your_username`). With no argument, the program prompts for a profile URL or username. To save captions and post metadata without downloading media:

```powershell
python .\scrape_instagram.py 'https://www.instagram.com/your_username/' --no-download
```

Each run creates `exports\instagram-<username>-<timestamp>\` containing `posts.json` and a `media\` folder. At most 100 posts are collected per run. The JSON stores captions, timestamps, permalinks, media types, and local media filenames; signed media URLs and cookies are not saved.

To compact media in an export created earlier (lossy; a smaller result replaces the original file), run:

```powershell
python .\compact_instagram_export.py '.\exports\instagram-example_profile-YYYYMMDD-HHMMSS'
```

The command updates `posts.json` after each successful file and skips files already marked as optimized.

## AI analysis of an export

Set `OPENCODE_API_KEY` in the local `.env` to a **freshly rotated** OpenCode Go key. The analyzer uses `muse-spark-1.3-contributor` through OpenCode Go's Responses API. It analyzes at most 100 posts, sending each post's caption, photos, available comments, and one extracted frame per video to OpenCode/Meta. It writes `profile_analysis.md` with only consolidated conclusions (profile portrait, professional content, possible hobbies, explicitly described family context, comment evidence, other details, limits, and summary), while `profile_analysis.json` retains the individual post analyses. The summary can include more detail when the evidence supports it, but does not enumerate posts or show their IDs.

OpenCode documents that the Contributor model may use prompts and completions to train future Meta models and does not offer zero data retention. The first run is blocked unless you pass `--allow-contributor-training` to acknowledge this. To analyze the newest export:

```powershell
python .\analyze_instagram_profile.py --allow-contributor-training
```

Or provide a specific export directory:

```powershell
python .\analyze_instagram_profile.py '.\exports\instagram-your_username-YYYYMMDD-HHMMSS' --allow-contributor-training
```

To refresh only the combined summary from previously saved post analyses without resending the media:

```powershell
python .\analyze_instagram_profile.py '.\exports\instagram-your_username-YYYYMMDD-HHMMSS' --synthesize-only --allow-contributor-training
```

Post analyses use up to six concurrent model requests by default (configurable from 1 to 8), and send at most six evenly spaced carousel items per post. Each photo sent to the model is normalized to JPEG quality 80 at up to 768 px by default; `OPENCODE_IMAGE_MAX_EDGE=1280` restores the higher-resolution option. Video analysis uses one 720 px frame. Per-post analyses use `minimal` reasoning by default and fall back to `low` if the API rejects that setting. The final synthesis uses `low`. These defaults can be changed with `OPENCODE_MAX_CONCURRENT_POST_ANALYSES`, `OPENCODE_MAX_MEDIA_PER_POST`, `OPENCODE_IMAGE_MAX_EDGE`, `OPENCODE_POST_MAX_OUTPUT_TOKENS`, `OPENCODE_POST_REASONING_EFFORT`, and `OPENCODE_SYNTHESIS_REASONING_EFFORT` in `.env`.

Completed post analyses are checkpointed in the export directory and resumed on the next run when the export and post-analysis settings still match. Pass `--no-resume` to force a fresh analysis. HTTP 429 responses are retried with backoff. The analyzer saves per-request token counts and durations, plus total usage and wall-clock timings, in `profile_analysis.json`; the Markdown report stays focused on profile conclusions. If OpenCode returns a cost, it is recorded as reported. Otherwise, the analyzer estimates cost using the model rates in the [OpenCode Go pricing table](https://docs.opencode.ai/docs/go/) and saves those rates alongside the totals. Missing API usage is marked unavailable instead of being presented as zero cost. The rates can change, so the JSON records the rate snapshot used for that run.

Video analysis and video compression require `ffmpeg`; still photos are normalized through Pillow before upload. The report preserves professional themes, possible hobbies, and meaningful one-off details. Family roles are described when explicitly stated by the author in a caption or reply. A woman appearing in a photo or a relationship claim by another commenter is insufficient evidence of marriage. Professional content is distinguished from self-declared occupation; no medical diagnoses or hidden personal circumstances are inferred. Source type and confidence accompany conclusions.

### Benchmark analyzer settings

Run a small paid comparison on the same 5-post sample (25 requests across five settings):

```powershell
python .\benchmark_optimizations.py '.\exports\instagram-example_profile-YYYYMMDD-HHMMSS' --posts 5 --allow-contributor-training
```

The benchmark records aggregate request time, token usage, estimated cost, selected media count, and encoded image bytes in `optimization_benchmark_<timestamp>.md` and `.json`. It does not save captions, images, post identifiers, or per-post model responses. To add a single control scenario to an existing benchmark, use `--scenario combined_768px_1media_1024tokens --append-to '<path-to-benchmark-json>'`. The model cost is estimated from recorded usage and the configured rate snapshot; the benchmark does not formally score analysis quality.

## Detailed analysis and comment coverage

The current pipeline handles up to 100 posts, samples up to six carousel items by default (configurable up to 20), and reserves 2048 output tokens per post plus 6500 for final synthesis. The web pipeline enforces at least six media and 2048 output tokens even if an older deployment still has the earlier 3-media/1024-token Secrets. More posts, media, comments, and output detail increase run time and API cost; earlier 50-post benchmarks do not predict these new totals.

Comments sent to the model are bounded by `OPENCODE_MAX_COMMENTS_PER_POST=100` and `OPENCODE_COMMENT_CHAR_BUDGET=20000`. Owner replies are prioritized if a thread exceeds the budget. The source export retains all collected comments; the JSON report records counts of collected/analyzed/skipped comments and text truncation. Missing comments are explicitly distinguished from a genuinely empty thread. Comments and prompt version participate in the checkpoint fingerprint, so a normal new analysis does not reuse the previous short analyses. `--synthesize-only` can improve the wording of existing evidence, but cannot recover comments or images that were never collected or analyzed.

Prompt version 5 retains publication dates in post analyses and orders dated evidence chronologically in final synthesis. Comment timestamps remain separate from publication dates; an unknown timestamp stays unknown. Synthesis also receives up to eight original comment excerpts per post (3,000 total text characters, at most 1,000 per excerpt), balancing owner and audience statements and preferring verbal comments over short reactions. Quote omission and truncation counts are recorded in the JSON. This is additional context alongside the per-post analyses, not a claim of complete verbatim coverage.

Personal-context sections describe observable joint activities, hugs, kisses and affectionate captions without burying them under repeated disclaimers. A third-party family claim is retained as that commenter's unverified statement. Dates and speaker roles constrain interpretations: an ambiguous claim about having stopped communicating does not establish a couple's breakup, and older shared photos do not establish a current relationship status. No identity matching across faces is performed. A normal new analysis is required for the new evidence fields; refreshing only an old summary cannot restore evidence absent from its saved post analyses.
