# Contributing to SearchOps

SearchOps is a personal-tool-turned-open-source project. Contributions are welcome
with a few ground rules.

## What we're looking for

- Bug fixes (especially in the scoring engine or discovery layer)
- New LLM provider implementations (see `app/providers/`)
- ATS client adapters for additional job boards (see `app/discovery/ats_clients.py`)
- Documentation improvements
- Test coverage for edge cases

## What we're not looking for (yet)

- UI redesigns without prior discussion
- ORM migrations (hand-written SQL is intentional)
- New external service dependencies without a strong reason

## Getting started

1. Fork the repo and create a branch from `main`.
2. Follow the [Quickstart](README.md#quickstart-local-dev) to run locally.
3. Make your change and add tests if the change touches scoring logic or routes.
4. Run `pytest tests/` — all tests must pass.
5. Open a pull request with a short description of what and why.

## Code style

- Match the style in the file you're editing.
- No reformatting for its own sake.
- Comments only when the **why** is non-obvious.
- See `AGENTS.md` for agent-specific conventions.

## Submitting a pull request

- Keep PRs focused — one concern per PR.
- Include a brief description of the change and how to test it.
- If the change affects scoring output, include before/after score examples.

## Reporting bugs

Use the GitHub issue templates. Include:
- What you did
- What you expected
- What actually happened
- Relevant logs or screenshots

## License

By contributing, you agree that your contributions will be licensed under the
project's [MIT License](LICENSE).
