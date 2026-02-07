---
name: weather-forecast
description: Get weather forecast data from Open Meteo for a specific time and/or location name
---

# Weather Forecast

## Goal

Fetch weather forecast data with location and/or time parameters from the conversation context and user prompt.

## Workflow

1. Infer the time information from the context and prompt.
    - If no specific time information, ask the user to provide time information.
    - If no time interval provided and/or time (specific date, start/end date), ask the user to provide it.
2. Infer the location information from the context and prompt.
    - If no location information found, ask the user to provide the location information.
    - Use the web search tool to infer the coordinates of the provided location information.
3. Use Open Meteo APIs as the primary source of the weather information.
    - Always use `uv` for python-related operations.
    - If third-party or external libraries are required
        - Check the existing installation using `uv pip freeze`
        - Install them using `uv pip install` if it's absolutely necessary to do so.
    - Output data in JSON format.
    - Save the result inside `output/weather-forecast` directory.
        - If the directory is not exist, create it using the `os` Python standard library.
    - Use unique filename for every result artifact.
        - Combine the appropriate input parameters for the filename and the UUID v4 string.
4. Clean up
    - Always clean up scripts used to complete the task.
5. Provide path information to the final output artifact.
