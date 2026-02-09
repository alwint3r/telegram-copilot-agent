---
name: plot-data
description: Create a plot for data visualization tasks
---

# Plot Data

## Goal

Turn data in the conversation context and user prompt into a beautiful and useful plot.

## Workflow

1. Identify the kind of data available in the conversation context.
2. Use matplotlib library for visualizing the data
    - Format the data into appropriate data structure first before creating the plot.
    - Always use `uv` for python-related operations.
    - Check the availability of the required modules by using `uv pip freeze`
    - If the required modules are not installed locally, use `uv pip install` command to install them.
    - When generating a time-series data and the data scope is within 24 hours (1 full day), use the hours as the X axis so the text does not overflow in the final result.
3. Generate the plot and save it as a PNG file.
    - Always use unique filename by generating UUID v4 using Python standard library.
    - Put the plots and other artifacts necessary for rendering the plot into the `output/plot-data` folder.
        - If the folder or directory is not exist, use the `os` Python standard library to create it. 
4. Cleanup
    - Always clean up custom python script, JSON file, CSV file, or other artifacts used to generate the plot.
