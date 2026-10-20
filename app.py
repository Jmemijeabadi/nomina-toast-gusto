"""Nomina Tacos El Franc: de Toast a Gusto.

    streamlit run app.py

Tres pasos: elegir el periodo, subir un reporte de tips por sucursal con empresa
de Gusto, generar los CSV.

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
import csv
import os
import re
import subprocess
import sys
from datetime import date, timedelta

import pandas as pd
import streamlit as st

import gusto_export
import toast_payroll
from toast_payroll import (ToastClient, previous_closed_pay_period,
                           resolve_locations)

# Como se llaman las empresas para quien opera. El id de la LLC no le dice nada
# a nadie que no haya visto el codigo.
NOMBRE_EMPRESA = {
    "tacos-franc-bonita-llc": "Plaza Bonita",
    "tacos-franc-gaslamp-llc": "Gaslamp",
}

# En que orden se muestran los grupos de "Antes de subir": primero lo que mueve
# dinero de alguien, al final la configuracion.
ORDEN_DE_GRUPOS = ("cuadre", "alta_en_gusto", "identidad", "tips", "jobs",
                   "turnos", "descansos", "sucursal", "config")
ICONO_GRUPO = {
    "cuadre": "🧮", "alta_en_gusto": "👤", "identidad": "🔗", "tips": "💵",
    "jobs": "🏷️", "turnos": "⏱️", "descansos": "🍽️", "sucursal": "🏪",
    "config": "⚙️",
}

COLUMNAS_CON_DATO = (
    "regular_hours", "overtime_hours", "double_overtime_hours",
    "missed_break_hours", "paycheck_tips", "cash_tips",
    "custom_earning_distributed_service_charges",
)

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


# Las columnas de la vista previa: como se llaman en pantalla, de que columna del
# CSV salen, y si son horas o dinero. El orden es el de Gusto.
COLUMNAS_PREVIA = (
    ("Reg", "regular_hours", "h"),
    ("OT", "overtime_hours", "h"),
    ("DT", "double_overtime_hours", "h"),
    ("Prima meal", "missed_break_hours", "h"),
    ("Tips cheque", "paycheck_tips", "$"),
    ("Tips efectivo", "cash_tips", "$"),
    ("Gratuity", "custom_earning_distributed_service_charges", "$"),
)
_HORAS = [t for t, _, u in COLUMNAS_PREVIA if u == "h"]
_DINERO = [t for t, _, u in COLUMNAS_PREVIA if u == "$"]


def _numero(valor):
    """El CSV trae texto; vacio es None, no cero, para que se vea distinto."""
    texto = (valor or "").strip()
    if not texto:
        return None
    try:
        return float(texto)
    except ValueError:
        return None


def puesto_limpio(fila: dict) -> str:
    return fila["title"].replace(" (Primary)", "").strip()


def _marcar_efectivo(valor):
    """Rojo para efectivo en un puesto que no lo recibe. Semitransparente para
    que se lea en tema claro y oscuro."""
    if valor is not None and valor == valor and valor > 0.005:
        return "background-color: rgba(220, 38, 38, 0.22); font-weight: 700"
    return ""


def tabla_previa(filas: list, marcar_efectivo: bool = False) -> None:
    """Pinta renglones tal como entran a Gusto, ordenados por persona."""
    if not filas:
        st.caption("— ninguno —")
        return
    ordenadas = sorted(filas, key=lambda f: (f["last_name"], f["first_name"],
                                             "(Primary)" not in f["title"]))
    marco = pd.DataFrame([{
        "Empleado": f"{f['last_name']}, {f['first_name']}",
        "Puesto": f["title"],
        **{titulo: _numero(f[col]) for titulo, col, _ in COLUMNAS_PREVIA},
    } for f in ordenadas])
    estilo = (marco.style
              .format("{:,.2f}", subset=_HORAS, na_rep="")
              .format("${:,.2f}", subset=_DINERO, na_rep=""))
    if marcar_efectivo:
        estilo = estilo.map(_marcar_efectivo, subset=["Tips efectivo"])
    st.dataframe(estilo, hide_index=True, width="stretch")


def tabla_totales(filas: list) -> None:
    """Una sola fila con el total de cada columna, alineada con la vista previa."""
    totales = {"": "TOTAL"}
    for titulo, col, _ in COLUMNAS_PREVIA:
        totales[titulo] = sum(_numero(f[col]) or 0.0 for f in filas)
    marco = pd.DataFrame([totales])
    estilo = (marco.style
              .format("{:,.2f}", subset=_HORAS)
              .format("${:,.2f}", subset=_DINERO))
    st.dataframe(estilo, hide_index=True, width="stretch")


def huella_de_entradas(inicio, fin, subidos: dict) -> tuple:
    """Lo que determina un resultado: el periodo y QUE archivos se subieron."""
    archivos = tuple(sorted(
        (empresa, getattr(a, "name", ""), getattr(a, "size", 0))
        for empresa, a in (subidos or {}).items()))
    return (inicio, fin, archivos)


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
    if st.button("Correr los tests", width="stretch"):
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
                       f"verificado. Genera los CSV primero y vuelve a correrlos.")
        else:
            st.success("Todos los tests pasan, sin saltos")
        cola = salida.strip().splitlines()[-12:]
        st.code("\n".join(cola) or "(sin salida)")


# ───────────────────────────── paso 1: periodo ─────────────────────────────

st.title("Nomina: de Toast a Gusto")

st.subheader("1. Elige el periodo")

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

# Solo las locations con empresa de Gusto piden reporte de tips. Una sucursal
# que Toast reporta y Gusto no conoce no tiene company a la que atribuirlos, y
# contarla aqui dejaba el boton Generar deshabilitado PARA SIEMPRE: su company
# es "", nunca entra a `subidos`, asi que `faltantes` nunca se vaciaba. Ademas
# st.columns(2) con zip truncaba la lista, asi que con tres locations una real
# se quedaba sin casilla. Paso al aparecer Tacos El Franc - Fresno.
con_empresa = [l for l in locations if l["configured"]]

st.subheader(f"2. Sube los {len(con_empresa)} reportes de tips")
st.caption("En Toast Web: **Reports → Labor → Tip management**, vista **By Day**, "
           f"con el rango **{inicio:%Y-%m-%d}** a **{fin:%Y-%m-%d}**, y descargalo. "
           "Uno por location.")

subidos = {}
columnas = st.columns(len(con_empresa)) if con_empresa else []
for columna, location in zip(columnas, con_empresa):
    with columna:
        st.markdown(f"**{location['short']}**")
        archivo = st.file_uploader("Reporte EmployeeTipTotals", type=["csv"],
                                   key=f"tips_{location['guid']}",
                                   label_visibility="collapsed")
        if archivo is not None:
            rango = fechas_del_nombre(archivo.name)
            if rango and rango != (inicio, fin):
                st.error(f"Ese archivo es del {rango[0]} al {rango[1]}, no del "
                         f"periodo elegido. Bajalo de nuevo con el rango correcto.")
            else:
                if not rango:
                    st.caption("No pude leer las fechas del nombre; revisa que sea "
                               "el del periodo correcto.")
                subidos[location["company"]] = archivo
                st.success(f"{archivo.name}")

faltantes = [l["short"] for l in con_empresa if l["company"] not in subidos]
if faltantes:
    st.info(f"Falta el reporte de: {', '.join(faltantes)}. Sin el, las columnas de "
            f"tips saldrian vacias.")


# ───────────────────────────── paso 3: generar ─────────────────────────────

st.subheader("3. Genera los CSV")

if st.button("Generar", type="primary", disabled=bool(faltantes),
             width="stretch"):
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
        st.session_state["huella"] = huella_de_entradas(inicio, fin, subidos)
    except Exception as error:
        st.session_state.pop("resultado", None)
        st.error(f"Fallo la generacion: {error}")
        st.exception(error)

# El resultado se muestra solo si corresponde a lo que hay en pantalla AHORA.
# Antes bastaba con que coincidiera el periodo: si se cambiaba un reporte de
# tips y no se volvia a generar, la pantalla seguia mostrando el cuadre y los
# archivos de la corrida vieja, y se podia bajar un CSV que no correspondia.
resultado = st.session_state.get("resultado")
vigente = st.session_state.get("huella") == huella_de_entradas(inicio, fin, subidos)
if resultado and not vigente:
    st.info("Cambiaste el periodo o los reportes desde la ultima vez. "
            "Dale **Generar** otra vez para ver los archivos que corresponden.")
if resultado and vigente:

    # ════════════════════════════════════════════════════════════════════
    # El orden de esta seccion es el orden en que la persona necesita las
    # cosas. Antes la descarga estaba en el paso 6 de 7 y el veredicto hasta el
    # final, debajo de una tabla de cuadre, siete avisos en prosa, la lista de
    # retenidos, catorce avisos informativos y la salida tecnica. Lo que casi
    # siempre se necesita —"¿esta bien? dame los archivos"— quedaba enterrado.
    #
    #   1. veredicto     ¿se puede subir?
    #   2. descargas     los dos archivos, ahi mismo
    #   3. antes de subir  solo lo que pide una decision, agrupado
    #   4. resumen       una tabla, las dos empresas lado a lado
    #   5. detalle       todo lo demas, cerrado
    # ════════════════════════════════════════════════════════════════════

    filas_por_empresa = {c: f for c, f in sorted(resultado["by_company"].items()) if f}

    # ── el cuadre decide si se puede subir ──
    tips_rep = resultado.get("tips_reported", 0.0)
    tips_csv = resultado.get("tips_placed", 0.0)
    tips_ret = sum(resultado.get("tips_held", {}).values())
    # Tercer bucket: tips que el pool dejo en cuentas que no son personas. Si no
    # se resta, la pantalla reporta un hueco que no existe y asusta de gratis.
    tips_sin_dueno = sum(resultado.get("tips_no_persona", {}).values())
    hueco_tips = tips_rep - tips_csv - tips_ret - tips_sin_dueno
    horas_ent = resultado.get("hours_reported", 0.0)
    horas_csv = resultado.get("hours_placed", 0.0)
    horas_ret = resultado.get("hours_held", 0.0)
    hueco_horas = horas_ent - horas_csv - horas_ret
    # Los MISMOS umbrales que el exportador, para que la pantalla no diga que
    # cuadra cuando el exportador cree que no.
    cuadra = (abs(hueco_horas) <= gusto_export.TOLERANCIA_HORAS
              and abs(hueco_tips) <= gusto_export.TOLERANCIA_TIPS)
    # El unico candado: un descuadre que la herramienta NO pudo atribuir.
    bloqueado = bool(resultado["problems"]) or not cuadra

    # Lo que pide una decision, agrupado por tipo. Sale de los avisos
    # estructurados, no de leer frases.
    accion = [a for a in (resultado.get("avisos") or []) if a["nivel"] == "accion"]
    informativos = [a for a in (resultado.get("avisos") or []) if a["nivel"] == "informativo"]

    # Los tips en renglones de puesto NO primario: todavia no esta comprobado
    # que Gusto los acepte ahi, asi que es algo a revisar antes de subir.
    secundarios = [
        (NOMBRE_EMPRESA.get(c, c), f) for c, filas in filas_por_empresa.items()
        for f in filas
        if "(Primary)" not in f["title"]
        and (float(f["paycheck_tips"] or 0) + float(f["cash_tips"] or 0)) > 0.005
    ]
    total_secundarios = sum(float(f["paycheck_tips"] or 0) + float(f["cash_tips"] or 0)
                            for _, f in secundarios)
    pendientes = len({a["grupo"] for a in accion}) + (1 if secundarios else 0)

    st.divider()

    # ── 1. veredicto ──
    periodo_txt = f"{inicio:%d %b} – {fin:%d %b %Y}"
    if bloqueado:
        st.error(f"### ❌ No subas estos archivos\n"
                 f"Periodo {periodo_txt}. Hay horas o tips que no se pudieron "
                 f"explicar: el archivo saldria incompleto y en Gusto se veria "
                 f"completo. Abajo dice cuanto y donde.")
        for problema in resultado["problems"]:
            st.markdown(f"- {problema}")
    elif pendientes:
        st.warning(f"### ⚠️ Listos — revisa {pendientes} cosa(s) antes de subir\n"
                   f"Periodo {periodo_txt}. Las cuentas cuadran. Lo que hay que "
                   f"mirar esta justo abajo de las descargas.")
    else:
        st.success(f"### ✅ Listos para subir a Gusto\n"
                   f"Periodo {periodo_txt}. Las cuentas cuadran y no hay nada "
                   f"pendiente.")

    # ── 2. descargas ──
    if not bloqueado and filas_por_empresa:
        columnas = st.columns(len(filas_por_empresa))
        for columna, (company, filas) in zip(columnas, filas_por_empresa.items()):
            nombre = NOMBRE_EMPRESA.get(company, company)
            suma = lambda col, fs=filas: sum(float(f[col] or 0) for f in fs)
            personas = len({f["gusto_employee_id"] for f in filas
                            if any((f[c] or "") for c in COLUMNAS_CON_DATO)})
            horas = (suma("regular_hours") + suma("overtime_hours")
                     + suma("double_overtime_hours"))
            tips = (suma("paycheck_tips") + suma("cash_tips")
                    + suma("custom_earning_distributed_service_charges"))

            buffer = io.StringIO()
            escritor = csv.DictWriter(buffer, fieldnames=gusto_export.GUSTO_COLUMNS,
                                      extrasaction="ignore")
            escritor.writeheader()
            escritor.writerows(filas)
            slug = company.replace("tacos-franc-", "").replace("-llc", "")
            with columna:
                st.download_button(
                    f"⬇  {nombre}", data=buffer.getvalue(),
                    file_name=f"gusto_import_{slug}_{inicio:%Y%m%d}_{fin:%Y%m%d}.csv",
                    mime="text/csv", type="primary", width="stretch")
                st.caption(f"{personas} personas · {horas:,.2f} h · {money(tips)} en tips")
        st.caption("Se suben en **Gusto → Pay → Run payroll → Import payroll "
                   "data**, un archivo por empresa.")

    # ── 3. antes de subir ──
    if pendientes and not bloqueado:
        st.markdown("#### Antes de subir")

        por_grupo = {}
        for a in accion:
            por_grupo.setdefault(a["grupo"], []).append(a)

        for grupo in ORDEN_DE_GRUPOS:
            items = por_grupo.get(grupo)
            if not items:
                continue
            icono = ICONO_GRUPO.get(grupo, "•")
            titulo = gusto_export.TITULOS_DE_GRUPO.get(grupo, grupo)

            if grupo == "alta_en_gusto":
                # Solo PERSONAS. Las cuentas de pedidos en linea no son gente y
                # ya salen en "Tips que no llegan a nadie": listarlas aqui
                # tambien las contaba dos veces y hacia ver mas gente de la que hay.
                no_personas = set(resultado.get("tips_no_persona") or {})
                retenidos = [h for h in (resultado.get("held_back") or [])
                             if (h["horas"] > 0.005 or h["tips"] > 0.005)
                             and h["nombre"].strip() not in no_personas]
                horas_r = sum(h["horas"] for h in retenidos)
                with st.expander(f"{icono}  {titulo} — {len(retenidos)} · "
                                 f"{horas_r:,.2f} h"):
                    st.caption("No entran al CSV. Hay que darlos de alta en "
                               "Gusto y volver a generar, o pagarles aparte.")
                    tabla = pd.DataFrame([{
                        "Persona": h["nombre"],
                        "Sucursal": h.get("sucursal", ""),
                        "Horas": f"{h['horas']:,.2f}",
                        "Tips": money(h["tips"]),
                        "Por que": h["motivo"],
                    } for h in sorted(retenidos, key=lambda x: -x["horas"])])
                    st.dataframe(tabla, hide_index=True, width="stretch")
                continue

            with st.expander(f"{icono}  {titulo} — {len(items)}"):
                for a in items:
                    st.markdown(f"- {a['texto']}")

        if secundarios:
            with st.expander(f"🧾  Tips en un puesto que no es el principal — "
                             f"{len(secundarios)} · {money(total_secundarios)}"):
                st.caption("Todavia no esta comprobado que Gusto acepte tips en "
                           "estos renglones. Al subir, revisa en la pantalla de "
                           "Gusto que cayeron donde dice aqui ANTES de procesar.")
                st.dataframe(pd.DataFrame([{
                    "Empresa": empresa,
                    "Persona": f"{f['last_name']}, {f['first_name']}",
                    "Puesto": f["title"],
                    "Tips cheque": f["paycheck_tips"] or "",
                    "Tips efectivo": f["cash_tips"] or "",
                } for empresa, f in secundarios]), hide_index=True,
                    width="stretch")

    # ── 4. asi entra a Gusto ──
    # La vista previa es la seccion principal, no un detalle escondido: es lo
    # que la persona revisa antes de subir. Sale de los MISMOS renglones que se
    # escriben en el CSV, asi que lo que se ve es exactamente lo que entra.
    if filas_por_empresa:
        st.markdown("#### Asi entra a Gusto")
        st.caption("Un renglon por puesto, tal cual trae el archivo. Las celdas "
                   "vacias no tocan nada en Gusto: dejan el valor que ya tenga.")

        pestanas = st.tabs([NOMBRE_EMPRESA.get(c, c) for c in filas_por_empresa])
        for pestana, (company, filas) in zip(pestanas, filas_por_empresa.items()):
            with pestana:
                con_dato = [f for f in filas
                            if any((f[c] or "") for c in COLUMNAS_CON_DATO)]
                vacios = len(filas) - len(con_dato)
                puestos_efectivo = gusto_export.PUESTOS_CON_EFECTIVO.get(company, set())

                if puestos_efectivo:
                    recibe = [f for f in con_dato
                              if puesto_limpio(f).lower() in puestos_efectivo]
                    resto = [f for f in con_dato
                             if puesto_limpio(f).lower() not in puestos_efectivo]

                    st.markdown(f"**💵 Reciben tips en efectivo** — "
                                f"{', '.join(sorted(p.title() for p in puestos_efectivo))} "
                                f"· {len(recibe)} renglones")
                    tabla_previa(recibe)

                    st.markdown(f"**Solo tips al cheque** — el resto de los puestos "
                                f"· {len(resto)} renglones")
                    # La columna de efectivo se queda aunque normalmente venga
                    # vacia: quitarla haria INVISIBLE el efectivo que cayera en un
                    # puesto que no lo recibe, que es justo lo que hay que ver.
                    tabla_previa(resto, marcar_efectivo=True)
                else:
                    st.caption("En esta empresa nadie recibe tips en efectivo.")
                    tabla_previa(con_dato, marcar_efectivo=True)

                st.markdown("**Totales del archivo**")
                tabla_totales(filas)
                if vacios:
                    st.caption(f"{vacios} renglon(es) del roster de Gusto vienen "
                               f"completamente vacios y no se muestran: no tocan "
                               f"nada en Gusto.")

    # ── 5. detalle, todo cerrado ──
    st.markdown("#### Detalle")

    with st.expander("Como cuadran las cuentas"):
        st.caption("Todo lo que entra tiene que salir: al CSV, a retenidos, o a "
                   "una cuenta que no es persona. 'Sin explicar' debe ser cero.")
        st.dataframe(pd.DataFrame([
            {"": "Horas", "Entro": f"{horas_ent:,.2f}", "Al CSV": f"{horas_csv:,.2f}",
             "Retenido": f"{horas_ret:,.2f}", "Sin dueño": "",
             "Sin explicar": f"{hueco_horas:,.2f}"},
            {"": "Tips", "Entro": money(tips_rep), "Al CSV": money(tips_csv),
             "Retenido": money(tips_ret), "Sin dueño": money(tips_sin_dueno),
             "Sin explicar": money(hueco_tips)},
        ]), hide_index=True, width="stretch")

    if informativos:
        with st.expander(f"Avisos informativos ({len(informativos)}) — no cambian "
                         f"nada de lo que se paga"):
            for a in informativos:
                st.markdown(f"- {a['texto']}")

    reporte = gusto_export.formatear_diagnostico(resultado, inicio, fin)
    with st.expander("Diagnostico tecnico — para reportar un problema"):
        st.caption("Si algo no cuadra, descarga esto y mandalo. Son conteos y "
                   "totales, sin nombres ni correos.")
        st.download_button(
            "Descargar el diagnostico", data=reporte,
            file_name=f"diagnostico_{inicio:%Y%m%d}_{fin:%Y%m%d}.txt",
            mime="text/plain")
        st.code(reporte, language="text")
