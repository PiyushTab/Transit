# Transit Reliability Analyzer

A dashboard for inspecting public transport delays. It uses Python's standard library for the server and works with generated sample telemetry or an uploaded CSV. Uploaded content is held temporarily in server memory and isolated to the visitor's browser session; it is removed when that session expires or the service restarts.

## Run

From this directory:

```sh
python3 server.py
```

Open [http://localhost:8000](http://localhost:8000). The dashboard starts with generated sample data. Use **Load sample data** to reset it, or upload your own CSV.

## CSV format

Required: `delay_min`, or both `scheduled_time` and `actual_time`. Optional columns: `route_id`, `route_name`, `trip_id`, `mode`, `headway_min`, `weather`. Timestamps should be ISO 8601, such as `2026-10-25T08:30:00`. Delays outside −30 to +180 minutes are excluded. Duplicate rows with the same trip and scheduled time are removed.

Example:

```csv
route_id,route_name,mode,trip_id,scheduled_time,actual_time,headway_min
BUS-101,Downtown Express,Bus,T100,2026-10-25T08:30:00,2026-10-25T08:34:00,12
```

## Ask the data

Use the question box to request route rankings, compare bus and metro performance, inspect observed delay averages by weather, ask about OTP, buffer time index, excess wait time, or forecast summaries. Answers respect the active filters and are generated locally with simple rules; no external AI service receives the data.

## Interactive controls

- Filter by route, mode, weather, and service date. Click a scorecard row or schematic route to select it.
- Use the congestion and rain-intensity sliders as illustrative stress tests. The sliders add a mode-weighted delay increment; they do not estimate causal weather impacts.
- The route diagram is schematic and not geographically accurate because stop coordinates are not part of the CSV schema.
- Export the filtered rows as CSV (including observed and scenario delay columns), or download a text summary report.

## Metrics

- OTP counts trips between 1 minute early and 5 minutes late.
- BTI = (95th percentile delay − mean delay) / max(mean delay, 1 minute).
- Excess wait time is estimated from provided headways. If headways aren't included, it reports 0 and the dashboard labels that data as insufficient in the answer.
- Forecast is a 48-hour seasonal baseline based on observed hourly delays. It is not a live operational prediction.

## Deploy on Render

The included `render.yaml` defines a free Python web service. In Render, choose **New → Blueprint**, connect this repository, and apply the Blueprint. Render deploys the app to a public `onrender.com` URL. Free services sleep after 15 minutes without traffic and may take about a minute to wake. Uploaded data is in memory only and clears when the service restarts or the browser session expires.
