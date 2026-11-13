"""مهاجرت 0013 — بازطراحی 0.15.0: محتوای قابل‌ویرایش مدیر + کاتالوگ تخصص‌ها در دیتابیس + حسابرسی مدیریتی.

جدول‌های جدید (هیچ جدول موجودی تغییر نمی‌کند؛ هیچ داده‌ای دست نمی‌خورد):
  - site_content  متن‌های قابل ویرایش سایت (کلید/مقدار) — فقط کلیدهای سفیدفهرست
  - specialties   کاتالوگ تخصص‌ها — seed اولیه از catalog_data.py؛ فعال/غیرفعال و ترتیب از پنل
  - admin_audit   سوابق عملیات مدیریتی (تاریخ + ساعت + مدیر + عملیات) — بدون هیچ رمزی

Idempotent است (الگوی 0012): اگر جدولی از قبل باشد دوباره ساخته نمی‌شود.
Seed تخصص‌ها فقط وقتی جدول خالی است اجرا می‌شود (ضد تکرار).
Downgrade فقط جدول‌های همین نسخه را حذف می‌کند.
"""
import json
from pathlib import Path

from alembic import op
from sqlalchemy import text

revision = "0013_sitecontent_specialties"
down_revision = "0012_brand_magazine"
branch_labels = None
depends_on = None

_TABLES = {
    "site_content": """
        CREATE TABLE {t} (
            content_key VARCHAR(80) NOT NULL PRIMARY KEY,
            content_value TEXT NOT NULL DEFAULT '',
            updated_at TIMESTAMP,
            updated_by VARCHAR(50)
        )
    """,
    "specialties": """
        CREATE TABLE {t} (
            id {idcol} PRIMARY KEY,
            slug VARCHAR(80) NOT NULL,
            name VARCHAR(120) NOT NULL,
            category VARCHAR(80) NOT NULL,
            sort_order INTEGER NOT NULL DEFAULT {sortdef},
            is_active BOOLEAN NOT NULL DEFAULT {booltrue},
            updated_at TIMESTAMP
        )
    """,
    "admin_audit": """
        CREATE TABLE {t} (
            id {idcol} PRIMARY KEY,
            admin_username VARCHAR(50) NOT NULL,
            action VARCHAR(120) NOT NULL,
            target VARCHAR(200) NOT NULL DEFAULT '',
            detail VARCHAR(400) NOT NULL DEFAULT '',
            created_at TIMESTAMP NOT NULL
        )
    """,
}

_INDEXES = {
    "site_content": [],
    "specialties": [
        ("uq_specialties_slug", "CREATE UNIQUE INDEX uq_specialties_slug ON specialties (slug)"),
        ("ix_specialties_category", "CREATE INDEX ix_specialties_category ON specialties (category)"),
        ("ix_specialties_sort", "CREATE INDEX ix_specialties_sort ON specialties (sort_order)"),
    ],
    "admin_audit": [
        ("ix_admin_audit_created", "CREATE INDEX ix_admin_audit_created ON admin_audit (created_at)"),
    ],
}

# seed از کد مرجع — مسیر backend/catalog_data.py کنار این فایل
_SEED_JSON = Path(__file__).resolve().parent.parent.parent / "backend" / "catalog_data.py"


def _is_sqlite(conn) -> bool:
    return (conn.dialect.name or "") == "sqlite"


def _tables(conn) -> set:
    if _is_sqlite(conn):
        return {r[0] for r in conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type='table'")).fetchall()}
    return {r[0] for r in conn.execute(text(
        "SELECT tablename FROM pg_tables WHERE schemaname='public'")).fetchall()}


def _indexes(conn) -> set:
    if _is_sqlite(conn):
        return {r[0] for r in conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type='index'")).fetchall()}
    return {r[0] for r in conn.execute(text(
        "SELECT indexname FROM pg_indexes WHERE schemaname='public'")).fetchall()}


def _load_specialties():
    """خواندن SPECIALTIES از catalog_data بدون import بار سنگین — با exec ماژول کوچک.

    catalog_data.py فقط دادهٔ ثابت دارد (بدون وابستگی)؛ import مستقیم امن است.
    """
    import sys
    root = Path(__file__).resolve().parent.parent.parent / "backend"
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    import catalog_data  # noqa: E402
    return catalog_data.SPECIALTIES


def upgrade() -> None:
    conn = op.get_bind()
    sqlite = _is_sqlite(conn)
    existing = _tables(conn)
    for name, ddl in _TABLES.items():
        if name in existing:
            continue
        conn.execute(text(ddl.format(
            t=name,
            idcol="INTEGER AUTOINCREMENT" if sqlite else "SERIAL",
            sortdef="0" if sqlite else "0",
            booltrue="1" if sqlite else "true")))
    existing_idx = _indexes(conn)
    for name, idxs in _INDEXES.items():
        if name not in _TABLES:
            continue
        for iname, iddl in idxs:
            if iname in existing_idx:
                continue
            conn.execute(text(iddl))

    # ---------- seed کاتالوگ تخصص‌ها — فقط اگر جدول خالی باشد ----------
    has_rows = conn.execute(text("SELECT COUNT(*) FROM specialties")).scalar() or 0
    if not has_rows:
        specs = _load_specialties()
        if sqlite:
            for i, sp in enumerate(specs):
                conn.execute(text(
                    "INSERT INTO specialties (slug, name, category, sort_order, is_active, updated_at) "
                    "VALUES (:s, :n, :c, :o, 1, NULL)"),
                    {"s": sp["slug"], "n": sp["name"], "c": sp["category"], "o": i})
        else:
            payload = [{"s": sp["slug"], "n": sp["name"], "c": sp["category"], "o": i}
                       for i, sp in enumerate(specs)]
            conn.execute(text(
                "INSERT INTO specialties (slug, name, category, sort_order, is_active, updated_at) "
                "VALUES (:s, :n, :c, :o, true, NULL)"), payload)


def downgrade() -> None:
    conn = op.get_bind()
    existing = _tables(conn)
    for name in ("admin_audit", "specialties", "site_content"):
        if name in existing:
            conn.execute(text(f"DROP TABLE IF EXISTS {name}"))
