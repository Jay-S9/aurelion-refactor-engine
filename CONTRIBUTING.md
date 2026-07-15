# Contributing to Aurelion Refactor Engine

Thanks for considering a contribution. This project is maintained by a
single developer, so clear, focused PRs are the fastest way to get
something merged.

## Before you start

- For anything beyond a small fix (a new command, a change to the plan
  format, a new plugin type), open an issue first to discuss the approach
  before writing code. This avoids wasted work on both sides.
- For bug fixes, typo fixes, or doc improvements, a PR without a prior
  issue is fine.

## Development setup

```bash
git clone https://github.com/Jay-S9/aurelion-refactor-engine.git
cd aurelion-refactor-engine
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

## Making changes

1. Fork the repo and create a branch from `main`:
   `git checkout -b fix/short-description`
2. Make your change. Keep the zero-mandatory-dependency principle intact —
   any new runtime dependency needs to be justified in the PR description.
3. Add or update tests for the behavior you changed.
4. Run the test suite before opening a PR:
```bash
   pytest
```
5. Run existing commands against the `examples/` (or equivalent) fixtures
   to confirm dry-run and backup/restore behavior still works as expected.

## Pull requests

- Keep PRs scoped to one change. Large, mixed PRs take longer to review
  and are more likely to get stuck.
- Describe *what* changed and *why* in the PR description — not just a
  restatement of the diff.
- Reference the related issue number if one exists.

## Reporting bugs

Open an issue with:
- The command you ran (full, not paraphrased)
- What you expected to happen
- What actually happened, including the full error output
- Your Python version and OS

## Code of conduct

Be respectful and constructive. Disagreements about implementation
approach are fine and expected; personal attacks are not.
