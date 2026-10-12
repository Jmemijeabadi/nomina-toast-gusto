"""Guarda las decisiones humanas de forma que sobrevivan al redeploy.

El problema concreto: en Streamlit Cloud el disco del contenedor es temporal.
Si quien usa la herramienta confirma una identidad con un clic, el cambio se
escribe en employee_map.csv y se pierde en el siguiente redeploy o reinicio.
Para una nomina cada dos semanas eso significa reconfirmar todo cada vez, que es
justamente lo que los botones venian a quitar.

Asi que hay dos destinos y se usan los dos:

  el archivo local   para que la corrida en curso ya vea el cambio
  GitHub             para que siga ahi la proxima vez

El commit a GitHub necesita un token en los secrets. Sin token la herramienta
sigue funcionando: guarda local y avisa que hay que respaldar el mapeo, porque
callarse eso seria dejar que el trabajo de alguien se borre en silencio.

Beneficio aparte, que para nomina importa: el historial de git queda como
registro de quien confirmo que y cuando.
"""

from __future__ import annotations

import base64
import csv
import json
import os
import urllib.error
import urllib.parse
import urllib.request

from build_maps import EMPLOYEE_MAP_COLUMNS

CONFIRMADO_SI = "si"
CONFIRMADO_NO = "no"

# Valores que cuentan como confirmacion afirmativa. El exportador ya acepta
# varias formas; aqui se escribe siempre "si" para no agregar variantes.
AFIRMATIVOS = ("si", "s", "yes", "true", "1", "sí")


def leer(path: str) -> list:
    """Las filas del mapeo, tal cual estan en el archivo."""
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def escribir(path: str, filas: list) -> None:
    """Reescribe el mapeo conservando el orden de columnas.

    Se escribe a un temporal y se reemplaza, para que una falla a medio escribir
    no deje el mapeo truncado: ese archivo es donde viven las confirmaciones de
    un humano y perderlo cuesta horas de trabajo ajeno.
    """
    columnas = list(EMPLOYEE_MAP_COLUMNS)
    for fila in filas:
        for col in fila:
            if col not in columnas:
                columnas.append(col)

    temporal = path + ".tmp"
    with open(temporal, "w", newline="", encoding="utf-8") as handle:
        escritor = csv.DictWriter(handle, fieldnames=columnas, extrasaction="ignore")
        escritor.writeheader()
        for fila in filas:
            escritor.writerow({c: fila.get(c, "") for c in columnas})
    os.replace(temporal, path)


def confirmar_persona(path: str, toast_guids, es_la_misma: bool) -> int:
    """Marca si varios registros de Toast son la MISMA persona.

    De eso depende si sus horas se suman para el overtime de California, asi que
    no es cosmetico: son horas que se pagan a 1.5x o no.
    """
    objetivo = set(toast_guids)
    filas = leer(path)
    tocadas = 0
    for fila in filas:
        if (fila.get("toast_guid") or "").strip() in objetivo:
            fila["persona_confirmada"] = CONFIRMADO_SI if es_la_misma else CONFIRMADO_NO
            if (fila.get("accion") or "").strip().upper() == "CONFIRMAR":
                fila["accion"] = ""
            tocadas += 1
    if tocadas:
        escribir(path, filas)
    return tocadas


def asignar_gusto_id(path: str, toast_guid: str, gusto_employee_id: str,
                     gusto_name: str = "") -> bool:
    """Liga un registro de Toast con un empleado de Gusto."""
    filas = leer(path)
    for fila in filas:
        if (fila.get("toast_guid") or "").strip() == toast_guid.strip():
            fila["gusto_employee_id"] = gusto_employee_id.strip()
            if gusto_name:
                fila["gusto_name"] = gusto_name
            fila["accion"] = ""
            fila["confianza"] = "confirmado a mano"
            escribir(path, filas)
            return True
    return False


def excluir(path: str, toast_guid: str, motivo: str = "") -> bool:
    """Marca un registro de Toast como algo que no se paga.

    Para cuentas de dispositivo, de prueba o de pedidos en linea: no son
    personas y no deben aparecer como pendientes en cada corrida.
    """
    filas = leer(path)
    for fila in filas:
        if (fila.get("toast_guid") or "").strip() == toast_guid.strip():
            fila["accion"] = "EXCLUIR"
            fila["gusto_employee_id"] = ""
            if motivo:
                fila["nota"] = motivo
            escribir(path, filas)
            return True
    return False


# ─────────────────────────── respaldo en GitHub ───────────────────────────


def _config_github(secrets=None) -> dict:
    """Lee la config del respaldo. Devuelve {} si no esta configurado."""
    fuente = secrets if secrets is not None else os.environ
    def leer_clave(nombre):
        try:
            valor = fuente.get(nombre) or ""
        except Exception:
            valor = ""
        return str(valor).strip()

    token = leer_clave("GITHUB_TOKEN")
    repo = leer_clave("GITHUB_REPO")
    if not (token and repo):
        return {}
    return {"token": token, "repo": repo,
            "rama": leer_clave("GITHUB_BRANCH") or "main"}


def respaldo_configurado(secrets=None) -> bool:
    return bool(_config_github(secrets))


def _api(ruta: str, cfg: dict, metodo: str = "GET", cuerpo: dict | None = None):
    url = f"https://api.github.com/repos/{cfg['repo']}/{ruta.lstrip('/')}"
    datos = json.dumps(cuerpo).encode() if cuerpo is not None else None
    peticion = urllib.request.Request(url, data=datos, method=metodo, headers={
        "Authorization": f"Bearer {cfg['token']}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
        "User-Agent": "nomina-toast-gusto",
    })
    with urllib.request.urlopen(peticion, timeout=30) as respuesta:
        return json.load(respuesta)


def publicar(path: str, mensaje: str, secrets=None) -> dict:
    """Sube el mapeo a GitHub para que la confirmacion sobreviva al redeploy.

    Devuelve {"ok": bool, "detalle": str}. Nunca lanza: si el respaldo falla, la
    nomina debe poder seguir, pero quien opera tiene que enterarse de que su
    confirmacion solo quedo en local.
    """
    cfg = _config_github(secrets)
    if not cfg:
        return {"ok": False, "detalle":
                "No hay respaldo configurado. La confirmacion quedo solo en este "
                "servidor y se puede perder al reiniciar: descarga el mapeo y "
                "guardalo, o configura GITHUB_TOKEN y GITHUB_REPO en los secrets."}

    nombre = os.path.basename(path)
    with open(path, "rb") as handle:
        contenido = base64.b64encode(handle.read()).decode()

    ruta_api = f"contents/{urllib.parse.quote(nombre)}"
    try:
        actual = _api(f"{ruta_api}?ref={urllib.parse.quote(cfg['rama'])}", cfg)
        sha = actual.get("sha")
    except urllib.error.HTTPError as error:
        if error.code == 404:
            sha = None          # el archivo aun no existe en la rama
        elif error.code in (401, 403):
            return {"ok": False, "detalle":
                    f"GitHub rechazo el token ({error.code}). Revisa que siga "
                    f"vigente y que tenga permiso de escritura de contenido en "
                    f"{cfg['repo']}."}
        else:
            return {"ok": False, "detalle": f"GitHub respondio {error.code} al leer {nombre}."}
    except Exception as error:
        return {"ok": False, "detalle": f"No se pudo hablar con GitHub: {error}"}

    cuerpo = {"message": mensaje, "content": contenido, "branch": cfg["rama"]}
    if sha:
        cuerpo["sha"] = sha
    try:
        resultado = _api(ruta_api, cfg, metodo="PUT", cuerpo=cuerpo)
    except urllib.error.HTTPError as error:
        if error.code == 409:
            return {"ok": False, "detalle":
                    "Alguien mas cambio el mapeo en GitHub al mismo tiempo. "
                    "Recarga la pagina y vuelve a confirmar."}
        return {"ok": False, "detalle": f"GitHub respondio {error.code} al guardar."}
    except Exception as error:
        return {"ok": False, "detalle": f"No se pudo guardar en GitHub: {error}"}

    commit = (resultado.get("commit") or {}).get("sha") or ""
    return {"ok": True, "detalle":
            f"Guardado en {cfg['repo']} ({cfg['rama']}), commit {commit[:7]}."}


def pendientes(path: str) -> dict:
    """Cuenta lo que falta decidir en el mapeo, para mostrarlo sin releer avisos."""
    filas = leer(path)
    sin_id = [f for f in filas
              if not (f.get("gusto_employee_id") or "").strip()
              and (f.get("accion") or "").strip().upper() != "EXCLUIR"]
    por_confirmar = [f for f in filas
                     if (f.get("accion") or "").strip().upper() == "CONFIRMAR"]
    # Solo pide confirmacion quien comparte person_key con otro registro: una
    # persona con un unico registro no tiene nada que confirmar, y contarla
    # convertia "3 identidades por confirmar" en "77", que es ruido.
    por_clave = {}
    for fila in filas:
        clave = (fila.get("person_key") or "").strip()
        if clave:
            por_clave.setdefault(clave, []).append(fila)
    personas_sin_confirmar = [
        fila for grupo in por_clave.values() if len(grupo) > 1
        for fila in grupo
        if (fila.get("persona_confirmada") or "").strip().lower()
        not in AFIRMATIVOS + (CONFIRMADO_NO,)]
    return {"sin_gusto_id": sin_id, "por_confirmar": por_confirmar,
            "personas_sin_confirmar": personas_sin_confirmar,
            "total": len(filas)}
