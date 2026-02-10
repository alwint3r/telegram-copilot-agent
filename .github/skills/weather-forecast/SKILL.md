---
name: weather-forecast
description: Fetch reliable weather forecasts from Open-Meteo using explicit location and time resolution rules
---

# Weather Forecast

## Goal

Fetch weather forecast data from Open-Meteo using location and time parameters inferred from conversation context and the current user prompt.

Always produce deterministic output artifacts and avoid ambiguous interpretation.

## Workflow

1. Resolve user intent into request parameters.
   - Required: location.
   - Optional: time window.
   - If location is missing, ask one concise follow-up question.
   - If time is missing, default to "today" in the resolved location timezone.
   - Convert relative time terms (for example: "tomorrow", "next Friday") to absolute dates before calling APIs.

2. Resolve location with Open-Meteo geocoding first.
   - Use Open-Meteo geocoding API to convert location text to coordinates.
   - Do not use generic web search for coordinates unless Open-Meteo geocoding fails.
   - If multiple geocoding matches are plausible, ask one disambiguation question (for example: "Springfield, IL or Springfield, MA?").
   - If no geocoding result is found, ask for country/state context or direct coordinates.

3. Build a valid forecast query.
   - Use Open-Meteo forecast API as the primary weather source.
   - Select hourly or daily fields based on request scope:
     - Intra-day or hour-specific requests: include hourly fields.
     - Day-level requests: include daily fields.
   - Validate start/end dates:
     - If only one date is given, use it for both start and end.
     - If start is after end, ask for correction.

4. Generate deterministic output.
   - Always use `uv` for Python-related operations.
   - Prefer Python standard library for request orchestration and file handling.
   - Write one JSON artifact to `output/weather-forecast`.
   - If the directory does not exist, create it using Python standard library (`os`).
   - Use a unique filename format:
     - `weather_<location-slug>_<start-date>_<uuid8>.json`
   - Slugify location for filename safety (lowercase letters, numbers, dashes only).

5. Return both a concise summary and structured data artifact.
   - Provide a short human-readable summary in the assistant response.
   - Include artifact path in the response.
   - JSON output must include this shape:
     - `request`: original request fields inferred from prompt/context.
     - `resolved_location`: name, latitude, longitude, country, timezone.
     - `forecast_window`: start_date, end_date, timezone.
     - `units`: temperature/wind/precipitation units used.
     - `data`: hourly and/or daily forecast arrays returned by API.
     - `source`: Open-Meteo endpoint URLs used.
     - `generated_at`: ISO-8601 timestamp in UTC.

6. Handle failures explicitly.
   - On transient network/API errors, retry once.
   - If still failing, return a concise error with the failed step and missing requirement.
   - Never fabricate weather values.

7. Clean up temporary files only.
   - Remove temporary scripts or intermediate files created for the task.
   - Do not delete final artifacts in `output/weather-forecast`.
