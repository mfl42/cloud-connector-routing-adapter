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
        ops = [self._build_operation(cmd) for cmd in commands]
        try:
            return self._post("/configure-list", ops)
        except RuntimeError:
            return self._configure_commands_sequential(commands)

    def discard_pending(self) -> dict:
        """Discard any uncommitted staged changes on the VyOS target."""
        try:
            return self._post("/configure", {"op": "discard"})
        except RuntimeError as exc:
            return {"success": False, "error": str(exc)}

    def _configure_commands_sequential(self, commands: list[str]) -> dict:
        responses: list[dict] = []
        for command in commands:
            resp = self._configure_command(command)
            responses.append(resp)
            if not resp.get("success", True) and not _is_idempotent_response(resp):
                self.discard_pending()
                return {
                    "success": False,
                    "error": str(resp.get("error") or "command failed"),
                    "operations": responses,
                }
        return {
            "success": all(
                item.get("success", True) or _is_idempotent_response(item)
                for item in responses
            ),
            "operations": responses,
        }

    def _build_operation(self, command: str) -> dict:
        tokens = shlex.split(command)
        if not tokens:
            raise ValueError("empty VyOS command")
        operation = tokens[0]
        if operation not in {"set", "delete"}:
            raise ValueError(f"unsupported VyOS configure operation: {operation}")
        if operation == "delete":
            return {"op": "delete", "path": tokens[1:]}
        if len(tokens) < 2:
            raise ValueError(f"invalid VyOS set command: {command}")
        if len(tokens) == 2:
            return {"op": "set", "path": tokens[1:]}
        return {"op": "set", "path": tokens[1:-1], "value": tokens[-1]}

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


def _is_idempotent_response(body: dict) -> bool:
    error = str(body.get("error") or "").lower()
    return (
        "already exists" in error
        or "is already defined" in error
        or "already set" in error
        or "is already present" in error
    )


def _json_dump(data: dict) -> str:
    import json

    return json.dumps(data)
