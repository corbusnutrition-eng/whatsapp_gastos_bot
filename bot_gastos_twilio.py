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

ADMINS = ["+593990516017", "+351927903369"]
NUMEROS_BYRON = ["+351961545289", "+351961545268"]

NUMERO_ARRIENDOS = "+593960153241"   # SOLO ESTE PUEDE ENVIAR COMPROBANTES
ADMIN_ARRIENDOS = "+593990516017"    # SOLO ESTE ACTIVA EL MODO A

# Memoria temporal del modo admin
#  P = Personal, S = Sociedad(Alex), A = Arriendos
modo_admin = {}          # { "+59399...": "P" }
modo_arriendos_activo = False  # flag global

# Pestañas dentro del archivo de GASTOS
TAB_PERSONAL = "PERSONAL"
TAB_ALEX = "ALEX"
TAB_BYRON = "BYRON"
ARCHIVO_GS_GASTOS = "GASTOS_AUTOMÁTICOS"

# ID del archivo de INGRESOS (de tu URL)
ID_INGRESOS = "1EiIZfeqTGGqh_Xufh2befFj3ftExu-3_tKxCtzDsORU"

# ==========================================
# 🔹 GOOGLE SHEETS + DRIVE + VISION
# ==========================================

scope = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/cloud-platform",
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

credentials = service_account.Credentials.from_service_account_info(
    credentials_dict, scopes=scope
)

client = gspread.authorize(credentials)
drive_service = build("drive", "v3", credentials=credentials)

# Cliente de Google Vision **USANDO LAS MISMAS CREDENCIALES**
vision_client = vision.ImageAnnotatorClient(credentials=credentials)

# Libros de Google Sheets
archivo_gastos = client.open(ARCHIVO_GS_GASTOS)
sheet_personal = archivo_gastos.worksheet(TAB_PERSONAL)
sheet_alex = archivo_gastos.worksheet(TAB_ALEX)
sheet_byron = archivo_gastos.worksheet(TAB_BYRON)

archivo_ingresos = client.open_by_key(ID_INGRESOS)
sheet_ingresos = archivo_ingresos.sheet1  # Hoja 1

# Carpeta en Drive para imágenes (puedes cambiarla)
FOLDER_DRIVE_COMPROBANTES = os.getenv(
    "FOLDER_DRIVE_COMPROBANTES",
    "1eLSP5656bzNMml3W7hE8uHi97kBX3Pse"  # la que ya tienes
)

# ==========================================
# 🔹 FUNCIONES GASTOS
# ==========================================

def extraer_monto_y_moneda(texto):
    t = texto.lower()

    patrones = [
        (re.compile(r'(?:€)\s*([0-9]+(?:[.,][0-9]{1,2})?)'), "€"),
        (re.compile(r'(?:\$)\s*([0-9]+(?:[.,][0-9]{1,2})?)'), "$"),
        (re.compile(r'([0-9]+(?:[.,][0-9]{1,2})?)\s*€'), "€"),
        (re.compile(r'([0-9]+(?:[.,][0-9]{1,2})?)\s*\$'), "$"),
    ]

    # 1️⃣ Intentar detectar con símbolo € o $
    for rex, moneda in patrones:
        m = rex.search(t)
        if m:
            return m.group(1).replace(",", "."), moneda

    # 2️⃣ Si no tiene símbolo → detectar número aislado y devolver €
    m = re.search(r"\b([0-9]+(?:[.,][0-9]{1,2})?)\b", t)
    if m:
        numero = m.group(1).replace(",", ".")
        return numero, "€"  # por defecto €

    return None, None


def clasificar_categoria(texto):
    texto = texto.lower()
    if "super" in texto:
        return "Supermercado"
    if "gasolina" in texto or "combustible" in texto:
        return "Combustible"
    if "rest" in texto or "comida" in texto or "almuerzo" in texto:
        return "Alimentación"
    return "Gastos varios"


def limpiar_descripcion(texto):
    return texto.strip().capitalize()


# ==========================================
# 🔹 SUBIR FOTO A DRIVE
# ==========================================

def subir_foto_drive(url):
    try:
        r = requests.get(url)
        if r.status_code != 200:
            return None

        os.makedirs("temp", exist_ok=True)
        fname = f"temp/{datetime.now().strftime('%Y%m%d_%H%M%S')}.jpg"
        with open(fname, "wb") as f:
            f.write(r.content)

        file_metadata = {
            "name": os.path.basename(fname),
            "parents": [FOLDER_DRIVE_COMPROBANTES],
        }
        media = MediaFileUpload(fname, mimetype="image/jpeg")
        file = (
            drive_service.files()
            .create(body=file_metadata, media_body=media, fields="id")
            .execute()
        )

        # Hacer público
        drive_service.permissions().create(
            fileId=file["id"], body={"role": "reader", "type": "anyone"}
        ).execute()

        link = f"https://drive.google.com/file/d/{file['id']}/view?usp=sharing"
        os.remove(fname)
        return link
    except Exception as e:
        print("Error al subir a Drive:", e)
        return None


# ==========================================
# 🔹 OCR CON GOOGLE VISION
# ==========================================

def leer_texto_ocr(url_imagen):
    """Descarga la imagen desde WhatsApp y obtiene el texto con Google Vision."""
    try:
        resp = requests.get(url_imagen)
        if resp.status_code != 200:
            return ""

        image = vision.Image(content=resp.content)
        result = vision_client.text_detection(image=image)
        if result.error.message:
            print("Error OCR:", result.error.message)
            return ""

        if not result.text_annotations:
            return ""

        return result.text_annotations[0].description  # texto completo
    except Exception as e:
        print("Error en leer_texto_ocr:", e)
        return ""


def extraer_monto_desde_texto(texto):
    monto, _ = extraer_monto_y_moneda(texto)
    return monto or ""


def extraer_numero_comprobante(texto):
    # Busca un número largo (6+ dígitos)
    m = re.search(r"\b(\d{6,})\b", texto.replace(" ", ""))
    return m.group(1) if m else ""


# ==========================================
# 🔹 WEBHOOK PRINCIPAL
# ==========================================

@app.route("/webhook", methods=["POST"])
def webhook():
    global modo_arriendos_activo

    msg = request.form.get("Body", "").strip()
    sender = request.form.get("From", "").replace("whatsapp:", "")
    num_media = int(request.form.get("NumMedia", 0))
    media_url = request.form.get("MediaUrl0")
    resp = MessagingResponse()
    r = resp.message()

    # ------------------------------------------
    # 1️⃣ ADMIN CAMBIA MODO (P / S / A)
    # ------------------------------------------
    if sender in ADMINS and msg.upper() in ["P", "S", "A"]:
        letra = msg.upper()

        if sender == ADMIN_ARRIENDOS and letra == "A":
            modo_arriendos_activo = True
            modo_admin[sender] = "A"
            r.body(
                "✔ Modo *ARRIENDOS* activado.\n"
                "Solo el número +593960153241 puede enviar comprobantes.\n"
                "Envía *P* (Personal) o *S* (Sociedad/Alex) para volver al modo gastos."
            )
            return str(resp)

        # Si envía P o S se apaga modo arriendos
        if letra in ["P", "S"]:
            modo_arriendos_activo = False
            modo_admin[sender] = letra
            destino = "PERSONAL" if letra == "P" else "ALEX"
            r.body(f"✔ Modo cambiado a: *{destino}* (gastos)")
            return str(resp)

    # ------------------------------------------
    # 2️⃣ MODO ARRIENDOS (solo si está activo + número autorizado)
    # ------------------------------------------
    if modo_arriendos_activo and sender == NUMERO_ARRIENDOS and num_media > 0:
        fecha = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        nombre = msg or ""  # aquí puedes escribir "Juan depto 3" cuando envías la foto

        # Subir imagen a Drive
        link_imagen = subir_foto_drive(media_url)

        # Leer texto con OCR
        texto_ocr = leer_texto_ocr(media_url)
        monto = extraer_monto_desde_texto(texto_ocr)
        num_comprobante = extraer_numero_comprobante(texto_ocr)

        # Registrar en hoja de INGRESOS_ARRIENDOS
        # Columnas: FECHA | NOMBRE | NUMERO DE COMPROBANTE O DOCUMENTO | MONTO O VALOR | LINK_IMAGEN
        sheet_ingresos.append_row(
            [fecha, nombre, num_comprobante, monto, link_imagen]
        )

        r.body(
            "✅ *Ingreso de arriendo registrado*\n"
            f"📅 Fecha: {fecha}\n"
            f"👤 Nombre: {nombre}\n"
            f"📄 Comprobante: {num_comprobante}\n"
            f"💰 Monto: {monto}\n"
            f"🔗 Imagen: {link_imagen}"
        )
        return str(resp)

    # ------------------------------------------
    # 3️⃣ LÓGICA NORMAL DE GASTOS (como antes)
    # ------------------------------------------

    # Determinar hoja destino de GASTOS
    if sender in NUMEROS_BYRON:
        hoja = sheet_byron
    elif sender in ADMINS:
        modo = modo_admin.get(sender, "P")
        hoja = sheet_personal if modo == "P" else sheet_alex
    else:
        hoja = sheet_byron  # por seguridad

    # Extraer datos de gasto
    monto, moneda = extraer_monto_y_moneda(msg)
    categoria = clasificar_categoria(msg)
    descripcion = limpiar_descripcion(msg)
    fecha = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    link = ""

    if num_media > 0 and media_url:
        link = subir_foto_drive(media_url)

    hoja.append_row([fecha, sender, categoria, descripcion, monto, moneda, link])

    r.body(
        "✅ Gasto registrado\n"
        f"📅 {fecha}\n"
        f"🏷️ {categoria}\n"
        f"💬 {descripcion}\n"
        f"💰 {monto}{moneda}"
    )
    return str(resp)


# ==========================================
# 🔹 INICIO LOCAL
# ==========================================

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
