import os
from urllib.parse import urlparse

from flask import Flask, render_template, jsonify
from psycopg2.pool import ThreadedConnectionPool
from psycopg2.extras import RealDictCursor

import joblib
import numpy as np
from twilio.rest import Client as TwilioClient

# ─── Configuration ─────────────────────────────────────────────────────────────
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL must be set")

# Twilio WhatsApp
TW_SID   = os.getenv("TWILIO_SID")
TW_TOKEN = os.getenv("TWILIO_TOKEN")
TW_FROM  = os.getenv("TWILIO_FROM")
TW_TO    = os.getenv("TWILIO_TO")
for var,name in [(TW_SID,"TWILIO_SID"), (TW_TOKEN,"TWILIO_TOKEN"),
                 (TW_FROM,"TWILIO_FROM"), (TW_TO,"TWILIO_TO")]:
    if not var:
        raise RuntimeError(f"{name} must be set")

# ─── Load RF model & encoder ───────────────────────────────────────────────────
_rf = joblib.load("rf_model_render.pkl")
_le = joblib.load("label_encoder_render.pkl")

def predict_severity(ir: float, humidity: float, gas: float) -> str:
    """Return RF‑predicted label."""
    arr = np.array([[ir, humidity, gas]])
    idx = _rf.predict(arr)[0]
    return _le.inverse_transform([idx])[0]

# ─── Flask & Postgres pool ─────────────────────────────────────────────────────
app = Flask(__name__)
url = urlparse(DATABASE_URL)
pool = ThreadedConnectionPool(
    1, 10,
    user=url.username, password=url.password,
    host=url.hostname, port=url.port or 5432,
    dbname=url.path.lstrip("/"),
    sslmode="require"
)

# ─── Twilio client & severity tracking ─────────────────────────────────────────
_twilio = TwilioClient(TW_SID, TW_TOKEN)
_last_severity = None  # keep track of what we last alerted on

def send_whatsapp_alert(lat: float, lng: float, severity: str):
    """Send a WhatsApp alert via Twilio."""
    link = f"https://www.google.com/maps/search/?api=1&query={lat},{lng}"
    body = f"Wildfire Alert ({severity}): risk detected!\nLocation: {link}"
    msg = _twilio.messages.create(body=body, from_=TW_FROM, to=TW_TO)
    print(f"[ALERT] Sent WhatsApp: {severity}, SID={msg.sid}")

# ─── Data fetchers ─────────────────────────────────────────────────────────────
def fetch_latest():
    """Return the newest reading and trigger WhatsApp alert only on severity change."""
    global _last_severity

    conn = pool.getconn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT
                  (EXTRACT(EPOCH FROM ts)*1000)::BIGINT AS timestamp,
                  (sensors->>'ir')::FLOAT       AS ir,
                  (sensors->>'humidity')::FLOAT AS humidity,
                  (sensors->>'gas')::FLOAT      AS gas,
                  (location->>'lat')::FLOAT     AS lat,
                  (location->>'lng')::FLOAT     AS lng
                FROM sensor_data
                ORDER BY ts DESC
                LIMIT 1
            """)
            row = cur.fetchone()
            if not row:
                return None

            # Predict severity
            sev = predict_severity(row["ir"], row["humidity"], row["gas"])
            row["severity"] = sev

            # Only alert once when crossing into MediumRisk/HighRisk
            if sev != _last_severity:
                if sev in ("MediumRisk", "HighRisk"):
                    send_whatsapp_alert(row["lat"], row["lng"], sev)
                _last_severity = sev

            return row
    finally:
        pool.putconn(conn)

def fetch_history(limit=100):
    """Return the last `limit` readings, oldest→newest."""
    conn = pool.getconn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT
                  (EXTRACT(EPOCH FROM ts)*1000)::BIGINT AS timestamp,
                  (sensors->>'ir')::FLOAT       AS ir,
                  (sensors->>'humidity')::FLOAT AS humidity,
                  (sensors->>'gas')::FLOAT      AS gas,
                  (location->>'lat')::FLOAT     AS lat,
                  (location->>'lng')::FLOAT     AS lng
                FROM sensor_data
                ORDER BY ts DESC
                LIMIT %s
            """, (limit,))
            rows = cur.fetchall()

        out = []
        for row in reversed(rows):
            row["severity"] = predict_severity(
                row["ir"], row["humidity"], row["gas"]
            )
            out.append(row)
        return out
    finally:
        pool.putconn(conn)

# ─── Routes ───────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/latest")
def latest():
    data = fetch_latest()
    return (jsonify(data) if data else ("", 204))

@app.route("/history")
def history():
    return jsonify(fetch_history())

# ─── Run ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
