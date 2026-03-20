from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import NodeNetplanConfig
from .models import NodeNetworkConfig
from .models import load_document


def load_documents(path: str | Path) -> list[NodeNetworkConfig | NodeNetplanConfig]:
    raw = Path(path).read_text()
    docs: list[dict[str, Any]] = []

    try:
        parsed_json = json.loads(raw)
    except json.JSONDecodeError:
        parsed_json = None

    if isinstance(parsed_json, list):
        docs.extend(item for item in parsed_json if isinstance(item, dict))
    elif isinstance(parsed_json, dict):
        docs.append(parsed_json)
    else:
        try:
            import yaml
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "YAML input requires PyYAML. Install dependencies from pyproject.toml "
                "or use JSON input."
            ) from exc

        docs.extend(item for item in yaml.safe_load_all(raw) if isinstance(item, dict))

    return [load_document(item) for item in docs if item.get("kind")]
