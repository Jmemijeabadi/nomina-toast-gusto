"""Tests del pipeline de nomina. Correr ANTES de cada nomina.

    python test_payroll.py

Los tests de la clase TestOvertime y TestCalendario son puros: no tocan la red y
corren en milisegundos. Los de TestReconciliacion llaman a Toast y necesitan
TOAST_CLIENT_ID y TOAST_CLIENT_SECRET; si no estan, se saltan.

Lo que se prueba es lo que cuesta dinero si se rompe:
  - las reglas de overtime y double time de California, con casos conocidos
  - que el calendario de nomina coincida con los pay periods reales de Gusto
  - que el CSV respete el contrato del Smart Import (vacio nunca 0.0, la columna
    del custom earning siempre vacia, los titles byte por byte del template)
  - que TODA hora y TODO dolar que entra salga: al CSV o a los retenidos
"""

from __future__ import annotations

import csv
import os
import unittest
from datetime import date

import ca_overtime
import toast_payroll
from ca_overtime import compute_overtime, workweek_start
from toast_payroll import pay_period_containing

SABADO = 5


class TestOvertime(unittest.TestCase):
    """Reglas de California sobre casos armados a mano."""

    def split(self, horas_por_dia: dict) -> dict:
        resultado = compute_overtime({"X": horas_por_dia}, SABADO)["X"]
        return resultado["totals"]

    def test_jornada_de_8_horas_es_toda_recta(self):
        self.assertEqual(self.split({"20260907": 8.0}),
                         {"regular": 8.0, "overtime": 0.0, "double": 0.0})

    def test_mas_de_8_en_el_dia_paga_1_5x(self):
        self.assertEqual(self.split({"20260907": 10.0}),
                         {"regular": 8.0, "overtime": 2.0, "double": 0.0})

    def test_mas_de_12_en_el_dia_paga_2x(self):
        self.assertEqual(self.split({"20260907": 14.0}),
                         {"regular": 8.0, "overtime": 4.0, "double": 2.0})

    def test_no_piramida_la_regla_diaria_con_la_semanal(self):
        """5 dias de 10 h son 50 h: 40 rectas y 10 a 1.5x, no 40 y 10 contadas dos veces."""
        dias = {d: 10.0 for d in ("20260905", "20260906", "20260907",
                                  "20260908", "20260909")}
        self.assertEqual(self.split(dias),
                         {"regular": 40.0, "overtime": 10.0, "double": 0.0})

    def test_mas_de_40_rectas_en_la_semana_paga_1_5x(self):
        """6 dias de 8 h son 48 h rectas: 40 quedan rectas y 8 pasan a 1.5x."""
        dias = {d: 8.0 for d in ("20260905", "20260906", "20260907",
                                 "20260908", "20260909", "20260910")}
        self.assertEqual(self.split(dias),
                         {"regular": 40.0, "overtime": 8.0, "double": 0.0})

    def test_septimo_dia_consecutivo_no_tiene_tiempo_recto(self):
        """7 dias de 8 h: el septimo va completo a 1.5x, y las 40 semanales
        empujan otras 8 h. Quedan 40 rectas y 16 a 1.5x."""
        dias = {d: 8.0 for d in ("20260905", "20260906", "20260907", "20260908",
                                 "20260909", "20260910", "20260911")}
        self.assertEqual(self.split(dias),
                         {"regular": 40.0, "overtime": 16.0, "double": 0.0})

    def test_septimo_dia_arriba_de_8_horas_paga_2x(self):
        dias = {d: 4.0 for d in ("20260905", "20260906", "20260907", "20260908",
                                 "20260909", "20260910")}
        dias["20260911"] = 10.0
        totales = self.split(dias)
        self.assertEqual(totales["double"], 2.0, "las horas 9 y 10 del 7mo dia van a 2x")

    def test_el_horario_de_verano_no_se_come_una_hora(self):
        """La noche en que termina el horario de verano, un turno de 22:00 a
        03:00 duro 6 h reales. Restar reloj de pared da 5 h y se pierde una
        hora de trabajo mas la prima de meal de ese turno."""
        from zoneinfo import ZoneInfo
        from datetime import datetime
        from toast_payroll import add_work_hours, elapsed_hours
        tz = ZoneInfo("America/Los_Angeles")
        entra = datetime(2026, 10, 31, 22, 0, tzinfo=tz)
        sale = datetime(2026, 11, 1, 3, 0, tzinfo=tz)
        self.assertAlmostEqual(elapsed_hours(entra, sale), 6.0, places=2)
        self.assertNotAlmostEqual((sale - entra).total_seconds() / 3600, 6.0, places=2,
                                  msg="la resta directa deberia dar 5 h: es el bug")
        # El deadline del meal se cuenta sobre horas trabajadas, no de reloj
        self.assertAlmostEqual(
            elapsed_hours(entra, add_work_hours(entra, 5.0)), 5.0, places=2)

    def test_la_semana_laboral_arranca_sabado(self):
        self.assertEqual(ca_overtime.WORKWEEK_START_WEEKDAY, SABADO)
        self.assertTrue(ca_overtime.WORKWEEK_START_CONFIRMED,
                        "el inicio de semana debe estar confirmado contra Gusto")
        # Un sabado abre su propia semana; el viernes siguiente la cierra.
        self.assertEqual(workweek_start(date(2026, 9, 5), SABADO), date(2026, 9, 5))
        self.assertEqual(workweek_start(date(2026, 9, 11), SABADO), date(2026, 9, 5))
        self.assertEqual(workweek_start(date(2026, 9, 12), SABADO), date(2026, 9, 12))

    def test_las_horas_nunca_se_crean_ni_se_destruyen(self):
        dias = {"20260905": 13.5, "20260906": 7.25, "20260910": 9.0, "20260911": 11.0}
        totales = self.split(dias)
        self.assertAlmostEqual(sum(totales.values()), sum(dias.values()), places=2)


class TestCalendario(unittest.TestCase):
    """El calendario debe coincidir con los pay periods reales de Gusto."""

    # Leidos del MCP de Gusto: (un dia dentro, inicio, fin, fecha de cheque)
    PERIODOS_REALES = [
        (date(2026, 8, 1), "2026-07-25", "2026-08-07", "2026-08-14"),
        (date(2026, 8, 15), "2026-08-08", "2026-08-21", "2026-08-28"),
        (date(2026, 8, 30), "2026-08-22", "2026-09-04", "2026-09-11"),
        (date(2026, 9, 10), "2026-09-05", "2026-09-18", "2026-09-25"),
        (date(2026, 9, 25), "2026-09-19", "2026-10-02", "2026-10-09"),
        (date(2026, 10, 10), "2026-10-03", "2026-10-16", "2026-10-23"),
        (date(2026, 11, 5), "2026-10-31", "2026-11-13", "2026-11-20"),
    ]

    def test_coincide_con_los_pay_periods_de_gusto(self):
        for dentro, inicio, fin, cheque in self.PERIODOS_REALES:
            with self.subTest(dia=dentro):
                s, e, c = pay_period_containing(dentro)
                self.assertEqual((str(s), str(e), str(c)), (inicio, fin, cheque))

    def test_todo_periodo_mide_14_dias_y_corre_sabado_a_viernes(self):
        for dentro, _, _, _ in self.PERIODOS_REALES:
            s, e, _ = pay_period_containing(dentro)
            with self.subTest(dia=dentro):
                self.assertEqual((e - s).days + 1, 14)
                self.assertEqual(s.weekday(), SABADO, "el periodo arranca sabado")
                self.assertEqual(e.weekday(), 4, "el periodo cierra viernes")

    def test_el_periodo_son_exactamente_dos_semanas_laborales(self):
        """Si no lo fueran, Gusto repartiria mal el Regular Rate of Pay."""
        for dentro, _, _, _ in self.PERIODOS_REALES:
            s, e, _ = pay_period_containing(dentro)
            semanas = {workweek_start(s, SABADO), workweek_start(e, SABADO)}
            with self.subTest(dia=dentro):
                self.assertEqual(len(semanas), 2, "ni una semana partida")


class TestContratoDelCSV(unittest.TestCase):
    """El CSV debe respetar el contrato del Smart Import de Gusto."""

    @classmethod
    def setUpClass(cls):
        cls.csvs = []
        for nombre in os.listdir(CSV_DIR) if os.path.isdir(CSV_DIR) else []:
            if nombre.startswith("gusto_import_") and nombre.endswith(".csv"):
                with open(os.path.join(CSV_DIR, nombre), newline="",
                          encoding="utf-8") as handle:
                    cls.csvs.append((nombre, list(csv.DictReader(handle))))
        if not cls.csvs:
            raise unittest.SkipTest(
                f"no hay CSV generados en {CSV_DIR}; corre gusto_export.py primero")

    def test_ninguna_celda_dice_cero_punto_cero(self):
        """En Smart Import los ceros SOBRESCRIBEN. Un 0.0 afirma un cero que
        nadie calculo, asi que una columna sin dato va vacia."""
        for nombre, filas in self.csvs:
            malas = [(i, k) for i, r in enumerate(filas, 2)
                     for k, v in r.items() if v == "0.0"]
            with self.subTest(csv=nombre):
                self.assertEqual(malas, [], f"celdas con 0.0 en {nombre}: {malas[:5]}")

    def test_el_custom_earning_de_la_prima_queda_vacio(self):
        """La prima va en missed_break_hours. Llenar las dos columnas la paga
        dos veces porque Gusto no las deduplica."""
        for nombre, filas in self.csvs:
            llenas = [r["last_name"] for r in filas
                      if r.get("custom_earning_meal_break_violation")]
            with self.subTest(csv=nombre):
                self.assertEqual(llenas, [], f"prima duplicada en {nombre}: {llenas}")

    def test_las_filas_son_exactamente_las_del_template(self):
        """Mismo numero, mismo orden y el title byte por byte: es lo unico que
        le dice a Gusto a que job va cada renglon."""
        for nombre, filas in self.csvs:
            plantilla = ("gusto_template_bonita.csv" if "bonita" in nombre
                         else "gusto_template_gaslamp.csv")
            if not os.path.exists(plantilla):
                continue
            with open(plantilla, newline="", encoding="utf-8-sig") as handle:
                esperadas = list(csv.DictReader(handle))
            with self.subTest(csv=nombre):
                self.assertEqual(len(filas), len(esperadas))
                for salida, esperada in zip(filas, esperadas):
                    self.assertEqual(salida["title"], esperada["title"])
                    self.assertEqual(salida["gusto_employee_id"],
                                     esperada["gusto_employee_id"])

    def test_la_prima_nunca_pasa_de_14_horas_por_periodo(self):
        """Tope de 1 h por dia por persona: en 14 dias, 14 h es el maximo."""
        for nombre, filas in self.csvs:
            for fila in filas:
                horas = float(fila.get("missed_break_hours") or 0)
                with self.subTest(csv=nombre, quien=fila["last_name"]):
                    self.assertLessEqual(horas, 14.0)

    def test_ninguna_fila_viene_sin_identidad(self):
        for nombre, filas in self.csvs:
            sin_id = [r["last_name"] for r in filas if not r["gusto_employee_id"]]
            with self.subTest(csv=nombre):
                self.assertEqual(sin_id, [])


class TestReconciliacion(unittest.TestCase):
    """Toda hora y todo dolar que entra debe salir: al CSV o a los retenidos."""

    @classmethod
    def setUpClass(cls):
        if not (os.environ.get("TOAST_CLIENT_ID")
                and os.environ.get("TOAST_CLIENT_SECRET")):
            raise unittest.SkipTest("sin credenciales de Toast")
        cls.client = toast_payroll.ToastClient.from_env()
        cls.locations = toast_payroll.resolve_locations(cls.client)

    def test_toast_reporta_las_dos_empresas_y_las_dos_estan_mapeadas(self):
        self.assertEqual(len(self.locations), 2)
        for location in self.locations:
            with self.subTest(location=location["toast_name"]):
                self.assertTrue(location["configured"],
                                "location sin empresa de Gusto: sus horas se caerian")
                self.assertTrue(os.path.exists(location["template"]),
                                f"falta el template {location['template']}")

    def test_las_horas_de_un_periodo_cerrado_cuadran_con_el_csv(self):
        inicio, fin, _ = toast_payroll.previous_closed_pay_period()
        total_toast = 0.0
        for location in self.locations:
            empleados = {e["guid"]: e for e in self.client.get_employees(location["guid"])}
            for turno in self.client.get_time_entries_by_business_date(
                    location["guid"], inicio, fin):
                if turno.get("deleted"):
                    continue
                empleado = empleados.get(
                    (turno.get("employeeReference") or {}).get("guid")) or {}
                nombre = (f"{empleado.get('lastName','')} "
                          f"{empleado.get('chosenName') or empleado.get('firstName','')}").lower()
                if any(m in nombre for m in toast_payroll.NON_PERSON_MARKERS):
                    continue
                total_toast += (float(turno.get("regularHours") or 0)
                                + float(turno.get("overtimeHours") or 0))

        marca = f"{inicio:%Y%m%d}_{fin:%Y%m%d}"
        total_csv = 0.0
        encontrados = 0
        for location in self.locations:
            slug = ("bonita" if location["short"] == "National City" else "gaslamp")
            ruta = os.path.join(CSV_DIR, f"gusto_import_{slug}_{marca}.csv")
            if not os.path.exists(ruta):
                continue
            encontrados += 1
            with open(ruta, newline="", encoding="utf-8") as handle:
                for fila in csv.DictReader(handle):
                    total_csv += sum(float(fila[c] or 0) for c in
                                     ("regular_hours", "overtime_hours",
                                      "double_overtime_hours"))
        if encontrados < 2:
            self.skipTest(f"faltan CSV del periodo {marca}; corre gusto_export.py")

        retenidas = 0.0
        ruta = os.path.join(CSV_DIR, f"retenidos_{marca}.csv")
        if os.path.exists(ruta):
            with open(ruta, newline="", encoding="utf-8") as handle:
                retenidas = sum(float(f["horas"] or 0) for f in csv.DictReader(handle))

        self.assertAlmostEqual(
            total_toast, total_csv + retenidas, places=1,
            msg=(f"horas sin explicar en {marca}: Toast {total_toast:.2f}, "
                 f"CSV {total_csv:.2f}, retenidas {retenidas:.2f}"))


class TestIdentidad(unittest.TestCase):
    """Pagarle a la persona equivocada es el error mas caro posible."""

    def test_apellidos_compuestos_que_solo_comparten_uno_no_son_la_misma_persona(self):
        """Con apellidos compuestos, aceptar UN token coincidente hacia que dos
        apellidos que solo comparten el primero se tomaran por la misma persona,
        y de ahi salia un match "seguro" apuntando a otra."""
        from build_maps import _last_name_matches, norm
        casos = [
            # (apellido en Toast, apellido en Gusto, misma persona?)
            # Apellidos inventados: el repo es publico y el padron real no va ahi.
            ("Olmeda", "Olmedas Quintero", True),      # truncado + falta de dedo
            ("Montero", "Vega Montero", True),         # compuesto truncado
            ("Olmedo Fernandes", "Olmedo Fernandez", True),
            ("Belmote Zarate", "Belmonte Zarate", True),
            ("Nava", "Nava Quiroz", True),
            ("Elias Verano", "Verano", True),            # subconjunto
            ("Aguirre Pardo", "Aguirre Solis", False),   # dos personas
            ("Aguirre Pardo", "Aguirre Mendez", False),  # dos personas
            ("Nava", "Navarro", False),   # corto contenido en otro mas largo
        ]
        for toast, gusto, esperado in casos:
            with self.subTest(toast=toast, gusto=gusto):
                self.assertEqual(_last_name_matches(norm(toast), gusto), esperado)


class TestReporteDeTips(unittest.TestCase):
    """Un reporte mal parseado daba ceros, y el cierre de tips daba 0.00 contra
    si mismo: la app decia 'cuadra' con los tips en blanco para 72 personas."""

    def test_rechaza_un_archivo_que_no_es_el_reporte(self):
        import io as _io
        import gusto_export
        cualquiera = "\n".join(["a,b,c", "1,2,3"]).encode()
        el_staging = "\n".join(["email,last_name,regular_hours",
                                "x@y.z,Perez,40"]).encode()
        for contenido in (cualquiera, el_staging):
            with self.subTest(contenido=contenido[:20]):
                with self.assertRaises(RuntimeError) as capturado:
                    gusto_export.load_tip_report(_io.BytesIO(contenido))
                self.assertIn("columnas", str(capturado.exception))

    def test_acepta_el_reporte_de_verdad(self):
        import gusto_export
        ruta = "reference/tips_GL_2026-09-05_2026-09-18.csv"
        if not os.path.exists(ruta):
            self.skipTest("sin reporte de referencia")
        filas = gusto_export.load_tip_report(ruta)
        self.assertGreater(len(filas), 0)
        self.assertTrue(all("non_cash" in f for f in filas))


CSV_DIR = os.environ.get("PAYROLL_CSV_DIR", ".")


if __name__ == "__main__":
    unittest.main(verbosity=2)
