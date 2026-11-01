"""Monitor externo de un bot de WhatsApp Cloud API («dead man's switch»).

Lo lanza Google Cloud Scheduler cada 5 minutos (y el cron de GitHub de respaldo). Pregunta a Meta (solo
lectura) a qué dirección manda los mensajes de WhatsApp, comprueba que esa dirección contesta en /salud y avisa
solo cuando cambia el estado: 🔴 si falla y lo confirma 2 minutos después, 🟢 al recuperarse y recordatorios a
la 1 h, a las 3 h, cada 6 h el resto del primer día y después uno al día (en las horas de silencio, por defecto
de 00:00 a 08:00 hora de Madrid, llegan por correo sin sonar en el móvil).

    python monitor.py --estado estado.json

Variables de entorno (secretos de GitHub Actions): WHATSAPP_APP_ID, WHATSAPP_APP_SECRET, SMTP_SERVIDOR,
SMTP_PUERTO, SMTP_USUARIO, SMTP_CLAVE, AVISOS_EMAIL. Opcionales: NOMBRE_BOT y COMO_ARRANCAR (secretos, para
personalizar los correos sin dejarlo en el código), NTFY_TEMA (secreto: además del correo, aviso push al móvil
con la app ntfy, con un botón «Ya lo sé»), y las variables del repositorio PAUSA_HASTA, HORAS_SILENCIO y
CONFIRMAR (segundos hasta confirmar un fallo, 120 por defecto). Con NTFY_TEMA, el bot puede dejar eventos en el
tema privado «<NTFY_TEMA>-ctl»: apagado intencionado (no avisar), arranque (con la causa de la caída) y caída ya
avisada por el propio bot (no repetir el aviso). El workflow pasa ULTIMA_EJECUCION (fin de la ejecución
anterior) para detectar si el monitor estuvo parado.

Sin dependencias externas y sin acceso a ningún token de GitHub. Nunca imprime secretos ni la dirección del bot,
y estado.json (público) no guarda ningún texto que mande el bot.
Sale con código 1 si no puede hacer su trabajo (falta configuración o el correo no sale): GitHub avisa entonces
al dueño del repositorio de que la ejecución ha fallado.
"""
import argparse
import http.client
import json
import os
import re
import smtplib
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import format_datetime, formataddr, make_msgid
from html import escape

VERSION_DE_LA_API = "v26.0"  # la misma que usa el bot
FALLOS_PARA_AVISAR = 2
RECORDATORIOS = tuple(timedelta(hours=h) for h in (1, 3, 9, 15, 21))  # el primer día; después, uno al día
CADA_RECORDATORIO = timedelta(hours=24)
APAGADO_RECORDATORIO = timedelta(hours=24)  # apagado avisado: un correo al día, sin push
AVISO_DEL_BOT = timedelta(minutes=20)  # si el bot avisó de su caída hace menos, el monitor no lo repite
SIN_COMPROBAR = timedelta(minutes=30)  # Meta sin contestar al monitor tanto tiempo: avisa de que no puede comprobar
HUECO = timedelta(minutes=30)  # tanto tiempo sin ejecuciones: el monitor estuvo parado
RELOJ_DEL_BOT = timedelta(minutes=5)  # margen para la hora que pone el bot en sus eventos
SILENCIO = (0, 8)  # horas de Madrid en las que los recordatorios y los verdes no suenan en el móvil
CONTROL_MAXIMO = 300  # caracteres de texto que se aceptan de un evento del bot
INTENTOS, ESPERA_ENTRE_INTENTOS, TIEMPO_MAXIMO = 3, 20, 10
PRIORIDAD_PUSH = 4  # alta: suena y se ve; la misma para todas, para que el móvil las ordene por hora
SECRETOS = ("WHATSAPP_APP_ID", "WHATSAPP_APP_SECRET", "SMTP_SERVIDOR", "SMTP_USUARIO", "SMTP_CLAVE", "AVISOS_EMAIL")
NOMBRE_POR_DEFECTO, ARRANQUE_POR_DEFECTO = "Bot de mensajería", "el script de arranque del bot"
TIPOS_DE_EVENTO = ("pausa", "arranque", "caida_avisada")
CAMPOS_PRIVADOS = ("contexto",)  # texto libre del bot: vive solo en memoria, nunca en estado.json (es público)
# Servicios de túneles con dominio compartido: nombrarlos no dice de quién es el bot. Un dominio propio, sí.
TUNELES_COMPARTIDOS = ("trycloudflare.com", "ngrok-free.app", "ngrok-free.dev", "ngrok.app", "ngrok.io", "loca.lt",
                       "localhost.run", "lhr.life", "serveo.net", "pinggy.link", "devtunnels.ms")
REPOSITORIO = r"[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}"
ERRORES_DE_RED = (urllib.error.URLError, http.client.HTTPException, ValueError, TimeoutError, OSError)

URGENCIAS = {"alta": ("#c0392b", "URGENTE", "🔴"), "ok": ("#1e8449", "RESUELTO", "🟢"), "info": ("#2471a3", "AVISO", "🔵")}
DIAS = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")
MESES = ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre",
         "noviembre", "diciembre")


def causas(arranque: str) -> dict:
    """Por causa: (qué pasa, qué hacer, qué pasa en corto, qué hacer en corto). Lo corto va en el push del móvil.

    `arranque` es cómo se enciende el bot (secreto COMO_ARRANCAR)."""
    return {
        "meta": ("Meta no accesible: el monitor no ha podido preguntar a Meta a qué dirección manda los mensajes",
                 "Suele ser un fallo pasajero de Meta o de GitHub. Si se repite, comprueba en developers.facebook.com "
                 "que la app sigue activa. Si cambiaste la clave secreta de la app, actualiza el secreto "
                 "WHATSAPP_APP_SECRET de este repositorio.",
                 "Meta no responde al monitor.", "Si se repite, revisa la app en developers.facebook.com."),
        "sin_suscripcion": ("Meta no tiene ninguna dirección activa para los mensajes de WhatsApp",
                            f"Abre {arranque} en el ordenador del bot: al arrancar vuelve a darle a Meta su dirección.",
                            "Meta no tiene la dirección del bot.", f"Abre {arranque}."),
        "no_responde": ("Meta apunta a una dirección que no responde: ordenador apagado o suspendido, ventana del bot "
                        "cerrada, sin internet o túnel caído",
                        f"Enciende el ordenador del bot (y que no se suspenda) y abre {arranque}.",
                        "Ordenador apagado, bot cerrado o sin internet.", f"Enciende el ordenador y abre {arranque}."),
        "sin_worker": ("La dirección responde, pero la parte que contesta a los clientes está parada",
                       f"Cierra la ventana del bot y vuelve a abrir {arranque}. Si se repite, revisa los registros del bot.",
                       "El bot está abierto pero no contesta.", f"Cierra la ventana del bot y vuelve a abrir {arranque}."),
    }


def _decir(texto: str) -> None:
    """Mensaje para el registro de la ejecución. Si la consola no puede escribirlo, se pierde el mensaje, nunca
    el trabajo: un fallo aquí no debe impedir guardar el estado (se repetirían los correos)."""
    try:
        print(texto, flush=True)
    except (OSError, ValueError, UnicodeError):
        pass


def _objeto(datos) -> dict:
    """Un JSON que debería ser un objeto: cualquier otra cosa (null, lista, número) cuenta como objeto vacío."""
    return datos if isinstance(datos, dict) else {}


# -- Hora de Madrid sin dependencias (las reglas de horario de verano de la UE) ---------------------------------

def _ultimo_domingo(anio: int, mes: int) -> datetime:
    dia = datetime(anio, mes, 31, 1, tzinfo=timezone.utc)
    return dia - timedelta(days=(dia.weekday() + 1) % 7)


def madrid(momento: datetime) -> datetime:
    momento = momento.astimezone(timezone.utc)
    verano = _ultimo_domingo(momento.year, 3) <= momento < _ultimo_domingo(momento.year, 10)
    return momento.astimezone(timezone(timedelta(hours=2 if verano else 1)))


def desde_madrid(local: datetime) -> datetime:
    """Una hora de Madrid sin zona horaria, en UTC."""
    desfase = madrid(local.replace(tzinfo=timezone.utc)).utcoffset()
    return (local.replace(tzinfo=None) - desfase).replace(tzinfo=timezone.utc)


def hora(momento: datetime) -> str:
    return madrid(momento).strftime("%d-%m-%Y %H:%M")


def cuando(momento: datetime, ahora: datetime) -> str:
    """«hoy a las 12:15», «ayer a las 12:15», «el lunes 19 a las 12:15» o, a una semana o más, «el lunes 19 de
    octubre a las 12:15» (hora de Madrid)."""
    local, hoy = madrid(momento), madrid(ahora).date()
    dias = (hoy - local.date()).days
    if dias == 0:
        dia = "hoy"
    elif dias == 1:
        dia = "ayer"
    elif 1 < dias < 7:
        dia = f"el {DIAS[local.weekday()]} {local.day}"
    else:
        dia = f"el {DIAS[local.weekday()]} {local.day} de {MESES[local.month - 1]}"
    return f"{dia} a las {local:%H:%M}"


def en_silencio(ahora: datetime, horas: tuple = SILENCIO) -> bool:
    inicio, fin = horas
    return inicio <= madrid(ahora).hour < fin if inicio <= fin else not fin <= madrid(ahora).hour < inicio


def leer_silencio(texto: str) -> tuple:
    """HORAS_SILENCIO: «0-8» por defecto (también si está vacía, que es lo que pasa GitHub si no existe), «23-7»
    cruza la medianoche y «0-0» lo desactiva."""
    texto = (texto or "").strip()
    if not texto:
        return SILENCIO
    encontrado = re.fullmatch(r"(\d{1,2})\s*-\s*(\d{1,2})", texto)
    if encontrado and all(0 <= int(x) <= 24 for x in encontrado.groups()):
        return int(encontrado[1]), int(encontrado[2])
    _decir(f"HORAS_SILENCIO no se entiende («{texto[:20]}»): se usa 0-8. Formato: 0-8, 23-7 o 0-0 para desactivarlo.")
    return SILENCIO


def horas_de_silencio(silencio: tuple) -> str:
    """«de 00:00 a 08:00», o «» si están desactivadas."""
    inicio, fin = silencio
    return "" if inicio == fin else f"de {inicio:02d}:00 a {fin:02d}:00"


def duracion(tramo: timedelta) -> str:
    minutos = max(0, int(tramo.total_seconds() // 60))
    dias, minutos = divmod(minutos, 1440)
    horas, minutos = divmod(minutos, 60)
    partes = ([f"{dias} d"] if dias else []) + ([f"{horas} h"] if horas else []) + ([f"{minutos} min"] if minutos else [])
    return " ".join(partes) or "0 min"


def leer_pausa(texto: str, ahora: datetime) -> datetime | None:
    """PAUSA_HASTA en hora de Madrid: «2026-10-20 08:00» o «2026-10-20» (hasta el final de ese día)."""
    texto = (texto or "").strip()
    if not texto:
        return None
    for formato, extra in (("%Y-%m-%d %H:%M", timedelta()), ("%Y-%m-%dT%H:%M", timedelta()), ("%Y-%m-%d", timedelta(days=1))):
        try:
            local = datetime.strptime(texto, formato) + extra
            break
        except ValueError:
            continue
    else:
        _decir(f"PAUSA_HASTA no se entiende («{texto[:40]}»): se ignora. Formato: 2026-10-20 08:00")
        return None
    return desde_madrid(local)


# -- Red ----------------------------------------------------------------------------------------------------

class SinRedirecciones(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise urllib.error.URLError("redirección rechazada")


def abridor():
    return urllib.request.build_opener(SinRedirecciones(), urllib.request.HTTPSHandler(context=ssl.create_default_context())).open


def enmascarar(url: str) -> str:
    """«***.trycloudflare.com» si es un servicio de túneles compartido; si no, «*** (dominio propio)»: los
    registros de un repositorio público los puede leer cualquiera y un dominio propio diría de quién es el bot."""
    try:
        nombre = (urllib.parse.urlsplit(url or "").hostname or "").lower()
    except ValueError:
        nombre = ""
    if "." not in nombre:
        return "(sin dirección)"
    for comun in TUNELES_COMPARTIDOS:
        if nombre.endswith("." + comun):
            return "***." + comun
    return "*** (dominio propio)"


def _tipo(error: Exception) -> str:
    return f"HTTP {error.code}" if isinstance(error, urllib.error.HTTPError) else type(error).__name__


def _pide_mensajes(suscripcion: dict) -> bool:
    campos = suscripcion.get("fields")
    return any(campo == "messages" or _objeto(campo).get("name") == "messages"
               for campo in (campos if isinstance(campos, list) else []))


def direccion_de_meta(app_id: str, secreto: str, abrir) -> tuple[str, str, str]:
    """(callback_url, causa, detalle). Solo lectura de la suscripción de la app.

    Solo vale la suscripción de WhatsApp: Messenger e Instagram también tienen un campo «messages» y su
    dirección no es la del bot. Si Meta no dice de qué producto es, se acepta como último recurso."""
    consulta = (f"https://graph.facebook.com/{VERSION_DE_LA_API}/{urllib.parse.quote(app_id)}/subscriptions?"
                + urllib.parse.urlencode({"access_token": f"{app_id}|{secreto}"}))
    try:
        with abrir(urllib.request.Request(consulta), timeout=20) as respuesta:
            datos = _objeto(json.loads(respuesta.read(200000))).get("data")
    except ERRORES_DE_RED as error:
        return "", "meta", _tipo(error)  # el tipo basta: el mensaje podría llevar la consulta con la credencial
    validas = [s for s in map(_objeto, datos if isinstance(datos, list) else [])
               if s.get("active") and str(s.get("callback_url") or "").startswith("https://") and _pide_mensajes(s)]
    for producto in ("whatsapp_business_account", None):
        for suscripcion in validas:
            if suscripcion.get("object") == producto:
                return str(suscripcion["callback_url"]), "", ""
    return "", "sin_suscripcion", "ninguna suscripción de WhatsApp activa con el campo messages"


def url_de_salud(callback: str) -> str:
    partes = urllib.parse.urlsplit(callback)
    ruta = partes.path[: -len("/whatsapp")] if partes.path.endswith("/whatsapp") else partes.path.rstrip("/")
    return urllib.parse.urlunsplit((partes.scheme, partes.netloc, ruta + "/salud", "", ""))


def comprobar_salud(url: str, abrir, dormir=time.sleep) -> tuple[str, str]:
    """('ok' | 'sin_worker' | 'no_responde', detalle). Tres intentos espaciados antes de dar un fallo."""
    resultado = ("no_responde", "")
    for intento in range(INTENTOS):
        if intento:
            dormir(ESPERA_ENTRE_INTENTOS)
        try:
            with abrir(urllib.request.Request(url, headers={"User-Agent": "bot-sync-monitor"}), timeout=TIEMPO_MAXIMO) as respuesta:
                cuerpo = _objeto(json.loads(respuesta.read(2000) or b"{}"))
                if respuesta.status == 200 and cuerpo.get("estado") == "ok":
                    return "ok", ""
                resultado = ("no_responde", f"HTTP {respuesta.status}")
        except urllib.error.HTTPError as error:
            try:
                estado = _objeto(json.loads(error.read(2000) or b"{}")).get("estado")
            except (ValueError, OSError, http.client.HTTPException):
                estado = ""
            resultado = ("sin_worker", "HTTP 503 «sin worker»") if estado == "sin worker" else ("no_responde", _tipo(error))
        except ERRORES_DE_RED as error:
            resultado = ("no_responde", _tipo(error))
    return resultado


def comprobar(entorno: dict, abrir, dormir=time.sleep) -> dict:
    """Una comprobación completa: {'ok': bool, 'causa': str, 'detalle': str, 'dominio': str}."""
    callback, causa, detalle = direccion_de_meta(entorno["WHATSAPP_APP_ID"], entorno["WHATSAPP_APP_SECRET"], abrir)
    if not callback:
        return {"ok": False, "causa": causa, "detalle": detalle, "dominio": ""}
    dominio = enmascarar(callback)
    estado, detalle = comprobar_salud(url_de_salud(callback), abrir, dormir)
    return {"ok": estado == "ok", "causa": "" if estado == "ok" else estado, "detalle": detalle, "dominio": dominio}


def ejecucion_anterior(respuesta, actual: str = "") -> str:
    """Fin de la ejecución anterior del workflow («2026-10-24T00:05:50Z») a partir de la respuesta de la API de
    GitHub «…/actions/workflows/<archivo>/runs?status=completed», o «» si no la hay. Las canceladas no cuentan:
    no llegaron a comprobar nada."""
    ejecuciones = _objeto(respuesta).get("workflow_runs")
    for ejecucion in map(_objeto, ejecuciones if isinstance(ejecuciones, list) else []):
        if str(ejecucion.get("id")) != str(actual) and ejecucion.get("conclusion") in ("success", "failure"):
            fin = str(ejecucion.get("updated_at") or "")
            return fin if re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", fin) else ""
    return ""


# -- Estado y decisión --------------------------------------------------------------------------------------

def _fecha(texto) -> datetime | None:
    """Fecha ISO 8601. Sin zona horaria se entiende hora de Madrid. Acepta «Z» también con Python 3.10."""
    if not texto or not isinstance(texto, str):
        return None
    texto = texto.strip()
    if texto[-1:] in ("Z", "z"):
        texto = texto[:-1] + "+00:00"
    try:
        momento = datetime.fromisoformat(texto)
    except ValueError:
        return None
    return desde_madrid(momento) if momento.tzinfo is None else momento


def _iso(momento: datetime) -> str:
    return momento.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def estado_inicial() -> dict:
    return {"estado": "desconocido", "desde": "", "fallos": 0, "primer_fallo": "", "causa": "", "ultimo_correo": "",
            "recordatorios": 0, "pausa_bot": "", "aviso_bot": "", "contexto": "", "ntfy_ultimo": "",
            "sin_comprobar": "", "aviso_sin_comprobar": ""}


def momento_del_evento(cuando_texto, recibido: datetime | None) -> datetime | None:
    """La hora que pone el bot (sin zona horaria, hora de Madrid). Si falta, no se entiende o va adelantada
    respecto a cuándo lo recibió ntfy, vale la de ntfy: el reloj del servidor no depende del ordenador del bot."""
    momento = _fecha(str(cuando_texto)[:40]) if cuando_texto else None
    if recibido and (not momento or momento > recibido + RELOJ_DEL_BOT):
        return recibido
    return momento


def aplicar_eventos(estado: dict, eventos: list) -> dict:
    """Eventos del bot en el orden en que llegaron: pausa (apagado intencionado o «Ya lo sé»), arranque (con la
    causa de la caída) y caida_avisada (el bot ya avisó de su propia caída). Solo cambia campos de control."""
    nuevo = dict(estado)
    for evento in eventos:
        tipo, momento = evento.get("tipo"), _fecha(evento.get("cuando"))
        if not momento:
            continue
        if tipo == "pausa":
            # «Ya lo sé» de un aviso de caída que ya terminó: no debe silenciar la siguiente.
            if evento.get("origen") == "boton" and nuevo.get("estado") == "arriba":
                continue
            nuevo["pausa_bot"] = _iso(momento)
        elif tipo == "arranque":
            nuevo["pausa_bot"] = ""
            nuevo["contexto"] = " ".join(str(evento.get("causa") or "").split())[:CONTROL_MAXIMO]
        elif tipo == "caida_avisada":
            nuevo["aviso_bot"] = _iso(momento)
    return nuevo


def recordatorios_debidos(tiempo: timedelta) -> int:
    """Cuántos recordatorios corresponden a una caída de esta duración: a la 1 h, a las 3 h, cada 6 h el resto del
    primer día y después uno al día."""
    debidos = sum(1 for limite in RECORDATORIOS if tiempo >= limite)
    if tiempo >= RECORDATORIOS[-1] + CADA_RECORDATORIO:
        debidos += int((tiempo - RECORDATORIOS[-1]) / CADA_RECORDATORIO)
    return debidos


def decidir(anterior: dict, resultado: dict | None, ahora: datetime, ultima: datetime | None = None) -> tuple[dict, dict | None]:
    """Nuevo estado y, si toca, el aviso {'tipo': 'caida'|'recordatorio'|'vuelta'|'apagado'|'sin_comprobar'|
    'comprobando', ...}.

    `resultado` None = en pausa por PAUSA_HASTA. `ultima` = fin de la ejecución anterior, para saber si el monitor
    estuvo parado. Estados: arriba, abajo, apagado (el propietario avisó de que lo sabe), pausa y desconocido."""
    nuevo = {**estado_inicial(), **anterior}
    correo = None
    antes = anterior.get("estado")
    hueco = ultima if ultima and ahora - ultima > HUECO else None
    estaba_ciego = bool(anterior.get("aviso_sin_comprobar"))
    if resultado is None or resultado["causa"] != "meta":
        nuevo.update(sin_comprobar="", aviso_sin_comprobar="")
    if resultado is None:
        nuevo.update(estado="pausa", fallos=0, primer_fallo="", causa="", recordatorios=0)
        if antes != "pausa":
            nuevo["desde"] = _iso(ahora)
    elif resultado["causa"] == "meta":
        # Sin respuesta de Meta no se sabe nada del bot: ni caída ni recuperación. Si dura, se avisa aparte.
        inicio = _fecha(anterior.get("sin_comprobar")) or ahora
        nuevo["sin_comprobar"] = _iso(inicio)
        if antes != "abajo":
            nuevo.update(fallos=0, primer_fallo="")
        if ahora - inicio >= SIN_COMPROBAR and not estaba_ciego:
            correo = {"tipo": "sin_comprobar", "desde": inicio, "duracion": ahora - inicio, **resultado}
            nuevo["aviso_sin_comprobar"] = _iso(ahora)
    elif resultado["ok"]:
        if antes in ("abajo", "apagado"):
            desde = _fecha(anterior.get("desde")) or ahora
            correo = {"tipo": "vuelta", "desde": desde, "duracion": ahora - desde, "intencionado": antes == "apagado",
                      "contexto": nuevo.get("contexto", ""), "hueco": hueco if hueco and hueco > desde else None,
                      **resultado}
        elif estaba_ciego:
            inicio = _fecha(anterior.get("sin_comprobar")) or ahora
            correo = {"tipo": "comprobando", "desde": inicio, "duracion": ahora - inicio, **resultado}
        if antes != "arriba":
            nuevo["desde"] = _iso(ahora)
        # Una pausa o un aviso del bot de hace un momento aún no se ven fuera (el túnel tarda unos segundos en
        # caer): se guardan para la próxima comprobación. Los antiguos se olvidan, para no silenciar otra caída.
        reciente = {campo: valor for campo in ("pausa_bot", "aviso_bot")
                    if (valor := nuevo.get(campo)) and antes == "arriba" and ahora - (_fecha(valor) or ahora) < AVISO_DEL_BOT}
        nuevo.update({"estado": "arriba", "fallos": 0, "primer_fallo": "", "causa": "", "recordatorios": 0,
                      "pausa_bot": "", "aviso_bot": "", "contexto": "", **reciente})
    elif nuevo.get("pausa_bot"):
        # El propietario avisó de que lo sabe: sin avisos urgentes; un correo al día para que no se olvide.
        pausa = _fecha(nuevo["pausa_bot"]) or ahora
        if antes != "apagado":
            inicio = (_fecha(anterior.get("desde")) if antes == "abajo" else None) or pausa
            nuevo.update(estado="apagado", desde=_iso(inicio), ultimo_correo=_iso(ahora), recordatorios=0)
        elif ahora - (_fecha(anterior.get("ultimo_correo")) or pausa) >= APAGADO_RECORDATORIO:
            desde = _fecha(anterior.get("desde")) or pausa
            correo = {"tipo": "apagado", "desde": desde, "duracion": ahora - desde, **resultado}
            nuevo["ultimo_correo"] = _iso(ahora)
        # Sin fallos pendientes: si el bot vuelve a arrancar y aún no responde, el fallo se confirma como cualquiera.
        nuevo.update(fallos=0, primer_fallo="", causa=resultado["causa"])
    elif antes == "abajo":
        desde = _fecha(anterior.get("desde")) or ahora
        debidos = recordatorios_debidos(ahora - desde)
        if debidos > int(anterior.get("recordatorios") or 0):
            correo = {"tipo": "recordatorio", "desde": desde, "duracion": ahora - desde, "hueco": hueco, **resultado}
            nuevo.update(recordatorios=debidos, ultimo_correo=_iso(ahora))
        nuevo["causa"] = resultado["causa"]
    else:
        fallos = min(int(anterior.get("fallos") or 0) + 1, FALLOS_PARA_AVISAR)
        primero = (_fecha(anterior.get("primer_fallo")) if fallos > 1 else None) or ahora
        nuevo.update(fallos=fallos, primer_fallo=_iso(primero), causa=resultado["causa"])
        if fallos >= FALLOS_PARA_AVISAR:
            nuevo.update(estado="abajo", desde=_iso(primero), ultimo_correo=_iso(ahora), recordatorios=0)
            aviso = _fecha(nuevo.get("aviso_bot"))
            if aviso and primero - AVISO_DEL_BOT <= aviso <= ahora:
                _decir("El bot ya avisó de su caída: no se repite el aviso.")
            else:
                correo = {"tipo": "caida", "desde": primero, "duracion": ahora - primero, **resultado}
    return nuevo, correo


# -- Correo «de un vistazo»: banda de color, tabla de datos y recuadro de qué hacer ---------------------------

def correo_html(urgencia: str, titulo: str, filas: list, que_hacer: str, pie: str = "", marca: str = NOMBRE_POR_DEFECTO) -> tuple[str, str]:
    color, etiqueta, emoji = URGENCIAS[urgencia]
    texto, celdas = [f"{emoji} {etiqueta} · {titulo}", ""], []
    for nombre, valor in filas:
        if valor in ("", None):
            continue
        texto.append(f"{nombre}: {valor}")
        celdas.append(f'<tr><td style="padding:6px 12px 6px 0;color:#666;white-space:nowrap;vertical-align:top">{escape(nombre)}</td>'
                      f'<td style="padding:6px 0;color:#111">{escape(str(valor))}</td></tr>')
    texto += ["", "QUÉ TIENES QUE HACER: " + que_hacer] + (["", pie] if pie else [])
    cuerpo = f"""<!doctype html><html><body style="margin:0;padding:0;background:#eef0f2">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#eef0f2;padding:16px 0"><tr><td align="center">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;background:#fff;border-radius:8px;overflow:hidden;font-family:Arial,Helvetica,sans-serif;font-size:15px;line-height:1.45">
<tr><td style="background:{color};color:#fff;padding:14px 20px">
<div style="font-size:12px;letter-spacing:1px;font-weight:bold">{emoji} {escape(etiqueta)} · {escape(marca.upper())}</div>
<div style="font-size:20px;font-weight:bold;margin-top:4px">{escape(titulo)}</div></td></tr>
<tr><td style="padding:16px 20px"><table role="presentation" cellpadding="0" cellspacing="0" width="100%">{''.join(celdas)}</table></td></tr>
<tr><td style="padding:0 20px 16px"><div style="background:#fff8e1;border:1px solid #f0d58a;border-radius:6px;padding:12px 14px">
<div style="font-size:12px;font-weight:bold;color:#8a6d00;letter-spacing:.5px">QUÉ TIENES QUE HACER</div>
<div style="margin-top:4px;color:#111">{escape(que_hacer)}</div></div></td></tr>
{f'<tr><td style="padding:0 20px 18px;color:#777;font-size:12px">{escape(pie)}</td></tr>' if pie else ''}
</table></td></tr></table></body></html>"""
    return "\n".join(texto), cuerpo


PIE = ("Aviso del monitor externo (GitHub Actions). Comprueba el bot cada 5 minutos y confirma cada fallo 2 "
       "minutos después: los avisos llegan unos 3-8 minutos después del cambio. Si vas a apagarlo a propósito, "
       "detenlo de la forma que avisa al monitor (o pausa el monitor) y no recibirás avisos de caída.")
PIE_BOTON = " Si ya lo sabes, pulsa «Ya lo sé» en el aviso del móvil: solo recibirás un correo al día."


def siguiente(silencio: tuple = SILENCIO) -> str:
    """Cuándo llega el siguiente aviso de una caída, con las horas de silencio que haya de verdad."""
    horas = horas_de_silencio(silencio)
    return ("Al recuperarse (🟢). Si sigue caído: recordatorios a la 1 h y a las 3 h, cada 6 h el resto del primer "
            "día y después uno al día" + (f" ({horas} llegan por correo sin sonar en el móvil)." if horas else "."))


def redactar(correo: dict, ahora: datetime, entorno: dict | None = None, silencio: tuple = SILENCIO) -> dict:
    """Correo y push de un aviso: {'asunto', 'texto', 'html', 'push_titulo', 'push_texto', 'push_boton'}."""
    entorno = entorno or {}
    marca = (entorno.get("NOMBRE_BOT") or "").strip() or NOMBRE_POR_DEFECTO
    arranque = (entorno.get("COMO_ARRANCAR") or "").strip() or ARRANQUE_POR_DEFECTO
    pie = PIE + (PIE_BOTON if (entorno.get("NTFY_TEMA") or "").strip() else "")
    textos = causas(arranque)
    causa, que_hacer, causa_corta, accion_corta = textos.get(correo.get("causa") or "", ("", "", "", ""))
    detalle = " · ".join(x for x in (correo.get("dominio"), correo.get("detalle")) if x)
    tiempo, desde = duracion(correo["duracion"]), cuando(correo["desde"], ahora)
    hueco = correo.get("hueco")
    parado = (f"Sin comprobaciones desde {cuando(hueco, ahora)} hasta {cuando(ahora, ahora)}: el monitor no se "
              "ejecutó (workflow desactivado, token de Cloud Scheduler caducado o retrasos del cron de GitHub)") if hueco else ""
    tipo = correo["tipo"]
    sin_push = {"push_titulo": "", "push_texto": "", "push_boton": False}
    if tipo == "vuelta":
        contexto = correo.get("contexto") or ""
        palabra = "apagado" if correo.get("intencionado") else "caído"
        if hueco:
            tiempo = f"entre {duracion(hueco - correo['desde'])} y {tiempo}"
        asunto = f"🟢 El bot vuelve a responder · estuvo {palabra} {tiempo}"
        que = ("El bot vuelve a contestar tras el apagado del que avisaste." if correo.get("intencionado")
               else "El bot vuelve a contestar por la dirección que tiene Meta.")
        filas = [("Qué ha pasado", que), ("Sin responder desde", desde),
                 ("Comprobado" if hueco else "Vuelve a responder", cuando(ahora, ahora)),
                 ("Tiempo sin responder", tiempo if hueco else f"unos {tiempo}"), ("Monitor parado", parado),
                 ("Qué pasó en el ordenador", contexto)]
        texto, html = correo_html("ok", "El bot vuelve a responder", filas,
                                  "Nada urgente. Si llegaron mensajes durante la caída, el bot te enviará un resumen "
                                  "de los que contestó y de los que siguen pendientes.", PIE, marca)
        push = (contexto + " " if contexto else "") + f"Estuvo sin responder {tiempo}."
        return {"asunto": asunto, "texto": texto, "html": html, "push_titulo": "🟢 El bot vuelve a responder",
                "push_texto": push, "push_boton": False}
    if tipo == "apagado":
        asunto = f"🔵 El bot sigue apagado · desde {desde}"
        filas = [("Qué pasa", "El bot sigue sin responder, pero avisaste de que lo sabías (lo apagaste a propósito "
                              "o pulsaste «Ya lo sé»). Los clientes que escriban no reciben respuesta hasta que vuelva."),
                 ("Apagado desde", desde), ("Tiempo apagado", tiempo),
                 ("Siguiente aviso", "Cuando vuelva (🟢), o mañana si sigue apagado. Sin avisos en el móvil.")]
        texto, html = correo_html("info", "El bot sigue apagado", filas,
                                  f"Nada si lo dejaste apagado a propósito. Para volver a atender, abre {arranque}.", PIE, marca)
        return {"asunto": asunto, "texto": texto, "html": html, **sin_push}
    if tipo == "sin_comprobar":
        asunto = f"🔵 No puedo comprobar el bot · desde {desde}"
        filas = [("Qué pasa", "El monitor no consigue preguntar a Meta a qué dirección manda los mensajes, así que no "
                              "sabe si el bot responde. No lo cuenta como caída."),
                 ("Sin poder comprobarlo desde", desde), ("Tiempo", tiempo), ("Detalle técnico", detalle),
                 ("Siguiente aviso", "Cuando vuelva a poder comprobarlo. Sin avisos en el móvil.")]
        texto, html = correo_html("info", "No puedo comprobar el bot", filas, textos["meta"][1], PIE, marca)
        return {"asunto": asunto, "texto": texto, "html": html, **sin_push}
    if tipo == "comprobando":
        asunto = "🔵 El monitor vuelve a comprobar el bot · responde"
        filas = [("Qué pasa", "Meta vuelve a contestar al monitor y el bot responde."),
                 ("Sin poder comprobarlo", f"desde {desde}, unos {tiempo}")]
        texto, html = correo_html("info", "El monitor vuelve a comprobar el bot", filas, "Nada.", PIE, marca)
        return {"asunto": asunto, "texto": texto, "html": html, **sin_push}
    recordatorio = tipo == "recordatorio"
    titulo = "El bot sigue sin responder" if recordatorio else "El bot no responde"
    asunto = f"🔴 {titulo} · desde {desde}" + (f" ({tiempo})" if recordatorio else "")
    filas = [("Qué ha pasado", "Los clientes que escriban por WhatsApp no reciben respuesta del bot."),
             ("Sin responder desde", f"{desde} (primera comprobación fallida)"),
             ("Comprobado", cuando(ahora, ahora)), ("Tiempo sin responder", tiempo if recordatorio else ""),
             ("Monitor parado", parado), ("Causa probable", causa), ("Detalle técnico", detalle),
             ("Siguiente aviso", siguiente(silencio))]
    texto, html = correo_html("alta", titulo, filas, que_hacer, pie, marca)
    push = (f"Desde {desde} ({tiempo}). " if recordatorio else "") + f"{causa_corta}\nQué hacer: {accion_corta}"
    return {"asunto": asunto, "texto": texto, "html": html, "push_titulo": f"🔴 {titulo}", "push_texto": push.strip(),
            "push_boton": True}


def suena(tipo: str, intencionado: bool, ahora: datetime, silencio: tuple = SILENCIO) -> bool:
    """¿Va también al móvil? La primera caída siempre; recordatorios y verdes, fuera de las horas de silencio;
    nada de lo que tenga que ver con un apagado avisado ni con que Meta no conteste al monitor."""
    if tipo == "caida":
        return True
    if tipo in ("apagado", "sin_comprobar", "comprobando") or intencionado:
        return False
    return not en_silencio(ahora, silencio)


def _dominio_del_correo(entorno: dict) -> str | None:
    dominio = str(entorno.get("SMTP_USUARIO") or "").rpartition("@")[2].strip().lower()
    return dominio if re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)+", dominio) else None


def hilo_del_incidente(entorno: dict, desde: datetime) -> str:
    """Message-ID del primer aviso de un incidente. Se deduce de cuándo empezó, así que no hay que guardarlo:
    los avisos siguientes lo citan y el programa de correo los agrupa (Gmail, además, exige el mismo asunto)."""
    return f"<incidente.{desde.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}@{_dominio_del_correo(entorno) or 'bot-sync-monitor.invalid'}>"


def mensaje_de_correo(entorno: dict, aviso: dict, ahora: datetime, hilo: str = "", raiz: bool = False) -> EmailMessage:
    """El correo completo, con fecha y Message-ID (sin ellos, más papeletas de acabar en spam)."""
    mensaje = EmailMessage()
    mensaje["Subject"] = aviso["asunto"]
    mensaje["From"] = formataddr(("Monitor del bot", entorno["SMTP_USUARIO"]))
    mensaje["To"] = entorno["AVISOS_EMAIL"]
    mensaje["Date"] = format_datetime(madrid(ahora))
    mensaje["Message-ID"] = hilo if hilo and raiz else make_msgid(domain=_dominio_del_correo(entorno) or "bot-sync-monitor.invalid")
    if hilo and not raiz:
        mensaje["In-Reply-To"] = mensaje["References"] = hilo
    mensaje.set_content(aviso["texto"])
    mensaje.add_alternative(aviso["html"], subtype="html")
    return mensaje


def enviar(entorno: dict, mensaje: EmailMessage, smtp=None) -> None:
    """SSL directo en el puerto 465; en cualquier otro (587, 25…), STARTTLS obligatorio: la clave nunca viaja
    sin cifrar. `smtp` sustituye a smtplib.SMTP_SSL / smtplib.SMTP en los tests."""
    puerto = int(entorno.get("SMTP_PUERTO") or 465)
    contexto = ssl.create_default_context()
    if puerto == 465:
        conexion = (smtp or smtplib.SMTP_SSL)(entorno["SMTP_SERVIDOR"], puerto, context=contexto, timeout=30)
    else:
        conexion = (smtp or smtplib.SMTP)(entorno["SMTP_SERVIDOR"], puerto, timeout=30)
    with conexion as servidor:
        if puerto != 465:
            servidor.starttls(context=contexto)
        servidor.login(entorno["SMTP_USUARIO"], entorno["SMTP_CLAVE"])
        servidor.send_message(mensaje)


# -- Programa -----------------------------------------------------------------------------------------------

def _tema(entorno: dict) -> str:
    tema = (entorno.get("NTFY_TEMA") or "").strip()
    if tema and not re.fullmatch(r"[A-Za-z0-9_-]{20,60}", tema):
        _decir("NTFY_TEMA no vale: usa entre 20 y 60 letras, números, - o _ (aleatorio). Sin avisos push ni eventos del bot.")
        return ""
    return tema


def avisar_movil(entorno: dict, titulo: str, texto: str, ahora: datetime, abrir, boton: bool = False) -> bool:
    """Notificación push con ntfy.sh (gratis, sin cuenta), solo si existe el secreto NTFY_TEMA.

    El tema hace de contraseña: quien lo conozca puede leer los avisos, así que debe ser largo y aleatorio.
    Todas van con la misma prioridad (alta) y la hora delante: Android ordena primero por prioridad, así que
    mezclarlas desordenaría la lista en el móvil. Al pulsarla se abre el historial del monitor. Con `boton`, lleva
    «Ya lo sé»: deja un evento de pausa en «<tema>-ctl» y la caída pasa a un correo al día, sin más pushes."""
    tema = _tema(entorno)
    if not tema or not titulo:
        return False
    datos = {"topic": tema, "title": f"{madrid(ahora):%H:%M} · {titulo}"[:200], "message": texto[:600],
             "priority": PRIORIDAD_PUSH}
    repositorio = entorno.get("GITHUB_REPOSITORY") or ""
    if re.fullmatch(REPOSITORIO, repositorio):
        datos["click"] = f"https://github.com/{repositorio}/actions"
    if boton:
        evento = json.dumps({"tipo": "pausa", "cuando": _iso(ahora), "origen": "boton"})
        datos["actions"] = [{"action": "http", "label": "Ya lo sé", "url": f"https://ntfy.sh/{tema}-ctl",
                             "method": "POST", "body": evento, "clear": True}]
    try:
        with abrir(urllib.request.Request("https://ntfy.sh/", data=json.dumps(datos).encode(), method="POST",
                                          headers={"Content-Type": "application/json"}), timeout=15) as respuesta:
            ok = 200 <= respuesta.status < 300
    except ERRORES_DE_RED as error:
        _decir(f"Aviso push NO enviado ({_tipo(error)}).")
        return False
    _decir("Aviso push enviado." if ok else "Aviso push NO enviado.")
    return ok


def leer_eventos(entorno: dict, desde_id: str, abrir) -> tuple[list, str, str | None]:
    """Eventos que el bot dejó en el tema privado «<NTFY_TEMA>-ctl» (ntfy.sh los guarda 12 horas).

    Devuelve (eventos válidos en el orden en que llegaron a ntfy, id del último leído, id desde el que volver a
    leer). Lo último es None salvo si hay un arranque con causa: entonces es el id anterior a ese arranque, para
    volver a leerlo mientras el bot no responda en vez de guardar su texto en estado.json, que es público.
    Si ntfy falla, ningún evento: el monitor sigue como siempre."""
    tema = _tema(entorno)
    if not tema:
        return [], desde_id, None
    desde = desde_id if re.fullmatch(r"[A-Za-z0-9]{6,20}", desde_id or "") else "12h"
    try:
        with abrir(urllib.request.Request(f"https://ntfy.sh/{tema}-ctl/json?poll=1&since={desde}"), timeout=15) as respuesta:
            lineas = respuesta.read(500000).decode("utf-8", "replace").splitlines()
    except ERRORES_DE_RED as error:
        _decir(f"Eventos del bot no disponibles ({_tipo(error)}).")
        return [], desde_id, None
    eventos, ultimo, reanudar = [], desde_id, None
    for linea in lineas:
        try:
            mensaje = _objeto(json.loads(linea))
        except ValueError:
            continue
        if mensaje.get("event") != "message":
            continue
        previo, ultimo = ultimo, str(mensaje.get("id") or ultimo)
        try:
            evento = _objeto(json.loads(mensaje.get("message") or "{}"))
        except (ValueError, TypeError):
            continue
        if evento.get("tipo") not in TIPOS_DE_EVENTO:
            continue
        try:
            recibido = datetime.fromtimestamp(mensaje["time"], timezone.utc) if isinstance(mensaje.get("time"), (int, float)) else None
        except (OverflowError, OSError, ValueError):
            recibido = None
        momento = momento_del_evento(evento.get("cuando"), recibido)
        if not momento:
            continue
        causa = " ".join(str(evento.get("causa") or "").split())[:CONTROL_MAXIMO]
        eventos.append({"tipo": evento["tipo"], "cuando": _iso(momento), "causa": causa,
                        "origen": str(evento.get("origen") or "")[:20]})
        if evento["tipo"] == "arranque":
            reanudar = previo if causa else None
    if eventos:
        _decir("Eventos del bot: " + ", ".join(e["tipo"] for e in eventos) + ".")
    return eventos, ultimo, reanudar


def leer_estado(ruta: str) -> dict:
    try:
        with open(ruta, encoding="utf-8") as archivo:
            datos = json.load(archivo)
    except (OSError, ValueError):
        return estado_inicial()
    return {**estado_inicial(), **_objeto(datos), "contexto": ""}


def guardar_estado(ruta: str, estado: dict) -> None:
    """Solo los campos conocidos y nunca los privados: el archivo se publica en el repositorio."""
    publico = {campo: estado.get(campo, valor) for campo, valor in estado_inicial().items() if campo not in CAMPOS_PRIVADOS}
    with open(ruta, "w", encoding="utf-8", newline="\n") as archivo:
        json.dump(publico, archivo, ensure_ascii=False, indent=1, sort_keys=True)
        archivo.write("\n")


def ronda(ruta: str, entorno: dict, ahora: datetime, abrir, dormir, smtp, silencio: tuple = SILENCIO,
          previa: datetime | None = None) -> tuple[int, bool]:
    """Una comprobación con su decisión y sus avisos. Devuelve (código de salida, hay que confirmar un fallo).

    `previa` es la hora de la comprobación anterior de esta misma ejecución (en la confirmación)."""
    anterior = leer_estado(ruta)
    eventos, ultimo, reanudar = leer_eventos(entorno, anterior.get("ntfy_ultimo", ""), abrir)
    anterior = aplicar_eventos(anterior, eventos)
    pausa = leer_pausa(entorno.get("PAUSA_HASTA", ""), ahora)
    if pausa and ahora < pausa:
        _decir(f"En pausa hasta {hora(pausa)} (hora de Madrid). No se comprueba nada.")
        resultado = None
    else:
        resultado = comprobar(entorno, abrir, dormir)
        if resultado["ok"]:
            _decir(f"El bot responde ({resultado['dominio']}).")
        elif resultado["causa"] == "meta":
            _decir(f"Meta no contesta al monitor ({resultado['detalle']}): no se sabe si el bot responde.")
        else:
            _decir(f"El bot NO responde: {resultado['causa']} ({' · '.join(x for x in (resultado['dominio'], resultado['detalle']) if x)}).")
    ultima = previa or _fecha(entorno.get("ULTIMA_EJECUCION") or "")
    nuevo, correo = decidir(anterior, resultado, ahora, ultima)
    if ultima and ahora - ultima > HUECO and not previa:
        _decir(f"El monitor no se ejecutó entre {hora(ultima)} y {hora(ahora)} (hora de Madrid).")
    # La causa de un arranque se vuelve a leer de ntfy mientras el bot no responda (no se guarda en el estado).
    consumido = resultado is not None and resultado["ok"]
    nuevo["ntfy_ultimo"] = ultimo if reanudar is None or consumido else reanudar
    codigo = 0
    if correo:
        aviso = redactar(correo, ahora, entorno, silencio)
        hilo = hilo_del_incidente(entorno, correo["desde"])
        try:
            enviar(entorno, mensaje_de_correo(entorno, aviso, ahora, hilo, raiz=correo["tipo"] in ("caida", "sin_comprobar")), smtp)
            correo_ok = True
            _decir(f"Correo enviado: {aviso['asunto']}")
        except Exception as error:  # noqa: BLE001 - el detalle del servidor podría repetir datos de acceso
            correo_ok = False
            _decir(f"El correo NO ha salido ({type(error).__name__}). Revisa los secretos SMTP_*.")
        push_ok = False
        if suena(correo["tipo"], correo.get("intencionado", False), ahora, silencio):
            push_ok = avisar_movil(entorno, aviso["push_titulo"], aviso["push_texto"], ahora, abrir, aviso["push_boton"])
        else:
            _decir("Sin aviso en el móvil (horas de silencio, apagado avisado o aviso solo por correo).")
        if not correo_ok:
            codigo = 1
            if push_ok:
                _decir("El aviso ha llegado al móvil: no se repetirá.")
            else:
                _decir("Se reintentará en la próxima comprobación.")
                nuevo = {**anterior, "ntfy_ultimo": nuevo["ntfy_ultimo"]}
    _decir(f"Estado: {nuevo['estado']} · fallos seguidos: {nuevo['fallos']}" + (f" · causa: {nuevo['causa']}" if nuevo["causa"] else ""))
    guardar_estado(ruta, nuevo)
    confirmar = 0 < int(nuevo.get("fallos") or 0) < FALLOS_PARA_AVISAR and codigo == 0
    return codigo, confirmar


def ejecutar(ruta: str, entorno: dict, ahora: datetime | None = None, abrir=None, dormir=time.sleep,
             smtp=None, confirmar: int = 120) -> int:
    """Una comprobación; si falla, otra `confirmar` segundos después para avisar sin esperar a la siguiente
    ejecución. `ahora` fijo solo en tests."""
    faltan = [nombre for nombre in SECRETOS if not entorno.get(nombre)]
    if faltan:
        _decir("Faltan secretos del repositorio: " + ", ".join(faltan))
        return 1
    abrir = abrir or abridor()
    silencio = leer_silencio(entorno.get("HORAS_SILENCIO", ""))
    primero = ahora or datetime.now(timezone.utc)
    codigo, repetir = ronda(ruta, entorno, primero, abrir, dormir, smtp, silencio)
    if repetir and confirmar:
        _decir(f"-- Confirmación del fallo en {confirmar} s")
        dormir(confirmar)
        momento = ahora + timedelta(seconds=confirmar) if ahora else datetime.now(timezone.utc)
        codigo = max(codigo, ronda(ruta, entorno, momento, abrir, dormir, smtp, silencio, previa=primero)[0])
    return codigo


def _entero(texto, defecto: int, minimo: int, maximo: int) -> int:
    try:
        return min(maximo, max(minimo, int(texto)))
    except (TypeError, ValueError):
        return defecto


def main():
    for salida in (sys.stdout, sys.stderr):
        try:
            salida.reconfigure(encoding="utf-8", errors="replace")  # emojis en cualquier consola
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--estado", default="estado.json", help="Archivo de estado entre ejecuciones")
    parser.add_argument("--ejecucion-anterior", action="store_true",
                        help="Lee de la entrada la respuesta de la API de GitHub con las ejecuciones del workflow e "
                             "imprime cuándo terminó la anterior (lo usa scripts/workflow.sh)")
    argumentos = parser.parse_args()
    entorno = dict(os.environ)
    if argumentos.ejecucion_anterior:
        try:
            respuesta = json.load(sys.stdin)
        except ValueError:
            respuesta = {}
        print(ejecucion_anterior(respuesta, entorno.get("GITHUB_RUN_ID", "")))
        return
    sys.exit(ejecutar(argumentos.estado, entorno, confirmar=_entero(entorno.get("CONFIRMAR"), 120, 30, 240)))


if __name__ == "__main__":
    main()
