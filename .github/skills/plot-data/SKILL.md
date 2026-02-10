---
name: plot-data
description: Create consistent, reliable plots from structured or inferred data with deterministic output artifacts
---

# Plot Data

## Goal

Turn data in the conversation context and user prompt into a clear, useful plot with deterministic rendering and artifact handling.

Always produce a plot that can be reproduced from saved artifacts.

## Workflow

1. Resolve plotting intent and data inputs.
   - Required: at least one usable dataset (table, series, key-value list, or CSV-like text).
   - Optional: chart type, title, axis labels, grouping, color preference.
   - If required data is missing or malformed, ask one concise follow-up question describing exactly what is needed.
   - If no chart type is specified, choose one by rule:
     - time-indexed values -> line chart
     - category comparisons -> bar chart
     - distribution -> histogram
     - relationship between two numeric columns -> scatter plot

2. Normalize and validate data before plotting.
   - Convert input into explicit columns and rows.
   - Validate numeric fields for plotted measures; drop or report invalid rows.
   - For time-series data:
     - parse timestamps to a consistent format
     - if the scope is within 24 hours, use hour labels on the X axis to prevent text overflow
   - If data cleaning changes results materially (for example: dropped rows), mention it in the response.

3. Render with deterministic plotting rules.
   - Use matplotlib for chart generation.
   - Always use `uv` for Python-related operations.
   - Prefer Python standard library plus matplotlib; avoid adding dependencies unless strictly required.
   - Use readable defaults:
     - figure size appropriate for the data density
     - axis labels and title when available
     - legible tick density and rotation for long labels
     - visible grid for quantitative charts unless user asks otherwise

4. Produce deterministic artifacts.
   - Save outputs to `output/plot-data`.
   - If directory does not exist, create it using Python standard library (`os`).
   - Generate a unique basename:
     - `plot_<chart-type>_<topic-slug>_<uuid8>`
   - Save:
     - `<basename>.png` as the final chart image
     - `<basename>.json` metadata with rendering context
   - Metadata JSON must include:
     - `request`: inferred user intent and options
     - `chart`: chart type, title, axis labels, styling choices
     - `data_summary`: row count, column names, dropped/cleaned rows count
     - `artifacts`: absolute or workspace-relative artifact paths
     - `generated_at`: ISO-8601 timestamp in UTC

5. Return concise user-facing output.
   - Provide a short summary of what was plotted.
   - Include the artifact path(s) for PNG and metadata JSON.
   - If assumptions were made (for example chart-type default), state them briefly.

6. Handle failures explicitly.
   - If plotting fails due to invalid data shape, report the specific mismatch.
   - If execution fails due to transient runtime issues, retry once.
   - Never fabricate data points to make the plot render.

7. Clean up temporary files only.
   - Remove temporary scripts and intermediate files created during plotting.
   - Do not delete final artifacts inside `output/plot-data`.
