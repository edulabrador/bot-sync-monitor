#!/usr/bin/env bash
# Pasos del workflow que no son la comprobación en sí. El estado se guarda en la rama «estado» (solo el archivo
# estado.json), así el historial de main no se llena de «Actualiza estado» y tus pushes nunca chocan con él.
#
#   scripts/workflow.sh traer <archivo>     copia el estado de la rama al archivo ({} si la rama aún no existe)
#   scripts/workflow.sh guardar <archivo>   commit en la rama solo si el estado cambió (nunca pisa otro cambio)
#   scripts/workflow.sh anterior            imprime cuándo terminó la ejecución anterior (API de GitHub)
#   scripts/workflow.sh latido              commit en la rama por defecto si lleva 25 días sin ninguno
#   scripts/workflow.sh vigilante           avisa a healthchecks.io (HEALTHCHECK_URL) de que el monitor ha corrido
#
# Variables: GITHUB_TOKEN (sin él, git usa sus propias credenciales), RAMA_ESTADO («estado»), REMOTO («origin»),
# RESULTADO (estado del job para «vigilante»), ESPERA (segundos entre reintentos, 5).
set -euo pipefail

rama="${RAMA_ESTADO:-estado}"
remoto="${REMOTO:-origin}"
LATIDO_DIAS=25  # GitHub desactiva el cron de un repositorio público tras 60 días sin actividad

git_autor() {  # los commits del monitor, siempre a nombre de github-actions[bot]
  local nombre="github-actions[bot]" correo="41898282+github-actions[bot]@users.noreply.github.com"
  GIT_AUTHOR_NAME="$nombre" GIT_AUTHOR_EMAIL="$correo" GIT_COMMITTER_NAME="$nombre" GIT_COMMITTER_EMAIL="$correo" git "$@"
}

git_red() {  # git con el token solo para hablar con GitHub; el checkout no deja credenciales guardadas
  if [ -n "${GITHUB_TOKEN:-}" ]; then
    local clave
    clave=$(printf 'x-access-token:%s' "$GITHUB_TOKEN" | base64 | tr -d '\n')
    if [ "${GITHUB_ACTIONS:-}" = "true" ]; then echo "::add-mask::$clave"; fi
    git -c "http.https://github.com/.extraheader=AUTHORIZATION: basic $clave" "$@"
  else
    git "$@"
  fi
}

traer() {
  local archivo="$1" codigo=0
  git_red ls-remote --exit-code --heads "$remoto" "refs/heads/$rama" >/dev/null || codigo=$?
  case "$codigo" in
    0)
      git_red fetch -q --depth=1 "$remoto" "+refs/heads/$rama:refs/remotes/$remoto/$rama"
      git show "refs/remotes/$remoto/$rama:estado.json" > "$archivo"
      echo "Estado leído de la rama «$rama»." ;;
    2)
      echo '{}' > "$archivo"
      echo "La rama «$rama» aún no existe: se creará al guardar el primer estado." ;;
    *)
      echo "No se puede consultar la rama «$rama» (git ls-remote terminó con $codigo)." >&2
      return 1 ;;
  esac
}

guardar() {
  local archivo="$1" padre blob arbol commit intento
  padre=$(git rev-parse -q --verify "refs/remotes/$remoto/$rama^{commit}" || true)
  if [ -n "$padre" ] && git show "$padre:estado.json" 2>/dev/null | cmp -s - "$archivo"; then
    echo "Estado sin cambios."
    return 0
  fi
  blob=$(git hash-object -w "$archivo")
  arbol=$(printf '100644 blob %s\testado.json\n' "$blob" | git mktree)
  if [ -n "$padre" ]; then
    commit=$(git_autor commit-tree "$arbol" -p "$padre" -m "Actualiza estado")
  else
    commit=$(git_autor commit-tree "$arbol" -m "Estado del monitor")
  fi
  # Un push normal solo avanza la rama si nadie la ha movido desde que se leyó: nunca pisa otro estado.
  for intento in 1 2 3; do
    if git_red push -q "$remoto" "$commit:refs/heads/$rama"; then
      echo "Estado guardado en la rama «$rama»."
      return 0
    fi
    [ "$intento" = 3 ] || sleep "${ESPERA:-5}"
  done
  echo "No se pudo guardar el estado en la rama «$rama»." >&2
  return 1
}

anterior() {
  if [ -z "${GITHUB_TOKEN:-}" ] || [ -z "${GITHUB_REPOSITORY:-}" ]; then return 0; fi
  local flujo="${GITHUB_WORKFLOW_REF:-}"
  flujo="${flujo#*/.github/workflows/}"
  flujo="${flujo%%@*}"
  case "$flujo" in ''|*/*) flujo="monitor.yml" ;; esac
  curl -fsS -m 15 --retry 2 \
    -H "Authorization: Bearer $GITHUB_TOKEN" -H "Accept: application/vnd.github+json" \
    -H "X-GitHub-Api-Version: 2022-11-28" \
    "${GITHUB_API_URL:-https://api.github.com}/repos/$GITHUB_REPOSITORY/actions/workflows/$flujo/runs?status=completed&per_page=5&exclude_pull_requests=true" \
    | python3 -I "$(dirname "$0")/../monitor.py" --ejecucion-anterior \
    || echo "No se sabe cuándo fue la ejecución anterior." >&2
}

latido() {
  local actual por_defecto ultimo
  actual=$(git rev-parse --abbrev-ref HEAD)
  por_defecto=$(git_red ls-remote --symref "$remoto" HEAD | sed -n 's|^ref: refs/heads/\(.*\)[[:space:]]HEAD$|\1|p')
  if [ -z "$por_defecto" ] || [ "$actual" != "$por_defecto" ]; then
    echo "Latido solo en la rama por defecto («${por_defecto:-?}»): aquí no hace falta."
    return 0
  fi
  ultimo=$(git log -1 --format=%ct HEAD)
  if [ $(( $(date +%s) - ultimo )) -lt $(( LATIDO_DIAS * 86400 )) ]; then
    echo "Hubo actividad en «$actual» en los últimos $LATIDO_DIAS días: sin latido."
    return 0
  fi
  mkdir -p .github
  date -u +%Y-%m-%dT%H:%M:%SZ > .github/latido
  git add .github/latido
  git_autor commit -q -m "Latido: mantiene activo el cron de GitHub"
  if git_red push -q "$remoto" "HEAD:refs/heads/$actual"; then
    echo "Latido guardado."
  else
    echo "No se pudo guardar el latido: se intentará en la próxima ejecución."
  fi
}

vigilante() {
  local url="${HEALTHCHECK_URL:-}" sufijo=""
  if [ -z "$url" ]; then
    echo "Sin HEALTHCHECK_URL: nadie avisará si el monitor deja de ejecutarse."
    return 0
  fi
  case "$url" in
    https://*) ;;
    *) echo "HEALTHCHECK_URL debe empezar por https://: no se avisa." >&2; return 0 ;;
  esac
  [ "${RESULTADO:-success}" = "success" ] || sufijo="/fail"
  if curl -fsS -m 10 --retry 3 -o /dev/null "${url%/}$sufijo"; then
    echo "Vigilante avisado (${RESULTADO:-success})."
  else
    echo "No se pudo avisar al vigilante."
  fi
}

orden="${1:-}"
case "$orden" in
  traer|guardar)
    [ -n "${2:-}" ] || { echo "Uso: $0 $orden <archivo>" >&2; exit 2; }
    "$orden" "$2" ;;
  anterior|latido|vigilante)
    "$orden" ;;
  *)
    echo "Uso: $0 traer|guardar <archivo> | anterior | latido | vigilante" >&2
    exit 2 ;;
esac
