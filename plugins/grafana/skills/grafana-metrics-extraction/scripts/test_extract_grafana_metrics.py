#!/usr/bin/env python3
"""Tests for extract_grafana_metrics.py."""

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from extract_grafana_metrics import (
    DashboardCache,
    extract_labels_from_selector,
    extract_metrics_from_dashboard,
    extract_metrics_from_expr,
    get_all_panels,
    is_prometheus_target,
    _metric_names_from_entry,
)


# ---------------------------------------------------------------------------
# extract_labels_from_selector
# ---------------------------------------------------------------------------

class TestExtractLabelsFromSelector:
    def test_simple_equality(self):
        assert extract_labels_from_selector('job="api"') == {"job"}

    def test_regex_match(self):
        assert extract_labels_from_selector('method=~"GET|POST"') == {"method"}

    def test_negative_equality(self):
        assert extract_labels_from_selector('status!="500"') == {"status"}

    def test_negative_regex(self):
        assert extract_labels_from_selector('code!~"5.."') == {"code"}

    def test_multiple_labels(self):
        labels = extract_labels_from_selector('job="api", method=~"GET", status!="500"')
        assert labels == {"job", "method", "status"}

    def test_excludes_dunder_name(self):
        labels = extract_labels_from_selector('__name__="foo", job="bar"')
        assert labels == {"job"}

    def test_empty_string(self):
        assert extract_labels_from_selector("") == set()

    def test_whitespace_before_operator(self):
        assert extract_labels_from_selector('job = "api"') == {"job"}


# ---------------------------------------------------------------------------
# extract_metrics_from_expr — metric extraction (return dict)
# ---------------------------------------------------------------------------

class TestExtractMetricsFromExpr:

    # -- Basic return type --

    def test_returns_dict(self):
        result = extract_metrics_from_expr("up")
        assert isinstance(result, dict)

    def test_empty_input(self):
        assert extract_metrics_from_expr("") == {}
        assert extract_metrics_from_expr(None) == {}
        assert extract_metrics_from_expr(42) == {}

    # -- Simple metrics --

    def test_bare_metric(self):
        r = extract_metrics_from_expr("up")
        assert "up" in r

    def test_two_bare_metrics(self):
        r = extract_metrics_from_expr("metric_a + metric_b")
        assert "metric_a" in r
        assert "metric_b" in r

    def test_metric_with_rate(self):
        r = extract_metrics_from_expr("rate(http_requests_total[5m])")
        assert "http_requests_total" in r

    def test_metric_with_colon(self):
        r = extract_metrics_from_expr("namespace:container_cpu_usage:sum")
        assert "namespace:container_cpu_usage:sum" in r

    # -- Labels from selectors --

    def test_labels_from_simple_selector(self):
        r = extract_metrics_from_expr('http_requests_total{method="GET", status="200"}')
        assert r["http_requests_total"] == {"method", "status"}

    def test_labels_from_regex_selector(self):
        r = extract_metrics_from_expr('up{job=~"api|web"}')
        assert r["up"] == {"job"}

    def test_labels_from_rate_with_selector(self):
        r = extract_metrics_from_expr('rate(http_requests_total{method="GET"}[5m])')
        assert "http_requests_total" in r
        assert "method" in r["http_requests_total"]

    def test_no_labels_for_bare_metric(self):
        r = extract_metrics_from_expr("go_goroutines")
        assert r["go_goroutines"] == set()

    # -- Labels from aggregation clauses --

    def test_by_clause_labels(self):
        r = extract_metrics_from_expr('sum by (method, code) (rate(http_requests_total{job="api"}[5m]))')
        labels = r["http_requests_total"]
        assert "method" in labels
        assert "code" in labels
        assert "job" in labels

    def test_without_clause_labels(self):
        r = extract_metrics_from_expr('avg without (instance) (node_cpu_seconds_total{mode="idle"})')
        labels = r["node_cpu_seconds_total"]
        assert "instance" in labels
        assert "mode" in labels

    def test_on_clause_labels(self):
        r = extract_metrics_from_expr("metric_a / on(job) metric_b")
        assert "job" in r["metric_a"]
        assert "job" in r["metric_b"]

    def test_ignoring_clause_labels(self):
        r = extract_metrics_from_expr("metric_a / ignoring(instance) metric_b")
        assert "instance" in r["metric_a"]
        assert "instance" in r["metric_b"]

    # -- __name__ matchers --

    def test_name_equality_matcher(self):
        r = extract_metrics_from_expr('{__name__="node_cpu_seconds_total", instance="localhost"}')
        assert "node_cpu_seconds_total" in r
        assert "instance" in r["node_cpu_seconds_total"]

    def test_name_regex_matcher(self):
        r = extract_metrics_from_expr('{__name__=~"node_cpu_seconds_total|node_memory_total", job="node"}')
        assert "node_cpu_seconds_total" in r
        assert "node_memory_total" in r
        assert "job" in r["node_cpu_seconds_total"]
        assert "job" in r["node_memory_total"]

    # -- label_values() --

    def test_label_values_two_args(self):
        r = extract_metrics_from_expr("label_values(up, job)")
        assert "up" in r
        assert "job" in r["up"]

    def test_label_values_with_filter(self):
        r = extract_metrics_from_expr('label_values(up{cluster=~"$cluster"}, instance)')
        assert "up" in r
        assert "instance" in r["up"]
        assert "cluster" in r["up"]

    def test_label_values_one_arg_no_metric(self):
        r = extract_metrics_from_expr("label_values(job)")
        assert len(r) == 0

    # -- query_result() --

    def test_query_result_recursive(self):
        r = extract_metrics_from_expr('query_result(sum by (job) (up{env="prod"}))')
        assert "up" in r
        assert "job" in r["up"]
        assert "env" in r["up"]

    # -- histogram_quantile --

    def test_histogram_quantile(self):
        r = extract_metrics_from_expr(
            'histogram_quantile(0.99, sum by (le) (rate(http_request_duration_seconds_bucket{job="api"}[5m])))'
        )
        assert "http_request_duration_seconds_bucket" in r
        labels = r["http_request_duration_seconds_bucket"]
        assert "le" in labels
        assert "job" in labels

    # -- PromQL function calls are not metrics --

    def test_functions_excluded(self):
        r = extract_metrics_from_expr("rate(my_counter[5m])")
        assert "rate" not in r
        assert "my_counter" in r

    def test_aggregation_functions_excluded(self):
        r = extract_metrics_from_expr("sum(my_gauge)")
        assert "sum" not in r
        assert "my_gauge" in r

    # -- Grafana template variables are stripped --

    def test_grafana_dollar_var_stripped(self):
        r = extract_metrics_from_expr('up{job=~"$job"}')
        assert "up" in r
        assert "job" in r["up"]

    def test_grafana_braces_var_stripped(self):
        r = extract_metrics_from_expr('up{job=~"${job}"}')
        assert "up" in r

    def test_grafana_bracket_var_stripped(self):
        r = extract_metrics_from_expr("up{job=~[[job]]}")
        assert "up" in r

    # -- Complex real-world expressions --

    def test_complex_division(self):
        r = extract_metrics_from_expr(
            'sum by (namespace) (rate(container_cpu_usage_seconds_total{container!=""}[5m]))'
            " / "
            'sum by (namespace) (kube_pod_container_resource_requests{resource="cpu"})'
        )
        assert "container_cpu_usage_seconds_total" in r
        assert "kube_pod_container_resource_requests" in r
        assert "namespace" in r["container_cpu_usage_seconds_total"]
        assert "container" in r["container_cpu_usage_seconds_total"]
        assert "resource" in r["kube_pod_container_resource_requests"]
        assert "namespace" in r["kube_pod_container_resource_requests"]

    def test_multi_function_expression(self):
        expr = 'increase(http_requests_total{job="api", method="POST"}[1h]) > 100'
        r = extract_metrics_from_expr(expr)
        assert "http_requests_total" in r
        assert r["http_requests_total"] == {"job", "method"}

    # -- Labels merge across multiple occurrences --

    def test_labels_merge(self):
        expr = 'http_requests_total{method="GET"} + http_requests_total{status="200"}'
        r = extract_metrics_from_expr(expr)
        assert "method" in r["http_requests_total"]
        assert "status" in r["http_requests_total"]


# ---------------------------------------------------------------------------
# is_prometheus_target
# ---------------------------------------------------------------------------

class TestIsPrometheusTarget:
    def test_prometheus_datasource(self):
        assert is_prometheus_target({"datasource": {"type": "prometheus"}, "expr": "up"})

    def test_victoriametrics_datasource(self):
        assert is_prometheus_target({"datasource": {"type": "victoriametrics-datasource"}, "expr": "up"})

    def test_elasticsearch_datasource_rejected(self):
        assert not is_prometheus_target({"datasource": {"type": "elasticsearch"}, "expr": "up"})

    def test_no_datasource_with_expr(self):
        assert is_prometheus_target({"expr": "up"})

    def test_no_datasource_no_expr(self):
        assert not is_prometheus_target({})

    def test_string_datasource(self):
        assert is_prometheus_target({"datasource": "Prometheus", "expr": "up"})

    def test_empty_type_with_expr(self):
        assert is_prometheus_target({"datasource": {"type": ""}, "expr": "up"})


# ---------------------------------------------------------------------------
# get_all_panels
# ---------------------------------------------------------------------------

class TestGetAllPanels:
    def test_flat_panels(self):
        dashboard = {"panels": [{"id": 1}, {"id": 2}]}
        assert len(get_all_panels(dashboard)) == 2

    def test_row_with_nested_panels(self):
        dashboard = {
            "panels": [
                {"id": 1, "type": "row", "panels": [{"id": 2}, {"id": 3}]},
                {"id": 4},
            ]
        }
        panels = get_all_panels(dashboard)
        ids = [p["id"] for p in panels]
        assert sorted(ids) == [1, 2, 3, 4]

    def test_non_row_with_nested_panels(self):
        dashboard = {
            "panels": [
                {"id": 1, "type": "panel", "panels": [{"id": 2}]},
            ]
        }
        panels = get_all_panels(dashboard)
        ids = [p["id"] for p in panels]
        assert sorted(ids) == [1, 2]

    def test_empty_panels(self):
        assert get_all_panels({}) == []


# ---------------------------------------------------------------------------
# extract_metrics_from_dashboard
# ---------------------------------------------------------------------------

class TestExtractMetricsFromDashboard:
    def _make_dashboard(self, panels=None, templating=None, annotations=None):
        d = {"dashboard": {}}
        if panels is not None:
            d["dashboard"]["panels"] = panels
        if templating is not None:
            d["dashboard"]["templating"] = templating
        if annotations is not None:
            d["dashboard"]["annotations"] = annotations
        return d

    def test_panel_targets(self):
        dash = self._make_dashboard(panels=[
            {
                "targets": [
                    {"datasource": {"type": "prometheus"}, "expr": 'rate(http_requests_total{job="api"}[5m])'}
                ]
            }
        ])
        m = extract_metrics_from_dashboard(dash)
        assert "http_requests_total" in m
        assert "job" in m["http_requests_total"]

    def test_template_variables(self):
        dash = self._make_dashboard(templating={
            "list": [
                {"query": "label_values(up, job)", "datasource": {"type": "prometheus"}}
            ]
        })
        m = extract_metrics_from_dashboard(dash)
        assert "up" in m
        assert "job" in m["up"]

    def test_template_variable_as_dict(self):
        dash = self._make_dashboard(templating={
            "list": [
                {
                    "query": {"query": "label_values(node_cpu_seconds_total, cpu)"},
                    "datasource": {"type": "prometheus"},
                }
            ]
        })
        m = extract_metrics_from_dashboard(dash)
        assert "node_cpu_seconds_total" in m
        assert "cpu" in m["node_cpu_seconds_total"]

    def test_annotations(self):
        dash = self._make_dashboard(annotations={
            "list": [
                {"expr": 'changes(deploy_timestamp{env="prod"}[1h]) > 0'}
            ]
        })
        m = extract_metrics_from_dashboard(dash)
        assert "deploy_timestamp" in m
        assert "env" in m["deploy_timestamp"]

    def test_annotation_with_target(self):
        dash = self._make_dashboard(annotations={
            "list": [
                {"target": {"expr": 'ALERTS{alertname="HighCPU"}'}}
            ]
        })
        m = extract_metrics_from_dashboard(dash)
        assert "ALERTS" in m
        assert "alertname" in m["ALERTS"]

    def test_skips_non_prometheus_targets(self):
        dash = self._make_dashboard(panels=[
            {
                "targets": [
                    {"datasource": {"type": "elasticsearch"}, "expr": "should_be_ignored"}
                ]
            }
        ])
        m = extract_metrics_from_dashboard(dash)
        assert len(m) == 0

    def test_skips_non_prometheus_template_vars(self):
        dash = self._make_dashboard(templating={
            "list": [
                {
                    "query": "label_values(ignored_metric, job)",
                    "datasource": {"type": "elasticsearch"},
                }
            ]
        })
        m = extract_metrics_from_dashboard(dash)
        assert len(m) == 0

    def test_nested_row_panels(self):
        dash = self._make_dashboard(panels=[
            {
                "type": "row",
                "panels": [
                    {"targets": [{"expr": "up"}]}
                ],
            }
        ])
        m = extract_metrics_from_dashboard(dash)
        assert "up" in m

    def test_merges_labels_across_panels(self):
        dash = self._make_dashboard(panels=[
            {"targets": [{"expr": 'http_requests_total{method="GET"}'}]},
            {"targets": [{"expr": 'http_requests_total{status="200"}'}]},
        ])
        m = extract_metrics_from_dashboard(dash)
        assert m["http_requests_total"] == {"method", "status"}


# ---------------------------------------------------------------------------
# DashboardCache
# ---------------------------------------------------------------------------

class TestDashboardCache:
    def _make_cache_dir(self):
        d = tempfile.mkdtemp(prefix="test_cache_")
        return DashboardCache(d), d

    def _cleanup(self, d):
        shutil.rmtree(d, ignore_errors=True)

    def test_empty_cache(self):
        cache, d = self._make_cache_dir()
        try:
            assert cache.dashboard_count() == 0
            assert cache.all_metrics() == []
            assert cache.all_metrics_with_labels() == {}
        finally:
            self._cleanup(d)

    def test_put_and_get(self):
        cache, d = self._make_cache_dir()
        try:
            cache.put("uid1", 1, "Title", "Folder", {
                "http_requests_total": {"method", "status"},
                "up": {"job"},
            })
            entry = cache.get("uid1")
            assert entry["title"] == "Title"
            assert entry["metrics"]["http_requests_total"] == ["method", "status"]
            assert entry["metrics"]["up"] == ["job"]
        finally:
            self._cleanup(d)

    def test_put_writes_file_immediately(self):
        cache, d = self._make_cache_dir()
        try:
            cache.put("uid1", 1, "Title", "Folder", {"up": {"job"}})
            fpath = Path(d) / "dashboards" / "uid1.json"
            assert fpath.exists()
            with open(fpath) as f:
                data = json.load(f)
            assert data["title"] == "Title"
            assert data["metrics"]["up"] == ["job"]
        finally:
            self._cleanup(d)

    def test_all_metrics(self):
        cache, d = self._make_cache_dir()
        try:
            cache.put("uid1", 1, "A", "F", {"metric_b": set(), "metric_a": {"job"}})
            cache.put("uid2", 1, "B", "F", {"metric_c": set(), "metric_a": {"instance"}})
            assert cache.all_metrics() == ["metric_a", "metric_b", "metric_c"]
        finally:
            self._cleanup(d)

    def test_all_metrics_with_labels_merges(self):
        cache, d = self._make_cache_dir()
        try:
            cache.put("uid1", 1, "A", "F", {"metric_a": {"job", "method"}})
            cache.put("uid2", 1, "B", "F", {"metric_a": {"job", "instance"}})
            mwl = cache.all_metrics_with_labels()
            assert mwl["metric_a"] == ["instance", "job", "method"]
        finally:
            self._cleanup(d)

    def test_remove(self):
        cache, d = self._make_cache_dir()
        try:
            cache.put("uid1", 1, "A", "F", {"up": set()})
            fpath = Path(d) / "dashboards" / "uid1.json"
            assert fpath.exists()
            cache.remove("uid1")
            assert cache.get("uid1") is None
            assert cache.dashboard_count() == 0
            assert not fpath.exists()
        finally:
            self._cleanup(d)

    def test_all_uids(self):
        cache, d = self._make_cache_dir()
        try:
            cache.put("uid1", 1, "A", "F", {"up": set()})
            cache.put("uid2", 1, "B", "F", {"up": set()})
            assert cache.all_uids() == {"uid1", "uid2"}
        finally:
            self._cleanup(d)

    def test_save_and_reload(self):
        cache, d = self._make_cache_dir()
        try:
            cache.put("uid1", 42, "Title", "Folder", {"up": {"job"}})
            cache.set_metadata("https://grafana.example.com")
            cache.save()

            cache2 = DashboardCache(d)
            assert cache2.dashboard_count() == 1
            assert cache2.all_metrics() == ["up"]
            assert cache2.all_metrics_with_labels() == {"up": ["job"]}
            assert cache2.meta["grafana_url"] == "https://grafana.example.com"
        finally:
            self._cleanup(d)

    def test_single_file_migration(self):
        d = tempfile.mkdtemp(prefix="test_cache_migrate_")
        try:
            v1_data = {
                "version": 1,
                "grafana_url": "https://grafana.example.com",
                "last_updated": "2026-01-01T00:00:00+00:00",
                "dashboards": {
                    "uid1": {
                        "version": 1,
                        "title": "Old Dashboard",
                        "folder": "General",
                        "metrics": ["metric_a", "metric_b"],
                        "cached_at": "2026-01-01T00:00:00+00:00",
                    }
                },
            }
            with open(Path(d) / "dashboard-metrics.json", "w") as f:
                json.dump(v1_data, f)

            cache = DashboardCache(d)
            assert cache.dashboard_count() == 1
            entry = cache.get("uid1")
            assert isinstance(entry["metrics"], dict)
            assert entry["metrics"] == {"metric_a": [], "metric_b": []}
            assert cache.all_metrics() == ["metric_a", "metric_b"]
            assert cache.meta["grafana_url"] == "https://grafana.example.com"
            assert (Path(d) / "dashboard-metrics.json.migrated").exists()
            assert not (Path(d) / "dashboard-metrics.json").exists()
            assert (Path(d) / "dashboards" / "uid1.json").exists()
        finally:
            self._cleanup(d)

    def test_nonexistent_cache_dir(self):
        d = "/tmp/test_nonexistent_cache_dir_" + str(os.getpid())
        try:
            cache = DashboardCache(d)
            assert cache.dashboard_count() == 0
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_corrupt_dashboard_file_skipped(self):
        d = tempfile.mkdtemp(prefix="test_cache_corrupt_")
        try:
            dashboards_dir = Path(d) / "dashboards"
            dashboards_dir.mkdir()
            with open(dashboards_dir / "good.json", "w") as f:
                json.dump({"version": 1, "title": "Good", "folder": "F", "metrics": {"up": []}}, f)
            with open(dashboards_dir / "bad.json", "w") as f:
                f.write("not json{{{")

            cache = DashboardCache(d)
            assert cache.dashboard_count() == 1
            assert cache.get("good") is not None
            assert cache.get("bad") is None
        finally:
            self._cleanup(d)


# ---------------------------------------------------------------------------
# _metric_names_from_entry
# ---------------------------------------------------------------------------

class TestMetricNamesFromEntry:
    def test_none_entry(self):
        assert _metric_names_from_entry(None) == set()

    def test_v2_dict_metrics(self):
        entry = {"metrics": {"up": ["job"], "go_goroutines": []}}
        assert _metric_names_from_entry(entry) == {"up", "go_goroutines"}

    def test_v1_list_metrics(self):
        entry = {"metrics": ["up", "go_goroutines"]}
        assert _metric_names_from_entry(entry) == {"up", "go_goroutines"}

    def test_empty_metrics(self):
        entry = {"metrics": {}}
        assert _metric_names_from_entry(entry) == set()


# ---------------------------------------------------------------------------
# Output formatters (smoke tests capturing stdout)
# ---------------------------------------------------------------------------

class TestOutputFormatters:
    def _make_populated_cache(self):
        d = tempfile.mkdtemp(prefix="test_cache_fmt_")
        cache = DashboardCache(d)
        cache.put("uid1", 1, "Dashboard A", "Production", {
            "http_requests_total": {"method", "status", "job"},
            "up": {"job"},
        })
        cache.put("uid2", 2, "Dashboard B", "General", {
            "go_goroutines": set(),
            "http_requests_total": {"method"},
        })
        cache.set_metadata("https://grafana.example.com")
        return cache, d

    def _cleanup(self, d):
        shutil.rmtree(d, ignore_errors=True)

    def test_print_summary(self, capsys):
        from extract_grafana_metrics import print_summary
        cache, d = self._make_populated_cache()
        try:
            print_summary(cache, stats=None, changed_dashboards=[])
            out = capsys.readouterr().out
            assert "Grafana Metrics Extraction Report" in out
            assert "Total unique metrics: 3" in out
            assert "http_requests_total" in out
            assert "[job, method, status]" in out
            assert "go_goroutines" in out
            assert "Dashboard A" in out
            assert "Dashboard B" in out
        finally:
            self._cleanup(d)

    def test_print_summary_with_stats(self, capsys):
        from extract_grafana_metrics import print_summary
        cache, d = self._make_populated_cache()
        try:
            stats = {"fetched": 5, "cached": 3, "new": 1, "updated": 1, "deleted": 0, "errors": 0}
            print_summary(cache, stats=stats, changed_dashboards=[])
            out = capsys.readouterr().out
            assert "Dashboards fetched: 5" in out
            assert "Unchanged (cached): 3" in out
        finally:
            self._cleanup(d)

    def test_print_json(self, capsys):
        from extract_grafana_metrics import print_json
        cache, d = self._make_populated_cache()
        try:
            print_json(cache, stats=None, changed_dashboards=[])
            out = capsys.readouterr().out
            data = json.loads(out)
            assert data["total_unique_metrics"] == 3
            assert "http_requests_total" in data["all_metrics"]
            assert "all_metrics_with_labels" in data
            assert "job" in data["all_metrics_with_labels"]["http_requests_total"]
            assert data["dashboards"]["uid1"]["metrics"]["up"] == ["job"]
        finally:
            self._cleanup(d)

    def test_print_metrics_only(self, capsys):
        from extract_grafana_metrics import print_metrics_only
        cache, d = self._make_populated_cache()
        try:
            print_metrics_only(cache)
            out = capsys.readouterr().out
            lines = out.strip().split("\n")
            assert sorted(lines) == ["go_goroutines", "http_requests_total", "up"]
        finally:
            self._cleanup(d)

    def test_print_metrics_with_labels(self, capsys):
        from extract_grafana_metrics import print_metrics_with_labels
        cache, d = self._make_populated_cache()
        try:
            print_metrics_with_labels(cache)
            out = capsys.readouterr().out
            lines = out.strip().split("\n")
            assert len(lines) == 3
            for line in lines:
                if line.startswith("http_requests_total"):
                    assert "\t" in line
                    parts = line.split("\t")
                    labels = parts[1].split(",")
                    assert "job" in labels
                    assert "method" in labels
                    assert "status" in labels
                elif line.startswith("up"):
                    assert "\tjob" in line
                elif line.startswith("go_goroutines"):
                    assert "\t" not in line
        finally:
            self._cleanup(d)

    def test_print_json_with_changes(self, capsys):
        from extract_grafana_metrics import print_json
        cache, d = self._make_populated_cache()
        try:
            changes = [
                {
                    "uid": "uid1",
                    "title": "Dashboard A",
                    "action": "new",
                    "old_metrics": set(),
                    "new_metrics": {"http_requests_total", "up"},
                }
            ]
            print_json(cache, stats=None, changed_dashboards=changes)
            out = capsys.readouterr().out
            data = json.loads(out)
            assert len(data["changes"]) == 1
            assert data["changes"][0]["action"] == "new"
            assert "http_requests_total" in data["changes"][0]["added_metrics"]
        finally:
            self._cleanup(d)


# ---------------------------------------------------------------------------
# Edge cases for extract_metrics_from_expr
# ---------------------------------------------------------------------------

class TestExtractMetricsEdgeCases:
    def test_single_char_tokens_excluded(self):
        r = extract_metrics_from_expr("a + b")
        assert len(r) == 0

    def test_numeric_literals_excluded(self):
        r = extract_metrics_from_expr("my_metric > 100")
        assert "my_metric" in r
        assert len(r) == 1

    def test_string_literals_excluded(self):
        r = extract_metrics_from_expr('label_replace(my_metric, "dst", "$1", "src", "(.*)")')
        assert "my_metric" in r

    def test_comment_ignored(self):
        r = extract_metrics_from_expr("my_metric # this is a comment with fake_metric")
        assert "my_metric" in r
        assert "fake_metric" not in r

    def test_duration_literals_excluded(self):
        r = extract_metrics_from_expr("rate(my_metric[5m])")
        assert "my_metric" in r
        assert len(r) == 1

    def test_offset_keyword_excluded(self):
        r = extract_metrics_from_expr("my_metric offset 5m")
        assert "my_metric" in r
        assert "offset" not in r

    def test_bool_keyword_excluded(self):
        r = extract_metrics_from_expr("my_metric > bool 10")
        assert "my_metric" in r
        assert "bool" not in r

    def test_multiple_selectors_same_metric(self):
        r = extract_metrics_from_expr(
            'http_requests_total{job="api"} / http_requests_total{job="api", method="GET"}'
        )
        assert r["http_requests_total"] == {"job", "method"}

    def test_nested_aggregation(self):
        expr = 'topk(5, sum by (pod) (rate(container_cpu_usage_seconds_total{namespace="prod"}[5m])))'
        r = extract_metrics_from_expr(expr)
        assert "container_cpu_usage_seconds_total" in r
        assert "pod" in r["container_cpu_usage_seconds_total"]
        assert "namespace" in r["container_cpu_usage_seconds_total"]

    def test_binary_operation_with_on(self):
        expr = (
            'sum by (namespace) (rate(container_memory_usage_bytes[5m]))'
            ' / on (namespace) '
            'sum by (namespace) (kube_namespace_resource_quota)'
        )
        r = extract_metrics_from_expr(expr)
        assert "container_memory_usage_bytes" in r
        assert "kube_namespace_resource_quota" in r
        assert "namespace" in r["container_memory_usage_bytes"]
        assert "namespace" in r["kube_namespace_resource_quota"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
