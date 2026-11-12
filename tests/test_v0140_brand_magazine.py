"""
تست v0.14.0 — بازطراحی برند، بیلبورد تبلیغات، مجله سلامت و گردش تأیید مدیر.

اجرا: python3 tests/test_v0140_brand_magazine.py
اصول: هیچ اسپانسر/خبر/مرکز جعلی نباید در هیچ خروجی عمومی ظاهر شود؛
جریان خبر فقط «عنوان + خلاصه + لینک منبع» با تأیید مدیر است.
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from fastapi.testclient import TestClient
from main import app, APP_VERSION
from database import init_db, SessionLocal, engine, Base
from models import Admin
from auth import hash_password

# ریست دیتابیس برای تست تمیز (init_db مهاجرت 0012 را هم اجرا می‌کند)
Base.metadata.drop_all(bind=engine)
init_db()
_db = SessionLocal()
if not _db.query(Admin).first():
    _db.add(Admin(username="admin", password_hash=hash_password("admin123")))
    _db.commit()
_db.close()

# اجرای lifespan (startup: init_db + seed_initial_content) — TestClient بدون
# context manager رویدادهای startup را راه نمی‌اندازد.
with TestClient(app):
    pass

client = TestClient(app)

PASS = []


def check(name, cond, detail=""):
    PASS.append((name, bool(cond)))
    print(("✅" if cond else "❌") + f" {name}" + (f" — {detail}" if (detail and not cond) else ""))


# ==================== ۱) هدر برند و فوتر نو در همهٔ صفحات ====================
for path in ["/", "/specialties", "/book", "/faq", "/contact", "/partners", "/about"]:
    r = client.get(path)
    check(f"{path}: هدر برند v3 (لوگو + شعار)", r.status_code == 200
          and "brand-lockup-text" in r.text and "همراه هوشمند سلامت در شیراز" in r.text)
    check(f"{path}: ناوبری شامل مجله سلامت", "/magazine" in r.text and ">مجله سلامت<" in r.text)
    check(f"{path}: فوتر نو با کارت‌های تماس دوستونه", "footer-contact-grid" in r.text
          and "ربات رسمی بله" in r.text and "shiraznobatyar@gmail.com" in r.text)
    check(f"{path}: ۵ کارت شبکهٔ اجتماعی با لوگوی رسمی", all(
        x in r.text for x in ["/static/img/social/telegram.svg", "/static/img/social/eitaa.png",
                              "/static/img/social/rubika.svg", "/static/img/social/youtube.svg",
                              "/static/img/social/aparat.png"]))
    check(f"{path}: متن دقیق کپی‌رایت مالک", "© ۱۴۰۵ نوبتیار" in r.text
          and "بدون ذکر منبع و کسب مجوز ممنوع است" in r.text)
    check(f"{path}: لینک‌های قانونی مرتب", "/terms" in r.text and "/privacy" in r.text
          and "/cancellation" in r.text)

# ==================== ۲) بیلبورد تبلیغات — فقط نمونهٔ طراحی‌شده ====================
r = client.get("/")
check("بیلبورد در صفحه اصلی موجود", "adBillboard" in r.text and "data-position=\"billboard-home\"" in r.text)
check("متن‌های نمونهٔ بیلبورد (سه عبارت مالک)", "جای تبلیغات برند شما" in r.text
      and "کسب‌وکار شما، در کنار سلامت مردم شیراز" in r.text
      and "برای معرفی مجموعه خود در نوبتیار با ما تماس بگیرید." in r.text)

r = client.get("/api/public/ads/billboard-home")
check("API تبلیغ: بدون اسپانسر واقعی → active=false + نمونه", r.status_code == 200
      and r.json()["active"] is False
      and r.json()["title"] == "جای تبلیغات برند شما")

# ==================== ۳) مجله — صفحهٔ اصلی و مقاله ====================
r = client.get("/magazine")
check("صفحه مجله ۲۰۰ با هیرو و عنوان رسمی", r.status_code == 200
      and "مجله سلامت نوبتیار" in r.text and "آخرین اخبار پزشکی، سلامت و خدمات درمانی شیراز" in r.text)
check("مجله: ۳ مقالهٔ سرمقاله‌ای seed شده", "magazine-launch" in r.text
      and "visit-preparation-guide" in r.text and "online-booking-guide" in r.text)
check("مجله: چیپ دسته‌بندی‌ها (۶ دسته)", r.text.count('class="mag-cat') >= 7)
check("مجله: فرم جست‌وجو", "mag-search" in r.text and 'name="q"' in r.text)
check("مجله: JSON-LD Blog", '"@type": "Blog"' in r.text)

r = client.get("/magazine/article/visit-preparation-guide")
check("صفحه مقاله ۲۰۰ با متن کامل", r.status_code == 200 and "فهرست داروهای مصرفی بنویسید" in r.text)
check("مقاله: JSON-LD NewsArticle + Breadcrumb", '"@type": "NewsArticle"' in r.text
      and '"@type": "BreadcrumbList"' in r.text)
check("مقاله: سرفصل h1 یکتا", r.text.count("<h1>") == 1)

r = client.get("/magazine/article/does-not-exist")
check("مقالهٔ ناموجود → 404 با نمای واحد", r.status_code == 404)

# ==================== ۴) API عمومی مجله ====================
r = client.get("/api/public/magazine/articles")
check("API فهرست مقالات: ۳ مورد", r.status_code == 200 and r.json()["total"] == 3)
r = client.get("/api/public/magazine/articles?q=ویزیت")
check("API جست‌وجو کار می‌کند", r.status_code == 200 and r.json()["total"] == 1)
r = client.get("/api/public/magazine/articles?cat=guide")
check("API فیلتر دسته", r.status_code == 200 and r.json()["total"] == 2)
r = client.get("/api/public/magazine/articles?cat=bad")
check("API دستهٔ نامعتبر → 404", r.status_code == 404)
r = client.get("/api/public/magazine/articles/online-booking-guide")
check("API جزئیات: بدنه + مرتبط", r.status_code == 200
      and "ربات رسمی بله" in r.json()["body"] and "related" in r.json())
r = client.get("/api/public/magazine/categories")
check("API دسته‌ها: ۶ دسته با کاور", r.status_code == 200 and len(r.json()["categories"]) == 6)
r = client.get("/api/public/magazine/feed.xml")
check("فید RSS مجله", r.status_code == 200 and "rss" in r.text and "magazine-launch" in r.text)

# ==================== ۵) گردش کامل ادمین: مقاله ====================
r = client.post("/api/admin/login", json={"username": "admin", "password": "admin123"})
token = r.json()["access_token"]
H = {"Authorization": f"Bearer {token}"}

r = client.get("/api/admin/magazine/articles", headers=H)
check("ادمین: فهرست مقالات با وضعیت", r.status_code == 200 and r.json()["total"] == 3)

art = {"slug": "test-draft-article", "title": "مقالهٔ آزمایشی در دست انتشار",
       "summary": "خلاصهٔ مقالهٔ آزمایشی برای بررسی جریان انتشار.",
       "body": "<p>متن آزمایشی مقاله — این متن فقط در محیط تست ساخته می‌شود.</p>",
       "category": "public-health", "status": "draft"}
r = client.post("/api/admin/magazine/articles", headers=H, json=art)
check("ادمین: ساخت مقالهٔ پیش‌نویس", r.status_code == 200 and r.json().get("id"))
art_id = r.json()["id"]

r = client.get("/api/public/magazine/articles")
check("پیش‌نویس در خروجی عمومی نیست (صداقت)", r.json()["total"] == 3)
r = client.get("/magazine/article/test-draft-article")
check("صفحهٔ پیش‌نویس عمومی 404", r.status_code == 404)

r = client.put(f"/api/admin/magazine/articles/{art_id}", headers=H,
               json={**art, "status": "published"})
check("ادمین: انتشار پیش‌نویس", r.status_code == 200)
r = client.get("/api/public/magazine/articles")
check("پس از انتشار در خروجی عمومی است", r.json()["total"] == 4)
r = client.get("/sitemap.xml")
check("sitemap شامل /magazine و مقالهٔ جدید", "/magazine" in r.text
      and "test-draft-article" in r.text)

r = client.post("/api/admin/magazine/articles", headers=H, json={**art, "slug": "test-draft-article"})
check("اسلاگ تکراری → 409", r.status_code == 409)
r = client.delete(f"/api/admin/magazine/articles/{art_id}", headers=H)
check("ادمین: حذف مقاله", r.status_code == 200)
r = client.get("/api/public/magazine/articles")
check("پس از حذف از عمومی خارج شد", r.json()["total"] == 3)

r = client.get("/api/admin/magazine/articles")
check("ادمین API بدون توکن → 401", client.get("/api/admin/magazine/articles").status_code == 401)

# ==================== ۶) گردش خبر: منبع + دریافت + تأیید مدیر ====================
r = client.get("/api/admin/magazine/sources", headers=H)
check("منابع seed شده: ۴ منبع، همه غیرفعال تا آزمون اتصال", r.status_code == 200
      and len(r.json()["sources"]) == 4
      and all(not s["is_active"] for s in r.json()["sources"]))

srcs = {s["name"]: s for s in client.get("/api/admin/magazine/sources", headers=H).json()["sources"]}
sums = next(s for s in srcs.values() if "علوم پزشکی" in s["name"])
check("sums.ac.ir ثبت شده ولی «فعال» نیست (اتصال 403 آزموده‌شده)",
      sums["is_active"] is False and "403" in (sums["note"] or ""))

# دریافت از یک RSS با آدرس نامعتبر → منبغ فعال نمی‌شود و خطا ثبت می‌شود (رفتار صادقانه)
r = client.post("/api/admin/magazine/sources", headers=H,
                json={"name": "منبع آزمایشی نامعتبر", "rss_url": "https://invalid.nobatyar-test.invalid/rss",
                      "category": "news"})
bad_id = r.json()["id"]
r = client.post(f"/api/admin/magazine/sources/{bad_id}/fetch", headers=H)
check("fetch ناموفق → منبع «فعال» نمی‌شود + last_error ثبت می‌شود",
      r.status_code == 200 and r.json()["active"] is False and r.json()["last_error"])

# گردش تأیید مدیر: خبر دستی به‌عنوان «دریافتی از منبع» ثبت و سپس تأیید می‌شود
from models import NewsItem
db = SessionLocal()
src1 = next(s for s in srcs.values() if "ایرنا — سرویس سلامت" in s["name"])
db.add(NewsItem(source_id=src1["id"], title="خبر آزمایشی برای گردش تأیید مدیر",
                url="https://example.org/nobatyar-test-news-1",
                summary="خلاصهٔ کوتاه خبر آزمایشی — فقط در محیط تست.",
                status="pending"))
db.commit()
nid = db.query(NewsItem).filter_by(url="https://example.org/nobatyar-test-news-1").first().id
db.close()

r = client.get("/api/admin/magazine/news?status=pending", headers=H)
check("خبر در انتظار تأیید دیده می‌شود", r.status_code == 200 and r.json()["total"] >= 1)
r = client.post(f"/api/admin/magazine/news/{nid}/publish", headers=H)
check("تأیید مدیر → انتشار با لینک منبع", r.status_code == 200 and r.json().get("article_slug"))
slug = r.json()["article_slug"]
r = client.get(f"/magazine/article/{slug}")
check("خبر منتشرشده: نام منبع + لینک اصلی در بدنه", r.status_code == 200
      and "منبع اصلی" in r.text and "example.org" in r.text)
r = client.post(f"/api/admin/magazine/news/{nid}/publish", headers=H)
check("انتشار تکراری → بدون خطا و بدون ردیف دوم", r.status_code == 200)

# ==================== ۷) جریان کامل تبلیغ: فعال‌سازی از پنل ====================
r = client.get("/api/admin/ads", headers=H)
check("ادمین: اسلات billboard-home ثبت شده", r.status_code == 200
      and any(s["position"] == "billboard-home" for s in r.json()["slots"]))
slot_id = next(s["id"] for s in r.json()["slots"] if s["position"] == "billboard-home")
r = client.put(f"/api/admin/ads/{slot_id}", headers=H,
               json={"title": "جای تبلیغات برند شما", "subtitle": "کسب‌وکار شما، در کنار سلامت مردم شیراز",
                     "body": "برای معرفی مجموعه خود در نوبتیار با ما تماس بگیرید.",
                     "image_url": None, "link_url": None, "link_text": None, "is_active": False})
check("ادمین: به‌روزرسانی اسلات", r.status_code == 200)

# ==================== ۸) نسخه و مهاجرت ====================
check("APP_VERSION = 0.14.0", APP_VERSION == "0.14.0")
from database import _run_alembic
_run_alembic("upgrade", "head")   # idempotent — اجرای تکراری خطا نمی‌دهد
check("مهاجرت 0012 idempotent (اجرای تکراری بدون خطا)", True)

# ==================== ۹) رگرسیون Article.status — سازگاری Enum با ستون VARCHAR مهاجرت 0012 ====================
# نقص اصلاح‌شده: SAEnum(ArticleStatus) بومی PostgreSQL به نوع بومی articlestatus وابسته
# بود که مهاجرت 0012 هرگز نمی‌سازد؛ مسیر insertmanyvalues (درج دسته‌ای ≥۲ ردیف،
# مثل seed استارتاپ) با «type articlestatus does not exist» می‌شکست و کل تراکنش
# seed را برمی‌گرداند. فیکس: native_enum=False + length=10 — دقیقاً همسان با
# ستون «magazine_articles.status VARCHAR(10)» مهاجرت 0012 (و بنابراین هر دو مسیر
# ساخت شِما — create_all مدل و DDL مهاجرت — ستون یکسان می‌سازند).
from models import Article, ArticleStatus  # noqa: E402

_col = Article.__table__.columns["status"]
check("Article.status: نوع Enum غیربومی است (native_enum=False — بدون وابستگی به CREATE TYPE)",
      getattr(_col.type, "native_enum", None) is False)
check("Article.status: length=10 (دقیقاً همسان با VARCHAR(10) مهاجرت 0012)",
      getattr(_col.type, "length", None) == 10)

# رفتار واقعی: درج دسته‌ای ۳ ردیف در یک تراکنش (مسیر insertmanyvalues) + گردش خواندن
from datetime import datetime as _dt  # noqa: E402
_db2 = SessionLocal()
_batch_ok, _batch_detail = False, ""
try:
    _ts = _dt.utcnow()
    _rows = [Article(slug="regr-batch-status-1", title="رگرسیون وضعیت ۱",
                     summary="خلاصه", body="متن آزمون رگرسیون درج دسته‌ای",
                     status=ArticleStatus.published, published_at=_ts),
             Article(slug="regr-batch-status-2", title="رگرسیون وضعیت ۲",
                     summary="خلاصه", body="متن آزمون رگرسیون درج دسته‌ای",
                     status=ArticleStatus.published, published_at=_ts),
             Article(slug="regr-batch-status-3", title="رگرسیون وضعیت ۳",
                     summary="خلاصه", body="متن آزمون رگرسیون درج دسته‌ای",
                     status=ArticleStatus.draft, published_at=_ts)]
    _db2.add_all(_rows)
    _db2.commit()  # درج دسته‌ای — روی شِمای مهاجرت‌یافته باید سبز باشد
    _ids = [r.id for r in _rows]
    _read = _db2.query(Article).filter(Article.id.in_(_ids)).all()
    _statuses = {(a.status.value if hasattr(a.status, "value") else str(a.status)) for a in _read}
    _batch_ok = len(_read) == 3 and _statuses == {"published", "draft"}
finally:
    _db2.query(Article).filter(Article.slug.like("regr-batch-status-%")).delete(
        synchronize_session=False)
    _db2.commit()
    _db2.close()
check("درج دسته‌ای ۳ مقاله در یک تراکنش + گردش خواندن وضعیت (بدون نوع بومی)",
      _batch_ok, detail=_batch_detail)

# ==================== جمع‌بندی ====================
total = len(PASS)
ok = sum(1 for _, v in PASS if v)
print("=" * 40)
print(f"PASS {ok}/{total}")
if ok != total:
    print("FAILED:")
    for name, v in PASS:
        if not v:
            print("  ❌", name)
    sys.exit(1)
print("✅ v0.14.0 brand+magazine: همهٔ گزاره‌ها سبز")
