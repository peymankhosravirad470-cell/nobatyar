"""مهاجرت 0012 — مجله سلامت نوبتیار + جایگاه تبلیغات (v0.14.0).

جدول‌های جدید (هیچ جدول موجودی تغییر نمی‌کند؛ هیچ داده‌ای دست نمی‌خورد):
  - magazine_articles   مقالات/اخبار مجله — فقط از پنل مدیریت منتشر می‌شود
  - magazine_sources    منابع خبری — فقط منبعی که اتصالش واقعاً آزموده شده «فعال» است
  - magazine_news_items اخبار دریافتی از منابع — «در انتظار تأیید» + url یکتا (ضد تکرار)
  - ad_slots            جایگاه‌های تبلیغاتی مستقل — جدا از هویت برند

Idempotent است: اگر جدولی از قبل وجود داشته باشد (مثلاً create_all در دیتابیس
تازه توسط 0001_baseline) دوباره ساخته نمی‌شود.
Downgrade فقط جدول‌های این نسخه را حذف می‌کند.
"""
from alembic import op
from sqlalchemy import text

revision = "0012_brand_magazine"
down_revision = "0011_waitlist_unique_active"
branch_labels = None
depends_on = None

_TABLES = {
    "magazine_articles": """
        CREATE TABLE {t} (
            id {idcol} PRIMARY KEY,
            slug VARCHAR(160) NOT NULL,
            title VARCHAR(220) NOT NULL,
            summary TEXT,
            body TEXT,
            category VARCHAR(40) NOT NULL DEFAULT 'guide',
            cover_image VARCHAR(300),
            author VARCHAR(80),
            status VARCHAR(10) NOT NULL DEFAULT 'published',
            views INTEGER NOT NULL DEFAULT 0,
            published_at TIMESTAMP NOT NULL,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP
        )
    """,
    "magazine_sources": """
        CREATE TABLE {t} (
            id {idcol} PRIMARY KEY,
            name VARCHAR(120) NOT NULL,
            site_url VARCHAR(300),
            rss_url VARCHAR(400) NOT NULL,
            category VARCHAR(40) NOT NULL DEFAULT 'news',
            is_active BOOLEAN NOT NULL DEFAULT {boolfalse},
            note VARCHAR(300),
            last_fetched_at TIMESTAMP,
            last_error VARCHAR(300),
            created_at TIMESTAMP NOT NULL
        )
    """,
    "magazine_news_items": """
        CREATE TABLE {t} (
            id {idcol} PRIMARY KEY,
            source_id INTEGER NOT NULL REFERENCES magazine_sources (id) ON DELETE CASCADE,
            title VARCHAR(300) NOT NULL,
            url VARCHAR(500) NOT NULL,
            summary TEXT,
            published_at_source TIMESTAMP,
            status VARCHAR(12) NOT NULL DEFAULT 'pending',
            published_article_id INTEGER REFERENCES magazine_articles (id) ON DELETE SET NULL,
            fetched_at TIMESTAMP NOT NULL
        )
    """,
    "ad_slots": """
        CREATE TABLE {t} (
            id {idcol} PRIMARY KEY,
            position VARCHAR(60) NOT NULL,
            title VARCHAR(200) NOT NULL DEFAULT '',
            subtitle VARCHAR(300) NOT NULL DEFAULT '',
            body TEXT,
            image_url VARCHAR(300),
            link_url VARCHAR(400),
            link_text VARCHAR(120),
            is_active BOOLEAN NOT NULL DEFAULT {boolfalse},
            updated_at TIMESTAMP
        )
    """,
}

_INDEXES = {
    "magazine_articles": [
        ("ix_magazine_articles_status_pub", "CREATE INDEX ix_magazine_articles_status_pub ON magazine_articles (status, published_at)"),
        ("ix_magazine_articles_category", "CREATE INDEX ix_magazine_articles_category ON magazine_articles (category)"),
        ("ix_magazine_articles_slug", "CREATE UNIQUE INDEX ix_magazine_articles_slug ON magazine_articles (slug)"),
        ("ix_magazine_articles_published_at", "CREATE INDEX ix_magazine_articles_published_at ON magazine_articles (published_at)"),
        ("ix_magazine_articles_id", "CREATE INDEX ix_magazine_articles_id ON magazine_articles (id)"),
    ],
    "magazine_sources": [
        ("ix_magazine_sources_id", "CREATE INDEX ix_magazine_sources_id ON magazine_sources (id)"),
        ("uq_magazine_sources_rss", "CREATE UNIQUE INDEX uq_magazine_sources_rss ON magazine_sources (rss_url)"),
    ],
    "magazine_news_items": [
        ("ix_magazine_news_items_status_fetched", "CREATE INDEX ix_magazine_news_items_status_fetched ON magazine_news_items (status, fetched_at)"),
        ("ix_magazine_news_items_source_id", "CREATE INDEX ix_magazine_news_items_source_id ON magazine_news_items (source_id)"),
        ("uq_news_item_url", "CREATE UNIQUE INDEX uq_news_item_url ON magazine_news_items (url)"),
        ("ix_magazine_news_items_id", "CREATE INDEX ix_magazine_news_items_id ON magazine_news_items (id)"),
    ],
    "ad_slots": [
        ("ix_ad_slots_is_active", "CREATE INDEX ix_ad_slots_is_active ON ad_slots (is_active)"),
        ("uq_ad_slots_position", "CREATE UNIQUE INDEX uq_ad_slots_position ON ad_slots (position)"),
    ],
}


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
            boolfalse="0" if sqlite else "false")))
    existing_idx = _indexes(conn)
    for name, idxs in _INDEXES.items():
        if name not in _TABLES:
            continue
        if name in existing and name not in existing:
            # جدول از قبل بود ولی ممکن است ایندکس‌های مدل‌محورش کامل نباشد —
            # فقط ایندکس‌های ویژهٔ همین مهاجرت را اضافه می‌کنیم (IF NOT EXISTS نحو مشترک ندارد؛ بررسی دستی)
            pass
        for iname, iddl in idxs:
            if iname in existing_idx:
                continue
            conn.execute(text(iddl))


def downgrade() -> None:
    conn = op.get_bind()
    existing = _tables(conn)
    for name in ("magazine_news_items", "magazine_sources", "magazine_articles", "ad_slots"):
        if name in existing:
            conn.execute(text(f"DROP TABLE IF EXISTS {name}"))
