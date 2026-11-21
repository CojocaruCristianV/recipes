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

## Using the site

Open the site from a **Home Screen icon** (Safari → Share → **Add to Home Screen**), not from a Safari tab. It opens full screen, and on iOS it keeps its own saved data (see "Your saved data" below).

### Tabs

| Tab | What it shows |
|---|---|
| **Home** | Recently added, Continue cooking (recipes you opened lately; appears after you open one), Quick (30 min or less), **Surprise me** (a random recipe) and **Browse by type** tiles. The gear icon opens Settings. |
| **Browse** | Every recipe with search, filters and sort. |
| **Saved** | Recipes you saved with the heart, most recently saved first. |
| **List** | Your shopping list. The badge shows how many items are still to buy. |

On a phone the tabs sit at the bottom; on a wide screen they move to a bar at the top.

### Search, filters and sort

- **Search** looks at the name, cuisine, type and ingredients. Accents are ignored (`creme` finds *Crème Brûlée*), and every word you type must match. Matches are highlighted, and if a word only matched an ingredient, that ingredient is shown under the card.
- **Ingredients only** searches the ingredients alone, and every word must appear in an ingredient. `garlic lemon` finds recipes that use both.
- **Filters** (the sliders button) open a sheet with type, cuisine, time (≤ 15 min, ≤ 30 min, ≤ 1 h, longer), *Saved only*, and *Show recipes that need review*. The number on each chip is how many recipes you would get. Active filters appear as pills under the search bar; tap a pill to remove it.
- **Sort**: Newest, Oldest, A-Z, Quickest, Fewest ingredients. For sorting and the time filter, the time text is turned into minutes: `1 h 15 min` = 75, `20 min + 24 h` = 1460, and a range like `25-30 min` counts as 30. Recipes with no readable time go last under Quickest.
- Search, filters and sort are remembered for your next visit.

### Recipe page

- The facts row shows total time, number of ingredients and number of steps.
- On a phone, Ingredients and Steps are two tabs; on a wide screen they sit side by side.
- Tap an ingredient to tick it; **Untick all** clears the ticks. Tap a step to highlight where you are. Ticks are kept until you clear them, even after closing the app.
- Amounts starting with `~` are estimates (usually read from a video) and are shown in grey. `TBD:` marks a missing detail and is highlighted; recipes with gaps also get a *Review* tag.
- **Save**, **Share** (the iOS share sheet; on a computer the link is copied) and **Instagram** (opens the original post).
- Each recipe has its own link (`…/recipes/#/r/<code>`), so a shared link opens straight on that recipe.
- You come back to the same scroll position and tab when you return to a page.

### Keep screen on

The **Keep screen on** switch on the recipe page stops the phone from dimming and locking while that recipe is open.

- It only works on a recipe page and lets go as soon as you leave it.
- The switch is remembered: once on, it is on for every recipe you open.
- iOS lets go of it when you switch apps or lock the phone; the site turns it back on when you return to the recipe.
- If the browser cannot keep the screen on (older iOS versions), the switch is not shown.

### Shopping list

- On a recipe page, **Add all to list** adds every ingredient. After ticking what you already have, **Add unticked (n)** adds only the rest.
- Items are grouped by recipe. Adding the same recipe again does not create duplicates; an item you had ticked off is unticked instead.
- Tap an item to tick it off. **Clear checked** removes ticked items. The × next to a recipe removes its whole group.
- **Copy / Share** sends the items still to buy as plain text: the share sheet on a phone (Notes, Messages, Reminders), the clipboard on a computer.

### Your saved data

Saved recipes, ticks, the shopping list, recently opened recipes and your Browse settings are stored **only on the device and browser you use**, in `localStorage`. Nothing is sent to GitHub, and nothing syncs between devices.

- Data is stored under each recipe's Instagram code, so it survives new recipes being added and `recipes.json` being reordered. If a recipe is removed from `recipes.json`, its saved entry is simply skipped.
- The site asks the browser to keep this data permanently (`navigator.storage.persist()`). Whether that is granted is up to the browser; it never shows you a prompt, and Safari decides on its own.
- **Safari clean-up:** in a normal Safari tab, iOS deletes a site's data after **7 days without a visit**. A **Home Screen icon** is treated as an app and does not have that limit, which is why it is the recommended way to use the site. The Home Screen app and Safari keep **separate** data, so what you save in one does not appear in the other.
- Clearing Safari's website data (Settings → Safari → Clear History and Website Data) or deleting the Home Screen icon also deletes the saved data.

### Backup and moving to another device

Open **Settings** (gear icon on Home):

- **Export** creates `recipes-backup-YYYY-MM-DD.json`. On iPhone the share sheet opens; pick **Save to Files** (or AirDrop it to another device). On a computer the file is downloaded.
- **Import** reads a backup file and, after you confirm, **replaces** this device's saved data with it, then reloads the page.

To move from Safari to the Home Screen app, or to a new phone: Export on the old one, then Import on the new one. It's worth exporting now and then, especially before clearing Safari data.

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
