from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values


@dataclass(frozen=True)
class Config:
    api_key: str = field(repr=False)
    router_username: str = field(repr=False)
    router_password: str = field(repr=False)
    list_path: Path
    data_dir: Path
    host: str = "127.0.0.1"
    port: int = 8765
    max_parallel_jobs: int = 4

    def __post_init__(self) -> None:
        if type(self.max_parallel_jobs) is not int or not 1 <= self.max_parallel_jobs <= 32:
            raise ValueError("MAX_PARALLEL_JOBS должен быть целым числом от 1 до 32.")

    @classmethod
    def load(cls, env_file: Path | None = None) -> "Config":
        path = (env_file or Path(__file__).resolve().parent.parent / ".env").resolve()
        values = dict(dotenv_values(path, interpolate=False)) if path.exists() else {}
        names = ("API_KEY", "ROUTER_USERNAME", "ROUTER_PASSWORD", "LIST_PATH", "DATA_DIR", "HOST", "PORT", "MAX_PARALLEL_JOBS")
        values.update({name: os.environ[name] for name in names if name in os.environ})
        for name in ("API_KEY", "ROUTER_USERNAME", "ROUTER_PASSWORD"):
            if not values.get(name) or not values[name].strip():
                raise ValueError(f"Не задан обязательный параметр {name}.")
        try:
            port = int(values.get("PORT") or "8765")
        except ValueError:
            raise ValueError("PORT должен быть целым числом.") from None
        if not 1 <= port <= 65535:
            raise ValueError("PORT должен быть от 1 до 65535.")
        try:
            max_parallel_jobs = int(values.get("MAX_PARALLEL_JOBS") or "4")
        except ValueError:
            raise ValueError("MAX_PARALLEL_JOBS должен быть целым числом от 1 до 32.") from None

        def resolve(name: str, default: str) -> Path:
            value = Path(values.get(name) or default).expanduser()
            return (value if value.is_absolute() else path.parent / value).resolve()

        return cls(
            api_key=values["API_KEY"],
            router_username=values["ROUTER_USERNAME"],
            router_password=values["ROUTER_PASSWORD"],
            list_path=resolve("LIST_PATH", "lists/router.txt"),
            data_dir=resolve("DATA_DIR", ".data"),
            host=values.get("HOST") or "127.0.0.1",
            port=port,
            max_parallel_jobs=max_parallel_jobs,
        )

    def redact(self, message: str) -> str:
        for value in sorted((self.api_key, self.router_username, self.router_password), key=len, reverse=True):
            if value:
                message = message.replace(value, "[REDACTED]")
        return message
