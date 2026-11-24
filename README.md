# Recipes: Instagram to a searchable recipe site

Share a reel or post from your iPhone and the recipe shows up on
**https://cojocarucristianv.github.io/recipes/** about 1-2 minutes later.

```
iOS Share Sheet -> GitHub Issue (queue) -> Actions: Apify / yt-dlp -> Gemini
  -> recipes.json commit -> Pages deploy -> issue comment + label + close
```

Edits and "Re-read from video" on the site use the same queue: the site opens an `edit` or
`reread` issue, and the bot updates the recipe in place.

## Where a recipe comes from

The bot tries these sources in order and stops at the first one that gives a complete recipe:

1. **Caption and the creator's own comments.** Many creators put the recipe in a comment ("recipe in the comments"). The post's first comments come with the post for free. When the caption points to the comments, one more Apify run reads up to 50 of them. Only comments written by the post's owner are used.
2. **The creator's website.** Links are tried in this order:
   - links in the caption or the creator's comments;
   - the link(s) in the creator's **Instagram bio**, including link pages such as Linktree or Beacons;
   - the creator's own site. That site is searched for the dish through the WordPress search API, then its sitemap.

   A page counts only when its name matches the dish. Pages with schema.org recipe data are read directly, with no Gemini call. Other pages are read by Gemini, quoting the page text.
3. **The video or images**, read by Gemini. Amounts that are only seen or heard are marked `~`.

**Paid content is never used.** Paywalled articles, members-only posts and recipes sold as products (ebooks, Shopify/Gumroad/Patreon/Substack paywalls) are detected and skipped. The bot then falls back to the next source.

Each recipe stores the **creator** (`author`, the Instagram username) and, when it came from a web page, that page (`source`). The site shows them under the title as *by @username* and *Recipe from site.com*.

## Folder structure

```
.github/workflows/process.yml   main pipeline (issues / daily cron / manual)
.github/workflows/local.yml     optional self-hosted fallback (off by default)
recipebot/                      Python package (python -m recipebot)
  cli.py        CLI: --url, --issue (also edit / re-read issues), --all-queued, --dry-run, --local, --force
  processor.py  per-item flow (new recipe, edit, re-read), retries, issue outcomes, publishing
  edits.py      website edits: request parsing, conflict check, validation, change summary
  fetchers.py   Apify -> yt-dlp chain, block detection, creator comments, bio links, Apify usage
  web.py        creator website lookup: safe fetching, Apify web fallback, recipe JSON-LD, paywall check
  probe.py      --probe: saves raw Apify output to .probe/ for debugging
  backfill.py   --backfill-authors: fills in the creator of older recipes
  gemini.py     google-genai calls, JSON schema, inline vs File API, 429 handling
  models.py     pydantic: model output + strict recipes.json record
  clean.py      emoji/bullet/hashtag/numbering cleanup, cuisine/type normalization
  store.py      merge, in-place updates, atomic write, git commit/push with conflict retry
  urls.py       URL extraction + canonicalization (/reel/, /reels/, /p/ -> /p/<code>/)
  gh.py         GitHub REST helpers
  prompts/      system.txt, caption.txt, media.txt, web.txt, reread.txt  (edit freely; $vars are substituted)
config.toml     model, fetcher order, Apify actors, web lookup, limits
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
4. **Labels.** Run `gh auth login`, then `.\scripts\create-labels.ps1`. If you set this up before editing existed, run it again: it adds the `edit` and `reread` labels. It is safe to re-run, since existing labels are only updated, never deleted.
5. **Gemini key.** Go to https://aistudio.google.com/apikey → Create API key. The free tier is enough. Confirm the model ID with `python -m recipebot --list-models` (see "Changing the Gemini model" below).
6. **Apify.** Sign up at https://apify.com. Then go to Console → Settings → API & Integrations and copy the **Personal API token**.
   - **Cost:** an Instagram run costs about $0.003, so the free $5/month covers roughly 1,500 runs. A recipe takes 1 run if the caption has it, and up to 3 if the bot also reads more comments and the creator's bio. A website fetch through `apify/cheerio-scraper` is about $0.001. Every run summary logs the month's usage (`apify_usd`).
   - **Actors:** `apify/instagram-scraper` needs no setup. `apify/cheerio-scraper` is only used when a website blocks plain requests. The first time it runs, the Apify Console may ask you to approve the actor's permissions: open the run in Console and approve them once.
7. **Secrets.** Repo → Settings → Secrets and variables → Actions → New repository secret:
   - `GEMINI_API_KEY`
   - `APIFY_TOKEN`
   - `IG_COOKIES_B64` *(optional, leave unset for now)*

   Then go to Actions → **process** → **Run workflow** once (leave `url` empty). This publishes the empty site for the first time.
8. **Phone PAT.** GitHub → Settings → Developer settings → Fine-grained tokens → Generate:
   - Repository access: **Only select repositories** → `recipes`
   - Permissions: **Issues: Read and write** (Metadata read-only is added automatically)
   - Expiration: the longest available. Set a calendar reminder to rotate it.

   The site uses the same token for edits and re-reads. It asks for it the first time you send one (see "Editing a recipe").
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

- **To find valid IDs**, run `python -m recipebot --list-models`. It lists every model your key can call, marks the configured one and the fallbacks, and names any configured ID your key can't use (those are simply skipped).
- **Backup models:** `fallback_models` in `config.toml` lists them, tried in order after the main model. Default: `gemini-3.7-flash`, `gemini-3.6-flash`, `gemini-3.5-flash`, `gemini-3.5-flash-lite`. The bot moves to the next model when the current one:
  - **runs out of quota (429):** the bot reads which limit was hit.
    - **Per-minute limit:** the bot waits (`rpm_waits_s`, default 20 s then 40 s) and tries the same model again. Requests are also spaced at least `min_interval_s` apart (default 4.5 s), so this is rare.
    - **Daily limit:** that model is skipped for the rest of the run. When every model's daily quota is used up, the issue gets a comment saying so and stays `queued`.
  - **is not found / not supported:** that model is skipped for the rest of the run,
  - **is busy (503, 5xx, timeout):** the next model is tried right away. The 30 s / 60 s / 120 s waits only start once every model in the list was busy; after each wait the bot starts again from the top.

  The model that answers stays in use for the rest of the run. Each switch is logged as `gemini-fallback`, and the issue comment says which model read the recipe (`model: ...`).
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
- `python -m recipebot --probe "<link>"` saves the raw Apify output to `.probe/`: the post, its comments and the creator's profile. It is ignored by git, so you can delete it at any time. Use it to see what Apify returns when a recipe was not found.
- `python -m recipebot --backfill-authors --dry-run` shows the creator it would fill in for each recipe that has none (one Apify run for all of them). Without `--dry-run` it commits and pushes `recipes.json`, like a normal run.

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

Re-run an item by adding the **`retry`** label to its issue (failed, needs-review or needs-local). In the GitHub app: open the issue → **Labels** → tick `retry`; the bot starts within a minute. Every failure comment says which step failed (fetching the post, reading the caption, downloading or reading the video) and repeats these instructions. To process a link without the phone: Actions → process → Run workflow → `url`.

## Using the site

Open the site from a **Home Screen icon** (Safari → Share → **Add to Home Screen**), not from a Safari tab. It opens full screen, and on iOS it keeps its own saved data (see "Your saved data" below).

### Tabs

| Tab | What it shows |
|---|---|
| **Home** | **In the spotlight** (6 random recipes, the same all day; **Shuffle** picks others), Continue cooking (recipes you opened lately; appears after you open one), Recently added, **Browse by cuisine** tiles and **Types** chips. Tapping a tile or chip opens Browse filtered to it. The **+** button adds a recipe from a link; the gear icon opens Settings. |
| **Browse** | Every recipe with search, filters and sort. The **+** button adds a recipe from a link. |
| **Saved** | Recipes you saved with the heart, most recently saved first. |
| **List** | Your shopping list. The badge shows how many items are still to buy. |

On a phone the tabs sit at the bottom; on a wide screen they move to a bar at the top.

### Search, filters and sort

- **Search** looks at the name, cuisine, type, ingredients and the creator's username. Accents are ignored (`creme` finds *Crème Brûlée*), and every word you type must match. Matches are highlighted, and if a word only matched an ingredient, that ingredient is shown under the card.
- **Ingredients only** searches the ingredients alone, and every word must appear in an ingredient. `garlic lemon` finds recipes that use both.
- **Filters** (the sliders button) open a sheet with cuisine, type, *Saved only*, and *Show recipes that need review*. The number on each chip is how many recipes you would get. Active filters appear as pills under the search bar; tap a pill to remove it.
- **Sort**: Newest, Oldest, A-Z, Fewest ingredients.
- Search, filters and sort are remembered for your next visit.

### Recipe page

- Covers are coloured by **cuisine** (Italian red, French indigo, Greek blue, Mexican green, Indian saffron...) with a cuisine icon. A cuisine without its own colour gets a fixed colour worked out from its name, plus the recipe type's icon.
- Under the title: *by @creator* (opens their Instagram profile) and, when the recipe was taken from a website, *Recipe from site.com* (opens that page).
- The facts row shows the estimated time (`~1 h 15 min`), number of ingredients and number of steps. Times come from captions or are guessed from the video, so they are shown with `~`.
- On a phone, Ingredients and Steps are two tabs; on a wide screen they sit side by side.
- Tap an ingredient to tick it; **Untick all** clears the ticks. Tap a step to highlight where you are. Ticks are kept until you clear them, even after closing the app.
- Amounts starting with `~` are estimates (usually read from a video) and are shown in grey. `TBD:` marks a missing detail and is highlighted; recipes with gaps also get a *Review* tag.
- **Save**, **Share** (the iOS share sheet; on a computer the link is copied) and **Instagram** (opens the original post).
- **Edit** and **Re-read from video**: see below.
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

### Adding a recipe from the site

Useful on a computer, or on a phone without the Shortcut.

1. Tap **+** on Home or Browse.
2. Paste an Instagram post or reel link (or tap **Paste**). A whole shared message works too; the first instagram.com link in it is used.
3. Tap **Send**. The first time, the site asks for the GitHub token (see "The GitHub token" below).

The site checks the link before sending: it must be an https instagram.com post, reel or tv link, or an `instagram.com/share/...` link. Post, reel and tv links are sent in the canonical `https://www.instagram.com/p/<code>/` form. If the recipe is already in the library, nothing is sent and the sheet links to it. Share links are sent unchanged, since only the bot can follow them; its own duplicate check covers them.

Send creates a `recipe` + `queued` issue, the same as the Shortcut, so the bot starts within a minute. The toast shows the issue number. The **+** sheet lists links sent from this device that haven't arrived yet; while one is fresh, the site checks for new recipes every 20 seconds and shows **Added: name** when it lands. The list is kept on this device only (not in backups) and entries are dropped after a day.

### Editing a recipe

Tap **Edit** on a recipe page.

- Every ingredient and step becomes a text box. Use the arrows to move a line up or down, the bin to delete it, and **Add ingredient** / **Add step** for a new line.
  - Return in a box splits the line at the cursor.
  - Pasting several lines at once makes one line each.
  - Empty boxes are dropped.
- **Name**, **cuisine**, **type** and **time** can be changed too. Cuisine and type suggest the values already in the library.
- **Mark as reviewed** appears when the recipe has the *Review* tag; turning it on removes the tag. This works even if some lines still contain `TBD:`, and the summary warns you about them.
- **Save** shows **Check your changes**: what was changed, added, removed or moved. Nothing is sent yet. **Keep editing** goes back, and **Send** creates an `edit` issue.
- **Cancel** leaves edit mode and asks first if you changed something. You can go to other pages while editing; the draft is still there when you come back. Reloading or closing the site discards it (the browser asks first).

After you send, the recipe shows **Edit sent (#N)** with a link to the issue. About a minute later the bot has checked the change, updated the recipe in place (same link, same date), committed it and rebuilt the site. The note then disappears by itself and a message says the edit **is live**. The issue is closed with a list of the changes.

- **If the recipe changed since you started editing** (another edit, or a re-read that finished meanwhile), the bot doesn't overwrite it. The issue is closed with *"Edit not applied"*: reload the recipe and make the edit again. The edit page also warns you if it notices the change before you send.
- **Undo:** every change is a commit. Open `recipes.json` on GitHub and tap **History**; the issue comment links to the commit. Copy the old lines back with another edit, or revert the commit on GitHub.

#### The GitHub token

The first time you send an edit or a re-read, a sheet asks for a GitHub token. Either:

- **Use your Shortcut's token:** open Shortcuts, then **Save Recipe**, then the **Get Contents of URL** action, then **Headers**. Copy the `Authorization` value without `Bearer ` (pasting it with `Bearer ` also works).
- **Or make a new one:** on GitHub go to Settings, then Developer settings, then **Fine-grained tokens**. Give it access to only `recipes` and set **Issues** to **Read and write**.

The site checks the token once with GitHub and saves it **on this device only**. The Home Screen app and Safari keep separate data, so each asks once.

- It is **never** part of an Export, and Import leaves it alone.
- **Settings → Editing → Forget token** removes it from the device. If GitHub later refuses it (expired or revoked), the site forgets it and asks again.

**About the shared address:** every GitHub Pages site on your account lives at `cojocarucristianv.github.io`, so any other Pages site you publish there could read the token. The token can only create issues in `recipes`, so the worst case is spam issues. If you publish other Pages sites, consider a separate token just for the site.

### Re-read from video

**Re-read from video** on a recipe page asks the bot to read the post again: the caption and the video (or images) together, asking for the *complete* ingredient and step lists.

- The caption's wording is kept where the caption covers an item. Details seen only in the video are added as `~` estimates, and gaps are marked `TBD:`.
- **Only the ingredients and steps are replaced.** Name, cuisine, type, time and date stay.
- **Changes you made to the ingredients or steps by hand are overwritten.** A confirmation says so before anything is sent.
- The note **Re-read requested (#N)** shows until the new version is live. The issue comment lists the new ingredients and steps; the old version is in `recipes.json` → History.
- If the post has no video or images, the issue is closed with *"Nothing to re-read"* and the recipe is not changed.
- A re-read needs Gemini. If Gemini is busy or out of quota, the request stays `queued` and runs later, like a new recipe. Edits never need Gemini, so they still go through.

### Your saved data

Saved recipes, ticks, the shopping list, recently opened recipes and your Browse settings are stored **only on the device and browser you use**, in `localStorage`. Nothing is sent to GitHub, and nothing syncs between devices.

The only exception is what you send yourself with **Edit** or **Re-read from video**: those create issues in the repo. The GitHub token they use is stored under its own key (`recipes-token`), separate from the rest, and it is never exported.

- Data is stored under each recipe's Instagram code, so it survives new recipes being added and `recipes.json` being reordered. If a recipe is removed from `recipes.json`, its saved entry is simply skipped.
- The site asks the browser to keep this data permanently (`navigator.storage.persist()`). Whether that is granted is up to the browser; it never shows you a prompt, and Safari decides on its own.
- **Safari clean-up:** in a normal Safari tab, iOS deletes a site's data after **7 days without a visit**. A **Home Screen icon** is treated as an app and does not have that limit, which is why it is the recommended way to use the site. The Home Screen app and Safari keep **separate** data, so what you save in one does not appear in the other.
- Clearing Safari's website data (Settings → Safari → Clear History and Website Data) or deleting the Home Screen icon also deletes the saved data.

### Backup and moving to another device

Open **Settings** (gear icon on Home):

- **Export** creates `recipes-backup-YYYY-MM-DD.json`. On iPhone the share sheet opens; pick **Save to Files** (or AirDrop it to another device). On a computer the file is downloaded.
- **Import** reads a backup file and, after you confirm, **replaces** this device's saved data with it, then reloads the page.
- Neither touches the GitHub token: it is not in the backup, and Import keeps the one already on the device.

To move from Safari to the Home Screen app, or to a new phone: Export on the old one, then Import on the new one. It's worth exporting now and then, especially before clearing Safari data.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| **Apify actor failing / out of credit** | Comment shows `Apify: ...` and then yt-dlp runs. Check Apify Console → Billing/Usage (free $5 resets monthly). If the actor was renamed or broke, change `apify_actor` in `config.toml`. Errors about credit or token count as "unavailable", not "blocked". |
| **IG blocking yt-dlp** | yt-dlp is upgraded on every run. If it's still blocked: set `IG_COOKIES_B64` (Netscape cookies.txt from a **throwaway** IG account, `certutil -encode` then strip the header and footer lines, or `[Convert]::ToBase64String(...)`), or enable the self-hosted runner. |
| **Private / login-required post** | Every fetcher reports a block, so the issue gets `needs-local`. Private posts can't be scraped without cookies. Close the issue or use cookies. |
| **Gemini busy (503 "high demand", 5xx, timeout)** | The bot first tries the next model in `fallback_models` right away. Only when every model was busy does it wait 30 s, 60 s, then 120 s and try again from the top, all in the same run, reusing the downloaded video. Retries after a wait send the video at low resolution (about 4x fewer tokens). If the caption already had part of the recipe, that part is published with the rest marked `TBD` and the `needs-review` label. Otherwise the issue goes back to `queued` with a comment, the run stops, and the next share or the daily run tries again; add `retry` to try sooner. Waits and settings are under `[gemini]` in `config.toml` (`busy_waits_s`, `light_retry`, `thinking_level`). |
| **Gemini 429 / quota** | The bot switches to the next model in `fallback_models` (on the free tier each model has its own quota) and skips the used-up one for the rest of the run. Only when every model is out of quota does the item stay `queued` and the run stop. It's then processed automatically by the next share, the daily cron, or Run workflow. |
| **Gemini model not found / retired** | The job fails with "Gemini model unavailable" and the issues stay `queued`. Run `python -m recipebot --list-models`, set the repo variable `GEMINI_MODEL` to a listed ID (no commit needed), then Run workflow. You can also add a backup to `fallback_models`. |
| **Shortcut 401** | The PAT has expired, was revoked, or was pasted wrong. Header must be exactly `Bearer github_pat_…`. |
| **Shortcut 403 / 404** | The PAT isn't scoped to `recipes`, or Issues isn't set to Read and write. Check the repo name in the URL. A 404 also happens if Issues is disabled in repo Settings. |
| **Shortcut 422 with labels** | Labels don't exist yet. Run `create-labels.ps1`. |
| **Push rejected** | The code retries 5 times (reset, then pull --rebase, then re-merge). If it still fails: check that branch protection allows `github-actions[bot]` to push to `main`, and that Workflow permissions aren't overriding `contents: write`. Items go back to `queued`. |
| **Pages not updating** | Pages source must be **GitHub Actions**. Check the `deploy` job in Actions (the `github-pages` environment must allow `main`). The CDN can take up to 10 minutes for `index.html`. |
| **Workflow not triggering** | Issues opened by someone other than the repo owner are skipped by design. Actions must be enabled, and the workflow file must be on the default branch. GitHub turns off cron after 60 days without repo activity; shares still trigger runs, and you can re-enable it on the Actions tab. |
| **Stuck in `processing`** | A run crashed or timed out. The next run picks up `processing` issues automatically, or add `retry`. |
| **Wrong cuisine / type** | Use **Edit** on the recipe page, or edit `recipes.json` directly (keep the format). Future runs reuse whatever values are there. Prompts are in `recipebot/prompts/`. |
| **Site: "GitHub did not accept the token"** (401) | The token expired, was revoked or was pasted wrong. The site forgets it and asks again; your edit is kept. Paste the current token (the same one as in your Shortcut, if you rotated it there). |
| **Site: "The token cannot create issues"** (403 / 404) | The token isn't scoped to `recipes`, or Issues isn't set to Read and write. A 404 also happens if Issues is disabled in repo Settings. |
| **Site: "GitHub refused the request"** (422) | Usually the `recipe` / `edit` / `reread` / `queued` labels don't exist yet. Run `create-labels.ps1` again. |
| **Site: "That isn't an Instagram post or reel link"** | The pasted text has no https instagram.com `/p/`, `/reel/`, `/reels/`, `/tv/` or `/share/` link (profile and story links don't work). Copy the link from the post's share menu. |
| **Added a link from the site, but it never shows up** | Open the issue from the **+** sheet to see what the bot said (failed, duplicate, needs-local...). Entries leave the waiting list after a day. |
| **Site: "Not sent"** | The message says why (offline, GitHub error). Your changes are kept, so tap Send again. |
| **"Edit not applied: changed since you started editing"** | Someone (or a re-read) changed the recipe after you opened Edit. Nothing was overwritten. Reload the recipe and make the edit again. |
| **"Could not apply this edit"** | The edit was refused as invalid: an empty name, no ingredients or steps left, a line over 500 characters, or a recipe that was removed. Fix it and send again. |
| **"Nothing to re-read"** | The post has no video or images (or they couldn't be found), so there was nothing to add. The recipe is unchanged; use Edit instead. |
| **Re-read stays `queued`** | Gemini was busy or out of quota. It runs with the next share or the daily run; add `retry` to try sooner. |
| **Edit sent, but the note doesn't go away** | Open the issue (the link in the note) to see what happened. The note stops waiting after 30 minutes. |
| **`apify-format-warning` in the log** | The Instagram actor's output is missing fields the bot expects (for example `ownerUsername` or `latestComments`). The actor probably changed. Run `--probe` on the link to see the raw output, then adjust `fetchers.py` or switch `apify_actor`. |
| **Recipe is on the creator's site, but the bot didn't find it** | The log shows `web` steps. Common causes: the page is paid or members-only (skipped on purpose), the site is a shop or a link page with no recipe, or its title doesn't match the dish. Use **Edit**, or raise `max_fetches` / lower `min_name_score` under `[web]`. To turn the lookup off, set `enabled = false`. |
| **"Daily free quota is used up"** | Every Gemini model hit its daily free limit. Issues stay `queued` and run on the next share or the daily cron once the quota resets. |
| **Website lookup is slow or uses Apify credit** | Limits are under `[web]` in `config.toml`: `max_seconds`, `max_fetches`, `max_bio_links` and `max_apify_runs`. Set `apify_fallback = false` to never use Apify for websites, or `follow_bio_links = false` to skip the bio. |

## Future ideas

### Optimized cooking timeline

Plan the cooking as a schedule rather than a plain step list: which steps can run at the same time, and when to start each one so everything is ready together. Design notes:

- **Data:** an optional `plan` per recipe, with one entry per step:
  - `active_min` (hands-on time) and `passive_min` (oven, resting, marinating);
  - `after`: the steps it depends on;
  - `uses`: the equipment it needs (oven, burner);
  - `est`: true when the times are guesses.
- **Extraction:** a separate text-only Gemini call on the finished ingredients and steps. It is cheap and needs no video. Its output is validated: no cycles, every `after` points to a real step, times are within limits.
- **Scheduling** runs on the site, not in the model: a deterministic critical-path scheduler in JavaScript. Defaults are 1 cook, 1 oven and 4 burners. Passive steps don't occupy the cook.
- **UI:** a **Timeline** tab next to Ingredients and Steps, with a Gantt-style chart and a "Cook now" mode that counts down to the next step.

### More sources

TikTok and YouTube Shorts links, using the same pipeline with other fetchers.
