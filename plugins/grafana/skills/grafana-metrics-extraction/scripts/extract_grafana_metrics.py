#!/usr/bin/env python3
"""
Extract metric names and their associated labels referenced in Grafana dashboards
with incremental caching.

Usage:
    python3 extract_grafana_metrics.py [options]
    python3 extract_grafana_metrics.py --cached-only
    python3 extract_grafana_metrics.py --force-refresh
    python3 extract_grafana_metrics.py --output metrics-only
    python3 extract_grafana_metrics.py --output metrics-with-labels

Environment:
    GRAFANA_URL          Grafana base URL (e.g. https://grafana.example.com)
    GRAFANA_TOKEN        Grafana API token (Bearer auth)
    GRAFANA_AUTH_HEADER  Alternative: full auth header (e.g. "Authorization: Basic ...")
"""

import argparse
import json
import os
import re
import ssl
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path


def _load_dotenv():
    """Load .env file from the current directory or any ancestor, without overwriting existing vars."""
    directory = Path.cwd()
    for candidate in [directory, *directory.parents]:
        env_file = candidate / ".env"
        if env_file.is_file():
            with open(env_file) as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, _, value = line.partition("=")
                    key = key.strip()
                    value = value.strip().strip("'\"")
                    if key and key not in os.environ:
                        os.environ[key] = value
            return

from pathlib import Path

# ---------------------------------------------------------------------------
# PromQL / MetricsQL known identifiers (excluded from metric extraction)
# ---------------------------------------------------------------------------

PROMQL_FUNCTIONS = {
    "abs", "absent", "absent_over_time", "acos", "acosh", "aggr_over_time",
    "any", "ascent_over_time", "asin", "asinh", "atan", "atan2", "atanh",
    "avg", "avg_over_time", "bitmap_and", "bitmap_or", "bitmap_xor",
    "bottomk", "bottomk_avg", "bottomk_last", "bottomk_max", "bottomk_median",
    "bottomk_min", "buckets_limit", "ceil", "changes", "clamp", "clamp_max",
    "clamp_min", "cos", "cosh", "count", "count_eq_over_time",
    "count_gt_over_time", "count_le_over_time", "count_ne_over_time",
    "count_over_time", "count_values", "count_values_over_time",
    "day_of_month", "day_of_week", "day_of_year", "days_in_month",
    "decreases_over_time", "deg", "delta", "deriv", "descent_over_time",
    "distinct", "distinct_over_time", "duration_over_time", "end", "exp",
    "floor", "geomean", "geomean_over_time", "group", "histogram",
    "histogram_avg", "histogram_over_time", "histogram_quantile",
    "histogram_quantiles", "histogram_share", "histogram_stddev",
    "histogram_stdvar", "hoeffding_bound_lower", "hoeffding_bound_upper",
    "holt_winters", "hour", "idelta", "increase", "increases_over_time",
    "integrate", "interpolate", "irate", "keep_last_value", "keep_next_value",
    "label_copy", "label_del", "label_graphite_group", "label_join",
    "label_keep", "label_lowercase", "label_map", "label_match",
    "label_mismatch", "label_move", "label_replace", "label_set",
    "label_transform", "label_uppercase", "label_value", "lag", "last_over_time",
    "lifetime", "limit_offset", "limitk", "ln", "log10", "log2", "mad",
    "mad_over_time", "max", "max_over_time", "median", "median_over_time",
    "min", "min_over_time", "minute", "mode", "mode_over_time", "month",
    "now", "outliers_iqr", "outliers_mad", "outliersk", "pi",
    "predict_linear", "present_over_time", "prometheus_buckets", "quantile",
    "quantile_over_time", "quantiles", "quantiles_over_time", "rad", "rand",
    "rand_exponential", "rand_normal", "range_avg", "range_first",
    "range_last", "range_linear_regression", "range_mad", "range_max",
    "range_median", "range_min", "range_normalize", "range_quantile",
    "range_stddev", "range_stdvar", "range_sum", "range_trim_outliers",
    "range_trim_spikes", "range_trim_zscore", "rate", "remove_resets",
    "resets", "rollup", "rollup_candlestick", "rollup_delta", "rollup_deriv",
    "rollup_increase", "rollup_rate", "rollup_scrape_interval", "round", "ru",
    "running_avg", "running_max", "running_min", "running_sum", "scalar",
    "scrape_interval", "sgn", "share", "share_gt_over_time",
    "share_le_over_time", "sin", "sinh", "smooth_exponential", "sort",
    "sort_by_label", "sort_by_label_desc", "sort_by_label_numeric",
    "sort_by_label_numeric_desc", "sort_desc", "sqrt", "start",
    "stddev", "stddev_over_time", "stdvar", "stdvar_over_time", "step",
    "sum", "sum2", "sum_over_time", "tan", "tanh", "time", "timestamp",
    "timezone_offset", "tmax_over_time", "tmin_over_time",
    "tfirst_over_time", "tlast_over_time", "topk", "topk_avg", "topk_last",
    "topk_max", "topk_median", "topk_min", "union", "vector", "year",
    "zscore", "zscore_over_time",
    # Grafana-specific query functions
    "label_values", "metrics", "query_result",
}

PROMQL_KEYWORDS = {
    "and", "bool", "by", "group_left", "group_right", "ignoring",
    "inf", "nan", "offset", "on", "or", "unless", "with", "without",
}

PROMQL_IDENTIFIERS_LOWER = {f.lower() for f in PROMQL_FUNCTIONS} | {k.lower() for k in PROMQL_KEYWORDS}

PROMETHEUS_DS_TYPES = {"prometheus", "victoriametrics-datasource"}


# ---------------------------------------------------------------------------
# PromQL metric name and label extraction
# ---------------------------------------------------------------------------

def extract_labels_from_selector(selector_content):
    """Extract label names from the content of a {...} selector block.

    Matches identifiers immediately followed by a PromQL matcher operator (=, !=, =~, !~).
    The special __name__ label is excluded.
    """
    labels = set()
    for m in re.finditer(r'([a-zA-Z_][a-zA-Z0-9_]*)\s*[=!~]', selector_content):
        label = m.group(1)
        if label != "__name__":
            labels.add(label)
    return labels


def extract_metrics_from_expr(expr):
    """Extract metric names and associated label names from a PromQL/MetricsQL expression.

    Returns a dict mapping each metric name to a set of label names observed alongside
    that metric — from label matchers ({...}), aggregation clauses (by/without/on/ignoring),
    and label_values() calls.
    """
    if not expr or not isinstance(expr, str):
        return {}

    result = {}

    def add_metric(name, labels=None):
        if name not in result:
            result[name] = set()
        if labels:
            result[name].update(labels)

    # 1. {__name__=~"m1|m2", label="val"} — metrics + labels from the selector block
    for m in re.finditer(r'\{([^}]*__name__\s*=~?\s*"[^"]*"[^}]*)\}', expr):
        block = m.group(1)
        name_match = re.search(r'__name__\s*=~?\s*"([^"]+)"', block)
        if name_match:
            labels = extract_labels_from_selector(block)
            for name in name_match.group(1).split("|"):
                name = name.strip()
                if re.fullmatch(r'[a-zA-Z_:][a-zA-Z0-9_:]*', name):
                    add_metric(name, labels)

    # Fallback: __name__ matchers not inside {} (safety net for edge cases)
    for m in re.finditer(r'__name__\s*=~?\s*"([^"]+)"', expr):
        for name in m.group(1).split("|"):
            name = name.strip()
            if re.fullmatch(r'[a-zA-Z_:][a-zA-Z0-9_:]*', name):
                add_metric(name)

    # 2. metric{label="val"} — metric name + labels from the selector
    for m in re.finditer(r'([a-zA-Z_:][a-zA-Z0-9_:]*)\s*\{([^}]*)\}', expr):
        candidate = m.group(1)
        block = m.group(2)
        if candidate.lower() not in PROMQL_IDENTIFIERS_LOWER:
            labels = extract_labels_from_selector(block)
            add_metric(candidate, labels)

    # 3. label_values(metric{filter}, label) — two-arg form extracts metric + labels
    for m in re.finditer(r'label_values\s*\(([^)]+)\)', expr):
        args_str = m.group(1)
        if "," in args_str:
            parts = args_str.rsplit(",", 1)
            first_arg = parts[0].strip()
            label_arg = parts[1].strip()
            name_match = re.match(r'([a-zA-Z_:][a-zA-Z0-9_:]*)', first_arg)
            if name_match:
                candidate = name_match.group(1)
                if candidate.lower() not in PROMQL_IDENTIFIERS_LOWER:
                    labels = set()
                    if re.fullmatch(r'[a-zA-Z_][a-zA-Z0-9_]*', label_arg):
                        labels.add(label_arg)
                    filter_match = re.search(r'\{([^}]*)\}', first_arg)
                    if filter_match:
                        labels.update(extract_labels_from_selector(filter_match.group(1)))
                    add_metric(candidate, labels)

    # 4. query_result(expr) — recurse into the inner expression
    for m in re.finditer(r'query_result\s*\((.+)\)', expr):
        for name, labels in extract_metrics_from_expr(m.group(1)).items():
            add_metric(name, labels)

    # 5. Collect labels from aggregation clauses: by/without/on/ignoring(label1, label2)
    aggregation_labels = set()
    for m in re.finditer(r'\b(?:by|without|on|ignoring)\s*\(([^)]*)\)', expr):
        for label in re.findall(r'([a-zA-Z_][a-zA-Z0-9_]*)', m.group(1)):
            aggregation_labels.add(label)

    # 6. General extraction: strip special constructs and collect remaining identifiers
    clean = expr
    clean = re.sub(r'\blabel_values\s*\([^)]*\)', '', clean)
    clean = re.sub(r'\bmetrics\s*\([^)]*\)', '', clean)
    clean = re.sub(r'\bquery_result\s*\([^)]*\)', '', clean)
    clean = re.sub(r'\$\{[^}]*\}', '', clean)
    clean = re.sub(r'\$[a-zA-Z_]\w*', '', clean)
    clean = re.sub(r'\[\[([^\]]*)\]\]', '', clean)
    clean = re.sub(r'"[^"]*"', '', clean)
    clean = re.sub(r"'[^']*'", '', clean)
    clean = re.sub(r'#.*$', '', clean, flags=re.MULTILINE)
    clean = re.sub(r'\{[^}]*\}', '', clean)
    clean = re.sub(r'\[[^\]]*\]', '', clean)
    clean = re.sub(r'\b(?:by|without|on|ignoring)\s*\([^)]*\)', '', clean)
    clean = re.sub(r'\b\d+\.?\d*([eE][+-]?\d+)?[smhdwy]?\b', '', clean)

    all_tokens = set(re.findall(r'\b([a-zA-Z_:][a-zA-Z0-9_:]*)\b', clean))
    func_calls = {m.group(1) for m in re.finditer(r'\b([a-zA-Z_:][a-zA-Z0-9_:]*)\s*\(', clean)}

    for token in all_tokens:
        if token.lower() in PROMQL_IDENTIFIERS_LOWER:
            continue
        if token in func_calls:
            continue
        if len(token) <= 1:
            continue
        add_metric(token)

    # 7. Capture metrics immediately before { from the original expression (safety net)
    for m in re.finditer(r'([a-zA-Z_:][a-zA-Z0-9_:]*)\s*\{', expr):
        token = m.group(1)
        if token.lower() not in PROMQL_IDENTIFIERS_LOWER:
            add_metric(token)

    # Associate aggregation labels with all metrics found in this expression
    if aggregation_labels:
        for name in result:
            result[name].update(aggregation_labels)

    return result


# ---------------------------------------------------------------------------
# Grafana API client
# ---------------------------------------------------------------------------

def make_request(url, token=None, auth_header=None):
    """Make an HTTP GET request to Grafana API."""
    req = urllib.request.Request(url)
    req.add_header("Accept", "application/json")
    req.add_header("Content-Type", "application/json")

    if token:
        req.add_header("Authorization", f"Bearer {token}")
    elif auth_header:
        parts = auth_header.split(":", 1)
        if len(parts) == 2:
            req.add_header(parts[0].strip(), parts[1].strip())

    ctx = ssl.create_default_context()
    if os.environ.get("GRAFANA_SKIP_TLS_VERIFY", "").lower() in ("1", "true", "yes"):
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

    with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
        return json.loads(resp.read())


def fetch_dashboard_list(base_url, token=None, auth_header=None):
    """Fetch all dashboards from Grafana search API (handles pagination)."""
    dashboards = []
    page = 1
    limit = 1000

    while True:
        url = f"{base_url.rstrip('/')}/api/search?type=dash-db&limit={limit}&page={page}"
        results = make_request(url, token, auth_header)
        if not results:
            break
        dashboards.extend(results)
        if len(results) < limit:
            break
        page += 1

    return dashboards


def fetch_dashboard_detail(base_url, uid, token=None, auth_header=None):
    """Fetch full dashboard JSON by UID."""
    url = f"{base_url.rstrip('/')}/api/dashboards/uid/{uid}"
    return make_request(url, token, auth_header)


# ---------------------------------------------------------------------------
# Dashboard parsing
# ---------------------------------------------------------------------------

def get_all_panels(dashboard):
    """Recursively collect all panels, including nested row panels."""
    panels = []
    for panel in dashboard.get("panels", []):
        panels.append(panel)
        if panel.get("type") == "row":
            for inner in panel.get("panels", []):
                panels.append(inner)
        elif "panels" in panel:
            for inner in panel.get("panels", []):
                panels.append(inner)
    return panels


def is_prometheus_target(target):
    """Check if a panel target uses a Prometheus-compatible datasource."""
    ds = target.get("datasource")
    if ds is None:
        return target.get("expr") is not None
    if isinstance(ds, str):
        return True  # legacy format, could be anything — include if has expr
    if isinstance(ds, dict):
        ds_type = ds.get("type", "")
        if not ds_type:
            return target.get("expr") is not None
        return ds_type in PROMETHEUS_DS_TYPES
    return False


def extract_metrics_from_dashboard(dashboard_data):
    """Extract all metric names and their associated labels from a Grafana dashboard."""
    metrics = {}  # {metric_name: set(label_names)}
    dashboard = dashboard_data.get("dashboard", {})

    def merge_metrics(new_metrics):
        for name, labels in new_metrics.items():
            if name not in metrics:
                metrics[name] = set()
            metrics[name].update(labels)

    for panel in get_all_panels(dashboard):
        for target in panel.get("targets", []):
            if not is_prometheus_target(target):
                continue
            expr = target.get("expr", "")
            if expr:
                merge_metrics(extract_metrics_from_expr(expr))

    for var in dashboard.get("templating", {}).get("list", []):
        ds = var.get("datasource")
        ds_ok = True
        if isinstance(ds, dict):
            ds_type = ds.get("type", "")
            if ds_type and ds_type not in PROMETHEUS_DS_TYPES:
                ds_ok = False

        if ds_ok:
            query = var.get("query", "")
            if isinstance(query, dict):
                query = query.get("query", "")
            if isinstance(query, str) and query:
                merge_metrics(extract_metrics_from_expr(query))

    for ann in dashboard.get("annotations", {}).get("list", []):
        expr = ann.get("expr", "")
        if not expr:
            target = ann.get("target", {})
            if isinstance(target, dict):
                expr = target.get("expr", "")
        if expr:
            merge_metrics(extract_metrics_from_expr(expr))

    return metrics


# ---------------------------------------------------------------------------
# Cache management
# ---------------------------------------------------------------------------


class DashboardCache:
    """Directory-based cache: one small JSON file per dashboard + a metadata file.

    Layout:
        <cache_dir>/
            meta.json                  — grafana_url, last_updated
            dashboards/<uid>.json      — per-dashboard extraction result
    """

    def __init__(self, cache_dir):
        self.dir = Path(cache_dir)
        self.dashboards_dir = self.dir / "dashboards"
        self.meta_path = self.dir / "meta.json"
        self._migrate_from_single_file()
        self.meta = self._load_meta()
        self.entries = self._load_entries()

    # -- loading --

    def _load_meta(self):
        if self.meta_path.exists():
            try:
                with open(self.meta_path) as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                pass
        return {"grafana_url": "", "last_updated": ""}

    def _load_entries(self):
        entries = {}
        if not self.dashboards_dir.exists():
            return entries
        for p in sorted(self.dashboards_dir.glob("*.json")):
            uid = p.stem
            try:
                with open(p) as f:
                    data = json.load(f)
                m = data.get("metrics", {})
                if isinstance(m, list):
                    data["metrics"] = {name: [] for name in m}
                entries[uid] = data
            except (json.JSONDecodeError, OSError):
                pass
        return entries

    def _migrate_from_single_file(self):
        """Auto-migrate from the old single-file cache format (dashboard-metrics.json)."""
        old_file = self.dir / "dashboard-metrics.json"
        if not old_file.exists():
            return
        try:
            with open(old_file) as f:
                data = json.load(f)
            self.dir.mkdir(parents=True, exist_ok=True)
            self.dashboards_dir.mkdir(parents=True, exist_ok=True)
            meta = {
                "grafana_url": data.get("grafana_url", ""),
                "last_updated": data.get("last_updated", ""),
            }
            with open(self.meta_path, "w") as f:
                json.dump(meta, f, indent=2)
            count = 0
            for uid, entry in data.get("dashboards", {}).items():
                m = entry.get("metrics", [])
                if isinstance(m, list):
                    entry["metrics"] = {name: [] for name in m}
                with open(self.dashboards_dir / f"{uid}.json", "w") as f:
                    json.dump(entry, f, indent=2)
                count += 1
            old_file.rename(old_file.with_suffix(".json.migrated"))
            print(f"  Migrated {count} dashboards from old single-file cache", file=sys.stderr)
        except (json.JSONDecodeError, OSError) as e:
            print(f"  Warning: failed to migrate old cache: {e}", file=sys.stderr)

    # -- read --

    def get(self, uid):
        return self.entries.get(uid)

    def all_uids(self):
        return set(self.entries.keys())

    def dashboard_count(self):
        return len(self.entries)

    # -- write (each call writes one small file) --

    def put(self, uid, version, title, folder, metrics):
        """Store extraction results and persist to disk immediately."""
        entry = {
            "version": version,
            "title": title,
            "folder": folder,
            "metrics": {m: sorted(labels) for m, labels in sorted(metrics.items())},
            "cached_at": datetime.now(timezone.utc).isoformat(),
        }
        self.entries[uid] = entry
        self.dashboards_dir.mkdir(parents=True, exist_ok=True)
        with open(self.dashboards_dir / f"{uid}.json", "w") as f:
            json.dump(entry, f, indent=2)

    def remove(self, uid):
        self.entries.pop(uid, None)
        path = self.dashboards_dir / f"{uid}.json"
        if path.exists():
            path.unlink()

    def save(self):
        """Persist metadata. Dashboard entries are already saved in put()/remove()."""
        self.dir.mkdir(parents=True, exist_ok=True)
        with open(self.meta_path, "w") as f:
            json.dump(self.meta, f, indent=2)

    def set_metadata(self, grafana_url):
        self.meta["grafana_url"] = grafana_url
        self.meta["last_updated"] = datetime.now(timezone.utc).isoformat()

    # -- aggregation --

    def _iter_metrics(self):
        """Yield (metric_name, labels_collection) from every dashboard entry."""
        for entry in self.entries.values():
            m = entry.get("metrics", {})
            if isinstance(m, list):
                for name in m:
                    yield name, []
            else:
                for name, labels in m.items():
                    yield name, labels

    def all_metrics(self):
        """Return a sorted list of all unique metric names across dashboards."""
        return sorted({name for name, _ in self._iter_metrics()})

    def all_metrics_with_labels(self):
        """Return {metric: sorted_label_list} merged across all dashboards."""
        result = {}
        for name, labels in self._iter_metrics():
            if name not in result:
                result[name] = set()
            result[name].update(labels)
        return {name: sorted(labels) for name, labels in sorted(result.items())}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _metric_names_from_entry(entry):
    """Extract the set of metric names from a cache entry's metrics field."""
    if entry is None:
        return set()
    m = entry.get("metrics", {})
    if isinstance(m, list):
        return set(m)
    return set(m.keys())


# ---------------------------------------------------------------------------
# Main extraction logic
# ---------------------------------------------------------------------------

DEFAULT_WORKERS = os.cpu_count() or 4


def _fetch_and_parse(base_url, uid, token, auth_header):
    """Fetch a single dashboard and extract its metrics. Thread-safe (no shared state)."""
    detail = fetch_dashboard_detail(base_url, uid, token, auth_header)
    version = detail.get("meta", {}).get("version", 0)
    metrics = extract_metrics_from_dashboard(detail)
    return uid, version, metrics


def run_extraction(base_url, token, auth_header, cache, force_refresh=False, workers=DEFAULT_WORKERS):
    """Fetch dashboards, compare with cache, extract metrics from changed ones."""
    search_results = fetch_dashboard_list(base_url, token, auth_header)
    print(f"Found {len(search_results)} dashboards in Grafana", file=sys.stderr)

    current_uids = {d["uid"] for d in search_results}
    cached_uids = cache.all_uids()

    new_uids = current_uids - cached_uids
    deleted_uids = cached_uids - current_uids

    search_meta = {d["uid"]: d for d in search_results}

    stats = {"fetched": 0, "cached": 0, "new": 0, "updated": 0, "deleted": 0, "errors": 0}
    changed_dashboards = []

    for uid in deleted_uids:
        cached = cache.get(uid)
        changed_dashboards.append({
            "uid": uid,
            "title": cached.get("title", "?") if cached else "?",
            "action": "deleted",
            "old_metrics": _metric_names_from_entry(cached),
            "new_metrics": set(),
        })
        cache.remove(uid)
        stats["deleted"] += 1

    uids_to_fetch = list(current_uids) if force_refresh else list(current_uids)
    total = len(uids_to_fetch)
    done = 0
    lock = threading.Lock()

    print(f"  Processing {total} dashboards with {workers} workers ...", file=sys.stderr)

    def _progress():
        return (
            f"  [{done}/{total}] "
            f"cached={stats['cached']} new={stats['new']} updated={stats['updated']} errors={stats['errors']}"
        )

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {}
        for uid in uids_to_fetch:
            # Skip dashboards whose cached version already matches (avoid submitting work)
            if not force_refresh:
                cached_entry = cache.get(uid)
                sm = search_meta.get(uid, {})
                # The search API doesn't return version, so we must fetch to compare.
                # But if the dashboard exists in cache, we still submit the fetch —
                # the version check happens after fetching.
                pass
            futures[pool.submit(_fetch_and_parse, base_url, uid, token, auth_header)] = uid

        for future in as_completed(futures):
            uid = futures[future]
            meta = search_meta.get(uid, {})
            title = meta.get("title", "?")
            folder = meta.get("folderTitle", "General")

            with lock:
                done += 1
                if done % 50 == 0 or done == total:
                    print(_progress(), file=sys.stderr)

            try:
                uid, version, metrics = future.result()
            except (urllib.error.URLError, urllib.error.HTTPError, OSError) as e:
                print(f"  ERROR fetching {uid} ({title}): {e}", file=sys.stderr)
                with lock:
                    stats["errors"] += 1
                continue
            except Exception as e:
                print(f"  ERROR processing {uid} ({title}): {e}", file=sys.stderr)
                with lock:
                    stats["errors"] += 1
                continue

            with lock:
                stats["fetched"] += 1
                cached_entry = cache.get(uid)

                if not force_refresh and cached_entry and cached_entry.get("version") == version:
                    stats["cached"] += 1
                    continue

                old_metric_names = _metric_names_from_entry(cached_entry)

                if uid in new_uids:
                    action = "new"
                    stats["new"] += 1
                else:
                    action = "updated"
                    stats["updated"] += 1

                changed_dashboards.append({
                    "uid": uid,
                    "title": title,
                    "folder": folder,
                    "action": action,
                    "old_metrics": old_metric_names,
                    "new_metrics": set(metrics.keys()),
                })

                cache.put(uid, version, title, folder, metrics)

    print(
        f"  Done: fetched={stats['fetched']} cached={stats['cached']} "
        f"new={stats['new']} updated={stats['updated']} deleted={stats['deleted']} errors={stats['errors']}",
        file=sys.stderr,
    )
    cache.set_metadata(base_url)
    cache.save()

    return stats, changed_dashboards


# ---------------------------------------------------------------------------
# Output formatters
# ---------------------------------------------------------------------------

def print_summary(cache, stats, changed_dashboards):
    """Print a human-readable summary."""
    all_mwl = cache.all_metrics_with_labels()

    print(f"\n{'=' * 60}")
    print("Grafana Metrics Extraction Report")
    print(f"{'=' * 60}")
    print(f"  Grafana URL:        {cache.meta.get('grafana_url', '?')}")
    print(f"  Last updated:       {cache.meta.get('last_updated', '?')}")
    print(f"  Total dashboards:   {cache.dashboard_count()}")
    print(f"  Total unique metrics: {len(all_mwl)}")
    total_labels = len({l for labels in all_mwl.values() for l in labels})
    print(f"  Total unique labels:  {total_labels}")
    print()

    if stats:
        print(f"  Dashboards fetched: {stats['fetched']}")
        print(f"  Unchanged (cached): {stats['cached']}")
        print(f"  New dashboards:     {stats['new']}")
        print(f"  Updated dashboards: {stats['updated']}")
        print(f"  Deleted dashboards: {stats['deleted']}")
        print(f"  Fetch errors:       {stats['errors']}")
        print()

    if changed_dashboards:
        print(f"{'─' * 60}")
        print("Changes since last run:")
        print(f"{'─' * 60}")
        for ch in changed_dashboards:
            action = ch["action"]
            title = ch["title"]
            if action == "deleted":
                removed = ch["old_metrics"]
                print(f"  DELETED: {title}")
                if removed:
                    print(f"    Metrics removed: {len(removed)}")
            elif action == "new":
                added = ch["new_metrics"]
                print(f"  NEW: {title} ({ch.get('folder', '')})")
                if added:
                    print(f"    Metrics: {len(added)}")
            else:
                added = ch["new_metrics"] - ch["old_metrics"]
                removed = ch["old_metrics"] - ch["new_metrics"]
                if added or removed:
                    print(f"  UPDATED: {title}")
                    if added:
                        print(f"    + {len(added)} new metrics: {', '.join(sorted(added)[:10])}")
                        if len(added) > 10:
                            print(f"      ... and {len(added) - 10} more")
                    if removed:
                        print(f"    - {len(removed)} removed metrics: {', '.join(sorted(removed)[:10])}")
                        if len(removed) > 10:
                            print(f"      ... and {len(removed) - 10} more")
        print()

    print(f"{'─' * 60}")
    print("Metrics per dashboard:")
    print(f"{'─' * 60}")
    dashboards = cache.entries
    for uid in sorted(dashboards, key=lambda u: dashboards[u].get("title", "")):
        entry = dashboards[uid]
        title = entry.get("title", "?")
        folder = entry.get("folder", "")
        m = entry.get("metrics", {})
        metric_count = len(m) if isinstance(m, dict) else len(m)
        if isinstance(m, dict):
            label_count = len({l for labels in m.values() for l in labels})
        else:
            label_count = 0
        prefix = f"[{folder}] " if folder and folder != "General" else ""
        print(f"  {prefix}{title}: {metric_count} metrics, {label_count} labels")
    print()

    print(f"{'─' * 60}")
    print(f"All unique metrics ({len(all_mwl)}):")
    print(f"{'─' * 60}")
    for m, labels in all_mwl.items():
        if labels:
            print(f"  {m}  [{', '.join(labels)}]")
        else:
            print(f"  {m}")


def print_json(cache, stats, changed_dashboards):
    """Print full results as JSON."""
    all_mwl = cache.all_metrics_with_labels()

    output = {
        "grafana_url": cache.meta.get("grafana_url", ""),
        "last_updated": cache.meta.get("last_updated", ""),
        "total_dashboards": cache.dashboard_count(),
        "total_unique_metrics": len(all_mwl),
        "all_metrics": sorted(all_mwl.keys()),
        "all_metrics_with_labels": all_mwl,
        "dashboards": {},
    }

    if stats:
        output["stats"] = stats

    if changed_dashboards:
        output["changes"] = [
            {
                "uid": ch["uid"],
                "title": ch["title"],
                "action": ch["action"],
                "added_metrics": sorted(ch["new_metrics"] - ch["old_metrics"]) if ch["action"] != "deleted" else [],
                "removed_metrics": sorted(ch["old_metrics"] - ch["new_metrics"]),
            }
            for ch in changed_dashboards
        ]

    for uid, entry in cache.entries.items():
        m = entry.get("metrics", {})
        if isinstance(m, list):
            m = {name: [] for name in m}
        output["dashboards"][uid] = {
            "title": entry.get("title", ""),
            "folder": entry.get("folder", ""),
            "metrics": m,
        }

    print(json.dumps(output, indent=2))


def print_metrics_only(cache):
    """Print one metric name per line (for piping to other tools)."""
    for m in cache.all_metrics():
        print(m)


def print_metrics_with_labels(cache):
    """Print metric names with their labels in TSV format (metric<TAB>label1,label2,...)."""
    for name, labels in cache.all_metrics_with_labels().items():
        if labels:
            print(f"{name}\t{','.join(labels)}")
        else:
            print(name)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    _load_dotenv()

    parser = argparse.ArgumentParser(
        description="Extract metric names and labels from Grafana dashboards with caching."
    )
    parser.add_argument(
        "--cache-dir",
        default=None,
        help="Path to cache directory (default: <script_dir>/cache)",
    )
    parser.add_argument(
        "--force-refresh",
        action="store_true",
        help="Ignore cached versions, re-fetch and re-parse all dashboards",
    )
    parser.add_argument(
        "--cached-only",
        action="store_true",
        help="Report from cache only, no Grafana API calls",
    )
    parser.add_argument(
        "--output",
        choices=["summary", "json", "metrics-only", "metrics-with-labels"],
        default="summary",
        help="Output format (default: summary)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help=f"Number of parallel workers for fetching dashboards (default: {DEFAULT_WORKERS})",
    )

    args = parser.parse_args()

    # Resolve cache directory
    if args.cache_dir:
        cache_dir = Path(args.cache_dir)
    else:
        script_dir = Path(__file__).resolve().parent
        cache_dir = script_dir / "cache"

    cache = DashboardCache(cache_dir)

    if args.cached_only:
        if cache.dashboard_count() == 0:
            print("Cache is empty. Run without --cached-only first.", file=sys.stderr)
            sys.exit(1)
        if args.output == "summary":
            print_summary(cache, stats=None, changed_dashboards=[])
        elif args.output == "json":
            print_json(cache, stats=None, changed_dashboards=[])
        elif args.output == "metrics-with-labels":
            print_metrics_with_labels(cache)
        else:
            print_metrics_only(cache)
        return

    # Read config from environment
    base_url = os.environ.get("GRAFANA_URL", "").rstrip("/")
    token = os.environ.get("GRAFANA_TOKEN", "")
    auth_header = os.environ.get("GRAFANA_AUTH_HEADER", "")

    if not base_url:
        print("ERROR: GRAFANA_URL environment variable is required.", file=sys.stderr)
        print("  export GRAFANA_URL='https://grafana.example.com'", file=sys.stderr)
        sys.exit(1)

    if not token and not auth_header:
        print("ERROR: Set GRAFANA_TOKEN or GRAFANA_AUTH_HEADER for authentication.", file=sys.stderr)
        print("  export GRAFANA_TOKEN='glsa_...'", file=sys.stderr)
        sys.exit(1)

    stats, changed = run_extraction(base_url, token, auth_header, cache, args.force_refresh, args.workers)

    if args.output == "summary":
        print_summary(cache, stats, changed)
    elif args.output == "json":
        print_json(cache, stats, changed)
    elif args.output == "metrics-with-labels":
        print_metrics_with_labels(cache)
    else:
        print_metrics_only(cache)


if __name__ == "__main__":
    main()
