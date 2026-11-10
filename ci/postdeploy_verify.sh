#!/bin/bash
# ============================================================
# راستی‌آزمایی پس از انتشار نوبتیار — اصلاح یافتهٔ F1
# پیش از این: نتایج فقط چاپ می‌شد و حتی با شکست، exit 0 و پیام «انجام شد».
# اکنون: هر گزاره جمع‌بندی می‌شود؛ اگر حتی یک گزاره شکست بخورد exit 1
#        و پیام واضح شکست + یادآوری Rollback چاپ می‌شود.
# سلامت نسخه تا ظاهرشدن نسخهٔ موردانتظار (با مهلت) نظرسنجی می‌شود
# تا یک بررسیِ زودهنگامِ پس از ساخت، نتیجهٔ گمراه‌کننده ندهد (بهبود مرتبط با F3).
#
# قابل‌تست: همهٔ پارامترها با متغیر محیطی قابل‌بازنویسی‌اند:
#   VERIFY_BASE_URL        (پیش‌فرض https://shirazhealthtour.ir)
#   VERIFY_EXPECTED_VERSION (پیش‌فرض 0.11.2)
#   VERIFY_RETRIES         (پیش‌فرض ۸ تلاش)
#   VERIFY_INTERVAL        (پیش‌فرض ۱۵ ثانیه)
# ============================================================
set -u

BASE_URL="${VERIFY_BASE_URL:-https://shirazhealthtour.ir}"
EXPECTED_VERSION="${VERIFY_EXPECTED_VERSION:-0.11.2}"
RETRIES="${VERIFY_RETRIES:-8}"
INTERVAL="${VERIFY_INTERVAL:-15}"

PASS=0
FAIL=0
declare -a FAILED_CHECKS=()

record() { # record <نام> <موفق؟0/1> <جزئیات>
  local name="$1" ok="$2" detail="$3"
  if [ "$ok" = "1" ]; then
    echo "✅ $name: $detail"
    PASS=$((PASS + 1))
  else
    echo "❌ $name: $detail"
    FAIL=$((FAIL + 1))
    FAILED_CHECKS+=("$name")
  fi
}

get_code() { local url="$1"; shift; curl -s -o /dev/null -w "%{http_code}" -m 20 "$@" "$url"; }

echo "== راستی‌آزمایی پس از انتشار: $BASE_URL (نسخهٔ موردانتظار $EXPECTED_VERSION) =="

# ---------- گام ۰: نظرسنجی سلامت تا ظاهرشدن نسخهٔ درست ----------
HEALTH_VERSION=""
i=1
while [ "$i" -le "$RETRIES" ]; do
  H=$(curl -s -m 20 "$BASE_URL/api/health" || true)
  HEALTH_VERSION=$(printf '%s' "$H" | python3 -c 'import json,sys
try: print(json.load(sys.stdin).get("version",""))
except Exception: print("")' 2>/dev/null || true)
  if [ "$HEALTH_VERSION" = "$EXPECTED_VERSION" ]; then
    break
  fi
  echo "  تلاش $i/$RETRIES: version='$HEALTH_VERSION' (منتظر $EXPECTED_VERSION)…"
  sleep "$INTERVAL"
  i=$((i + 1))
done

if [ "$HEALTH_VERSION" = "$EXPECTED_VERSION" ]; then
  record "health/version" 1 "$HEALTH_VERSION"
else
  record "health/version" 0 "نسخهٔ فعال '$HEALTH_VERSION' است نه $EXPECTED_VERSION (پس از $RETRIES تلاش)"
fi

# ---------- صفحات اصلی (فقط اگر نسخه درست بود ادامه معنادار است؛ در هر حال گزاره‌ها جمع‌بندی می‌شوند) ----------
PAGES=("/" "/book" "/register" "/register/office" "/register/clinic" "/partners" "/contact" "/admin/login" "/admin/panel" "/clinic/login" "/terms" "/privacy" "/robots.txt")
for p in "${PAGES[@]}"; do
  code=$(get_code "$BASE_URL$p")
  case "$code" in
    200) record "page $p" 1 "$code" ;;
    *)   record "page $p" 0 "HTTP $code (انتظار 200)" ;;
  esac
done

# ---------- گزاره‌های امنیتی v0.11.2 ----------
code=$(get_code "$BASE_URL/docs")
[ "$code" = "404" ] && record "docs بسته" 1 "HTTP $code" || record "docs بسته" 0 "HTTP $code (انتظار 404)"

code=$(get_code "$BASE_URL/openapi.json")
[ "$code" = "404" ] && record "openapi.json بسته" 1 "HTTP $code" || record "openapi.json بسته" 0 "HTTP $code (انتظار 404)"

code=$(get_code "$BASE_URL/api/clinics/1")
[ "$code" = "404" ] && record "مرکز pending محفوظ" 1 "HTTP $code" || record "مرکز pending محفوظ" 0 "HTTP $code (انتظار 404)"

# ---------- گارد ۴۰۱ صفحات/API مدیریتی بدون توکن ----------
code=$(get_code "$BASE_URL/api/admin/backup/status")
case "$code" in 401|403) record "admin API بدون توکن بسته" 1 "HTTP $code" ;; *) record "admin API بدون توکن بسته" 0 "HTTP $code (انتظار 401/403)" ;; esac

code=$(get_code "$BASE_URL/api/admin/bot/connect-code" -X POST)
case "$code" in 401|403|405) record "admin bot-code بدون توکن بسته" 1 "HTTP $code" ;; *) record "admin bot-code بدون توکن بسته" 0 "HTTP $code (انتظار 401/403)" ;; esac

# ---------- API عمومی ----------
code=$(get_code "$BASE_URL/api/clinics/public")
[ "$code" = "200" ] && record "API عمومی clinics/public" 1 "HTTP $code" || record "API عمومی clinics/public" 0 "HTTP $code (انتظار 200)"

code=$(get_code "$BASE_URL/api/labs")
[ "$code" = "200" ] && record "API عمومی labs" 1 "HTTP $code" || record "API عمومی labs" 0 "HTTP $code (انتظار 200)"

n=$(curl -s -m 20 "$BASE_URL/sitemap.xml" | grep -c 'terms' || true)
[ "${n:-0}" -ge 1 ] && record "sitemap شامل terms" 1 "شمار=$n" || record "sitemap شامل terms" 0 "شمار=$n"

n=$(curl -s -m 20 -D - -o /dev/null -H 'Accept-Encoding: gzip' "$BASE_URL/" | grep -ci 'content-encoding: gzip' || true)
[ "${n:-0}" -eq 1 ] && record "gzip فعال" 1 "header=1" || record "gzip فعال" 0 "header=$n"

n=$(curl -s -m 20 -D - -o /dev/null "$BASE_URL/" | grep -i 'strict-transport' | head -1)
[ -n "$n" ] && record "HSTS" 1 "$n" || record "HSTS" 0 "هدر یافت نشد"

n=$(curl -s -m 20 "$BASE_URL/" | grep -c 'shiraznobatyar@gmail.com' || true)
[ "${n:-0}" -ge 1 ] && record "فوتر رسمی (ایمیل)" 1 "شمار=$n" || record "فوتر رسمی (ایمیل)" 0 "شمار=$n"

n=$(curl -s -m 20 -D - -o /dev/null "$BASE_URL/" | grep -ci 'content-security-policy' || true)
[ "${n:-0}" -ge 1 ] && record "CSP" 1 "header=$n" || record "CSP" 0 "header=$n"

# ---------- جمع‌بندی (قلب اصلاح F1) ----------
TOTAL=$((PASS + FAIL))
echo "=================================================="
echo "جمع‌بندی راستی‌آزمایی: $PASS موفق از $TOTAL گزاره | شکست: $FAIL"
if [ "$FAIL" -gt 0 ]; then
  printf 'گزاره‌های ناموفق: %s\n' "${FAILED_CHECKS[*]}"
  echo "❌ راستی‌آزمایی شکست خورد — انتشار تأیید نمی‌شود؛ طبق گام بازگشت به v0.11.1 بازگردید."
  exit 1
fi
echo "✅ همهٔ گزاره‌ها سبز است؛ نسخهٔ $EXPECTED_VERSION سالم و تأییدشده است."
exit 0
