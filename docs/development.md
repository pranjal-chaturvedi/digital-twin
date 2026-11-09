# Development

How to run the digital twin locally and test it.

## Running locally

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

1. Create a `.env` file in the repo root with:

   | Variable | Description |
   |---|---|
   | `MODEL_NAME` | Model to use |
   | `BASE_URL` | Base URL of the OpenAI-compatible API |
   | `MODEL_API_KEY` | API key for that provider |
   | `HF_DATASET_REPO` | Dataset for stored records, e.g. `<user>/digital-twin-data`; it's created as private if it doesn't exist |
   | `HF_TOKEN` | Hugging Face access token with write access to that dataset (a fine-grained token scoped to that one repo is best) |

   Optional limits, such as `MAX_TOOL_ROUNDS` and the abuse-protection settings, are listed in [Protections](protections.md#configuration).

2. Run:

   ```bash
   uv run python app.py
   ```

## Tests

```bash
uv run pytest
```

The suite needs no API keys or network access: the model is replaced with a fake, and nothing is written to the dataset.

It includes adversarial cases from `tests/fixtures/adversarial.json`: prompt extraction, jailbreaks, contact spam, forged headers, and long and multilingual input. `tests/test_adversarial.py` runs each one through the real chat handler and checks what the code guarantees, whatever the model does.

## Live tests against the real model

The same adversarial cases can run against the real model, to check how it behaves (for example, whether it keeps its instructions private). This calls the API and **costs money**, so it's skipped unless asked for:

```bash
uv run pytest --live -m live
```

- It stops after `LIVE_SMOKE_MAX_CALLS` model calls in total (default 40).
- Nothing is stored: the tools write to memory, not the dataset.
- It fails if a reply quotes the system prompt's instructions, or if more records are stored than a case allows.
- Every reply is written to `.context/smoke/` for review.

## Branches, CI and releases

- `develop` is the default branch. Create `feature/*` and `hotfix/*` branches from it and open pull requests back into it.
- `main` is production: [Render](https://render.com) deploys every push to `main` by itself. Nothing in this repo deploys.
- CI (`.github/workflows/ci.yml`) runs the tests on every push to `develop` or `main` and on every pull request into them. It needs no secrets.

To release:

1. On `develop`, bump `version` in `pyproject.toml`, and in [CHANGELOG.md](../CHANGELOG.md) rename `## [Unreleased]` to `## [x.y.z] - YYYY-MM-DD`, start a new empty `## [Unreleased]`, and update the links at the bottom.
2. Merge `develop` into `main`. Render deploys it, and CI, once the tests pass, creates the GitHub release `vx.y.z` with that CHANGELOG section as its notes.

If the version is already tagged, CI skips the release, so merges without a version bump (for example, a docs fix) don't create one. If the CHANGELOG has no section for the new version, the release step fails and says so.

Render deploys `main` whether or not CI passes. To stop failing code reaching production, protect `main` on GitHub so pull requests need the CI `test` check to pass before merging, or set Render to deploy only after CI checks pass.
