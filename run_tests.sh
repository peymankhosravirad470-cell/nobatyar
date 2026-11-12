#!/bin/bash
# ============================================================================
# Nobatyar — تک‌فرمان مستند اجرای کامل مجموعهٔ تست‌ها (v0.13.0)
#
# اجرا:   bash run_tests.sh
# خروجی:  گزارش هر فایل + جمع‌بندی کشف‌شده/اجرا شده/موفق/ناموفق/ردشده
# Exit Code کل: 0 = همهٔ فایل‌های قابل‌اجرا واقعاً سبز؛ 1 = شکست یا «سبزِ پوچ»
#
# طبقه‌بندی صادقانهٔ هر فایل:
#   PASS    — exit=0 و گام‌های واقعی اجرا شده (شمارش ✅ یا «N passed» pytest)
#   FAIL    — exit≠0 (غیر از کد skip=3)
#   SKIP    — exit=3 یا pytest فقط «skipped» (محیط PG موجود نیست؛
#             هرگز به‌عنوان موفق شمرده نمی‌شود — بند ۳/۷ دستور کارفرما)
#   VACUOUS — exit=0 ولی هیچ گامی اجرا نشده («سبزِ پوچ» = شکست مجموعه)
#
# متغیرهای محیطی اختیاری:
#   NOBATYAR_TEST_PG_URI      URI PostgreSQL واقعی برای ۳ آزمون PG
#                             (مثلاً postgresql://postgres@127.0.0.1:5432/postgres)
#   WAITLIST_RACE_ROUNDS      تعداد دور نزاع فهرست انتظار (پیش‌فرض ۱۰۰)
#
# هیچ تماسی با دیتابیس تولیدی ندارد؛ PG فقط با دیتابیس‌های اختصاصیِ
# «nobatyar_migration_test» و «nobatyar_race_test» کار می‌کند (drop/create).
# ============================================================================
set -u
cd "$(dirname "$0")" || exit 9

LOGDIR="${NOBATYAR_TEST_LOGDIR:-/tmp/nobatyar_test_logs}"
rm -rf "$LOGDIR"; mkdir -p "$LOGDIR"
export PYTHONUNBUFFERED=1
export SECRET_KEY="${SECRET_KEY:-nobatyar-test-suite-secret-not-for-prod}"

TOTAL_FILES=0; N_PASS=0; N_FAIL=0; N_SKIP=0; N_VAC=0
FAIL_LIST=""; SKIP_LIST=""; VAC_LIST=""
declare -a SUMMARY

echo "=================================================================="
echo " Nobatyar test suite — $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
echo " PG بیرونی: ${NOBATYAR_TEST_PG_URI:-<تنظیم نشده — آزمون‌های PG فقط در صورت pgserver یا skip شفاف>}"
echo "=================================================================="

for t in tests/test_*.py; do
  f=$(basename "$t")
  TOTAL_FILES=$((TOTAL_FILES+1))
  LOG="$LOGDIR/$f.log"
  python3 "$t" > "$LOG" 2>&1
  rc=$?
  ok_marks=$(grep -c '✅' "$LOG" 2>/dev/null || true)
  pytest_passed=$(grep -oE '[0-9]+ passed' "$LOG" | head -1 | grep -oE '[0-9]+' || true)

  status=""
  if [ "$rc" -eq 3 ]; then
    status="SKIP"; N_SKIP=$((N_SKIP+1)); SKIP_LIST="$SKIP_LIST $f"
  elif [ "$rc" -eq 0 ]; then
    if [ "${pytest_passed:-0}" -gt 0 ] 2>/dev/null || [ "${ok_marks:-0}" -gt 0 ] 2>/dev/null; then
      status="PASS"; N_PASS=$((N_PASS+1))
    else
      status="VACUOUS"; N_VAC=$((N_VAC+1)); VAC_LIST="$VAC_LIST $f"
    fi
  else
    status="FAIL(rc=$rc)"; N_FAIL=$((N_FAIL+1)); FAIL_LIST="$FAIL_LIST $f"
  fi

  steps="${pytest_passed:-$ok_marks}"
  printf "%-40s exit=%-3s steps=%-5s %s\n" "$f" "$rc" "$steps" "$status"
  SUMMARY+=("$f|$rc|$steps|$status")
done

echo "=================================================================="
echo " فایل‌های کشف‌شده: $TOTAL_FILES"
echo " اجرا شده (PASS): $N_PASS"
echo " ناموفق (FAIL):   $N_FAIL$FAIL_LIST"
echo " ردشده (SKIP):    $N_SKIP$SKIP_LIST"
echo " سبزِ پوچ (VAC):  $N_VAC$VAC_LIST"
echo "=================================================================="

# تفکیک گام‌های واقعی برای گزارش نهایی
REAL_STEPS=$(grep -h '✅' "$LOGDIR"/*.log 2>/dev/null | wc -l)
PYTEST_STEPS=$(grep -hoE '[0-9]+ passed' "$LOGDIR"/*.log 2>/dev/null | grep -oE '^[0-9]+' | awk '{s+=$1} END {print s+0}')
echo " گام‌های واقعی سبز: script-style=$REAL_STEPS pytest=$PYTEST_STEPS"
echo " لاگ‌ها: $LOGDIR"
echo "=================================================================="

if [ "$N_FAIL" -gt 0 ] || [ "$N_VAC" -gt 0 ]; then
  echo " نتیجه: RED — شکست یا سبزِ پوچ وجود دارد"; exit 1
fi
echo " نتیجه: GREEN — همهٔ فایل‌های اجرا شده واقعاً سبز (skipها شفاف گزارش شد)"
exit 0
