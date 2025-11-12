from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
import re
import difflib
from datetime import datetime
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
import os
import requests

app = Flask(__name__)

# ==================================================
# 🔹 CONFIGURACIÓN GOOGLE SHEETS
# ==================================================
scope = ["https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"]
import os
from google.oauth2 import service_account

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
sheet = client.open("GASTOS_AUTOMÁTICOS").sheet1

# ==================================================
# 🔹 CONFIGURACIÓN GOOGLE DRIVE
# ==================================================
FOLDER_ID = "1WUdVX2k39tj4pcJE4FIUKeJ0FjgRQdw"  # ✅ cambia por tu carpeta de Drive

drive_service = build('drive', 'v3', credentials=credentials)

def subir_foto_drive(url_imagen, categoria, monto, moneda):
    """Descarga la imagen de Twilio y la sube a Google Drive."""
    try:
        # Descargar la imagen
        response = requests.get(url_imagen)
        if response.status_code != 200:
            return None
        os.makedirs("temp", exist_ok=True)
        nombre_local = f"temp/{datetime.now().strftime('%Y%m%d_%H%M%S')}_{categoria}_{monto}{moneda}.jpg"
        with open(nombre_local, "wb") as f:
            f.write(response.content)

        # Subir a Drive
        file_metadata = {'name': os.path.basename(nombre_local), 'parents': [FOLDER_ID]}
        media = MediaFileUpload(nombre_local, mimetype='image/jpeg')
        file = drive_service.files().create(body=file_metadata, media_body=media, fields='id').execute()

        # Hacer público
        drive_service.permissions().create(
            fileId=file.get('id'),
            body={'role': 'reader', 'type': 'anyone'}
        ).execute()

        enlace = f"https://drive.google.com/file/d/{file.get('id')}/view?usp=sharing"
        os.remove(nombre_local)
        return enlace
    except Exception as e:
        print(f"❌ Error al subir imagen a Drive: {e}")
        return None


# ==================================================
# 🔹 FUNCIONES DE PROCESAMIENTO
# ==================================================
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
        "Supermercado": ["supermercado", "continente", "pingo", "mercado"],
        "Alimentación": ["restaurante", "parrillada", "churrasco", "bufet", "almuerzo", "desayuno", "cena", "merienda", "comida"],
        "Combustible": ["gasolina", "combustible", "gasolinera"],
        "Mantenimiento": ["carro", "repuestos", "revisión", "mantenimiento", "arreglo", "reparación", "vehículo", "oliveira", "césped"],
        "Servicios básicos": ["agua", "luz", "internet", "teléfono", "planes", "gas", "paneles", "meo", "edp"],
        "Salud": ["medicina", "hospital", "clínica", "médico", "doctor", "dentista", "lentes", "terapia", "medicamentos", "salud"],
        "Cuidado personal": ["uñas", "peluquería", "belleza", "depilación", "masajes", "botox", "estética", "pelo", "cabello"],
        "Educación": ["escuela", "libro", "curso", "colegio", "natación", "música"],
        "Diversión": ["discoteca", "salida", "cervezas", "juegos", "diversión", "jumpers"],
        "Impuestos Portugal": ["portugal", "porto", "irs", "finanzas"],
        "Multas": ["multa"],
        "Impuestos Ecuador": ["guisella", "guise", "ecuador"],
        "Transporte": ["peajes", "uber"],
        "Construcción": ["construcción", "remodelación"],
        "Viajes": ["viaje", "avión", "vuelo", "visita"],
        "Vestimenta": ["ropa", "vestido", "zapatos", "gorra", "camisa", "pantalon", "camiseta", "aretes"],
        "Inversiones": ["cripto", "acciones", "trading"],
        "Créditos": ["banco", "crédito"]
    }
    texto_limpio = texto.lower()
    categoria_detectada = "Gastos varios"
    palabras = re.findall(r'\b\w+\b', texto_limpio)
    for palabra in palabras:
        for cat, keywords in categorias.items():
            if difflib.get_close_matches(palabra, keywords, cutoff=0.8):
                return cat
    return categoria_detectada


def limpiar_descripcion(texto):
    descripcion = texto
    descripcion = re.sub(r'(\bEUR\b|\bUSD\b|€|\$)\s*[0-9]+(?:[.,][0-9]{1,2})?', '', descripcion, flags=re.IGNORECASE)
    descripcion = re.sub(r'[0-9]+(?:[.,][0-9]{1,2})?\s*(€|\$|\bEUR\b|\bUSD\b)', '', descripcion, flags=re.IGNORECASE)
    descripcion = re.sub(r'\b\d{1,2}:\d{2}\b', '', descripcion)
    descripcion = re.sub(r'\b\d{4}-\d{2}-\d{2}\b', '', descripcion)
    descripcion = re.sub(r'\b\d{1,2}/\d{1,2}/\d{2,4}\b', '', descripcion)
    descripcion = re.sub(r'\b(editado|reenviado)\b', '', descripcion, flags=re.IGNORECASE)
    return re.sub(r'\s+', ' ', descripcion).strip().capitalize()


# ==================================================
# 🔹 ENDPOINT TWILIO MEJORADO
# ==================================================
@app.route("/whatsapp", methods=["POST"])
def whatsapp_webhook():
    msg = request.form.get("Body", "").strip()
    sender = request.form.get("From", "").replace("whatsapp:", "")
    num_media = int(request.form.get("NumMedia", 0))
    resp = MessagingResponse()
    r = resp.message()

    # Si es texto
    if msg:
        monto, moneda = extraer_monto_y_moneda(msg)
        categoria = clasificar_categoria(msg)
        descripcion = limpiar_descripcion(msg)
        fecha = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        enlace_comprobante = ""

        # Si viene imagen
        if num_media > 0:
            media_url = request.form.get("MediaUrl0")
            enlace_comprobante = subir_foto_drive(media_url, categoria, monto or "0", moneda or "€")

        try:
            sheet.append_row([fecha, sender, categoria, descripcion, monto or "0", moneda or "€", enlace_comprobante])
            mensaje_ok = f"✅ Gasto registrado:\n📅 {fecha}\n🏷️ {categoria}\n💬 {descripcion}\n💰 {monto or '0'}{moneda or '€'}"
            if enlace_comprobante:
                mensaje_ok += f"\n📎 [Comprobante]({enlace_comprobante})"
            r.body(mensaje_ok)
        except Exception as e:
            r.body(f"❌ Error al guardar: {e}")
    else:
        r.body("👋 Envía tus gastos así:\n💬 *Supermercado 25€*\n📸 Puedes incluir una foto del comprobante.")

    return str(resp)


# ==================================================
# 🔹 INICIO SERVIDOR
# ==================================================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000) 
