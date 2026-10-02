"""出張の領収書を撮ってメールで送るアプリ（Railway の 1 サービスで完結）

- 毎月「第 1 土曜〜その月曜」の出張分として、タクシー・新幹線・マリンライナー（高松→岡山）の
  領収書をスマホのカメラで撮ってためておき、ボタン 1 つで決まった宛先にまとめてメールする
- 出張初日の朝と、月曜の夜（未送信なら翌日以降も 1 週間）にプッシュ通知で知らせる

  python server.py      # http://localhost:8000
"""
import base64
import hashlib
import hmac
import io
import json
import os
import secrets
import smtplib
import ssl
import threading
import uuid
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from pathlib import Path

import requests
import uvicorn
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageOps
from pydantic import BaseModel

HERE = Path(__file__).parent
STATIC = HERE / "static"
DATA = Path(os.environ.get("DATA_DIR", HERE / "data"))
IMG = DATA / "img"
STATE = DATA / "state.json"
JST = timezone(timedelta(hours=9))
PASSWORD = os.environ.get("APP_PASSWORD", "")
WEEK = "月火水木金土日"
MAX_MAIL_BYTES = 20 * 1024 * 1024  # Gmail の上限 25MB より余裕を持たせる

KINDS = {
    "taxi": "タクシー",
    "shinkansen": "新幹線",
    "marine": "マリンライナー（高松→岡山）",
}

_lock = threading.Lock()


# ---------------------------------------------------------------- 保存
def now():
    return datetime.now(JST)


def load():
    if STATE.exists():
        st = json.loads(STATE.read_text(encoding="utf-8"))
    else:
        st = {}
    st.setdefault("settings", {})
    st.setdefault("months", {})
    st.setdefault("subs", [])
    return st


def save(st):
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(STATE)


def settings(st=None):
    s = (st or load())["settings"]
    return {
        "to": s.get("to") or os.environ.get("MAIL_TO", ""),
        "cc": s.get("cc") or os.environ.get("MAIL_CC", ""),
        "name": s.get("name") or os.environ.get("SENDER_NAME", ""),
    }


# ---------------------------------------------------------------- 出張の日程
def trip_days(ym):
    """その月の第 1 土曜と、続く日曜・月曜。"""
    y, m = map(int, ym.split("-"))
    first = date(y, m, 1)
    sat = first + timedelta(days=(5 - first.weekday()) % 7)
    return [sat, sat + timedelta(days=1), sat + timedelta(days=2)]


def fmt_day(d):
    return f"{d.month}/{d.day}({WEEK[d.weekday()]})"


def check_ym(ym):
    try:
        y, m = map(int, ym.split("-"))
        date(y, m, 1)
    except Exception:
        raise HTTPException(400, "月の指定が不正です")
    return f"{y:04d}-{m:02d}"


def default_ym():
    """今月の出張が終わって 10 日以上たっていれば来月を開く。"""
    t = now().date()
    ym = f"{t.year:04d}-{t.month:02d}"
    if t > trip_days(ym)[2] + timedelta(days=10) and month_of(load(), ym)["sent"]:
        y, m = (t.year + 1, 1) if t.month == 12 else (t.year, t.month + 1)
        ym = f"{y:04d}-{m:02d}"
    return ym


def month_of(st, ym):
    return st["months"].setdefault(ym, {"items": [], "sent": []})


# ---------------------------------------------------------------- ログイン
def _secret():
    s = os.environ.get("SESSION_SECRET")
    if s:
        return s.encode()
    p = DATA / "session_secret"
    if not p.exists():
        DATA.mkdir(parents=True, exist_ok=True)
        p.write_text(secrets.token_hex(32))
    return p.read_text().strip().encode()


def _token():
    return hmac.new(_secret(), b"receipt-owner:" + PASSWORD.encode(), hashlib.sha256).hexdigest()


def auth(request: Request):
    if not PASSWORD:
        return  # APP_PASSWORD を設定しなければログイン無しで使える（ローカル用）
    if not hmac.compare_digest(request.cookies.get("rc_auth", ""), _token()):
        raise HTTPException(401, "ログインしてください")


app = FastAPI()


# ---------------------------------------------------------------- メール
def mail_mode():
    if os.environ.get("RESEND_API_KEY"):
        return "resend"
    if os.environ.get("SMTP_USER") and os.environ.get("SMTP_PASS"):
        return "smtp"
    return "outbox"  # 送信設定が無いときは data/outbox に .eml を保存するだけ（動作確認用）


def mail_from():
    addr = os.environ.get("MAIL_FROM") or os.environ.get("SMTP_USER", "receipt@localhost")
    name = settings()["name"]
    return formataddr((name, addr)) if name else addr


def send_mail(to, cc, subject, body, attachments):
    """attachments: [(filename, bytes)]。送れたら説明の文字列を返す。"""
    mode = mail_mode()
    to_list = [a.strip() for a in to.split(",") if a.strip()]
    cc_list = [a.strip() for a in cc.split(",") if a.strip()]
    if mode == "resend":
        r = requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {os.environ['RESEND_API_KEY']}"},
            json={
                "from": mail_from(), "to": to_list, "cc": cc_list or None, "subject": subject, "text": body,
                "attachments": [{"filename": n, "content": base64.b64encode(b).decode()} for n, b in attachments],
            },
            timeout=60,
        )
        if r.status_code >= 300:
            raise RuntimeError(f"Resend のエラー: {r.status_code} {r.text[:300]}")
        return "送信しました"

    msg = EmailMessage()
    msg["From"] = mail_from()
    msg["To"] = ", ".join(to_list)
    if cc_list:
        msg["Cc"] = ", ".join(cc_list)
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid()
    msg.set_content(body)
    for n, b in attachments:
        msg.add_attachment(b, maintype="image", subtype="jpeg", filename=n)

    if mode == "outbox":
        out = DATA / "outbox"
        out.mkdir(parents=True, exist_ok=True)
        p = out / f"{now():%Y%m%d-%H%M%S}.eml"
        p.write_bytes(bytes(msg))
        return f"送信設定が無いので {p.name} に保存しました（テスト）"

    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "465"))
    ctx = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=ctx, timeout=60) as s:
            s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=60) as s:
            s.starttls(context=ctx)
            s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
            s.send_message(msg)
    return "送信しました"


def compose(ym, mo, name):
    days = trip_days(ym)
    y, m = map(int, ym.split("-"))
    subject = f"【領収書】{y}年{m}月 出張分（{fmt_day(days[0])}〜{fmt_day(days[2])}）"
    lines = ["お疲れさまです。" + (f"{name}です。" if name else ""), "",
             f"{y}年{m}月の出張分（{fmt_day(days[0])}〜{fmt_day(days[2])}）の領収書を送付します。", ""]
    files, total, n = [], 0, 0
    for kind, label in KINDS.items():
        items = [it for it in mo["items"] if it["kind"] == kind]
        if not items:
            continue
        lines.append(f"■ {label}　{len(items)} 件")
        for i, it in enumerate(sorted(items, key=lambda x: (x.get("day") or "9999", x["created"])), 1):
            n += 1
            d = date.fromisoformat(it["day"]) if it.get("day") else None
            parts = [f"  {i}. {fmt_day(d) if d else '日付なし'}"]
            if it.get("amount"):
                parts.append(f"{it['amount']:,}円")
                total += it["amount"]
            if it.get("note"):
                parts.append(it["note"])
            fname = f"{n:02d}_{label.split('（')[0]}_{d:%m%d}.jpg" if d else f"{n:02d}_{label.split('（')[0]}.jpg"
            parts.append(f"→ {fname}")
            lines.append("　".join(parts))
            files.append((fname, (IMG / it["file"]).read_bytes()))
        lines.append("")
    if total:
        lines += [f"合計（入力した金額）：{total:,}円", ""]
    lines += [f"添付：画像 {len(files)} 枚", "", "よろしくお願いいたします。"]
    return subject, "\n".join(lines), files


# ---------------------------------------------------------------- プッシュ通知
def _vapid():
    from cryptography.hazmat.primitives import serialization
    from py_vapid import Vapid
    pem = DATA / "vapid_private.pem"
    if not pem.exists():
        DATA.mkdir(parents=True, exist_ok=True)
        v = Vapid()
        v.generate_keys()
        v.save_key(str(pem))
    v = Vapid.from_file(str(pem))
    raw = v.public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return str(pem), base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def notify(title, body):
    from pywebpush import WebPushException, webpush
    pem, _ = _vapid()
    domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "localhost")
    with _lock:
        subs = load()["subs"]
    alive = []
    for sub in subs:
        try:
            webpush(sub, json.dumps({"title": title, "body": body}), vapid_private_key=pem,
                    vapid_claims={"sub": f"https://{domain}"})
            alive.append(sub)
        except WebPushException as e:
            if e.response is None or e.response.status_code not in (404, 410):
                alive.append(sub)  # 期限切れ（404/410）だけ消す
    if len(alive) != len(subs):
        with _lock:
            st = load()
            st["subs"] = alive
            save(st)


def job_remind():
    """毎日 8 時と 19 時に呼ばれ、出張初日の朝と、月曜夜〜1 週間（未送信の間）に通知する。"""
    t = now()
    ym = f"{t.year:04d}-{t.month:02d}"
    days = trip_days(ym)
    st = load()
    mo = month_of(st, ym)
    try:
        if t.date() == days[0] and t.hour < 12:
            notify("今日から出張です", "タクシー・新幹線・マリンライナーの領収書は受け取ったらすぐ撮っておきましょう 📸")
        elif days[2] <= t.date() <= days[2] + timedelta(days=7) and t.hour >= 12 and not mo["sent"]:
            n = len(mo["items"])
            missing = [KINDS[k].split("（")[0] for k in KINDS if not any(i["kind"] == k for i in mo["items"])]
            body = f"{n} 枚撮影済み・まだ送っていません。"
            if missing:
                body += f" 未撮影：{'・'.join(missing)}"
            notify("領収書を送りましょう", body)
    except Exception as e:  # 通知の失敗でスケジューラを止めない
        print("notify failed:", e)


# ---------------------------------------------------------------- API
class Login(BaseModel):
    password: str


class Settings(BaseModel):
    to: str = ""
    cc: str = ""
    name: str = ""


class ItemPatch(BaseModel):
    kind: str | None = None
    day: str | None = None
    amount: int | None = None
    note: str | None = None


def _view_month(ym, st):
    mo = month_of(st, ym)
    return {
        "ym": ym,
        "days": [{"iso": d.isoformat(), "label": fmt_day(d)} for d in trip_days(ym)],
        "items": sorted(mo["items"], key=lambda x: x["created"]),
        "sent": mo["sent"],
    }


@app.post("/api/login")
def login(body: Login, response: Response):
    if not PASSWORD or not hmac.compare_digest(body.password, PASSWORD):
        raise HTTPException(401, "パスワードが違います")
    response.set_cookie("rc_auth", _token(), max_age=60 * 60 * 24 * 365, httponly=True,
                        secure=bool(os.environ.get("RAILWAY_PUBLIC_DOMAIN")), samesite="lax")
    return {"ok": True}


@app.get("/api/me", dependencies=[Depends(auth)])
def me():
    return {"default_ym": default_ym(), "settings": settings(), "mail_mode": mail_mode(),
            "kinds": KINDS}


@app.post("/api/settings", dependencies=[Depends(auth)])
def set_settings(body: Settings):
    with _lock:
        st = load()
        st["settings"] = {"to": body.to.strip(), "cc": body.cc.strip(), "name": body.name.strip()}
        save(st)
    return {"settings": settings()}


@app.get("/api/month/{ym}", dependencies=[Depends(auth)])
def get_month(ym: str):
    ym = check_ym(ym)
    return _view_month(ym, load())


@app.post("/api/month/{ym}/items", dependencies=[Depends(auth)])
async def add_item(ym: str, kind: str = Form(...), day: str = Form(""), photo: UploadFile = File(...)):
    ym = check_ym(ym)
    if kind not in KINDS:
        raise HTTPException(400, "種類が不正です")
    raw = await photo.read()
    if len(raw) > 25 * 1024 * 1024:
        raise HTTPException(413, "画像が大きすぎます")
    try:
        im = ImageOps.exif_transpose(Image.open(io.BytesIO(raw))).convert("RGB")
    except Exception:
        raise HTTPException(400, "画像として読めませんでした")
    im.thumbnail((2000, 2000))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=85, optimize=True)
    IMG.mkdir(parents=True, exist_ok=True)
    fid = uuid.uuid4().hex
    (IMG / f"{fid}.jpg").write_bytes(buf.getvalue())
    days = [d.isoformat() for d in trip_days(ym)]
    if day not in days:
        today = now().date().isoformat()
        day = today if today in days else ""
    item = {"id": fid, "kind": kind, "day": day or None, "amount": None, "note": "",
            "file": f"{fid}.jpg", "created": now().isoformat(timespec="seconds")}
    with _lock:
        st = load()
        month_of(st, ym)["items"].append(item)
        save(st)
        return _view_month(ym, st)


@app.patch("/api/month/{ym}/items/{iid}", dependencies=[Depends(auth)])
def patch_item(ym: str, iid: str, body: ItemPatch):
    ym = check_ym(ym)
    with _lock:
        st = load()
        it = next((x for x in month_of(st, ym)["items"] if x["id"] == iid), None)
        if not it:
            raise HTTPException(404, "見つかりません")
        data = body.model_dump(exclude_unset=True)
        if "kind" in data and data["kind"] not in KINDS:
            raise HTTPException(400, "種類が不正です")
        if "day" in data and data["day"] and data["day"] not in [d.isoformat() for d in trip_days(ym)]:
            raise HTTPException(400, "日付が不正です")
        if "amount" in data and data["amount"] is not None and not 0 <= data["amount"] <= 10_000_000:
            raise HTTPException(400, "金額が不正です")
        it.update(data)
        save(st)
        return _view_month(ym, st)


@app.delete("/api/month/{ym}/items/{iid}", dependencies=[Depends(auth)])
def delete_item(ym: str, iid: str):
    ym = check_ym(ym)
    with _lock:
        st = load()
        mo = month_of(st, ym)
        it = next((x for x in mo["items"] if x["id"] == iid), None)
        if not it:
            raise HTTPException(404, "見つかりません")
        mo["items"].remove(it)
        save(st)
        (IMG / it["file"]).unlink(missing_ok=True)
        return _view_month(ym, st)


@app.get("/api/month/{ym}/preview", dependencies=[Depends(auth)])
def preview(ym: str):
    ym = check_ym(ym)
    st = load()
    s = settings(st)
    subject, body, files = compose(ym, month_of(st, ym), s["name"])
    return {"to": s["to"], "cc": s["cc"], "subject": subject, "body": body,
            "files": [n for n, _ in files], "bytes": sum(len(b) for _, b in files), "mail_mode": mail_mode()}


@app.post("/api/month/{ym}/send", dependencies=[Depends(auth)])
def send(ym: str):
    ym = check_ym(ym)
    st = load()
    s = settings(st)
    if not s["to"]:
        raise HTTPException(400, "宛先メールアドレスを設定してください（⚙ 設定）")
    mo = month_of(st, ym)
    if not mo["items"]:
        raise HTTPException(400, "まだ領収書がありません")
    subject, body, files = compose(ym, mo, s["name"])
    if sum(len(b) for _, b in files) > MAX_MAIL_BYTES:
        raise HTTPException(413, "添付が大きすぎます（20MB 超）。枚数を減らしてください")
    try:
        result = send_mail(s["to"], s["cc"], subject, body, files)
    except Exception as e:
        raise HTTPException(502, f"送信に失敗しました：{e}")
    with _lock:
        st = load()
        month_of(st, ym)["sent"].append({"at": now().isoformat(timespec="seconds"), "to": s["to"],
                                         "count": len(files), "mode": mail_mode()})
        save(st)
        return {"result": result, "month": _view_month(ym, st)}


@app.get("/img/{name}", dependencies=[Depends(auth)])
def img(name: str):
    p = IMG / Path(name).name
    if not p.exists():
        raise HTTPException(404)
    return FileResponse(p, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=31536000"})


@app.get("/api/push/key", dependencies=[Depends(auth)])
def push_key():
    return {"key": _vapid()[1]}


@app.post("/api/push/subscribe", dependencies=[Depends(auth)])
async def push_subscribe(request: Request):
    sub = await request.json()
    with _lock:
        st = load()
        st["subs"] = [x for x in st["subs"] if x.get("endpoint") != sub.get("endpoint")] + [sub]
        save(st)
    return {"ok": True}


@app.post("/api/push/test", dependencies=[Depends(auth)])
def push_test():
    notify("テスト通知", "通知は届いています ✅")
    return {"ok": True}


@app.api_route("/", methods=["GET", "HEAD"])
def index():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/sw.js")
def sw():
    return FileResponse(STATIC / "sw.js", media_type="text/javascript", headers={"Cache-Control": "no-cache"})


app.mount("/static", StaticFiles(directory=STATIC), name="static")


def start_scheduler():
    sch = BackgroundScheduler(timezone=JST)
    sch.add_job(job_remind, "cron", hour="8,19", minute=0, misfire_grace_time=3600)
    sch.start()


if __name__ == "__main__":
    start_scheduler()
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
