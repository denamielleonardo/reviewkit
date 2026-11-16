# Review skills

These are the review rules built into ReviewKit. Each folder is one Claude skill:

| Folder | Used for |
| --- | --- |
| `capataz-api-reviewer/` | Code review: Capataz API (.NET) |
| `angular-code-reviewer/` | Code review: Angular |
| `ionic-framework-code-reviewer/` | Code review: Ionic |
| `react-code-reviewer/` | Code review: React (web) |
| `react-native-code-reviewer/` | Code review: React Native / Expo |
| `python-code-reviewer/` | Code review: Python |
| `technical-design-reviewer/` | Technical design (.docx) review |

The code review rules are version-independent: each one starts by detecting the project's actual framework and language version and must only suggest code that works on that version.

**To update a rule:** edit the `SKILL.md` (or copy in the latest version from your synced Claude skills, usually `%USERPROFILE%\.claude\skills\synced\<org-id>\`) and push to `main`. CI rebuilds the app and publishes it to https://denamielleonardo.github.io/reviewkit/.

To add a new review type, add a skill folder, register it in `app/skills.py` (`REVIEWS`), teach `app/detect.py` how to recognise that kind of project, and add its `SKILL.md` to the required list in `.github/workflows/build-and-publish.yml`.
