from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
import re
import difflib
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

# Memoria temporal del modo para administradores
modo_usuario = {}  # {numero: "P" o "S"}

# Nombre de pestañas dentro del mismo archivo
TAB_PERSONAL = "PERSONAL"
TAB_SOCIEDAD = "SOCIEDAD"
ARCHIVO_GS = "GASTOS_AUTOMÁTICOS"

# ==========================================
# 🔹 GOOGLE SHEETS
# ==========================================

scope = ["https://www.googleapis.com/auth/spreadsheets",
         "https://www.googleapis.com/auth/drive"]

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
# 🔹 SUBIR FOTO A GOOGLE DRIVE
# ==========================================

FOLDER_ID = "1WUdVX2k39tj4pcJE4FIUKeJ0FjgRQdw"

def subir_foto_drive(url_imagen, categoria, monto, moneda):
    try:
        response = requests.get(url_imagen)
        if response.status_code != 200:
            return None

        os.makedirs("temp", exist_ok=True)
        nombre_local = f"temp/{datetime.now().strftime('%Y%m%d_%H%M%S')}_{categoria}_{monto}{moneda}.jpg"

        with open(nombre_local, "wb") as f:
            f.write(response.content)

        file_metadata = {'name': os.path.basename(nombre_local), 'parents': [FOLDER_ID]}
        media = MediaFileUpload(nombre_local, mimetype='image/jpeg')
        file = drive_service.files().create(body=file_metadata, media_body=media, fields='id').execute()

        # Hacer el archivo público
        drive_service.permissions().create(
            fileId=file.get('id'),
            body={'role': 'reader', 'type': 'anyone'}
        ).execute()

        enlace = f"https://drive.google.com/file/d/{file.get('id')}/view?usp=sharing"
        os.remove(nombre_local)
        return enlace

    except Exception as e:
        print(f"❌ Error al subir imagen: {e}")
        return None

# ==========================================
# 🔹 EXTRACCIÓN Y CATEGORIZACIÓN
# ==========================================

def extraer_monto_y_moneda(texto):
    t = texto.lower()
    patrones = [
        (re.compile(r'(?:€|\bEUR\b)\s*([0-9]+(?:[.,][0-9]{1,2})?)'), "€"),
        (re.compile(r'(?:\$|\bUSD\b)\s*([0-9]+(?:[.,][0-9]{1,2})?)'), "$"),
        (re.compile(r'([0-9]+(?:[.,][0-9]{1,2})?)\s*(?:€|\bEUR\b)'), "€"),
        (re.compile(r'([0-9]+(?:[.,][0-9]{1,2})?)\s*(?:\$|\bUSD\b)'), "$"),
        (re.compile(r'(?<!\d:)(\b[0-9]+(?:[.,][0-9]{1,2})?\b)(?!:\d{2})'), None),
    ]
    for rex, moneda_forzada in patrones:
        m = rex.search(t)
        if m:
            monto = m.group(1).replace(",", ".")
            moneda = moneda_forzada or "€"
            return monto, moneda
    return None, None

def clasificar_categoria(texto):
    categorias = {
        "Supermercado": ["supermercado", "continente", "mercado", "pingo"],
        "Alimentación": ["almuerzo", "comida", "restaurante", "cena"],
        "Combustible": ["combustible", "gasolina"],
        "Salud": ["hospital", "medicina", "doctor", "dentista"],
        "Educación": ["colegio", "libros"],
        "Diversión": ["juegos", "salida", "cervezas"],
        "Vestimenta": ["ropa", "zapatos"],
        "Viajes": ["viaje", "vuelo"],
        "Mantenimiento": ["arreglo", "reparación"],
        "Servicios básicos": ["agua", "luz", "internet"],
        "Créditos": ["crédito", "banco"],
        "Construcción": ["construcción"],
        "Transporte": ["uber", "taxi"],
    }

    texto_limpio = texto.lower()
    for palabra in texto_limpio.split():
        for cat, keywords in categorias.items():
            if palabra in keywords:
                return cat

    return "Gastos varios"

def limpiar_descripcion(texto):
    descripcion = re.sub(r'[€$]\s*\d+(?:[.,]\d{1,2})?', '', texto)
    descripcion = re.sub(r'\s+', ' ', descripcion)
    return descripcion.strip().capitalize()

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

    # ADMIN cambia modo con "P" o "S"
    if sender in ADMINS and msg.upper() in ["P", "S"]:
        modo_usuario[sender] = msg.upper()
        r.body(f"✔ Modo cambiado a: *{'PERSONAL' if msg.upper()=='P' else 'SOCIEDAD'}*")
        return str(resp)

    # Determinar hoja destino
    if sender in NUMEROS_SOCIEDAD:
        hoja = sheet_sociedad
    elif sender in ADMINS and sender in modo_usuario:
        hoja = sheet_sociedad if modo_usuario[sender] == "S" else sheet_personal
    else:
        hoja = sheet_personal

    # Extraer datos
    monto, moneda = extraer_monto_y_moneda(msg)
    categoria = clasificar_categoria(msg)
    descripcion = limpiar_descripcion(msg)
    fecha = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    enlace = ""

    # Si incluye foto
    if num_media > 0:
        enlace = subir_foto_drive(request.form.get("MediaUrl0"), categoria, monto or "0", moneda or "€")

    # ==========================================
# 🔗 LINK DEL FORMULARIO PARA SUBIR COMPROBANTE
# ==========================================

GOOGLE_FORM_LINK = "https://docs.google.com/forms/d/1bI7ce81pm6N3Nem5s6zRsnTMlIUfce5M2h2oBVCUTiSQ/viewform?usp=pp_url"

hoja.append_row([fecha, sender, categoria, descripcion, monto, moneda, enlace])

mensaje = (
    "✅ *Gasto registrado*\n"
    f"📅 {fecha}\n"
    f"🏷 {categoria}\n"
    f"💬 {descripcion}\n"
    f"💰 {monto}{moneda}\n\n"
    "📎 *Sube el comprobante aquí:*\n"
    f"{GOOGLE_FORM_LINK}"
)

r.body(mensaje)
return str(resp)

    return str(resp)

# ==========================================
# 🔹 INICIO FLASK
# ==========================================

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
