"""Genera el CSV de Smart Import de Gusto, una empresa por archivo.

Uso:
    python gusto_export.py --start 2026-09-19 --end 2026-10-02 \\
        --tips-nc reference/tips_NC_2026-09-19_2026-10-02.csv \\
        --tips-gl reference/tips_GL_2026-09-19_2026-10-02.csv

Fuentes de cada columna:

  regular_hours / overtime_hours / double_overtime_hours
      Calculadas desde los punches de Toast con las reglas de California
      (ca_overtime.py). NO se copia el overtimeHours de Toast, que se calcula
      por restaurante y no trae double time.

  missed_break_hours
      Horas de prima de meal que detecta el motor, topadas en 1 por jornada.
      Va en HORAS a proposito: Gusto las valua con su propio Regular Rate of
      Pay, que es lo que exige California. La columna
      custom_earning_meal_break_violation se deja VACIA; llenar las dos paga
      la prima dos veces porque Gusto no las deduplica.

  paycheck_tips / cash_tips / custom_earning_distributed_service_charges
      Del reporte EmployeeTipTotals de Toast (Reports > Labor > Tip
      management, vista By Day, exportable como CSV), columnas "after
      pooling". El nonCashTips de la API es PRE-pool y no sirve: en Gaslamp
      27 de 31 personas cobran algo distinto de lo que registraron, y en
      National City 32 personas de cocina cobran con cero en la API.

Reglas duras:
  - Una columna sin dato calculado va VACIA, nunca "0.0". En Smart Import los
    ceros sobrescriben y los blancos no tocan nada, asi que un 0.0 le afirma a
    Gusto un cero que nadie calculo.
  - El string de `title` se copia byte por byte del template, incluido el
    sufijo " (Primary)": es lo UNICO que le dice a Gusto a que job va el
    renglon.
  - El dinero (tips, service charges, prima) va una sola vez, en el renglon
    primario, imitando el patron de `reimbursement` del template.
  - Ninguna fila cruza empresas: cada location es una LLC distinta.
  - Si algo queda sin mapear, no se escribe nada.
"""

from __future__ import annotations

import argparse
import csv
import io
import os
import sys
import unicodedata
from collections import defaultdict
from datetime import date, datetime, timedelta

import ca_overtime
import toast_payroll
from toast_payroll import (FIRST_MEAL_WAIVER_CEILING_HOURS, GUSTO_COMPANIES,
                           MEAL_PREMIUM_HOURS_CAP_PER_DAY, MEAL_WAIVERS_DOCUMENTED,
                           NON_PERSON_MARKERS,
                           ToastClient, audit_time_entry, load_employee_map,
                           resolve_locations, write_csv)

# Columnas del template de Gusto, en orden. Se copian del archivo real.
GUSTO_COLUMNS = [
    "last_name", "first_name", "title", "gusto_employee_id",
    "regular_hours", "overtime_hours", "double_overtime_hours",
    "missed_break_hours", "bonus", "commission", "paycheck_tips", "cash_tips",
    "correction_payment", "custom_earning_distributed_service_charges",
    "custom_earning_meal_break_violation", "reimbursement", "personal_note",
]

# A donde va cada tipo de tip del reporte.
#
#   Cash tips after pooling     -> cash_tips      ya los cobro en efectivo,
#                                                 Gusto solo retiene impuesto
#   Non-cash tips after pooling -> paycheck_tips  se pagan en el cheque
#
# Esta cuenta trae nonCashTipsRoundingLoss en null en los 1,280 turnos
# revisados, campo que solo se puebla cuando los tips de tarjeta se pagan del
# cajon. Eso respalda que los de tarjeta van en el cheque. Si Toast Web dice
# "Pay out from the cash drawer" en Employees > Shift review > Payout options,
# cambiar esto a "cash_tips".
# Tolerancia del cierre de cuentas. Arriba de esto, el descuadre no se puede
# achacar a redondeo y bloquea: un CSV incompleto que parece completo es
# peor que no tener CSV.
TOLERANCIA_HORAS = 0.05
TOLERANCIA_TIPS = 0.50

NON_CASH_TIPS_COLUMN = "paycheck_tips"

# La prima de meal nunca se escribe como monto: ver el docstring.
MEAL_PREMIUM_COLUMN = "missed_break_hours"


def norm(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", (value or "").strip().lower())
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(stripped.split())


def money(value: str) -> float:
    import re
    cleaned = re.sub(r"[^0-9.\-]", "", (value or ""))
    try:
        return float(cleaned) if cleaned not in ("", "-", ".") else 0.0
    except ValueError:
        return 0.0


def slug(company: str) -> str:
    """Nombre corto de la empresa para el nombre del archivo."""
    return (company or "sin-empresa").replace("tacos-franc-", "").replace("-llc", "")


def parse_day(value: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        raise argparse.ArgumentTypeError(f"Fecha invalida: {value}. Usa YYYY-MM-DD.")


def load_template(path: str) -> list:
    """Lee el template de Gusto conservando el string exacto de title."""
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8-sig") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def load_tip_report(path: str) -> list:
    """Lee el EmployeeTipTotals de Toast y devuelve las filas crudas.

    La columna Employee viene como "Nombre Apellido" (el nombre LEGAL de Toast,
    no el preferido), y Job como el job de Toast. Resolver eso a
    (gusto_employee_id, title de Gusto) se hace en build(), con el employee_map
    y el job_map, que ya estan validados. Hacerlo con heuristicas de nombre aqui
    perdia el 54% de los tips: el reporte escribe el apellido
    compuesto separado y el template de Gusto lo escribe junto.
    """
    if not path:
        return []

    if hasattr(path, "read"):          # archivo subido desde Streamlit
        data = path.read()
        if isinstance(data, bytes):
            data = data.decode("utf-8-sig")
        handle = io.StringIO(data)
        close = False
    else:
        if not os.path.exists(path):
            raise RuntimeError(f"No existe el reporte de tips: {path}")
        handle = open(path, newline="", encoding="utf-8-sig")
        close = True

    COLUMNAS_NECESARIAS = (
        "Employee", "Job", "Hours worked",
        "Cash tips after pooling", "Non-cash tips after pooling",
        "Cash gratuity after pooling", "Non-cash gratuity after pooling",
        "Tips and gratuity after pooling",
    )

    rows = []
    try:
        lector = csv.DictReader(handle)
        # Sin esto, un archivo con otros encabezados devolvia puros ceros y el
        # cierre de tips daba 0.00 contra si mismo: la app decia "cuadra" con
        # los tips en blanco para las 72 personas.
        faltan = [c for c in COLUMNAS_NECESARIAS
                  if c not in (lector.fieldnames or [])]
        if faltan:
            raise RuntimeError(
                "El reporte de tips no tiene las columnas esperadas. Faltan: "
                + ", ".join(faltan)
                + ". Tiene que ser el EmployeeTipTotals de Toast Web "
                  "(Reports > Labor > Tip management, vista By Day).")
        for row in lector:
            rows.append({
                "employee": (row.get("Employee") or "").strip(),
                "job": (row.get("Job") or "").strip(),
                "hours": money(row.get("Hours worked")),
                "cash": money(row.get("Cash tips after pooling")),
                "non_cash": money(row.get("Non-cash tips after pooling")),
                "gratuity": (money(row.get("Cash gratuity after pooling"))
                             + money(row.get("Non-cash gratuity after pooling"))),
            })
    finally:
        if close:
            handle.close()
    return rows


def load_job_map(path: str = "") -> dict:
    """{(location, toast_job_normalizado): gusto_title}."""
    path = path or toast_payroll.data_path("job_map.csv")
    if not os.path.exists(path):
        return {}
    mapping = {}
    with open(path, newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            title = (row.get("gusto_title") or "").strip()
            if title:
                mapping[(row.get("toast_location", "").strip(),
                         norm(row.get("toast_job")))] = title
    return mapping


def blank_or(value: float, decimals: int = 2) -> str:
    """Vacio cuando no hay nada que afirmar. Nunca '0.0'."""
    return f"{value:.{decimals}f}" if value else ""


def build(client: ToastClient, start: date, end_inclusive: date,
          tip_reports: dict) -> dict:
    """Arma las filas del CSV de Gusto por empresa."""
    locations = resolve_locations(client)
    employee_map = load_employee_map()
    job_map = load_job_map()

    # Tres niveles, no dos. La regla: si se puede nombrar que esta mal y cuanto,
    # es para REVISAR y decide quien opera. Solo para el programa cuando detecta
    # un descuadre que no puede atribuir, porque ahi el operador no tiene con que
    # actuar y el archivo pareceria completo.
    problems = []      # para el programa: descuadre sin atribuir
    revisar = []       # nombrado y cuantificado: lo decide la persona
    warnings = []      # informativo
    held_back = []
    if not ca_overtime.WORKWEEK_START_CONFIRMED:
        revisar.append(
            "El inicio de la semana laboral no esta confirmado "
            f"(usando {('lun','mar','mie','jue','vie','sab','dom')[ca_overtime.WORKWEEK_START_WEEKDAY]}). "
            "Leelo en Gusto > Pay > Pay settings y ponlo en ca_overtime.py: "
            "de ese valor dependen las 40 horas semanales y el septimo dia."
        )

    # Paso 1: auditar los turnos del periodo, con 7 dias de contexto previo para
    # poder ver rachas de 7 dias consecutivos que arrancan antes del periodo.
    audited = []
    for location in locations:
        if not location["configured"]:
            # Una location que Toast reporta y Gusto no conoce. Puede ser una
            # sucursal nueva que todavia no abre (cero turnos, solo se avisa) o
            # una que ya opera y cuyas horas se estarian perdiendo. Se mide para
            # no gritar igual en los dos casos.
            # Aislado a proposito. Antes de medir las horas, este GUID no se
            # consultaba nunca; si la consulta falla (permisos del credential,
            # una location a medio dar de alta, cualquier 4xx) NO puede tirar la
            # nomina de las empresas que si estan bien. Se degrada a aviso.
            try:
                huerfanas = client.get_time_entries_by_business_date(
                    location["guid"], start, end_inclusive)
                vivos = [e for e in huerfanas if not e.get("deleted")]
                horas = sum(audit_time_entry(e)["payable_hours"] for e in vivos)
            except Exception as error:
                revisar.append(
                    f"{location['toast_name']} no tiene empresa de Gusto y "
                    f"tampoco se pudieron leer sus turnos ({type(error).__name__}), "
                    f"asi que no se sabe si tiene horas sin pagar. Revisala a mano "
                    f"o agregala a GUSTO_COMPANIES.")
                continue
            # El disparador es que HAYA turnos, no que sumen horas. Un turno
            # abierto trae regularHours en 0 porque Toast aun no lo calcula, asi
            # que medir solo las horas clasificaba como "no opera" a una
            # sucursal con gente fichada en ese momento.
            if vivos:
                revisar.append(
                    f"{location['toast_name']} no tiene empresa de Gusto y "
                    f"tiene {len(vivos)} turno(s) en este periodo, "
                    f"{horas:.2f} h pagables. Esas horas no entran a ningun "
                    f"CSV, y tampoco cuentan para el overtime semanal de quien "
                    f"ademas trabaje en otra sucursal. Agregala a "
                    f"GUSTO_COMPANIES con su template, o se quedan sin pagar.")
            else:
                warnings.append(
                    f"{location['toast_name']} existe en Toast sin empresa de "
                    f"Gusto, pero no tuvo turnos en este periodo. Hay que "
                    f"mapearla antes de que empiece a operar.")
            continue
        entries = client.get_time_entries_by_business_date(
            location["guid"], start - timedelta(days=7), end_inclusive)
        for time_entry in entries:
            if time_entry.get("deleted"):
                continue
            row = audit_time_entry(time_entry)
            row["location"] = location["short"]
            row["company"] = location["company"]
            row["job_guid"] = (time_entry.get("jobReference") or {}).get("guid")
            audited.append(row)

    # Paso 2: identidad y jobs de Toast.
    employees = {}
    jobs = {}
    for location in locations:
        if not location["configured"]:
            continue
        for job in client.get_jobs(location["guid"]):
            jobs[job["guid"]] = (location["short"], job.get("title") or "")
        for employee in client.get_employees(location["guid"]):
            guid = employee.get("guid")
            override = employee_map.get(guid) or {}
            employees[guid] = {
                "gusto_employee_id": override.get("gusto_employee_id", ""),
                "person_key": override.get("person_key", ""),
                "persona_confirmada": override.get("persona_confirmada", ""),
                "excluir": bool(override.get("excluir")),
                "pendiente": bool(override.get("pendiente", True)),
                "display": f"{employee.get('lastName','')}, "
                           f"{employee.get('chosenName') or employee.get('firstName') or ''}",
                "last_name": (employee.get("lastName") or "").strip(),
                "first_name_legal": (employee.get("firstName") or "").strip(),
                "chosen_name_only": (employee.get("chosenName") or "").strip(),
                "location_guid": location["guid"],
                "deleted": bool(employee.get("deleted")),
            }

    # Paso 3: horas por PERSONA y jornada, para el overtime de California.
    #
    # La identidad del motor es el ser humano, NO el gusto_employee_id: Gusto
    # asigna un id distinto por empresa, asi que quien ficha en las dos LLCs
    # entraria como dos personas y los umbrales de 8 h/dia, 12 h/dia, 40 h/semana
    # y septimo dia se medirian sobre la mitad de sus dias. Verificado en esta
    # cuenta: la misma persona con un id distinto por empresa sumaba ~82 h en
    # misma persona con un id distinto por empresa sumaba ~82 h en dos semanas
    # laborales y solo 2.89 h de OT, porque cada mitad quedaba bajo las 40.
    #
    # Agregar horas entre dos LLCs solo corresponde si son joint employers. Esa
    # es una decision legal, no tecnica, asi que si un humano no la confirmo en
    # employee_map.csv el export se detiene en vez de elegir en silencio.
    sin_confirmar = {}

    def person_of(guid: str) -> str:
        employee = employees.get(guid) or {}
        clave = employee.get("person_key") or ""
        confirmada = (employee.get("persona_confirmada") or "").strip().lower()
        if clave and confirmada in ("si", "s", "yes", "true", "1"):
            return f"PERSONA::{clave}"
        if clave and confirmada:
            sin_confirmar[clave] = employee.get("display", guid)
        return (employee.get("gusto_employee_id")
                or f"SIN-MAPEO::{guid}")

    person_days = defaultdict(lambda: defaultdict(float))
    for row in audited:
        employee = employees.get(row["employee_guid"]) or {}
        if employee.get("excluir"):
            continue
        person_days[person_of(row["employee_guid"])][row["business_date"]] +=             row["payable_hours"]

    overtime = ca_overtime.compute_overtime(dict(person_days))

    # Paso 4: reparto del dia entre los jobs trabajados ese dia, en proporcion a
    # las horas. Gusto necesita el split reg/OT/DT por renglon de job.
    unmapped_jobs = defaultdict(set)
    no_job_ref = {}
    unmapped_routed = {}
    # Titles de Gusto por empleado, para resolver los turnos sin jobReference.
    primary_titles = {}
    for location in locations:
        if not location["configured"]:
            continue
        for template_row in load_template(location["template"]):
            key = (location["company"], template_row["gusto_employee_id"])
            primary_titles.setdefault(key, []).append(
                template_row["title"].replace(" (Primary)", "").strip())
    hours_by_cell = defaultdict(lambda: {"regular": 0.0, "overtime": 0.0, "double": 0.0})
    premium_by_identity = defaultdict(float)
    day_jobs = defaultdict(lambda: defaultdict(float))
    for row in audited:
        if not (start <= ca_overtime.parse_business_date(row["business_date"]) <= end_inclusive):
            continue
        employee = employees.get(row["employee_guid"]) or {}
        if employee.get("excluir"):
            continue
        identity = person_of(row["employee_guid"])
        pago = (employee.get("gusto_employee_id")
                or f"SIN-MAPEO::{row['employee_guid']}")
        location, toast_job = jobs.get(row["job_guid"], (row["location"], ""))
        gusto_title = job_map.get((location, norm(toast_job)), "")
        if not gusto_title and not toast_job:
            # Turno sin jobReference: la persona ficho sin elegir job. Si en
            # Gusto tiene UN solo job, sus horas no tienen otro destino posible,
            # asi que van ahi. Si tiene varios, no se adivina.
            titles = primary_titles.get((row["company"], pago), [])
            if len(titles) == 1:
                gusto_title = titles[0]
                no_job_ref[(location, pago)] = employee.get("display", "?")
            else:
                unmapped_jobs[(location, "(sin jobReference)")].add(
                    employee.get("display", "?"))
                continue
        if not gusto_title:
            # Job sin mapeo: mismo criterio que arriba. Si en Gusto tiene un
            # solo job, sus horas no tienen otro destino posible.
            titles = primary_titles.get((row["company"], pago), [])
            if len(titles) == 1:
                gusto_title = titles[0]
                unmapped_routed[(location, toast_job, pago)] = (
                    employee.get("display", "?"), titles[0])
            else:
                unmapped_jobs[(location, toast_job)].add(employee.get("display", "?"))
                continue
        # La llave del dia lleva la PERSONA (para medir su jornada completa) y
        # la celda lleva el id de PAGO (para que ninguna fila cruce empresas).
        day_jobs[(identity, row["business_date"])][
            (pago, row["company"], gusto_title)] += row["payable_hours"]

    for (identity, business_date), cells in day_jobs.items():
        day = ca_overtime.parse_business_date(business_date)
        split = (overtime.get(identity, {}).get("days", {}) or {}).get(day)
        if not split:
            continue
        total = sum(cells.values())
        if total <= 0:
            continue
        for cell, hours in cells.items():
            share = hours / total
            target = hours_by_cell[cell]      # (id_de_pago, empresa, title)
            target["regular"] += split["regular"] * share
            target["overtime"] += split["overtime"] * share
            target["double"] += split["double"] * share

    # Paso 5: prima de meal, topada por persona y jornada.
    day_violations = defaultdict(int)
    for row in audited:
        if not (start <= ca_overtime.parse_business_date(row["business_date"]) <= end_inclusive):
            continue
        employee = employees.get(row["employee_guid"]) or {}
        if employee.get("excluir"):
            continue
        day_violations[(person_of(row["employee_guid"]), row["business_date"])] +=             len(row["violations"])
    # Los descansos de comida no tomados, por sucursal. Vale decirlo aparte de
    # la prima porque la asimetria entre sucursales es el dato que importa: si
    # una concentra casi todos, no es mala suerte, es como se esta operando.
    # Medido el 2026-10-02 en 09-05/18: Gaslamp 36 turnos, National City 0.
    perdidos = defaultdict(lambda: {"turnos": 0, "personas": set()})
    for row in audited:
        if not (start <= ca_overtime.parse_business_date(row["business_date"]) <= end_inclusive):
            continue
        if "Missed Meal Break" in row["violations"]:
            acumulado = perdidos[row["location"]]
            acumulado["turnos"] += 1
            acumulado["personas"].add(row["employee_guid"])
    for nombre_loc, datos in sorted(perdidos.items()):
        revisar.append(
            f"{nombre_loc}: {datos['turnos']} turno(s) de "
            f"{len(datos['personas'])} persona(s) arriba de 6 h donde Toast "
            f"reporta el descanso de comida como NO tomado. La prima ya va en el "
            f"CSV, asi que la nomina esta bien; lo que conviene mirar es por que "
            f"pasa, sobre todo si una sucursal concentra casi todos.")

    premium_por_persona = defaultdict(float)
    for (persona, _day), count in day_violations.items():
        if count > 0:
            premium_por_persona[persona] += min(
                float(count), MEAL_PREMIUM_HOURS_CAP_PER_DAY)
    # Se baja de persona a id de pago: si la persona tiene dos empresas, su
    # prima se paga UNA vez, en la empresa donde tiene mas horas.
    horas_por_pago = defaultdict(float)
    for (pago, _company, _title), cell in hours_by_cell.items():
        horas_por_pago[pago] += cell["regular"] + cell["overtime"] + cell["double"]
    for guid, employee in employees.items():
        pago = employee.get("gusto_employee_id")
        if not pago:
            continue
        persona = person_of(guid)
        if persona in premium_por_persona:
            mejor = max(
                (p for p in horas_por_pago
                 if any(person_of(g) == persona
                        and (employees.get(g) or {}).get("gusto_employee_id") == p
                        for g in employees)),
                key=lambda p: horas_por_pago[p], default=pago)
            if mejor == pago:
                premium_by_identity[pago] = premium_por_persona[persona]

    for (location, toast_job, identity), (name, title) in sorted(unmapped_routed.items()):
        warnings.append(
            f"{name}: su job '{toast_job}' de {location} no existe en Gusto, asi "
            f"que sus horas se pagaron como '{title}', su unico job alla.")

    for (location, identity), name in sorted(no_job_ref.items()):
        warnings.append(
            f"{name} ({location}) tiene turnos sin jobReference en Toast. Sus "
            f"horas se mandaron a su unico job de Gusto, pero conviene "
            f"arreglarlo en Toast para que fiche con job.")

    for (location, toast_job), people in sorted(unmapped_jobs.items()):
        revisar.append(
            f"Job '{toast_job}' de {location} no tiene title en Gusto "
            f"({len(people)} persona(s): {', '.join(sorted(people)[:3])}"
            f"{'...' if len(people) > 3 else ''}). Crea el job en Gusto o "
            f"mapealo en job_map.csv.")

    # Reconciliacion: horas cuyo title no existe para esa persona en Gusto.
    #
    # Pasa porque Toast y Gusto no coinciden en el job de varias personas. El
    # proceso manual lo tapa poniendo las horas en el renglon que exista, asi
    # que aca se hace lo mismo PERO reportandolo: si la persona tiene un solo
    # job en Gusto, sus horas van ahi (es la unica tarifa que existe para ella);
    # si tiene varios, no se adivina y se bloquea.
    #
    # Medido en un periodo real: ~568 h en esta situacion, el caso mas grande
    # 77 h fichadas con un job que en Gusto no existe para esa persona.
    unmapped_hours = {}
    name_by_identity = {}
    for guid, employee in employees.items():
        if employee.get("gusto_employee_id"):
            name_by_identity[employee["gusto_employee_id"]] = employee["display"]

    for key in sorted(list(hours_by_cell)):
        identity, company, title = key
        cell = hours_by_cell[key]
        total = cell["regular"] + cell["overtime"] + cell["double"]
        titles = primary_titles.get((company, identity), [])
        if title in titles:
            continue
        who = name_by_identity.get(identity, identity)
        if total <= 0.005:
            del hours_by_cell[key]
            continue
        if not titles:
            # Sin identidad en Gusto: ya se reporta por nombre mas abajo, aqui
            # solo se acumulan las horas para decir cuanto esta en juego.
            unmapped_hours[identity] = unmapped_hours.get(identity, 0.0) + total
            del hours_by_cell[key]
            continue
        if len(titles) == 1:
            target = (identity, company, titles[0])
            destination = hours_by_cell[target]
            for bucket in ("regular", "overtime", "double"):
                destination[bucket] += cell[bucket]
            del hours_by_cell[key]
            warnings.append(
                f"{who}: {total:.2f} h que Toast marca como '{title}' se pagaron "
                f"como '{titles[0]}', que es su unico job en Gusto. Si la tarifa "
                f"deberia ser distinta, hay que alinear el job en los dos sistemas.")
        else:
            revisar.append(
                f"{who}: {total:.2f} h de '{title}' no tienen renglon en Gusto, y "
                f"tiene varios jobs ({', '.join(titles)}) asi que no se adivina a "
                f"cual van. Agrega ese job en Gusto o corrige el de Toast.")

    # Solo cuenta como pendiente quien tiene turnos DENTRO del periodo. Antes se
    # miraba todo `audited`, que incluye los 7 dias de contexto previo, asi que
    # un registro viejo de alguien que si cobra salia en retenidos pidiendo
    # pagarle a mano: pago doble si el operador obedece.
    guids_en_periodo = {
        r["employee_guid"] for r in audited
        if start <= ca_overtime.parse_business_date(r["business_date"]) <= end_inclusive
    }
    pending = sorted({
        employees[g]["display"] for g in guids_en_periodo
        if g in employees and employees[g]["pendiente"] and not employees[g]["excluir"]
    })
    # Paso 5b: resolver los tips del reporte a (empresa, empleado, title).
    #
    # El reporte trae el nombre LEGAL de Toast (con el apellido compuesto escrito
    # separado, no como lo escribe el template de Gusto) y el job de Toast. Se
    # resuelve por el indice de empleados de Toast, que tiene las dos variantes
    # de nombre, y de ahi al gusto_employee_id del mapeo.
    tips_by_cell = defaultdict(lambda: {"cash": 0.0, "non_cash": 0.0, "gratuity": 0.0})
    tips_unresolved = []
    tips_held = {}
    tips_no_persona = {}
    tips_reported_total = 0.0

    for location in locations:
        if not location["configured"]:
            continue
        company = location["company"]
        rows_raw = tip_reports.get(company) or []
        if not rows_raw:
            continue

        # norm("nombre apellido") -> guid, con firstName y chosenName
        by_name = {}
        for prefer_active in (True, False):
            for guid, employee in employees.items():
                if employee.get("location_guid") != location["guid"]:
                    continue
                if employee.get("deleted") == prefer_active:
                    continue   # primera pasada solo activos, segunda los de baja
                last = employee.get("last_name", "")
                for first in (employee.get("first_name_legal"),
                              employee.get("chosen_name_only")):
                    if first:
                        by_name.setdefault(norm(f"{first} {last}"), guid)

        for raw in rows_raw:
            total = raw["cash"] + raw["non_cash"] + raw["gratuity"]
            tips_reported_total += total
            if any(marker in norm(raw["employee"]) for marker in NON_PERSON_MARKERS):
                # El pool de Toast dejo dinero parado en una cuenta que no es
                # persona (Online Ordering). Antes se descontaba de los DOS lados
                # del cierre, asi que el hueco daba 0.00 por construccion y nadie
                # veia esos tips. Medido: $190.91 y $235.28 por periodo.
                if total > 0.005:
                    tips_no_persona[raw["employee"].strip()] = (
                        tips_no_persona.get(raw["employee"].strip(), 0.0) + total)
                continue
            guid = by_name.get(norm(raw["employee"]))
            if not guid:
                if total > 0.005:
                    tips_unresolved.append((location["short"], raw["employee"],
                                            raw["job"], total))
                continue
            employee = employees.get(guid) or {}
            if employee.get("excluir"):
                continue
            identity = employee.get("gusto_employee_id")
            if not identity:
                if total > 0.005:
                    tips_held[employee.get("display", raw["employee"])] = (
                        tips_held.get(employee.get("display", raw["employee"]), 0.0) + total)
                continue
            titles = primary_titles.get((company, identity), [])
            title = job_map.get((location["short"], norm(raw["job"])), "")
            if title not in titles:
                title = titles[0] if len(titles) == 1 else ""
            if not title:
                if total > 0.005:
                    tips_unresolved.append((location["short"], raw["employee"],
                                            raw["job"], total))
                continue
            cell = tips_by_cell[(company, identity, title)]
            cell["cash"] += raw["cash"]
            cell["non_cash"] += raw["non_cash"]
            cell["gratuity"] += raw["gratuity"]

    for loc, who, job, amount in sorted(tips_unresolved, key=lambda x: -x[3]):
        revisar.append(
            f"${amount:,.2f} de tips de '{who}' ({job}, {loc}) no se pudieron "
            f"ubicar en el template de Gusto. Sin resolverlo esos tips no se pagan.")

    # Quien trabajo y no recibio NADA del pool, en una sucursal donde el pool si
    # repartio. No lo decide la herramienta: el reporte de tips es la fuente. Pero
    # si alguien tiene horas y el reporte no lo menciona, lo mas probable es que
    # sus turnos no traigan job en Toast y el pool no haya podido acreditarlo.
    # Eso es dinero que quiza le toca, y a simple vista no se ve: su renglon del
    # CSV sale con horas y la columna de tips vacia, igual que un puesto que
    # legitimamente no recibe del pool.
    for location in locations:
        if not location["configured"]:
            continue
        company = location["company"]
        if not (tip_reports.get(company) or []):
            continue
        pagados = {
            pago for (comp, pago, _t), celda in tips_by_cell.items()
            if comp == company and (celda.get("non_cash", 0.0)
                                    or celda.get("cash", 0.0)
                                    or celda.get("gratuity", 0.0))
        }
        if not pagados:
            continue
        for (pago, comp, titulo), celda in sorted(hours_by_cell.items()):
            if comp != company or pago in pagados:
                continue
            horas = (celda.get("regular", 0.0) + celda.get("overtime", 0.0)
                     + celda.get("double", 0.0))
            if horas <= 0.005:
                continue
            revisar.append(
                f"{location['short']}: alguien con {horas:.2f} h en '{titulo}' "
                f"(id de Gusto {pago}) no recibio NADA del pool, mientras el "
                f"resto de la sucursal si. Hay dos explicaciones y conviene "
                f"saber cual es: que ese puesto no reciba del pool segun la "
                f"politica de Tips Manager, y entonces esta bien; o que sus "
                f"turnos no traigan job en Toast, y entonces el pool no pudo "
                f"acreditarlo y son tips que no le llegaron.")

    # Paso 6: llenar el template de cada empresa.
    output = {}
    for location in locations:
        if not location["configured"]:
            continue
        company = location["company"]
        template = load_template(location["template"])
        if not template:
            revisar.append(
                f"Falta el template de Gusto {location['template']}, asi que no "
                f"se genera el CSV de esa empresa. El de la otra si sale.")
            continue

        by_employee = defaultdict(list)
        for row in template:
            by_employee[row["gusto_employee_id"]].append(row)

        rows = []
        for employee_id, template_rows in by_employee.items():
            primary = next((r for r in template_rows if "(Primary)" in r["title"]),
                           template_rows[0])
            for template_row in template_rows:
                title = template_row["title"]                      # byte por byte
                clean_title = title.replace(" (Primary)", "").strip()
                cell = hours_by_cell.get((employee_id, company, clean_title), {})

                is_primary = template_row is primary
                tip = tips_by_cell.get((company, employee_id, clean_title), {})

                record = {c: "" for c in GUSTO_COLUMNS}
                record["last_name"] = template_row["last_name"]
                record["first_name"] = template_row["first_name"]
                record["title"] = title
                record["gusto_employee_id"] = employee_id
                record["regular_hours"] = blank_or(cell.get("regular", 0.0))
                record["overtime_hours"] = blank_or(cell.get("overtime", 0.0))
                record["double_overtime_hours"] = blank_or(cell.get("double", 0.0))

                # Los tips se pagan por job: el reporte ya los trae asi.
                record[NON_CASH_TIPS_COLUMN] = blank_or(tip.get("non_cash", 0.0))
                if NON_CASH_TIPS_COLUMN != "cash_tips":
                    record["cash_tips"] = blank_or(tip.get("cash", 0.0))
                record["custom_earning_distributed_service_charges"] = \
                    blank_or(tip.get("gratuity", 0.0))

                # La prima es a nivel empleado: va solo en el renglon primario.
                if is_primary:
                    record[MEAL_PREMIUM_COLUMN] = blank_or(
                        premium_by_identity.get(employee_id, 0.0))
                rows.append(record)

        output[company] = rows

    hours_by_name = {}
    for guid, employee in employees.items():
        identity = f"SIN-MAPEO::{guid}"
        if identity in unmapped_hours:
            hours_by_name[employee["display"]] = unmapped_hours[identity]
    for name in pending:
        hours = hours_by_name.get(name, 0.0)
        held_back.append({
            "nombre": name.strip(),
            "horas": round(hours, 2),
            "tips": round(tips_held.get(name, tips_held.get(name.strip(), 0.0)), 2),
            "motivo": "sin alta en Gusto o sin gusto_employee_id confirmado",
            "que_hacer": "dar de alta en Gusto y volver a correr, o capturar a mano",
        })
        warnings.append(
            f"RETENIDO: {name.strip()} con {hours:.2f} h no entra al CSV porque no "
            f"tiene alta en Gusto. Hay que pagarle aparte o darlo de alta y repetir.")

    reported_held = {h["nombre"] for h in held_back}
    for who, amount in sorted(tips_held.items(), key=lambda x: -x[1]):
        if who.strip() in reported_held or amount <= 0.005:
            continue
        held_back.append({
            "nombre": who.strip(), "horas": 0.0, "tips": round(amount, 2),
            "motivo": "tiene tips en el reporte pero no se pudo resolver su identidad en Gusto",
            "que_hacer": "revisar si es un registro duplicado o de baja en Toast",
        })
        warnings.append(
            f"RETENIDO: ${amount:,.2f} de tips de {who.strip()} no entran al CSV "
            f"porque no se resolvio su identidad en Gusto.")

    # Turnos abiertos y auto-cerrados. audit_time_entry ya calcula las dos
    # banderas y _collect_exceptions las reporta, pero eso vive en la ruta de
    # staging: build(), que es la que genera el CSV que mueve dinero, nunca las
    # leia. Un turno sin clock-out entra con payable_hours = 0 y la jornada
    # completa de esa persona se paga en $0.00 sin un solo aviso.
    abiertos = []
    auto_cerrados = []
    for row in audited:
        dia = ca_overtime.parse_business_date(row["business_date"])
        if not (start <= dia <= end_inclusive):
            continue
        employee = employees.get(row["employee_guid"]) or {}
        if employee.get("excluir"):
            continue
        quien = employee.get("display", row["employee_guid"])
        if row["is_open_shift"]:
            abiertos.append((quien, row["business_date"], row["location"]))
        elif row["auto_clocked_out"]:
            auto_cerrados.append((quien, row["business_date"], row["location"]))

    for quien, dia, loc in sorted(abiertos):
        revisar.append(
            f"{quien} tiene un turno SIN clock-out el {dia} en {loc}. Sus horas "
            f"de ese dia valen 0 y no cuentan para las 40 semanales. Hay que "
            f"cerrarlo en Toast antes de generar el CSV.")
    for quien, dia, loc in sorted(auto_cerrados):
        warnings.append(
            f"{quien}: el turno del {dia} en {loc} lo cerro Toast automaticamente, "
            f"no la persona. Las horas pueden no reflejar lo que trabajo.")

    for clave, nombre in sorted(sin_confirmar.items()):
        revisar.append(
            f"{nombre} tiene registros en las DOS empresas de Gusto. Sus horas NO "
            f"se estan sumando para el overtime de California, que es lo que pasa "
            f"hoy sin la herramienta. Si deben sumarse (joint employer), pon 'si' "
            f"en persona_confirmada de sus dos filas en employee_map.csv y volve a "
            f"generar.")

    # B) CIERRE DE HORAS. No existia: hours_placed y hours_held se calculaban y
    # nunca se comparaban contra lo que entro, asi que una nomina completa podia
    # perderse y el programa devolvia exito. Es el guardian que habria atrapado
    # los bugs ya conocidos.
    hours_reported = 0.0
    for row in audited:
        dia = ca_overtime.parse_business_date(row["business_date"])
        if not (start <= dia <= end_inclusive):
            continue
        employee = employees.get(row["employee_guid"]) or {}
        if employee.get("excluir"):
            continue
        hours_reported += row["payable_hours"]

    placed = sum(c["cash"] + c["non_cash"] + c["gratuity"] for c in tips_by_cell.values())
    for quien, monto in sorted(tips_no_persona.items(), key=lambda x: -x[1]):
        held_back.append({
            "nombre": quien, "horas": 0.0, "tips": round(monto, 2),
            "motivo": "tips que el pool de Toast dejo en una cuenta que no es persona",
            "que_hacer": "revisar en Tips Manager a quien le corresponden; en "
                         "California los tips son propiedad de los empleados",
        })
        revisar.append(
            f"${monto:,.2f} de tips quedaron en la cuenta '{quien}', que no es una "
            f"persona, asi que no llegan a nadie. Decidir en Tips Manager a quien "
            f"corresponden; en California los tips son propiedad de los empleados.")

    gap = tips_reported_total - placed - sum(tips_held.values()) - sum(tips_no_persona.values())
    if abs(gap) > TOLERANCIA_TIPS:
        problems.append(
            f"Cierre de tips: el reporte trae ${tips_reported_total:,.2f}, se "
            f"ubicaron ${placed:,.2f} y ${sum(tips_held.values()):,.2f} quedaron "
            f"retenidos. Faltan ${gap:,.2f} sin explicar; no subir hasta cuadrarlo.")

    hours_placed = sum(c["regular"] + c["overtime"] + c["double"]
                       for c in hours_by_cell.values())
    # Limpieza de la lista de retenidos, ahora que ya se sabe quien cobro:
    #   - fuera quien no tiene ni horas ni tips (ruido que devalua la lista)
    #   - fuera quien ya aparece pagado en el CSV, porque pedirle al operador que
    #     le pague a mano seria pago doble
    pagados = {
        (f["last_name"] + " " + f["first_name"]).strip().lower()
        for filas in output.values() for f in filas
        if any(float(f[c] or 0) for c in ("regular_hours", "overtime_hours",
                                          "double_overtime_hours"))
    }

    def ya_cobro(nombre: str) -> bool:
        partes = {t for t in norm(nombre).replace(",", " ").split() if len(t) > 2}
        return any(len(partes & {t for t in norm(p).split() if len(t) > 2}) >= 2
                   for p in pagados)

    held_back[:] = [h for h in held_back
                    if (h["horas"] > 0.005 or h["tips"] > 0.005)
                    and not ya_cobro(h["nombre"])]

    hours_held = sum(h["horas"] for h in held_back)
    hueco_horas = hours_reported - hours_placed - hours_held
    if abs(hueco_horas) > TOLERANCIA_HORAS:
        problems.append(
            f"Cierre de HORAS: entraron {hours_reported:.2f} h, salieron "
            f"{hours_placed:.2f} h al CSV y {hours_held:.2f} h a retenidos. "
            f"Faltan {hueco_horas:.2f} h sin explicar; no subir hasta cuadrarlo.")
    # Diagnostico: lo que hace falta para revisar una corrida sin tener acceso a
    # la cuenta. Es agregado a proposito — conteos, totales y desgloses, sin
    # nombres ni correos — porque se pensó para pegarse en un chat o un correo.
    # Los nombres que si importan ya salen en problems/revisar/warnings.
    diag_loc = {}
    for location in locations:
        nombre_loc = location["short"]
        del_loc = [r for r in audited if r["location"] == nombre_loc]
        viol = defaultdict(int)
        for r in del_loc:
            for v in r["violations"]:
                viol[v] += 1
        meals = [b for r in del_loc for b in r["breaks"] if b["is_meal_type"]]
        diag_loc[nombre_loc] = {
            "mapeada": location["configured"],
            "template": (location["template"] if location["configured"] else ""),
            "turnos": len(del_loc),
            "turnos_arriba_de_6h": sum(
                1 for r in del_loc if r["work_period_hours"] > FIRST_MEAL_WAIVER_CEILING_HOURS),
            "turnos_abiertos": sum(1 for r in del_loc if r["is_open_shift"]),
            "turnos_asalariados": sum(1 for r in del_loc if r["is_salaried_shift"]),
            "horas_pagables": round(sum(r["payable_hours"] for r in del_loc), 2),
            "breaks_de_comida": len(meals),
            "breaks_no_tomados": sum(1 for b in meals if b["missed"]),
            "breaks_renunciados": sum(1 for b in meals if b["waived"]),
            "violaciones": dict(sorted(viol.items())),
        }

    diagnostico = {
        "semana_laboral_arranca": ca_overtime.WORKWEEK_START_WEEKDAY,
        "semana_laboral_confirmada": ca_overtime.WORKWEEK_START_CONFIRMED,
        "tope_prima_por_dia": MEAL_PREMIUM_HOURS_CAP_PER_DAY,
        "waivers_documentados": MEAL_WAIVERS_DOCUMENTED,
        "columna_de_tips": NON_CASH_TIPS_COLUMN,
        "columna_de_prima": MEAL_PREMIUM_COLUMN,
        "tolerancia_horas": TOLERANCIA_HORAS,
        "tolerancia_tips": TOLERANCIA_TIPS,
        "reportes_de_tips_recibidos": {
            comp: len(filas or []) for comp, filas in sorted((tip_reports or {}).items())},
        "por_sucursal": diag_loc,
    }

    return {"by_company": output, "problems": sorted(set(problems)),

            "revisar": sorted(set(revisar)),
            "hours_reported": round(hours_reported, 2),
            "hours_placed": round(hours_placed, 2),
            "hours_held": round(hours_held, 2),
            "tips_reported": round(tips_reported_total, 2),
            "tips_placed": round(placed, 2), "tips_held": tips_held,
            # Tips que el pool dejo en una cuenta que no es una persona. Es un
            # tercer bucket, no cero: sin exponerlo, quien reconcilia ve un
            # hueco fantasma del tamano de esas cuentas.
            "tips_no_persona": tips_no_persona,
            "warnings": sorted(set(warnings)), "held_back": held_back,
            "diagnostico": diagnostico}


def formatear_diagnostico(resultado: dict, start: date, end_inclusive: date) -> str:
    """Arma el reporte tecnico de una corrida, en texto, para pegar o adjuntar.

    Existe porque revisar una corrida desde afuera requiere los numeros, no la
    pantalla: cuantos turnos, cuantas violaciones de cada tipo, como cerro el
    cuadre, que avisos salieron. Es agregado y sin nombres ni correos porque se
    va a pegar en un chat; lo que lleva nombre ya viene en los avisos.
    """
    d = resultado.get("diagnostico") or {}
    L = []
    w = L.append

    w("=" * 72)
    w(f"  DIAGNOSTICO DE LA CORRIDA   {start} a {end_inclusive}")
    w("=" * 72)

    w("")
    w("CONFIGURACION")
    dias = ("lunes", "martes", "miercoles", "jueves", "viernes", "sabado", "domingo")
    arranque = d.get("semana_laboral_arranca")
    w(f"  semana laboral arranca      {dias[arranque] if isinstance(arranque, int) else '?'}"
      f"  (confirmada={d.get('semana_laboral_confirmada')})")
    w(f"  tope de prima por dia       {d.get('tope_prima_por_dia')} h")
    w(f"  waivers de meal documentados {d.get('waivers_documentados')}")
    w(f"  tips no-efectivo a la columna '{d.get('columna_de_tips')}'")
    w(f"  prima a la columna          '{d.get('columna_de_prima')}'")
    w(f"  tolerancia del cuadre       {d.get('tolerancia_horas')} h / "
      f"${d.get('tolerancia_tips')}")
    recibidos = d.get("reportes_de_tips_recibidos") or {}
    if recibidos:
        for comp, n in recibidos.items():
            w(f"  reporte de tips             {comp}: {n} renglones")
    else:
        w("  reporte de tips             NINGUNO (las columnas de tips van vacias)")

    w("")
    w("POR SUCURSAL")
    for nombre, s in (d.get("por_sucursal") or {}).items():
        w(f"  {nombre}   {'mapeada' if s['mapeada'] else 'SIN EMPRESA DE GUSTO'}")
        w(f"     turnos {s['turnos']}  (arriba de 6 h: {s['turnos_arriba_de_6h']}, "
          f"abiertos: {s['turnos_abiertos']}, asalariados: {s['turnos_asalariados']})")
        w(f"     horas pagables {s['horas_pagables']:,.2f}")
        w(f"     breaks de comida {s['breaks_de_comida']}  "
          f"(no tomados: {s['breaks_no_tomados']}, "
          f"renunciados: {s['breaks_renunciados']})")
        if s["violaciones"]:
            for tipo, n in s["violaciones"].items():
                w(f"        {tipo:24} {n}")
        else:
            w("        sin violaciones")

    w("")
    w("CIERRE DE CUENTAS")
    hr, hc, hh = (resultado.get("hours_reported", 0.0),
                  resultado.get("hours_placed", 0.0),
                  resultado.get("hours_held", 0.0))
    tr, tc = resultado.get("tips_reported", 0.0), resultado.get("tips_placed", 0.0)
    th = sum((resultado.get("tips_held") or {}).values())
    tn = sum((resultado.get("tips_no_persona") or {}).values())
    w(f"  horas  entro {hr:>10,.2f}  al CSV {hc:>10,.2f}  retenido {hh:>9,.2f}"
      f"  SIN EXPLICAR {hr - hc - hh:>+9,.2f}")
    w(f"  tips   entro {tr:>10,.2f}  al CSV {tc:>10,.2f}  retenido {th:>9,.2f}"
      f"  sin dueno {tn:>8,.2f}  SIN EXPLICAR {tr - tc - th - tn:>+9,.2f}")

    w("")
    w("LOS CSV")
    for company, filas in sorted((resultado.get("by_company") or {}).items()):
        def suma(col):
            return sum(float(f[col] or 0) for f in filas)
        con_algo = sum(1 for f in filas if any(
            (f[c] or "") for c in ("regular_hours", "overtime_hours",
                                   "double_overtime_hours", "missed_break_hours",
                                   NON_CASH_TIPS_COLUMN, "cash_tips",
                                   "custom_earning_distributed_service_charges")))
        w(f"  {company}")
        w(f"     renglones {len(filas)}  (con algun dato: {con_algo}, "
          f"en blanco: {len(filas) - con_algo})")
        w(f"     regular {suma('regular_hours'):>10,.2f}   "
          f"OT {suma('overtime_hours'):>8,.2f}   "
          f"DT {suma('double_overtime_hours'):>7,.2f}")
        w(f"     prima de meal {suma('missed_break_hours'):>7,.2f} h")
        w(f"     tips: paycheck {suma(NON_CASH_TIPS_COLUMN):>10,.2f}  "
          f"efectivo {suma('cash_tips'):>8,.2f}  "
          f"gratuity {suma('custom_earning_distributed_service_charges'):>9,.2f}")

    retenidos = [h for h in (resultado.get("held_back") or [])
                 if h["horas"] > 0 or h["tips"] > 0]
    if retenidos:
        w("")
        w(f"NO ENTRARON AL CSV ({len(retenidos)})")
        for h in retenidos:
            w(f"  {h['horas']:>8,.2f} h  ${h['tips']:>9,.2f}   {h['motivo']}")

    for etiqueta, clave in (("BLOQUEA", "problems"), ("PARA REVISAR", "revisar"),
                            ("AVISOS", "warnings")):
        items = resultado.get(clave) or []
        w("")
        w(f"{etiqueta} ({len(items)})")
        for item in items:
            w(f"  - {item}")

    w("")
    w("=" * 72)
    return "\n".join(L)


def main() -> int:
    parser = argparse.ArgumentParser(description="Genera el CSV de Gusto Smart Import.")
    parser.add_argument("--start", required=True, type=parse_day)
    parser.add_argument("--end", required=True, type=parse_day, help="Ultimo dia, inclusive")
    parser.add_argument("--tips-nc", default="", help="EmployeeTipTotals de National City")
    parser.add_argument("--tips-gl", default="", help="EmployeeTipTotals de Gaslamp")
    parser.add_argument("--out-dir", default=".")
    parser.add_argument("--force", action="store_true",
                        help="Escribe el CSV aunque haya problemas (no recomendado)")
    args = parser.parse_args()

    span = (args.end - args.start).days + 1
    if span != 14 and not args.force:
        print(f"El rango mide {span} dias y el periodo son 14 (cada dos viernes).",
              file=sys.stderr)
        return 2
    if args.end >= date.today() and not args.force:
        print(f"El periodo termina el {args.end} y hoy es {date.today()}: todavia "
              "hay gente fichando. Corre el export cuando el periodo cierre.",
              file=sys.stderr)
        return 2

    try:
        client = ToastClient.from_env()
    except RuntimeError as error:
        print(error, file=sys.stderr)
        return 1

    tip_reports = {}
    for guid, config in GUSTO_COMPANIES.items():
        path = args.tips_nc if config["short"] == "National City" else args.tips_gl
        try:
            tip_reports[config["company"]] = load_tip_report(path)
        except RuntimeError as error:
            print(error, file=sys.stderr)
            return 1
        if not path:
            print(f"  aviso: sin reporte de tips para {config['short']}; "
                  f"las columnas de tips saldran vacias", file=sys.stderr)

    print(f"Generando: {args.start} -> {args.end} ({span} dias)")
    result = build(client, args.start, args.end, tip_reports)

    if result.get("revisar"):
        print()
        print(f"  {len(result['revisar'])} cosa(s) PARA REVISAR. No impiden generar el")
        print("  CSV, pero conviene mirarlas antes de subirlo:")
        for item in result["revisar"]:
            print(f"    - {item}")

    if result.get("warnings"):
        print()
        print(f"  Avisos ({len(result['warnings'])}):")
        for warning in result["warnings"][:12]:
            print(f"    - {warning}")
        if len(result["warnings"]) > 12:
            print(f"    ... y {len(result['warnings']) - 12} mas")

    if result["problems"]:
        print()
        print("  " + "=" * 60)
        print("  NO SE PUEDE GENERAR: hay un descuadre que no se pudo atribuir.")
        print("  Esto no es una cosa para revisar: el archivo saldria incompleto")
        print("  y pareceria completo.")
        for problema in result["problems"]:
            print(f"    - {problema}")
        print("  " + "=" * 60)
        return 3

    os.makedirs(args.out_dir, exist_ok=True)
    stamp = f"{args.start:%Y%m%d}_{args.end:%Y%m%d}"

    for company, rows in sorted(result["by_company"].items()):
        if not rows:
            continue
        path = os.path.join(args.out_dir,
                            f"gusto_import_{slug(company)}_{stamp}.csv")
        write_csv(path, rows, GUSTO_COLUMNS)
        suma = lambda col: sum(float(r[col] or 0) for r in rows)
        print()
        print(f"  {company}: {len(rows)} renglones -> {path}")
        print(f"    regular {suma('regular_hours'):,.2f} h | "
              f"OT {suma('overtime_hours'):,.2f} h | "
              f"DT {suma('double_overtime_hours'):,.2f} h")
        print(f"    prima de meal {suma('missed_break_hours'):,.2f} h | "
              f"tips ${suma('paycheck_tips') + suma('cash_tips') + suma('custom_earning_distributed_service_charges'):,.2f}")

    held = result.get("held_back") or []
    if held:
        held_path = os.path.join(args.out_dir, f"retenidos_{stamp}.csv")
        write_csv(held_path, held, ["nombre", "horas", "tips", "motivo", "que_hacer"])
        print()
        print("  " + "!" * 60)
        print(f"  {len(held)} persona(s) NO entraron al CSV, con "
              f"{sum(h['horas'] for h in held):.2f} h sin pagar:")
        for item in held:
            print(f"    {item['nombre']:<28} {item['horas']:>8.2f} h  "
                  f"${item.get('tips', 0):>9,.2f} de tips")
        print(f"  Detalle: {held_path}")
        print("  " + "!" * 60)

    print()
    print("  Revisa el CSV antes de subirlo. Se sube en:")
    print("    Gusto > Pay > Run payroll > Import payroll data > Upload")
    return 5 if held else 0


if __name__ == "__main__":
    sys.exit(main())
