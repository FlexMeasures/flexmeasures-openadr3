# flexmeasures-openadr3

FlexMeasures plugin that connects to an **OpenADR 3 Virtual Top Node (VTN)** as a **Virtual End Node (VEN)** and ingests demand-response capacity signals into FlexMeasures time-series sensors.

Requires FlexMeasures **≥ v1.1.0** (currently its `main` branch, as plugin-defined automations are not in a release yet) and Python **≥ 3.12**.

## What is OpenADR?

[OpenADR](https://www.openadr.org/) (Open Automated Demand Response) is an industry standard for exchanging demand-response signals between a grid operator or aggregator (**VTN — Virtual Top Node**) and flexible assets or sites (**VEN — Virtual End Node**). Events describe when and how much capacity is available or limited, typically as time-bounded intervals with numeric payloads.

**OpenADR 3** modernises the protocol around REST APIs and JSON payloads (compared with the XML/SOAP OpenADR 2.0b profile). This plugin implements a **VEN client** using the [`openadr3-client`](https://pypi.org/project/openadr3-client/) Open-Source Python library and targets **OpenADR 3** VTNs.

### Related: OpenLEADR

[OpenLEADR](https://lfenergy.org/projects/openleadr/) is another [LF Energy](https://lfenergy.org/) project. It provides open-source OpenADR implementations, including a **Rust-based OpenADR 3 VTN** ([openleadr-rs](https://github.com/OpenLEADR/openleadr-rs)). OpenLEADR plays the **VTN** role; this FlexMeasures plugin plays the **VEN** role and can consume events published by VTNs such as OpenLEADR.

## Overview

The plugin adds a configuration UI and registers an **automation type** with FlexMeasures. Setup happens through the plugin's web dashboard; running, (de)activating and inspecting polling schedules happens on FlexMeasures' own *Automations* page.

| Capability | Description |
|------------|-------------|
| **VEN clients** | Register connection details to a VTN: base URL, OAuth client credentials (stored encrypted), and scopes. Each VEN client is represented as a FlexMeasures **generic asset** of type `OpenADR VEN`. |
| **Polling schedules** | Per VEN client, define one or more polling schedules. Each polling schedule is a FlexMeasures **automation** of type `openadr3-fetch-events` on the VEN client's asset, which runs on a cron schedule (in a timezone of your choice) and fetches demand response signals from the OpenADR VTN configured in the VEN client. |
| **Sensor mapping** | For each polling schedule, the plugin creates or links FlexMeasures **sensors** on the VEN asset and writes incoming event values as **beliefs** (15-minute resolution, kW). |

### Supported signals (current scope)

Only two OpenADR event payload types are supported as of the current release of this plugin:

| Payload type | FlexMeasures sensor | Unit |
|--------------|----------------------|------|
| `IMPORT_CAPACITY_LIMIT` | `import-capacity-limit` | kW |
| `EXPORT_CAPACITY_LIMIT` | `export-capacity-limit` | kW |

Additional OpenADR payload types may be supported in future releases. The ingestion pipeline is limited to these capacity-limit signals for now.

### Dashboard

After installation, open the plugin from the FlexMeasures main menu:

- **Menu label:** OpenADR 3 Configuration
- **URL:** `/flexmeasures-openadr3/dashboard`

From the dashboard you can create and manage VEN clients, polling schedules, and inspect linked sensors. Each VEN client's polling schedules page links to the asset's FlexMeasures *Automations* page, where you can *Run now*, (de)activate a schedule, and see its recent runs and jobs.

## How it works

```mermaid
flowchart LR
    subgraph config [Configuration UI]
        UI[Dashboard]
        VC[VEN client]
        SC[polling schedule]
        UI --> VC
        VC --> SC
    end

    subgraph fm [FlexMeasures]
        GA[Generic asset: OpenADR VEN]
        IS[import-capacity-limit sensor]
        ES[export-capacity-limit sensor]
        SC --> GA
        SC --> IS
        SC --> ES
    end

    subgraph runtime [Automation run]
        AR[flexmeasures jobs run-automations]
        RQ[RQ job on the ingestion queue]
        AR --> RQ
        VEN[openadr3-client VEN HTTP client]
        VTN[(OpenADR 3 VTN)]
        RQ --> VEN
        VEN -->|GET events by targets| VTN
        VEN -->|TimedBelief| IS
        VEN -->|TimedBelief| ES
    end

    SC -.->|is an automation, due per its cron string| AR
```

1. An administrator configures a **VEN client** (VTN URL, OAuth) and one or more **polling schedules** with optional OpenADR targets to filter on, a cron string and a timezone.
2. When signals are enabled, the plugin provisions **import** and/or **export capacity limit** sensors on the VEN asset, and creates the polling schedule as a FlexMeasures **automation** on that asset. Its configuration (targets and sensors) is stored on the automation's data source, `OpenADR 3 VTN`, which belongs to the VEN asset's account.
3. FlexMeasures' automation dispatcher (`flexmeasures jobs run-automations`) queues an **RQ job** on the `ingestion` queue whenever the schedule is due (or when you click *Run now*). The job calls the VTN, filters events for the supported payload types, validates that active intervals do not overlap, and hands the beliefs back to FlexMeasures, which stores them on the mapped sensors under the automation's data source. Beliefs that are already on record are not saved again.
4. These sensors can then be coupled to assets within FlexMeasures. The most obvious mapping for the currently supported signals would be for an OpenADR sensor to map to the `site-consumption-capacity` (import capacity limit) and `site-production-capacity` (export capacity limit) on an asset of the `building` type. But other variations may exist.

## Installation

1. Add the plugin to your FlexMeasures config via `FLEXMEASURES_PLUGINS` (a list). Use either:
   - The filesystem path to this repository, e.g. `/path/to/flexmeasures-plugin-oadr3`, or
   - The installed package name: `flexmeasures_openadr3` (after `pip install` / `uv pip install`).

2. Set FlexMeasures' `FLEXMEASURES_SECRETS_ENCRYPTION_KEYS` (required, FlexMeasures v1.0+): a JSON object mapping key IDs to master key values, e.g. `{"1": "<random 32-byte url-safe token>"}`. This plugin uses FlexMeasures' native platform-secrets feature to encrypt VEN OAuth credentials at rest on the VEN's asset, and this key material must be identical across every process (`server`, any `worker`, and the automation dispatcher), exactly like `SECRET_KEY`. Unlike most FlexMeasures settings, this one is **not** read from an environment variable — set it in `flexmeasures.cfg` (in your FlexMeasures instance folder) instead, e.g. `FLEXMEASURES_SECRETS_ENCRYPTION_KEYS = {"1": "<random 32-byte url-safe token>"}`.

3. Run FlexMeasures' automation dispatcher, `flexmeasures jobs run-automations`, once a minute (e.g. from cron, or a loop like the `automation-runner` service in `docker-compose.yml`), and an RQ worker on the `ingestion` queue (`flexmeasures jobs run-worker --queue ingestion`). Both need the plugin in `FLEXMEASURES_PLUGINS`, as they load the plugin's automation type. Fetch jobs are bounded by the `ingestion` entry of `FLEXMEASURES_JOB_TIMEOUT`, which also covers the OAuth token request to the VTN's authorization server.

4. Restart FlexMeasures and open **OpenADR 3 Configuration** in the menu.

Polling schedules can also be created without the UI, like any automation (FlexMeasures' own UI cannot create plugin automations yet). The automation must hang off the VEN client's asset, and its sensors must belong to it:

```bash
echo '{"targets": ["my-target"], "import-sensor": 12, "export-sensor": 13}' > fetch-config.json
flexmeasures add automation --asset <VEN asset id> --name daily-poll --type openadr3-fetch-events --cron "30 2 * * *" --timezone UTC --config fetch-config.json
```

### Upgrading from plugin version 1.0

Polling schedules used to be stored in the VEN asset's attributes and run by the plugin's own `flexmeasures oadr3-run-cron-scheduler` process. They are not migrated: recreate each polling schedule in the dashboard (its sensors are reused when the name is the same), and replace the `cron-scheduler` process with the automation dispatcher (see step 3).

## Development

We use pre-commit to keep code quality up.

Install necessary tools with:

```bash
uv sync --dev
pre-commit install
```

Try it:

```bash
pre-commit run --all-files --show-diff-on-failure
```

To inspect RQ jobs, you can use the rq-dashboard package which can be optionally installed as a dev dependency:

```bash
uv run rq-dashboard --redis-url redis://{USERNAME}:{PASSWORD}@{HOST}:{PORT}/{QUEUE_NAME}
```

In the case of the redis queue configured in the `docker-compose.yml`, this would result in the following command:

```bash
uv run rq-dashboard --redis-url redis://:fm-redis-pass@localhost:6379/0
```
