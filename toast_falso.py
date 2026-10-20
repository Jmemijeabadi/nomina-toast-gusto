"""Un Toast de mentiras, para probar los casos que los datos reales no tienen.

Varios bugs de esta herramienta no disparan con la cuenta como esta hoy: hacen
falta dos registros de Toast para la misma persona, o alguien que sigue fichando
despues de salir de Gusto, o un turno con un job que no existe en el template.
Esperar a que pasen en produccion es esperar a pagarle mal a alguien.

Con esto el escenario se arma a mano y se corre el exportador completo contra
el, asi que lo que se prueba es el camino de verdad y no una pieza aislada.

    esc = Escenario()
    ana = esc.empleado("Gaslamp Quarter", "Ruiz", "Ana")
    esc.turno(ana, "2026-09-07", horas=8.0, job="Taquero")
    resultado = esc.correr(date(2026, 9, 5), date(2026, 9, 18))
"""

from __future__ import annotations

import csv
import os
import tempfile
from datetime import date, datetime, timedelta, timezone

import toast_payroll

# Los dos guid de location tienen que ser los de GUSTO_COMPANIES para que
# resolve_locations las reconozca como configuradas.
GUIDS = {
    "National City": "2bef5cef-4e30-4d2a-860f-d43406a5ff29",
    "Gaslamp Quarter": "ac4c6cbd-8723-45cb-bf16-6d31369c5b50",
}
# Un break de comida valido, del tipo que la cuenta usa de verdad.
TIPO_MEAL = sorted(toast_payroll.MEAL_BREAK_TYPE_GUIDS)[0]


class ClienteFalso:
    """Responde como ToastClient pero con lo que le pongas."""

    def __init__(self, empleados, turnos, jobs):
        self._empleados = empleados      # {location_guid: [empleado, ...]}
        self._turnos = turnos            # {(location_guid, "YYYYMMDD"): [turno, ...]}
        self._jobs = jobs                # {location_guid: [job, ...]}

    def get_restaurants(self):
        return [{"restaurantGuid": g} for g in self._empleados]

    def get_restaurant_config(self, restaurant_guid):
        return {"general": {"timeZone": toast_payroll.TOAST_TIMEZONE},
                "closeoutHour": 4}

    def get_employees(self, restaurant_guid):
        return list(self._empleados.get(restaurant_guid, []))

    def get_jobs(self, restaurant_guid):
        return list(self._jobs.get(restaurant_guid, []))

    def get_time_entries_by_business_date(self, restaurant_guid, start, end_inclusive):
        salida, dia = [], start
        while dia <= end_inclusive:
            salida.extend(self._turnos.get(
                (restaurant_guid, dia.strftime("%Y%m%d")), []))
            dia += timedelta(days=1)
        return salida

    def _get(self, path, restaurant_guid, params=None):
        if "timeEntries" in path:
            stamp = (params or {}).get("businessDate", "")
            return list(self._turnos.get((restaurant_guid, stamp), []))
        return []


class Escenario:
    """Arma empleados, turnos, mapeo y templates, y corre el exportador."""

    def __init__(self):
        self._empleados = {g: [] for g in GUIDS.values()}
        self._turnos = {}
        self._jobs = {g: [] for g in GUIDS.values()}
        self._jobs_vistos = {}
        self._mapa = []          # filas de employee_map.csv
        self._templates = {}     # {company: [fila, ...]}
        self._n = 0
        # Si los jobs de Toast se mapean a un title igual. Con False el
        # escenario toma el camino de "job sin mapeo", que es otro bug.
        self.mapear_jobs = True
        # Jobs que NO entran al job_map, para escenarios donde unos
        # puestos estan mapeados y otros no — que es el caso real cuando
        # alguien abre un puesto nuevo en Toast.
        self.jobs_sin_mapear = set()
        self._dir = tempfile.mkdtemp(prefix="toast_falso_")

    # ── armar el escenario ──

    def empleado(self, sucursal, apellido, nombre, *, gusto_id=None,
                 accion="", telefono="", email="", en_template=True,
                 chosen=None, borrado=False, job="Taquero"):
        """Agrega un empleado de Toast y, si se pide, su renglon de Gusto."""
        self._n += 1
        guid = f"emp-{self._n:04d}"
        loc = GUIDS[sucursal]
        self._empleados[loc].append({
            "guid": guid, "lastName": apellido, "firstName": nombre,
            "chosenName": chosen, "email": email, "phoneNumber": telefono,
            "deleted": borrado, "jobReferences": [],
            "externalEmployeeId": "",
        })
        self._mapa.append({
            "toast_guid": guid, "toast_location": sucursal,
            "toast_name": f"{apellido}, {chosen or nombre}",
            "toast_email": email, "toast_phone": telefono,
            "person_key": "", "persona_confirmada": "",
            "gusto_employee_id": gusto_id or "", "gusto_name": "",
            "confianza": "exacto" if gusto_id else "", "accion": accion, "nota": "",
        })
        if gusto_id and en_template:
            self.renglon_de_gusto(sucursal, apellido, nombre, gusto_id,
                                  job=job, primario=True)
        return guid

    def renglon_de_gusto(self, sucursal, apellido, nombre, gusto_id, *,
                         job="Taquero", primario=False):
        """Un renglon del template de Gusto para esa empresa."""
        company = toast_payroll.GUSTO_COMPANIES[GUIDS[sucursal]]["company"]
        titulo = f"{job} (Primary)" if primario else job
        self._templates.setdefault(company, []).append({
            "last_name": apellido, "first_name": nombre, "title": titulo,
            "gusto_employee_id": gusto_id,
        })

    def turno(self, guid_empleado, dia, *, horas=8.0, job="Taquero",
              con_meal=False, sucursal=None):
        """Un turno. `dia` es 'YYYY-MM-DD'."""
        loc = GUIDS[sucursal] if sucursal else next(
            g for g, lista in self._empleados.items()
            if any(e["guid"] == guid_empleado for e in lista))

        if job not in self._jobs_vistos:
            jguid = f"job-{len(self._jobs_vistos):03d}"
            self._jobs_vistos[job] = jguid
            for lista in self._jobs.values():
                lista.append({"guid": jguid, "title": job})
        jguid = self._jobs_vistos[job]

        d = date.fromisoformat(dia)
        # Aritmetica de fechas de verdad: un turno largo cruza la medianoche, y
        # armar la hora como texto daba "30:00" para un turno de 14 h.
        inicio_dt = datetime(d.year, d.month, d.day, 16, 0, tzinfo=timezone.utc)
        fin_dt = inicio_dt + timedelta(hours=horas)
        breaks = []
        if con_meal:
            breaks = [{
                "guid": f"b-{guid_empleado}-{dia}",
                "breakType": {"guid": TIPO_MEAL},
                "inDate": f"{dia}T20:00:00.000+0000",
                "outDate": f"{dia}T20:32:00.000+0000",
                "paid": False, "missed": False, "waived": False, "deleted": False,
            }]
        turno = {
            "guid": f"te-{guid_empleado}-{dia}-{job}",
            "businessDate": int(d.strftime("%Y%m%d")), "deleted": False,
            "inDate": inicio_dt.strftime("%Y-%m-%dT%H:%M:%S.000+0000"),
            "outDate": fin_dt.strftime("%Y-%m-%dT%H:%M:%S.000+0000"),
            "regularHours": horas, "overtimeHours": 0.0,
            "hourlyWage": 18.0, "breaks": breaks,
            "employeeReference": {"guid": guid_empleado},
            "jobReference": {"guid": jguid},
        }
        self._turnos.setdefault((loc, d.strftime("%Y%m%d")), []).append(turno)
        return turno

    # ── correr ──

    def _escribir(self):
        import build_maps
        ruta_mapa = os.path.join(self._dir, "employee_map.csv")
        with open(ruta_mapa, "w", newline="", encoding="utf-8") as h:
            w = csv.DictWriter(h, fieldnames=build_maps.EMPLOYEE_MAP_COLUMNS,
                               extrasaction="ignore")
            w.writeheader()
            for fila in self._mapa:
                w.writerow({c: fila.get(c, "") for c in build_maps.EMPLOYEE_MAP_COLUMNS})

        rutas = {}
        for guid, cfg in toast_payroll.GUSTO_COMPANIES.items():
            company = cfg["company"]
            ruta = os.path.join(self._dir, cfg["template"])
            columnas = ["last_name", "first_name", "title", "gusto_employee_id"]
            with open(ruta, "w", newline="", encoding="utf-8") as h:
                w = csv.DictWriter(h, fieldnames=columnas, extrasaction="ignore")
                w.writeheader()
                for fila in self._templates.get(company, []):
                    w.writerow(fila)
            rutas[company] = ruta
        # Cada job de Toast mapeado a un title igual, que es el caso normal.
        # Con mapear_jobs=False el job_map queda vacio y el escenario ejercita
        # el camino de "job sin mapeo", que es un bug distinto: hay que pedirlo
        # a proposito para que un test no crea estar probando otra cosa.
        columnas_job = ["toast_location", "toast_job", "gusto_title",
                        "confianza", "accion", "nota"]
        with open(os.path.join(self._dir, "job_map.csv"), "w", newline="",
                  encoding="utf-8") as h:
            w = csv.DictWriter(h, fieldnames=columnas_job, extrasaction="ignore")
            w.writeheader()
            if self.mapear_jobs:
                for sucursal in GUIDS:
                    for job in self._jobs_vistos:
                        if job in self.jobs_sin_mapear:
                            continue
                        w.writerow({"toast_location": sucursal, "toast_job": job,
                                    "gusto_title": job, "confianza": "exacto"})
        return ruta_mapa

    def correr(self, inicio, fin, tip_reports=None):
        """Corre gusto_export.build contra este escenario."""
        import gusto_export
        self._escribir()
        # DATA_DIR se lee del entorno al IMPORTAR el modulo, asi que poner la
        # variable de entorno aqui llega tarde: hay que mover el atributo.
        anterior_dir = toast_payroll.DATA_DIR
        anterior_mapa = toast_payroll.EMPLOYEE_MAP_PATH
        toast_payroll.DATA_DIR = self._dir
        toast_payroll.EMPLOYEE_MAP_PATH = ""
        try:
            cliente = ClienteFalso(self._empleados, self._turnos, self._jobs)
            return gusto_export.build(cliente, inicio, fin, tip_reports or {})
        finally:
            toast_payroll.DATA_DIR = anterior_dir
            toast_payroll.EMPLOYEE_MAP_PATH = anterior_mapa

    def limpiar(self):
        import shutil
        shutil.rmtree(self._dir, ignore_errors=True)


def hueco_de_horas(resultado: dict) -> float:
    """Lo que entro menos lo que salio al CSV y a retenidos."""
    return (resultado["hours_reported"] - resultado["hours_placed"]
            - resultado["hours_held"])


def horas_escritas(resultado: dict) -> float:
    """Las horas que de verdad quedaron en los renglones del CSV."""
    return sum(
        float(fila[columna] or 0)
        for filas in resultado["by_company"].values() for fila in filas
        for columna in ("regular_hours", "overtime_hours", "double_overtime_hours"))
