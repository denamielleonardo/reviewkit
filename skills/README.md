# Review skills (not in this repo)

The review rules are internal to Eclectus Technologies, so they are not committed here. Before running or building ReviewKit, copy these skill folders into this directory:

```
skills/
  capataz-api-reviewer/SKILL.md
  capataz-api-reviewer/examples/review_example.md
  angular-code-reviewer/SKILL.md
  ionic-framework-code-reviewer/SKILL.md
  technical-design-reviewer/SKILL.md
```

You'll find them in your synced Claude skills, usually `%USERPROFILE%\.claude\skills\synced\<org-id>\`, or you can ask the team lead. `build.spec` bundles this folder into the exe, so developers who use the built app don't need these files.
