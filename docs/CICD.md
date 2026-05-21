# CI/CD

Status: **implemented.** `ci.yml`, `release.yml` and `dev.yml` live in
`.github/workflows/`. CI runs on every push and pull request right away. The
release and dev workflows need two Docker Hub secrets on the repo before they
can publish — see *Secrets* below.

## Why

Before this, tests, image builds and registry pushes were all manual from a
laptop. That cost real time:

- **Architecture mismatch.** `docker build` on an Apple Silicon laptop
  produces an `arm64` image; the deployment runs on `amd64` nodes, so the pod
  crashed at startup with `exec format error` and the image had to be rebuilt.
- **Manual version sync.** The version lives in `pyproject.toml`, the git tag
  and the Docker image tag, kept in step by hand with nothing catching a slip.
- **Tests were opt-in.** The suite ran via `make test` only when someone
  remembered; nothing blocked an untested change.

## CI — `ci.yml`

Runs on every `push` and `pull_request`. Two jobs:

- **test** — a Python matrix (3.10–3.13, matching
  `requires-python = ">=3.10"`): checkout → `setup-python` with pip cache →
  `pip install -e ".[test]"` → `pytest`.
- **lint** — `astral-sh/ruff-action`, a blocking gate. The existing code was
  cleaned first (15 ruff violations fixed) so the gate passes from day one.
  Ruff config lives in `pyproject.toml` under `[tool.ruff]`.

## CD — `release.yml`

Runs on a pushed tag matching `v*`:

1. **Version guard** — assert the git tag (`vX.Y.Z`) and the
   `pyproject.toml` version agree; fail fast on a mismatch.
2. `setup-qemu` + `setup-buildx` for multi-platform builds.
3. Log in to Docker Hub with repo secrets.
4. `metadata-action` derives the image tag from the git tag.
5. `build-push-action` builds `linux/amd64,linux/arm64` with a GHA layer cache
   and pushes `xieyanbo/slack-log:<version>`. No `:latest` is published —
   deployments pin exact versions, and a floating `latest` is an easy way to
   ship the wrong thing.
6. A GitHub Release is created for the tag with auto-generated notes.

Multi-arch is deliberate: it makes the architecture mismatch above
structurally impossible. The `arm64` half builds under QEMU emulation and
adds a few minutes per release — acceptable for a small image that ships
infrequently.

## Dev images — `dev.yml`

`release.yml` only fires on a version tag. To try a change on a test
deployment without cutting a release, `dev.yml` builds an unversioned image
on demand. By design CI tests run automatically on every push and PR, but
building a deployable image stays a manual step.

- **Trigger** — manual only (`workflow_dispatch`). Run it from the Actions
  tab, or `gh workflow run dev.yml -f tag=dev`.
- **Any branch, including a PR.** `workflow_dispatch` builds whatever ref it
  is pointed at: `gh workflow run dev.yml --ref <branch> -f tag=pr-123`
  builds that branch's code, so an open PR can go to a test target before it
  merges — no need to merge to main first.
- **Build** — single-arch `linux/amd64` (matches the cluster, no QEMU — much
  faster than a release build), pushed to `xieyanbo/slack-log:<tag>`. The
  `tag` input defaults to `dev`.
- No version guard, no GitHub Release — it is not a release.

Deploy a dev image to a test target with
`kubectl set image deployment/<name> <container>=xieyanbo/slack-log:<tag>`.

## Secrets

The release and dev workflows need two GitHub repository secrets
(Settings → Secrets and variables → Actions):

- `DOCKERHUB_USERNAME` — `xieyanbo`
- `DOCKERHUB_TOKEN` — a Docker Hub access token with **Read & Write** scope
  (the workflows only push images; Delete is not needed)

Until these are set, `ci.yml` works but `release.yml` cannot push.

## Cutting a release

Once the secrets are in place:

1. Bump `version` in `pyproject.toml`.
2. Commit, then tag: `git tag vX.Y.Z`.
3. Push the tag: `git push origin vX.Y.Z`.

The release workflow does the rest — version guard, multi-arch build, push,
GitHub Release. The three version numbers (`pyproject.toml`, git tag, image
tag) can no longer drift: the guard fails the build on a mismatch.

## Action versions

Actions are pinned to major-version tags (`@v4`, `@v6`, …). Tighten to commit
SHAs later if supply-chain pinning becomes a requirement.
