# Skills

A collection of agent skills for Grafana and observability tooling. These skills help AI agents extract, analyze, and optimize metrics across Grafana dashboards and TSDB backends.

## Available Plugins

| Plugin | Skills | Purpose |
|--------|--------|---------|
| [grafana](plugins/grafana) | grafana-metrics-extraction | Extract and analyze metrics referenced in Grafana dashboards |

## Installation

### Via Claude Code plugin marketplace

Add the marketplace source:

```
/plugin marketplace add sergeyshevch/skills
```

Install plugins:

```
/plugin install grafana@sergeyshevch-tools
```

### Via skills.sh

Install a specific skill:

```
npx skills add sergeyshevch/skills --skill grafana-metrics-extraction
```

### Via Cursor

Copy the skill directory to your personal or project skills location:

```bash
# Personal (available across all projects)
cp -r plugins/grafana/skills/grafana-metrics-extraction ~/.cursor/skills/

# Project-specific
cp -r plugins/grafana/skills/grafana-metrics-extraction .cursor/skills/
```

### Local development

Load a plugin directly for testing:

```bash
claude --plugin-dir ./plugins/grafana
```

## Skills

### Grafana plugin

| Skill | Purpose |
|-------|---------|
| grafana-metrics-extraction | Extract all metric names referenced in Grafana dashboards with incremental caching, cross-reference with TSDB ingestion data, and identify optimization opportunities |

## Usage

Once installed, skills are available as slash commands and are also triggered automatically when Claude detects a matching request:

```
/grafana:grafana-metrics-extraction  - extract and analyze Grafana dashboard metrics
```

**Example prompts that trigger skills:**

- "Which metrics are used in Grafana?" → `grafana-metrics-extraction`
- "Cross-reference Grafana dashboards with ingested metrics" → `grafana-metrics-extraction`
- "Find metrics ingested but not used in any dashboard" → `grafana-metrics-extraction`
- "Prepare a metric cleanup list from Grafana" → `grafana-metrics-extraction`

## Environment Variables

The Grafana skills expect these environment variables (or a `.env` file):

```
GRAFANA_URL           # Grafana base URL (e.g., https://grafana.example.com)
GRAFANA_TOKEN         # Grafana service account token (glsa_...)
GRAFANA_AUTH_HEADER   # Alternative: full auth header (optional)
```

## License

[Apache License 2.0](LICENSE)
