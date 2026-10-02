"""Nomina Tacos El Franc: de Toast a Gusto.

    streamlit run app.py

Tres pasos: elegir el periodo, subir los dos reportes de tips, generar los CSV.

La herramienta MUESTRA lo que hay que revisar y deja que decida quien opera: el
objetivo es llegar al resultado que se hacia a mano, no ser mas estricto que eso.
Solo se detiene en un caso: cuando detecta que entraron horas o dolares que no
salen ni al CSV ni al listado de retenidos Y no puede atribuirlos a nadie. Ahi el
archivo saldria incompleto pareciendo completo, y el operador no tendria con que
actuar.

Al desplegarla, los archivos con nombres y sueldos NO van al repo: se suben por
el navegador y viven solo en la sesion. Y lleva contrasena propia, porque las
apps de Streamlit Community Cloud son publicas por URL salvo que se restrinjan
los viewers.
"""

from __future__ import annotations

import io
import os
import re
import subprocess
import sys
from datetime import date, timedelta

import pandas as pd
import streamlit as st

import gusto_export
import toast_payroll
from toast_payroll import (ToastClient, pay_period_containing,
                           previous_closed_pay_period, resolve_locations)

st.set_page_config(page_title="Nomina Toast a Gusto", page_icon="💸", layout="wide")

CENTAVO = 0.05          # tolerancia del cierre de cuentas
HORA_TOLERANCIA = 0.05

# Archivos de configuracion que la app necesita. En local estan en el directorio;
# desplegada se suben por el navegador, para que el repo no lleve ni un nombre ni
# un sueldo.
ARCHIVOS_CONFIG = {
    "employee_map.csv": "Mapeo de empleados (Toast -> Gusto)",
    "job_map.csv": "Mapeo de jobs (Toast -> title de Gusto)",
    "gusto_template_bonita.csv": "Template de Gusto de Bonita",
    "gusto_template_gaslamp.csv": "Template de Gusto de Gaslamp",
}


def exigir_acceso() -> None:
    """Candado de la app.

    Las apps de Streamlit Community Cloud son publicas por URL salvo que se
    restrinjan los viewers. Esta muestra horas, sueldos y tips de 72 personas,
    asi que ademas de restringirlos lleva su propia contrasena: si alguien da con
    la URL, no ve nomina.
    """
    try:
        esperada = st.secrets["APP_PASSWORD"]
    except Exception:
        esperada = os.environ.get("APP_PASSWORD", "")

    if not esperada:
        st.error("**Esta app no tiene contrasena y muestra datos de nomina.**")
        st.caption(
            "Agrega `APP_PASSWORD` en los secrets antes de desplegarla. En "
            "Streamlit Cloud: Settings > Secrets. Y en Settings > Sharing, "
            "restringi quien puede verla a tu correo y al de quien la opera."
        )
        st.stop()

    if st.session_state.get("acceso_ok"):
        return

    st.title("Nomina Toast a Gusto")
    with st.form("acceso"):
        clave = st.text_input("Contrasena", type="password")
        if st.form_submit_button("Entrar"):
            if clave == esperada:
                st.session_state["acceso_ok"] = True
                st.rerun()
            else:
                st.error("Contrasena incorrecta")
    st.stop()


exigir_acceso()


def money(value: float) -> str:
    return f"${value:,.2f}"


@st.cache_resource(show_spinner=False)
def get_client() -> ToastClient:
    return ToastClient.from_env()


@st.cache_data(ttl=600, show_spinner=False)
def get_locations() -> list:
    return resolve_locations(get_client())


def periodos_recientes(cuantos: int = 8) -> list:
    """Los ultimos periodos cerrados, mas reciente primero."""
    inicio, fin, cheque = previous_closed_pay_period()
    salida = []
    for _ in range(cuantos):
        salida.append((inicio, fin, cheque))
        inicio -= timedelta(days=14)
        fin -= timedelta(days=14)
        cheque -= timedelta(days=14)
    return salida


def fechas_del_nombre(nombre: str) -> tuple | None:
    """Saca el rango del nombre del archivo que exporta Toast.

    EmployeeTipTotals-2026_09_05-2026_09_18.csv -> (2026-09-05, 2026-09-18)
    """
    encontrados = re.findall(r"(\d{4})[_-](\d{2})[_-](\d{2})", nombre or "")
    if len(encontrados) < 2:
        return None
    try:
        return (date(*map(int, encontrados[0])), date(*map(int, encontrados[1])))
    except ValueError:
        return None


# ───────────────────────────── barra lateral ─────────────────────────────

with st.sidebar:
    st.header("Estado")

    tiene_credenciales = bool(os.environ.get("TOAST_CLIENT_ID")
                              and os.environ.get("TOAST_CLIENT_SECRET"))
    if not tiene_credenciales:
        try:
            os.environ.setdefault("TOAST_CLIENT_ID", st.secrets["TOAST_CLIENT_ID"])
            os.environ.setdefault("TOAST_CLIENT_SECRET", st.secrets["TOAST_CLIENT_SECRET"])
            tiene_credenciales = True
        except Exception:
            pass

    if tiene_credenciales:
        st.success("Credenciales de Toast cargadas")
    else:
        st.error("Faltan credenciales de Toast")
        st.caption("Ponlas en `.streamlit/secrets.toml` como TOAST_CLIENT_ID y "
                   "TOAST_CLIENT_SECRET. Ver `secrets.toml.example`.")
        st.stop()

    # Configuracion: los mapeos y los templates vienen en el repo, asi que la app
    # no pide nada. El uploader existe solo para reemplazarlos sin tener que
    # commitear, por ejemplo cuando entra alguien nuevo y hay que actualizar el
    # mapeo a mitad de una quincena.
    propios = st.session_state.get("dir_config")
    if propios:
        os.environ["PAYROLL_DATA_DIR"] = propios
        toast_payroll.DATA_DIR = propios

    faltan_config = [n for n in ARCHIVOS_CONFIG
                     if not os.path.exists(toast_payroll.data_path(n))]

    st.divider()
    if faltan_config:
        st.error(f"Falta la configuracion: {', '.join(faltan_config)}")
        st.caption("Deberia venir en el repo. Subila aca para esta sesion.")
    else:
        st.caption("Configuracion cargada del repo.")

    with st.expander("Cambiar la configuracion de esta sesion", expanded=bool(faltan_config)):
        st.caption("Solo si hace falta un mapeo mas nuevo que el del repo. Lo que "
                   "subas vale para esta sesion; para que quede, hay que "
                   "commitearlo.")
        if not propios:
            import shutil
            import tempfile
            propios = tempfile.mkdtemp(prefix="payroll_cfg_")
            for nombre in ARCHIVOS_CONFIG:
                origen = toast_payroll.data_path(nombre)
                if os.path.exists(origen):
                    shutil.copy(origen, os.path.join(propios, nombre))
            st.session_state["dir_config"] = propios
            os.environ["PAYROLL_DATA_DIR"] = propios
            toast_payroll.DATA_DIR = propios

        for nombre, etiqueta in ARCHIVOS_CONFIG.items():
            subido = st.file_uploader(etiqueta, type=["csv"], key=f"cfg_{nombre}")
            if subido is not None:
                with open(os.path.join(propios, nombre), "wb") as archivo:
                    archivo.write(subido.getvalue())
                get_locations.clear()
                st.rerun()
            ruta = os.path.join(propios, nombre)
            if os.path.exists(ruta):
                st.caption(f"✅ {nombre}")

    if faltan_config:
        st.stop()
    st.divider()

    try:
        locations = get_locations()
        for location in locations:
            marca = "✅" if location["configured"] else "⚠️"
            st.write(f"{marca} {location['short']}")
            if not location["configured"]:
                st.caption("sin empresa de Gusto mapeada")
            elif not os.path.exists(location["template"]):
                st.caption(f"falta {location['template']}")
    except Exception as error:
        st.error("No se pudo hablar con Toast")
        st.code(str(error))
        st.caption("Las credenciales van en Settings > Secrets de Streamlit, sin "
                   "comillas ni espacios. La app ya limpia esos dos casos; si "
                   "sigue fallando, compara las longitudes de arriba con las de "
                   "Toast Web.")
        st.stop()

    st.divider()
    st.caption("Antes de una nomina real conviene correr los tests.")
    if st.button("Correr los tests", use_container_width=True):
        with st.spinner("Corriendo…"):
            entorno = dict(os.environ)
            # Los tests del contrato del CSV leen PAYROLL_CSV_DIR. Si no se les
            # apunta a los CSV recien generados, se SALTAN, y unittest devuelve 0
            # cuando algo se salta: el boton decia "todos pasan" sin haber
            # verificado el archivo que se va a subir.
            generados = st.session_state.get("dir_csv")
            if generados:
                entorno["PAYROLL_CSV_DIR"] = generados
            proceso = subprocess.run([sys.executable, "test_payroll.py"],
                                     capture_output=True, text=True, env=entorno)
        salida = (proceso.stderr or "") + (proceso.stdout or "")
        saltados = 0
        encontrado = re.search(r"skipped=(\d+)", salida)
        if encontrado:
            saltados = int(encontrado.group(1))

        if proceso.returncode != 0:
            st.error("Hay tests FALLANDO: no corras la nomina")
        elif saltados:
            st.warning(f"{saltados} test(s) se SALTARON, asi que no esta todo "
                       f"verificado. Genera los CSV primero y volve a correrlos.")
        else:
            st.success("Todos los tests pasan, sin saltos")
        cola = salida.strip().splitlines()[-12:]
        st.code("\n".join(cola) or "(sin salida)")


# ───────────────────────────── paso 1: periodo ─────────────────────────────

st.title("Nomina: de Toast a Gusto")

st.subheader("1. Elegi el periodo")

opciones = periodos_recientes()
etiquetas = [f"{i:%d %b} al {f:%d %b %Y}   ·   cheque {c:%d %b}"
             for i, f, c in opciones]
elegido = st.selectbox("Periodo de nomina (sabado a viernes)", range(len(opciones)),
                       format_func=lambda i: etiquetas[i])
inicio, fin, cheque = opciones[elegido]

columnas = st.columns(3)
columnas[0].metric("Arranca", f"{inicio:%d %b %Y}")
columnas[1].metric("Cierra", f"{fin:%d %b %Y}")
columnas[2].metric("Cheque", f"{cheque:%d %b %Y}")

if fin >= date.today():
    st.warning("Ese periodo todavia no cierra: hay gente fichando y las horas se "
               "van a mover. Espera al cierre.")


# ───────────────────────────── paso 2: tips ─────────────────────────────

st.subheader("2. Subi los dos reportes de tips")
st.caption("En Toast Web: **Reports → Labor → Tip management**, vista **By Day**, "
           f"con el rango **{inicio:%Y-%m-%d}** a **{fin:%Y-%m-%d}**, y descargalo. "
           "Uno por location.")

subidos = {}
columnas = st.columns(2)
for columna, location in zip(columnas, locations):
    with columna:
        st.markdown(f"**{location['short']}**")
        archivo = st.file_uploader("Reporte EmployeeTipTotals", type=["csv"],
                                   key=f"tips_{location['guid']}",
                                   label_visibility="collapsed")
        if archivo is not None:
            rango = fechas_del_nombre(archivo.name)
            if rango and rango != (inicio, fin):
                st.error(f"Ese archivo es del {rango[0]} al {rango[1]}, no del "
                         f"periodo elegido. Bajalo con el rango correcto.")
            else:
                if not rango:
                    st.caption("No pude leer las fechas del nombre; revisa que sea "
                               "el del periodo correcto.")
                subidos[location["company"]] = archivo
                st.success(f"{archivo.name}")

faltantes = [l["short"] for l in locations if l["company"] not in subidos]
if faltantes:
    st.info(f"Falta el reporte de: {', '.join(faltantes)}. Sin el, las columnas de "
            f"tips saldrian vacias.")


# ───────────────────────────── paso 3: generar ─────────────────────────────

st.subheader("3. Genera los CSV")

if st.button("Generar", type="primary", disabled=bool(faltantes),
             use_container_width=True):
    try:
        reportes = {company: gusto_export.load_tip_report(archivo)
                    for company, archivo in subidos.items()}
        with st.spinner("Leyendo Toast y calculando…"):
            resultado = gusto_export.build(get_client(), inicio, fin, reportes)
        # Los CSV se escriben a un temporal para que los tests del contrato
        # puedan verificarlos de verdad desde la barra lateral.
        import csv as _c, tempfile
        destino = tempfile.mkdtemp(prefix="payroll_")
        marca = f"{inicio:%Y%m%d}_{fin:%Y%m%d}"
        for empresa, filas in resultado["by_company"].items():
            slug = empresa.replace("tacos-franc-", "").replace("-llc", "")
            with open(os.path.join(destino, f"gusto_import_{slug}_{marca}.csv"),
                      "w", newline="", encoding="utf-8") as h:
                w = _c.DictWriter(h, fieldnames=gusto_export.GUSTO_COLUMNS,
                                  extrasaction="ignore")
                w.writeheader(); w.writerows(filas)
        st.session_state["dir_csv"] = destino
        st.session_state["resultado"] = resultado
        st.session_state["periodo"] = (inicio, fin)
    except Exception as error:
        st.session_state.pop("resultado", None)
        st.error(f"Fallo la generacion: {error}")
        st.exception(error)

resultado = st.session_state.get("resultado")
if resultado and st.session_state.get("periodo") == (inicio, fin):

    # ── cierre de cuentas: la condicion para poder descargar ──
    st.markdown("### Cierre de cuentas")

    tips_rep = resultado.get("tips_reported", 0.0)
    tips_csv = resultado.get("tips_placed", 0.0)
    tips_ret = sum(resultado.get("tips_held", {}).values())
    hueco_tips = tips_rep - tips_csv - tips_ret

    horas_ent = resultado.get("hours_reported", 0.0)
    horas_csv = resultado.get("hours_placed", 0.0)
    horas_ret = resultado.get("hours_held", 0.0)
    cuadre = pd.DataFrame([
        {"Concepto": "Horas", "Entro": f"{horas_ent:,.2f} h",
         "Al CSV": f"{horas_csv:,.2f} h", "Retenido": f"{horas_ret:,.2f} h",
         "Sin explicar": f"{horas_ent - horas_csv - horas_ret:,.2f} h"},
        {"Concepto": "Tips", "Entro": money(tips_rep),
         "Al CSV": money(tips_csv), "Retenido": money(tips_ret),
         "Sin explicar": money(hueco_tips)},
    ])
    st.dataframe(cuadre, hide_index=True, use_container_width=True)

    # El unico candado: un descuadre que la herramienta NO pudo atribuir. Todo lo
    # que si puede nombrar va a "para revisar" y lo decide quien opera: el
    # objetivo es llegar al resultado que se hacia a mano, no ser mas estricto
    # que eso.
    bloqueado = bool(resultado["problems"])

    if bloqueado:
        st.error("**No se puede generar el CSV: hay un descuadre que no se pudo "
                 "atribuir a nadie.** Si se generara, el archivo saldria incompleto "
                 "y pareceria completo.")
        for problema in resultado["problems"]:
            st.markdown(f"- {problema}")

    revisar = resultado.get("revisar") or []
    if revisar:
        st.warning(f"**{len(revisar)} cosa(s) para revisar.** No impiden generar el "
                   f"CSV; son decisiones tuyas o cosas que conviene mirar antes de "
                   f"subirlo.")
        for item in revisar:
            st.markdown(f"- {item}")

    # ── retenidos ──
    retenidos = [h for h in (resultado.get("held_back") or [])
                 if h["horas"] > 0 or h["tips"] > 0]
    if retenidos:
        st.markdown("### Gente que NO entra al CSV")
        st.caption("Hay que darla de alta en Gusto y volver a generar, o pagarle aparte.")
        st.dataframe(pd.DataFrame(retenidos)[["nombre", "horas", "tips", "motivo"]],
                     hide_index=True, use_container_width=True)

    # ── avisos ──
    if resultado.get("warnings"):
        with st.expander(f"Avisos ({len(resultado['warnings'])}) — no bloquean",
                         expanded=False):
            for aviso in resultado["warnings"]:
                st.markdown(f"- {aviso}")

    # ── resumen y descarga ──
    st.markdown("### Los CSV")
    for company, filas in sorted(resultado["by_company"].items()):
        if not filas:
            continue
        suma = lambda col: sum(float(f[col] or 0) for f in filas)
        st.markdown(f"**{company}** — {len(filas)} renglones")
        metricas = st.columns(5)
        metricas[0].metric("Regular", f"{suma('regular_hours'):,.2f} h")
        metricas[1].metric("Overtime", f"{suma('overtime_hours'):,.2f} h")
        metricas[2].metric("Double time", f"{suma('double_overtime_hours'):,.2f} h")
        metricas[3].metric("Prima de meal", f"{suma('missed_break_hours'):,.2f} h")
        metricas[4].metric("Tips", money(suma('paycheck_tips') + suma('cash_tips')
                                         + suma('custom_earning_distributed_service_charges')))

        tabla = pd.DataFrame(filas)[[
            "last_name", "first_name", "title", "regular_hours", "overtime_hours",
            "double_overtime_hours", "missed_break_hours", "paycheck_tips",
            "cash_tips", "custom_earning_distributed_service_charges"]]
        with st.expander("Ver los renglones", expanded=False):
            st.dataframe(tabla, hide_index=True, use_container_width=True)

        if not bloqueado:
            buffer = io.StringIO()
            import csv as _csv
            escritor = _csv.DictWriter(buffer, fieldnames=gusto_export.GUSTO_COLUMNS,
                                       extrasaction="ignore")
            escritor.writeheader()
            escritor.writerows(filas)
            slug = company.replace("tacos-franc-", "").replace("-llc", "")
            st.download_button(
                f"Descargar el CSV de {slug}",
                data=buffer.getvalue(),
                file_name=f"gusto_import_{slug}_{inicio:%Y%m%d}_{fin:%Y%m%d}.csv",
                mime="text/csv", use_container_width=True)
        else:
            st.caption("La descarga aparece cuando se resuelva el descuadre.")
        st.divider()

    if cuadra:
        st.success("Las cuentas cuadran. Se sube en **Gusto → Pay → Run payroll → "
                   "Import payroll data → Upload**, una empresa por archivo.")
        st.caption("Revisa en la pantalla de Gusto que los tips cayeron donde "
                   "esperas ANTES de procesar la nomina.")
