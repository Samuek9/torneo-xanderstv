"""
Torneo XandersTV — Sistema de venta de balotas
Flask + PostgreSQL (Neon) + Wompi payment links
"""
import hashlib
import hmac
import io
import json
import os
import random
import smtplib
import uuid
from datetime import datetime, timedelta, timezone
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import psycopg2
import psycopg2.extras
import qrcode
import requests
from dotenv import load_dotenv
from flask import Flask, abort, g, jsonify, make_response, redirect, render_template, request, url_for
from PIL import Image, ImageDraw, ImageFont

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "dev-secret-xanders-2025")

# ─── Config ──────────────────────────────────────────────────────────────────
DATABASE_URL = os.getenv("DATABASE_URL", "")

WOMPI_PUBLIC_KEY = os.getenv("WOMPI_PUBLIC_KEY", "")
WOMPI_PRIVATE_KEY = os.getenv("WOMPI_PRIVATE_KEY", "")
WOMPI_EVENTS_KEY = os.getenv("WOMPI_EVENTS_KEY", "")
WOMPI_INTEGRITY_KEY = os.getenv("WOMPI_INTEGRITY_KEY", "")
WOMPI_SANDBOX = os.getenv("WOMPI_SANDBOX", "true").lower() == "true"
WOMPI_BASE = "https://sandbox.wompi.co/v1" if WOMPI_SANDBOX else "https://production.wompi.co/v1"

TICKET_PRICE_COP = int(os.getenv("TICKET_PRICE_COP", "21500"))
MAX_TICKETS_TOTAL = int(os.getenv("MAX_TICKETS_TOTAL", "10000"))
MAX_TICKETS_PER_ORDER = int(os.getenv("MAX_TICKETS_PER_ORDER", "50"))
RESERVATION_MINUTES = 15
APP_URL = os.getenv("APP_URL", "http://localhost:5000").rstrip("/")

EMAIL_HOST = os.getenv("EMAIL_HOST", "smtp.resend.com")
EMAIL_PORT = int(os.getenv("EMAIL_PORT", "587"))
EMAIL_USER = os.getenv("EMAIL_HOST_USER", "resend")
EMAIL_PASS = os.getenv("EMAIL_HOST_PASSWORD", "")
EMAIL_FROM = os.getenv("DEFAULT_FROM_EMAIL", "torneos@xanderstv.com")

TORNEO_NAME = os.getenv("TORNEO_NAME", "Gran Torneo XandersTV 2025")
TORNEO_DESCRIPTION = os.getenv("TORNEO_DESCRIPTION", "Acceso completo + participación en el sorteo")


# ─── Database (PostgreSQL via Neon) ──────────────────────────────────────────
def get_db():
    if "db" not in g:
        conn = psycopg2.connect(DATABASE_URL)
        conn.autocommit = False
        g.db = conn
    return g.db


@app.teardown_appcontext
def close_db(e=None):
    db = g.pop("db", None)
    if db is not None:
        try:
            db.close()
        except Exception:
            pass


def _cur(conn):
    return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)


def db_exec(sql, params=None):
    conn = get_db()
    with _cur(conn) as cur:
        cur.execute(sql, params)
    conn.commit()


def db_one(sql, params=None):
    conn = get_db()
    with _cur(conn) as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def db_all(sql, params=None):
    conn = get_db()
    with _cur(conn) as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def db_insert(sql, params=None):
    """Execute an INSERT ... RETURNING id and return the new id."""
    conn = get_db()
    with _cur(conn) as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
        conn.commit()
        return row["id"] if row else None


def db_scalar(sql, params=None):
    conn = get_db()
    with _cur(conn) as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
        if row is None:
            return None
        return list(row.values())[0]


def init_db():
    with app.app_context():
        conn = psycopg2.connect(DATABASE_URL)
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS buyers (
                id SERIAL PRIMARY KEY,
                full_name TEXT NOT NULL,
                doc_type TEXT NOT NULL,
                doc_number TEXT NOT NULL,
                phone TEXT NOT NULL,
                email TEXT NOT NULL,
                city TEXT NOT NULL,
                accepted_terms INTEGER DEFAULT 0,
                access_token TEXT UNIQUE NOT NULL,
                created_at TIMESTAMPTZ DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS orders (
                id SERIAL PRIMARY KEY,
                buyer_id INTEGER NOT NULL REFERENCES buyers(id),
                quantity INTEGER NOT NULL,
                unit_price INTEGER NOT NULL,
                total_amount INTEGER NOT NULL,
                wompi_payment_link_id TEXT DEFAULT '',
                wompi_payment_link_url TEXT DEFAULT '',
                wompi_transaction_id TEXT DEFAULT '',
                status TEXT DEFAULT 'PENDING',
                reservation_expires_at TIMESTAMPTZ NOT NULL,
                created_at TIMESTAMPTZ DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS balotas (
                id SERIAL PRIMARY KEY,
                number INTEGER NOT NULL UNIQUE,
                order_id INTEGER REFERENCES orders(id),
                status TEXT DEFAULT 'AVAILABLE',
                reserved_until TIMESTAMPTZ,
                sold_at TIMESTAMPTZ
            );

            CREATE INDEX IF NOT EXISTS idx_balotas_status ON balotas(status);
            CREATE INDEX IF NOT EXISTS idx_balotas_order  ON balotas(order_id);
            CREATE INDEX IF NOT EXISTS idx_orders_status  ON orders(status);
            CREATE INDEX IF NOT EXISTS idx_buyers_email   ON buyers(email);
            CREATE INDEX IF NOT EXISTS idx_buyers_token   ON buyers(access_token);
        """)

        # Seed balotas only if empty
        cur.execute("SELECT COUNT(*) FROM balotas")
        count = cur.fetchone()[0]
        if count == 0:
            cur.execute("""
                INSERT INTO balotas (number)
                SELECT generate_series(0, %s - 1)
                ON CONFLICT (number) DO NOTHING
            """, (MAX_TICKETS_TOTAL,))

        cur.close()
        conn.close()


# ─── Wompi ────────────────────────────────────────────────────────────────────
def wompi_create_payment_link(amount_cop: int, reference: str, description: str) -> dict:
    url = f"{WOMPI_BASE}/payment_links"
    headers = {
        "Authorization": f"Bearer {WOMPI_PRIVATE_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "name": TORNEO_NAME[:255],
        "description": description[:255],
        "single_use": True,
        "collect_shipping": False,
        "currency": "COP",
        "amount_in_cents": amount_cop * 100,
        "redirect_url": f"{APP_URL}/pago/resultado",
        "reference": reference,
    }
    resp = requests.post(url, json=payload, headers=headers, timeout=10)
    resp.raise_for_status()
    return resp.json().get("data", {})


def wompi_validate_webhook(payload_bytes: bytes, signature: str) -> bool:
    if not WOMPI_EVENTS_KEY:
        return True
    expected = hmac.new(WOMPI_EVENTS_KEY.encode(), payload_bytes, hashlib.sha256).hexdigest()
    provided = signature.replace("sha256=", "")
    return hmac.compare_digest(expected, provided)


def wompi_generate_integrity_hash(reference: str, amount_cents: int, currency: str = "COP") -> str:
    raw = f"{reference}{amount_cents}{currency}{WOMPI_INTEGRITY_KEY}"
    return hashlib.sha256(raw.encode()).hexdigest()


# ─── QR + Pase Digital ────────────────────────────────────────────────────────
def generate_qr_bytes(data: str) -> bytes:
    qr = qrcode.QRCode(version=1, box_size=6, border=2)
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="#1a1a2e", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def generate_pass_image(buyer_name: str, ticket_numbers: list, access_token: str) -> bytes:
    W, H = 800, 450
    img = Image.new("RGB", (W, H), color="#0f0f1a")
    draw = ImageDraw.Draw(img)

    for y in range(H):
        ratio = y / H
        r = int(15 + ratio * 20)
        gv = int(15 + ratio * 10)
        b = int(26 + ratio * 30)
        draw.line([(0, y), (W, y)], fill=(r, gv, b))

    draw.rectangle([(0, 0), (W, 6)], fill="#e63946")
    draw.rectangle([(0, H - 6), (W, H)], fill="#e63946")

    try:
        font_title = ImageFont.truetype("arial.ttf", 26)
        font_name = ImageFont.truetype("arial.ttf", 32)
        font_small = ImageFont.truetype("arial.ttf", 18)
        font_tiny = ImageFont.truetype("arial.ttf", 14)
    except Exception:
        font_title = ImageFont.load_default()
        font_name = font_title
        font_small = font_title
        font_tiny = font_title

    draw.text((40, 30), "XANDERS TV", fill="#e63946", font=font_title)
    draw.text((40, 65), TORNEO_NAME, fill="#cccccc", font=font_small)
    draw.rectangle([(40, 100), (760, 101)], fill="#333355")
    draw.text((40, 115), "PASE DE ACCESO DIGITAL", fill="#aaaaaa", font=font_tiny)
    draw.text((40, 140), buyer_name.upper(), fill="#ffffff", font=font_name)

    nums_str = "  ".join(f"{n:04d}" for n in sorted(ticket_numbers[:12]))
    if len(ticket_numbers) > 12:
        nums_str += f"  +{len(ticket_numbers) - 12} más"
    draw.text((40, 200), "BALOTAS:", fill="#aaaaaa", font=font_tiny)
    draw.text((40, 220), nums_str, fill="#f1c40f", font=font_small)
    draw.text((40, 280), f"Total balotas: {len(ticket_numbers)}", fill="#cccccc", font=font_small)
    draw.text((40, 315), "Válido para todos los partidos del torneo", fill="#888888", font=font_tiny)

    qr_url = f"{APP_URL}/mi-cuenta/{access_token}"
    qr_bytes = generate_qr_bytes(qr_url)
    qr_img = Image.open(io.BytesIO(qr_bytes)).resize((160, 160))
    img.paste(qr_img, (600, 140))
    draw.text((600, 305), "Mi Cuenta", fill="#888888", font=font_tiny)
    draw.text((40, 420), f"ID: {access_token[:16].upper()}", fill="#444466", font=font_tiny)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ─── Email ────────────────────────────────────────────────────────────────────
def send_confirmation_email(buyer: dict, ticket_numbers: list, pass_image_bytes: bytes):
    if not EMAIL_PASS:
        app.logger.warning("Email not configured, skipping send.")
        return

    msg = MIMEMultipart("related")
    msg["Subject"] = f"¡Confirmación de compra! {TORNEO_NAME}"
    msg["From"] = EMAIL_FROM
    msg["To"] = buyer["email"]

    nums_html = "".join(
        f'<span style="background:#f1c40f;color:#000;padding:4px 8px;margin:2px;border-radius:4px;'
        f'font-weight:bold;display:inline-block">{n:04d}</span>'
        for n in sorted(ticket_numbers)
    )

    html = f"""
    <div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;background:#0f0f1a;color:#fff;padding:30px;border-radius:10px">
      <div style="border-top:4px solid #e63946;padding-top:20px">
        <h1 style="color:#e63946;margin:0">XANDERS TV</h1>
        <h2 style="color:#ccc;margin:5px 0 20px">{TORNEO_NAME}</h2>
      </div>
      <p>Hola <strong>{buyer['full_name']}</strong>,</p>
      <p>¡Tu compra fue confirmada! Aquí están tus números de balota:</p>
      <div style="margin:20px 0">{nums_html}</div>
      <p style="color:#aaa">Total de balotas: <strong style="color:#fff">{len(ticket_numbers)}</strong></p>
      <p>Tu <strong>Pase de Acceso Digital</strong> está adjunto a este correo. También puedes consultarlo en:</p>
      <a href="{APP_URL}/mi-cuenta/{buyer['access_token']}"
         style="display:inline-block;background:#e63946;color:#fff;padding:12px 24px;border-radius:6px;text-decoration:none;font-weight:bold;margin:10px 0">
        Ver mi Pase Digital
      </a>
      <hr style="border-color:#333;margin:25px 0">
      <p style="color:#666;font-size:12px">Correo generado automáticamente. No respondas a este mensaje.</p>
    </div>
    """

    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(html, "html"))
    msg.attach(alt)

    pass_img = MIMEImage(pass_image_bytes, name="pase_acceso.png")
    pass_img.add_header("Content-Disposition", "attachment", filename="pase_acceso.png")
    msg.attach(pass_img)

    try:
        if EMAIL_PORT == 465:
            ctx = __import__("ssl").create_default_context()
            with smtplib.SMTP_SSL(EMAIL_HOST, EMAIL_PORT, context=ctx) as smtp:
                smtp.login(EMAIL_USER, EMAIL_PASS)
                smtp.sendmail(EMAIL_FROM, [buyer["email"]], msg.as_string())
        else:
            with smtplib.SMTP(EMAIL_HOST, EMAIL_PORT) as smtp:
                smtp.starttls()
                smtp.login(EMAIL_USER, EMAIL_PASS)
                smtp.sendmail(EMAIL_FROM, [buyer["email"]], msg.as_string())
    except Exception as e:
        app.logger.error(f"Email send failed: {e}")


# ─── Helpers ──────────────────────────────────────────────────────────────────
def now_utc():
    return datetime.now(timezone.utc)


def expire_old_reservations():
    db_exec("""
        UPDATE balotas
        SET status='AVAILABLE', reserved_until=NULL, order_id=NULL
        WHERE status='RESERVED' AND reserved_until < NOW()
    """)
    db_exec("""
        UPDATE orders SET status='EXPIRED'
        WHERE status='PENDING' AND reservation_expires_at < NOW()
    """)


def reserve_random_balotas(quantity: int, order_id: int) -> list:
    expire_old_reservations()
    rows = db_all(
        "SELECT id, number FROM balotas WHERE status='AVAILABLE' ORDER BY RANDOM() LIMIT %s",
        (quantity,),
    )
    if len(rows) < quantity:
        return []
    expires_at = now_utc() + timedelta(minutes=RESERVATION_MINUTES)
    ids = [r["id"] for r in rows]
    conn = get_db()
    with _cur(conn) as cur:
        cur.execute(
            "UPDATE balotas SET status='RESERVED', reserved_until=%s, order_id=%s WHERE id = ANY(%s)",
            (expires_at, order_id, ids),
        )
    conn.commit()
    return [r["number"] for r in rows]


# ─── Routes ───────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    sold = db_scalar("SELECT COUNT(*) FROM balotas WHERE status='SOLD'") or 0
    available = MAX_TICKETS_TOTAL - sold
    return render_template("index.html",
        torneo_name=TORNEO_NAME,
        torneo_description=TORNEO_DESCRIPTION,
        ticket_price=TICKET_PRICE_COP,
        available=available,
        total=MAX_TICKETS_TOTAL,
        max_per_order=MAX_TICKETS_PER_ORDER,
    )


@app.route("/reservar", methods=["POST"])
def reservar():
    f = request.form
    required = ["full_name", "doc_type", "doc_number", "phone", "email", "city", "quantity"]
    for field in required:
        if not f.get(field, "").strip():
            return render_template("index.html",
                torneo_name=TORNEO_NAME, torneo_description=TORNEO_DESCRIPTION,
                ticket_price=TICKET_PRICE_COP, available=MAX_TICKETS_TOTAL,
                total=MAX_TICKETS_TOTAL, max_per_order=MAX_TICKETS_PER_ORDER,
                error=f"El campo '{field}' es obligatorio.",
            ), 400

    if not f.get("terms"):
        return render_template("index.html",
            torneo_name=TORNEO_NAME, torneo_description=TORNEO_DESCRIPTION,
            ticket_price=TICKET_PRICE_COP, available=MAX_TICKETS_TOTAL,
            total=MAX_TICKETS_TOTAL, max_per_order=MAX_TICKETS_PER_ORDER,
            error="Debes aceptar los términos y condiciones.",
        ), 400

    try:
        quantity = int(f.get("quantity", 1))
    except ValueError:
        abort(400)
    if quantity < 1 or quantity > MAX_TICKETS_PER_ORDER:
        abort(400)

    access_token = str(uuid.uuid4())
    expires_at = now_utc() + timedelta(minutes=RESERVATION_MINUTES)
    total_amount = quantity * TICKET_PRICE_COP

    buyer_id = db_insert("""
        INSERT INTO buyers (full_name, doc_type, doc_number, phone, email, city, accepted_terms, access_token)
        VALUES (%s, %s, %s, %s, %s, %s, 1, %s)
        RETURNING id
    """, (
        f["full_name"].strip(), f["doc_type"], f["doc_number"].strip(),
        f["phone"].strip(), f["email"].strip().lower(), f["city"].strip(),
        access_token,
    ))

    order_id = db_insert("""
        INSERT INTO orders (buyer_id, quantity, unit_price, total_amount, reservation_expires_at)
        VALUES (%s, %s, %s, %s, %s)
        RETURNING id
    """, (buyer_id, quantity, TICKET_PRICE_COP, total_amount, expires_at))

    numbers = reserve_random_balotas(quantity, order_id)
    if not numbers:
        db_exec("DELETE FROM orders WHERE id=%s", (order_id,))
        db_exec("DELETE FROM buyers WHERE id=%s", (buyer_id,))
        return render_template("index.html",
            torneo_name=TORNEO_NAME, torneo_description=TORNEO_DESCRIPTION,
            ticket_price=TICKET_PRICE_COP, available=0,
            total=MAX_TICKETS_TOTAL, max_per_order=MAX_TICKETS_PER_ORDER,
            error="No hay suficientes balotas disponibles. Intenta con una cantidad menor.",
        ), 409

    payment_url = ""
    reference = f"TORNEO-{order_id}"
    if WOMPI_PRIVATE_KEY:
        try:
            nums_preview = ", ".join(f"{n:04d}" for n in sorted(numbers[:5]))
            suffix = f" +{len(numbers)-5} más" if len(numbers) > 5 else ""
            link_data = wompi_create_payment_link(
                amount_cop=total_amount,
                reference=reference,
                description=f"{quantity} balota(s): {nums_preview}{suffix}",
            )
            payment_url = link_data.get("permalink") or link_data.get("url", "")
            link_id = str(link_data.get("id", ""))
            db_exec(
                "UPDATE orders SET wompi_payment_link_id=%s, wompi_payment_link_url=%s WHERE id=%s",
                (link_id, payment_url, order_id),
            )
        except Exception as e:
            app.logger.error(f"Wompi payment link error: {e}")

    return render_template("checkout.html",
        torneo_name=TORNEO_NAME,
        buyer_name=f["full_name"].strip(),
        buyer_email=f["email"].strip(),
        quantity=quantity,
        unit_price=TICKET_PRICE_COP,
        total_amount=total_amount,
        numbers=sorted(numbers),
        payment_url=payment_url,
        order_id=order_id,
        reference=reference,
        access_token=access_token,
        expires_minutes=RESERVATION_MINUTES,
        wompi_public_key=WOMPI_PUBLIC_KEY,
        integrity_hash=wompi_generate_integrity_hash(reference, total_amount * 100),
        amount_cents=total_amount * 100,
    )


@app.route("/pago/resultado")
def pago_resultado():
    return render_template("resultado.html",
        torneo_name=TORNEO_NAME,
        transaction_id=request.args.get("id", ""),
        order_ref=request.args.get("reference", ""),
        status=request.args.get("status", ""),
    )


@app.route("/wompi/webhook", methods=["POST"])
def wompi_webhook():
    signature = request.headers.get("X-Wompi-Signature", "")
    if not wompi_validate_webhook(request.data, signature):
        return "", 400

    event = request.json or {}
    if event.get("event") != "transaction.updated":
        return "", 200

    tx_data = event.get("data", {}).get("transaction", {})
    wompi_tx_id = tx_data.get("id", "")
    reference = tx_data.get("reference", "")
    wompi_status = tx_data.get("status", "")

    if not reference.startswith("TORNEO-"):
        return "", 200

    try:
        order_id = int(reference.split("-")[1])
    except (IndexError, ValueError):
        return "", 200

    order = db_one("SELECT * FROM orders WHERE id=%s", (order_id,))
    if not order:
        return "", 200

    if wompi_status == "APPROVED" and order["status"] == "PENDING":
        db_exec(
            "UPDATE orders SET status='PAID', wompi_transaction_id=%s WHERE id=%s",
            (wompi_tx_id, order_id),
        )
        db_exec(
            "UPDATE balotas SET status='SOLD', sold_at=NOW() WHERE order_id=%s AND status='RESERVED'",
            (order_id,),
        )
        buyer = db_one("SELECT * FROM buyers WHERE id=%s", (order["buyer_id"],))
        ticket_numbers = [
            r["number"] for r in
            db_all("SELECT number FROM balotas WHERE order_id=%s AND status='SOLD'", (order_id,))
        ]
        if buyer and ticket_numbers:
            buyer_dict = dict(buyer)
            pass_img = generate_pass_image(buyer_dict["full_name"], ticket_numbers, buyer_dict["access_token"])
            send_confirmation_email(buyer_dict, ticket_numbers, pass_img)

    elif wompi_status in ("DECLINED", "VOIDED", "ERROR"):
        db_exec("UPDATE orders SET status='FAILED' WHERE id=%s AND status='PENDING'", (order_id,))
        db_exec(
            "UPDATE balotas SET status='AVAILABLE', reserved_until=NULL, order_id=NULL WHERE order_id=%s AND status='RESERVED'",
            (order_id,),
        )

    return "", 200


@app.route("/mi-cuenta/<token>")
def mi_cuenta(token):
    buyer = db_one("SELECT * FROM buyers WHERE access_token=%s", (token,))
    if not buyer:
        abort(404)

    orders = db_all(
        "SELECT * FROM orders WHERE buyer_id=%s ORDER BY created_at DESC",
        (buyer["id"],),
    )
    orders_with_tickets = []
    for order in orders:
        tickets = db_all(
            "SELECT number FROM balotas WHERE order_id=%s ORDER BY number",
            (order["id"],),
        )
        orders_with_tickets.append({
            "order": dict(order),
            "tickets": [r["number"] for r in tickets],
        })

    return render_template("cuenta.html",
        torneo_name=TORNEO_NAME,
        buyer=dict(buyer),
        orders=orders_with_tickets,
        app_url=APP_URL,
    )


@app.route("/pase/<token>.png")
def pase_image(token):
    buyer = db_one("SELECT * FROM buyers WHERE access_token=%s", (token,))
    if not buyer:
        abort(404)

    ticket_numbers = [
        r["number"] for r in db_all("""
            SELECT b.number FROM balotas b
            JOIN orders o ON b.order_id = o.id
            WHERE o.buyer_id=%s AND b.status='SOLD' ORDER BY b.number
        """, (buyer["id"],))
    ]

    img_bytes = generate_pass_image(buyer["full_name"], ticket_numbers, token)
    resp = make_response(img_bytes)
    resp.headers["Content-Type"] = "image/png"
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/admin/stats")
def admin_stats():
    if request.args.get("secret", "") != os.getenv("ADMIN_SECRET", ""):
        abort(403)
    return jsonify({
        "sold":          db_scalar("SELECT COUNT(*) FROM balotas WHERE status='SOLD'") or 0,
        "reserved":      db_scalar("SELECT COUNT(*) FROM balotas WHERE status='RESERVED'") or 0,
        "available":     db_scalar("SELECT COUNT(*) FROM balotas WHERE status='AVAILABLE'") or 0,
        "orders_paid":   db_scalar("SELECT COUNT(*) FROM orders WHERE status='PAID'") or 0,
        "orders_pending":db_scalar("SELECT COUNT(*) FROM orders WHERE status='PENDING'") or 0,
        "revenue_cop":   db_scalar("SELECT COALESCE(SUM(total_amount),0) FROM orders WHERE status='PAID'") or 0,
        "buyers":        db_scalar("SELECT COUNT(*) FROM buyers") or 0,
    })


if __name__ == "__main__":
    init_db()
    app.run(debug=True, host="0.0.0.0", port=5000)
