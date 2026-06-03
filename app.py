import os
import re
import hmac
import hashlib
import logging
import threading
import time as _time
from datetime import datetime, timedelta, time, date

import pytz
import requests
from dotenv import load_dotenv
from flask import Flask, request, jsonify
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.jobstores.memory import MemoryJobStore

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
app = Flask(__name__)

SOLAPI_API_KEY    = os.getenv("SOLAPI_API_KEY", "")
SOLAPI_API_SECRET = os.getenv("SOLAPI_API_SECRET", "")
SOLAPI_SENDER     = os.getenv("SOLAPI_SENDER", "")
KST = pytz.timezone("Asia/Seoul")
APP_URL = os.getenv("APP_URL", "")

scheduler = BackgroundScheduler(jobstores={"default": MemoryJobStore()}, timezone=KST)
if not scheduler.running:
    scheduler.start()

def _keep_alive():
    while True:
        _time.sleep(600)
        try:
            requests.get(f"{APP_URL}/health", timeout=10)
        except:
            pass

threading.Thread(target=_keep_alive, daemon=True).start()

def send_text_response(text):
    return {"version": "2.0", "template": {"outputs": [{"simpleText": {"text": text}}]}}

def _auth_header():
    now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    salt = os.urandom(16).hex()
    sig = hmac.new(SOLAPI_API_SECRET.encode(), (now+salt).encode(), hashlib.sha256).hexdigest()
    return {"Authorization": f"HMAC-SHA256 apiKey={SOLAPI_API_KEY}, date={now}, salt={salt}, signature={sig}", "Content-Type": "application/json"}

def send_sms(phone, text):
    resp = requests.post("https://api.solapi.com/messages/v4/send", headers=_auth_header(),
        json={"message": {"to": phone, "from": SOLAPI_SENDER, "text": text, "type": "SMS"}}, timeout=10)
    if resp.status_code not in (200, 201):
        logger.error("SMS FAILED | %s | %s", resp.status_code, resp.text)
    else:
        logger.info("SMS SENT to %s", phone)

# Seminar room booking:
# - Reservation opens 5 days before at 00:00
# - Alerts at 30min, 10min, 5min before reservation opens
FORMAT_HELP = (
    "입력 형식: 전화번호 월/일\n"
    "예시: 01012345678 06/10\n\n"
    "• 세미나실 예약일 5일 전 자정부터 예약 가능\n"
    "• 예약 오픈 30분 전, 10분 전, 5분 전에 SMS 알림 발송"
)

INPUT_PATTERN = re.compile(r'^(01\d{8,9})\s+(\d{1,2})[/월]\s*(\d{1,2})일?\s*$')

def parse_date(month, day):
    now = datetime.now(tz=KST).date()
    year = now.year
    try:
        d = date(year, month, day)
    except ValueError:
        return None
    if d <= now:
        try:
            d = date(year+1, month, day)
        except ValueError:
            return None
    return d

def schedule_alerts(phone, booking_date):
    now_kst   = datetime.now(tz=KST)
    open_dt   = KST.localize(datetime.combine(booking_date - timedelta(days=5), time(0, 0)))
    date_label = booking_date.strftime("%m월 %d일")
    scheduled = []

    for mins, label in [(30, "30분 전"), (10, "10분 전"), (5, "5분 전")]:
        fire_at = open_dt - timedelta(minutes=mins)
        if fire_at <= now_kst:
            continue
        job_id  = f"seminar_{phone}_{booking_date}_{mins}min"
        message = f"[세미나실 알림] {date_label} 세미나실 예약 오픈 {label}입니다!"
        scheduler.add_job(send_sms, trigger="date", run_date=fire_at,
            args=[phone, message], id=job_id, replace_existing=True, misfire_grace_time=120)
        scheduled.append((label, fire_at.strftime("%m/%d %H:%M")))
        logger.info("Scheduled seminar alert | %s | %s KST", label, fire_at)

    return scheduled

@app.route("/webhook", methods=["POST"])
def webhook():
    body      = request.get_json(silent=True) or {}
    utterance = body.get("userRequest", {}).get("utterance", "").strip()
    logger.info("Utterance: %s", utterance)

    if not utterance:
        return jsonify(send_text_response(FORMAT_HELP))

    m = INPUT_PATTERN.match(utterance)
    if not m:
        return jsonify(send_text_response("⚠️ 입력 형식이 올바르지 않습니다.\n\n" + FORMAT_HELP))

    phone = m.group(1)
    booking_date = parse_date(int(m.group(2)), int(m.group(3)))
    if not booking_date:
        return jsonify(send_text_response("⚠️ 날짜가 올바르지 않습니다.\n\n" + FORMAT_HELP))

    scheduled = schedule_alerts(phone, booking_date)
    date_str  = booking_date.strftime("%m월 %d일")

    if not scheduled:
        return jsonify(send_text_response(
            f"⚠️ {date_str} 알림을 설정할 수 없습니다.\n예약 오픈 시간이 이미 지났습니다."))

    lines = "\n".join(f"  • {l}: {t} KST" for l, t in scheduled)
    return jsonify(send_text_response(
        f"✅ 세미나실 예약 알림이 설정되었습니다!\n"
        f"📅 예약 날짜: {date_str}\n"
        f"📱 SMS 수신: {phone}\n"
        f"🔔 알림 일정:\n{lines}"
    ))

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "server_time_kst": datetime.now(tz=KST).strftime("%Y-%m-%d %H:%M:%S %Z")})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)), debug=False)
