"""
Torneo de las Luces 2026 — Códigos de Acceso Streaming
Flask + PostgreSQL (Neon) + Wompi
"""
import hashlib
import hmac
import io
import os
import uuid
from datetime import datetime, timedelta, timezone

import base64

import psycopg2
import psycopg2.extras
import qrcode
import requests
from dotenv import load_dotenv
from flask import Flask, abort, g, jsonify, make_response, render_template, request
from PIL import Image, ImageDraw, ImageFont

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "dev-secret-xanders-2025")

# ─── Config ──────────────────────────────────────────────────────────────────
DATABASE_URL        = os.getenv("DATABASE_URL", "")
WOMPI_SANDBOX       = os.getenv("WOMPI_SANDBOX", "false").lower() == "true"
WOMPI_BASE          = "https://sandbox.wompi.co/v1" if WOMPI_SANDBOX else "https://production.wompi.co/v1"
WOMPI_PUBLIC_KEY    = os.getenv("WOMPI_PUBLIC_KEY", "")
WOMPI_PRIVATE_KEY   = os.getenv("WOMPI_PRIVATE_KEY", "")
WOMPI_EVENTS_KEY    = os.getenv("WOMPI_EVENTS_KEY", "")
WOMPI_INTEGRITY_KEY = os.getenv("WOMPI_INTEGRITY_KEY", "")

CODE_PRICE_COP      = int(os.getenv("TICKET_PRICE_COP", "50000"))
MAX_CODES_PER_COLOR = 10000          # 0000–9999 por color
MIN_PACKS           = 1              # mínimo 1 paquete = 10 códigos
MAX_PACKS           = int(os.getenv("MAX_TICKETS_PER_ORDER", "20"))
RESERVATION_MINUTES = 60
APP_URL             = os.getenv("APP_URL", "http://localhost:5000").rstrip("/")

RESEND_API_KEY = os.getenv("EMAIL_HOST_PASSWORD", "")
EMAIL_FROM     = os.getenv("DEFAULT_FROM_EMAIL", "onboarding@resend.dev")

TORNEO_NAME  = os.getenv("TORNEO_NAME", "Torneo de las luces 2026")

# ─── 10 Colores (1 sorteo por color) ─────────────────────────────────────────
COLORS = [
    {"id": "blanco",   "name": "Blanco",       "hex": "#FFFFFF", "text": "#000000"},
    {"id": "verde",    "name": "Verde Lima",    "hex": "#AEEA00", "text": "#000000"},
    {"id": "amarillo", "name": "Amarillo",      "hex": "#FFD600", "text": "#000000"},
    {"id": "marino",   "name": "Azul Marino",   "hex": "#1A237E", "text": "#FFFFFF"},
    {"id": "rojo",     "name": "Rojo",          "hex": "#DD2C00", "text": "#FFFFFF"},
    {"id": "teal",     "name": "Verde Azulado", "hex": "#004D40", "text": "#FFFFFF"},
    {"id": "rosa",     "name": "Rosa",          "hex": "#E91E63", "text": "#FFFFFF"},
    {"id": "naranja",  "name": "Naranja",       "hex": "#E65100", "text": "#FFFFFF"},
    {"id": "celeste",  "name": "Azul Claro",    "hex": "#81D4FA", "text": "#000000"},
    {"id": "negro",    "name": "Negro",         "hex": "#212121", "text": "#FFFFFF"},
]
COLOR_MAP = {c["id"]: c for c in COLORS}


# ─── Database ────────────────────────────────────────────────────────────────
def get_db():
    if "db" not in g:
        conn = psycopg2.connect(DATABASE_URL)
        conn.autocommit = False
        g.db = conn
    return g.db


@app.teardown_appcontext
def close_db(e=None):
    db = g.pop("db", None)
    if db:
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
        return list(row.values())[0] if row else None


def init_db():
    with app.app_context():
        conn = psycopg2.connect(DATABASE_URL)
        conn.autocommit = True
        cur = conn.cursor()

        # ── Migrate: add color column if missing ──────────────────────────────
        cur.execute("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name='balotas' AND column_name='color'
        """)
        needs_migration = cur.fetchone() is None

        if needs_migration:
            cur.execute("DROP TABLE IF EXISTS balotas CASCADE")

        cur.execute("""
            CREATE TABLE IF NOT EXISTS buyers (
                id            SERIAL PRIMARY KEY,
                full_name     TEXT NOT NULL,
                doc_type      TEXT NOT NULL,
                doc_number    TEXT NOT NULL,
                phone         TEXT NOT NULL,
                email         TEXT NOT NULL,
                city          TEXT NOT NULL,
                accepted_terms INTEGER DEFAULT 0,
                access_token  TEXT UNIQUE NOT NULL,
                created_at    TIMESTAMPTZ DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS orders (
                id                      SERIAL PRIMARY KEY,
                buyer_id                INTEGER NOT NULL REFERENCES buyers(id),
                packs                   INTEGER NOT NULL DEFAULT 1,
                quantity                INTEGER NOT NULL,
                unit_price              INTEGER NOT NULL,
                total_amount            INTEGER NOT NULL,
                wompi_payment_link_id   TEXT DEFAULT '',
                wompi_payment_link_url  TEXT DEFAULT '',
                wompi_transaction_id    TEXT DEFAULT '',
                status                  TEXT DEFAULT 'PENDING',
                reservation_expires_at  TIMESTAMPTZ NOT NULL,
                created_at              TIMESTAMPTZ DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS balotas (
                id            SERIAL PRIMARY KEY,
                color         TEXT NOT NULL,
                number        INTEGER NOT NULL,
                order_id      INTEGER REFERENCES orders(id),
                status        TEXT DEFAULT 'AVAILABLE',
                reserved_until TIMESTAMPTZ,
                sold_at       TIMESTAMPTZ,
                UNIQUE (color, number)
            );

            CREATE INDEX IF NOT EXISTS idx_balotas_status ON balotas(status);
            CREATE INDEX IF NOT EXISTS idx_balotas_color  ON balotas(color);
            CREATE INDEX IF NOT EXISTS idx_balotas_order  ON balotas(order_id);
            CREATE INDEX IF NOT EXISTS idx_orders_status  ON orders(status);
            CREATE INDEX IF NOT EXISTS idx_buyers_email   ON buyers(email);
            CREATE INDEX IF NOT EXISTS idx_buyers_token   ON buyers(access_token);
        """)

        # ── Migrate: add packs column to orders if missing ───────────────────
        cur.execute("""
            ALTER TABLE orders ADD COLUMN IF NOT EXISTS packs INTEGER NOT NULL DEFAULT 1
        """)

        # ── Seed 10 colors × 10,000 = 100,000 códigos ────────────────────────
        cur.execute("SELECT COUNT(*) FROM balotas")
        count = cur.fetchone()[0]
        if count < 100000:
            for color in COLORS:
                cur.execute("""
                    INSERT INTO balotas (color, number)
                    SELECT %s, generate_series(0, %s)
                    ON CONFLICT (color, number) DO NOTHING
                """, (color["id"], MAX_CODES_PER_COLOR - 1))

        cur.close()
        conn.close()


# ─── Wompi ───────────────────────────────────────────────────────────────────
def wompi_create_payment_link(amount_cop: int, reference: str, description: str):
    url = f"{WOMPI_BASE}/payment_links"
    # Wompi payment_links uses the private key for server-to-server auth
    headers = {
        "Authorization": f"Bearer {WOMPI_PRIVATE_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "name": TORNEO_NAME[:100],
        "description": description[:255],
        "single_use": True,
        "collect_shipping": False,
        "currency": "COP",
        "amount_in_cents": amount_cop * 100,
        "redirect_url": f"{APP_URL}/pago/resultado",
        "reference": reference,
    }
    app.logger.info(f"Wompi POST {url} | key={WOMPI_PRIVATE_KEY[:12]}... | cents={amount_cop*100}")
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=15)
    except requests.exceptions.Timeout:
        raise Exception("Timeout conectando a Wompi (>15s)")
    except requests.exceptions.ConnectionError as e:
        raise Exception(f"No se pudo conectar a Wompi: {e}")
    if not resp.ok:
        msg = f"Wompi {resp.status_code}: {resp.text[:300]}"
        app.logger.error(msg)
        raise Exception(msg)
    data = resp.json().get("data", {})
    if not data:
        raise Exception(f"Wompi devolvió respuesta vacía: {resp.text[:200]}")
    return data


def wompi_validate_webhook(payload_bytes: bytes, signature: str) -> bool:
    if not WOMPI_EVENTS_KEY or not signature:
        return True
    expected = hmac.new(WOMPI_EVENTS_KEY.encode(), payload_bytes, hashlib.sha256).hexdigest()
    provided = signature.replace("sha256=", "")
    return hmac.compare_digest(expected, provided)


def wompi_integrity_hash(reference: str, amount_cents: int) -> str:
    raw = f"{reference}{amount_cents}COP{WOMPI_INTEGRITY_KEY}"
    return hashlib.sha256(raw.encode()).hexdigest()


def wompi_find_approved_transaction(reference: str) -> str:
    """Devuelve el transaction ID si la referencia tiene una transacción APPROVED."""
    url = f"{WOMPI_BASE}/transactions?reference={reference}"
    headers = {"Authorization": f"Bearer {WOMPI_PRIVATE_KEY}"}
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.ok:
            for tx in resp.json().get("data", []):
                if tx.get("status") == "APPROVED":
                    return str(tx.get("id", ""))
    except Exception as e:
        app.logger.warning(f"wompi_find_approved_transaction error: {e}")
    return ""


def wompi_get_transaction(tx_id: str) -> dict:
    """Obtiene una transacción por su ID."""
    url = f"{WOMPI_BASE}/transactions/{tx_id}"
    headers = {"Authorization": f"Bearer {WOMPI_PRIVATE_KEY}"}
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.ok:
            return resp.json().get("data", {})
    except Exception as e:
        app.logger.warning(f"wompi_get_transaction error: {e}")
    return {}


def confirm_order(order_id: int, tx_id: str):
    """Marca la orden como PAID y envía el correo. Idempotente."""
    order = db_one("SELECT * FROM orders WHERE id=%s AND status IN ('PENDING','EXPIRED')", (order_id,))
    if not order:
        app.logger.info(f"confirm_order: order {order_id} not found or already processed")
        return

    # Si expiró, los códigos fueron liberados — hay que reasignar
    if order["status"] == "EXPIRED":
        app.logger.info(f"confirm_order: order {order_id} was EXPIRED, re-reserving codes")
        new_codes = reserve_codes(order["packs"], order_id)
        if not new_codes:
            app.logger.error(f"confirm_order: no codes available to re-assign for order {order_id}")
            # Marcar pagado de todas formas; admin puede resolver manualmente
            db_exec("UPDATE orders SET status='PAID', wompi_transaction_id=%s WHERE id=%s", (tx_id, order_id))
            return

    db_exec("UPDATE orders SET status='PAID', wompi_transaction_id=%s WHERE id=%s", (tx_id, order_id))
    db_exec("UPDATE balotas SET status='SOLD', sold_at=NOW() WHERE order_id=%s AND status='RESERVED'", (order_id,))
    buyer = db_one("SELECT * FROM buyers WHERE id=%s", (order["buyer_id"],))
    codes = [dict(r) for r in db_all(
        "SELECT color, number FROM balotas WHERE order_id=%s AND status='SOLD'", (order_id,)
    )]
    if buyer and codes:
        pass_img = generate_pass_image(buyer["full_name"], codes, buyer["access_token"])
        send_confirmation_email(dict(buyer), codes, pass_img)
        app.logger.info(f"Order {order_id} confirmed, email sent to {buyer['email']}")
    else:
        app.logger.error(f"confirm_order: buyer or codes missing for order {order_id}")


# ─── QR + Pase Digital ───────────────────────────────────────────────────────
_FONT_DIR = os.path.join(os.path.dirname(__file__), "fonts")

def _font(size: int, bold: bool = False):
    candidates = [
        os.path.join(_FONT_DIR, "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"),
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "arialbd.ttf" if bold else "arial.ttf",
    ]
    for p in candidates:
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            continue
    try:
        return ImageFont.load_default(size=size)
    except Exception:
        return ImageFont.load_default()


def _qr_bytes(data: str) -> bytes:
    qr = qrcode.QRCode(version=1, box_size=12, border=2)
    qr.add_data(data)
    qr.make(fit=True)
    buf = io.BytesIO()
    qr.make_image(fill_color="#0D47A1", back_color="white").save(buf, format="PNG")
    return buf.getvalue()


def generate_pass_image(buyer_name: str, codes: list, access_token: str) -> bytes:
    """codes = [{"color": "rojo", "number": 42}, ...]"""
    W, H = 1800, 900
    img = Image.new("RGB", (W, H))
    draw = ImageDraw.Draw(img)

    # Gradient background
    for y in range(H):
        t = y / H
        r  = int(10  + t * 25)
        gv = int(55  + t * 45)
        b  = int(160 + t * (-60))
        draw.line([(0, y), (W, y)], fill=(r, gv, b))

    # Top accent bar
    draw.rectangle([(0, 0), (W, 14)], fill="#1976D2")

    # Left decorative stripe
    draw.rectangle([(0, 0), (10, H)], fill="#1976D2")

    f_title  = _font(72, bold=True)
    f_year   = _font(48, bold=False)
    f_label  = _font(30, bold=False)
    f_name   = _font(50, bold=True)
    f_code   = _font(36, bold=True)
    f_small  = _font(28, bold=False)
    f_tiny   = _font(22, bold=False)

    # Header
    draw.text((70, 40),  "TORNEO DE LAS LUCES", fill="#FFFFFF", font=f_title)
    draw.text((70, 122), "2026  ·  Códigos de Acceso Streaming", fill="#90CAF9", font=f_year)
    draw.rectangle([(70, 185), (900, 188)], fill="#1976D2")

    # Buyer name
    draw.text((70, 200), "TITULAR", fill="#90CAF9", font=f_label)
    draw.text((70, 232), buyer_name.upper(), fill="#FFFFFF", font=f_name)

    # Codes grid
    by_color = {}
    for c in codes:
        by_color.setdefault(c["color"], []).append(c["number"])

    row, col = 0, 0
    cols_per_row = 2
    cell_w, cell_h = 820, 70
    start_x, start_y = 70, 320

    for color_id, nums in sorted(by_color.items()):
        cx = start_x + col * (cell_w + 40)
        cy = start_y + row * cell_h
        meta = COLOR_MAP.get(color_id, {"hex": "#888888", "text": "#ffffff", "name": color_id})

        # Color circle
        draw.ellipse([(cx, cy + 6), (cx + 44, cy + 50)], fill=meta["hex"])

        # Color name
        draw.text((cx + 56, cy + 4), meta["name"], fill="#BBDEFB", font=f_small)

        # Codes
        nums_str = "  ".join(f"{n:04d}" for n in sorted(nums)[:5])
        if len(nums) > 5:
            nums_str += f"  +{len(nums) - 5} más"
        draw.text((cx + 56, cy + 32), nums_str, fill="#FFFFFF", font=f_code)

        col += 1
        if col >= cols_per_row:
            col = 0
            row += 1

    # Footer
    footer_y = H - 56
    draw.rectangle([(0, footer_y - 10), (W, footer_y - 9)], fill="#1565C0")
    draw.text((70, footer_y), f"ID: {access_token[:28].upper()}", fill="#546E7A", font=f_tiny)
    draw.text((70, footer_y + 26), "Válido para acceso streaming al Torneo de las Luces 2026", fill="#546E7A", font=f_tiny)

    # QR code (right side)
    qr_url = f"{APP_URL}/mi-cuenta/{access_token}"
    qr_size = 300
    qr_img = Image.open(io.BytesIO(_qr_bytes(qr_url))).resize((qr_size, qr_size), Image.LANCZOS)
    qr_x = W - qr_size - 60
    qr_y = H - qr_size - 70
    # White background for QR
    draw.rectangle([(qr_x - 10, qr_y - 10), (qr_x + qr_size + 10, qr_y + qr_size + 10)], fill="#FFFFFF")
    img.paste(qr_img, (qr_x, qr_y))
    draw.text((qr_x, qr_y + qr_size + 14), "Escanea → Mi Cuenta", fill="#90CAF9", font=f_tiny)

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=False, compress_level=1)
    return buf.getvalue()


# ─── Email (Resend REST API) ──────────────────────────────────────────────────
def send_confirmation_email(buyer: dict, codes: list, pass_bytes: bytes):
    if not RESEND_API_KEY:
        app.logger.warning("RESEND_API_KEY not set, skipping email.")
        return

    by_color = {}
    for c in codes:
        by_color.setdefault(c["color"], []).append(c["number"])

    rows_html = ""
    for color_id, nums in sorted(by_color.items()):
        meta = COLOR_MAP.get(color_id, {"hex": "#888", "text": "#fff", "name": color_id})
        chips = "".join(
            f'<span style="background:{meta["hex"]};color:{meta["text"]};padding:3px 8px;'
            f'margin:2px;border-radius:4px;font-family:monospace;font-weight:bold;'
            f'display:inline-block">{n:04d}</span>'
            for n in sorted(nums)
        )
        rows_html += (
            f'<tr>'
            f'<td style="padding:8px 12px"><span style="display:inline-block;width:14px;height:14px;'
            f'border-radius:50%;background:{meta["hex"]};border:1px solid #ccc;vertical-align:middle;'
            f'margin-right:6px"></span><strong>{meta["name"]}</strong></td>'
            f'<td style="padding:8px 12px">{chips}</td></tr>'
        )

    html = (
        f'<div style="font-family:Arial,sans-serif;max-width:640px;margin:0 auto;'
        f'background:#0D47A1;color:#fff;padding:30px;border-radius:10px">'
        f'<h1 style="margin:0 0 4px;font-size:2rem">TORNEO DE LAS LUCES</h1>'
        f'<p style="color:#90CAF9;margin:0 0 20px;font-style:italic">2026</p>'
        f'<p>Hola <strong>{buyer["full_name"]}</strong>,</p>'
        f'<p>¡Tus <strong>Códigos de Streaming</strong> de acceso al torneo han sido confirmados!</p>'
        f'<table style="width:100%;border-collapse:collapse;background:rgba(255,255,255,.08);'
        f'border-radius:8px;overflow:hidden;margin:16px 0">{rows_html}</table>'
        f'<p>Total de códigos: <strong>{len(codes)}</strong></p>'
        f'<a href="{APP_URL}/mi-cuenta/{buyer["access_token"]}"'
        f' style="display:inline-block;background:#E91E63;color:#fff;padding:12px 24px;'
        f'border-radius:6px;text-decoration:none;font-weight:bold;margin:12px 0">'
        f'Ver mi Pase Digital</a>'
        f'<p style="color:#90CAF9;font-size:0.8rem;margin-top:24px">Correo generado automáticamente.</p>'
        f'</div>'
    )

    payload = {
        "from": EMAIL_FROM,
        "to": [buyer["email"]],
        "subject": f"¡Tus códigos de streaming! {TORNEO_NAME}",
        "html": html,
        "attachments": [{
            "filename": "pase_streaming.png",
            "content": base64.b64encode(pass_bytes).decode(),
        }],
    }

    try:
        resp = requests.post(
            "https://api.resend.com/emails",
            json=payload,
            headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
            timeout=15,
        )
        if resp.ok:
            app.logger.info(f"Email sent to {buyer['email']} | id={resp.json().get('id')}")
        else:
            app.logger.error(f"Resend error {resp.status_code}: {resp.text[:300]}")
    except Exception as e:
        app.logger.error(f"Email send failed: {e}")


# ─── Helpers ─────────────────────────────────────────────────────────────────
def now_utc():
    return datetime.now(timezone.utc)


def expire_old_reservations():
    db_exec("""
        UPDATE balotas SET status='AVAILABLE', reserved_until=NULL, order_id=NULL
        WHERE status='RESERVED' AND reserved_until < NOW()
    """)
    db_exec("""
        UPDATE orders SET status='EXPIRED'
        WHERE status='PENDING' AND reservation_expires_at < NOW()
    """)


def reserve_codes(packs: int, order_id: int) -> list:
    """Reserve packs×10 codes: packs random codes per color. Returns list of {color, number}."""
    expire_old_reservations()
    all_rows = []
    for color in COLORS:
        rows = db_all(
            "SELECT id, color, number FROM balotas WHERE status='AVAILABLE' AND color=%s ORDER BY RANDOM() LIMIT %s",
            (color["id"], packs),
        )
        if len(rows) < packs:
            return []  # not enough in this color
        all_rows.extend(rows)

    expires_at = now_utc() + timedelta(minutes=RESERVATION_MINUTES)
    ids = [r["id"] for r in all_rows]
    conn = get_db()
    with _cur(conn) as cur:
        cur.execute(
            "UPDATE balotas SET status='RESERVED', reserved_until=%s, order_id=%s WHERE id = ANY(%s)",
            (expires_at, order_id, ids),
        )
    conn.commit()
    return [{"color": r["color"], "number": r["number"]} for r in all_rows]


# ─── Routes ──────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    sold = db_scalar("SELECT COUNT(*) FROM balotas WHERE status='SOLD'") or 0
    available_packs = (100000 - sold) // 10  # approximate packs remaining
    return render_template("index.html",
        torneo_name=TORNEO_NAME,
        code_price=CODE_PRICE_COP,
        pack_price=CODE_PRICE_COP * 10,
        available_packs=available_packs,
        max_packs=MAX_PACKS,
        colors=COLORS,
        sold=sold,
    )


@app.route("/reservar", methods=["POST"])
def reservar():
    f = request.form
    for field in ["full_name", "doc_type", "doc_number", "phone", "email", "city", "packs"]:
        if not f.get(field, "").strip():
            return render_template("index.html",
                torneo_name=TORNEO_NAME, code_price=CODE_PRICE_COP,
                pack_price=CODE_PRICE_COP * 10, available_packs=9999,
                max_packs=MAX_PACKS, colors=COLORS, sold=0,
                error=f"El campo '{field}' es obligatorio.",
            ), 400

    if not f.get("terms"):
        return render_template("index.html",
            torneo_name=TORNEO_NAME, code_price=CODE_PRICE_COP,
            pack_price=CODE_PRICE_COP * 10, available_packs=9999,
            max_packs=MAX_PACKS, colors=COLORS, sold=0,
            error="Debes aceptar los términos y condiciones.",
        ), 400

    try:
        packs = int(f["packs"])
    except ValueError:
        abort(400)
    if packs < MIN_PACKS or packs > MAX_PACKS:
        abort(400)

    total_codes  = packs * 10
    total_amount = total_codes * CODE_PRICE_COP
    access_token = str(uuid.uuid4())
    expires_at   = now_utc() + timedelta(minutes=RESERVATION_MINUTES)

    buyer_id = db_insert("""
        INSERT INTO buyers (full_name, doc_type, doc_number, phone, email, city, accepted_terms, access_token)
        VALUES (%s, %s, %s, %s, %s, %s, 1, %s) RETURNING id
    """, (
        f["full_name"].strip(), f["doc_type"], f["doc_number"].strip(),
        f["phone"].strip(), f["email"].strip().lower(), f["city"].strip(), access_token,
    ))

    order_id = db_insert("""
        INSERT INTO orders (buyer_id, packs, quantity, unit_price, total_amount, reservation_expires_at)
        VALUES (%s, %s, %s, %s, %s, %s) RETURNING id
    """, (buyer_id, packs, total_codes, CODE_PRICE_COP, total_amount, expires_at))

    codes = reserve_codes(packs, order_id)
    if not codes:
        db_exec("DELETE FROM orders WHERE id=%s", (order_id,))
        db_exec("DELETE FROM buyers WHERE id=%s", (buyer_id,))
        return render_template("index.html",
            torneo_name=TORNEO_NAME, code_price=CODE_PRICE_COP,
            pack_price=CODE_PRICE_COP * 10, available_packs=0,
            max_packs=MAX_PACKS, colors=COLORS, sold=0,
            error="No hay suficientes códigos disponibles. Intenta con menos paquetes.",
        ), 409

    reference   = f"TORNEO-{order_id}"
    payment_url = ""
    wompi_error = ""

    if WOMPI_PRIVATE_KEY:
        try:
            link_data = wompi_create_payment_link(
                amount_cop=total_amount,
                reference=reference,
                description=f"{packs} paquete(s) — {total_codes} códigos de streaming",
            )
            link_id = str(link_data.get("id", ""))
            payment_url = (
                link_data.get("permalink")
                or link_data.get("url")
                or (f"https://checkout.wompi.co/l/{link_id}" if link_id else "")
            )
            if not payment_url:
                wompi_error = f"Wompi OK pero sin ID ni URL. Campos: {list(link_data.keys())}"
                app.logger.error(wompi_error)
            else:
                db_exec(
                    "UPDATE orders SET wompi_payment_link_id=%s, wompi_payment_link_url=%s WHERE id=%s",
                    (link_id, payment_url, order_id),
                )
        except Exception as e:
            wompi_error = str(e)
            app.logger.error(f"Wompi link error: {e}")

    # Group codes by color for template
    by_color = {}
    for c in codes:
        by_color.setdefault(c["color"], []).append(c["number"])

    codes_by_color = [
        {**COLOR_MAP[cid], "numbers": sorted(nums)}
        for cid, nums in sorted(by_color.items())
    ]

    return render_template("checkout.html",
        torneo_name=TORNEO_NAME,
        buyer_name=f["full_name"].strip(),
        buyer_email=f["email"].strip(),
        packs=packs,
        total_codes=total_codes,
        unit_price=CODE_PRICE_COP,
        total_amount=total_amount,
        codes_by_color=codes_by_color,
        payment_url=payment_url,
        wompi_error=wompi_error,
        reference=reference,
        access_token=access_token,
        expires_minutes=RESERVATION_MINUTES,
        wompi_public_key=WOMPI_PUBLIC_KEY,
        integrity_hash=wompi_integrity_hash(reference, total_amount * 100),
        amount_cents=total_amount * 100,
    )


@app.route("/pago/resultado")
def pago_resultado():
    tx_id  = request.args.get("id", "")
    ref    = request.args.get("reference", "")
    status = request.args.get("status", "")
    access_token = ""

    # Si no tenemos referencia pero sí el tx_id, consultamos Wompi para obtenerla
    if tx_id and not ref:
        try:
            tx = wompi_get_transaction(tx_id)
            ref    = tx.get("reference", "")
            status = tx.get("status", status)
            app.logger.info(f"pago_resultado lookup by tx_id={tx_id} → ref={ref} status={status}")
        except Exception as e:
            app.logger.error(f"pago_resultado tx lookup error: {e}")

    if ref.startswith("TORNEO-"):
        try:
            order_id = int(ref.split("-")[1])
            order = db_one("SELECT * FROM orders WHERE id=%s", (order_id,))
            if order:
                buyer = db_one("SELECT * FROM buyers WHERE id=%s", (order["buyer_id"],))
                if buyer:
                    access_token = buyer["access_token"]

                if order["status"] == "PENDING":
                    confirmed_tx = tx_id if status == "APPROVED" else wompi_find_approved_transaction(ref)
                    if confirmed_tx:
                        confirm_order(order_id, confirmed_tx)
                        status = "APPROVED"
                elif order["status"] == "PAID":
                    status = "APPROVED"
        except Exception as e:
            app.logger.error(f"pago_resultado error: {e}")

    return render_template("resultado.html",
        torneo_name=TORNEO_NAME,
        transaction_id=tx_id,
        order_ref=ref,
        status=status,
        access_token=access_token,
    )


@app.route("/buscar", methods=["GET", "POST"])
def buscar():
    results = None
    email_buscado = ""
    error = ""
    if request.method == "POST":
        email_buscado = request.form.get("email", "").strip().lower()
        if not email_buscado:
            error = "Ingresa tu correo electrónico."
        else:
            # Buscar todos los buyers con ese correo (puede haberse registrado varias veces)
            buyers = db_all("SELECT * FROM buyers WHERE email=%s ORDER BY id DESC", (email_buscado,))
            if not buyers:
                error = "No encontramos ningún registro con ese correo."
            else:
                main_buyer = dict(buyers[0])
                orders_data = []
                for buyer in buyers:
                    orders = db_all("SELECT * FROM orders WHERE buyer_id=%s ORDER BY created_at DESC", (buyer["id"],))
                    for order in orders:
                        raw = db_all(
                            "SELECT color, number FROM balotas WHERE order_id=%s ORDER BY color, number",
                            (order["id"],),
                        )
                        by_color = {}
                        for r in raw:
                            by_color.setdefault(r["color"], []).append(r["number"])
                        codes_by_color = [
                            {**COLOR_MAP[cid], "numbers": nums}
                            for cid, nums in sorted(by_color.items())
                            if cid in COLOR_MAP
                        ]
                        orders_data.append({
                            "order": dict(order),
                            "codes_by_color": codes_by_color,
                            "buyer_token": buyer["access_token"],
                        })
                # Ordenar: PAID primero, luego por fecha
                orders_data.sort(key=lambda x: (x["order"]["status"] != "PAID", x["order"]["id"]), reverse=False)
                results = {"buyer": main_buyer, "orders": orders_data}

    return render_template("buscar.html",
        torneo_name=TORNEO_NAME,
        results=results,
        email_buscado=email_buscado,
        error=error,
    )


@app.route("/wompi/webhook", methods=["POST"])
def wompi_webhook():
    sig = request.headers.get("X-Wompi-Signature", "")
    if not wompi_validate_webhook(request.data, sig):
        app.logger.warning(f"Webhook signature mismatch. sig={sig[:40]}")
        return "", 400

    event        = request.json or {}
    event_type   = event.get("event", "")
    tx_data      = event.get("data", {}).get("transaction", {})
    reference    = tx_data.get("reference", "")
    wompi_status = tx_data.get("status", "")
    wompi_tx_id  = str(tx_data.get("id", ""))

    # Log completo para diagnóstico
    import json as _json
    app.logger.info(f"WEBHOOK_FULL: {_json.dumps(event)[:800]}")
    app.logger.info(f"Webhook event={event_type} ref={reference} status={wompi_status} tx={wompi_tx_id}")

    if event_type != "transaction.updated" or not reference.startswith("TORNEO-"):
        return "", 200

    try:
        order_id = int(reference.split("-")[1])
    except (IndexError, ValueError):
        return "", 200

    if wompi_status == "APPROVED":
        confirm_order(order_id, wompi_tx_id)
    elif wompi_status in ("DECLINED", "VOIDED", "ERROR"):
        db_exec("UPDATE orders SET status='FAILED' WHERE id=%s AND status='PENDING'", (order_id,))
        db_exec("""UPDATE balotas SET status='AVAILABLE', reserved_until=NULL, order_id=NULL
                   WHERE order_id=%s AND status='RESERVED'""", (order_id,))

    return "", 200


@app.route("/mi-cuenta/<token>")
def mi_cuenta(token):
    buyer = db_one("SELECT * FROM buyers WHERE access_token=%s", (token,))
    if not buyer:
        abort(404)

    orders = db_all("SELECT * FROM orders WHERE buyer_id=%s ORDER BY created_at DESC", (buyer["id"],))
    orders_data = []
    for order in orders:
        raw = db_all("SELECT color, number FROM balotas WHERE order_id=%s ORDER BY color, number",
                     (order["id"],))
        by_color = {}
        for r in raw:
            by_color.setdefault(r["color"], []).append(r["number"])
        codes_by_color = [
            {**COLOR_MAP[cid], "numbers": nums}
            for cid, nums in sorted(by_color.items())
            if cid in COLOR_MAP
        ]
        orders_data.append({"order": dict(order), "codes_by_color": codes_by_color})

    return render_template("cuenta.html",
        torneo_name=TORNEO_NAME,
        buyer=dict(buyer),
        orders=orders_data,
        app_url=APP_URL,
    )


@app.route("/pase/<token>.png")
def pase_image(token):
    buyer = db_one("SELECT * FROM buyers WHERE access_token=%s", (token,))
    if not buyer:
        abort(404)
    codes = [dict(r) for r in db_all("""
        SELECT b.color, b.number FROM balotas b
        JOIN orders o ON b.order_id = o.id
        WHERE o.buyer_id=%s AND b.status='SOLD' ORDER BY b.color, b.number
    """, (buyer["id"],))]
    img_bytes = generate_pass_image(buyer["full_name"], codes, token)
    resp = make_response(img_bytes)
    resp.headers["Content-Type"] = "image/png"
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/admin")
def admin_panel():
    if request.args.get("secret", "") != os.getenv("ADMIN_SECRET", ""):
        abort(403)

    # Global stats
    stats = {
        "buyers":         db_scalar("SELECT COUNT(*) FROM buyers") or 0,
        "orders_paid":    db_scalar("SELECT COUNT(*) FROM orders WHERE status='PAID'") or 0,
        "orders_pending": db_scalar("SELECT COUNT(*) FROM orders WHERE status='PENDING'") or 0,
        "codes_sold":     db_scalar("SELECT COUNT(*) FROM balotas WHERE status='SOLD'") or 0,
        "codes_available":db_scalar("SELECT COUNT(*) FROM balotas WHERE status='AVAILABLE'") or 0,
        "revenue_cop":    db_scalar("SELECT COALESCE(SUM(total_amount),0) FROM orders WHERE status='PAID'") or 0,
    }

    # Per-color stats with buyer list
    color_stats = []
    for c in COLORS:
        cid = c["id"]
        sold      = db_scalar("SELECT COUNT(*) FROM balotas WHERE color=%s AND status='SOLD'", (cid,)) or 0
        available = db_scalar("SELECT COUNT(*) FROM balotas WHERE color=%s AND status='AVAILABLE'", (cid,)) or 0
        reserved  = db_scalar("SELECT COUNT(*) FROM balotas WHERE color=%s AND status='RESERVED'", (cid,)) or 0
        pct       = round(sold / 100, 1)  # of 10,000

        rows = db_all("""
            SELECT b.full_name, array_agg(ba.number ORDER BY ba.number) AS nums
            FROM balotas ba
            JOIN orders o  ON ba.order_id = o.id
            JOIN buyers b  ON b.id = o.buyer_id
            WHERE ba.color=%s AND ba.status='SOLD'
            GROUP BY b.full_name
            ORDER BY b.full_name
        """, (cid,))
        buyers_list = [{"full_name": r["full_name"], "codes": [f"{n:04d}" for n in (r["nums"] or [])]} for r in rows]
        color_stats.append({**c, "sold": sold, "available": available, "reserved": reserved, "pct": pct, "buyers": buyers_list})

    # Orders (recent 200)
    orders = db_all("""
        SELECT o.id, o.status, o.packs, o.total_amount, o.created_at,
               b.full_name, b.email, b.access_token
        FROM orders o JOIN buyers b ON b.id = o.buyer_id
        ORDER BY o.id DESC LIMIT 200
    """)

    # Buyers with paid order count and code chips
    buyers_raw = db_all("""
        SELECT b.id, b.full_name, b.email, b.doc_type, b.doc_number,
               b.city, b.phone, b.created_at,
               COUNT(o.id) FILTER (WHERE o.status='PAID') AS paid_orders
        FROM buyers b
        LEFT JOIN orders o ON o.buyer_id = b.id
        GROUP BY b.id ORDER BY b.id DESC
    """)

    buyers = []
    for b in buyers_raw:
        codes_raw = db_all("""
            SELECT ba.color, ba.number FROM balotas ba
            JOIN orders o ON ba.order_id = o.id
            WHERE o.buyer_id=%s AND ba.status='SOLD'
            ORDER BY ba.color, ba.number
        """, (b["id"],))
        chips = [{"label": f"{r['number']:04d}", "hex": COLOR_MAP.get(r["color"], {}).get("hex", "#888")} for r in codes_raw]
        buyers.append({**dict(b), "code_chips": chips})

    return render_template("admin.html",
        stats=stats,
        color_stats=color_stats,
        orders=[dict(r) for r in orders],
        buyers=buyers,
        admin_secret=os.getenv("ADMIN_SECRET", ""),
        now=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    )


@app.route("/admin/ordenes")
def admin_ordenes():
    if request.args.get("secret", "") != os.getenv("ADMIN_SECRET", ""):
        abort(403)
    rows = db_all("""
        SELECT o.id, o.status, o.packs, o.total_amount, o.wompi_transaction_id,
               o.wompi_payment_link_url, o.reservation_expires_at, o.created_at,
               b.full_name, b.email, b.access_token
        FROM orders o JOIN buyers b ON b.id = o.buyer_id
        ORDER BY o.id DESC LIMIT 20
    """)
    return jsonify([dict(r) for r in rows])


@app.route("/admin/confirmar/<int:order_id>")
def admin_confirmar(order_id):
    if request.args.get("secret", "") != os.getenv("ADMIN_SECRET", ""):
        abort(403)
    order = db_one("SELECT * FROM orders WHERE id=%s", (order_id,))
    if not order:
        return jsonify({"error": "orden no encontrada"}), 404
    if order["status"] == "PAID":
        return jsonify({"ok": True, "msg": "ya estaba pagada"})
    # Buscar tx aprobado en Wompi
    ref = f"TORNEO-{order_id}"
    tx_id = wompi_find_approved_transaction(ref)
    if not tx_id:
        tx_id = f"MANUAL-{order_id}"  # confirmar manualmente sin tx real
    confirm_order(order_id, tx_id)
    return jsonify({"ok": True, "msg": f"orden {order_id} confirmada", "tx_id": tx_id})


@app.route("/admin/test-email")
def admin_test_email():
    if request.args.get("secret", "") != os.getenv("ADMIN_SECRET", ""):
        abort(403)
    to = request.args.get("to", "samuelvasquez0804@gmail.com")
    payload = {
        "from": EMAIL_FROM,
        "to": [to],
        "subject": "Test email - Torneo de las Luces",
        "html": "<p>Si recibes esto, el email está funcionando ✅</p>",
    }
    try:
        resp = requests.post(
            "https://api.resend.com/emails",
            json=payload,
            headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
            timeout=15,
        )
        return jsonify({
            "status": resp.status_code,
            "resend_response": resp.json(),
            "from": EMAIL_FROM,
            "to": to,
            "api_key_preview": RESEND_API_KEY[:10] + "..." if RESEND_API_KEY else "(vacía)",
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/debug-env")
def debug_env():
    secret = request.args.get("s", "")
    if secret != os.getenv("ADMIN_SECRET", ""):
        abort(403)
    def mask(v):
        return v[:6] + "…" + v[-4:] if v and len(v) > 10 else ("(vacía)" if not v else v)
    return jsonify({
        "WOMPI_SANDBOX":       os.getenv("WOMPI_SANDBOX", "(no set)"),
        "WOMPI_PUBLIC_KEY":    mask(os.getenv("WOMPI_PUBLIC_KEY", "")),
        "WOMPI_PRIVATE_KEY":   mask(os.getenv("WOMPI_PRIVATE_KEY", "")),
        "WOMPI_EVENTS_KEY":    mask(os.getenv("WOMPI_EVENTS_KEY", "")),
        "WOMPI_INTEGRITY_KEY": mask(os.getenv("WOMPI_INTEGRITY_KEY", "")),
        "APP_URL":             os.getenv("APP_URL", "(no set)"),
        "DATABASE_URL":        "…" + os.getenv("DATABASE_URL", "")[-20:] if os.getenv("DATABASE_URL") else "(vacía)",
    })


@app.route("/admin/stats")
def admin_stats():
    if request.args.get("secret", "") != os.getenv("ADMIN_SECRET", ""):
        abort(403)
    stats = {"colors": {}}
    for color in COLORS:
        cid = color["id"]
        stats["colors"][cid] = {
            "sold":      db_scalar("SELECT COUNT(*) FROM balotas WHERE color=%s AND status='SOLD'", (cid,)) or 0,
            "available": db_scalar("SELECT COUNT(*) FROM balotas WHERE color=%s AND status='AVAILABLE'", (cid,)) or 0,
        }
    stats["orders_paid"]    = db_scalar("SELECT COUNT(*) FROM orders WHERE status='PAID'") or 0
    stats["orders_pending"] = db_scalar("SELECT COUNT(*) FROM orders WHERE status='PENDING'") or 0
    stats["revenue_cop"]    = db_scalar("SELECT COALESCE(SUM(total_amount),0) FROM orders WHERE status='PAID'") or 0
    stats["buyers"]         = db_scalar("SELECT COUNT(*) FROM buyers") or 0
    return jsonify(stats)


try:
    with app.app_context():
        init_db()
except Exception as _init_err:
    import traceback
    print(f"[WARN] init_db failed at startup: {_init_err}")
    traceback.print_exc()

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
