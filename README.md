# ReviewKit

Eclectus Technologies' local review tool. Each developer runs it on their own PC and connects their **own Claude account**.

- **Code review**: pick your local clone and enter the **source** and **target** branch. Claude reviews the diff `origin/<target>...origin/<source>`. The review type (.NET, Angular, Ionic) is detected automatically and can't be changed by hand. The repository's project files decide it (`.sln`/`.csproj`, `angular.json`, `ionic.config.json`/`capacitor.config.*`). In a repository with more than one (e.g. a .NET API with an Angular `ClientApp`), each changed file is reviewed with the rules for its project, and a change touching both gets one review per part. See [`app/detect.py`](app/detect.py).
- **Technical design review**: drop in a `.docx`. Claude returns prioritized review points with severities.
- **History**: reviews are kept on your PC for 7 days, where you can copy them or export them as `.md`. Turn on **Settings → History → Auto-archive** and choose a folder to move older reviews there (one sub-folder per month) instead of deleting them.
- **Updates**: ReviewKit checks the download page for new builds. With **Install updates automatically** on (the default), a new version is downloaded, verified (SHA-256) and installed when the app starts; otherwise an **Update now** banner appears.

The review rules are the team's Claude skills in [`skills/`](skills/README.md), built into the app so everyone reviews against the same standards. Your own `~/.claude` settings, CLAUDE.md, and skills are ignored during a review.

## For developers: install and connect

1. Install **Git for Windows** and make sure you can `git fetch` your GitHub repos (your normal git credentials are used).
2. Unzip `ReviewKit.zip` anywhere and run `ReviewKit\ReviewKit.exe`.
3. Go to **Settings → Sign in with Claude**. A console window and your browser open. Sign in; ReviewKit picks it up automatically.
   - Alternative: paste your own Anthropic Console API key under **API key**.
4. Click **Test connection**. You should see "Claude replied: OK".

You don't need to install Claude Code separately, because ReviewKit ships with its own copy. If you already use Claude Code, you're already signed in.

## Safety

Claude runs **read-only** during code reviews. It may only use `Read`, `Grep`, `Glob`, `git diff`, `git log`, and `git show` inside the selected repo. Everything else is denied automatically. TD reviews run with no tools at all.

Data stays on your PC under `%APPDATA%\ReviewKit\` (settings, API key if you set one, and history).

## For maintainers

```bash
py -3.12 -m venv .venv
.venv\Scripts\pip install -r requirements.txt pyinstaller
.venv\Scripts\python -m app.main            # desktop window
.venv\Scripts\python -m app.main --browser  # or in your browser
```

### Releases (CI/CD)

Every push to `main` runs [`.github/workflows/build-and-publish.yml`](.github/workflows/build-and-publish.yml). It builds the Windows app on GitHub Actions, smoke-tests it, zips it, and publishes it with the download page ([`site/index.html`](site/index.html)) to **https://denamielleonardo.github.io/reviewkit/**, which is the only download location. Nothing is built locally.

The workflow also publishes `version.json` (version, size, SHA-256). Installed copies read it to update themselves ([`app/updater.py`](app/updater.py)): download → verify hash → extract to `%APPDATA%\ReviewKit\update` → a PowerShell hand-off script waits for the app to exit, copies the new files over the install folder and restarts it (log: `%APPDATA%\ReviewKit\update\update.log`). Copies older than this feature must be updated manually once. Set `REVIEWKIT_UPDATE_URL` to point a build at a different update source for testing.

One-time repository setup: **Settings → Pages → Build and deployment → Source: GitHub Actions.**

**Updating review rules:** edit the skills in [`skills/`](skills/README.md) and push to `main`. You can also re-run the workflow manually (Actions → Build and publish ReviewKit → Run workflow). Both the repo and the download page are public. An optional `REVIEWKIT_SKILLS_B64` repository secret (base64 of a zip of the skill folders) overrides the committed skills at build time. To add a new review type, add a skill folder and an entry in `app/skills.py` (`REVIEWS`), then add a button in `app/static/index.html`.

### Layout

| Path | What it does |
| --- | --- |
| `app/main.py` | Starts the local API on 127.0.0.1 and opens the window (pywebview) |
| `app/api.py` | Endpoints for status/login, repos, reviews (streamed NDJSON), history |
| `app/claude_runner.py` | Runs Claude through `claude-agent-sdk` with the selected skill as system prompt |
| `app/git_ops.py` | fetch, branch list, diff |
| `app/history.py` | Saved reviews, 7-day retention, auto-archive |
| `app/updater.py` | Update check and in-place install |
| `app/docx_reader.py` | `.docx` → Markdown text (headings, lists, tables) |
| `app/static/` | UI (light/dark/system theme) |
| `skills/` | Embedded review skills |
