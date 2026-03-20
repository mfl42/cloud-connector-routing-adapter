from __future__ import annotations

import shlex
from dataclasses import dataclass


@dataclass(slots=True)
class VyosApiClient:
    base_url: str
    api_key: str
    verify_tls: bool = False
    timeout: float = 30.0

    def configure_commands(self, commands: list[str]) -> dict:
        responses: list[dict] = []
        for command in commands:
            responses.append(self._configure_command(command))
        return {
            "success": all(item.get("success", True) for item in responses),
            "operations": responses,
        }

    def show_config(self) -> dict:
        return self._post("/retrieve", {"op": "showConfig", "path": []})

    def _configure_command(self, command: str) -> dict:
        tokens = shlex.split(command)
        if not tokens:
            raise ValueError("empty VyOS command")

        operation = tokens[0]
        if operation not in {"set", "delete"}:
            raise ValueError(f"unsupported VyOS configure operation: {operation}")

        if operation == "delete":
            return self._post("/configure", {"op": "delete", "path": tokens[1:]})

        if len(tokens) < 2:
            raise ValueError(f"invalid VyOS set command: {command}")

        if len(tokens) == 2:
            return self._post("/configure", {"op": "set", "path": tokens[1:]})

        try:
            return self._post(
                "/configure",
                {
                    "op": "set",
                    "path": tokens[1:-1],
                    "value": tokens[-1],
                },
            )
        except RuntimeError:
            return self._post("/configure", {"op": "set", "path": tokens[1:]})

    def _post(self, endpoint: str, data: dict) -> dict:
        import requests

        response = requests.post(
            f"{self.base_url.rstrip('/')}{endpoint}",
            data={"key": self.api_key, "data": _json_dump(data)},
            timeout=self.timeout,
            verify=self.verify_tls,
        )
        body = response.json()
        if response.status_code >= 400:
            raise RuntimeError(
                f"VyOS API {endpoint} failed with HTTP {response.status_code}: {body}"
            )
        return body


def _json_dump(data: dict) -> str:
    import json

    return json.dumps(data)
