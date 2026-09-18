# BI reporting catalog and semantic contract

**Implemented as version-controlled SQL and OSS Metabase manifest; NOT verified as deployed.** Files `../questions/q01_*.sql` through `q24_*.sql` are selected through `../manifest/content.json`. The actual Metabase data source must be named `Sahabino BI Source` and use the column-restricted reader. No raw personal review text, author, external review ID, full individual review record, inferred model version, made-up release/event, or PCAP data is surfaced. Historical crawl locale is always from the exact related `crawl_tasks` record. Category is the **current** primary registry category (no historical category table). MyRightel fixture `ir.rightel.myrightel` is excluded from every product-facing result **and peer cohort**, not deleted from source.

## Saved Questions (24)

| ID | Name / purpose | Grain and relevant context |
| --- | --- | --- |
| Q01 | Portfolio — app coverage | One product application, including missing snapshots; latest successful source observation, timestamp, age and current category. |
| Q02 | Store — daily rating trend | App × crawl locale × actually observed UTC day; last observation in that day, raw 0–5 score, no fabricated dates. |
| Q03 | Store — category peer comparison | App × locale, peers have same primary category, latest snapshot day and locale; **one peer is insufficient** and benchmark NULL. |
| Q04 | Store — rating and review count changes | App × locale × UTC observed day; preceding actually observed day retained, separate signed cumulative-count deltas. |
| Q05 | Store — install milestones | App × locale × observed day; `min_installs` is a reported **lower bound**, not exact installs or daily install growth. |
| Q06 | Executive — portfolio summary | All non-fixture apps, present/missing store snapshot counts and latest data freshness. |
| Q07 | Executive — category summary | Current primary category, app count, snapshot coverage and mean latest store score (NULL if no valid sample). |
| Q08 | Store — observed rating changes | App × locale × day, last daily rating minus previous actual observation; no first-observation delta. |
| Q09 | Executive — investigation signals | Most recent app/locale observation: explicit missing baseline, negative count correction, observation gap, or rating change; no composite quality/risk score. |
| Q10 | Review — unique star distribution | Latest eligible observation per **unique review** at end cutoff; 1–5 star distribution by app and selected crawl locale. |
| Q11 | Review — unique cohort KPIs | Unique review denominator, mean stars, shares of 1–2 and 4–5 stars, sample count and freshness. |
| Q12 | Review — true star revisions | Consecutive observation transitions of one review: score changed (upgrade/downgrade), not repeated crawl records. |
| Q13 | Sentiment — coverage and pipeline | One eligible observation per unique review, four separate statuses and valid classified coverage. **Optional.** |
| Q14 | Sentiment — classified labels | Positive/neutral/negative distribution ONLY among `done` with valid labels. **Optional.** |
| Q15 | Sentiment — first-observed cohort trend | Latest eligible review label at cutoff grouped by *first encounter day*, not daily inference events; NULL share on zero classified. **Optional.** |
| Q16 | Sentiment — language breakdown | Classified sample and three-label share by `sentiment_language` (inferred language), distinct from crawl language. **Optional.** |
| Q17 | Review stars × sentiment | Two different measures cross-tabulated only for classified unique reviews, never score-derived label. **Optional.** |
| Q18 | Store — Baham versus Pinno | Real same-day, same historical crawl locale **store** score comparison; independent of insufficient category peer threshold. |
| Q19 | Store — ratings count deltas | Signed cumulative store ratings-count changes with preceding observed baseline and gap size. |
| Q20 | Store — reviews count deltas | Signed cumulative store reviews-count changes, **not unique crawled review count**. |
| Q21 | Store — current install threshold | Latest reported `min_installs` lower bound by app and crawl locale. |
| Q22 | Store — latest rating by application | Latest valid rating by application/locale; raw score and actual collection time. |
| Q23 | Review — newly first observed per day | Earliest actual crawler observation per globally unique review, grouped by its original observation locale and `reviews.first_observed_at` UTC day. |
| Q24 | Review — crawl/sample coverage | Review task success fraction, distinct sampled reviews and most recent sample timestamp, separately aggregated to avoid join fan-out. |

Every question has a descriptive manifest `description`, a stable logical `key`, a read-only SQL file and a visualization setting. Q04 is a table to avoid mixing unlike measures into a single falsely comparable chart. Charts have explicitly named date axes and units where used. Raw score precision is preserved in SQL; UI may format for readability. 15-product dashboard usability requires actual large-sample UI validation (not proven in this environment).

## Four dashboard definitions

**Executive Portfolio:** 6 compact cards covering totals and missing snapshot coverage (Q06), snapshot inventory (Q01), latest ratings (Q22), category summary (Q07), recent changes (Q08), explicit investigation signals (Q09). Present missing data and all actual timeframe/age fields. No aggregate quality or risk score.

**Store Growth & Rating:** 11 cards: Q02/Q22/Q03/Q04/Q05/Q18/Q08/Q19/Q20/Q21 and Q01 for detailed diagnostics. Observed history only, all originally available history by default. Ratings/reviews **published cumulative counts** and their signed changes are never described as installs. A day gap is visible; no seven/28-day period is manufactured.

**Review Intelligence:** 5 cards: Q10/Q11/Q12/Q23/Q24. Runs against base schema at Alembic 0006 with no Sentiment worker prerequisite.

**Sentiment Intelligence (conditional):** 5 cards: Q13–Q17. Separate dashboard prevents missing-column query errors from breaking Review. It is provisioned only when the exact three observation sentiment columns, grants and at least one valid `done` label are detected. An app without classification may have status/coverage in Q13 but no Q14 distribution row; **no 0% negative rate is inferred**. If sentiment was enabled and later removed from the source, managed objects are not auto-deleted; operator must restrict/hide the stale dashboard until restored.

Dashboard grids/positions and per-card template tag mappings live in the manifest. Application filters are package string equality; category is current primary, country and language relate to the historic crawl task; UTC date filters are connected **only** to questions with valid semantics. Q01/Q03/Q06/Q07/latest-state do not get misleading dates. Q04/Q05/Q08/Q09/Q12/Q19/Q20 compute previous observation BEFORE limiting the selected output date, preserving `LAG()` baselines. Q23 selects the globally original locale BEFORE applying locale filter. Category peers Q03 are computed BEFORE filtering the focal package. Optional native `{{tag}}` parameters are mapped as Metabase dashboard variables; field filters are not used on computed dates/CTE aliases because that would give incorrect semantics.

## Review semantics, denominators and timestamps

- `reviews.id` is the stable *internal* unique review key; `review_observations` primary key is (`crawl_task_id`, `review_id`) and records **versions**, not separate reviews. Manager queries never emit this identity. Each latest-state/cohort Review or Sentiment metric chooses **one last eligible observation per unique review** using `(observed_at DESC, crawl_task_id DESC)`, as known up to **inclusive** `end_date` (UTC date); score and sentiment are taken from that observation, never mutable `reviews.score`.
- `source_at` represents purported original source publication time, reliability may vary and it is NOT used for cohort denominators. `observed_at` is crawler's version observation timestamp. `reviews.first_observed_at` is Sahabino's first encounter, not a new review publication or a unique person's lifetime review. Q23 independently ranks original observed versions and preserves its original crawl locale.
- `start_date`: Review/Sentiment cohort includes reviews first encountered on or after that UTC date; `end_date`: last eligible observed version on or before cutoff. Locale filters restrict observation eligibility **before** choosing the last version: a review seen in several locales appears once per filtered locale cohort, and unfiltered global grouping assigns it only to its latest eligible locale. **Never sum locale-filtered report totals as globally distinct reviews.** Historical score under a cutoff comes from that cutoff's observation.
- Q10 distribution denominator = count of distinct eligible reviews with stars 1–5. Q11 **mean** = sum of selected observed stars / eligible unique count; negative review share = count of selected scores 1–2 / eligible count; positive review share = count of scores 4–5 / eligible count; neutral star 3 participates in denominator but neither numerator. An empty cohort produces no application row or NULL aggregate, never a fabricated zero ratio. Sample represents crawled results, NOT the entire store's population.
- Q12 sorts *all* eligible versions of a review by `(observed_at, crawl_task_id)` before `LAG(score)`, counts only `score<>previous_score`, and classifies `>` upgrades, `<` downgrades. Date and locale filters are applied **after** forming actual global consecutive transitions: the transition is attributed to the newly observed version's crawl locale. No current review score reconstructs history. Q23 counts one global earliest observation per review, including original locale; Q24 independently aggregates crawl tasks and unique review sample, then joins summaries only.

## Sentiment semantics and missingness

Q13–Q17 require real 0007/0008-derived schema; current production revision last reported as 0006, **not verified changed**. The ingestion/worker contract stores sentiment status, label and language on `review_observations`, may reuse analysis for identical content and can record a different label for a revised review. There is no persisted reliable model-version provenance. These reports deliberately measure **latest-known review-level sentiment at observation cutoff**, NOT independent sentiment-event frequency or distinct review-version prevalence; one eligible observation per review, not number of `done` rows.

- Pipeline statuses: `pending`, `done`, `skipped`, `failed` counts in Q13 use latest eligible observation for each distinct review. `done` with null/invalid label is **not** a classified result. Coverage = count(`done` AND label ∈ {positive,neutral,negative}) / all eligible unique reviews. Historic migration `skipped` stays missing and is never recoded as neutral or negative.
- Q14 share for each label = unique classified reviews with that label / *only* unique `done` reviews with a valid label (within the same app/locale cohort). Negative sentiment share is the same valid denominator with label negative; Q15 shows this share by *first-observed date cohort*, with `NULL` if classified denominator zero. Shares are not scaled to percentage points in SQL; a UI percentage format may multiply by 100 **for display**.
- Q16 groups valid classified reviews by inferred `sentiment_language`; its sample size is explicitly given per inferred language. `crawl_language_code` reflects requested crawler locale, not inferred text language; `sentiment_language` is **not** the same as country or category. Q17 displays review stars and independently predicted label together without substituting one for the other. Status coverage/sample size must accompany distributions; no model accuracy, topic/network complaint, or inferred model version is claimed. Changing text and labels between observations can alter historical-as-of labels; no historical inference claim without matching snapshot exists.

## Verification, missing data and pending dashboards

See [`../README.md`](../README.md) for test/deployment commands and [`../tests/README.md`](../tests/README.md) for disposable schema coverage. Verify direct PostgreSQL results **as reader**, not only as administrator: sample raw-count corrections, one-peer insufficient benchmark, cross-category dedup, multi-day missing gaps, score revision 1→1→4, review distinct denominators, sentiment skipped/done and denial of all sensitive columns. Compare chart dates, lines, filters, descriptions and actual API dashboard-card IDs in a disposable Metabase first. No actual production dashboard, SQL query result or screenshot has been verified here.

**Evidence-gated definitions:** Q25-Q42 and their three dashboards are implemented. They remain non-interpretable until their named schema, grant, provenance, sample-size, release-evidence and time-window gates pass. Q18 remains Store-only. Sentiment is not topic classification or evidence of a network complaint.


## Network, cross-domain and release questions (Q25-Q42)

These definitions are implemented, but their evidence remains capability-gated. Private manifests are compiled in-process into typed CTEs, then the generated SQL is persisted in Metabase Saved Questions and the private synchronization state; checked-in SQL contains an executable typed-empty relation. Only the reporting-field allowlist is compiled. See [SOURCE_CONTRACT.md](SOURCE_CONTRACT.md) for formulas, persistence/visibility boundaries and [NETWORK_CAPTURE_GUIDE.md](NETWORK_CAPTURE_GUIDE.md) for collection.

| ID | Managerial question | Grain, unit and missing-data rule | Recommended view and filters |
| --- | --- | --- | --- |
| Q25 | What prevents Network reporting? | One readiness row; named schema/grant/data/manifest state. No data is not zero performance. | Table; no filters. |
| Q26 | Which capture/KPI combinations are usable? | Experiment x session x app x scenario x profile x KPI; distinct total/analyzed/eligible captures and reasons. | Table; all Network filters. |
| Q27 | What happened in each transfer trial? | One capture; Mbps, ms and byte ratio. Ineligible effective throughput is NULL; average stays separate. | Evidence table; all Network filters. |
| Q28 | What transfer byte cost was observed? | One capture; IP bytes/file bytes and header ratio 0-1, formatted once in UI. | Table or dot plot; all Network filters. |
| Q29 | What TCP stability evidence exists? | One capture; RTT ms, segment indicator ratios and payload recovery tax. QUIC-only is N/A. | Evidence table; all Network filters. |
| Q30 | Which protocols/capabilities were observable? | One capture; booleans, transport byte shares and warning count. | Table; all Network filters. |
| Q31 | What is the descriptive benchmark? | App x experiment x session x scenario x file/size x app version x device/Android x network type/profile x tool/version x phase; metric-specific n and NULL aggregate under 3. | Summary table; all Network filters. |
| Q32 | What do validated paired trials show? | One pair_id within one allowed opaque cohort and identical experiment/session/scenario/file/device/network conditions; each side and displayed metric validated independently. | Paired table; all Network filters. |
| Q33 | Which apps have Store, Review and Network evidence? | App x latest historical Store locale; independent aggregates, timestamps and source gap. | Coverage table; app/locale. |
| Q34 | Do Store and Network observations align on a UTC day? | App x day x Store locale x scenario/profile. Missing side visible; not correlation. | Timeline table; app/scenario/profile/locale/date. |
| Q35 | How do User Voice cohorts and Network evidence coexist? | App x day x scenario/profile; review score is latest observation strictly before that cohort day's UTC cutoff. | Table; app/scenario/profile/date. |
| Q36 | What evidence exists for the two app pairs? | App x exact network experiment/session/scenario/file/device/version/network condition plus independent Store/Review context; Upload/Download are separate. | Matrix; app/pair/locale. |
| Q37 | Is a release event analyzable? | One event; evidence, precision, verification, windows and states. | Readiness table; app/event/date. |
| Q38 | How did controlled Network metrics differ? | Release x matched scenario/file/device/Android/network/tool conditions; separate observed/eligible/excluded counts and at least 3 per period for each metric, otherwise its delta is NULL. | Table; app/event/scenario/profile/date. |
| Q39 | How did Store observations differ? | Release x historical locale; median daily score and signed endpoint count differences. | Table; app/event/locale/date. |
| Q40 | How did sampled User Voice cohorts differ? | Release x locale; each period has an explicit exclusive UTC cutoff and latest observation by that cutoff. Unavailable historical sentiment remains NULL/not classified. | Table; app/event/locale/date. |
| Q41 | Which evidence domains cover both periods? | App x release x scenario x matched network conditions after joining actual analyzed metric-eligible captures; manifest row count alone never establishes readiness. | Evidence matrix; app/event/date. |
| Q42 | What limits interpretation? | One event; missing evidence and standing limitations. | Table; app/event/date. |

## Added dashboards

**Network Benchmark** orders readiness, performance, efficiency, stability, protocol and evidence sections (Q25-Q32). Filters are application, scenario, experiment, session, network profile and capture date. Without network schema/grants, only readiness remains; no cards are deleted.

**Application Experience - Store x Network x User Voice** contains Q33-Q36 and the two named app pairs. Each fact source is aggregated before joining, Store locale remains visible, and there is no composite score, ranking, correlation or causal claim.

**Release Impact Explorer** contains Q37-Q42. Transition dates are excluded from before/after periods. Outputs are observational. Network complaints remain unavailable without a separate validated topic-annotation source.
