---
name: Use uv for Python projects
description: User prefers uv for all Python project setup and dependency management
type: feedback
---

Always use `uv` for Python work: `uv init` to create a project, `uv add` to install packages, `uv run` to execute scripts.

**Why:** User explicitly prefers uv over pip/venv/conda.

**How to apply:** Any time Python tooling is needed, reach for uv — don't install packages with pip or check for system Python first.
