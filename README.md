# ReviewKit

Eclectus Technologies' local review tool. Each developer runs it on their own PC and connects their **own Claude account**.

- **Code review**: pick your local clone and enter the **source** and **target** branch. Claude reviews the diff `origin/<target>...origin/<source>`. The review type (.NET, Python, Angular, Ionic, React, React Native) is detected automatically and can't be changed by hand. The repository's project files decide it (`.sln`/`.csproj`; `pyproject.toml`/`requirements*.txt`/`setup.py`; `angular.json`; `ionic.config.json`/`capacitor.config.*`; a `package.json` that depends on `react`, or on `react-native`/`expo`). The rules don't assume a framework version: each review first reads the project's own version (target framework, `package.json`, `requires-python`) and only suggests code that works on it. In a repository with more than one (e.g. a .NET API with an Angular `ClientApp`, or a Django API with a React front end), each changed file is reviewed with the rules for its project, and a change touching both gets one review per part. See [`app/detect.py`](app/detect.py).
- **Technical design review**: drop in a `.docx`. Claude returns prioritized review points with severities.
- **History**: reviews are kept on your PC for 7 days, where you can copy them or export them as `.md`. Turn on **Settings → History → Auto-archive** and choose a folder to move older reviews there (one sub-folder per month) instead of deleting them. **Clear history** (History page or Settings) deletes every saved review after a confirmation; archived copies aren't touched.
- **Apply code review points**: after a code review, tick the points you want and ReviewKit edits your local clone to resolve them. It only runs when the clone is on the reviewed source branch, contains the reviewed commit, and has no uncommitted changes. Claude gets file tools only (no shell), and a guard blocks writes outside the repository or into `.git`, so **nothing is ever committed or pushed**. You can view the diff, undo the changes (only while you haven't edited those files since), or change the selection.
- **Pull request and review request**: after a code review, **Create PR & email** creates the pull request `source → target` in Azure DevOps (or reuses the open one for those branches) using the sign-in Git already has, so no token is needed. It then opens a `[For Code Review]: Pull Request <id>: <title>` email in Outlook on the web with the reviewer, greeting, PR line and link filled in. Outlook adds the sender's own signature, and ReviewKit never sends the email. The title follows `TYPE: TICKET - summary` (type and ticket from the branch name, summary from Claude) and can be edited first. The reviewer's address and greeting are remembered per PC. See [`app/azure_devops.py`](app/azure_devops.py) and [`app/review_request.py`](app/review_request.py).
- **Users and activity log**: when the build is connected to Supabase, ReviewKit records who uses it (by their Claude sign-in e-mail, or a work e-mail entered in Settings for API-key users) and what they do: reviews run, points applied or undone, pull requests created, history exported or cleared, settings changed, updates. Only names and counts are sent, never code, diffs or review text. Events are queued on the PC and sent in the background, so ReviewKit works the same offline. The admin sees everyone on the **Users** page (after signing in with the admin account set up in Supabase) and can turn a user's access off, which stops them running reviews, applying points or creating PRs. See [`app/activity.py`](app/activity.py).
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

**Users and activity log (Supabase), one-time setup:**

1. Create a Supabase project. In the SQL editor, run [`supabase/migrations/20261117000000_reviewkit_activity.sql`](supabase/migrations/20261117000000_reviewkit_activity.sql), then add the admin: `insert into public.rk_admins (email) values ('<admin e-mail>');` (kept out of the repo because it's public).
2. **Authentication → Users → Add user → Create new user**: the same admin e-mail and a strong password, with *Auto confirm user* on. This is what you sign in with on the Users page. **Authentication → Sign In / Providers**: turn off *Allow new users to sign up*. No e-mail template or SMTP setup is needed.
3. GitHub **Settings → Secrets and variables → Actions → Variables**: add `REVIEWKIT_SUPABASE_URL` (Project URL) and `REVIEWKIT_SUPABASE_KEY` (the **anon** or **publishable** key; never the service-role/secret key, which the build refuses). Builds without them have the feature switched off. For local testing set the same names as environment variables.

The user e-mail comes from each PC's Claude sign-in and Supabase can't verify it, so treat the log as usage tracking rather than tamper-proof evidence. Admin access is verified by Supabase Auth (the admin user's password).

**Updating review rules:** edit the skills in [`skills/`](skills/README.md) and push to `main`. You can also re-run the workflow manually (Actions → Build and publish ReviewKit → Run workflow). Both the repo and the download page are public. An optional `REVIEWKIT_SKILLS_B64` repository secret (base64 of a zip of the skill folders) overrides the committed skills at build time. To add a new review type, add a skill folder and an entry in `app/skills.py` (`REVIEWS`), teach `app/detect.py` how to recognise that kind of project, and add the skill to the required list in the workflow.

### Layout

| Path | What it does |
| --- | --- |
| `app/main.py` | Starts the local API on 127.0.0.1 and opens the window (pywebview) |
| `app/api.py` | Endpoints for status/login, repos, reviews (streamed NDJSON), history |
| `app/claude_runner.py` | Runs Claude through `claude-agent-sdk` with the selected skill as system prompt |
| `app/git_ops.py` | fetch, branch list, diff |
| `app/detect.py` | Picks the review type(s) from the repository's project files |
| `app/versions.py` | Reads framework/library versions from the project files once, remembers them in `%APPDATA%\ReviewKit\versions.json`, and puts them in the review prompt |
| `app/history.py` | Saved reviews, 7-day retention, auto-archive |
| `app/activity.py` | Users and activity log in Supabase (offline queue, access check, admin page) |
| `supabase/migrations/` | Tables and access rules for the users and activity log |
| `app/updater.py` | Update check and in-place install |
| `app/docx_reader.py` | `.docx` → Markdown text (headings, lists, tables) |
| `app/static/` | UI (light/dark/system theme) |
| `skills/` | Embedded review skills |
