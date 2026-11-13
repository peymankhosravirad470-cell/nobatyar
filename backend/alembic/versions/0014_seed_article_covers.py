"""مهاجرت 0014 — کاور اختصاصی مقالات seed مجله (تکمیل دور تصاویر 0.15.0).

سه مقالهٔ سرمقاله‌ای seed با cover_image=NULL ساخته شده بودند و همه از کاور
پیش‌فرض دستهٔ خود استفاده می‌کردند؛ نتیجه: دو مقالهٔ «راهنمای» با کاور تکراری
کنار هم نمایش داده می‌شد. این مهاجرت فقط برای همین سه slug مشخص، کاور اختصاصی
و متمایز ثبت می‌کند و مقالات دیگر (مقالات مدیر) را دست نمی‌زند.

Idempotent: فقط ردیف‌هایی که cover_image خالی دارند به‌روزرسانی می‌شوند.
Downgrade: کاور مقالات seed دوباره NULL می‌شود.
"""
from alembic import op
from sqlalchemy import text

revision = "0014_seed_article_covers"
down_revision = "0013_sitecontent_specialties"
branch_labels = None
depends_on = None

_SEED_COVERS = {
    "magazine-launch": "/static/img/v14-mag-cover-hospitals.webp",
    "visit-preparation-guide": "/static/img/img-reception-new.png",
    "online-booking-guide": "/static/img/v14-mag-cover-news.webp",
}


def _table_exists(conn, name: str) -> bool:
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        q = text("SELECT 1 FROM information_schema.tables WHERE table_name = :n")
    else:
        q = text("SELECT 1 FROM sqlite_master WHERE type='table' AND name = :n")
    return conn.execute(q, {"n": name}).first() is not None


def upgrade() -> None:
    conn = op.get_bind()
    if not _table_exists(conn, "magazine_articles"):
        return  # هنوز جدول مجله ساخته نشده — بعداً seed کاور را می‌گذارد
    for slug, cover in _SEED_COVERS.items():
        conn.execute(
            text("UPDATE magazine_articles SET cover_image = :c "
                 "WHERE slug = :s AND (cover_image IS NULL OR cover_image = '')"),
            {"c": cover, "s": slug},
        )


def downgrade() -> None:
    conn = op.get_bind()
    if not _table_exists(conn, "magazine_articles"):
        return
    for slug in _SEED_COVERS:
        conn.execute(
            text("UPDATE magazine_articles SET cover_image = NULL WHERE slug = :s"),
            {"s": slug},
        )
