# Repository Agent Instructions

## Must Read First

Before changing, committing, pushing, deploying, releasing, or updating this
repository, read `MEMORY.md`.

## Release Rule

For this repository, user requests such as "提交", "推送", "发布", "部署",
"更新", or combinations like "提交推送发布" mean a formal release update unless
the user explicitly says "main only" or "no version bump".

Do not stop after pushing `main` for user-facing changes. The stable update
channel uses the latest GitHub Release tag, and `main` images do not update
`latest`.

Required release flow:

1. Bump `VERSION`.
2. Run `python3 scripts/version.py sync`.
3. Run `python3 scripts/version.py check`.
4. Commit the version bump together with, or immediately after, the code change.
5. Push `main`.
6. Create and push the matching `vX.Y.Z` git tag.
7. Wait for the tag-triggered GitHub Actions `Docker Release` run to complete
   successfully.

Main-branch Docker Release runs are not enough for production/default updates.
