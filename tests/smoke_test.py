"""Run the bridge's real-git and stdio-MCP smoke suite without credentials."""
from pathlib import Path

import pytest

if __name__ == '__main__':
    root = Path(__file__).resolve().parents[1]
    raise SystemExit(pytest.main(['-q', str(root / 'tests')]))
