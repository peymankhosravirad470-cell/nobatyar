"""مجله سلامت نوبتیار + جایگاه تبلیغات (v0.14.0) — APIRouter مستقل.

اصول صادقانه (الزام مالک):
- هیچ خبر/مقاله/اسپانسر جعلی تولید نمی‌شود؛ محتوا فقط از پنل مدیریت می‌آید.
- از منابع خبری فقط «عنوان + خلاصهٔ اختصاصی + لینک اصلی + نام منبع + تاریخ»
  نمایش داده می‌شود؛ متن کامل خبر منبع هرگز کپی نمی‌شود.
- منبعی که اتصال واقعی‌اش آزموده نشده «فعال» معرفی نمی‌شود.
- اخبار دریافتی پیش از انتشار عمومی باید تأیید مدیر شوند.
- جایگاه تبلیغات از هویت برند کاملاً جدا است؛ وقتی اسپانسر واقعی نداریم
  فقط «جای تبلیغات برند شما» (نمونهٔ طراحی‌شده) برگردانده می‌شود.
"""
import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import List, Optional
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import or_
from sqlalchemy.orm import Session

from database import get_db
from auth import get_current_admin
from models import (AdSlot, Article, ArticleStatus, NewsItem, NewsSource)

LOG = logging.getLogger("nobatyar.magazine")
AdminDep = Depends(get_current_admin)

router = APIRouter(prefix="/api/public/magazine", tags=["magazine"])
admin_router = APIRouter(prefix="/api/admin", tags=["magazine-admin"])

# ---------- دسته‌بندی‌های ثابت مجله ----------
CATEGORIES = [
    {"slug": "news", "name": "اخبار پزشکی", "cover": "/static/img/v14-mag-cover-news.webp"},
    {"slug": "public-health", "name": "سلامت عمومی", "cover": "/static/img/v14-mag-cover-health.webp"},
    {"slug": "hospitals", "name": "بیمارستان‌ها و مراکز درمانی", "cover": "/static/img/v14-mag-cover-hospitals.webp"},
    {"slug": "events", "name": "رویدادهای پزشکی شیراز", "cover": "/static/img/v14-mag-cover-events.webp"},
    {"slug": "labs-tech", "name": "آزمایشگاه‌ها و فناوری سلامت", "cover": "/static/img/v14-mag-cover-labtech.webp"},
    {"slug": "guide", "name": "راهنمای سلامت", "cover": "/static/img/v14-mag-cover-guide.webp"},
]
_CATEGORY_SLUGS = {c["slug"] for c in CATEGORIES}
_DEFAULT_COVER = "/static/img/v14-magazine-hero.webp"


def category_name(slug: str) -> str:
    for c in CATEGORIES:
        if c["slug"] == slug:
            return c["name"]
    return slug


def category_cover(slug: str) -> str:
    for c in CATEGORIES:
        if c["slug"] == slug:
            return c["cover"]
    return _DEFAULT_COVER


# ==================== مدل‌های ورودی ====================

class ArticleIn(BaseModel):
    slug: str = Field(..., min_length=2, max_length=150)
    title: str = Field(..., min_length=3, max_length=220)
    summary: str = Field(..., min_length=3, max_length=1000)
    body: str = Field(..., min_length=10)
    category: str = Field("guide", max_length=40)
    cover_image: Optional[str] = Field(None, max_length=300)
    author: Optional[str] = Field(None, max_length=80)
    status: str = Field("published", max_length=10)  # = VARCHAR(10) مهاجرت 0012
    published_at: Optional[datetime] = None

    @field_validator("slug")
    @classmethod
    def _slug(cls, v: str) -> str:
        v = v.strip().lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9\-]{1,148}[a-z0-9]", v):
            raise ValueError("اسلاگ فقط حروف/رقم/خط تیره لاتین")
        return v

    @field_validator("category")
    @classmethod
    def _cat(cls, v: str) -> str:
        if v not in _CATEGORY_SLUGS:
            raise ValueError("دستهٔ نامعتبر")
        return v

    @field_validator("status")
    @classmethod
    def _status(cls, v: str) -> str:
        if v not in ("draft", "published"):
            raise ValueError("وضعیت نامعتبر")
        return v


class AdSlotIn(BaseModel):
    title: str = Field("", max_length=200)
    subtitle: str = Field("", max_length=300)
    body: str = Field("", max_length=1000)
    image_url: Optional[str] = Field(None, max_length=300)
    link_url: Optional[str] = Field(None, max_length=400)
    link_text: Optional[str] = Field(None, max_length=120)
    is_active: bool = False

    @field_validator("image_url", "link_url")
    @classmethod
    def _safe_url(cls, v: Optional[str]) -> Optional[str]:
        """سخت‌سازی امنیتی v0.15.0 — فقط http/https مطلق یا مسیر نسبی داخل خود سایت.
        javascript:/data:/vbscript: و هر scheme دیگر → 422 (ضد تزریق URL)."""
        if v is None:
            return v
        v = v.strip()
        if not v:
            return None
        low = v.lower()
        if low.startswith(("https://", "http://", "/")):
            if "javascript:" in low or "data:" in low or "vbscript:" in low:
                raise ValueError("این نشانی مجاز نیست")
            return v
        raise ValueError("نشانی باید http/https یا مسیر نسبی داخل سایت باشد")


class NewsSourceIn(BaseModel):
    name: str = Field(..., min_length=2, max_length=120)
    site_url: Optional[str] = Field(None, max_length=300)
    rss_url: str = Field(..., min_length=10, max_length=400)
    category: str = Field("news", max_length=40)
    note: Optional[str] = Field(None, max_length=300)


# ==================== ابزار عمومی ====================

def _article_out(a: Article) -> dict:
    return {
        "id": a.id, "slug": a.slug, "title": a.title, "summary": a.summary,
        "category": a.category, "category_name": category_name(a.category),
        "cover_image": a.cover_image or category_cover(a.category),
        "author": a.author, "views": a.views,
        "published_at": a.published_at.isoformat() if a.published_at else None,
    }


def _get_published_article(db: Session, slug: str) -> Optional[Article]:
    return db.query(Article).filter(
        Article.slug == slug, Article.status == ArticleStatus.published).first()


def _require_admin_article(db: Session, article_id: int) -> Article:
    a = db.query(Article).get(article_id)
    if not a:
        raise HTTPException(404, "مقاله یافت نشد")
    return a


# ==================== API عمومی مجله ====================

@router.get("/categories")
def magazine_categories():
    return {"categories": CATEGORIES}


@router.get("/articles")
def magazine_articles(
    q: Optional[str] = Query(None, max_length=60),
    cat: Optional[str] = Query(None, max_length=40),
    page: int = Query(1, ge=1),
    per_page: int = Query(9, ge=1, le=30),
    db: Session = Depends(get_db),
):
    """فهرست مقالات منتشرشده — جست‌وجو/فیلتر دسته + صفحه‌بندی."""
    query = db.query(Article).filter(Article.status == ArticleStatus.published)
    if cat:
        if cat not in _CATEGORY_SLUGS:
            raise HTTPException(404, "دستهٔ نامعتبر")
        query = query.filter(Article.category == cat)
    if q:
        ns = q.strip()[:60]
        if ns:
            query = query.filter(or_(
                Article.title.ilike(f"%{ns}%"),
                Article.summary.ilike(f"%{ns}%"),
            ))
    total = query.count()
    rows = query.order_by(Article.published_at.desc(), Article.id.desc()).offset(
        (page - 1) * per_page).limit(per_page).all()
    return {"total": total, "page": page, "per_page": per_page,
            "articles": [_article_out(a) for a in rows]}


@router.get("/articles/{slug}")
def magazine_article(slug: str, db: Session = Depends(get_db)):
    a = _get_published_article(db, slug)
    if not a:
        raise HTTPException(404, "مقاله یافت نشد")
    a.views = (a.views or 0) + 1
    db.commit()
    related = db.query(Article).filter(
        Article.status == ArticleStatus.published,
        Article.category == a.category, Article.id != a.id,
    ).order_by(Article.published_at.desc()).limit(3).all()
    out = _article_out(a)
    out["body"] = a.body
    out["updated_at"] = a.updated_at.isoformat() if a.updated_at else None
    out["related"] = [_article_out(r) for r in related]
    return out


@router.get("/feed.xml", include_in_schema=False)
def magazine_feed(db: Session = Depends(get_db)):
    """فید RSS مجله نوبتیار — عنوان/خلاصه/لینک؛ بدون PHI."""
    base = (os.environ.get("NOBATYAR_SITE_URL") or "https://shirazhealthtour.ir").rstrip("/")
    rows = db.query(Article).filter(Article.status == ArticleStatus.published).order_by(
        Article.published_at.desc()).limit(20).all()
    items = []
    for a in rows:
        esc = lambda s: (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        items.append(
            f"<item><title>{esc(a.title)}</title>"
            f"<link>{base}/magazine/article/{a.slug}</link>"
            f"<guid>{base}/magazine/article/{a.slug}</guid>"
            f"<description>{esc(a.summary)}</description>"
            f"<pubDate>{a.published_at.strftime('%a, %d %b %Y %H:%M:%S +0000')}</pubDate></item>")
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0"><channel>'
        f"<title>مجله سلامت نوبتیار</title>"
        f"<link>{base}/magazine</link>"
        f"<description>آخرین اخبار پزشکی، سلامت و خدمات درمانی شیراز</description>"
        f"{''.join(items)}</channel></rss>"
    )
    return Response(xml, media_type="application/rss+xml")


# ==================== API عمومی تبلیغات ====================

ads_router = APIRouter(prefix="/api/public/ads", tags=["ads"])

_PLACEHOLDER = {
    "title": "جای تبلیغات برند شما",
    "subtitle": "کسب‌وکار شما، در کنار سلامت مردم شیراز",
    "body": "برای معرفی مجموعه خود در نوبتیار با ما تماس بگیرید.",
    "image_url": None,
    "link_url": None,
    "link_text": None,
}


@ads_router.get("/{position}")
def public_ad(position: str, db: Session = Depends(get_db)):
    """جایگاه تبلیغاتی — بدون اسپانسر جعلی.

    اسلات فعال واقعی → محتوای آن؛ در غیر این صورت → نمونهٔ طراحی‌شده (placeholder)."""
    slot = db.query(AdSlot).filter(AdSlot.position == position).first()
    if slot and slot.is_active:
        return {"active": True, "position": position,
                "title": slot.title, "subtitle": slot.subtitle, "body": slot.body,
                "image_url": slot.image_url, "link_url": slot.link_url,
                "link_text": slot.link_text}
    return {"active": False, "position": position, **_PLACEHOLDER}


# ==================== پنل مدیریت: مقالات ====================

@admin_router.get("/magazine/articles")
def admin_articles(status: Optional[str] = None, page: int = 1,
                   per_page: int = Query(20, ge=1, le=100),
                   db: Session = Depends(get_db), admin=AdminDep):
    query = db.query(Article)
    if status in ("draft", "published"):
        query = query.filter(Article.status == (ArticleStatus.published
                                                if status == "published" else ArticleStatus.draft))
    total = query.count()
    rows = query.order_by(Article.published_at.desc()).offset(
        (page - 1) * per_page).limit(per_page).all()
    out = [_article_out(a) for a in rows]
    for o, a in zip(out, rows):
        o["body"] = a.body
        o["status"] = a.status.value if hasattr(a.status, "value") else str(a.status)
    return {"total": total, "page": page, "articles": out}


@admin_router.post("/magazine/articles")
def admin_article_create(data: ArticleIn, db: Session = Depends(get_db),
                         admin=AdminDep):
    if db.query(Article).filter(Article.slug == data.slug).first():
        raise HTTPException(409, "اسلاگ تکراری است")
    a = Article(slug=data.slug, title=data.title, summary=data.summary,
                body=data.body, category=data.category,
                cover_image=data.cover_image, author=data.author,
                status=ArticleStatus.published if data.status == "published"
                else ArticleStatus.draft,
                published_at=data.published_at or datetime.utcnow())
    db.add(a)
    db.commit()
    db.refresh(a)
    return {"message": "مقاله ثبت شد", "id": a.id, "slug": a.slug}


@admin_router.put("/magazine/articles/{article_id}")
def admin_article_update(article_id: int, data: ArticleIn,
                         db: Session = Depends(get_db), admin=AdminDep):
    a = _require_admin_article(db, article_id)
    clash = db.query(Article).filter(
        Article.slug == data.slug, Article.id != article_id).first()
    if clash:
        raise HTTPException(409, "اسلاگ تکراری است")
    a.slug, a.title, a.summary, a.body = data.slug, data.title, data.summary, data.body
    a.category, a.cover_image, a.author = data.category, data.cover_image, data.author
    a.status = (ArticleStatus.published if data.status == "published"
                else ArticleStatus.draft)
    if data.published_at:
        a.published_at = data.published_at
    db.commit()
    return {"message": "مقاله به‌روزرسانی شد", "id": a.id}


@admin_router.delete("/magazine/articles/{article_id}")
def admin_article_delete(article_id: int, db: Session = Depends(get_db),
                         admin=AdminDep):
    a = _require_admin_article(db, article_id)
    db.delete(a)
    db.commit()
    return {"message": "مقاله حذف شد", "id": article_id}


# ==================== پنل مدیریت: جایگاه تبلیغات ====================

@admin_router.get("/ads")
def admin_ads(db: Session = Depends(get_db), admin=AdminDep):
    slots = db.query(AdSlot).order_by(AdSlot.position).all()
    return {"slots": [{
        "id": s.id, "position": s.position, "title": s.title,
        "subtitle": s.subtitle, "body": s.body, "image_url": s.image_url,
        "link_url": s.link_url, "link_text": s.link_text,
        "is_active": bool(s.is_active),
        "updated_at": s.updated_at.isoformat() if s.updated_at else None,
    } for s in slots]}


@admin_router.put("/ads/{slot_id}")
def admin_ad_update(slot_id: int, data: AdSlotIn, db: Session = Depends(get_db),
                    admin=AdminDep):
    s = db.query(AdSlot).get(slot_id)
    if not s:
        raise HTTPException(404, "جایگاه تبلیغاتی یافت نشد")
    s.title, s.subtitle, s.body = data.title, data.subtitle, data.body
    s.image_url, s.link_url, s.link_text = data.image_url, data.link_url, data.link_text
    s.is_active = bool(data.is_active)
    # v0.15.0: هر تغییر جایگاه تبلیغ در حسابرسی مدیریتی ثبت می‌شود (بدون هیچ رمزی)
    from site_content import _audit
    _audit(db, admin, "ویرایش جایگاه تبلیغات", f"ads/{s.position}",
           f"فعال={s.is_active}")
    db.commit()
    return {"message": "جایگاه تبلیغاتی به‌روزرسانی شد", "id": s.id,
            "active": bool(s.is_active)}


# ==================== پنل مدیریت: منابع خبر + گردش تأیید ====================

@admin_router.get("/magazine/sources")
def admin_sources(db: Session = Depends(get_db), admin=AdminDep):
    rows = db.query(NewsSource).order_by(NewsSource.id).all()
    return {"sources": [{
        "id": s.id, "name": s.name, "site_url": s.site_url, "rss_url": s.rss_url,
        "category": s.category, "is_active": bool(s.is_active), "note": s.note,
        "last_fetched_at": s.last_fetched_at.isoformat() if s.last_fetched_at else None,
        "last_error": s.last_error,
    } for s in rows]}


@admin_router.post("/magazine/sources")
def admin_source_create(data: NewsSourceIn, db: Session = Depends(get_db),
                        admin=AdminDep):
    if db.query(NewsSource).filter(NewsSource.rss_url == data.rss_url).first():
        raise HTTPException(409, "این RSS از قبل ثبت شده است")
    # صداقت: منبع جدید تا آزمون واقعی اتصال «غیرفعال» ثبت می‌شود؛
    # فعال‌سازی فقط از مسیر آزمون موفق fetch انجام می‌شود.
    s = NewsSource(name=data.name, site_url=data.site_url, rss_url=data.rss_url,
                   category=data.category if data.category in _CATEGORY_SLUGS else "news",
                   is_active=False, note=data.note)
    db.add(s)
    db.commit()
    db.refresh(s)
    return {"message": "منبع ثبت شد (غیرفعال تا آزمون واقعی اتصال)", "id": s.id}


@admin_router.put("/magazine/sources/{source_id}")
def admin_source_update(source_id: int, data: NewsSourceIn,
                        db: Session = Depends(get_db), admin=AdminDep):
    s = db.query(NewsSource).get(source_id)
    if not s:
        raise HTTPException(404, "منبع یافت نشد")
    s.name, s.site_url, s.note = data.name, data.site_url, data.note
    s.category = data.category if data.category in _CATEGORY_SLUGS else s.category
    db.commit()
    return {"message": "منبع به‌روزرسانی شد", "id": s.id}


@admin_router.delete("/magazine/sources/{source_id}")
def admin_source_delete(source_id: int, db: Session = Depends(get_db),
                        admin=AdminDep):
    s = db.query(NewsSource).get(source_id)
    if not s:
        raise HTTPException(404, "منبع یافت نشد")
    db.delete(s)
    db.commit()
    return {"message": "منبع حذف شد", "id": source_id}


_FETCH_STATE = {"last": 0.0}


def _safe_url(url: str) -> bool:
    """فقط http/https بدون نویسهٔ نقل‌قول/براکت — لینک فید خارجی قبل از ذخیره/نمایش."""
    u = (url or "").strip()
    return bool(re.match(r"^https?://[^\s'\"<>]+$", u[:500]))


def _safe_href(url: str) -> str:
    """لینک امن برای درج در HTML (پس از تأیید مدیر) — escape کامل + بررسی scheme."""
    from html import escape
    u = (url or "").strip()
    if not _safe_url(u):
        return "#"
    return escape(u, quote=True)


def _fetch_source(db: Session, src: NewsSource, max_items: int = 12) -> int:
    """دریافت RSS یک منبع + ذخیرهٔ موارد تازه به‌عنوان «در انتظار تأیید».

    - فقط عنوان/لینک/خلاصهٔ کوتاه/تاریخ از فید خوانده می‌شود (بدون متن کامل).
    - ضدتکرار با url یکتا (سطح دیتابیس).
    - بازهٔ زمانی حداقلی بین fetchها (۶۰ ثانیه) برای هر فراخوانی انبوه."""
    now_ts = time.time()
    if now_ts - _FETCH_STATE["last"] < 1.0:
        time.sleep(1.0)
    _FETCH_STATE["last"] = now_ts
    added = 0
    try:
        req = Request(src.rss_url, headers={"User-Agent": "NobatyarMagazine/1.0 (+https://shirazhealthtour.ir)"})
        with urlopen(req, timeout=20) as resp:
            raw = resp.read(2_000_000)
        root = ET.fromstring(raw)
        channel = root.find("channel") if root.tag == "rss" else root
        items = channel.findall("item")[:max_items] if channel is not None else []
        for it in items:
            def _t(tag):
                el = it.find(tag)
                return (el.text or "").strip() if el is not None and el.text else ""
            title = _t("title")[:300]
            link = _t("link") or (_t("guid")[:500])
            desc_el = it.find("description")
            desc = re.sub(r"<[^>]+>", " ", desc_el.text or "") if desc_el is not None and desc_el.text else ""
            desc = re.sub(r"\s+", " ", desc).strip()[:400]
            pub_raw = _t("pubDate")
            pub_dt = None
            if pub_raw:
                try:
                    pub_dt = parsedate_to_datetime(pub_raw)
                except Exception:  # noqa: BLE001
                    pub_dt = None
            if not title or not link:
                continue
            # امنیت: فقط لینک http/https بدون نویسهٔ خطرناک — جلوگیری از XSS از فید متخاصم
            if not _safe_url(link):
                continue
            if db.query(NewsItem).filter(NewsItem.url == link).first():
                continue
            db.add(NewsItem(source_id=src.id, title=title, url=link[:500],
                            summary=desc, published_at_source=pub_dt))
            added += 1
        db.commit()
        src.last_fetched_at = datetime.utcnow()
        src.last_error = None
        src.is_active = True   # اتصال واقعی موفق بود → مجاز به «فعال» بودن
        db.commit()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        src.last_fetched_at = datetime.utcnow()
        src.last_error = f"{type(exc).__name__}"
        # منبع دردسترس‌نیافته «فعال» معرفی نمی‌شود (الزام مالک)
        src.is_active = False
        db.commit()
        LOG.warning("fetch source %s failed: %s", src.name, type(exc).__name__)
    return added


@admin_router.post("/magazine/sources/{source_id}/fetch")
def admin_source_fetch(source_id: int, db: Session = Depends(get_db),
                       admin=AdminDep):
    s = db.query(NewsSource).get(source_id)
    if not s:
        raise HTTPException(404, "منبع یافت نشد")
    added = _fetch_source(db, s)
    return {"message": "دریافت انجام شد", "added": added,
            "active": bool(s.is_active), "last_error": s.last_error}


@admin_router.get("/magazine/news")
def admin_news(status: str = "pending", page: int = 1, per_page: int = Query(30, ge=1, le=100),
               db: Session = Depends(get_db), admin=AdminDep):
    if status not in ("pending", "published", "rejected", "all"):
        raise HTTPException(404, "وضعیت نامعتبر")
    query = db.query(NewsItem)
    if status != "all":
        query = query.filter(NewsItem.status == status)
    total = query.count()
    rows = query.order_by(NewsItem.fetched_at.desc()).offset(
        (page - 1) * per_page).limit(per_page).all()
    return {"total": total, "items": [{
        "id": n.id, "title": n.title, "url": n.url, "summary": n.summary,
        "status": n.status, "source": n.source.name if n.source else None,
        "published_at_source": n.published_at_source.isoformat() if n.published_at_source else None,
        "fetched_at": n.fetched_at.isoformat() if n.fetched_at else None,
    } for n in rows]}


@admin_router.post("/magazine/news/{item_id}/publish")
def admin_news_publish(item_id: int, db: Session = Depends(get_db),
                       admin=AdminDep):
    """تأیید مدیر → خبر به‌صورت «آیتم خبری با لینک منبع» عمومی می‌شود.

    فقط عنوان + خلاصهٔ کوتاه + لینک اصلی نمایش داده می‌شود؛ کپی متن کامل نیست."""
    n = db.query(NewsItem).get(item_id)
    if not n:
        raise HTTPException(404, "خبر یافت نشد")
    if n.status == "published":
        return {"message": "از قبل منتشر شده", "id": n.id}
    from html import escape as _hesc
    _url_html = _safe_href(n.url)
    a = Article(
        slug=f"news-{n.id}-{datetime.utcnow().strftime('%Y%m%d')}",
        title=n.title[:220],
        summary=(n.summary or n.title)[:900],
        body=(f"<p>{_hesc((n.summary or n.title), quote=False)}</p>"
              f"<p class='news-source-note'>منبع اصلی: "
              f"<a href='{_url_html}' rel='noopener nofollow' target='_blank'>{_hesc(n.url, quote=False)}</a></p>"),
        category=n.source.category if n.source else "news",
        author=n.source.name if n.source else None,
        status=ArticleStatus.published,
        published_at=n.published_at_source or datetime.utcnow(),
    )
    db.add(a)
    db.commit()
    db.refresh(a)
    n.status = "published"
    n.published_article_id = a.id
    db.commit()
    return {"message": "خبر با تأیید مدیر منتشر شد", "article_id": a.id,
            "article_slug": a.slug}


@admin_router.post("/magazine/news/{item_id}/reject")
def admin_news_reject(item_id: int, db: Session = Depends(get_db),
                      admin=AdminDep):
    n = db.query(NewsItem).get(item_id)
    if not n:
        raise HTTPException(404, "خبر یافت نشد")
    n.status = "rejected"
    db.commit()
    return {"message": "خبر رد شد", "id": n.id}


# ==================== مقالات سرمقاله‌ای آغاز مجله (محتوای واقعی — تألیف تیم نوبتیار) ====================

_SEED_ARTICLES = [
    {
        "slug": "magazine-launch",
        "title": "مجله سلامت نوبتیار آغاز به کار کرد؛ مرجع اخبار و آموزش سلامت شیراز",
        "summary": ("بخش «مجله سلامت نوبتیار» با هدف پوشش اخبار پزشکی، سلامت عمومی و خدمات درمانی "
                    "شیراز آغاز به کار کرد. این مجله به منابع خبری معتبر ارجاع می‌دهد، متن کامل "
                    "رسانه‌های دیگر را کپی نمی‌کند و هر خبر پیش از انتشار توسط مدیر بررسی می‌شود."),
        "body": ("<p>نوبتیار از امروز فقط یک سامانه نوبت‌دهی نیست؛ با راه‌اندازی «مجله سلامت نوبتیار»، "
                 "تلاش می‌کنیم به مرجعی برای اخبار، آموزش و معرفی خدمات سلامت شیراز تبدیل شویم.</p>"
                 "<h2>هدف ما چیست؟</h2>"
                 "<p>بیمار ایرانی پیش از هر تصمیم درمانی، به دنبال اطلاعات معتبر است. متأسفانه بخش "
                 "بزرگی از محتوای سلامت در فضای مجازی بدون منبع و بدون بازبینی منتشر می‌شود. "
                 "مجله سلامت نوبتیار با سه قاعدهٔ ساده کار می‌کند:</p>"
                 "<ul>"
                 "<li><b>ارجاع به منبع:</b> هر خبر تأییدشده، نام منبع، تاریخ و لینک اصلی را به‌طور واضح نمایش می‌دهد.</li>"
                 "<li><b>بدون کپی متن کامل:</b> متن کامل خبرهای رسانه‌های دیگر کپی نمی‌شود؛ فقط عنوان، خلاصهٔ اختصاصی و لینک اصلی منتشر می‌شود.</li>"
                 "<li><b>تأیید مدیر:</b> هیچ خبری بدون بررسی و تأیید مدیر به انتشار عمومی نمی‌رسد.</li>"
                 "</ul>"
                 "<h2>چه چیزهایی منتشر می‌شود؟</h2>"
                 "<p>اخبار پزشکی، سلامت عمومی، معرفی بیمارستان‌ها و مراکز درمانی، رویدادهای پزشکی شیراز، "
                 "و آزمایشگاه‌ها و فناوری سلامت — به‌همراه مطالب آموزشی «راهنمای سلامت» که توسط تیم "
                 "نوبتیار برای بیماران نوشته می‌شود.</p>"
                 "<h2>منابع خبری</h2>"
                 "<p>در گام نخست، اتصال به منابع معتبر ملی دارای پوشش سلامت و اخبار استان فارس برقرار شده "
                 "است. منبعی که اتصال واقعی‌اش آزموده نشده باشد، هرگز به‌عنوان «منبع فعال» معرفی نمی‌شود و "
                 "زمان آخرین به‌روزرسانی هر منبع در پنل مدیریت قابل مشاهده است.</p>"
                 "<p>اگر مجموعهٔ شما در حوزهٔ سلامت شیراز خبر یا رویدادی برای انتشار دارد، از صفحهٔ "
                 "<a href='/contact'>تماس با ما</a> با ما در ارتباط باشید.</p>"),
        "category": "news",
        "author": "تیم نوبتیار",
        "status": "published",
    },
    {
        "slug": "visit-preparation-guide",
        "title": "راهنمای آماده‌سازی برای ویزیت پزشک؛ ۸ نکتهٔ کاربردی برای بیماران",
        "summary": ("آماده‌شدن درست برای ویزیت پزشک، هم زمان شما و هم پزشک را بهینه می‌کند. در این "
                    "راهنمای آموزشی، هشت نکتهٔ عمومی و کاربردی برای مدیریت بهتر ویزیت را مرور می‌کنیم."),
        "body": ("<p>ویزیت کوتاه است و هر دقیقه آن ارزشمند است. این راهنمای عمومی به شما کمک می‌کند "
                 "با آمادگی بیشتر سر ویزیت حاضر شوید. توجه کنید که این مطلب جایگزین توصیهٔ پزشک "
                 "نیست و برای شرایط اورژانسی باید بلافاصله با اورژانس ۱۱۵ تماس بگیرید.</p>"
                 "<h2>۱) فهرست داروهای مصرفی بنویسید</h2>"
                 "<p>نام همهٔ داروها، مکمل‌ها و گیاهان دارویی مصرفی — همراه با دوز و ساعت مصرف — را "
                 "روی کاغذ بنویسید یا از یادآور داروی نوبتیار (ربات بله) خروجی بگیرید.</p>"
                 "<h2>۲) شرح علائم را با تاریخ مرور کنید</h2>"
                 "<p>علائم از کی شروع شده، چه چیزی آن را بهتر یا بدتر می‌کند و آیا دوره‌ای است؟ "
                 "یک خط زمانی ساده به پزشک کمک زیادی می‌کند.</p>"
                 "<h2>۳) سؤالات خود را از قبل بنویسید</h2>"
                 "<p>دو یا سه سؤال اصلی خود را مرتب کنید تا در پایان ویزیت چیزی از قلم نیفتد.</p>"
                 "<h2>۴) مدارک قبلی را ببرید</h2>"
                 "<p>نتایج آزمایش، جواب سونوگرافی، عکس‌ها و نسخه‌های قبلی مرتبط با مشکل فعلی را همراه داشته باشید.</p>"
                 "<h2>۵) سابقهٔ بیماری خانوادگی را یادداشت کنید</h2>"
                 "<p>بیماری‌های مهم اعضای خانواده (دیابت، فشار خون، سرطان و…) اطلاعاتی کلیدی برای پزشک است.</p>"
                 "<h2>۶) ساعات کاری و نوبت را چک کنید</h2>"
                 "<p>پیش از مراجعه، زمان دقیق نوبت و آدرس مرکز را بررسی کنید تا معطل نشوید. در نوبتیار می‌توانید "
                 "زمان‌های خالی واقعی را ببینید و نوبت بگیرید.</p>"
                 "<h2>۷) بیمه و مدارک هویتی را فراموش نکنید</h2>"
                 "<p>کارت بیمه و مدرک شناسایی را همراه داشته باشید.</p>"
                 "<h2>۸) توصیه‌های پایانی را دقیق یادداشت کنید</h2>"
                 "<p>در پایان ویزیت، دستور دارویی و توصیه‌های پزشک را بنویسید؛ اگر لازم شد از او بپرسید کی باید "
                 "پیگیری کنید. یادآور نوبت بعدی را می‌توانید در ربات بله فعال کنید.</p>"
                 "<p>این مطلب جنبهٔ آموزشی عمومی دارد و جایگزین مشاورهٔ پزشکی شخصی‌سازی‌شده نیست.</p>"),
        "category": "guide",
        "author": "تیم نوبتیار",
        "status": "published",
    },
    {
        "slug": "online-booking-guide",
        "title": "چگونه از نوبتیار نوبت آنلاین بگیریم؟ گام‌به‌گام",
        "summary": ("رزرو نوبت در نوبتیار از وب‌سایت یا ربات بله انجام می‌شود؛ بدون تماس تلفنی و بدون صف. "
                    "در این راهنما، مراحل جست‌وجوی پزشک، انتخاب زمان خالی و مدیریت نوبت را گام‌به‌گام می‌بینیم."),
        "body": ("<p>نوبتیار برای اینکه گرفتن نوبت ساده و شفاف باشد ساخته شده است. در ادامه مراحل "
                 "رزرو نوبت آنلاین را گام‌به‌گام می‌بینید.</p>"
                 "<h2>گام ۱ — جست‌وجو و انتخاب</h2>"
                 "<p>در نوار جست‌وجوی صفحهٔ اصلی، نام پزشک، تخصص، علامت بیماری یا مرکز درمانی را وارد کنید؛ "
                 "یا از صفحهٔ <a href='/specialties'>تخصص‌ها</a> وارد شوید. نتیجه‌ها فقط مرکزهای واقعی و "
                 "تأییدشده را نشان می‌دهند.</p>"
                 "<h2>گام ۲ — دیدن زمان‌های خالی واقعی</h2>"
                 "<p>مرکز درمانی برنامهٔ کاری خود را در نوبتیار تعریف می‌کند؛ بنابراین زمان‌های خالی که "
                 "می‌بینید با تقویم واقعی مرکز هم‌خوان است، نه یک عدد نمایشی.</p>"
                 "<h2>گام ۳ — ثبت نوبت</h2>"
                 "<p>زمان دلخواه را انتخاب کنید و مشخصات خواسته‌شده را وارد کنید. ثبت هم‌زمانِ تکراری برای "
                 "یک نفر روی یک زمان، در سطح دیتابیس مسدود شده است تا دو نفر روی یک نوبت نزنند.</p>"
                 "<h2>گام ۴ — یادآور و پیگیری</h2>"
                 "<p>با اتصال <a href='https://ble.ir/nobatyarshirazbot' target='_blank' rel='noopener'>ربات رسمی بله</a>، "
                 "یادآور نوبت و یادآور مصرف دارو دریافت می‌کنید.</p>"
                 "<h2>گام ۵ — لغو یا تغییر</h2>"
                 "<p>اگر قادر به حضور نیستید، طبق <a href='/cancellation'>سیاست لغو و تغییر نوبت</a> زودتر "
                 "لغو کنید تا زمان برای بیمار دیگر آزاد شود.</p>"
                 "<p>اگر مرکز درمانی هستید و هنوز در نوبتیار ثبت‌نام نکرده‌اید، از "
                 "<a href='/register'>صفحهٔ ثبت‌نام مراکز</a> شروع کنید.</p>"),
        "category": "guide",
        "author": "تیم نوبتیار",
        "status": "published",
    },
]
