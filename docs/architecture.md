# Architecture

![Architecture v0.2](Architecture_V02.svg)

## Changes since v0.1

The structure of v0.1 held: two sources, one fetcher with date-based routing,
object storage, transform, PostgreSQL with staging and reporting, and an
orchestrator triggering all steps. The implementation confirmed it; the changes
are additions and decisions inside the components.

**Architecture**
- Orchestrator: Kestra, with its own PostgreSQL for metadata, separate from the
  pipeline database.
- Aggregation implemented as a stored procedure inside PostgreSQL, called by Kestra;
  no separate component.
- Orchestrator modes: `daily` (scheduled) and `backfill` (manual) with the modes
  `full`, `fetch_only` and `transform_only` instead of separate rerun paths.

**Inside the components**
- Object storage: SeaweedFS used. Access stays S3-compatible, so the later move to the cloud is
  unaffected.
- Fetcher: on the 1st of a month, yesterday is fetched from the permalink, because
  the previous month's archive is not yet published.
- Transform: lines are additionally identified by stops (`via_bpuic`), because the
  data has no network id and SBB runs an S1 in two networks.
- Data product: median instead of mean; individual weekday as an additional dimension.

Details and reasoning: see the component READMEs.