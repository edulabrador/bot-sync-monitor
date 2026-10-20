# bot-sync-monitor

Vigilante externo y gratuito («dead man's switch») para un bot de **WhatsApp Cloud API** que corre en tu
propio ordenador detrás de un túnel cuya dirección cambia en cada arranque (por ejemplo, un túnel rápido de
Cloudflare). Te avisa por **correo** y, si quieres, con una **notificación push** en el móvil cuando el bot
deja de responder: ordenador apagado o suspendido, programa cerrado, sin internet, túnel caído o Meta
apuntando a una dirección que ya no existe. Un bot no puede avisar de su propia caída: esto corre fuera.

- **Coste: 0 €.** GitHub Actions es gratis en repositorios públicos, ntfy no necesita cuenta y Google Cloud
  Scheduler (opcional) incluye 3 tareas gratis al mes.
- **Sin dependencias:** un solo script de Python (`monitor.py`) y un workflow.
- **Sin datos tuyos en el código:** todo lo privado va en secretos cifrados de GitHub.
- Inspirado en [Upptime](https://github.com/upptime/upptime), reducido a lo mínimo para direcciones dinámicas.

## Cómo funciona

```
Cloud Scheduler o cron de GitHub ──► GitHub Actions ──► monitor.py
                                                         1. Pregunta a Meta a qué dirección manda los mensajes
                                                         2. Llama a esa dirección en /salud
                                                         3. Compara con estado.json y avisa solo si cambia
```

1. Pregunta a Meta, **solo lectura**, la suscripción de tu app (`GET /{APP_ID}/subscriptions`) y toma la
   dirección activa del campo `messages`.
2. Llama a esa dirección cambiando `/whatsapp` por `/salud` (3 intentos separados 20 segundos).
3. Si falla, lo **confirma 2 minutos después** en la misma ejecución (evita falsas alarmas por cortes de un
   minuto). Avisa **solo cuando cambia el estado**:
   - 🔴 «El bot no responde», con la causa probable y qué hacer.
   - 🔴 «El bot sigue sin responder»: recordatorios a la 1 h, a las 3 h y luego cada 6 h.
   - 🟢 «El bot vuelve a responder», con cuánto tiempo estuvo caído (y qué le pasó al ordenador, si el bot
     lo cuenta: ver «Eventos del bot»).

**Avisos de un vistazo.** El correo lleva todo el detalle; el móvil, dos líneas (qué pasa y qué hacer) con la
hora delante, y al pulsarlo abre el historial del monitor. Las fechas se escriben como las dirías: «hoy a las
12:15», «ayer a las 12:15», «el lunes 19 a las 12:15». De 00:00 a 08:00 (hora de Madrid) los recordatorios y los
verdes llegan por correo sin sonar en el móvil; el primer rojo suena siempre.

Distingue la causa probable: Meta no accesible, Meta sin dirección activa, la dirección no responde, o el
servidor responde pero su proceso de trabajo está parado. Cada aviso dice qué hacer.

## Qué necesita tu bot

Un único punto de salud público junto al webhook:

| Petición | Respuesta si va bien | Respuesta si está parado por dentro |
|---|---|---|
| `GET https://<tu-direccion>/salud` | `200` con `{"estado": "ok"}` | `503` con `{"estado": "sin worker"}` |

Si el webhook de Meta termina en `/whatsapp`, el monitor llama a la misma dirección con `/salud`. Si termina
en otra ruta, añade `/salud` al final de esa ruta. Cualquier otra respuesta, o ninguna, cuenta como caída.

## Puesta en marcha (unos 15 minutos)

### 1. Copia el repositorio

Pulsa **Fork** (o descarga los archivos y súbelos a un repositorio nuevo). Debe ser **público** para que
GitHub Actions sea gratis e ilimitado; en uno privado gastaría los minutos gratuitos en pocos días. No
subas nunca un archivo `.env` ni claves: todo lo privado va en el paso 2.

En tu copia, ve a **Actions** y pulsa **«I understand my workflows, go ahead and enable them»**.

### 2. Crea los secretos

**Settings → Secrets and variables → Actions → New repository secret**, uno por fila:

| Secreto | Qué es | Dónde se consigue |
|---|---|---|
| `WHATSAPP_APP_ID` | Identificador de tu app de Meta | developers.facebook.com → tu app → Configuración → Básica |
| `WHATSAPP_APP_SECRET` | Clave secreta de esa app | En la misma página («Clave secreta de la app») |
| `SMTP_SERVIDOR` | Servidor de correo saliente | Tu proveedor de correo (p. ej. `smtp.tudominio.com`) |
| `SMTP_PUERTO` | Puerto SSL | Normalmente `465` |
| `SMTP_USUARIO` | Buzón que envía los avisos | Un buzón tuyo, mejor uno solo para avisos |
| `SMTP_CLAVE` | Contraseña de ese buzón | La del buzón o una contraseña de aplicación |
| `AVISOS_EMAIL` | Quién recibe los avisos | Tu correo |

Opcionales, para personalizar los avisos sin que el texto quede en el código público:

| Secreto | Para qué |
|---|---|
| `NOMBRE_BOT` | Nombre que aparece en la cabecera del correo (por defecto «Bot de mensajería») |
| `COMO_ARRANCAR` | Qué tienes que abrir para encender el bot (p. ej. «el acceso directo del escritorio») |
| `NTFY_TEMA` | Avisos push al móvil (paso 4) y eventos del bot |

La clave de la app solo se usa para **leer** la suscripción; el monitor nunca cambia nada en Meta.

### 3. Pruébalo

**Actions → Bot sync monitor → Run workflow.** Con el bot encendido debe terminar en verde, decir
«El bot responde (\*\*\*.trycloudflare.com)» y no mandar nada. Para probar el aviso, apaga el bot y lánzalo
dos veces: llegará el correo rojo; enciéndelo y lánzalo otra vez: llegará el verde.

### 4. Avisos push al móvil (opcional, gratis, sin cuenta)

1. Inventa un tema largo y aleatorio, de 20 a 64 letras, números, `-` o `_`. **Es la contraseña de tus
   avisos**: quien lo conozca puede leerlos, así que no uses palabras ni nombres.
2. Instala la app **ntfy** (Android o iPhone), pulsa **+** y suscríbete a ese tema en `ntfy.sh`.
3. Guárdalo como secreto `NTFY_TEMA`.

Todas las notificaciones van con la misma prioridad y la hora delante, para que el móvil las ordene por hora.

### 5. Lanzamiento fiable cada 10 minutos (recomendado, gratis)

El cron de GitHub no es fiable: en repositorios nuevos o poco activos puede saltarse la mayoría de las
ejecuciones (en una prueba real hizo 2 de 66 en una noche). Se queda como respaldo y **Google Cloud
Scheduler** lanza el monitor cada 5 minutos exactos. La capa gratuita incluye 3 tareas al mes por cuenta
de facturación y no gasta créditos; necesitas un proyecto de Google Cloud con facturación activada.

1. **Token de GitHub** (https://github.com/settings/personal-access-tokens/new): tipo *fine-grained*,
   caducidad 1 año, **Only select repositories** → solo tu copia de este repositorio, permisos →
   **Actions: Read and write** y nada más. Cópialo con el icono de copiar y no lo pegues en ningún otro sitio.
2. **Tarea** (https://console.cloud.google.com/cloudscheduler, activa la API si lo pide) → **Crear tarea**:
   - Frecuencia `*/5 * * * *` (cada 5 minutos; sigue siendo una sola tarea, así que gratis), la zona horaria que quieras.
   - Destino **HTTP**, método **POST**, URL
     `https://api.github.com/repos/<tu-usuario>/<tu-repositorio>/actions/workflows/monitor.yml/dispatches`
   - Cuerpo: `{"ref":"main"}`
   - Encabezados: `Accept: application/vnd.github+json`, `Content-Type: application/json` y
     `Authorization: Bearer <tu-token>` (con un espacio tras `Bearer`).
3. Pulsa **Forzar ejecución**. En **Actions** debe aparecer una ejecución nueva al momento. Si Cloud
   Logging muestra un error 401, el token está mal pegado o ha caducado: regenéralo y vuelve a pegarlo.

Pon en tu calendario renovar el token antes de que caduque.

## Eventos del bot (opcional): un solo aviso por incidente

Si tu bot puede hacer una petición HTTP, que avise al monitor dejando un mensaje en el tema privado
`<NTFY_TEMA>-ctl` de ntfy (no lo suscribas en el móvil: es solo para el monitor). Cuerpo de la petición
`POST https://ntfy.sh/`:

```json
{"topic": "<NTFY_TEMA>-ctl", "message": "{\"tipo\": \"pausa\", \"cuando\": \"2026-10-20T20:00:00+02:00\"}"}
```

| `tipo` | Cuándo lo envía el bot | Qué hace el monitor |
|---|---|---|
| `pausa` | Al cerrarlo tú a propósito | No manda alertas de caída; un correo al día si sigue apagado; el verde, sin sonar |
| `arranque` | Al encenderse, con `"causa"`: qué le pasó al ordenador | Quita la pausa y pone esa causa en el aviso verde |
| `caida_avisada` | Si se detiene por un error y ya te avisó él | No repite el aviso rojo; los recordatorios y el verde siguen |

ntfy guarda los mensajes 12 horas; el monitor apunta lo leído en `estado.json`. No envíes datos de clientes
ni claves en estos eventos. Sin eventos, el monitor funciona igual.

## Uso diario

**Apagados intencionados.** Pausa con fin automático (hora de Madrid), para no olvidarte de reactivarlo:
**Settings → Secrets and variables → Actions → Variables → New repository variable**, nombre
`PAUSA_HASTA`, valor `2026-10-20 08:00` (o solo `2026-10-20` para todo el día). Al llegar esa hora vuelve a
vigilar solo; bórrala para reanudar antes. Desde la terminal:

```bash
gh variable set PAUSA_HASTA --body "2026-10-20 08:00" -R <tu-usuario>/<tu-repositorio>
gh variable delete PAUSA_HASTA -R <tu-usuario>/<tu-repositorio>
```

Para pararlo sin fecha: **Actions → Bot sync monitor → ⋯ → Disable workflow** (y *Enable* para volver).
Si tu bot envía el evento `pausa` (ver arriba), basta con cerrarlo: no hace falta pausar el monitor.

**Horas de silencio.** Variable `HORAS_SILENCIO` (por defecto `0-8`, hora de Madrid): en esas horas los
recordatorios y los verdes no suenan en el móvil. `23-7` cruza la medianoche; `0-0` lo desactiva.

**Si cambias una clave** (la del buzón o la de la app de Meta), actualiza el secreto correspondiente.

## Tiempos reales

Con Cloud Scheduler cada 5 minutos y la confirmación a los 2 minutos, el aviso rojo llega **unos 3-8
minutos** después de la caída (20-30 o más si solo usas el cron de GitHub). Una ejecución con el bot
funcionando dura unos segundos; con el bot caído, unos 3 minutos. Un corte de uno o dos minutos no avisa.

## Seguridad y privacidad

- Ningún dato privado va en el código: credenciales, nombre de tu negocio y textos personalizados son
  secretos cifrados. GitHub los oculta en los registros y nadie, ni tú, puede volver a leerlos.
- El script nunca imprime secretos ni la dirección de tu bot (como mucho `***.trycloudflare.com`).
- El workflow no se ejecuta con *pull requests*: quien copie el repositorio no puede llegar a tus secretos.
- La acción de terceros (`actions/checkout`) va fijada a un commit concreto.
- El repositorio es público: cualquiera puede ver el código y el historial de `estado.json` (a qué horas
  estuvo caído «un bot»), pero no de quién es el bot ni su dirección. No pongas datos personales en
  descripciones, temas ni mensajes de commit.
- Protege tu cuenta de GitHub con verificación en dos pasos: guarda la clave de tu app de Meta.

## Que no se apague solo

GitHub desactiva los cron de los repositorios públicos tras 60 días sin actividad. `estado.json` se guarda
cuando cambia el estado y, como mínimo, cada 25 días (campo `latido`), así que siempre hay actividad. Con
Cloud Scheduler da igual: lanza el workflow por su cuenta.

## Problemas frecuentes

| Síntoma | Causa y solución |
|---|---|
| La ejecución termina en rojo y GitHub te escribe «Run failed» | Falta un secreto o el correo no sale: el registro dice cuál. El aviso pendiente se reintenta solo. |
| «Meta no accesible» | Fallo pasajero de Meta, o la clave de la app cambió: actualiza `WHATSAPP_APP_SECRET`. |
| «Meta no tiene ninguna dirección activa» | El webhook de la app está vacío o sin el campo `messages`: arranca el bot para que lo registre. |
| No aparecen ejecuciones | Workflow desactivado (actívalo en Actions) o la tarea de Cloud Scheduler da 401 (renueva el token). |
| Llegan avisos estando el bot encendido | Comprueba que `/salud` responde desde fuera de tu red con `{"estado": "ok"}`. |

## Contenido

| Archivo | Qué es |
|---|---|
| `monitor.py` | El vigilante (Python 3.10+, solo biblioteca estándar) |
| `.github/workflows/monitor.yml` | Cuándo y cómo se ejecuta |
| `estado.json` | Último estado conocido (lo actualiza el propio workflow) |
