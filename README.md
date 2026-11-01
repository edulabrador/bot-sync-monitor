# bot-sync-monitor

Vigilante externo y gratuito («dead man's switch») para un bot de **WhatsApp Cloud API** que corre en tu
propio ordenador detrás de un túnel cuya dirección cambia en cada arranque (por ejemplo, un túnel rápido de
Cloudflare). Te avisa por **correo** y, si quieres, con una **notificación push** en el móvil cuando el bot
deja de responder: ordenador apagado o suspendido, programa cerrado, sin internet, túnel caído o Meta
apuntando a una dirección que ya no existe. Un bot no puede avisar de su propia caída: esto corre fuera.

- **Coste: 0 €.** GitHub Actions es gratis en repositorios públicos, ntfy no necesita cuenta, Google Cloud
  Scheduler (opcional) incluye 3 tareas gratis al mes y healthchecks.io (opcional) tiene plan gratuito.
- **Sin dependencias:** un script de Python (`monitor.py`, solo biblioteca estándar), un script de shell para
  los pasos de git y un workflow.
- **Sin datos tuyos en el código ni en el historial:** todo lo privado va en secretos cifrados de GitHub.
- **Con tests:** `python -m unittest discover -s tests -v` (también corren solos en cada push).
- Inspirado en [Upptime](https://github.com/upptime/upptime), reducido a lo mínimo para direcciones dinámicas.

## Cómo funciona

```
Cloud Scheduler o cron de GitHub ──► GitHub Actions ──► monitor.py
                                                         1. Pregunta a Meta a qué dirección manda los mensajes
                                                         2. Llama a esa dirección en /salud
                                                         3. Compara con el último estado y avisa solo si cambia
```

1. Pregunta a Meta, **solo lectura**, la suscripción de tu app (`GET /{APP_ID}/subscriptions`) y toma la
   dirección activa de WhatsApp (`whatsapp_business_account`) con el campo `messages`. Las suscripciones de
   Messenger o Instagram, que también tienen ese campo, no cuentan.
2. Llama a esa dirección cambiando `/whatsapp` por `/salud` (3 intentos separados 20 segundos).
3. Si falla, lo **confirma 2 minutos después** en la misma ejecución (evita falsas alarmas por cortes de un
   minuto). Avisa **solo cuando cambia el estado**:
   - 🔴 «El bot no responde», con la causa probable y qué hacer.
   - 🔴 «El bot sigue sin responder»: recordatorios a la 1 h y a las 3 h, cada 6 h el resto del primer día
     (9 h, 15 h y 21 h) y después **uno al día**.
   - 🟢 «El bot vuelve a responder», con cuánto tiempo estuvo caído (y qué le pasó al ordenador, si el bot
     lo cuenta: ver «Eventos del bot»).
   - 🔵 «No puedo comprobar el bot» si Meta no contesta al monitor durante 30 minutos. **No cuenta como
     caída**: sin Meta no se sabe si el bot responde, y casi siempre es un fallo pasajero de Meta o de GitHub.

**Avisos de un vistazo.** El correo lleva todo el detalle; el móvil, dos líneas (qué pasa y qué hacer) con la
hora delante, y al pulsarlo abre el historial del monitor. Las fechas se escriben como las dirías: «hoy a las
12:15», «ayer a las 12:15», «el lunes 19 a las 12:15». En las horas de silencio (por defecto de 00:00 a 08:00,
hora de Madrid) los recordatorios y los verdes llegan por correo sin sonar en el móvil; el primer rojo suena
siempre. Todos los correos de una misma caída citan al primero, así que el programa de correo los agrupa en un
hilo (Gmail agrupa también por asunto, así que allí puede que se vean sueltos).

**«Ya lo sé».** Los avisos rojos del móvil traen un botón: si lo pulsas, el monitor entiende que lo sabes,
deja de mandarte pushes de esa caída y solo te escribe un correo al día hasta que el bot vuelva.

**Si el monitor estuvo parado** (workflow desactivado, token caducado o retrasos del cron), el aviso verde no
se inventa la duración: dice «estuvo caído entre 2 h y 8 d» y desde cuándo no hubo comprobaciones.

## Qué necesita tu bot

Un único punto de salud público junto al webhook:

| Petición | Respuesta si va bien | Respuesta si está parado por dentro |
|---|---|---|
| `GET https://<tu-direccion>/salud` | `200` con `{"estado": "ok"}` | `503` con `{"estado": "sin worker"}` |

Si el webhook de Meta termina en `/whatsapp`, el monitor llama a la misma dirección con `/salud`. Si termina
en otra ruta, añade `/salud` al final de esa ruta. Cualquier otra respuesta, o ninguna, cuenta como caída.

## Puesta en marcha (unos 20 minutos)

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
| `SMTP_PUERTO` | `465` (SSL) o `587` (STARTTLS) | Tu proveedor. Sin él, `465`. Office 365, por ejemplo, usa `587` |
| `SMTP_USUARIO` | Buzón que envía los avisos | Un buzón tuyo, mejor uno solo para avisos |
| `SMTP_CLAVE` | Contraseña de ese buzón | La del buzón o una contraseña de aplicación |
| `AVISOS_EMAIL` | Quién recibe los avisos | Tu correo |

Opcionales, para personalizar los avisos sin que el texto quede en el código público:

| Secreto | Para qué |
|---|---|
| `NOMBRE_BOT` | Nombre que aparece en la cabecera del correo (por defecto «Bot de mensajería») |
| `COMO_ARRANCAR` | Qué tienes que abrir para encender el bot (p. ej. «el acceso directo del escritorio») |
| `NTFY_TEMA` | Avisos push al móvil con el botón «Ya lo sé» (paso 4) y eventos del bot |
| `HEALTHCHECK_URL` | Aviso si el propio monitor deja de ejecutarse (paso 6) |

La clave de la app solo se usa para **leer** la suscripción; el monitor nunca cambia nada en Meta. Con cualquier
puerto que no sea el 465 se exige STARTTLS: la clave del buzón nunca viaja sin cifrar.

### 3. Pruébalo

**Actions → Bot sync monitor → Run workflow.** Con el bot encendido debe terminar en verde, decir
«El bot responde (\*\*\*.trycloudflare.com)» y no mandar nada. La primera ejecución crea la rama `estado`, donde
se guarda el último estado conocido. Para probar el aviso, apaga el bot y lánzalo: tarda unos 3 minutos porque
confirma el fallo, y llega el correo rojo. Enciéndelo y lánzalo otra vez: llega el verde.

### 4. Avisos push al móvil (opcional, gratis, sin cuenta)

1. Inventa un tema largo y aleatorio, de **20 a 60** letras, números, `-` o `_` (el monitor usa además el tema
   `<tema>-ctl`, y ntfy no admite más de 64 caracteres). **Es la contraseña de tus avisos**: quien lo conozca
   puede leerlos y pulsar «Ya lo sé» por ti, así que no uses palabras ni nombres.
2. Instala la app **ntfy** (Android o iPhone), pulsa **+** y suscríbete a ese tema en `ntfy.sh`.
3. Guárdalo como secreto `NTFY_TEMA`.

Todas las notificaciones van con la misma prioridad y la hora delante, para que el móvil las ordene por hora.

### 5. Lanzamiento fiable cada 5 minutos (recomendado, gratis)

El cron de GitHub no es fiable: en repositorios nuevos o poco activos puede saltarse la mayoría de las
ejecuciones (en una prueba real hizo 2 de 66 en una noche, y unas 6 al día de las 144 programadas durante una
semana). Se queda como respaldo y **Google Cloud Scheduler** lanza el monitor cada 5 minutos exactos. La capa
gratuita incluye 3 tareas al mes por cuenta de facturación y no gasta créditos; necesitas un proyecto de Google
Cloud con facturación activada.

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

Pon en tu calendario renovar el token antes de que caduque (el paso 6 te avisa si se te olvida).

### 6. Vigila al vigilante (recomendado, gratis)

Si el monitor deja de ejecutarse (token de Cloud Scheduler caducado, workflow desactivado, fallo de GitHub),
nadie te avisaría. [healthchecks.io](https://healthchecks.io) lo resuelve: cada ejecución le manda una señal y,
si deja de llegar, te escribe él.

1. Crea una cuenta gratuita y pulsa **Add Check**.
2. Con Cloud Scheduler: **Period** 5 minutos y **Grace Time** 30 minutos. Solo con el cron de GitHub, que es
   irregular: **Period** 1 hora y **Grace Time** 6 horas.
3. Copia la **Ping URL** (`https://hc-ping.com/…`) y guárdala como secreto `HEALTHCHECK_URL`.

Si una ejecución termina en error, el monitor manda la señal de fallo y healthchecks.io también te avisa. Si
desactivas el monitor a propósito, pausa también el check en healthchecks.io.

## Eventos del bot (opcional): un solo aviso por incidente

Si tu bot puede hacer una petición HTTP, que avise al monitor dejando un mensaje en el tema privado
`<NTFY_TEMA>-ctl` de ntfy (no lo suscribas en el móvil: es solo para el monitor). Cuerpo de la petición
`POST https://ntfy.sh/`:

```json
{"topic": "<NTFY_TEMA>-ctl", "message": "{\"tipo\": \"pausa\", \"cuando\": \"2026-10-20T20:00:00+02:00\"}"}
```

| `tipo` | Cuándo lo envía el bot | Qué hace el monitor |
|---|---|---|
| `pausa` | Al cerrarlo tú a propósito (o al pulsar «Ya lo sé» en el móvil) | No manda alertas de caída; un correo al día si sigue apagado; el verde, sin sonar |
| `arranque` | Al encenderse, con `"causa"`: qué le pasó al ordenador | Quita la pausa y pone esa causa en el aviso verde |
| `caida_avisada` | Si se detiene por un error y ya te avisó él | No repite el aviso rojo; los recordatorios y el verde siguen |

- `cuando` mejor con zona horaria (`+02:00` o `Z`). Sin zona se entiende hora de Madrid. Si falta o va
  adelantada más de 5 minutos respecto a cuándo llegó a ntfy, vale la hora de ntfy.
- Los eventos se aplican en el orden en que llegaron a ntfy.
- Tras un `arranque`, el bot debe volver a responder como siempre: si aún no responde, el fallo se confirma a
  los 2 minutos igual que cualquier otro (no hay alarma instantánea por arrancar despacio).
- ntfy guarda los mensajes 12 horas. La `causa` de un arranque **no se guarda en el repositorio** (que es
  público): el monitor la vuelve a leer de ntfy hasta que el bot responde. Aun así, no envíes datos de
  clientes ni claves en estos eventos: viajan por ntfy.sh. Sin eventos, el monitor funciona igual.

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
Si tu bot envía el evento `pausa` (ver arriba), basta con cerrarlo: no hace falta pausar el monitor. Y si ya
te llegó el aviso rojo, pulsa «Ya lo sé» en el móvil.

**Horas de silencio.** Variable `HORAS_SILENCIO` (por defecto `0-8`, hora de Madrid): en esas horas los
recordatorios y los verdes no suenan en el móvil. `23-7` cruza la medianoche; `0-0` lo desactiva. Los correos
dicen las horas que tengas puestas.

**Confirmación.** Variable `CONFIRMAR`: segundos entre la primera comprobación fallida y la que la confirma
(por defecto `120`, entre `30` y `240`).

**Si cambias una clave** (la del buzón o la de la app de Meta), actualiza el secreto correspondiente. Si el
correo no sale, la ejecución termina en rojo y GitHub te avisa; el push de ese aviso sí llega y no se repite.

## Tiempos reales

Con Cloud Scheduler cada 5 minutos y la confirmación a los 2 minutos, el aviso rojo llega **unos 3-8
minutos** después de la caída (horas si solo usas el cron de GitHub). Una ejecución con el bot funcionando
dura unos segundos; con el bot caído, de 1 a 5 minutos. Un corte de uno o dos minutos no avisa.

## Seguridad y privacidad

- Ningún dato privado va en el código: credenciales, nombre de tu negocio y textos personalizados son
  secretos cifrados. GitHub los oculta en los registros y nadie, ni tú, puede volver a leerlos.
- Los registros de Actions de un repositorio público los puede leer cualquiera: el script nunca imprime
  secretos ni la dirección de tu bot. De un servicio de túneles compartido dice solo el servicio
  (`***.trycloudflare.com`); de un dominio propio, ni eso (`*** (dominio propio)`).
- `estado.json` (rama `estado`, pública) solo guarda fechas y contadores: a qué horas estuvo caído «un bot»,
  pero no de quién es, su dirección ni ningún texto que mande el bot.
- `monitor.py` nunca recibe el token de GitHub: el checkout no guarda credenciales y solo
  `scripts/workflow.sh` usa el token, para leer y guardar el estado y saber cuándo fue la ejecución anterior.
- El workflow del monitor no se ejecuta con *pull requests*: quien copie el repositorio no puede llegar a tus
  secretos. El de tests sí, pero no usa ningún secreto.
- Las acciones de terceros (`actions/checkout`, `actions/setup-python`) van fijadas a un commit concreto.
- No pongas datos personales en descripciones, temas ni mensajes de commit.
- Protege tu cuenta de GitHub con verificación en dos pasos: guarda la clave de tu app de Meta.

## Dónde está el estado

En la rama **`estado`**, que solo contiene `estado.json`: así el historial de `main` no se llena de
«Actualiza estado» y tus cambios nunca chocan con los del monitor. El monitor la lee al empezar cada ejecución
(nunca un estado viejo) y solo hace commit si cambia; si alguien la movió mientras tanto, no la pisa: la
ejecución termina en rojo y la siguiente parte del estado nuevo.

Para empezar de cero (por ejemplo, tras tener el monitor desactivado mucho tiempo), borra la rama `estado`: la
siguiente ejecución la vuelve a crear. Si vienes de una versión anterior, el `estado.json` de `main` ya no se
usa y la primera ejecución empieza de cero.

## Que no se apague solo

GitHub desactiva los cron de los repositorios públicos tras 60 días sin actividad. Si `main` lleva 25 días sin
ningún commit, el monitor hace uno que solo actualiza `.github/latido`. Con Cloud Scheduler da igual: lanza el
workflow por su cuenta.

## Problemas frecuentes

| Síntoma | Causa y solución |
|---|---|
| La ejecución termina en rojo y GitHub te escribe «Run failed» | Falta un secreto o el correo no sale: el registro dice cuál. El aviso pendiente se reintenta solo (salvo si ya llegó al móvil). |
| «No puedo comprobar el bot» | Meta lleva 30 minutos sin contestar al monitor: fallo pasajero, o la clave de la app cambió (actualiza `WHATSAPP_APP_SECRET`). |
| «Meta no tiene ninguna dirección activa» | El webhook de WhatsApp de la app está vacío o sin el campo `messages`: arranca el bot para que lo registre. |
| No aparecen ejecuciones | Workflow desactivado (actívalo en Actions) o la tarea de Cloud Scheduler da 401 (renueva el token). Con `HEALTHCHECK_URL`, healthchecks.io te avisa. |
| Llegan avisos estando el bot encendido | Comprueba que `/salud` responde desde fuera de tu red con `{"estado": "ok"}`. |
| «No se pudo guardar el estado» | Alguien cambió la rama `estado` durante la ejecución. La siguiente ejecución parte del estado nuevo. |

## Contenido

| Archivo | Qué es |
|---|---|
| `monitor.py` | El vigilante (Python 3.10+, solo biblioteca estándar) |
| `scripts/workflow.sh` | Leer y guardar el estado, latido y aviso a healthchecks.io |
| `.github/workflows/monitor.yml` | Cuándo y cómo se ejecuta el monitor |
| `.github/workflows/tests.yml` | Tests en cada push (Python 3.10 y 3.13) |
| `tests/` | Tests de `monitor.py` y de `scripts/workflow.sh` |
| `estado.json` (rama `estado`) | Último estado conocido (lo actualiza el propio workflow) |
