"""Monitor externo de un bot de WhatsApp Cloud API («dead man's switch»).

Lo ejecuta GitHub Actions cada 10 minutos. Pregunta a Meta (solo lectura) a qué dirección manda los mensajes
de WhatsApp, comprueba que esa dirección contesta en /salud y avisa por correo solo cuando cambia el estado:
🔴 tras dos comprobaciones fallidas seguidas, 🟢 al recuperarse y un recordatorio como mucho cada 6 horas.

    python monitor.py --estado estado.json

Variables de entorno (secretos de GitHub Actions): WHATSAPP_APP_ID, WHATSAPP_APP_SECRET, SMTP_SERVIDOR,
SMTP_PUERTO, SMTP_USUARIO, SMTP_CLAVE, AVISOS_EMAIL. Opcionales: NOMBRE_BOT y COMO_ARRANCAR (secretos, para
personalizar los correos sin dejarlo en el código), NTFY_TEMA (secreto: además del correo, aviso push al móvil
con la app ntfy) y PAUSA_HASTA (variable del repo). Cada ejecución comprueba RONDAS veces (2), separadas
SEPARACION segundos (300): con el cron cada 10 minutos, el bot se comprueba cada 5.
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
RECORDATORIO = timedelta(hours=6)
LATIDO = timedelta(days=25)  # un commit al menos cada 25 días: GitHub apaga el cron tras 60 días sin actividad
INTENTOS, ESPERA_ENTRE_INTENTOS, TIEMPO_MAXIMO = 3, 20, 10
SECRETOS = ("WHATSAPP_APP_ID", "WHATSAPP_APP_SECRET", "SMTP_SERVIDOR", "SMTP_USUARIO", "SMTP_CLAVE", "AVISOS_EMAIL")
NOMBRE_POR_DEFECTO, ARRANQUE_POR_DEFECTO = "Bot de mensajería", "el script de arranque del bot"

URGENCIAS = {"alta": ("#c0392b", "URGENTE", "🔴"), "ok": ("#1e8449", "RESUELTO", "🟢")}



def causas(arranque: str) -> dict:
    """Causa probable y qué hacer. `arranque` es cómo se enciende el bot (secreto COMO_ARRANCAR)."""
    return {
        "meta": ("Meta no accesible: el monitor no ha podido preguntar a Meta a qué dirección manda los mensajes",
                 "Suele ser un fallo pasajero de Meta o de GitHub. Si se repite, comprueba en developers.facebook.com "
                 "que la app sigue activa. Si cambiaste la clave secreta de la app, actualiza el secreto "
                 "WHATSAPP_APP_SECRET de este repositorio."),
        "sin_suscripcion": ("Meta no tiene ninguna dirección activa para los mensajes de WhatsApp",
                            f"Abre {arranque} en el ordenador del bot: al arrancar vuelve a darle a Meta su dirección."),
        "no_responde": ("Meta apunta a una dirección que no responde: ordenador apagado o suspendido, ventana del bot "
                        "cerrada, sin internet o túnel caído",
                        f"Enciende el ordenador del bot (y que no se suspenda) y abre {arranque}."),
        "sin_worker": ("La dirección responde, pero la parte que contesta a los clientes está parada",
                       f"Cierra la ventana del bot y vuelve a abrir {arranque}. Si se repite, revisa los registros del bot."),
    }


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
        print(f"PAUSA_HASTA no se entiende («{texto[:40]}»): se ignora. Formato: 2026-10-20 08:00")
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
    return {"estado": "desconocido", "desde": "", "fallos": 0, "primer_fallo": "", "causa": "", "ultimo_correo": "", "latido": ""}


def decidir(anterior: dict, resultado: dict | None, ahora: datetime) -> tuple[dict, dict | None]:
    """Nuevo estado y, si toca, el correo {'tipo': 'caida'|'recordatorio'|'vuelta', ...}. `resultado` None = en pausa."""
    nuevo = {**estado_inicial(), **anterior}
    correo = None
    if resultado is None:
        nuevo.update(estado="pausa", fallos=0, primer_fallo="", causa="")
        if anterior.get("estado") != "pausa":
            nuevo["desde"] = _iso(ahora)
    elif resultado["ok"]:
        if anterior.get("estado") == "abajo":
            desde = _fecha(anterior.get("desde")) or ahora
            correo = {"tipo": "vuelta", "desde": desde, "duracion": ahora - desde, **resultado}
        if anterior.get("estado") != "arriba":
            nuevo["desde"] = _iso(ahora)
        nuevo.update(estado="arriba", fallos=0, primer_fallo="", causa="")
    else:
        if anterior.get("estado") == "abajo":
            ultimo = _fecha(anterior.get("ultimo_correo"))
            if not ultimo or ahora - ultimo >= RECORDATORIO:
                desde = _fecha(anterior.get("desde")) or ahora
                correo = {"tipo": "recordatorio", "desde": desde, "duracion": ahora - desde, **resultado}
                nuevo["ultimo_correo"] = _iso(ahora)
            nuevo["causa"] = resultado["causa"]
        else:
            fallos = min(int(anterior.get("fallos") or 0) + 1, FALLOS_PARA_AVISAR)
            primero = _fecha(anterior.get("primer_fallo")) if fallos > 1 else None
            primero = primero or ahora
            nuevo.update(fallos=fallos, primer_fallo=_iso(primero), causa=resultado["causa"])
            if fallos >= FALLOS_PARA_AVISAR:
                nuevo.update(estado="abajo", desde=_iso(primero), ultimo_correo=_iso(ahora))
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


PIE = ("Aviso del monitor externo (GitHub Actions). Comprueba cada 5 minutos, pero GitHub puede retrasar "
       "sus ejecuciones entre 5 y 15 minutos: este aviso llega unos 10-20 minutos después del cambio. Para pausarlo "
       "en un apagado intencionado, consulta el README del repositorio.")


def redactar(correo: dict, ahora: datetime, entorno: dict | None = None) -> tuple[str, str, str]:
    """(asunto, texto, html)."""
    entorno = entorno or {}
    marca = (entorno.get("NOMBRE_BOT") or "").strip() or NOMBRE_POR_DEFECTO
    arranque = (entorno.get("COMO_ARRANCAR") or "").strip() or ARRANQUE_POR_DEFECTO
    causa, que_hacer = causas(arranque).get(correo.get("causa") or "", ("", ""))
    detalle = " · ".join(x for x in (correo.get("dominio"), correo.get("detalle")) if x)
    if correo["tipo"] == "vuelta":
        asunto = f"🟢 El bot vuelve a responder · estuvo caído {duracion(correo['duracion'])}"
        filas = [("Qué ha pasado", "El bot vuelve a contestar por la dirección que tiene Meta."),
                 ("Caído desde", hora(correo["desde"])), ("Vuelve a responder", hora(ahora)),
                 ("Tiempo caído", f"unos {duracion(correo['duracion'])} (precisión de unos 5-15 minutos)")]
        texto, html = correo_html("ok", "El bot vuelve a responder", filas,
                                  "Nada urgente. Revisa si algún cliente escribió durante la caída y no recibió "
                                  "respuesta.", PIE, marca)
        return asunto, texto, html
    recordatorio = correo["tipo"] == "recordatorio"
    titulo = "El bot sigue sin responder" if recordatorio else "El bot no responde"
    asunto = (f"🔴 {titulo} · caído desde hace {duracion(correo['duracion'])}" if recordatorio
              else f"🔴 {titulo} · desde las {madrid(correo['desde']).strftime('%H:%M')}")
    filas = [("Qué ha pasado", "Los clientes que escriban por WhatsApp no reciben respuesta del bot."),
             ("Sin responder desde", f"{hora(correo['desde'])} (primera comprobación fallida)"),
             ("Comprobado", hora(ahora)), ("Tiempo caído", duracion(correo["duracion"]) if recordatorio else ""),
             ("Causa probable", causa), ("Detalle técnico", detalle),
             ("Siguiente aviso", "Al recuperarse (🟢). Si sigue caído, un recordatorio cada 6 horas.")]
    texto, html = correo_html("alta", titulo, filas, que_hacer, PIE, marca)
    return asunto, texto, html


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

def avisar_movil(entorno: dict, asunto: str, texto: str, urgente: bool, abrir) -> bool:
    """Notificación push con ntfy.sh (gratis, sin cuenta), solo si existe el secreto NTFY_TEMA.

    El tema hace de contraseña: quien lo conozca puede leer los avisos, así que debe ser largo y aleatorio."""
    tema = (entorno.get("NTFY_TEMA") or "").strip()
    if not tema:
        return False
    if not re.fullmatch(r"[A-Za-z0-9_-]{20,64}", tema):
        print("NTFY_TEMA no vale: usa entre 20 y 64 letras, números, - o _ (aleatorio). Aviso push no enviado.")
        return False
    cuerpo = json.dumps({"topic": tema, "title": asunto[:200], "message": texto[:1500],
                         "priority": 5 if urgente else 3}).encode()
    try:
        with abrir(urllib.request.Request("https://ntfy.sh/", data=cuerpo, method="POST",
                                          headers={"Content-Type": "application/json"}), timeout=15) as respuesta:
            ok = 200 <= respuesta.status < 300
    except (urllib.error.URLError, ValueError, TimeoutError, OSError) as error:
        print(f"Aviso push NO enviado ({_tipo(error)}).")
        return False
    print("Aviso push enviado." if ok else "Aviso push NO enviado.")
    return ok


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
    """Una comprobación con su decisión y sus avisos. Devuelve (código de salida, en pausa)."""
    anterior = leer_estado(ruta)
    pausa = leer_pausa(entorno.get("PAUSA_HASTA", ""), ahora)
    if pausa and ahora < pausa:
        print(f"En pausa hasta {hora(pausa)} (hora de Madrid). No se comprueba nada.")
        resultado = None
    else:
        resultado = comprobar(entorno, abrir, dormir)
        if resultado["ok"]:
            print(f"El bot responde ({resultado['dominio']}).")
        else:
            print(f"El bot NO responde: {resultado['causa']} ({' · '.join(x for x in (resultado['dominio'], resultado['detalle']) if x)}).")
    nuevo, correo = decidir(anterior, resultado, ahora)
    codigo = 0
    if correo:
        asunto, texto, html = redactar(correo, ahora, entorno)
        try:
            enviar(entorno, asunto, texto, html, smtp)
            print(f"Correo enviado: {asunto}")
        except Exception as error:  # noqa: BLE001 - el detalle del servidor podría repetir datos de acceso
            print(f"El correo NO ha salido ({type(error).__name__}). Se reintentará en la próxima comprobación.")
            nuevo, codigo = {**anterior, "latido": nuevo["latido"]}, 1
        avisar_movil(entorno, asunto, texto, correo["tipo"] != "vuelta", abrir)
    print(f"Estado: {nuevo['estado']} · fallos seguidos: {nuevo['fallos']}" + (f" · causa: {nuevo['causa']}" if nuevo["causa"] else ""))
    guardar_estado(ruta, nuevo)
    return codigo, resultado is None


def ejecutar(ruta: str, entorno: dict, ahora: datetime | None = None, abrir=None, dormir=time.sleep,
             smtp=smtplib.SMTP_SSL, rondas: int = 1, separacion: int = 300) -> int:
    """`rondas` comprobaciones separadas `separacion` segundos. `ahora` fijo solo en tests."""
    faltan = [nombre for nombre in SECRETOS if not entorno.get(nombre)]
    if faltan:
        print("Faltan secretos del repositorio: " + ", ".join(faltan))
        return 1
    abrir, codigo = abrir or abridor(), 0
    for numero in range(max(1, rondas)):
        if numero:
            dormir(separacion)
            print(f"-- Comprobación {numero + 1} de {rondas}")
        momento = ahora + timedelta(seconds=numero * separacion) if ahora else datetime.now(timezone.utc)
        resultado, en_pausa = ronda(ruta, entorno, momento, abrir, dormir, smtp)
        codigo = max(codigo, resultado)
        if en_pausa:
            break
    return codigo


def _entero(texto, defecto: int, minimo: int, maximo: int) -> int:
    try:
        return min(maximo, max(minimo, int(texto)))
    except (TypeError, ValueError):
        return defecto


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--estado", default="estado.json", help="Archivo de estado entre ejecuciones")
    entorno = dict(os.environ)
    sys.exit(ejecutar(parser.parse_args().estado, entorno, rondas=_entero(entorno.get("RONDAS"), 2, 1, 3),
                      separacion=_entero(entorno.get("SEPARACION"), 300, 60, 400)))


if __name__ == "__main__":
    main()
