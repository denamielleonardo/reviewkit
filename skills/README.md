# Review skills

These are the review rules built into ReviewKit. Each folder is one Claude skill:

| Folder | Used for |
| --- | --- |
| `capataz-api-reviewer/` | Code review: Capataz API (.NET) |
| `angular-code-reviewer/` | Code review: Angular |
| `ionic-framework-code-reviewer/` | Code review: Ionic |
| `technical-design-reviewer/` | Technical design (.docx) review |

**To update a rule:** edit the `SKILL.md` (or copy in the latest version from your synced Claude skills, usually `%USERPROFILE%\.claude\skills\synced\<org-id>\`) and push to `main`. CI rebuilds the app and publishes it to https://denamielleonardo.github.io/reviewkit/.

To add a new review type, add a skill folder, register it in `app/skills.py` (`REVIEWS`), and add a button in `app/static/index.html`.
