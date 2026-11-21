# Recipes: Instagram to a searchable recipe site

Share a reel or post from your iPhone and the recipe shows up on
**https://cojocarucristianv.github.io/recipes/** about 1-2 minutes later.

```
iOS Share Sheet -> GitHub Issue (queue) -> Actions: Apify / yt-dlp -> Gemini
  -> recipes.json commit -> Pages deploy -> issue comment + label + close
```

## Folder structure

```
.github/workflows/process.yml   main pipeline (issues / daily cron / manual)
.github/workflows/local.yml     optional self-hosted fallback (off by default)
recipebot/                      Python package (python -m recipebot)
  cli.py        CLI: --url, --issue, --all-queued, --dry-run, --local, --force
  processor.py  per-item flow, retries, issue outcomes, publishing
  fetchers.py   Apify -> yt-dlp chain, block detection
  gemini.py     google-genai calls, JSON schema, inline vs File API, 429 handling
  models.py     pydantic: model output + strict recipes.json record
  clean.py      emoji/bullet/hashtag/numbering cleanup, cuisine/type normalization
  store.py      merge, atomic write, git commit/push with conflict retry
  urls.py       URL extraction + canonicalization (/reel/, /reels/, /p/ -> /p/<code>/)
  gh.py         GitHub REST helpers
  prompts/      system.txt, caption.txt, media.txt  (edit freely; $vars are substituted)
config.toml     model, fetcher order, Apify actor, limits
index.html      the website (static, no dependencies)
recipes.json    the data (starts as [])
scripts/create-labels.ps1
```

## Setup (one time, about 20 minutes)

1. **Repo.** Create a **public** repo `CojocaruCristianV/recipes` with no README. Then, from this folder on your personal machine:
   ```bat
   git init -b main && git add . && git commit -m "Initial commit"
   git remote add origin https://github.com/CojocaruCristianV/recipes.git
   git push -u origin main
   ```
2. **Pages.** Repo → Settings → Pages → Build and deployment → Source: **GitHub Actions**.
3. **Actions hardening.** Repo → Settings → Actions → General:
   - Workflow permissions: **Read repository contents** (each workflow grants its own permissions).
   - Fork pull request workflows: **Require approval for all outside collaborators**.
   - Actions permissions: **Allow actions created by GitHub** only. All actions used are `actions/*`.
4. **Labels.** Run `gh auth login`, then `.\scripts\create-labels.ps1`.
5. **Gemini key.** Go to https://aistudio.google.com/apikey → Create API key. The free tier is enough. Confirm the model ID with `python -m recipebot --list-models` (see "Changing the Gemini model" below).
6. **Apify.** Sign up at https://apify.com. Then Console → Settings → API & Integrations → copy the **Personal API token**. The free $5/month of credit covers a few hundred posts. The actor `apify/instagram-scraper` needs no setup.
7. **Secrets.** Repo → Settings → Secrets and variables → Actions → New repository secret:
   - `GEMINI_API_KEY`
   - `APIFY_TOKEN`
   - `IG_COOKIES_B64` *(optional, leave unset for now)*

   Then go to Actions → **process** → **Run workflow** once (leave `url` empty). This publishes the empty site for the first time.
8. **Phone PAT.** GitHub → Settings → Developer settings → Fine-grained tokens → Generate:
   - Repository access: **Only select repositories** → `recipes`
   - Permissions: **Issues: Read and write** (Metadata read-only is added automatically)
   - Expiration: the longest available. Set a calendar reminder to rotate it.
9. **iOS Shortcut** (Shortcuts app → + → name it **Save Recipe**):
   1. Tap **ⓘ** → turn on **Show in Share Sheet**. Receive: **URLs** and **Text** only. If there's no input: **Stop and Respond**.
   2. **Text** action, containing the **Shortcut Input** variable.
   3. **Match Text**: pattern `https?://(www\.)?instagram\.com/[^\s]+` in **Text**.
   4. **Get Item from List**: **First Item** from **Matches**.
   5. **If** *Item from List* **does not have any value**:
      - **Show Notification** "No Instagram link found"
      - **Stop This Shortcut**
      - **End If**
   6. **Get Contents of URL**:
      - URL `https://api.github.com/repos/CojocaruCristianV/recipes/issues`
      - Method **POST**
      - Headers:
        - `Authorization` = `Bearer github_pat_...` (your PAT)
        - `Accept` = `application/vnd.github+json`
        - `X-GitHub-Api-Version` = `2022-11-28`
      - Request Body **JSON** with these fields:
        - `title` (Text) = *Item from List*
        - `body` (Text) = *Item from List*
        - `labels` (Array) with two Text items: `recipe`, `queued`
   7. *(no separate Dictionary action needed)*
   8. **Get Dictionary Value** `number` from *Contents of URL*.
   9. **If** *Dictionary Value* **has any value**:
      - **Show Notification** `Saved #` + *Dictionary Value*
      - **Otherwise**: **Get Dictionary Value** `message` from *Contents of URL* → **Show Notification** `Error: ` + *Dictionary Value*
      - **End If**
10. **Optional self-hosted runner.** Leave this off until your personal machine is set up.
    1. On the personal PC, create a standard (non-admin) local user, e.g. `ghrunner`.
    2. Repo → Settings → Actions → Runners → New self-hosted runner → Windows. Run the shown `config.cmd` with `--labels windows --runasservice --windowslogonaccount .\ghrunner`.
    3. Install Python 3.12 (so that `py -3.12` works for that user) and Git.
    4. Repo → Settings → Secrets and variables → Actions → **Variables** → `USE_LOCAL_RUNNER` = `true`.

    Hardening already in place:
    - The workflow has no `pull_request` triggers, and fork PRs need approval.
    - The runner is used only by the repo and runs as a low-privilege account.
    - The workspace is wiped after each job, and only GitHub-owned actions are allowed.
    - It has its own concurrency group, so it never blocks the main queue.

    You can also keep the runner service set to manual start so it's only online when you want it.

## Changing the Gemini model

The default is `model` in `config.toml` (currently `gemini-3.8-flash`). Overrides, highest priority first:

| Where | Scope | Needs a commit? |
|---|---|---|
| Actions → process → Run workflow → `model` input (or `--model X` locally) | that one run | no |
| Repo → Settings → Secrets and variables → Actions → **Variables** → `GEMINI_MODEL` (or `GEMINI_MODEL=` in `.env` locally) | every run from now on | **no** |
| `config.toml` → `[gemini] model` | the committed default | yes |

- **To find valid IDs**, run `python -m recipebot --list-models`. It lists every model your key can call, and the configured one is marked.
- **Backup models:** list them in `fallback_models = ["…"]` in `config.toml`. If a model returns "not found" or "not supported", the code moves to the next one. A quota error (429) does not switch models.
- **If no model in the list is available:**
  - the run stops,
  - the issues stay `queued`,
  - the job fails with *"Gemini model unavailable"*, so GitHub emails you.

  Set `GEMINI_MODEL` to a valid ID, then use Run workflow, or wait for the next share or the daily run.

## Local use (debugging)

```bat
py -3.12 -m venv .venv && .venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env      &rem then fill GEMINI_API_KEY and APIFY_TOKEN
python -m recipebot --url "https://www.instagram.com/reel/XXXX/?igsh=abc" --dry-run
python -m recipebot --issue 12 --dry-run
python -m recipebot --all-queued --dry-run
```

- `--dry-run` prints JSON only: no commit, no issue changes.
- Without `--dry-run`, the issue commands use `gh auth token`.
- `--force` reprocesses a URL that's already in `recipes.json`.
- Every item writes one JSON log line to stderr: issue, url, status, source, fetcher, ms, error.

## End-to-end test

1. **Dry run with two links** (local, needs `.env`):
   ```bat
   python -m recipebot --url "<post with full recipe in caption>" --dry-run
   python -m recipebot --url "<reel with recipe only in the video>" --dry-run
   ```
   Check the logs:
   - The first link should log `"source": "caption"`. Ingredients and steps should match the caption word for word.
   - The second link should log `"source": "video"` (or `"caption + video"`). Amounts should be prefixed with `~`.
2. **Real share.** In Instagram tap **Share → Save Recipe**. You should get a "Saved #N" notification.
3. **Check the queue.** Repo → Actions → **process** should be running. Within about 2 minutes the issue gets a summary comment, the `done` label, and is closed.
4. **Check the site.** Open `https://cojocarucristianv.github.io/recipes/`. The page loads `recipes.json` with `no-store` and a timestamp, and reloads it whenever you come back to the tab, so a normal refresh is enough. If a new `index.html` doesn't show up (Pages' CDN caches it for about 10 minutes), use `…/recipes/?v=2` or Settings → Safari → Advanced → Website Data → remove `github.io`. Optionally use Share → **Add to Home Screen** to get an app-style icon.

Re-run an item by adding the **`retry`** label to its issue (failed, needs-review or needs-local). To process a link without the phone: Actions → process → Run workflow → `url`.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| **Apify actor failing / out of credit** | Comment shows `Apify: ...` and then yt-dlp runs. Check Apify Console → Billing/Usage (free $5 resets monthly). If the actor was renamed or broke, change `apify_actor` in `config.toml`. Errors about credit or token count as "unavailable", not "blocked". |
| **IG blocking yt-dlp** | yt-dlp is upgraded on every run. If it's still blocked: set `IG_COOKIES_B64` (Netscape cookies.txt from a **throwaway** IG account, `certutil -encode` then strip the header and footer lines, or `[Convert]::ToBase64String(...)`), or enable the self-hosted runner. |
| **Private / login-required post** | Every fetcher reports a block, so the issue gets `needs-local`. Private posts can't be scraped without cookies. Close the issue or use cookies. |
| **Gemini 429 / quota** | The item stays `queued` and the run stops. It's processed automatically by the next share, the daily cron, or Run workflow. If it keeps happening, set `GEMINI_MODEL` to a lighter model; `--list-models` shows the options. |
| **Gemini model not found / retired** | The job fails with "Gemini model unavailable" and the issues stay `queued`. Run `python -m recipebot --list-models`, set the repo variable `GEMINI_MODEL` to a listed ID (no commit needed), then Run workflow. You can also add a backup to `fallback_models`. |
| **Shortcut 401** | The PAT has expired, was revoked, or was pasted wrong. Header must be exactly `Bearer github_pat_…`. |
| **Shortcut 403 / 404** | The PAT isn't scoped to `recipes`, or Issues isn't set to Read and write. Check the repo name in the URL. A 404 also happens if Issues is disabled in repo Settings. |
| **Shortcut 422 with labels** | Labels don't exist yet. Run `create-labels.ps1`. |
| **Push rejected** | The code retries 5 times (reset, then pull --rebase, then re-merge). If it still fails: check that branch protection allows `github-actions[bot]` to push to `main`, and that Workflow permissions aren't overriding `contents: write`. Items go back to `queued`. |
| **Pages not updating** | Pages source must be **GitHub Actions**. Check the `deploy` job in Actions (the `github-pages` environment must allow `main`). The CDN can take up to 10 minutes for `index.html`. |
| **Workflow not triggering** | Issues opened by someone other than the repo owner are skipped by design. Actions must be enabled, and the workflow file must be on the default branch. GitHub turns off cron after 60 days without repo activity; shares still trigger runs, and you can re-enable it on the Actions tab. |
| **Stuck in `processing`** | A run crashed or timed out. The next run picks up `processing` issues automatically, or add `retry`. |
| **Wrong cuisine / type** | Edit `recipes.json` directly (keep the format). Future runs reuse whatever values are there. Prompts are in `recipebot/prompts/`. |
