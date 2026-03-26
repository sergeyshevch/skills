---
name: grafana-metrics-extraction
description: >
  Extract all metric names referenced in Grafana dashboards using the Grafana API, with
  incremental caching so only changed dashboards are re-parsed on subsequent runs. Use this
  skill when the user wants to know which metrics are used in Grafana, cross-reference Grafana
  dashboards with TSDB ingestion data, find metrics that are ingested but not used in any
  dashboard, identify broken dashboard panels referencing missing metrics, or prepare for a
  metric cleanup by getting the full list of Grafana-referenced metrics. Also trigger when
  the user mentions "dashboard metrics", "metrics used in Grafana", "grafana metric list",
  "cross-reference Grafana", or "which metrics does Grafana use".
allowed-tools: Bash(python3:*), Bash(curl:*)
---

# Extract Metrics Referenced in Grafana Dashboards

Discover every metric name used across all Grafana dashboards, with smart caching that avoids re-processing unchanged dashboards on subsequent runs.

## Credential Safety

**CRITICAL: NEVER read, display, or log the contents of `.env` files or the values of credential environment variables.** The script auto-loads `.env` from the working directory (or any parent). You do not need to read, source, or inspect `.env` — just run the script.

To verify credentials are configured without exposing them:

```bash
test -n "$GRAFANA_TOKEN" && echo "GRAFANA_TOKEN is set" || echo "GRAFANA_TOKEN is NOT set"
```

## Environment

The script auto-loads variables from the nearest `.env` file (walking up from CWD). You can also set them as shell environment variables.

```bash
# $GRAFANA_URL - Grafana base URL (required)
#   export GRAFANA_URL="https://grafana.example.com"
#
# Authentication (one of the following is required):
#
# $GRAFANA_TOKEN - Grafana service account token (recommended)
#   export GRAFANA_TOKEN="glsa_xxxxxxxxxxxx"
#
# $GRAFANA_AUTH_HEADER - Alternative: full auth header
#   export GRAFANA_AUTH_HEADER="Authorization: Basic dXNlcjpwYXNz"
#
# Optional:
# $GRAFANA_SKIP_TLS_VERIFY - set to "true" to skip TLS certificate verification
```

**Creating a Grafana service account token:**

1. Go to Grafana → Administration → Service accounts → Add service account
2. Role: **Viewer** (read-only access is sufficient)
3. Add token → copy the `glsa_...` value
4. `export GRAFANA_TOKEN="glsa_..."`

**If the user hasn't set these variables**, ask them for the Grafana URL and API token before proceeding. **Never ask the user to paste credentials in chat** — instruct them to add values to `.env` directly.

## How It Works

The extraction script uses the Grafana API to fetch dashboard JSON and parse PromQL/MetricsQL expressions from:

- **Panel targets** (`panels[].targets[].expr`) — the main query expressions
- **Template variables** (`templating.list[].query`) — `label_values()`, `query_result()`, and raw PromQL
- **Annotations** (`annotations.list[].expr`) — annotation queries
- **Nested panels** — panels inside collapsed rows

For each metric, the script also extracts **label names** used alongside it:

- **Selector labels** — from `{job="api", method=~"GET|POST"}` label matchers
- **Aggregation labels** — from `by(method, status)`, `without(instance)`, `on(job)`, `ignoring(le)` clauses
- **label_values() labels** — from `label_values(metric, label_name)` template variable queries

This label data enables downstream optimization: identifying labels that are ingested but never referenced in any dashboard for a given metric.

Only targets using Prometheus-compatible datasources (`prometheus`, `victoriametrics-datasource`) are processed. Panels using Elasticsearch, Loki, CloudWatch, etc. are skipped.

### Caching Strategy

Results are cached per-dashboard as individual files in `<skill_base_dir>/cache/`:

```
cache/
  meta.json                      — Grafana URL + last updated timestamp
  dashboards/
    <uid>.json                   — one file per dashboard
```

Each dashboard file contains:

```json
{
  "version": 42,
  "title": "API Monitoring",
  "folder": "Production",
  "metrics": {
    "http_requests_total": ["job", "method", "status"],
    "http_request_duration_seconds_bucket": ["job", "le", "method"]
  },
  "cached_at": "2026-03-25T10:00:00+00:00"
}
```

Each metric stores its associated label names (from selectors, aggregation clauses, and `label_values()` calls). Dashboard files are written immediately after extraction — if the script is interrupted, already-processed dashboards are preserved. When a dashboard is deleted from Grafana, its cache file is removed. Existing single-file caches (`dashboard-metrics.json`) are automatically migrated on first load.

On each run:

1. **Fetch dashboard list** from `/api/search` (single API call)
2. **Fetch each dashboard** via `/api/dashboards/uid/:uid`
3. **Compare `version`** with cached entry — if unchanged, skip PromQL parsing
4. **Re-parse only changed dashboards** and update cache
5. **Detect deleted dashboards** and remove from cache

This means the first run processes everything, but subsequent runs only re-parse dashboards that were actually modified in Grafana.

## Usage

### Step 1: Run the extraction script

```bash
python3 <skill_base_dir>/scripts/extract_grafana_metrics.py
```

This outputs a human-readable summary: total dashboards, total metrics, per-dashboard breakdown, changes since last run, and the full metric list.

### Available options

```bash
# Default: fetch from Grafana, use cache for unchanged dashboards
python3 <skill_base_dir>/scripts/extract_grafana_metrics.py

# Force re-fetch and re-parse everything (ignore cache)
python3 <skill_base_dir>/scripts/extract_grafana_metrics.py --force-refresh

# Report from cache only (no API calls)
python3 <skill_base_dir>/scripts/extract_grafana_metrics.py --cached-only

# Output as JSON (for programmatic use)
python3 <skill_base_dir>/scripts/extract_grafana_metrics.py --output json

# Output just the metric names, one per line (for piping)
python3 <skill_base_dir>/scripts/extract_grafana_metrics.py --output metrics-only

# Output metrics with their labels in TSV format (metric<TAB>label1,label2,...)
python3 <skill_base_dir>/scripts/extract_grafana_metrics.py --output metrics-with-labels

# Custom cache directory
python3 <skill_base_dir>/scripts/extract_grafana_metrics.py --cache-dir /tmp/my-cache
```

### Step 2: Interpret the results

The summary output shows:

- **Total dashboards** — how many dashboards were found in Grafana
- **Total unique metrics** — deduplicated count of all metric names across all dashboards
- **Total unique labels** — deduplicated count of all label names across all metrics
- **Changes since last run** — new, updated, and deleted dashboards with metric diffs
- **Per-dashboard breakdown** — each dashboard with its metric and label counts
- **Full metric list** — all unique metric names sorted alphabetically, with their associated labels shown in brackets

## Cross-Referencing with a TSDB

After extracting Grafana metrics, you can cross-reference with your TSDB (e.g. VictoriaMetrics, Prometheus, Thanos) to find optimization opportunities.

### Metrics ingested but NOT used in Grafana

```bash
# Get Grafana metrics list
python3 <skill_base_dir>/scripts/extract_grafana_metrics.py --output metrics-only > /tmp/grafana_metrics.txt

# Get ingested metrics from your TSDB (Prometheus-compatible API)
curl -s ${TSDB_AUTH_HEADER:+-H "$TSDB_AUTH_HEADER"} \
  "$TSDB_URL/api/v1/label/__name__/values" | \
  jq -r '.data[]' > /tmp/tsdb_metrics.txt

# Find metrics ingested but not in any dashboard
comm -23 <(sort /tmp/tsdb_metrics.txt) <(sort /tmp/grafana_metrics.txt)

# Find metrics in dashboards but not being ingested (broken panels)
comm -13 <(sort /tmp/tsdb_metrics.txt) <(sort /tmp/grafana_metrics.txt)
```

### Labels optimization

Use the `--output metrics-with-labels` format to find label optimization opportunities:

```bash
# Get metrics with their Grafana-referenced labels
python3 <skill_base_dir>/scripts/extract_grafana_metrics.py --output metrics-with-labels > /tmp/grafana_metrics_labels.tsv

# Or use JSON output for programmatic access to all_metrics_with_labels
python3 <skill_base_dir>/scripts/extract_grafana_metrics.py --output json | jq '.all_metrics_with_labels'
```

Cross-reference with actual ingested labels to find:

- **Unused labels**: labels ingested for a metric but never referenced in any Grafana dashboard, alert, or recording rule → candidates for `metric_relabel_configs` drop
- **High-cardinality labels**: labels with many unique values that are rarely used in dashboards → candidates for aggregation or dropping

### Categorizing metrics by usage

Use the Grafana metrics list alongside TSDB query statistics to distinguish between:

- **Truly unused**: not queried AND not in any Grafana dashboard → safe to drop
- **Dashboard-only**: not queried (because nobody opened the dashboard recently) but present in dashboards → verify before dropping
- **Query-only**: queried by alerts/recording rules but not in dashboards → probably essential

## Limitations and Caveats

- **Grafana variable expansion**: Expressions using template variables (e.g., `$metric_name`) may produce incomplete metric names. The script strips variables but cannot predict their runtime values.
- **Dynamic metric names**: Metrics constructed dynamically via `label_join()` or recording rule outputs may not be detected.
- **Library panels**: Panels shared via Grafana library panels are resolved at render time. The script processes them as they appear in each dashboard's JSON.
- **Non-Prometheus datasources**: Only Prometheus and VictoriaMetrics datasource types are parsed. Loki, Elasticsearch, and other datasource panels are skipped.
- **False positives**: The PromQL parser uses heuristic regex extraction, not a full parser. Occasionally a label name or function argument may be misidentified as a metric. Review the output for obvious non-metrics.

