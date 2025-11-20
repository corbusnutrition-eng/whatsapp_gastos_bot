from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
import re
from datetime import datetime
import gspread
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
import os
import requests

app = Flask(__name__)

# ==========================================
# ⚙️ CONFIGURACIÓN DE NÚMEROS
# ==========================================

ADMINS = ["+593990516017", "+351927903369"]
NUMEROS_SOCIEDAD = ["+351961545289", "+351961545268"]

# Memoria temporal del modo admin
modo_usuario = {}     # { "+59399..." : "P" / "S" }

# Archivos y pestañas
ARCHIVO_GS = "GASTOS_AUTOMÁTICOS"
TAB_PERSONAL = "PERSONAL"
TAB_SOCIEDAD = "SOCIEDAD"

# ==========================================
# 📁 CARPETAS GOOGLE DRIVE (PON TUS IDS)
# ==========================================

FOLDER_PERSONAL = "1DAPnUuuR19moXTPLN70GsLRVbyjT06R0"
FOLDER_SOCIEDAD = "1eLsPS5656bzNMlm3W7hF8uH197kBX3Pse"

# ==========================================
# 🔹 GOOGLE SHEETS + GOOGLE DRIVE
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

archivo = client.open(ARCHIVO_GS)
sheet_personal = archivo.worksheet(TAB_PERSONAL)
sheet_sociedad = archivo.worksheet(TAB_SOCIEDAD)

# ==========================================
# 🔹 SUBIR FOTO A CARPETA CORRECTA
# ==========================================

def subir_foto_drive(url_imagen, carpeta_id, categoria, monto, moneda):
    try:
        response = requests.get(url_imagen)
        if response.status_code != 200:
            return None

        os.makedirs("temp", exist_ok=True)
        filename = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{categoria}_{monto}{moneda}.jpg"
        local_path = f"temp/{filename}"

        with open(local_path, "wb") as f:
            f.write(response.content)

        metadata = {'name': filename, 'parents': [carpeta_id]}
        media = MediaFileUpload(local_path, mimetype="image/jpeg")

        file = drive_service.files().create(
            body=metadata, media_body=media, fields="id"
        ).execute()

        # Hacer archivo público
        drive_service.permissions().create(
            fileId=file["id"],
            body={"role": "reader", "type": "anyone"}
        ).execute()

        os.remove(local_path)

        return f"https://drive.google.com/file/d/{file['id']}/view?usp=sharing"

    except Exception as e:
        print("❌ Error subiendo imagen:", e)
        return None

# ==========================================
# 🔹 EXTRAER MONTO Y MONEDA
# ==========================================

def extraer_monto_y_moneda(texto):
    t = texto.lower()
    patrones = [
        (re.compile(r'(?:€|eur)\s*([0-9]+(?:[.,][0-9]{1,2})?)'), "€"),
        (re.compile(r'(?:\$|usd)\s*([0-9]+(?:[.,][0-9]{1,2})?)'), "$"),
        (re.compile(r'([0-9]+(?:[.,][0-9]{1,2})?)\s*€'), "€"),
        (re.compile(r'([0-9]+(?:[.,][0-9]{1,2})?)\s*\$'), "$"),
    ]

    for reg, moneda in patrones:
        m = reg.search(t)
        if m:
            return m.group(1).replace(",", "."), moneda

    return None, None

# ==========================================
# 🔹 CLASIFICACIÓN DE CATEGORÍAS
# ==========================================

def clasificar_categoria(texto):
    texto = texto.lower()
    categorias = {
        "Supermercado": ["supermercado", "continente", "mercado", "pingo"],
        "Alimentación": ["almuerzo", "comida", "restaurante", "cena", "buffet"],
        "Combustible": ["gasolina", "combustible"],
        "Salud": ["hospital", "doctor", "dentista", "medicina"],
        "Diversión": ["juegos", "salida", "discoteca"],
        "Vestimenta": ["ropa", "zapatos"],
        "Viajes": ["vuelo", "viaje"],
        "Servicios básicos": ["agua", "luz", "internet"],
    }

    for cat, palabras in categorias.items():
        if any(p in texto for p in palabras):
            return cat

    return "Gastos varios"

# ==========================================
# 🔹 LIMPIAR DESCRIPCIÓN
# ==========================================

def limpiar_descripcion(texto):
    texto = re.sub(r'[€\$]\s*\d+(?:[.,]\d{1,2})?', '', texto)
    return texto.strip().capitalize()

# ==========================================
# 🔹 WEBHOOK PRINCIPAL
# ==========================================

@app.route("/webhook", methods=["POST"])
def webhook():
    msg = request.form.get("Body", "").strip()
    sender = request.form.get("From", "").replace("whatsapp:", "")
    num_media = int(request.form.get("NumMedia", 0))

    resp = MessagingResponse()
    r = resp.message()

    # 1️⃣ ADMIN cambia modo
    if sender in ADMINS and msg.upper() in ["P", "S"]:
        modo_usuario[sender] = msg.upper()
        r.body(f"✔ Modo cambiado a: *{'PERSONAL' if msg.upper()=='P' else 'SOCIEDAD'}*")
        return str(resp)

    # 2️⃣ Determinar hoja + carpeta
    if sender in NUMEROS_SOCIEDAD:
        hoja = sheet_sociedad
        carpeta = FOLDER_SOCIEDAD

    elif sender in ADMINS and sender in modo_usuario:
        if modo_usuario[sender] == "S":
            hoja = sheet_sociedad
            carpeta = FOLDER_SOCIEDAD
        else:
            hoja = sheet_personal
            carpeta = FOLDER_PERSONAL

    else:
        hoja = sheet_personal
        carpeta = FOLDER_PERSONAL

    # 3️⃣ Extraer datos
    monto, moneda = extraer_monto_y_moneda(msg)
    categoria = clasificar_categoria(msg)
    descripcion = limpiar_descripcion(msg)
    fecha = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    enlace = ""

    if num_media > 0:
        enlace = subir_foto_drive(
            request.form.get("MediaUrl0"),
            carpeta,
            categoria,
            monto or "0",
            moneda or "€"
        )

    hoja.append_row([fecha, sender, categoria, descripcion, monto or "0", moneda or "€", enlace])

    r.body(f"✅ Gasto registrado\n📅 {fecha}\n🏷 {categoria}\n💬 {descripcion}\n💰 {monto}{moneda}\n📎 {enlace}")

    return str(resp)

# ==========================================
# 🔹 INICIO FLASK
# ==========================================

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
