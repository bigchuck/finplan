# Project Overview
- **Stack**: Standard Vanilla Python (No heavy frameworks or external bundlers)
- **Primary Use Case**: General-purpose scripts and data logic

# Development Commands
- **Run active script**: `python path/to/script.py`
- **Check syntax**: `python -m py_compile path/to/script.py`

# Architecture & Style Guidelines
- **Formatting**: Strictly follow standard PEP 8 naming styles (snake_case for variables/functions, PascalCase for classes).
- **Indentation**: Explicitly use 4 spaces per indentation level. Do not mix tabs and spaces.
- **Dependencies**: Keep external dependencies to a bare minimum. Do not introduce packages unless explicitly requested in the prompt.
- **Execution**: When running code, always use the standard system `python` interpreter command. Do not attempt to look for uv, poetry, black, or ruff.
