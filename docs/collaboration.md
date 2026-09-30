# Collaboration & GitHub workflow

Aligned with the ETEC306 “Introduction to Using GitHub in Engineering Projects” workbook.

## Version control

1. Work on a feature branch (`feature/...` or `docs/...`), not directly on `main` for larger changes.
2. Commit often with meaningful messages (what changed and why).
3. Open a pull request for review before merging into `main`.

## Issues & milestones

- Create an Issue for each concrete task (hardware, firmware, docs, bugs).
- Assign an owner; reference the issue from commits (`Refs #2`) or close it from a PR (`Closes #2`).
- Group Issues into milestones that match project phases when useful.

## Project board

Use a GitHub Project with columns:

| Column | Meaning |
| --- | --- |
| To Do | Agreed work not started |
| In Progress | Actively owned this week |
| Done | Merged / verified |

Move cards as status changes so the board reflects reality for demos and grading screenshots.

## Documentation

Keep `README.md` and `docs/` current before any presentation. Prefer short, accurate updates over long stale notes.
