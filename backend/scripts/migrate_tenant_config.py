#!/usr/bin/env python3
"""Preview canonical tenant settings; pass --apply for an insert-only migration."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from routes.deps import db  # noqa: E402
from tenant_config import migrate_tenant_config  # noqa: E402


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="insert if canonical config is absent")
    args = parser.parse_args()
    result = await migrate_tenant_config(db, apply=args.apply)
    # Config has no secrets by contract; refs are opaque vault keys.
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
