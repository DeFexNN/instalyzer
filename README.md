# Instalyzer

Streamlit-застосунок для аналізу тем, інтересів і видимих занять у Instagram-профілі. Український інтерфейс, реальний прогрес обробки, історія сесії, перегляд матеріалів і завантаження підсумкового Markdown-звіту. У публічному демо є позначений приклад вигаданого профілю; реальні запуски захищені кодом доступу.

**Streamlit Cloud:** гілка `main`, головний файл `streamlit_app.py`. Детальна інструкція та Secrets: [DEPLOY.md](DEPLOY.md).

```powershell
python -m pip install -r requirements.txt
python -m streamlit run streamlit_app.py
```

Нижче — документація окремих CLI-скриптів, які використовує вебінтерфейс.

## Instagram export

Exports up to 50 posts from the selected Instagram profile, including captions, dates, links, and local copies of photos/videos. Carousel posts are saved as multiple media files. `IG_USERNAME` identifies the account represented by the local session cookies; the requested profile can be another profile visible to that signed-in account.

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

Comments are off by default. Set `IG_INCLUDE_COMMENTS=true` in `.env` to try including top-level comments; Instagram's comments endpoint can change independently of the post export.

This local exporter uses headless Playwright to load the profile and collect its GraphQL responses. These web endpoints are not a documented public scraping API and may change. Session cookies can expire or be revoked.

Downloaded photos are normalized to JPEG quality 80 and at most 1280 px on the long edge when that makes the file smaller. Video variants are selected at up to 720 px, then optionally transcoded with FFmpeg to H.264 CRF 28; the transcoded file replaces the download only when it is smaller. Install FFmpeg and make sure it is on `PATH` to enable video compression. If FFmpeg is unavailable, the selected source video is kept.

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

Each run creates `exports\instagram-<username>-<timestamp>\` containing `posts.json` and a `media\` folder. At most 50 posts are collected per run. The JSON stores captions, timestamps, permalinks, media types, and local media filenames; signed media URLs and cookies are not saved.

To compact media in an export created earlier (lossy; a smaller result replaces the original file), run:

```powershell
python .\compact_instagram_export.py '.\exports\instagram-example_profile-YYYYMMDD-HHMMSS'
```

The command updates `posts.json` after each successful file and skips files already marked as optimized.

## AI analysis of an export

Set `OPENCODE_API_KEY` in the local `.env` to a **freshly rotated** OpenCode Go key. The analyzer uses `muse-spark-1.3-contributor` through OpenCode Go's Responses API. It analyzes at most 50 posts, sending each post's caption, photos, and one extracted frame per video to OpenCode/Meta. It writes `profile_analysis.md` with only the consolidated conclusions (portrait, likely interests and activities, limits, and final confidence), while `profile_analysis.json` retains the individual post analyses. The summary can include more detail when the evidence supports it, but does not enumerate posts or show their IDs.

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

Post analyses use up to six concurrent model requests by default (configurable from 1 to 8), and send at most three evenly spaced carousel items per post. Each photo sent to the model is normalized to JPEG quality 80 at up to 768 px by default; `OPENCODE_IMAGE_MAX_EDGE=1280` restores the higher-resolution option. Video analysis uses one 720 px frame. Per-post analyses use `minimal` reasoning by default and fall back to `low` if the API rejects that setting. The final synthesis uses `low`. These defaults can be changed with `OPENCODE_MAX_CONCURRENT_POST_ANALYSES`, `OPENCODE_MAX_MEDIA_PER_POST`, `OPENCODE_IMAGE_MAX_EDGE`, `OPENCODE_POST_MAX_OUTPUT_TOKENS`, `OPENCODE_POST_REASONING_EFFORT`, and `OPENCODE_SYNTHESIS_REASONING_EFFORT` in `.env`.

Completed post analyses are checkpointed in the export directory and resumed on the next run when the export and post-analysis settings still match. Pass `--no-resume` to force a fresh analysis. HTTP 429 responses are retried with backoff. The analyzer saves per-request token counts and durations, plus total usage and wall-clock timings, in `profile_analysis.json`; the Markdown report stays focused on profile conclusions. If OpenCode returns a cost, it is recorded as reported. Otherwise, the analyzer estimates cost using the model rates in the [OpenCode Go pricing table](https://docs.opencode.ai/docs/go/) and saves those rates alongside the totals. Missing API usage is marked unavailable instead of being presented as zero cost. The rates can change, so the JSON records the rate snapshot used for that run.

Video analysis and video compression require `ffmpeg`; still photos are normalized through Pillow before upload. The report focuses on visible activities, interests, and post themes, with evidence and confidence; it avoids sensitive personal-attribute guesses.

### Benchmark analyzer settings

Run a small paid comparison on the same 5-post sample (25 requests across five settings):

```powershell
python .\benchmark_optimizations.py '.\exports\instagram-example_profile-YYYYMMDD-HHMMSS' --posts 5 --allow-contributor-training
```

The benchmark records aggregate request time, token usage, estimated cost, selected media count, and encoded image bytes in `optimization_benchmark_<timestamp>.md` and `.json`. It does not save captions, images, post identifiers, or per-post model responses. To add a single control scenario to an existing benchmark, use `--scenario combined_768px_1media_1024tokens --append-to '<path-to-benchmark-json>'`. The model cost is estimated from recorded usage and the configured rate snapshot; the benchmark does not formally score analysis quality.
