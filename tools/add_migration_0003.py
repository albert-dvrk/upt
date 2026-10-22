#!/usr/bin/env python3
"""Создаёт миграцию Alembic «workers.agent_token_hash», подцепляя её к текущей голове цепочки.

Запуск из корня репозитория:  python3 tools/add_migration_0003.py
Повторный запуск ничего не делает, если миграция уже есть.
"""

import re
import sys
from pathlib import Path

VERSIONS = Path(__file__).resolve().parent.parent / "coordinator" / "migrations" / "versions"
REV = "0003"
TEMPLATE = '''"""Токен агента: хеш в таблице workers.

Revision ID: {rev}
Revises: {down}
"""

import sqlalchemy as sa
from alembic import op

revision = "{rev}"
down_revision = "{down}"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {{c["name"] for c in sa.inspect(op.get_bind()).get_columns("workers")}}
    if "agent_token_hash" not in columns:
        op.add_column("workers", sa.Column("agent_token_hash", sa.String(64), nullable=True))


def downgrade() -> None:
    op.drop_column("workers", "agent_token_hash")
'''


def read_chain() -> dict[str, str | None]:
    chain = {}
    for f in VERSIONS.glob("*.py"):
        text = f.read_text(encoding="utf-8")
        rev = re.search(r"^revision(?:\s*:\s*[^=]+)?\s*=\s*['\"]([^'\"]+)['\"]", text, re.M)
        down = re.search(r"^down_revision(?:\s*:\s*[^=]+)?\s*=\s*(None|['\"]([^'\"]+)['\"])", text, re.M)
        if rev:
            chain[rev.group(1)] = down.group(2) if down and down.group(2) else None
    return chain


def main() -> int:
    chain = read_chain()
    if REV in chain:
        print(f"Миграция {REV} уже есть — ничего не делаю.")
        return 0
    if not chain:
        print("В coordinator/migrations/versions нет миграций. Сначала должны быть 0001 и 0002.", file=sys.stderr)
        return 1
    parents = {p for p in chain.values() if p}
    heads = [r for r in chain if r not in parents]
    if len(heads) != 1:
        print(f"Ожидалась одна голова цепочки, найдено: {heads}", file=sys.stderr)
        return 1
    target = VERSIONS / f"{REV}_agent_token.py"
    target.write_text(TEMPLATE.format(rev=REV, down=heads[0]), encoding="utf-8")
    print(f"Создано {target.relative_to(target.parent.parent.parent.parent)} (после {heads[0]})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
