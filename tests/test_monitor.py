"""Tests de monitor.py, solo con la biblioteca estándar: python -m unittest discover -s tests -v

Toda la red (Meta, /salud, ntfy) y el correo son de mentira; nada sale a internet."""
import http.client
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
import monitor as m  # noqa: E402

T0 = datetime(2026, 10, 20, 10, 0, tzinfo=timezone.utc)  # martes 20 de octubre, 12:00 en Madrid (verano)
MIN = timedelta(minutes=1)
HORA = timedelta(hours=1)
TEMA = "tema-de-prueba-muy-largo-123"


def ok():
    return {"ok": True, "causa": "", "detalle": "", "dominio": "***.trycloudflare.com"}


def mal(causa="no_responde", detalle="HTTP 530"):
    return {"ok": False, "causa": causa, "detalle": detalle, "dominio": "" if causa == "meta" else "***.trycloudflare.com"}


class Respuesta:
    def __init__(self, status=200, cuerpo=b"", error_al_leer=None):
        self.status, self.cuerpo, self.error_al_leer = status, cuerpo, error_al_leer

    def read(self, n=-1):
        if self.error_al_leer:
            raise self.error_al_leer
        return self.cuerpo if n < 0 else self.cuerpo[:n]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _bytes(cuerpo):
    return cuerpo if isinstance(cuerpo, bytes) else json.dumps(cuerpo).encode()


class Red:
    """Internet de mentira: Meta, /salud y ntfy (pushes y tema de eventos)."""

    def __init__(self):
        self.suscripciones = [{"object": "whatsapp_business_account", "active": True,
                               "callback_url": "https://abc.trycloudflare.com/whatsapp", "fields": [{"name": "messages"}]}]
        self.meta_error = None
        self.salud = (200, {"estado": "ok"})
        self.mensajes_ctl = []
        self.pushes = []
        self.consultas_ctl = []

    def __call__(self, peticion, timeout=None):
        url = peticion.full_url
        if url.startswith("https://graph.facebook.com/"):
            if self.meta_error:
                raise self.meta_error
            return Respuesta(200, _bytes({"data": self.suscripciones}))
        if url.endswith("/salud"):
            if isinstance(self.salud, Exception):
                raise self.salud
            status, cuerpo = self.salud
            if status >= 400:
                raise urllib.error.HTTPError(url, status, "error", {}, io.BytesIO(_bytes(cuerpo)))
            return Respuesta(status, _bytes(cuerpo))
        if url == "https://ntfy.sh/":
            self.pushes.append(json.loads(peticion.data))
            return Respuesta(200, b"{}")
        if url.startswith("https://ntfy.sh/") and "/json?" in url:
            self.consultas_ctl.append(url)
            desde = url.rsplit("since=", 1)[1]
            ids = [mensaje["id"] for mensaje in self.mensajes_ctl]
            mensajes = self.mensajes_ctl[ids.index(desde) + 1:] if desde in ids else self.mensajes_ctl
            return Respuesta(200, "\n".join(json.dumps(x) for x in mensajes).encode())
        raise AssertionError(f"Petición inesperada: {url}")

    def evento(self, cuerpo, recibido=T0):
        """El bot (o el botón del móvil) deja un mensaje en el tema de eventos."""
        self.mensajes_ctl.append({"id": f"ev{len(self.mensajes_ctl) + 1:010d}", "time": int(recibido.timestamp()),
                                  "event": "message", "message": cuerpo if isinstance(cuerpo, str) else json.dumps(cuerpo)})


class Buzon:
    """Servidor SMTP de mentira: guarda conexiones y mensajes, o falla al entrar."""

    def __init__(self, falla=False):
        self.falla, self.mensajes, self.conexiones = falla, [], []

    def __call__(self, servidor, puerto, **opciones):
        conexion = _Conexion(self, servidor, puerto, opciones)
        self.conexiones.append(conexion)
        return conexion


class _Conexion:
    def __init__(self, buzon, servidor, puerto, opciones):
        self.buzon, self.servidor, self.puerto, self.opciones = buzon, servidor, puerto, opciones
        self.pasos = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def starttls(self, context=None):
        self.pasos.append("starttls")

    def login(self, usuario, clave):
        self.pasos.append("login")
        if self.buzon.falla:
            raise OSError("autenticación rechazada")

    def send_message(self, mensaje):
        self.pasos.append("send")
        self.buzon.mensajes.append(mensaje)


def entorno(**extra):
    base = {nombre: "x" for nombre in m.SECRETOS}
    base.update(SMTP_USUARIO="avisos@ejemplo.com", AVISOS_EMAIL="yo@ejemplo.com", SMTP_SERVIDOR="smtp.ejemplo.com",
                NTFY_TEMA=TEMA, HORAS_SILENCIO="0-0", GITHUB_REPOSITORY="alguien/bot-sync-monitor")
    base.update(extra)
    return {clave: valor for clave, valor in base.items() if valor is not None}


class Base(unittest.TestCase):
    def setUp(self):
        self.registro = []
        parche = mock.patch.object(m, "_decir", self.registro.append)
        parche.start()
        self.addCleanup(parche.stop)

    def dijo(self, trozo):
        return any(trozo in linea for linea in self.registro)


class Escenario:
    """Varias ejecuciones seguidas del monitor sobre el mismo estado.json."""

    def __init__(self, caso, **extra):
        carpeta = tempfile.TemporaryDirectory()
        caso.addCleanup(carpeta.cleanup)
        self.ruta = os.path.join(carpeta.name, "estado.json")
        self.red, self.buzon, self.entorno, self.dormido = Red(), Buzon(), entorno(**extra), []

    def correr(self, ahora, confirmar=120):
        return m.ejecutar(self.ruta, self.entorno, ahora=ahora, abrir=self.red, dormir=self.dormido.append,
                          smtp=self.buzon, confirmar=confirmar)

    @property
    def estado(self):
        return m.leer_estado(self.ruta)

    @property
    def archivo(self):
        with open(self.ruta, encoding="utf-8") as archivo:
            return json.load(archivo)

    def asuntos(self):
        return [mensaje["Subject"] for mensaje in self.buzon.mensajes]


# -- Horas y textos -------------------------------------------------------------------------------------------

class TestHoras(Base):
    def test_cambios_de_hora_de_la_ue(self):
        self.assertEqual(m.madrid(datetime(2026, 3, 29, 0, 59, tzinfo=timezone.utc)).utcoffset(), HORA)
        self.assertEqual(m.madrid(datetime(2026, 3, 29, 1, 0, tzinfo=timezone.utc)).utcoffset(), 2 * HORA)
        self.assertEqual(m.madrid(datetime(2026, 10, 25, 0, 59, tzinfo=timezone.utc)).utcoffset(), 2 * HORA)
        self.assertEqual(m.madrid(datetime(2026, 10, 25, 1, 0, tzinfo=timezone.utc)).utcoffset(), HORA)

    def test_desde_madrid(self):
        self.assertEqual(m.desde_madrid(datetime(2026, 7, 1, 12, 0)), datetime(2026, 7, 1, 10, 0, tzinfo=timezone.utc))
        self.assertEqual(m.desde_madrid(datetime(2026, 12, 1, 12, 0)), datetime(2026, 12, 1, 11, 0, tzinfo=timezone.utc))

    def test_cuando(self):
        self.assertEqual(m.cuando(T0, T0), "hoy a las 12:00")
        self.assertEqual(m.cuando(T0 - 24 * HORA, T0), "ayer a las 12:00")
        self.assertEqual(m.cuando(T0 - 72 * HORA, T0), "el sábado 17 a las 12:00")
        self.assertEqual(m.cuando(T0 - 8 * 24 * HORA, T0), "el lunes 12 de octubre a las 12:00")

    def test_horas_de_silencio(self):
        self.assertEqual(m.leer_silencio(""), (0, 8))
        self.assertEqual(m.leer_silencio(None), (0, 8))
        self.assertEqual(m.leer_silencio(" 23 - 7 "), (23, 7))
        self.assertEqual(m.leer_silencio("0-0"), (0, 0))
        self.assertFalse(self.registro)
        self.assertEqual(m.leer_silencio("de noche"), (0, 8))
        self.assertTrue(self.dijo("HORAS_SILENCIO no se entiende"))
        noche = datetime(2026, 10, 20, 23, 30, tzinfo=timezone.utc)  # 01:30 en Madrid
        self.assertTrue(m.en_silencio(noche, (0, 8)))
        self.assertTrue(m.en_silencio(noche, (23, 7)))
        self.assertFalse(m.en_silencio(noche, (0, 0)))
        self.assertTrue(m.en_silencio(T0, (0, 24)))
        self.assertEqual(m.horas_de_silencio((23, 7)), "de 23:00 a 07:00")
        self.assertEqual(m.horas_de_silencio((0, 0)), "")

    def test_duracion(self):
        self.assertEqual(m.duracion(timedelta()), "0 min")
        self.assertEqual(m.duracion(-HORA), "0 min")
        self.assertEqual(m.duracion(59 * MIN), "59 min")
        self.assertEqual(m.duracion(65 * MIN), "1 h 5 min")
        self.assertEqual(m.duracion(timedelta(days=1, hours=2, minutes=3)), "1 d 2 h 3 min")
        self.assertEqual(m.duracion(2 * HORA), "2 h")
        self.assertEqual(m.duracion(timedelta(days=8, minutes=4)), "8 d 4 min")

    def test_pausa_hasta(self):
        self.assertEqual(m.leer_pausa("2026-10-20 08:00", T0), datetime(2026, 10, 20, 6, 0, tzinfo=timezone.utc))
        self.assertEqual(m.leer_pausa("2026-10-20T08:00", T0), datetime(2026, 10, 20, 6, 0, tzinfo=timezone.utc))
        self.assertEqual(m.leer_pausa("2026-10-20", T0), datetime(2026, 10, 20, 22, 0, tzinfo=timezone.utc))
        self.assertIsNone(m.leer_pausa("", T0))
        self.assertIsNone(m.leer_pausa("mañana", T0))
        self.assertTrue(self.dijo("PAUSA_HASTA no se entiende"))

    def test_fechas(self):
        self.assertEqual(m._fecha("2026-10-20T10:00:00Z"), T0)
        self.assertEqual(m._fecha("2026-10-20T10:00:00.123Z"), T0.replace(microsecond=123000))
        self.assertEqual(m._fecha("2026-10-20T12:00:00"), T0)  # sin zona: hora de Madrid
        self.assertEqual(m._fecha("2026-10-20T12:00:00+02:00"), T0)
        for malo in ("", None, "ayer", 12, "2026-13-01"):
            self.assertIsNone(m._fecha(malo))


# -- Red -------------------------------------------------------------------------------------------------------

class TestSalud(Base):
    def comprobar(self, abrir):
        dormido = []
        return m.comprobar_salud("https://abc.trycloudflare.com/salud", abrir, dormido.append), dormido

    def test_ok_al_primer_intento(self):
        resultado, dormido = self.comprobar(lambda r, timeout: Respuesta(200, b'{"estado": "ok"}'))
        self.assertEqual(resultado, ("ok", ""))
        self.assertEqual(dormido, [])

    def test_tres_intentos_y_recuperacion(self):
        respuestas = iter([TimeoutError(), Respuesta(200, b'{"estado": "ok"}')])

        def abrir(peticion, timeout):
            siguiente = next(respuestas)
            if isinstance(siguiente, Exception):
                raise siguiente
            return siguiente
        resultado, dormido = self.comprobar(abrir)
        self.assertEqual(resultado, ("ok", ""))
        self.assertEqual(dormido, [m.ESPERA_ENTRE_INTENTOS])

    def test_sin_worker(self):
        def abrir(peticion, timeout):
            raise urllib.error.HTTPError(peticion.full_url, 503, "x", {}, io.BytesIO(b'{"estado": "sin worker"}'))
        resultado, dormido = self.comprobar(abrir)
        self.assertEqual(resultado[0], "sin_worker")
        self.assertEqual(len(dormido), 2)

    def test_error_http_cualquiera(self):
        def abrir(peticion, timeout):
            raise urllib.error.HTTPError(peticion.full_url, 530, "x", {}, io.BytesIO(b"<html>error 1033</html>"))
        self.assertEqual(self.comprobar(abrir)[0], ("no_responde", "HTTP 530"))

    def test_json_que_no_es_un_objeto_no_rompe_nada(self):
        for cuerpo in (b"null", b"[]", b"200", b'"ok"'):
            with self.subTest(cuerpo=cuerpo):
                resultado, _ = self.comprobar(lambda r, timeout, c=cuerpo: Respuesta(200, c))
                self.assertEqual(resultado, ("no_responde", "HTTP 200"))

                def abrir(peticion, timeout, c=cuerpo):
                    raise urllib.error.HTTPError(peticion.full_url, 503, "x", {}, io.BytesIO(c))
                self.assertEqual(self.comprobar(abrir)[0], ("no_responde", "HTTP 503"))

    def test_respuesta_cortada(self):
        resultado, _ = self.comprobar(lambda r, timeout: Respuesta(200, error_al_leer=http.client.IncompleteRead(b"")))
        self.assertEqual(resultado, ("no_responde", "IncompleteRead"))

    def test_url_de_salud(self):
        self.assertEqual(m.url_de_salud("https://a.trycloudflare.com/whatsapp"), "https://a.trycloudflare.com/salud")
        self.assertEqual(m.url_de_salud("https://a.ejemplo.com/hook/"), "https://a.ejemplo.com/hook/salud")
        self.assertEqual(m.url_de_salud("https://a.ejemplo.com/whatsapp?x=1"), "https://a.ejemplo.com/salud")

    def test_enmascarar(self):
        self.assertEqual(m.enmascarar("https://abc-def.trycloudflare.com/whatsapp"), "***.trycloudflare.com")
        self.assertEqual(m.enmascarar("https://x.ngrok-free.app/whatsapp"), "***.ngrok-free.app")
        self.assertEqual(m.enmascarar("https://bot.peluqueria-ana.es/whatsapp"), "*** (dominio propio)")
        self.assertEqual(m.enmascarar("https://trycloudflare.com.mio.es/"), "*** (dominio propio)")
        self.assertEqual(m.enmascarar(""), "(sin dirección)")
        self.assertEqual(m.enmascarar("https://[::1"), "(sin dirección)")


class TestMeta(Base):
    def direccion(self, datos=None, error=None):
        def abrir(peticion, timeout):
            if error:
                raise error
            return Respuesta(200, _bytes(datos))
        return m.direccion_de_meta("123", "secreto-de-la-app", abrir)

    @staticmethod
    def suscripcion(url, objeto="whatsapp_business_account", activa=True, campos=({"name": "messages"},)):
        datos = {"active": activa, "callback_url": url, "fields": list(campos)}
        if objeto is not None:
            datos["object"] = objeto
        return datos

    def test_elige_whatsapp_aunque_messenger_vaya_antes(self):
        datos = {"data": [self.suscripcion("https://otro.ejemplo.com/messenger", objeto="page"),
                          self.suscripcion("https://a.trycloudflare.com/whatsapp")]}
        self.assertEqual(self.direccion(datos)[0], "https://a.trycloudflare.com/whatsapp")

    def test_solo_messenger_no_vale(self):
        datos = {"data": [self.suscripcion("https://otro.ejemplo.com/messenger", objeto="page")]}
        self.assertEqual(self.direccion(datos)[1], "sin_suscripcion")

    def test_sin_producto_se_acepta_como_ultimo_recurso(self):
        datos = {"data": [self.suscripcion("https://a.trycloudflare.com/whatsapp", objeto=None, campos=("messages",))]}
        self.assertEqual(self.direccion(datos)[0], "https://a.trycloudflare.com/whatsapp")

    def test_descarta_inactivas_sin_https_y_sin_messages(self):
        datos = {"data": [self.suscripcion("https://a.trycloudflare.com/whatsapp", activa=False),
                          self.suscripcion("http://a.trycloudflare.com/whatsapp"),
                          self.suscripcion("https://a.trycloudflare.com/whatsapp", campos=({"name": "statuses"},))]}
        self.assertEqual(self.direccion(datos)[1], "sin_suscripcion")

    def test_respuestas_raras(self):
        for datos in (None, [], {"data": None}, {"data": [None, 3, "x"]}, {"data": [{"fields": None, "active": True}]}):
            with self.subTest(datos=datos):
                self.assertEqual(self.direccion(datos)[1], "sin_suscripcion")

    def test_error_sin_filtrar_la_credencial(self):
        error = urllib.error.HTTPError("https://graph.facebook.com/?access_token=123|secreto-de-la-app", 400, "x", {}, io.BytesIO(b""))
        url, causa, detalle = self.direccion(error=error)
        self.assertEqual((url, causa, detalle), ("", "meta", "HTTP 400"))
        self.assertEqual(self.direccion(error=TimeoutError())[2], "TimeoutError")


class TestEjecucionAnterior(Base):
    def test_salta_la_actual_y_las_canceladas(self):
        respuesta = {"workflow_runs": [
            {"id": 9, "conclusion": "success", "updated_at": "2026-10-24T00:10:00Z"},
            {"id": 8, "conclusion": "cancelled", "updated_at": "2026-10-24T00:06:00Z"},
            {"id": 7, "conclusion": "failure", "updated_at": "2026-10-24T00:05:50Z"},
        ]}
        self.assertEqual(m.ejecucion_anterior(respuesta, "9"), "2026-10-24T00:05:50Z")
        self.assertEqual(m.ejecucion_anterior(respuesta, ""), "2026-10-24T00:10:00Z")

    def test_respuestas_raras(self):
        for respuesta in (None, [], {"workflow_runs": None}, {"workflow_runs": [{"id": 1, "conclusion": "success", "updated_at": "ayer"}]}):
            self.assertEqual(m.ejecucion_anterior(respuesta, "2"), "")

    def test_desde_la_linea_de_ordenes(self):
        respuesta = json.dumps({"workflow_runs": [{"id": 5, "conclusion": "success", "updated_at": "2026-10-24T00:05:50Z"}]})
        salida = subprocess.run([sys.executable, str(RAIZ / "monitor.py"), "--ejecucion-anterior"], input=respuesta,
                                capture_output=True, text=True, env={"GITHUB_RUN_ID": "6", "PATH": os.environ.get("PATH", "")},
                                check=True)
        self.assertEqual(salida.stdout.strip(), "2026-10-24T00:05:50Z")
        salida = subprocess.run([sys.executable, str(RAIZ / "monitor.py"), "--ejecucion-anterior"], input="no es json",
                                capture_output=True, text=True, check=True)
        self.assertEqual(salida.stdout.strip(), "")


# -- Eventos del bot ---------------------------------------------------------------------------------------------

class TestEventos(Base):
    def leer(self, red, desde_id="", **extra):
        return m.leer_eventos(entorno(**extra), desde_id, red)

    def test_se_aplican_en_el_orden_de_llegada_aunque_las_zonas_difieran(self):
        red = Red()
        red.evento({"tipo": "arranque", "cuando": "2026-10-20T21:30:00+02:00"}, recibido=datetime(2026, 10, 20, 19, 30, tzinfo=timezone.utc))
        red.evento({"tipo": "pausa", "cuando": "2026-10-20T20:00:00+00:00"}, recibido=datetime(2026, 10, 20, 20, 0, tzinfo=timezone.utc))
        eventos, ultimo, _ = self.leer(red)
        self.assertEqual([e["tipo"] for e in eventos], ["arranque", "pausa"])
        self.assertEqual(ultimo, "ev0000000002")
        self.assertEqual(m.aplicar_eventos(m.estado_inicial(), eventos)["pausa_bot"], "2026-10-20T20:00:00+00:00")

    def test_hora_sin_zona_es_hora_de_madrid(self):
        red = Red()
        red.evento({"tipo": "caida_avisada", "cuando": "2026-10-20T12:00:00"}, recibido=T0)
        self.assertEqual(self.leer(red)[0][0]["cuando"], "2026-10-20T10:00:00+00:00")

    def test_reloj_del_bot_adelantado_o_sin_hora(self):
        red = Red()
        red.evento({"tipo": "pausa", "cuando": "2026-10-20T15:00:00+02:00"}, recibido=T0)  # 3 h adelantado
        red.evento({"tipo": "pausa"}, recibido=T0 + MIN)
        red.evento({"tipo": "pausa", "cuando": "2026-10-20T10:03:00Z"}, recibido=T0)  # dentro del margen
        cuandos = [e["cuando"] for e in self.leer(red)[0]]
        self.assertEqual(cuandos, ["2026-10-20T10:00:00+00:00", "2026-10-20T10:01:00+00:00", "2026-10-20T10:03:00+00:00"])

    def test_ignora_lo_que_no_es_un_evento(self):
        red = Red()
        red.evento("texto suelto")
        red.evento({"tipo": "borrar_todo", "cuando": "2026-10-20T10:00:00Z"})
        red.evento("[1, 2]")
        red.mensajes_ctl.append({"id": "abierto0001", "event": "open"})
        red.evento({"tipo": "arranque", "cuando": "2026-10-20T10:00:00Z", "causa": "  Corte   de luz  "})
        eventos, ultimo, reanudar = self.leer(red)
        self.assertEqual([(e["tipo"], e["causa"]) for e in eventos], [("arranque", "Corte de luz")])
        self.assertEqual(ultimo, "ev0000000005")  # el mensaje «open» no tiene id de evento ni avanza nada
        self.assertEqual(reanudar, "ev0000000003")

    def test_reanudar_solo_si_el_ultimo_arranque_trae_causa(self):
        red = Red()
        red.evento({"tipo": "arranque", "cuando": "2026-10-20T10:00:00Z", "causa": "Windows Update"})
        self.assertEqual(self.leer(red, "")[2], "")  # el arranque es el primero: volver a leer desde el principio
        red.evento({"tipo": "arranque", "cuando": "2026-10-20T10:05:00Z"})
        self.assertIsNone(self.leer(red, "")[2])

    def test_desde_que_mensaje_se_lee(self):
        red = Red()
        self.leer(red, "abc123def456")
        self.leer(red, "no vale!")
        self.assertTrue(red.consultas_ctl[0].endswith(f"/{TEMA}-ctl/json?poll=1&since=abc123def456"))
        self.assertTrue(red.consultas_ctl[1].endswith("since=12h"))

    def test_ntfy_caido_o_tema_invalido(self):
        def roto(peticion, timeout):
            raise urllib.error.URLError("sin red")
        self.assertEqual(m.leer_eventos(entorno(), "abc123def456", roto), ([], "abc123def456", None))
        self.assertEqual(m.leer_eventos(entorno(NTFY_TEMA="a" * 64), "abc123def456", Red()), ([], "abc123def456", None))
        self.assertTrue(self.dijo("entre 20 y 60"))

    def test_boton_ya_lo_se_tras_la_recuperacion_no_silencia(self):
        evento = [{"tipo": "pausa", "cuando": m._iso(T0), "origen": "boton"}]
        self.assertEqual(m.aplicar_eventos({**m.estado_inicial(), "estado": "arriba"}, evento)["pausa_bot"], "")
        self.assertEqual(m.aplicar_eventos({**m.estado_inicial(), "estado": "abajo"}, evento)["pausa_bot"], m._iso(T0))


# -- Decisión -----------------------------------------------------------------------------------------------------

class TestDecidir(Base):
    def caer(self, inicio=T0, estado=None):
        """Dos comprobaciones fallidas seguidas: devuelve el estado «abajo» y el aviso."""
        estado, correo = m.decidir(estado or {**m.estado_inicial(), "estado": "arriba"}, mal(), inicio)
        self.assertIsNone(correo)
        self.assertEqual(estado["fallos"], 1)
        estado, correo = m.decidir(estado, mal(), inicio + 2 * MIN)
        self.assertEqual((estado["estado"], correo["tipo"]), ("abajo", "caida"))
        return estado, correo

    def test_caida_confirmada_y_recuperacion(self):
        estado, correo = self.caer()
        self.assertEqual(correo["desde"], T0)
        estado, correo = m.decidir(estado, ok(), T0 + 30 * MIN)
        self.assertEqual((estado["estado"], correo["tipo"], correo["duracion"]), ("arriba", "vuelta", 30 * MIN))
        self.assertFalse(correo["intencionado"])

    def test_un_fallo_suelto_no_avisa(self):
        estado, _ = m.decidir({**m.estado_inicial(), "estado": "arriba"}, mal(), T0)
        estado, correo = m.decidir(estado, ok(), T0 + 2 * MIN)
        self.assertIsNone(correo)
        self.assertEqual((estado["estado"], estado["fallos"]), ("arriba", 0))

    def test_recordatorios(self):
        esperados = {0: 0, 59: 0, 60: 1, 180: 2, 539: 2, 540: 3, 900: 4, 1260: 5, 2699: 5, 2700: 6, 4140: 7}
        for minutos, debidos in esperados.items():
            self.assertEqual(m.recordatorios_debidos(minutos * MIN), debidos, minutos)
        estado, _ = self.caer()
        enviados = []
        for minutos in range(5, 72 * 60, 5):
            estado, correo = m.decidir(estado, mal(), T0 + minutos * MIN)
            if correo:
                enviados.append(minutos // 60)
        self.assertEqual(enviados, [1, 3, 9, 15, 21, 45, 69])

    def test_tras_dias_sin_comprobar_solo_un_recordatorio(self):
        estado, _ = self.caer()
        estado, correo = m.decidir(estado, mal(), T0 + timedelta(days=10), ultima=T0 + HORA)
        self.assertEqual(correo["tipo"], "recordatorio")
        self.assertEqual(correo["hueco"], T0 + HORA)
        self.assertEqual(estado["recordatorios"], m.recordatorios_debidos(timedelta(days=10)))
        self.assertIsNone(m.decidir(estado, mal(), T0 + timedelta(days=10, minutes=5))[1])

    def test_el_bot_ya_aviso_de_su_caida(self):
        estado = m.aplicar_eventos({**m.estado_inicial(), "estado": "arriba"},
                                   [{"tipo": "caida_avisada", "cuando": "2026-10-20T11:58:00"}])  # hora de Madrid
        estado, _ = m.decidir(estado, mal(), T0)
        estado, correo = m.decidir(estado, mal(), T0 + 2 * MIN)
        self.assertEqual(estado["estado"], "abajo")
        self.assertIsNone(correo)
        self.assertTrue(self.dijo("ya avisó"))

    def test_apagado_avisado_y_arranque_que_aun_no_responde(self):
        estado = m.aplicar_eventos({**m.estado_inicial(), "estado": "arriba"}, [{"tipo": "pausa", "cuando": m._iso(T0)}])
        estado, correo = m.decidir(estado, mal(), T0 + 3 * MIN)
        self.assertEqual((estado["estado"], estado["fallos"], correo), ("apagado", 0, None))
        # Ocho horas después el bot arranca, pero el túnel aún no está listo: hay que confirmar, como siempre.
        estado = m.aplicar_eventos(estado, [{"tipo": "arranque", "cuando": m._iso(T0 + 8 * HORA)}])
        estado, correo = m.decidir(estado, mal(), T0 + 8 * HORA + MIN)
        self.assertIsNone(correo)
        self.assertEqual(estado["fallos"], 1)
        estado, correo = m.decidir(estado, mal(), T0 + 8 * HORA + 3 * MIN)
        self.assertEqual(correo["tipo"], "caida")
        self.assertEqual(correo["desde"], T0 + 8 * HORA + MIN)

    def test_apagado_desde_una_caida_no_arrastra_fechas_viejas(self):
        estado, _ = self.caer()
        estado = m.aplicar_eventos(estado, [{"tipo": "pausa", "cuando": m._iso(T0 + HORA), "origen": "boton"}])
        estado, correo = m.decidir(estado, mal(), T0 + HORA + 5 * MIN)
        self.assertEqual((estado["estado"], estado["desde"], correo), ("apagado", m._iso(T0), None))
        estado = m.aplicar_eventos(estado, [{"tipo": "arranque", "cuando": m._iso(T0 + timedelta(days=3))}])
        estado, correo = m.decidir(estado, mal(), T0 + timedelta(days=3, minutes=1))
        self.assertIsNone(correo)
        estado, correo = m.decidir(estado, mal(), T0 + timedelta(days=3, minutes=3))
        self.assertEqual(correo["desde"], T0 + timedelta(days=3, minutes=1))

    def test_apagado_un_correo_al_dia_y_vuelta_sin_urgencia(self):
        estado = m.aplicar_eventos({**m.estado_inicial(), "estado": "arriba"}, [{"tipo": "pausa", "cuando": m._iso(T0)}])
        estado, _ = m.decidir(estado, mal(), T0 + 3 * MIN)
        self.assertIsNone(m.decidir(estado, mal(), T0 + 23 * HORA)[1])
        estado, correo = m.decidir(estado, mal(), T0 + 24 * HORA + 3 * MIN)
        self.assertEqual((correo["tipo"], correo["desde"]), ("apagado", T0))
        estado, correo = m.decidir(estado, ok(), T0 + 30 * HORA)
        self.assertEqual(correo["tipo"], "vuelta")
        self.assertTrue(correo["intencionado"])

    def test_pausa_de_hace_un_momento_se_guarda(self):
        estado = m.aplicar_eventos({**m.estado_inicial(), "estado": "arriba"}, [{"tipo": "pausa", "cuando": m._iso(T0)}])
        estado, _ = m.decidir(estado, ok(), T0 + MIN)  # el túnel aún responde
        self.assertEqual(estado["pausa_bot"], m._iso(T0))
        estado, _ = m.decidir(estado, ok(), T0 + 25 * MIN)  # siguió funcionando: la pausa ya no vale
        self.assertEqual(estado["pausa_bot"], "")

    def test_meta_no_contesta_no_es_una_caida(self):
        estado, _ = m.decidir({**m.estado_inicial(), "estado": "arriba"}, mal(), T0)
        self.assertEqual(estado["fallos"], 1)
        enviados = []
        for minutos in range(5, 60, 5):
            estado, correo = m.decidir(estado, mal("meta", "HTTP 500"), T0 + minutos * MIN)
            enviados.append(correo and correo["tipo"])
        self.assertEqual(estado["estado"], "arriba")
        self.assertEqual(estado["fallos"], 0)  # el fallo anterior no se arrastra
        self.assertEqual([t for t in enviados if t], ["sin_comprobar"])
        self.assertEqual(enviados.index("sin_comprobar"), 6)  # a los 30 min de empezar (minuto 35)
        estado, correo = m.decidir(estado, ok(), T0 + HORA)
        self.assertEqual(correo["tipo"], "comprobando")
        self.assertEqual((estado["sin_comprobar"], estado["aviso_sin_comprobar"]), ("", ""))

    def test_meta_no_contesta_durante_una_caida(self):
        estado, _ = self.caer()
        for minutos in range(60, 300, 5):
            estado, correo = m.decidir(estado, mal("meta"), T0 + minutos * MIN)
            self.assertNotEqual(correo and correo["tipo"], "recordatorio")
        self.assertEqual((estado["estado"], estado["fallos"]), ("abajo", 2))
        estado, correo = m.decidir(estado, mal(), T0 + 300 * MIN)
        self.assertEqual(correo["tipo"], "recordatorio")

    def test_pausa_hasta(self):
        estado, _ = self.caer()
        estado, correo = m.decidir(estado, None, T0 + HORA)
        self.assertEqual((estado["estado"], correo), ("pausa", None))
        estado, correo = m.decidir(estado, ok(), T0 + 5 * HORA)
        self.assertEqual((estado["estado"], correo), ("arriba", None))

    def test_vuelta_tras_un_hueco_del_monitor(self):
        estado, _ = self.caer()
        estado, correo = m.decidir(estado, ok(), T0 + timedelta(days=8), ultima=T0 + 2 * HORA)
        self.assertEqual(correo["hueco"], T0 + 2 * HORA)
        estado, _ = self.caer(T0 + timedelta(days=9), {**estado})
        _, correo = m.decidir(estado, ok(), T0 + timedelta(days=9, hours=1), ultima=T0 + timedelta(days=9, minutes=55))
        self.assertIsNone(correo["hueco"])


# -- Textos y correo ---------------------------------------------------------------------------------------------

class TestRedactar(Base):
    def test_caida(self):
        correo = {"tipo": "caida", "desde": T0, "duracion": 2 * MIN, **mal()}
        aviso = m.redactar(correo, T0 + 2 * MIN, entorno(COMO_ARRANCAR="el icono verde"), (23, 7))
        self.assertEqual(aviso["asunto"], "🔴 El bot no responde · desde hoy a las 12:00")
        self.assertTrue(aviso["push_boton"])
        self.assertIn("de 23:00 a 07:00 llegan por correo", aviso["texto"])
        self.assertIn("el icono verde", aviso["push_texto"])
        self.assertIn("«Ya lo sé»", aviso["texto"])
        sin_silencio = m.redactar(correo, T0, entorno(NTFY_TEMA=None), (0, 0))
        self.assertNotIn("sin sonar", sin_silencio["texto"])
        self.assertNotIn("«Ya lo sé»", sin_silencio["texto"])

    def test_vuelta_con_hueco_y_contexto_escapado(self):
        correo = {"tipo": "vuelta", "desde": T0, "duracion": timedelta(days=8), "intencionado": False,
                  "contexto": "<b>Corte</b> de luz", "hueco": T0 + 2 * HORA, **ok()}
        aviso = m.redactar(correo, T0 + timedelta(days=8), entorno())
        self.assertEqual(aviso["asunto"], "🟢 El bot vuelve a responder · estuvo caído entre 2 h y 8 d")
        self.assertIn("Monitor parado: Sin comprobaciones desde el martes 20 de octubre a las 14:00", aviso["texto"])
        self.assertIn("&lt;b&gt;Corte&lt;/b&gt;", aviso["html"])
        self.assertNotIn("<b>Corte", aviso["html"])
        self.assertFalse(aviso["push_boton"])

    def test_avisos_solo_por_correo(self):
        for tipo in ("apagado", "sin_comprobar", "comprobando"):
            with self.subTest(tipo=tipo):
                aviso = m.redactar({"tipo": tipo, "desde": T0, "duracion": HORA, **mal("meta")}, T0 + HORA, entorno())
                self.assertTrue(aviso["asunto"].startswith("🔵"))
                self.assertEqual(aviso["push_titulo"], "")
                self.assertFalse(m.suena(tipo, False, T0, (0, 0)))

    def test_suena(self):
        noche = datetime(2026, 10, 20, 23, 30, tzinfo=timezone.utc)
        self.assertTrue(m.suena("caida", False, noche))
        self.assertFalse(m.suena("recordatorio", False, noche))
        self.assertTrue(m.suena("recordatorio", False, T0))
        self.assertFalse(m.suena("vuelta", True, T0))


class TestCorreo(Base):
    AVISO = {"asunto": "🔴 Prueba", "texto": "texto", "html": "<p>html</p>"}

    def test_cabeceras_y_hilo(self):
        ent = entorno()
        hilo = m.hilo_del_incidente(ent, T0)
        self.assertEqual(hilo, "<incidente.20261020T100000Z@ejemplo.com>")
        raiz = m.mensaje_de_correo(ent, self.AVISO, T0, hilo, raiz=True)
        self.assertEqual(raiz["Message-ID"], hilo)
        self.assertEqual(raiz["Date"], "Tue, 20 Oct 2026 12:00:00 +0200")
        self.assertIsNone(raiz["In-Reply-To"])
        respuesta = m.mensaje_de_correo(ent, self.AVISO, T0 + HORA, hilo)
        self.assertNotEqual(respuesta["Message-ID"], hilo)
        self.assertTrue(respuesta["Message-ID"].endswith("@ejemplo.com>"))
        self.assertEqual((respuesta["In-Reply-To"], respuesta["References"]), (hilo, hilo))
        self.assertEqual(respuesta["From"], "Monitor del bot <avisos@ejemplo.com>")
        self.assertIn("bot-sync-monitor.invalid", m.hilo_del_incidente(entorno(SMTP_USUARIO="usuario"), T0))

    def test_puerto_465_ssl_directo(self):
        buzon = Buzon()
        m.enviar(entorno(SMTP_PUERTO="465"), m.mensaje_de_correo(entorno(), self.AVISO, T0), buzon)
        conexion = buzon.conexiones[0]
        self.assertIn("context", conexion.opciones)
        self.assertEqual(conexion.pasos, ["login", "send"])

    def test_puerto_587_starttls_antes_de_la_clave(self):
        buzon = Buzon()
        m.enviar(entorno(SMTP_PUERTO="587"), m.mensaje_de_correo(entorno(), self.AVISO, T0), buzon)
        conexion = buzon.conexiones[0]
        self.assertEqual(conexion.puerto, 587)
        self.assertNotIn("context", conexion.opciones)
        self.assertEqual(conexion.pasos, ["starttls", "login", "send"])

    def test_puerto_por_defecto_y_puerto_raro(self):
        buzon = Buzon()
        m.enviar(entorno(SMTP_PUERTO=None), m.mensaje_de_correo(entorno(), self.AVISO, T0), buzon)
        self.assertEqual(buzon.conexiones[0].puerto, 465)
        with self.assertRaises(ValueError):
            m.enviar(entorno(SMTP_PUERTO="cuatro"), m.mensaje_de_correo(entorno(), self.AVISO, T0), buzon)


# -- Ejecuciones completas -------------------------------------------------------------------------------------

class TestEjecutar(Base):
    def test_bot_funcionando(self):
        e = Escenario(self)
        self.assertEqual(e.correr(T0), 0)
        self.assertEqual((e.estado["estado"], e.asuntos(), e.red.pushes, e.dormido), ("arriba", [], [], []))
        self.assertTrue(self.dijo("El bot responde (***.trycloudflare.com)"))

    def test_caida_confirmada_en_la_misma_ejecucion(self):
        e = Escenario(self)
        e.correr(T0)
        e.red.salud = urllib.error.URLError("caído")
        self.assertEqual(e.correr(T0 + 5 * MIN), 0)
        self.assertIn(120, e.dormido)
        self.assertEqual(e.estado["estado"], "abajo")
        self.assertEqual(e.asuntos(), ["🔴 El bot no responde · desde hoy a las 12:05"])
        self.assertEqual(e.buzon.mensajes[0]["Message-ID"], "<incidente.20261020T100500Z@ejemplo.com>")
        push = e.red.pushes[0]
        self.assertEqual(push["title"], "12:07 · 🔴 El bot no responde")
        self.assertEqual(push["click"], "https://github.com/alguien/bot-sync-monitor/actions")
        boton = push["actions"][0]
        self.assertEqual((boton["action"], boton["url"], boton["clear"]), ("http", f"https://ntfy.sh/{TEMA}-ctl", True))
        self.assertEqual(json.loads(boton["body"])["tipo"], "pausa")

    def test_boton_ya_lo_se(self):
        e = Escenario(self)
        e.correr(T0)
        e.red.salud = urllib.error.URLError("caído")
        e.correr(T0 + 5 * MIN)
        # El móvil publica el cuerpo del botón en el tema de eventos, como haría ntfy.
        e.red.evento(e.red.pushes[0]["actions"][0]["body"], recibido=T0 + 20 * MIN)
        e.correr(T0 + 25 * MIN)
        self.assertEqual(e.estado["estado"], "apagado")
        pushes = len(e.red.pushes)
        for minutos in range(30, 23 * 60, 5):
            e.correr(T0 + minutos * MIN)
        self.assertEqual(len(e.red.pushes), pushes)
        self.assertEqual(len(e.asuntos()), 1)
        e.correr(T0 + 25 * HORA)
        self.assertEqual(e.asuntos()[-1], "🔵 El bot sigue apagado · desde ayer a las 12:05")

    def test_correo_roto_pero_push_entregado_no_se_repite(self):
        e = Escenario(self)
        e.buzon.falla = True
        e.correr(T0)
        e.red.salud = urllib.error.URLError("caído")
        self.assertEqual(e.correr(T0 + 5 * MIN), 1)
        self.assertEqual(e.estado["estado"], "abajo")
        for minutos in (10, 15, 20):
            self.assertEqual(e.correr(T0 + minutos * MIN), 0)
        self.assertEqual(len(e.red.pushes), 1)
        self.assertTrue(self.dijo("no se repetirá"))

    def test_correo_roto_sin_push_se_reintenta(self):
        e = Escenario(self, NTFY_TEMA=None)
        e.buzon.falla = True
        e.correr(T0)
        e.red.salud = urllib.error.URLError("caído")
        self.assertEqual(e.correr(T0 + 5 * MIN), 1)
        self.assertNotEqual(e.estado["estado"], "abajo")
        e.buzon.falla = False
        self.assertEqual(e.correr(T0 + 10 * MIN), 0)
        self.assertEqual(e.estado["estado"], "abajo")
        self.assertEqual(len(e.asuntos()), 1)
        self.assertEqual(len(e.buzon.conexiones), 2)  # el intento fallido y el reintento

    def test_faltan_secretos(self):
        e = Escenario(self, SMTP_CLAVE=None)
        self.assertEqual(e.correr(T0), 1)
        self.assertTrue(self.dijo("Faltan secretos del repositorio: SMTP_CLAVE"))

    def test_salud_con_json_raro_no_rompe_la_ejecucion(self):
        e = Escenario(self)
        e.red.salud = (200, b"null")
        self.assertEqual(e.correr(T0, confirmar=30), 0)
        self.assertEqual(e.estado["estado"], "abajo")

    def test_la_causa_del_arranque_nunca_llega_a_estado_json(self):
        e = Escenario(self)
        e.correr(T0)
        e.red.salud = urllib.error.URLError("caído")
        e.correr(T0 + 5 * MIN)
        e.red.evento({"tipo": "arranque", "cuando": m._iso(T0 + HORA), "causa": "Reinicio de Windows en PC-DE-ANA"},
                     recibido=T0 + HORA)
        e.correr(T0 + HORA + 2 * MIN)  # el bot arranca, pero aún no responde
        self.assertNotIn("contexto", e.archivo)
        self.assertNotIn("PC-DE-ANA", json.dumps(e.archivo))
        self.assertEqual(e.archivo["ntfy_ultimo"], "")  # se volverá a leer el arranque
        e.red.salud = (200, {"estado": "ok"})
        e.correr(T0 + HORA + 7 * MIN)
        self.assertIn("Reinicio de Windows en PC-DE-ANA", e.buzon.mensajes[-1].get_body(("plain",)).get_content())
        self.assertEqual(e.archivo["ntfy_ultimo"], "ev0000000001")
        self.assertEqual(set(e.archivo), set(m.estado_inicial()) - {"contexto"})

    def test_meta_caida_media_hora_solo_un_correo_azul(self):
        e = Escenario(self)
        e.correr(T0)
        e.red.meta_error = urllib.error.HTTPError("https://graph.facebook.com/", 500, "x", {}, io.BytesIO(b""))
        for minutos in range(5, 65, 5):
            self.assertEqual(e.correr(T0 + minutos * MIN), 0)
        self.assertEqual(e.asuntos(), ["🔵 No puedo comprobar el bot · desde hoy a las 12:05"])
        self.assertEqual((e.red.pushes, e.dormido), ([], []))
        e.red.meta_error = None
        e.correr(T0 + 70 * MIN)
        self.assertEqual(e.asuntos()[-1], "🔵 El monitor vuelve a comprobar el bot · responde")

    def test_hueco_del_monitor(self):
        e = Escenario(self)
        e.correr(T0)
        e.red.salud = urllib.error.URLError("caído")
        e.correr(T0 + 5 * MIN)
        e.red.salud = (200, {"estado": "ok"})
        e.entorno["ULTIMA_EJECUCION"] = "2026-10-20T11:00:00Z"
        e.correr(T0 + timedelta(days=8))
        self.assertTrue(self.dijo("El monitor no se ejecutó entre 20-10-2026 13:00"))
        self.assertIn("entre", e.asuntos()[-1])

    def test_pausa_hasta(self):
        e = Escenario(self, PAUSA_HASTA="2026-10-20 18:00")
        e.red.salud = urllib.error.URLError("caído")
        self.assertEqual(e.correr(T0), 0)
        self.assertEqual((e.estado["estado"], e.asuntos(), e.dormido), ("pausa", [], []))

    def test_main_sin_secretos_sale_con_1(self):
        with tempfile.TemporaryDirectory() as carpeta:
            salida = subprocess.run([sys.executable, str(RAIZ / "monitor.py"), "--estado", os.path.join(carpeta, "e.json")],
                                    capture_output=True, text=True, env={"PATH": os.environ.get("PATH", "")})
        self.assertEqual(salida.returncode, 1)
        self.assertIn("Faltan secretos", salida.stdout)


if __name__ == "__main__":
    unittest.main()
