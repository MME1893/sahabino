# Contributing

## Git Workflow

Development follows an issue-driven workflow:

1. Create or select a GitLab issue.
2. Create a branch from `main`.
3. Implement the change using focused commits.
4. Open a Merge Request.
5. Merge into `main` after CI checks pass.

Avoid committing feature work directly to `main`.

## Branch Naming

Branches should start with the related GitLab issue number followed by a short description.

```text
<issue-number>-<short-description>
```

Examples:

```text
1-bootstrap-project-foundation
2-add-application-registry
3-add-kafka-messaging
```

## Commit Messages

This project follows the Conventional Commits convention.

```text
<type>(<scope>): <description>
```

The scope is optional but recommended when it makes the affected subsystem clearer.

### Types

| Type       | Purpose                                             |
| ---------- | --------------------------------------------------- |
| `feat`     | Introduces a new user-facing or system capability   |
| `fix`      | Fixes a bug                                         |
| `test`     | Adds or modifies tests                              |
| `docs`     | Documentation-only changes                          |
| `refactor` | Changes internal code without changing behavior     |
| `perf`     | Improves performance                                |
| `chore`    | Repository maintenance, dependencies, or tooling    |
| `ci`       | CI/CD configuration or pipeline changes             |
| `build`    | Build system, packaging, or container build changes |

### Common Scopes

Use scopes only when they add useful context.

```text
repo
registry
db
crawler
messaging
ingestion
network
analytics
infra
test
docs
```

New scopes may be introduced when a clear subsystem requires one.

### Examples

```text
chore(repo): bootstrap Python project

chore(tooling): configure pre-commit hooks

feat(registry): add application creation endpoint

feat(db): add application persistence models

feat(crawler): publish Play Store statistics

fix(ingestion): prevent duplicate review revisions

test(network): cover retransmission calculation

ci(gitlab): add lint and test jobs

docs(architecture): document Kafka delivery semantics
```

### Commit Guidelines

A commit should represent one coherent logical change.

Prefer:

```text
feat(registry): add application deactivation
test(registry): cover application deactivation
```

Avoid vague messages such as:

```text
update
changes
fix stuff
final changes
```

Do not split changes into meaningless commits solely to create a longer Git history.

## Before Committing

Run:

```bash
uv run pre-commit run --all-files
```

Before opening a Merge Request, also verify:

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest
```
