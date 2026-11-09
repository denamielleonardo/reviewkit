# ReviewKit

Eclectus Technologies' local review tool. Each developer runs it on their own PC and connects their **own Claude account**.

- **Code review**: pick your local clone, choose the review type (Capataz API .NET, Angular, Ionic), and enter the **source** and **target** branch. Claude reviews the diff `origin/<target>...origin/<source>`.
- **Technical design review**: drop in a `.docx`. Claude returns prioritized review points with severities.
- **History**: every review is saved on your PC, where you can copy it or export it as `.md`.

The review rules are the team's Claude skills in [`skills/`](skills/README.md), built into the app so everyone reviews against the same standards. Your own `~/.claude` settings, CLAUDE.md, and skills are ignored during a review.

## For developers: install and connect

1. Install **Git for Windows** and make sure you can `git fetch` your GitHub repos (your normal git credentials are used).
2. Unzip `ReviewKit.zip` anywhere and run `ReviewKit\ReviewKit.exe`.
3. Go to **Settings → Sign in with Claude**. A console window and your browser open. Sign in, then click **Refresh**.
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

One-time repository setup: **Settings → Pages → Build and deployment → Source: GitHub Actions.**

**Updating review rules:** edit the skills in [`skills/`](skills/README.md) and push to `main`. You can also re-run the workflow manually (Actions → Build and publish ReviewKit → Run workflow). Both the repo and the download page are public. An optional `REVIEWKIT_SKILLS_B64` repository secret (base64 of a zip of the skill folders) overrides the committed skills at build time. To add a new review type, add a skill folder and an entry in `app/skills.py` (`REVIEWS`), then add a button in `app/static/index.html`.

### Layout

| Path | What it does |
| --- | --- |
| `app/main.py` | Starts the local API on 127.0.0.1 and opens the window (pywebview) |
| `app/api.py` | Endpoints for status/login, repos, reviews (streamed NDJSON), history |
| `app/claude_runner.py` | Runs Claude through `claude-agent-sdk` with the selected skill as system prompt |
| `app/git_ops.py` | fetch, branch list, diff |
| `app/docx_reader.py` | `.docx` → Markdown text (headings, lists, tables) |
| `app/static/` | UI (light/dark/system theme) |
| `skills/` | Embedded review skills |
