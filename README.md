# bot-sync-monitor

Monitor externo («dead man's switch») para un bot de WhatsApp Cloud API que corre en un ordenador propio
detrás de un túnel con dirección cambiante. Avisa por correo si el bot deja de responder: ordenador apagado
o suspendido, proceso cerrado, sin internet, túnel caído o Meta apuntando a una dirección muerta. Un bot no
puede avisar de su propia caída; esto corre fuera, en GitHub Actions. Inspirado en
[Upptime](https://github.com/upptime/upptime), reducido a un script sin dependencias.

## Cómo funciona

Cada 10 minutos, `monitor.py`:

1. Pregunta a Meta, solo lectura, a qué dirección manda los mensajes (`GET /{APP_ID}/subscriptions`).
2. Llama a esa dirección cambiando `/whatsapp` por `/salud` (espera `200 {"estado": "ok"}`): 3 intentos,
   separados 20 segundos.
3. Guarda el resultado en `estado.json` y avisa **solo cuando cambia**:
   - 🔴 «El bot no responde» tras **2 comprobaciones fallidas seguidas** (evita falsas alarmas).
   - 🔴 «El bot sigue sin responder»: un recordatorio como mucho cada 6 horas.
   - 🟢 «El bot vuelve a responder», con cuánto tiempo estuvo caído.

Causas que distingue: Meta no accesible, Meta sin dirección activa, la dirección no responde y
«/salud responde pero sin worker» (503 `{"estado": "sin worker"}`). Cada correo dice qué hacer.

**Retraso real:** GitHub retrasa los cron entre 5 y 15 minutos y hacen falta dos fallos, así que el correo
rojo llega unos **20-30 minutos** después de la caída. No sirve para caídas de un par de minutos.

**Si falla el propio monitor** (falta un secreto o el correo no sale), la ejecución termina en error y
GitHub manda su correo de «Run failed» a la cuenta del repositorio. El correo pendiente se reintenta en la
siguiente ejecución.

## Pausa (apagados intencionados)

Pausa con fin automático (hora de Madrid), para no olvidarte de reactivarlo:

```bash
gh variable set PAUSA_HASTA --body "2026-10-20 08:00"
gh variable delete PAUSA_HASTA
```

Con una fecha sola (`2026-10-20`) la pausa dura hasta el final de ese día. Al terminar vuelve a vigilar solo.
Para pararlo sin fecha: `gh workflow disable monitor.yml` y, para volver, `gh workflow enable monitor.yml`
(desde una copia del repositorio o añadiendo `-R <usuario>/bot-sync-monitor`).

## Secretos

En Settings → Secrets and variables → Actions: `WHATSAPP_APP_ID`, `WHATSAPP_APP_SECRET`, `SMTP_SERVIDOR`,
`SMTP_PUERTO`, `SMTP_USUARIO`, `SMTP_CLAVE` y `AVISOS_EMAIL`. Opcionales, para personalizar los correos sin
dejarlo en el código: `NOMBRE_BOT` (cabecera del correo) y `COMO_ARRANCAR` (qué abrir para encenderlo).
El correo sale por SMTP SSL. El script no imprime secretos ni la dirección del bot (como mucho
`***.trycloudflare.com`). El workflow no se ejecuta con pull requests, así que una copia no ve los secretos.

## Que no se apague solo

GitHub desactiva los cron de los repositorios públicos tras 60 días sin actividad. `estado.json` se
commitea cuando cambia el estado y, como mínimo, cada 25 días (campo `latido`), así que siempre hay
actividad. Si aun así se desactivara, GitHub avisa por correo y se reactiva con `gh workflow enable`.

## Coste

0 €: en repositorios públicos los minutos de GitHub Actions son gratuitos.
