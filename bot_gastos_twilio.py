from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
import re
from datetime import datetime
import gspread
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.cloud import vision
import os
import requests

app = Flask(__name__)

# ==========================================
# ⚙️ CONFIGURACIÓN DE NÚMEROS
# ==========================================

ADMIN_PRINCIPAL = "+593990516017"

ADMINS = [
    "+593990516017",
    "+351927903369"
]

NUMEROS_BYRON = ["+351961545289", "+351961545268"]

# Números autorizados para enviar comprobantes de arriendos
NUMEROS_ARRIENDOS = ["+593960153241", "+593990516017"]

# Memoria temporal del modo admin
modo_admin = {}

# ==========================================
# HOJAS DE GASTOS
# ==========================================

TAB_PERSONAL = "PERSONAL"
TAB_ALEX = "ALEX"
TAB_BYRON = "BYRON"
ARCHIVO_GS = "GASTOS_AUTOMÁTICOS"

# ==========================================
# HOJA NUEVA: ARRIENDOS
# ==========================================

ARCHIVO_ARRIENDOS = "INGRESOS_ARRIENDOS"
TABLA_ARRIENDOS = "Hoja 1"

# ==========================================
# 🔹 GOOGLE CREDENTIALS
# ==========================================

scope = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

credentials_dict = {
    "type": os.getenv("GOOGLE_TYPE"),
    "project_id": os.getenv("GOOGLE_PROJECT_ID"),
    "private_key_id": os.getenv("GOOGLE_PRIVATE_KEY_ID"),
    "private_key": os.getenv("GOOGLE_PRIVATE_KEY").replace("\\n", "\n"),
    "client_email": os.getenv("GOOGLE_CLIENT_EMAIL"),
    "client_id": os.getenv("GOOGLE_CLIENT_ID"),
    "auth_uri": os.getenv("GOOGLE_AUTH_URI"),
    "token_uri": os.getenv("GOOGLE_TOKEN_URI"),
    "auth_provider_x509_cert_url": os.getenv("GOOGLE_AUTH_PROVIDER_X509_CERT_URL"),
    "client_x509_cert_url": os.getenv("GOOGLE_CLIENT_X509_CERT_URL"),
}

credentials = service_account.Credentials.from_service_account_info(
    credentials_dict, scopes=scope
)

client = gspread.authorize(credentials)
drive_service = build("drive", "v3", credentials=credentials)

# Sheets gastos
archivo = client.open(ARCHIVO_GS)
sheet_personal = archivo.worksheet(TAB_PERSONAL)
sheet_alex = archivo.worksheet(TAB_ALEX)
sheet_byron = archivo.worksheet(TAB_BYRON)

# Sheets arriendos
archivo_arriendos = client.open(ARCHIVO_ARRIENDOS)
sheet_arriendos = archivo_arriendos.sheet1

# ==========================================
# OCR CLIENT
# ==========================================
vision_client = vision.ImageAnnotatorClient()

# ==========================================
# 🔹 SUBIR FOTO A DRIVE
# ==========================================

def subir_foto_drive(url):
    try:
        r = requests.get(url)
        if r.status_code != 200:
            return None, None

        os.makedirs("temp", exist_ok=True)

        fname = f"temp/{datetime.now().strftime('%Y%m%d_%H%M%S')}.jpg"
        with open(fname, "wb") as f:
            f.write(r.content)

        meta = {"name": os.path.basename(fname)}
        media = MediaFileUpload(fname, mimetype="image/jpeg")

        file = drive_service.files().create(
            body=meta, media_body=media, fields="id"
        ).execute()

        drive_service.permissions().create(
            fileId=file["id"],
            body={"role": "reader", "type": "anyone"},
        ).execute()

        link = f"https://drive.google.com/file/d/{file['id']}/view?usp=sharing"
        return fname, link

    except Exception as e:
        print("ERROR SUBIENDO A DRIVE:", e)
        return None, None


# ==========================================
# 🔹 OCR
# ==========================================

def leer_texto_ocr(local_path):
    with open(local_path, "rb") as img_file:
        content = img_file.read()

    image = vision.Image(content=content)
    response = vision_client.text_detection(image=image)

    if not response.text_annotations:
        return ""

    return response.text_annotations[0].description


# ==========================================
# Extraer monto
# ==========================================

def extraer_monto_y_moneda(texto):
    t = texto.lower()
    patrones = [
        (re.compile(r'(?:\$)\s*([0-9]+(?:[.,][0-9]{1,2})?)'), "$"),
        (re.compile(r'([0-9]+(?:[.,][0-9]{1,2})?)\s*\$'), "$"),
        (re.compile(r'(?:€)\s*([0-9]+(?:[.,][0-9]{1,2})?)'), "€"),
        (re.compile(r'([0-9]+(?:[.,][0-9]{1,2})?)\s*€'), "€"),
    ]

    for rex, moneda in patrones:
        m = rex.search(t)
        if m:
            return m.group(1).replace(",", "."), moneda

    # Detecta número suelto
    m = re.search(r'\b([0-9]+(?:[.,][0-9]{1,2})?)\b', t)
    if m:
        return m.group(1).replace(",", "."), "USD"

    return None, None


# ==========================================
# 🔥 WEBHOOK
# ==========================================

@app.route("/webhook", methods=["POST"])
def webhook():

    msg = request.form.get("Body", "").strip()
    sender = request.form.get("From", "").replace("whatsapp:", "")
    num_media = int(request.form.get("NumMedia", 0))

    resp = MessagingResponse()
    r = resp.message()

    # ==========================================
    # 🔥 ADMIN Cambia Modo: P, S, A
    # ==========================================
    if sender == ADMIN_PRINCIPAL and msg.upper() in ["P", "S", "A"]:
        modo_admin[ADMIN_PRINCIPAL] = msg.upper()

        destino = {
            "P": "PERSONAL",
            "S": "ALEX",
            "A": "ARRIENDOS",
        }[msg.upper()]

        r.body(f"✔ Modo cambiado a: *{destino}*")
        return str(resp)

    # ==========================================
    # 🔥 MODO ARRIENDOS
    # ==========================================

    if modo_admin.get(ADMIN_PRINCIPAL) == "A" and sender in NUMEROS_ARRIENDOS:

        if num_media == 0:
            r.body("❗ Envía la *foto del comprobante* para registrar el ingreso.")
            return str(resp)

        local_path, drive_link = subir_foto_drive(request.form.get("MediaUrl0"))
        texto = leer_texto_ocr(local_path)

        monto, moneda = extraer_monto_y_moneda(texto)
        doc = re.search(r'\b\d{6,}\b', texto)
        documento = doc.group(0) if doc else "NO_DETECTADO"

        fecha = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        sheet_arriendos.append_row(
            [fecha, sender, documento, monto, drive_link]
        )

        r.body(
            f"🏠 *Ingreso registrado correctamente*\n\n"
            f"📅 Fecha: {fecha}\n"
            f"👤 Número: {sender}\n"
            f"🧾 Documento: {documento}\n"
            f"💰 Monto: {monto}\n"
            f"📎 Comprobante: {drive_link}"
        )
        return str(resp)

    # ==========================================
    # 🔥 GASTOS NORMALES
    # ==========================================

    if sender in NUMEROS_BYRON:
        hoja = sheet_byron
    elif sender in ADMINS:
        modo = modo_admin.get(sender, "P")
        hoja = sheet_personal if modo == "P" else sheet_alex
    else:
        hoja = sheet_byron

    monto, moneda = extraer_monto_y_moneda(msg)
    fecha = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    hoja.append_row([fecha, sender, "Gasto", msg, monto, moneda, ""])

    r.body("✅ Gasto registrado correctamente")

    return str(resp)


# ==========================================
# RUN
# ==========================================

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
