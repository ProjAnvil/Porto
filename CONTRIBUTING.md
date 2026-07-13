# Contributing

Thanks for your interest in contributing to Porto.

## Development Setup

1. Install dependencies:

```bash
uv sync --all-groups
```

2. Run quality checks:

```bash
make check
```

3. Run the local viewer (optional):

```bash
make serve
```

## Pull Request Guidelines

- Keep PRs focused and small.
- Add or update tests for behavior changes.
- Update docs when command behavior or file structure changes.
- Ensure `make check` passes before opening or updating a PR.

## Commit Messages

- Use clear, imperative messages.
- Explain why a change is needed, not only what changed.

## Reporting Issues

- Include reproduction steps.
- Include expected vs actual behavior.
- Add environment details (OS, Python version, uv version).
