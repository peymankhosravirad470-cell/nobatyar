"""پالایش و مقداردهی پیش‌فرض تنظیمات منوی عمومی مراکز."""


def effective_public_menu(clinic) -> dict:
    """پروژکشن allow-list برای سایت/ربات؛ پیش‌نویس هرگز از این مسیر خوانده نمی‌شود."""
    default = {
        "menu_title": "منوی اختصاصی مرکز",
        "show_doctors": True,
        "show_services": True,
        "show_phone": True,
        "show_directions": True,
        "show_online_booking": True,
        "show_address": True,
        "show_working_hours": True,
        "items": [],
    }
    config = getattr(clinic, "public_menu_config", None)
    if not isinstance(config, dict):
        return default
    try:
        # اعتبارسنجی دوباره برای رکوردهای قدیمی/خراب؛ هیچ کلید دلخواهی pass-through نمی‌شود.
        from schemas import ClinicPublicMenuConfig
        return ClinicPublicMenuConfig.model_validate(config).model_dump(mode="json")
    except Exception:
        # رکورد خراب نباید ناخواسته داده‌ها یا رزرو آنلاین را دوباره منتشر کند.
        restricted = default.copy()
        for key in (
            "show_doctors", "show_services", "show_phone", "show_directions",
            "show_online_booking", "show_address", "show_working_hours",
        ):
            restricted[key] = False
        return restricted


LEGACY_DEMO_CLINIC_MARKER = "نمونه — نسخه نمایشی"


def public_clinic_visibility_clause():
    """SQLAlchemy clause for enabled clinics, excluding retired demo-seeder rows."""
    from models import Clinic
    return Clinic.is_enabled.is_(True) & ~Clinic.name.like(f"%{LEGACY_DEMO_CLINIC_MARKER}%")
