---
name: Use uv instead of pip
description: User prefers uv for Python package installation, not pip
type: feedback
---

Always use `uv` (e.g., `uv run`, `uv pip install`) instead of `pip install` for Python packages.

**Why:** User preference — they use uv as their Python package manager.

**How to apply:** Any time a Python package needs to be installed, use `uv pip install` or `uv run` instead of `pip install`.
