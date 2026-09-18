# Sahabino Google Play Crawler Documentation

> **Scope and freshness:** The five in-depth chapters were initially drafted
> against a 2026-09-09 crawler snapshot. Use the checked-out crawler code,
> migrations and current runtime configuration as the source of truth for
> differences introduced after that baseline. This index was reviewed against
> the supplied 2026-09-18 source archive; it does not certify every line of the
> historical deep-dive chapters as current.

## Who these documents are for

This documentation set deliberately serves different readers instead of forcing every reader through the same level of detail.

| Document | Primary audience | Purpose |
| --- | --- | --- |
| [01 - Architecture and System Overview](01-architecture-and-system-overview.md) | Product, operations, new team members, architects, developers | Explain what the crawler is, why it exists, what it owns, how a crawl moves through the system, and how the major resilience mechanisms fit together. The first sections require little or no programming knowledge. |
| [02 - Developer Reference](02-developer-reference.md) | Developers and reviewers | Describe the code structure, ports, policies, adapters, persistence, messaging, runtime composition, important algorithms, and file-by-file responsibilities. |
| [03 - Testing and Quality Guide](03-testing-and-quality.md) | Developers, reviewers, CI maintainers | Explain the testing strategy, test layers, behavior matrix, fixtures, deterministic time/concurrency techniques, external smoke tests, and current coverage boundaries. |
| [04 - Operations and Configuration Guide](04-operations-and-configuration.md) | Developers and operations | Explain how to run the crawler, configure it, provision dependencies, use Docker Compose, understand lifecycle/Kafka behavior, and diagnose common failure modes. |
| [05 - Architecture Decisions, Risks, and Maintenance](05-architecture-decisions-risks-and-maintenance.md) | Architects, senior developers, maintainers | Record the main implementation decisions, trade-offs, constraints, known risks, and safe extension points. |

## Documentation approach

The set borrows from three widely used documentation approaches:

- **arc42** provides the architecture-document structure: goals, constraints, context, building blocks, runtime views, deployment, cross-cutting concepts, quality goals, risks, and glossary.
- **C4** provides the diagramming approach: zoom from system context to containers/components only when that extra detail adds value. Dynamic and deployment diagrams are used for runtime and operational stories.
- **Diátaxis** encourages separation between **explanation** and **reference**. The architecture document explains the system; the developer document is reference-oriented; the operations document is task-oriented.

Official references:

- arc42: <https://arc42.org/> and <https://docs.arc42.org/>
- C4 model: <https://c4model.com/>
- Diátaxis: <https://diataxis.fr/>

## Documentation principles used here

1. **Current behavior over aspiration.** If the initial design and code differ, the code is documented and the difference is called out in the risks/decisions document.
2. **One owner per concern.** Concurrency, retry, network/proxy health, rate limiting, adapter fallback, circuit breaking, lifecycle persistence, and publication are explained separately because the implementation separates them.
3. **Logical attempts are not physical requests.** This distinction is central to understanding lifecycle counters, retries, review fetching, and rate limiting.
4. **Lifecycle data and collected data have different owners.** PostgreSQL records crawler execution state. Kafka carries collected Google Play data.
5. **The secondary adapter is not a networking strategy.** It is an alternate parser/implementation used for a narrow class of adapter failures.
6. **Mermaid blocks are source diagrams.** They are intentionally kept as text so they can be rendered by GitHub, Mermaid CLI, documentation tooling, or replaced with exported images later.

## Suggested repository placement

These files are ready for a docs-as-code layout:

```text
docs/
└── crawler/
    ├── README.md
    ├── 01-architecture-and-system-overview.md
    ├── 02-developer-reference.md
    ├── 03-testing-and-quality.md
    ├── 04-operations-and-configuration.md
    └── 05-architecture-decisions-risks-and-maintenance.md
```

This directory is the crawler landing page (`docs/crawler.md` is not present
in the reviewed archive). Keep cross-subsystem acceptance commands in the
[production checklist](../operations/PRODUCTION_ACCEPTANCE.md); do not
reproduce the mutable production test script in the crawler architecture docs.

## Source scope

The documentation covers:

- `src/sahabino/crawler/**`
- crawler-facing configuration in `src/sahabino/common/config.py`
- crawler lifecycle database support in `src/sahabino/db/sync_session.py` and the crawler Alembic migration
- crawler Kafka contracts and producer behavior in `src/sahabino/messaging/**`
- crawler unit, integration, and external tests
- Docker Compose/runtime commands relevant to the crawler

The downstream ingestion pipeline **is implemented** in
`src/sahabino/ingestion/**`. This crawler guide focuses on the crawler boundary:
its lifecycle is recorded directly in PostgreSQL, while collected application
and review events are published to Kafka for ingestion. For downstream output
verification, Kafka lag and exact-run snapshot/review checks, see
[production acceptance](../operations/PRODUCTION_ACCEPTANCE.md). Standalone
Sentiment and Metabase live outside the crawler subsystem and are documented
in their own READMEs.
