from __future__ import annotations

import argparse
import sys
from pathlib import Path

import uvicorn

from .app import create_app
from .config import Config


def main() -> int:
    parser = argparse.ArgumentParser(description="HTTP-сервис настройки роутеров.")
    parser.add_argument("--env-file", type=Path, help="Файл конфигурации; по умолчанию .env в каталоге проекта.")
    args = parser.parse_args()
    try:
        config = Config.load(args.env_file)
    except (OSError, ValueError) as exc:
        print(f"Ошибка конфигурации: {exc}", file=sys.stderr)
        return 1
    uvicorn.run(create_app(config), host=config.host, port=config.port, workers=1, access_log=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
