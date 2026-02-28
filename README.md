# Splunk Bulk Config CLI

A command-line utility for bulk-managing Splunk configuration stanzas via the REST API. It reads a JSON file of changes and applies them to a target Splunk instance, automatically creating stanzas that don't exist or updating ones that do.

## Requirements

- Python 3
- `requests` library (`pip install requests`)
- A Splunk JWT token with appropriate permissions

## Usage

```bash
python splunk_config_cli.py --token <JWT> --host <SPLUNK_HOST> --file <JSON_FILE> [--type <CONF_TYPE>] [--post-by-id] [--port 8089] [--update-only] [--log <PATH>] [--shc] [--shc-delay SECONDS]
```

| Argument | Required | Description |
|---|---|---|
| `--token` | Yes | Splunk JWT bearer token |
| `--host` | Yes | Splunk hostname or IP |
| `--type` | Conditional | Configuration file type (e.g. `savedsearches`, `macros`, `props`). Required unless `--post-by-id` is specified |
| `--file` | Yes | Path to the JSON changes file |
| `--post-by-id` | No | Use the `id` field from each JSON item as the request URL; `--type` becomes optional |
| `--port` | No | Management port (default: `8089`) |
| `--update-only` | No | Only update existing stanzas; skip creation on 404 |
| `--log` | No | Write log output to a file at the specified path |
| `--shc` | No | Validate configs replicated to all SHC members after push |
| `--shc-delay` | No | Seconds to wait for SHC replication before validating (default: `5`) |

## JSON File Format

Each JSON file is an array of objects. Every object represents a single stanza to create or update:

```json
[
  {
    "title": "stanza_name",
    "app": "target_app",
    "configs": {
      "key": "value"
    }
  }
]
```

| Field | Required | Description |
|---|---|---|
| `title` | Yes | The stanza name (e.g. saved search name, sourcetype, macro name) |
| `app` | No | Target app context (default: `search`) |
| `id` | No | Full REST API URL for the stanza (e.g. from `\| rest`). When present, used as the request URL instead of constructing from `title`/`app`/`--type` |
| `configs` | Yes | Key-value pairs matching the conf file settings for that stanza |

## Examples

### Saved Searches (`--type savedsearches`)

```json
[
  {
    "title": "test_cli_saved_search",
    "app": "search",
    "configs": {
      "disabled": "1"
    }
  },
  {
    "title": "test_cli_search_two",
    "app": "search",
    "configs": {
      "search": "index=_internal sourcetype=splunk_web_access | stats count by uri_path | sort -count | head 20",
      "cron_schedule": "0 */6 * * *",
      "dispatch.earliest_time": "-24h@h",
      "dispatch.latest_time": "now",
      "is_scheduled": "1",
      "disabled": "1",
      "description": "Top web access URIs - created by CLI utility"
    }
  }
]
```

```bash
python splunk_config_cli.py --token "$TOKEN" --host splunk.example.com --type savedsearches --file changes.json
```

### Macros (`--type macros`)

```json
[
  {
    "title": "test_cli_macro",
    "app": "search",
    "configs": {
      "definition": "index=_internal sourcetype=splunkd log_level=ERROR",
      "description": "Test macro - internal errors",
      "iseval": "0"
    }
  },
  {
    "title": "test_cli_macro_with_args(2)",
    "app": "search",
    "configs": {
      "definition": "index=$idx$ sourcetype=$st$ | stats count",
      "description": "Test macro with args - flexible stats",
      "args": "idx,st",
      "iseval": "0"
    }
  }
]
```

```bash
python splunk_config_cli.py --token "$TOKEN" --host splunk.example.com --type macros --file changes_macros.json
```

### Props (`--type props`)

```json
[
  {
    "title": "test_cli_sourcetype",
    "app": "search",
    "configs": {
      "TIME_PREFIX": "^",
      "TIME_FORMAT": "%Y-%m-%dT%H:%M:%S.%3N%z",
      "MAX_TIMESTAMP_LOOKAHEAD": "32",
      "SHOULD_LINEMERGE": "false",
      "LINE_BREAKER": "([\\r\\n]+)",
      "category": "Custom",
      "description": "Test props entry from CLI utility"
    }
  }
]
```

```bash
python splunk_config_cli.py --token "$TOKEN" --host splunk.example.com --type props --file changes_props.json
```

### Update-Only Mode

Use `--update-only` to skip stanza creation. If a stanza doesn't exist (404), it will be skipped instead of created:

```bash
python splunk_config_cli.py --token "$TOKEN" --host splunk.example.com --type macros --file changes_macros.json --update-only
```

### Logging to a File

Use `--log` to write timestamped output to a log file (output is still printed to the console):

```bash
python splunk_config_cli.py --token "$TOKEN" --host splunk.example.com --type savedsearches --file changes.json --log ./run.log
```

Example log output:

```
2026-02-28 10:15:00,123 [INFO] Loaded 3 stanza(s) from changes.json
2026-02-28 10:15:00,124 [INFO] Target: https://splunk.example.com:8089 | conf-type: savedsearches | update-only: False
2026-02-28 10:15:00,125 [INFO] Processing [test_cli_saved_search] in app=search
2026-02-28 10:15:00,450 [INFO] Successfully applied changes to test_cli_saved_search.
2026-02-28 10:15:01,200 [INFO] Complete: 3 succeeded, 0 failed, 0 skipped
```

### SHC Validation

Use `--shc` to verify that pushed configs replicated to all Search Head Cluster members. The tool queries the captain for cluster health and member list, waits for replication, then checks each stanza on every member:

```bash
python splunk_config_cli.py --token "$TOKEN" --host shc-captain.example.com --type savedsearches --file changes.json --shc
```

With a custom replication delay:

```bash
python splunk_config_cli.py --token "$TOKEN" --host shc-captain.example.com --type savedsearches --file changes.json --shc --shc-delay 10
```

Example output:

```
2026-02-28 10:15:02,100 [INFO] Complete: 2 succeeded, 0 failed, 0 skipped
2026-02-28 10:15:02,101 [INFO] SHC Validation: checking captain health...
2026-02-28 10:15:02,300 [INFO] SHC Validation: captain 'sh1' is healthy (service_ready_flag=1)
2026-02-28 10:15:02,500 [INFO] SHC Validation: discovered 3 member(s): sh1, sh2, sh3
2026-02-28 10:15:02,501 [INFO] SHC Validation: waiting 5s for knowledge bundle replication...
2026-02-28 10:15:07,502 [INFO]   [sh1] test_cli_saved_search: PASS
2026-02-28 10:15:07,600 [INFO]   [sh2] test_cli_saved_search: PASS
2026-02-28 10:15:07,700 [INFO]   [sh3] test_cli_saved_search: PASS
2026-02-28 10:15:07,800 [INFO]   [sh1] test_cli_search_two: PASS
2026-02-28 10:15:07,900 [INFO]   [sh2] test_cli_search_two: PASS
2026-02-28 10:15:08,000 [INFO]   [sh3] test_cli_search_two: PASS
2026-02-28 10:15:08,001 [INFO] SHC Validation: 2/2 stanzas verified across 3 members (6/6 checks passed)
```

### Generating a JSON File with Splunk SPL (Cron Skew Example)

You can use a Splunk search to generate a JSON change file directly. This example finds all enabled saved searches with cron schedules starting at minute `0` (top of the hour) and skews them across the first 30 minutes to reduce scheduling contention:

```spl
| rest splunk_server=local /servicesNS/-/-/saved/searches
| fields title eai:acl.app disabled cron_schedule is_scheduled schedule_window id
| where isnotnull(cron_schedule) AND match(cron_schedule, "^0[^\d]") AND disabled=0
| streamstats count as ct_count
| eventstats max(ct_count) as max_count
| eval spm = round(max_count / 30, 0), diff = if(round(ct_count/spm) > 1, round(ct_count/spm), 1)
| eval new_cron = cron_schedule
| rex mode=sed field=new_cron "s/^0//g"
| eval new_cron = diff.new_cron
| eval json_object = json_object("title", title, "app", 'eai:acl.app', "id", id, "configs", json_object("disabled", disabled, "cron_schedule", new_cron, "is_schedule", is_scheduled, "schedule_window", "auto"))
| stats values(json_object) as json_object
| eval json_object = "[" + mvjoin(json_object, ",||") + "]"
| rex mode=sed field=json_object "s/\|\|/\n/g"
```

Copy the `json_object` field value from the results into a file (e.g. `cron_skew.json`) and run:

```bash
python splunk_config_cli.py --token "$TOKEN" --host splunk.example.com --post-by-id --file cron_skew.json --update-only
```

Since the JSON includes an `id` field (produced by `| rest`), `--post-by-id` lets you skip `--type` entirely. The tool uses the `id` path as the request URL, ensuring the correct endpoint is hit regardless of URL-encoding or object type.

## How It Works

For each entry in the JSON file, the tool:

1. **Determines the target URL** — if `id` is present, its path is used with the CLI-specified `--host`/`--port`; otherwise a URL is constructed from `app`, `--type`, and `title`
2. **Attempts an update** by POSTing to the stanza URL
3. **If the stanza doesn't exist** (HTTP 404) and `--update-only` is not set, it **creates** the stanza by POSTing to the parent endpoint with `name` included in the payload
4. Reports success or failure for each stanza
