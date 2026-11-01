"""Tests de scripts/workflow.sh contra repositorios git locales (sin red ni GitHub).

Cada test crea un remoto vacío, lo clona superficialmente como hace actions/checkout y ejecuta el script ahí."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
SCRIPT = RAIZ / "scripts" / "workflow.sh"
GIT_AISLADO = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "Prueba",
               "GIT_AUTHOR_EMAIL": "prueba@ejemplo.com", "GIT_COMMITTER_NAME": "Prueba",
               "GIT_COMMITTER_EMAIL": "prueba@ejemplo.com", "GIT_TERMINAL_PROMPT": "0"}


@unittest.skipUnless(shutil.which("git") and shutil.which("bash"), "hacen falta git y bash")
class TestWorkflowSh(unittest.TestCase):
    def setUp(self):
        carpeta = tempfile.TemporaryDirectory()
        self.addCleanup(carpeta.cleanup)
        self.dir = Path(carpeta.name)
        self.remoto = self.dir / "remoto.git"
        self.git("init", "-q", "--bare", "-b", "main", str(self.remoto), cwd=self.dir)
        semilla = self.dir / "semilla"
        self.git("init", "-q", "-b", "main", str(semilla), cwd=self.dir)
        (semilla / "README.md").write_text("hola\n")
        self.git("add", "README.md", cwd=semilla)
        self.git("commit", "-q", "-m", "Inicio", cwd=semilla, env={"GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z",
                                                                    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z"})
        self.git("push", "-q", str(self.remoto), "main", cwd=semilla)
        self.clon = self.clonar("clon")

    def git(self, *argumentos, cwd, env=None, check=True):
        return subprocess.run(["git", *argumentos], cwd=cwd, env={**os.environ, **GIT_AISLADO, **(env or {})},
                              capture_output=True, text=True, check=check)

    def clonar(self, nombre, rama="main"):
        destino = self.dir / nombre
        self.git("clone", "-q", "--depth=1", "--branch", rama, f"file://{self.remoto}", str(destino), cwd=self.dir)
        return destino

    def script(self, *argumentos, cwd=None, env=None):
        entorno = {**os.environ, **GIT_AISLADO, "ESPERA": "0", **(env or {})}
        for variable in ("GITHUB_TOKEN", "GITHUB_ACTIONS", "HEALTHCHECK_URL"):
            if variable not in (env or {}):
                entorno.pop(variable, None)
        return subprocess.run(["bash", str(SCRIPT), *argumentos], cwd=cwd or self.clon, env=entorno,
                              capture_output=True, text=True)

    def estado_remoto(self):
        return json.loads(self.git("show", "estado:estado.json", cwd=self.remoto).stdout)

    def commits_de_estado(self):
        return int(self.git("rev-list", "--count", "estado", cwd=self.remoto).stdout)

    def test_primera_vez_sin_rama(self):
        archivo = self.dir / "estado.json"
        salida = self.script("traer", str(archivo))
        self.assertEqual(salida.returncode, 0, salida.stderr)
        self.assertIn("aún no existe", salida.stdout)
        self.assertEqual(json.loads(archivo.read_text()), {})
        archivo.write_text('{"estado": "arriba"}\n')
        salida = self.script("guardar", str(archivo))
        self.assertEqual(salida.returncode, 0, salida.stderr)
        self.assertEqual(self.estado_remoto(), {"estado": "arriba"})
        archivos = self.git("ls-tree", "--name-only", "estado", cwd=self.remoto).stdout.split()
        self.assertEqual(archivos, ["estado.json"])  # solo el estado: nada del código
        self.assertEqual(self.git("log", "-1", "--format=%an", "estado", cwd=self.remoto).stdout.strip(), "github-actions[bot]")

    def test_ciclo_completo_y_sin_commits_vacios(self):
        archivo = self.dir / "estado.json"
        self.script("traer", str(archivo))
        archivo.write_text('{"estado": "arriba"}\n')
        self.script("guardar", str(archivo))
        clon2 = self.clonar("clon2")
        archivo2 = self.dir / "estado2.json"
        self.assertEqual(self.script("traer", str(archivo2), cwd=clon2).returncode, 0)
        self.assertEqual(json.loads(archivo2.read_text()), {"estado": "arriba"})
        salida = self.script("guardar", str(archivo2), cwd=clon2)
        self.assertIn("sin cambios", salida.stdout)
        self.assertEqual(self.commits_de_estado(), 1)
        archivo2.write_text('{"estado": "abajo"}\n')
        self.assertEqual(self.script("guardar", str(archivo2), cwd=clon2).returncode, 0)
        self.assertEqual(self.estado_remoto(), {"estado": "abajo"})
        self.assertEqual(self.commits_de_estado(), 2)

    def test_no_pisa_un_estado_mas_nuevo(self):
        archivo = self.dir / "estado.json"
        self.script("traer", str(archivo))
        archivo.write_text('{"estado": "arriba"}\n')
        self.script("guardar", str(archivo))
        lento, rapido = self.dir / "lento.json", self.dir / "rapido.json"
        clon_lento, clon_rapido = self.clonar("lento"), self.clonar("rapido")
        self.script("traer", str(lento), cwd=clon_lento)
        self.script("traer", str(rapido), cwd=clon_rapido)
        rapido.write_text('{"estado": "abajo"}\n')
        self.assertEqual(self.script("guardar", str(rapido), cwd=clon_rapido).returncode, 0)
        lento.write_text('{"estado": "pausa"}\n')
        salida = self.script("guardar", str(lento), cwd=clon_lento)
        self.assertEqual(salida.returncode, 1)
        self.assertIn("No se pudo guardar", salida.stderr)
        self.assertEqual(self.estado_remoto(), {"estado": "abajo"})

    def test_remoto_inaccesible(self):
        self.git("remote", "set-url", "origin", str(self.dir / "no-existe.git"), cwd=self.clon)
        salida = self.script("traer", str(self.dir / "estado.json"))
        self.assertNotEqual(salida.returncode, 0)
        self.assertIn("No se puede consultar", salida.stderr)

    def test_latido_tras_25_dias_sin_actividad(self):
        salida = self.script("latido")
        self.assertEqual(salida.returncode, 0, salida.stderr)
        self.assertIn("Latido guardado", salida.stdout)
        self.assertEqual(self.git("log", "-1", "--format=%s", "main", cwd=self.remoto).stdout.strip(),
                         "Latido: mantiene activo el cron de GitHub")
        fecha = self.git("show", "main:.github/latido", cwd=self.remoto).stdout.strip()
        self.assertRegex(fecha, r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        salida = self.script("latido")  # el commit de ahora cuenta como actividad
        self.assertIn("sin latido", salida.stdout)

    def test_latido_solo_en_la_rama_por_defecto(self):
        self.git("push", "-q", "origin", "HEAD:refs/heads/otra", cwd=self.clon)
        salida = self.script("latido", cwd=self.clonar("otra", rama="otra"))
        self.assertIn("Latido solo en la rama por defecto", salida.stdout)
        self.assertEqual(int(self.git("rev-list", "--count", "main", cwd=self.remoto).stdout), 1)

    def test_latido_con_actividad_reciente(self):
        (self.clon / "README.md").write_text("cambio\n")
        self.git("commit", "-q", "-am", "Cambio reciente", cwd=self.clon)
        self.git("push", "-q", "origin", "main", cwd=self.clon)
        self.assertIn("sin latido", self.script("latido").stdout)

    def test_vigilante(self):
        salida = self.script("vigilante")
        self.assertEqual(salida.returncode, 0)
        self.assertIn("Sin HEALTHCHECK_URL", salida.stdout)
        salida = self.script("vigilante", env={"HEALTHCHECK_URL": "http://hc-ping.com/uuid"})
        self.assertEqual(salida.returncode, 0)
        self.assertIn("debe empezar por https://", salida.stderr)

    def test_vigilante_avisa_del_resultado(self):
        registro = self.dir / "curl.log"
        falso = self.dir / "bin"
        falso.mkdir()
        (falso / "curl").write_text(f'#!/bin/sh\nfor a; do last="$a"; done\necho "$last" >> "{registro}"\n')
        (falso / "curl").chmod(0o755)
        ruta = f"{falso}{os.pathsep}{os.environ.get('PATH', '')}"
        for resultado in ("success", "failure", "cancelled"):
            salida = self.script("vigilante", env={"HEALTHCHECK_URL": "https://hc-ping.com/abc/", "RESULTADO": resultado, "PATH": ruta})
            self.assertEqual(salida.returncode, 0, salida.stderr)
        self.assertEqual(registro.read_text().split(), ["https://hc-ping.com/abc", "https://hc-ping.com/abc/fail",
                                                        "https://hc-ping.com/abc/fail"])

    def test_anterior_sin_token_no_imprime_nada(self):
        salida = self.script("anterior")
        self.assertEqual((salida.returncode, salida.stdout), (0, ""))

    def test_anterior_con_api(self):
        falso = self.dir / "bin"
        falso.mkdir()
        respuesta = json.dumps({"workflow_runs": [{"id": 2, "conclusion": "success", "updated_at": "2026-10-24T00:10:00Z"},
                                                  {"id": 1, "conclusion": "success", "updated_at": "2026-10-24T00:05:50Z"}]})
        (falso / "curl").write_text(f"#!/bin/sh\necho \"$@\" > \"{self.dir / 'args'}\"\ncat <<'FIN'\n{respuesta}\nFIN\n")
        (falso / "curl").chmod(0o755)
        salida = self.script("anterior", env={"GITHUB_TOKEN": "t", "GITHUB_REPOSITORY": "alguien/repo", "GITHUB_RUN_ID": "2",
                                              "GITHUB_WORKFLOW_REF": "alguien/repo/.github/workflows/monitor.yml@refs/heads/main",
                                              "PATH": f"{falso}{os.pathsep}{os.environ.get('PATH', '')}"})
        self.assertEqual(salida.returncode, 0, salida.stderr)
        self.assertEqual(salida.stdout.strip(), "2026-10-24T00:05:50Z")
        self.assertIn("/repos/alguien/repo/actions/workflows/monitor.yml/runs?status=completed",
                      (self.dir / "args").read_text())

    def test_uso(self):
        self.assertEqual(self.script().returncode, 2)
        self.assertEqual(self.script("guardar").returncode, 2)
        self.assertEqual(self.script("borrar").returncode, 2)


if __name__ == "__main__":
    unittest.main()
