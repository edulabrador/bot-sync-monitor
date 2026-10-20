"""Monitor externo de un bot de WhatsApp Cloud API («dead man's switch»).

Lo lanza Google Cloud Scheduler cada 5 minutos (y el cron de GitHub de respaldo). Pregunta a Meta (solo
lectura) a qué dirección manda los mensajes de WhatsApp, comprueba que esa dirección contesta en /salud y avisa
solo cuando cambia el estado: 🔴 si falla y lo confirma 2 minutos después, 🟢 al recuperarse y recordatorios a
la 1 h, a las 3 h y luego cada 6 h (sin sonar de 00:00 a 08:00, hora de Madrid).

    python monitor.py --estado estado.json

Variables de entorno (secretos de GitHub Actions): WHATSAPP_APP_ID, WHATSAPP_APP_SECRET, SMTP_SERVIDOR,
SMTP_PUERTO, SMTP_USUARIO, SMTP_CLAVE, AVISOS_EMAIL. Opcionales: NOMBRE_BOT y COMO_ARRANCAR (secretos, para
personalizar los correos sin dejarlo en el código), NTFY_TEMA (secreto: además del correo, aviso push al móvil
con la app ntfy) y PAUSA_HASTA (variable del repo). Con NTFY_TEMA, el bot puede dejar eventos en el tema
privado «<NTFY_TEMA>-ctl»: apagado intencionado (no avisar), arranque (con la causa de la caída) y caída ya
avisada por el propio bot (no repetir el aviso). CONFIRMAR: segundos hasta confirmar un fallo (120).
Sin dependencias externas. Nunca imprime secretos ni la dirección completa del bot.
Sale con código 1 si no puede hacer su trabajo (falta configuración o el correo no sale): GitHub avisa entonces
al dueño del repositorio de que la ejecución ha fallado.
"""
import argparse
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
from html import escape

VERSION_DE_LA_API = "v26.0"  # la misma que usa el bot
FALLOS_PARA_AVISAR = 2
RECORDATORIOS = (timedelta(hours=1), timedelta(hours=3))  # después, uno cada 6 horas
CADA_RECORDATORIO = timedelta(hours=6)
APAGADO_RECORDATORIO = timedelta(hours=24)  # apagado intencionado: un correo al día, sin push
AVISO_DEL_BOT = timedelta(minutes=20)  # si el bot avisó de su caída hace menos, el monitor no lo repite
SILENCIO = (0, 8)  # horas de Madrid en las que los recordatorios y los verdes no suenan en el móvil
CONTROL_MAXIMO = 300  # caracteres de texto que se aceptan de un evento del bot
LATIDO = timedelta(days=25)  # un commit al menos cada 25 días: GitHub apaga el cron tras 60 días sin actividad
INTENTOS, ESPERA_ENTRE_INTENTOS, TIEMPO_MAXIMO = 3, 20, 10
PRIORIDAD_PUSH = 4  # alta: suena y se ve; la misma para todas, para que el móvil las ordene por hora
SECRETOS = ("WHATSAPP_APP_ID", "WHATSAPP_APP_SECRET", "SMTP_SERVIDOR", "SMTP_USUARIO", "SMTP_CLAVE", "AVISOS_EMAIL")
NOMBRE_POR_DEFECTO, ARRANQUE_POR_DEFECTO = "Bot de mensajería", "el script de arranque del bot"

URGENCIAS = {"alta": ("#c0392b", "URGENTE", "🔴"), "ok": ("#1e8449", "RESUELTO", "🟢"), "info": ("#2471a3", "AVISO", "🔵")}
DIAS = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")



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


# -- Hora de Madrid sin dependencias (las reglas de horario de verano de la UE) ---------------------------------

def _ultimo_domingo(anio: int, mes: int) -> datetime:
    dia = datetime(anio, mes, 31, 1, tzinfo=timezone.utc)
    return dia - timedelta(days=(dia.weekday() + 1) % 7)


def madrid(momento: datetime) -> datetime:
    momento = momento.astimezone(timezone.utc)
    verano = _ultimo_domingo(momento.year, 3) <= momento < _ultimo_domingo(momento.year, 10)
    return momento.astimezone(timezone(timedelta(hours=2 if verano else 1)))


def hora(momento: datetime) -> str:
    return madrid(momento).strftime("%d-%m-%Y %H:%M")


def cuando(momento: datetime, ahora: datetime) -> str:
    """«hoy a las 12:15», «ayer a las 12:15» o «el lunes 19 a las 12:15» (hora de Madrid)."""
    local, hoy = madrid(momento), madrid(ahora).date()
    dias = (hoy - local.date()).days
    dia = "hoy" if dias == 0 else "ayer" if dias == 1 else f"el {DIAS[local.weekday()]} {local.day}"
    return f"{dia} a las {local:%H:%M}"


def en_silencio(ahora: datetime, horas: tuple = SILENCIO) -> bool:
    inicio, fin = horas
    return inicio <= madrid(ahora).hour < fin if inicio <= fin else not fin <= madrid(ahora).hour < inicio


def leer_silencio(texto: str) -> tuple:
    """HORAS_SILENCIO «0-8» (por defecto) o «» para desactivarlo."""
    encontrado = re.fullmatch(r"\s*(\d{1,2})\s*-\s*(\d{1,2})\s*", texto or "")
    if encontrado and all(0 <= int(x) <= 24 for x in encontrado.groups()):
        return int(encontrado[1]), int(encontrado[2])  # «0-0» lo desactiva
    return SILENCIO


def duracion(tramo: timedelta) -> str:
    minutos = max(0, int(tramo.total_seconds() // 60))
    dias, minutos = divmod(minutos, 1440)
    horas, minutos = divmod(minutos, 60)
    partes = ([f"{dias} d"] if dias else []) + ([f"{horas} h"] if horas else []) + [f"{minutos} min"]
    return " ".join(partes if dias or horas else [f"{minutos} min"])


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
    desfase = madrid(local.replace(tzinfo=timezone.utc)).utcoffset()
    return (local - desfase).replace(tzinfo=timezone.utc)


# -- Red ----------------------------------------------------------------------------------------------------

class SinRedirecciones(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise urllib.error.URLError("redirección rechazada")


def abridor():
    return urllib.request.build_opener(SinRedirecciones(), urllib.request.HTTPSHandler(context=ssl.create_default_context())).open


def enmascarar(url: str) -> str:
    """Solo el dominio principal: «***.trycloudflare.com». La dirección completa no se publica en los registros."""
    nombre = urllib.parse.urlsplit(url or "").hostname or ""
    partes = nombre.split(".")
    return "***." + ".".join(partes[-2:]) if len(partes) >= 2 else "(sin dirección)"


def _tipo(error: Exception) -> str:
    return f"HTTP {error.code}" if isinstance(error, urllib.error.HTTPError) else type(error).__name__


def direccion_de_meta(app_id: str, secreto: str, abrir) -> tuple[str, str, str]:
    """(callback_url, causa, detalle). Solo lectura de la suscripción de la app."""
    consulta = (f"https://graph.facebook.com/{VERSION_DE_LA_API}/{urllib.parse.quote(app_id)}/subscriptions?"
                + urllib.parse.urlencode({"access_token": f"{app_id}|{secreto}"}))
    try:
        with abrir(urllib.request.Request(consulta), timeout=20) as respuesta:
            datos = json.loads(respuesta.read(200000)).get("data", [])
    except (urllib.error.URLError, ValueError, TimeoutError, OSError) as error:
        return "", "meta", _tipo(error)  # el tipo basta: el mensaje podría llevar la consulta con la credencial
    for suscripcion in datos:
        url = str(suscripcion.get("callback_url") or "")
        if (suscripcion.get("active") and url.startswith("https://")
                and any(campo.get("name") == "messages" for campo in suscripcion.get("fields", []))):
            return url, "", ""
    return "", "sin_suscripcion", "ninguna suscripción activa con el campo messages"


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
                cuerpo = json.loads(respuesta.read(2000) or b"{}")
                if respuesta.status == 200 and cuerpo.get("estado") == "ok":
                    return "ok", ""
                resultado = ("no_responde", f"HTTP {respuesta.status}")
        except urllib.error.HTTPError as error:
            try:
                estado = json.loads(error.read(2000) or b"{}").get("estado")
            except (ValueError, OSError):
                estado = ""
            resultado = ("sin_worker", "HTTP 503 «sin worker»") if estado == "sin worker" else ("no_responde", _tipo(error))
        except (urllib.error.URLError, ValueError, TimeoutError, OSError) as error:
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


# -- Estado y decisión --------------------------------------------------------------------------------------

def _fecha(texto) -> datetime | None:
    try:
        return datetime.fromisoformat(texto) if texto else None
    except ValueError:
        return None


def _iso(momento: datetime) -> str:
    return momento.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def estado_inicial() -> dict:
    return {"estado": "desconocido", "desde": "", "fallos": 0, "primer_fallo": "", "causa": "", "ultimo_correo": "",
            "latido": "", "recordatorios": 0, "pausa_bot": "", "aviso_bot": "", "contexto": "", "ntfy_ultimo": ""}


def aplicar_eventos(estado: dict, eventos: list) -> dict:
    """Eventos del bot en orden: pausa (apagado intencionado), arranque (con la causa de la caída) y
    caida_avisada (el bot ya avisó de su propia caída). Solo cambia campos de control, nunca el estado."""
    nuevo = dict(estado)
    for evento in eventos:
        tipo, momento = evento.get("tipo"), _fecha(evento.get("cuando"))
        if not momento:
            continue
        if tipo == "pausa":
            nuevo["pausa_bot"] = _iso(momento)
        elif tipo == "arranque":
            nuevo["pausa_bot"] = ""
            nuevo["contexto"] = " ".join(str(evento.get("causa") or "").split())[:CONTROL_MAXIMO]
        elif tipo == "caida_avisada":
            nuevo["aviso_bot"] = _iso(momento)
    return nuevo


def recordatorios_debidos(tiempo: timedelta) -> int:
    """Cuántos recordatorios corresponden a una caída de esta duración: a la 1 h, a las 3 h y luego cada 6 h."""
    debidos = sum(1 for limite in RECORDATORIOS if tiempo >= limite)
    if tiempo >= RECORDATORIOS[-1] + CADA_RECORDATORIO:
        debidos += int((tiempo - RECORDATORIOS[-1]) / CADA_RECORDATORIO)
    return debidos


def decidir(anterior: dict, resultado: dict | None, ahora: datetime) -> tuple[dict, dict | None]:
    """Nuevo estado y, si toca, el aviso {'tipo': 'caida'|'recordatorio'|'vuelta'|'apagado', ...}.

    `resultado` None = en pausa por PAUSA_HASTA. Estados: arriba, abajo, apagado (lo cerró el propietario), pausa."""
    nuevo = {**estado_inicial(), **anterior}
    correo = None
    antes = anterior.get("estado")
    if resultado is None:
        nuevo.update(estado="pausa", fallos=0, primer_fallo="", causa="", recordatorios=0)
        if antes != "pausa":
            nuevo["desde"] = _iso(ahora)
    elif resultado["ok"]:
        if antes in ("abajo", "apagado"):
            desde = _fecha(anterior.get("desde")) or ahora
            correo = {"tipo": "vuelta", "desde": desde, "duracion": ahora - desde, "intencionado": antes == "apagado",
                      "contexto": nuevo.get("contexto", ""), **resultado}
        if antes != "arriba":
            nuevo["desde"] = _iso(ahora)
        # Una pausa o un aviso del bot de hace un momento aún no se ven fuera (el túnel tarda unos segundos en
        # caer): se guardan para la próxima comprobación. Los antiguos se olvidan, para no silenciar otra caída.
        reciente = {campo: valor for campo in ("pausa_bot", "aviso_bot")
                    if (valor := nuevo.get(campo)) and antes == "arriba" and ahora - (_fecha(valor) or ahora) < AVISO_DEL_BOT}
        nuevo.update({"estado": "arriba", "fallos": 0, "primer_fallo": "", "causa": "", "recordatorios": 0,
                      "pausa_bot": "", "aviso_bot": "", "contexto": "", **reciente})
    elif nuevo.get("pausa_bot"):
        # El propietario cerró el bot a propósito: sin avisos urgentes; un correo al día para que no se olvide.
        pausa = _fecha(nuevo["pausa_bot"]) or ahora
        if antes != "apagado":
            nuevo.update(estado="apagado", desde=_iso(pausa), ultimo_correo=_iso(ahora), recordatorios=0)
        elif ahora - (_fecha(anterior.get("ultimo_correo")) or pausa) >= APAGADO_RECORDATORIO:
            correo = {"tipo": "apagado", "desde": pausa, "duracion": ahora - pausa, **resultado}
            nuevo["ultimo_correo"] = _iso(ahora)
        nuevo.update(fallos=FALLOS_PARA_AVISAR, causa=resultado["causa"])
    elif antes == "abajo":
        desde = _fecha(anterior.get("desde")) or ahora
        debidos = recordatorios_debidos(ahora - desde)
        if debidos > int(anterior.get("recordatorios") or 0):
            correo = {"tipo": "recordatorio", "desde": desde, "duracion": ahora - desde, **resultado}
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
    latido = _fecha(nuevo.get("latido"))
    if not latido or ahora - latido >= LATIDO:
        nuevo["latido"] = _iso(ahora)
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
       "minutos después: los avisos llegan unos 5-10 minutos después del cambio. Si vas a apagarlo a propósito, "
       "cierra antes la ventana del bot y no recibirás avisos de caída.")
SIGUIENTE = ("Al recuperarse (🟢). Si sigue caído: recordatorios a la 1 h, a las 3 h y luego cada 6 h "
             "(de 00:00 a 08:00 llegan por correo sin sonar en el móvil).")


def redactar(correo: dict, ahora: datetime, entorno: dict | None = None) -> dict:
    """Correo y push de un aviso: {'asunto', 'texto', 'html', 'push_titulo', 'push_texto'}."""
    entorno = entorno or {}
    marca = (entorno.get("NOMBRE_BOT") or "").strip() or NOMBRE_POR_DEFECTO
    arranque = (entorno.get("COMO_ARRANCAR") or "").strip() or ARRANQUE_POR_DEFECTO
    causa, que_hacer, causa_corta, accion_corta = causas(arranque).get(correo.get("causa") or "", ("", "", "", ""))
    detalle = " · ".join(x for x in (correo.get("dominio"), correo.get("detalle")) if x)
    tiempo, desde = duracion(correo["duracion"]), cuando(correo["desde"], ahora)
    tipo = correo["tipo"]
    if tipo == "vuelta":
        contexto = correo.get("contexto") or ""
        palabra = "apagado" if correo.get("intencionado") else "caído"
        asunto = f"🟢 El bot vuelve a responder · estuvo {palabra} {tiempo}"
        que = ("El bot vuelve a contestar tras el apagado que hiciste." if correo.get("intencionado")
               else "El bot vuelve a contestar por la dirección que tiene Meta.")
        filas = [("Qué ha pasado", que), ("Sin responder desde", desde), ("Vuelve a responder", cuando(ahora, ahora)),
                 ("Tiempo sin responder", f"unos {tiempo}"), ("Qué pasó en el ordenador", contexto)]
        texto, html = correo_html("ok", "El bot vuelve a responder", filas,
                                  "Nada urgente. Si llegaron mensajes durante la caída, el bot te enviará un resumen "
                                  "de los que contestó y de los que siguen pendientes.", PIE, marca)
        push = (contexto + " " if contexto else "") + f"Estuvo sin responder {tiempo}."
        return {"asunto": asunto, "texto": texto, "html": html, "push_titulo": "🟢 El bot vuelve a responder",
                "push_texto": push}
    if tipo == "apagado":
        asunto = f"🔵 El bot sigue apagado · lo cerraste {desde}"
        filas = [("Qué pasa", "El bot sigue apagado desde que cerraste su ventana. Los clientes que escriban no "
                              "reciben respuesta hasta que lo enciendas."),
                 ("Apagado desde", desde), ("Tiempo apagado", tiempo),
                 ("Siguiente aviso", "Al encenderlo (🟢), o mañana si sigue apagado. Sin avisos en el móvil.")]
        texto, html = correo_html("info", "El bot sigue apagado", filas,
                                  f"Nada si lo dejaste apagado a propósito. Para volver a atender, abre {arranque}.", PIE, marca)
        return {"asunto": asunto, "texto": texto, "html": html, "push_titulo": "", "push_texto": ""}
    recordatorio = tipo == "recordatorio"
    titulo = "El bot sigue sin responder" if recordatorio else "El bot no responde"
    asunto = f"🔴 {titulo} · desde {desde}" + (f" ({tiempo})" if recordatorio else "")
    filas = [("Qué ha pasado", "Los clientes que escriban por WhatsApp no reciben respuesta del bot."),
             ("Sin responder desde", f"{desde} (primera comprobación fallida)"),
             ("Comprobado", cuando(ahora, ahora)), ("Tiempo sin responder", tiempo if recordatorio else ""),
             ("Causa probable", causa), ("Detalle técnico", detalle), ("Siguiente aviso", SIGUIENTE)]
    texto, html = correo_html("alta", titulo, filas, que_hacer, PIE, marca)
    push = (f"Desde {desde} ({tiempo}). " if recordatorio else "") + f"{causa_corta}\nQué hacer: {accion_corta}"
    return {"asunto": asunto, "texto": texto, "html": html, "push_titulo": f"🔴 {titulo}", "push_texto": push.strip()}


def suena(tipo: str, intencionado: bool, ahora: datetime, silencio: tuple = SILENCIO) -> bool:
    """¿Va también al móvil? La primera caída siempre; recordatorios y verdes, fuera de las horas de silencio;
    nada de lo que tenga que ver con un apagado intencionado."""
    if tipo == "caida":
        return True
    if tipo == "apagado" or intencionado:
        return False
    return not en_silencio(ahora, silencio)


def enviar(entorno: dict, asunto: str, texto: str, html: str, smtp=smtplib.SMTP_SSL) -> None:
    mensaje = EmailMessage()
    mensaje["Subject"], mensaje["From"] = asunto, f"Monitor del bot <{entorno['SMTP_USUARIO']}>"
    mensaje["To"] = entorno["AVISOS_EMAIL"]
    mensaje.set_content(texto)
    mensaje.add_alternative(html, subtype="html")
    puerto = int(entorno.get("SMTP_PUERTO") or 465)
    with smtp(entorno["SMTP_SERVIDOR"], puerto, context=ssl.create_default_context(), timeout=30) as servidor:
        servidor.login(entorno["SMTP_USUARIO"], entorno["SMTP_CLAVE"])
        servidor.send_message(mensaje)


# -- Programa -----------------------------------------------------------------------------------------------

def _tema(entorno: dict) -> str:
    tema = (entorno.get("NTFY_TEMA") or "").strip()
    if tema and not re.fullmatch(r"[A-Za-z0-9_-]{20,60}", tema):
        _decir("NTFY_TEMA no vale: usa entre 20 y 60 letras, números, - o _ (aleatorio). Sin avisos push ni eventos del bot.")
        return ""
    return tema


def avisar_movil(entorno: dict, titulo: str, texto: str, ahora: datetime, abrir) -> bool:
    """Notificación push con ntfy.sh (gratis, sin cuenta), solo si existe el secreto NTFY_TEMA.

    El tema hace de contraseña: quien lo conozca puede leer los avisos, así que debe ser largo y aleatorio.
    Todas van con la misma prioridad (alta) y la hora delante: Android ordena primero por prioridad, así que
    mezclarlas desordenaría la lista en el móvil. Al pulsarla se abre el historial del monitor."""
    tema = _tema(entorno)
    if not tema or not titulo:
        return False
    datos = {"topic": tema, "title": f"{madrid(ahora):%H:%M} · {titulo}"[:200], "message": texto[:600],
             "priority": PRIORIDAD_PUSH}
    repositorio = entorno.get("GITHUB_REPOSITORY") or ""
    if re.fullmatch(r"[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}", repositorio):
        datos["click"] = f"https://github.com/{repositorio}/actions"
    try:
        with abrir(urllib.request.Request("https://ntfy.sh/", data=json.dumps(datos).encode(), method="POST",
                                          headers={"Content-Type": "application/json"}), timeout=15) as respuesta:
            ok = 200 <= respuesta.status < 300
    except (urllib.error.URLError, ValueError, TimeoutError, OSError) as error:
        _decir(f"Aviso push NO enviado ({_tipo(error)}).")
        return False
    _decir("Aviso push enviado." if ok else "Aviso push NO enviado.")
    return ok


def leer_eventos(entorno: dict, desde_id: str, abrir) -> tuple[list, str]:
    """Eventos que el bot dejó en el tema privado «<NTFY_TEMA>-ctl» (ntfy.sh los guarda 12 horas).

    Devuelve (eventos válidos en orden, id del último leído). Si falla, ninguno: el monitor sigue como siempre."""
    tema = _tema(entorno)
    if not tema:
        return [], desde_id
    desde = desde_id if re.fullmatch(r"[A-Za-z0-9]{6,20}", desde_id or "") else "12h"
    try:
        with abrir(urllib.request.Request(f"https://ntfy.sh/{tema}-ctl/json?poll=1&since={desde}"), timeout=15) as respuesta:
            lineas = respuesta.read(500000).decode("utf-8", "replace").splitlines()
    except (urllib.error.URLError, ValueError, TimeoutError, OSError) as error:
        _decir(f"Eventos del bot no disponibles ({_tipo(error)}).")
        return [], desde_id
    eventos, ultimo = [], desde_id
    for linea in lineas:
        try:
            mensaje = json.loads(linea)
            if mensaje.get("event") != "message":
                continue
            ultimo = str(mensaje.get("id") or ultimo)
            evento = json.loads(mensaje.get("message") or "{}")
        except (ValueError, TypeError, AttributeError):
            continue
        if isinstance(evento, dict) and evento.get("tipo") in ("pausa", "arranque", "caida_avisada"):
            eventos.append({"tipo": evento["tipo"], "cuando": str(evento.get("cuando") or "")[:40],
                            "causa": str(evento.get("causa") or "")[:CONTROL_MAXIMO]})
    if eventos:
        _decir("Eventos del bot: " + ", ".join(e["tipo"] for e in eventos) + ".")
    return sorted(eventos, key=lambda e: e["cuando"]), ultimo


def leer_estado(ruta: str) -> dict:
    try:
        with open(ruta, encoding="utf-8") as archivo:
            datos = json.load(archivo)
        return {**estado_inicial(), **datos} if isinstance(datos, dict) else estado_inicial()
    except (OSError, ValueError):
        return estado_inicial()


def guardar_estado(ruta: str, estado: dict) -> None:
    with open(ruta, "w", encoding="utf-8", newline="\n") as archivo:
        json.dump(estado, archivo, ensure_ascii=False, indent=1, sort_keys=True)
        archivo.write("\n")


def ronda(ruta: str, entorno: dict, ahora: datetime, abrir, dormir, smtp) -> tuple[int, bool]:
    """Una comprobación con su decisión y sus avisos. Devuelve (código de salida, hay que confirmar un fallo)."""
    anterior = leer_estado(ruta)
    eventos, ultimo = leer_eventos(entorno, anterior.get("ntfy_ultimo", ""), abrir)
    anterior = {**aplicar_eventos(anterior, eventos), "ntfy_ultimo": ultimo}
    pausa = leer_pausa(entorno.get("PAUSA_HASTA", ""), ahora)
    if pausa and ahora < pausa:
        _decir(f"En pausa hasta {hora(pausa)} (hora de Madrid). No se comprueba nada.")
        resultado = None
    else:
        resultado = comprobar(entorno, abrir, dormir)
        if resultado["ok"]:
            _decir(f"El bot responde ({resultado['dominio']}).")
        else:
            _decir(f"El bot NO responde: {resultado['causa']} ({' · '.join(x for x in (resultado['dominio'], resultado['detalle']) if x)}).")
    nuevo, correo = decidir(anterior, resultado, ahora)
    codigo = 0
    if correo:
        aviso = redactar(correo, ahora, entorno)
        try:
            enviar(entorno, aviso["asunto"], aviso["texto"], aviso["html"], smtp)
        except Exception as error:  # noqa: BLE001 - el detalle del servidor podría repetir datos de acceso
            _decir(f"El correo NO ha salido ({type(error).__name__}). Se reintentará en la próxima comprobación.")
            nuevo, codigo = {**anterior, "latido": nuevo["latido"]}, 1
        else:
            _decir(f"Correo enviado: {aviso['asunto']}")
        if suena(correo["tipo"], correo.get("intencionado", False), ahora, leer_silencio(entorno.get("HORAS_SILENCIO", ""))):
            avisar_movil(entorno, aviso["push_titulo"], aviso["push_texto"], ahora, abrir)
        else:
            _decir("Sin aviso en el móvil (horas de silencio o apagado intencionado).")
    _decir(f"Estado: {nuevo['estado']} · fallos seguidos: {nuevo['fallos']}" + (f" · causa: {nuevo['causa']}" if nuevo["causa"] else ""))
    guardar_estado(ruta, nuevo)
    confirmar = resultado is not None and not resultado["ok"] and nuevo["estado"] not in ("abajo", "apagado") and codigo == 0
    return codigo, confirmar


def ejecutar(ruta: str, entorno: dict, ahora: datetime | None = None, abrir=None, dormir=time.sleep,
             smtp=smtplib.SMTP_SSL, confirmar: int = 120) -> int:
    """Una comprobación; si falla, otra `confirmar` segundos después para avisar sin esperar a la siguiente
    ejecución. `ahora` fijo solo en tests."""
    faltan = [nombre for nombre in SECRETOS if not entorno.get(nombre)]
    if faltan:
        _decir("Faltan secretos del repositorio: " + ", ".join(faltan))
        return 1
    abrir = abrir or abridor()
    codigo, repetir = ronda(ruta, entorno, ahora or datetime.now(timezone.utc), abrir, dormir, smtp)
    if repetir and confirmar:
        _decir(f"-- Confirmación del fallo en {confirmar} s")
        dormir(confirmar)
        momento = ahora + timedelta(seconds=confirmar) if ahora else datetime.now(timezone.utc)
        codigo = max(codigo, ronda(ruta, entorno, momento, abrir, dormir, smtp)[0])
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
    entorno = dict(os.environ)
    sys.exit(ejecutar(parser.parse_args().estado, entorno, confirmar=_entero(entorno.get("CONFIRMAR"), 120, 30, 240)))


if __name__ == "__main__":
    main()
