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
# CONFIGURACIÓN DE NÚMEROS
# ==========================================

ADMIN_PRINCIPAL = "+593990516017"
NUMERO_ARRIENDOS = "+593960153241"

ADMINS = ["+593990516017", "+351927903369"]
NUMEROS_BYRON = ["+351961545289", "+351961545268"]

# Memoria temporal del modo admin
modo_admin = {}    # { numero: "P" / "S" / "A" }

# ARCHIVOS
ARCHIVO_GASTOS = "GASTOS_AUTOMÁTICOS"
ARCHIVO_ARRIENDOS = "INGRESOS_ARRIENDOS"

TAB_PERSONAL = "PERSONAL"
TAB_ALEX = "ALEX"
TAB_BYRON = "BYRON"

# ==========================================
# GOOGLE AUTH
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
    "auth_provider_x509_cert_url": os.getenv("GOOGLE_AUTH_PROVIDER_CERT_URL"),
    "client_x509_cert_url": os.getenv("GOOGLE_CLIENT_CERT_URL"),
}

credentials = service_account.Credentials.from_service_account_info(credentials_dict, scopes=scope)
client = gspread.authorize(credentials)
drive_service = build('drive', 'v3', credentials=credentials)
vision_client = vision.ImageAnnotatorClient()

# Cargar hojas
gastos = client.open(ARCHIVO_GASTOS)
sheet_personal = gastos.worksheet(TAB_PERSONAL)
sheet_alex = gastos.worksheet(TAB_ALEX)
sheet_byron = gastos.worksheet(TAB_BYRON)

hoja_arriendos = client.open(ARCHIVO_ARRIENDOS).sheet1


# ==========================================
# FUNCIONES OCR
# ==========================================

def leer_texto_imagen(url):
    """Devuelve texto leído desde una imagen usando Vision AI"""
    img_data = requests.get(url).content
    image = vision.Image(content=img_data)
    response = vision_client.text_detection(image=image)
    return response.text_annotations[0].description if response.text_annotations else ""


def extraer_datos_arriendo(texto):
    """Extrae datos comunes de comprobantes bancarios."""
    # NOMBRE (primera palabra larga con letras)
    nombre = re.search(r"[A-ZÑ ]{4,}", texto.upper())
    nombre = nombre.group(0).title() if nombre else "Desconocido"

    # MONTO: detecta 12.50 / 12,50 / $12.50 / USD 12.50
    monto = re.search(r"(\$|USD)?\s*([0-9]+[\.,][0-9]{2})", texto)
    valor = monto.group(2).replace(",", ".") if monto else ""

    # NUMERO DE COMPROBANTE
    comp = re.search(r"(COMPROBANTE|TRANSACCIÓN|DOC|REFERENCIA)[^\d]*(\d+)", texto, re.IGNORECASE)
    comprobante = comp.group(2) if comp else ""

    return nombre, comprobante, valor


# ==========================================
# WEBHOOK PRINCIPAL
# ==========================================

@app.route("/webhook", methods=["POST"])
def webhook():
    msg = request.form.get("Body", "").strip()
    sender = request.form.get("From", "").replace("whatsapp:", "")
    num_media = int(request.form.get("NumMedia", 0))

    resp = MessagingResponse()
    r = resp.message()

    # ====================================
    # 🔥 MODO A → ARRIENDOS
    # ====================================
    if sender == ADMIN_PRINCIPAL and msg.upper() == "A":
        modo_admin[sender] = "A"
        r.body("🏠 Modo *ARRIENDOS* ACTIVADO.\nEnvía la imagen del comprobante.")
        return str(resp)

    # ====================================
    # 🏠 PROCESAR ARRIENDOS (solo modo A)
    # ====================================
    if modo_admin.get(sender) == "A":

        if sender != NUMERO_ARRIENDOS:
            r.body("❌ Este número NO está autorizado para registrar arriendos.")
            return str(resp)

        if num_media == 0:
            r.body("📸 Envía una *imagen del comprobante* para procesar el arriendo.")
            return str(resp)

        # Leer imagen con OCR
        url = request.form.get("MediaUrl0")
        texto = leer_texto_imagen(url)
        nombre, comprobante, valor = extraer_datos_arriendo(texto)

        fecha_actual = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        hoja_arriendos.append_row([
            fecha_actual,
            nombre,
            comprobante,
            valor
        ])

        r.body(
            f"🏠 *ARRENDAMIENTO REGISTRADO*\n"
            f"📅 Fecha: {fecha_actual}\n"
            f"👤 Nombre: {nombre}\n"
            f"📄 Comprobante: {comprobante}\n"
            f"💰 Valor: {valor}\n"
        )
        return str(resp)

    # ====================================
    # 🔥 MODO GASTOS (tu lógica actual)
    # ====================================

    # Administrador cambia modo P/S
    if sender in ADMINS and msg.upper() in ["P", "S"]:
        modo_admin[sender] = msg.upper()
        destino = "PERSONAL" if msg.upper() == "P" else "ALEX"
        r.body(f"✔ Modo cambiado a: *{destino}*")
        return str(resp)

    # Selección de hoja destino
    if sender in NUMEROS_BYRON:
        hoja = sheet_byron
    elif sender in ADMINS:
        modo = modo_admin.get(sender, "P")
        hoja = sheet_personal if modo == "P" else sheet_alex
    else:
        hoja = sheet_byron

    # Registro normal de gastos
    monto, moneda = extraer_monto_y_moneda(msg)
    categoria = clasificar_categoria(msg)
    descripcion = limpiar_descripcion(msg)
    fecha = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    link = ""

    if num_media > 0:
        link = subir_foto_drive(request.form.get("MediaUrl0"))

    hoja.append_row([fecha, sender, categoria, descripcion, monto, moneda, link])

    r.body(f"✅ Gasto registrado\n📅 {fecha}\n🏷️ {categoria}\n💬 {descripcion}\n💰 {monto}{moneda}")
    return str(resp)


# ==========================================
# INICIO
# ==========================================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
