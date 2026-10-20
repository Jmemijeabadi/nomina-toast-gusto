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
import io
import os
import re
import unittest
import urllib.error
import urllib.request
from datetime import date

import build_maps
import ca_overtime
import gusto_export
import toast_falso
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

    def test_toda_location_que_trabajo_esta_mapeada(self):
        """Fijar el numero de locations en 2 hacia fallar este test cada vez que
        abriera una sucursal. Lo que importa no es cuantas hay sino que ninguna
        con horas se quede fuera de Gusto: una sucursal dada de alta pero sin
        turnos todavia no le debe dinero a nadie."""
        inicio, fin, _ = toast_payroll.previous_closed_pay_period()
        self.assertGreaterEqual(len(self.locations), 2)
        medidas = 0
        for location in self.locations:
            with self.subTest(location=location["toast_name"]):
                turnos = self.client.get_time_entries_by_business_date(
                    location["guid"], inicio, fin)
                horas = sum(
                    toast_payroll.audit_time_entry(t)["payable_hours"]
                    for t in turnos if not t.get("deleted"))
                if horas <= 0.005:
                    continue
                medidas += 1
                self.assertTrue(
                    location["configured"],
                    f"{location['toast_name']} trabajo {horas:.2f} h y no tiene "
                    f"empresa de Gusto: esas horas se caerian")
                self.assertTrue(os.path.exists(location["template"]),
                                f"falta el template {location['template']}")

        # Sin esto el test pasaba vacio: si ninguna location reporta horas, todo
        # el ciclo hace continue y no se afirma nada. Un periodo cerrado del
        # negocio operando SIEMPRE tiene horas en algun lado.
        self.assertGreater(medidas, 0,
                           f"ninguna location reporto horas en {inicio} a {fin}: "
                           f"el test no probo nada, revisa credenciales o periodo")

    def test_nadie_se_cae_de_la_lista_de_retenidos(self):
        """Si se avisa que alguien queda retenido, tiene que estar en la lista.

        Un filtro comparaba nombres y borraba de `held_back` a quien "ya parecia
        pagado": "Madriles, Abraham Isaac" comparte dos palabras con "Madriles
        Gonzalez, Abraham", que es otra persona, y sus 6.56 h se evaporaron del
        cuadre. El aviso ya estaba emitido, asi que el nombre se veia en pantalla
        mientras sus horas no estaban en ningun lado. Esta es la invariante que
        eso rompia.
        """
        inicio, fin, _ = toast_payroll.previous_closed_pay_period()
        resultado = gusto_export.build(self.client, inicio, fin, {})

        avisados = set()
        for aviso in resultado["warnings"]:
            m = re.match(r"RETENIDO: (.+?) con [\d.,]+ h no entra al CSV", aviso)
            if m:
                avisados.add(m.group(1).strip())
        listados = {h["nombre"].strip() for h in resultado["held_back"]}

        faltantes = sorted(avisados - listados)
        self.assertEqual(
            faltantes, [],
            f"se aviso que quedaban retenidos pero no estan en la lista, asi que "
            f"sus horas no cuentan en el cuadre: {faltantes}")

    def test_el_cuadre_de_horas_cierra_sin_hueco(self):
        """entro == al CSV + retenido. Si no, hay horas que no estan en ningun lado."""
        inicio, fin, _ = toast_payroll.previous_closed_pay_period()
        resultado = gusto_export.build(self.client, inicio, fin, {})
        hueco = (resultado["hours_reported"] - resultado["hours_placed"]
                 - resultado["hours_held"])
        self.assertLessEqual(
            abs(hueco), gusto_export.TOLERANCIA_HORAS,
            f"{hueco:+.2f} h sin explicar en {inicio} a {fin}: entraron "
            f"{resultado['hours_reported']:.2f}, al CSV {resultado['hours_placed']:.2f}, "
            f"retenidas {resultado['hours_held']:.2f}")

    def test_las_horas_de_un_periodo_cerrado_cuadran_con_el_csv(self):
        inicio, fin, _ = toast_payroll.previous_closed_pay_period()
        # Solo las locations con empresa de Gusto, y las MISMAS en los dos lados
        # de la igualdad. Mezclarlas comparaba poblaciones distintas: una
        # sucursal sin mapear sumaba horas en Toast y no tenia CSV donde salir.
        mapeadas = [l for l in self.locations if l["configured"]]
        total_toast = 0.0
        for location in mapeadas:
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
        for location in mapeadas:
            # Derivado de la empresa, no de un else: con tres locations el else
            # mandaba la tercera al CSV de gaslamp y lo contaba dos veces.
            ruta = os.path.join(
                CSV_DIR,
                f"gusto_import_{gusto_export.slug(location['company'])}_{marca}.csv")
            if not os.path.exists(ruta):
                continue
            encontrados += 1
            with open(ruta, newline="", encoding="utf-8") as handle:
                for fila in csv.DictReader(handle):
                    total_csv += sum(float(fila[c] or 0) for c in
                                     ("regular_hours", "overtime_hours",
                                      "double_overtime_hours"))
        if encontrados < len(mapeadas):
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


class TestReintento(unittest.TestCase):
    """El reintento ante 429 tiene que reintentar de verdad.

    Se agrego sin test y no servia: `import time` quedaba pisado por el `time`
    de datetime, asi que `time.sleep` lanzaba AttributeError y la corrida moria
    en el PRIMER 429, mas opaca que antes del cambio. Una nomina de 14 dias son
    cientos de llamadas; un 429 es cuestion de tiempo.
    """

    def setUp(self):
        self.client = toast_payroll.ToastClient("x" * 32, "y" * 64)
        self.esperas = []
        self._sleep_real = toast_payroll.sleep
        toast_payroll.sleep = self.esperas.append
        self._urlopen_real = toast_payroll.urllib.request.urlopen

    def tearDown(self):
        toast_payroll.sleep = self._sleep_real
        toast_payroll.urllib.request.urlopen = self._urlopen_real

    def _falsear(self, respuestas):
        """respuestas: lista de codigos HTTP, o None para una respuesta buena."""
        self.llamadas = []

        class Buena:
            def read(self_inner):
                return b'{"ok": true}'
            def __enter__(self_inner):
                return self_inner
            def __exit__(self_inner, *a):
                return False

        def falso(request, timeout=None):
            codigo = respuestas[len(self.llamadas)] if len(self.llamadas) < len(respuestas) else None
            self.llamadas.append(request.full_url)
            if codigo is None:
                return Buena()
            raise urllib.error.HTTPError(
                request.full_url, codigo, "forzado", {}, None)

        toast_payroll.urllib.request.urlopen = falso

    def test_un_429_se_reintenta_y_la_segunda_pasa(self):
        self._falsear([429])
        request = urllib.request.Request("https://ejemplo/x")
        self.assertEqual(self.client._abrir_con_reintento(request), {"ok": True})
        self.assertEqual(len(self.llamadas), 2, "no reintento despues del 429")
        self.assertEqual(self.esperas, [2.0], "no esperó antes de reintentar")

    def test_un_500_tambien_se_reintenta(self):
        self._falsear([503])
        request = urllib.request.Request("https://ejemplo/x")
        self.assertEqual(self.client._abrir_con_reintento(request), {"ok": True})
        self.assertEqual(len(self.llamadas), 2)

    def test_un_404_no_se_reintenta(self):
        self._falsear([404])
        request = urllib.request.Request("https://ejemplo/x")
        with self.assertRaises(urllib.error.HTTPError):
            self.client._abrir_con_reintento(request)
        self.assertEqual(len(self.llamadas), 1, "reintento algo que no debia")

    def test_la_espera_se_duplica_y_al_final_explica(self):
        intentos = toast_payroll.ToastClient.RATE_LIMIT_INTENTOS
        self._falsear([429] * intentos)
        request = urllib.request.Request("https://ejemplo/x")
        with self.assertRaises(RuntimeError) as ctx:
            self.client._abrir_con_reintento(request)
        self.assertIn("429", str(ctx.exception))
        self.assertEqual(len(self.llamadas), intentos)
        cliente = toast_payroll.ToastClient
        esperado = [min(cliente.RATE_LIMIT_ESPERA_BASE * (2 ** i),
                        cliente.RATE_LIMIT_ESPERA_MAXIMA)
                    for i in range(intentos - 1)]
        self.assertEqual(self.esperas, esperado)
        self.assertLessEqual(max(self.esperas), cliente.RATE_LIMIT_ESPERA_MAXIMA,
                             "una espera se paso del tope: congela la app")

    def test_un_retry_after_absurdo_se_topa(self):
        """Un encabezado del servidor no debe decidir cuanto se congela la app."""
        llamadas = []

        class Buena:
            def read(self_inner):
                return b'{"ok": true}'
            def __enter__(self_inner):
                return self_inner
            def __exit__(self_inner, *a):
                return False

        def falso(request, timeout=None):
            llamadas.append(1)
            if len(llamadas) == 1:
                raise urllib.error.HTTPError(
                    request.full_url, 429, "espera una hora",
                    {"Retry-After": "3600"}, None)
            return Buena()

        toast_payroll.urllib.request.urlopen = falso
        request = urllib.request.Request("https://ejemplo/x")
        self.client._abrir_con_reintento(request)
        self.assertEqual(
            self.esperas, [toast_payroll.ToastClient.RATE_LIMIT_ESPERA_MAXIMA],
            "se respeto un Retry-After de una hora en vez de topalo")

    def test_una_caida_de_red_tambien_se_reintenta(self):
        """El reintento solo atrapaba HTTPError. Una conexion cortada a la mitad
        tiraba la corrida entera: paso de verdad el 2026-10-20 con
        "SSL: UNEXPECTED_EOF_WHILE_READING" a media verificacion."""
        import ssl
        llamadas = []

        class Buena:
            def read(self_inner):
                return b'{"ok": true}'
            def __enter__(self_inner):
                return self_inner
            def __exit__(self_inner, *a):
                return False

        def falso(request, timeout=None):
            llamadas.append(1)
            if len(llamadas) == 1:
                raise urllib.error.URLError(ssl.SSLError("UNEXPECTED_EOF_WHILE_READING"))
            if len(llamadas) == 2:
                raise ConnectionResetError("la conexion se corto")
            return Buena()

        toast_payroll.urllib.request.urlopen = falso
        request = urllib.request.Request("https://ejemplo/x")
        self.assertEqual(self.client._abrir_con_reintento(request), {"ok": True})
        self.assertEqual(len(llamadas), 3, "no reintento las fallas de red")

    def test_una_caida_de_red_persistente_explica_que_paso(self):
        intentos = toast_payroll.ToastClient.RATE_LIMIT_INTENTOS
        def siempre_cae(request, timeout=None):
            raise urllib.error.URLError("sin conexion")
        toast_payroll.urllib.request.urlopen = siempre_cae
        with self.assertRaises(RuntimeError) as ctx:
            self.client._abrir_con_reintento(urllib.request.Request("https://ejemplo/x"))
        self.assertIn("No se pudo hablar con Toast", str(ctx.exception))
        self.assertEqual(len(self.esperas), intentos - 1)



class TestNombresDefinidos(unittest.TestCase):
    """Ningun modulo puede usar un nombre que no existe.

    La app desplegada se cayo con `NameError: cuadra` en app.py: la variable se
    usaba y nunca se asignaba. Los tests no lo vieron porque app.py es codigo de
    Streamlit a nivel de modulo, que ningun test importa, y la rama solo se
    alcanza despues de generar una nomina completa. pyflakes lo encuentra en un
    segundo, asi que ahora es parte de la suite.
    """

    def test_ningun_nombre_sin_definir(self):
        try:
            from pyflakes import api, messages, reporter
        except ImportError:
            self.skipTest("falta pyflakes; esta en requirements.txt")

        encontrados = []

        class Colector(reporter.Reporter):
            def __init__(self):
                super().__init__(io.StringIO(), io.StringIO())

            def flake(self, mensaje):
                if isinstance(mensaje, (messages.UndefinedName,
                                        messages.UndefinedLocal,
                                        messages.UndefinedExport)):
                    encontrados.append(
                        f"{os.path.basename(mensaje.filename)}:"
                        f"{mensaje.lineno} {mensaje.message % mensaje.message_args}")

            def unexpectedError(self, filename, msg):
                encontrados.append(f"{filename}: {msg}")

        raiz = os.path.dirname(os.path.abspath(__file__))
        colector = Colector()
        for nombre in sorted(os.listdir(raiz)):
            if nombre.endswith(".py"):
                api.checkPath(os.path.join(raiz, nombre), colector)

        self.assertEqual(encontrados, [], "nombres sin definir:\n  " +
                         "\n  ".join(encontrados))


class TestMapeoNoSePierde(unittest.TestCase):
    """Una confirmacion humana no puede ser perecedera.

    build_maps genera filas solo para quien TRABAJO en la ventana consultada, y
    al reescribir el archivo se perdia la fila de quien no aparecia. Medido:
    'Lopez, Eduardo' estaba activo en Toast con 13.54 h un periodo y 0.00 el
    siguiente, y su gusto_employee_id confirmado se borro solo. Al volver a
    trabajar sus horas se habrian ido a retenidos.
    """

    def _archivo(self, filas):
        import tempfile
        fd, ruta = tempfile.mkstemp(suffix=".csv")
        os.close(fd)
        with open(ruta, "w", newline="", encoding="utf-8") as h:
            w = csv.DictWriter(h, fieldnames=build_maps.EMPLOYEE_MAP_COLUMNS,
                               extrasaction="ignore")
            w.writeheader()
            for f in filas:
                w.writerow({c: f.get(c, "") for c in build_maps.EMPLOYEE_MAP_COLUMNS})
        self.addCleanup(lambda: os.path.exists(ruta) and os.unlink(ruta))
        return ruta

    def test_conserva_a_quien_no_trabajo_en_la_ventana(self):
        ruta = self._archivo([
            {"toast_guid": "g-trabajo", "toast_name": "Ejemplo, Uno",
             "gusto_employee_id": "aaa111"},
            {"toast_guid": "g-descanso", "toast_name": "Ejemplo, Dos",
             "gusto_employee_id": "bbb222"},
        ])
        # esta corrida solo propone al que trabajo
        filas = [{"toast_guid": "g-trabajo", "toast_name": "Ejemplo, Uno",
                  "gusto_employee_id": "", "accion": "CONFIRMAR"}]

        build_maps.merge_existing(ruta, filas, "toast_guid",
                                  ("gusto_employee_id", "gusto_name", "accion"),
                                  compare_field="toast_name")

        por_guid = {f["toast_guid"]: f for f in filas}
        self.assertIn("g-descanso", por_guid,
                      "se perdio la fila de quien no trabajo: su mapeo confirmado "
                      "desaparece y sus horas se iran a retenidos")
        self.assertEqual(por_guid["g-descanso"]["gusto_employee_id"], "bbb222")
        self.assertEqual(por_guid["g-trabajo"]["gusto_employee_id"], "aaa111")

    def test_no_revive_una_fila_sin_resolver(self):
        """Lo que nunca se confirmo no se arrastra: seria ruido permanente."""
        ruta = self._archivo([
            {"toast_guid": "g-pendiente", "toast_name": "Ejemplo, Tres",
             "gusto_employee_id": "", "accion": "CONFIRMAR"},
        ])
        filas = []
        build_maps.merge_existing(ruta, filas, "toast_guid",
                                  ("gusto_employee_id", "gusto_name", "accion"))
        self.assertEqual(filas, [], "arrastro una fila que nadie confirmo")

    def test_conserva_un_excluir(self):
        """Marcar algo como no-persona tambien es una decision humana."""
        ruta = self._archivo([
            {"toast_guid": "g-kds", "toast_name": "Sistema, Cuenta",
             "gusto_employee_id": "", "accion": "EXCLUIR"},
        ])
        filas = []
        build_maps.merge_existing(ruta, filas, "toast_guid",
                                  ("gusto_employee_id", "gusto_name", "accion"))
        self.assertEqual(len(filas), 1, "se perdio un EXCLUIR y la cuenta de "
                                        "sistema volveria a aparecer cada corrida")


class TestDisparadorDelMeal(unittest.TestCase):
    """El primer meal se dispara arriba de 6 h, como la app de auditoria.

    La app de auditoria de meal breaks es la fuente de registro: su
    waiver_limit_hours es 6.0 y su logica es `worked_hours > 6.0`. No hay
    concepto de waiver: es solo el umbral. Una version anterior de este archivo
    agregaba banderas de waiver que la app no tiene y pedia confirmarlas en cada
    corrida; esos tests se fueron con ellas.
    """

    @staticmethod
    def _turno(horas, con_meal=False):
        """Un turno de N horas, con o sin un meal de 32 min bien tomado."""
        inicio = "2026-09-10T16:00:00.000+0000"
        fin_h = 16 + int(horas)
        fin_m = int(round((horas % 1) * 60))
        fin = f"2026-09-10T{fin_h:02d}:{fin_m:02d}:00.000+0000"
        breaks = []
        if con_meal:
            breaks = [{
                "guid": "b1",
                "breakType": {"guid": sorted(toast_payroll.MEAL_BREAK_TYPE_GUIDS)[0]},
                # arranca a las 4 h, antes del fin de la quinta hora
                "inDate": "2026-09-10T20:00:00.000+0000",
                "outDate": "2026-09-10T20:32:00.000+0000",
                "paid": False, "missed": False, "waived": False, "deleted": False,
            }]
        return {
            "guid": "t1", "businessDate": 20260910, "deleted": False,
            "inDate": inicio, "outDate": fin,
            "regularHours": horas, "overtimeHours": 0.0,
            "hourlyWage": 18.0, "breaks": breaks,
            "employeeReference": {"guid": "e1"},
            "jobReference": {"guid": "j1"},
        }

    def test_cinco_horas_y_media_sin_descanso_no_es_violacion(self):
        """La app usa 6 h: la banda de 5 a 6 h no genera violacion."""
        a = toast_payroll.audit_time_entry(self._turno(5.5))
        self.assertEqual(a["violations"], [])

    def test_exactamente_seis_horas_no_es_violacion(self):
        """'> 6.0': a las 6 justas todavia no se debe, igual que la app."""
        a = toast_payroll.audit_time_entry(self._turno(6.0))
        self.assertEqual(a["violations"], [])

    def test_exactamente_cinco_horas_no_es_violacion(self):
        """'More than five hours': a las 5 justas todavia no se debe."""
        a = toast_payroll.audit_time_entry(self._turno(5.0))
        self.assertEqual(a["violations"], [])

    def test_seis_horas_y_media_sin_descanso_sigue_siendo_violacion(self):
        a = toast_payroll.audit_time_entry(self._turno(6.5))
        self.assertIn("Missed Meal Break", a["violations"])

    def test_con_descanso_a_tiempo_no_hay_violacion(self):
        a = toast_payroll.audit_time_entry(self._turno(5.5, con_meal=True))
        self.assertEqual(a["violations"], [])


class TestNoHayPagoDoble(unittest.TestCase):
    """Nadie puede estar en el CSV y en la lista de 'pagale aparte' a la vez.

    Una fila con accion=CONFIRMAR que SI traia gusto_employee_id se pagaba en el
    CSV y al mismo tiempo generaba el aviso 'RETENIDO ... no entra al CSV, hay
    que pagarle aparte'. Quien obedece el aviso paga dos veces. Y pagar con un
    mapeo que nadie confirmo es como el salario de una persona acaba en la
    cuenta de otra. Se llega a ese estado por la rama de merge_existing que
    detecta un cambio de nombre en Toast: pone CONFIRMAR sin limpiar el id.
    """

    def _mapa(self, filas):
        import tempfile
        fd, ruta = tempfile.mkstemp(suffix=".csv")
        os.close(fd)
        columnas = ["toast_guid", "toast_location", "toast_name",
                    "gusto_employee_id", "accion"]
        with open(ruta, "w", newline="", encoding="utf-8") as h:
            w = csv.DictWriter(h, fieldnames=columnas, extrasaction="ignore")
            w.writeheader()
            for f in filas:
                w.writerow({c: f.get(c, "") for c in columnas})
        self.addCleanup(lambda: os.path.exists(ruta) and os.unlink(ruta))
        return ruta

    def test_un_id_sin_confirmar_no_se_usa_para_pagar(self):
        ruta = self._mapa([{
            "toast_guid": "g1", "toast_location": "X", "toast_name": "Ejemplo, Uno",
            "gusto_employee_id": "aaa111", "accion": "CONFIRMAR"}])
        mapa = toast_payroll.load_employee_map(ruta)
        self.assertEqual(
            mapa["g1"]["gusto_employee_id"], "",
            "un id con accion=CONFIRMAR no debe servir para pagar: sus horas "
            "entrarian al CSV y a la vez se avisaria que se le pague aparte")
        self.assertTrue(mapa["g1"]["pendiente"])
        self.assertEqual(mapa["g1"]["gusto_employee_id_propuesto"], "aaa111",
                         "la propuesta se conserva para poder ofrecerla con un boton")

    def test_un_id_confirmado_si_se_usa(self):
        ruta = self._mapa([{
            "toast_guid": "g2", "toast_location": "X", "toast_name": "Ejemplo, Dos",
            "gusto_employee_id": "bbb222", "accion": ""}])
        mapa = toast_payroll.load_employee_map(ruta)
        self.assertEqual(mapa["g2"]["gusto_employee_id"], "bbb222")
        self.assertFalse(mapa["g2"]["pendiente"])

    def test_un_excluir_no_queda_pendiente(self):
        ruta = self._mapa([{
            "toast_guid": "g3", "toast_location": "X", "toast_name": "Sistema, Cuenta",
            "gusto_employee_id": "", "accion": "EXCLUIR"}])
        mapa = toast_payroll.load_employee_map(ruta)
        self.assertTrue(mapa["g3"]["excluir"])
        self.assertFalse(mapa["g3"]["pendiente"],
                         "un EXCLUIR no debe salir como pendiente de confirmar")


class TestNadaSePierdeEnRetenidos(unittest.TestCase):
    """Toda hora que entra sale al CSV o a retenidos, en los casos raros.

    Estos escenarios no existen en la cuenta como esta hoy, asi que los tests
    contra datos reales pasaban aunque los bugs estuvieran ahi. Se fabrican con
    toast_falso para no esperar a que le paguen mal a alguien.
    """

    INICIO, FIN = date(2026, 9, 5), date(2026, 9, 18)
    DIAS = ("2026-09-07", "2026-09-08", "2026-09-09")

    def setUp(self):
        self.esc = toast_falso.Escenario()
        self.addCleanup(self.esc.limpiar)

    def test_una_persona_con_un_registro_por_sucursal_sin_mapear(self):
        """hours_by_name ASIGNABA en vez de acumular, y pending era un set de
        nombres: los dos registros se llaman igual, colapsaban en uno y las
        horas del segundo desaparecian del cuadre."""
        tel = "6195550101"
        a = self.esc.empleado("Gaslamp Quarter", "Ejemplo", "Dalia", telefono=tel)
        b = self.esc.empleado("National City", "Ejemplo", "Dalia", telefono=tel)
        for dia in self.DIAS:
            self.esc.turno(a, dia, horas=8.0, con_meal=True)
            self.esc.turno(b, dia, horas=6.0, con_meal=True)

        r = self.esc.correr(self.INICIO, self.FIN)

        self.assertAlmostEqual(toast_falso.hueco_de_horas(r), 0.0, places=2,
                               msg="horas sin explicar: alguna se perdio entre "
                                   "los dos registros de la misma persona")
        self.assertAlmostEqual(r["hours_held"], 42.0, places=2,
                               msg="deben quedar retenidas las 24 h de una "
                                   "sucursal MAS las 18 h de la otra")
        sucursales = {h.get("sucursal") for h in r["held_back"] if h["horas"] > 0}
        self.assertEqual(sucursales, {"Gaslamp Quarter", "National City"},
                         "cada registro necesita su propia fila: si no, quien "
                         "paga a mano no sabe que son dos sucursales")

    def test_un_id_de_gusto_que_no_esta_en_ningun_template(self):
        """Caso que NINGUN aviso nombraba: la persona tiene id de Gusto, el id
        no aparece en el template, sus horas se borraban del CSV y no entraban
        a retenidos. Pasa con quien sigue fichando despues de salir de Gusto."""
        quien = self.esc.empleado("Gaslamp Quarter", "Ejemplo", "Eliseo",
                                  gusto_id="zzz999", en_template=False)
        for dia in self.DIAS:
            self.esc.turno(quien, dia, horas=8.0, con_meal=True)

        r = self.esc.correr(self.INICIO, self.FIN)

        self.assertAlmostEqual(toast_falso.hueco_de_horas(r), 0.0, places=2,
                               msg="24 h desaparecieron: ni al CSV ni a retenidos")
        self.assertAlmostEqual(r["hours_held"], 24.0, places=2)
        self.assertTrue(
            any("zzz999" in a["texto"] for a in r["avisos"]),
            "ningun aviso nombra el id que no esta en el template, asi que "
            "quien opera no tiene con que actuar")

    def test_el_cuadre_mide_lo_que_se_escribe_no_las_celdas(self):
        """Un job cuyo title no esta entre los de la persona: la celda
        sobrevivia, el CSV salia sin esas horas y el cuadre decia +0.00."""
        quien = self.esc.empleado("Gaslamp Quarter", "Ejemplo", "Fabiola",
                                  gusto_id="yyy888")
        # un segundo renglon para que tenga DOS titles y caiga en el `else`
        self.esc.renglon_de_gusto("Gaslamp Quarter", "Ejemplo", "Fabiola",
                                  "yyy888", job="Lead")
        for dia in self.DIAS:
            self.esc.turno(quien, dia, horas=8.0, job="Dishwasher", con_meal=True)

        r = self.esc.correr(self.INICIO, self.FIN)

        self.assertAlmostEqual(
            r["hours_placed"], toast_falso.horas_escritas(r), places=2,
            msg="'al CSV' no coincide con lo escrito: el cuadre esta mintiendo")
        self.assertAlmostEqual(toast_falso.hueco_de_horas(r), 0.0, places=2)
        self.assertAlmostEqual(r["hours_held"], 24.0, places=2,
                               msg="esas 24 h no salen en el CSV, asi que tienen "
                                   "que quedar en retenidos para pagarlas aparte")
    def test_un_job_sin_mapear_no_tira_las_horas(self):
        """Dos `continue` tiraban las horas al vacio: ni al CSV, ni a retenidos,
        ni al hueco. El aviso de la persona salia diciendo 'con 0.00 h', que es
        lo que delataba el bug. Pasa con un job nuevo en Toast sin mapear."""
        self.esc.mapear_jobs = False
        quien = self.esc.empleado("Gaslamp Quarter", "Ejemplo", "Gael",
                                  gusto_id="xxx777")
        self.esc.renglon_de_gusto("Gaslamp Quarter", "Ejemplo", "Gael",
                                  "xxx777", job="Lead")
        for dia in self.DIAS:
            self.esc.turno(quien, dia, horas=8.0, job="PuestoNuevo", con_meal=True)

        r = self.esc.correr(self.INICIO, self.FIN)

        self.assertAlmostEqual(toast_falso.hueco_de_horas(r), 0.0, places=2,
                               msg="24 h tiradas al vacio por un job sin mapear")
        self.assertAlmostEqual(r["hours_held"], 24.0, places=2)
        avisos_con_horas = [a for a in r["avisos"]
                            if "RETENIDO" in a["texto"] and "0.00 h" in a["texto"]]
        self.assertEqual(avisos_con_horas, [],
                         "un aviso de RETENIDO con 0.00 h significa que las "
                         "horas se perdieron antes de contarse")
    def test_el_overtime_no_se_mete_en_las_celdas_que_sobreviven(self):
        """El split de regular/OT/DT es de la jornada COMPLETA. Prorratearlo
        sobre las celdas que sobreviven metia el OT de las horas descartadas en
        las que si quedaron: una jornada de 12 h salia como 12 h en una celda de
        4, y las 8 descartadas ademas iban a retenidos. Daba cuadre NEGATIVO,
        que es como se delato en el periodo 2026-07-25/08-07 (-3.92 h)."""
        # Taquero SI esta mapeado; PuestoNuevo no.
        self.esc.jobs_sin_mapear = {"PuestoNuevo"}
        quien = self.esc.empleado("Gaslamp Quarter", "Ejemplo", "Hilda",
                                  gusto_id="www666")
        # dos puestos en Gusto, para que el job sin mapear no se pueda adivinar
        self.esc.renglon_de_gusto("Gaslamp Quarter", "Ejemplo", "Hilda",
                                  "www666", job="Lead")
        # el mismo dia: 4 h en un puesto que SI tiene renglon y 8 en uno que no
        self.esc.turno(quien, "2026-09-07", horas=4.0, job="Taquero")
        self.esc.turno(quien, "2026-09-07", horas=8.0, job="PuestoNuevo")

        r = self.esc.correr(self.INICIO, self.FIN)

        self.assertAlmostEqual(
            toast_falso.hueco_de_horas(r), 0.0, places=2,
            msg="cuadre negativo: el OT de las horas descartadas se metio en "
                "las celdas que sobrevivieron")
        self.assertLessEqual(
            toast_falso.horas_escritas(r), 4.01,
            msg="el CSV no puede llevar mas de las 4 h cuyo puesto si tiene "
                "renglon; lo demas va a retenidos")
        self.assertAlmostEqual(r["hours_held"], 8.0, places=2)




class TestAlineadoConLaAppDeAuditoria(unittest.TestCase):
    """Los meal violations salen de la app de auditoria, no de mi lectura.

    Esa app es la fuente con la que se capturan hoy. Fijar sus parametros aqui
    evita que alguien los "mejore" y la nomina deje de reproducir lo que ya se
    revisa. Verificado contra el periodo que Tany hizo a mano
    (2026-07-25/08-07): National City da 17 de 17, 11 de 11 personas iguales.
    """

    def test_los_parametros_son_los_de_la_app(self):
        self.assertEqual(toast_payroll.FIRST_MEAL_WAIVER_CEILING_HOURS, 6.0,
                         "waiver_limit_hours de la app")
        self.assertEqual(toast_payroll.MEAL_DEADLINE_HOURS, 5.0,
                         "meal_deadline_hours de la app")
        self.assertEqual(toast_payroll.SECOND_MEAL_TRIGGER_HOURS, 12.0,
                         "second_meal_limit_hours de la app; la ley pide 10, "
                         "asi que este valor es mas permisivo a proposito")
        self.assertEqual(toast_payroll.SECOND_MEAL_DEADLINE_HOURS, 10.0,
                         "second_meal_deadline_hours de la app")
        self.assertEqual(toast_payroll.MIN_BREAK_MINUTES, 30)

    def test_dispara_con_las_horas_pagables(self):
        """La app usa worked_hours = regularHours + overtimeHours."""
        turno = {
            "guid": "t", "businessDate": 20260910, "deleted": False,
            "inDate": "2026-09-10T16:00:00.000+0000",
            "outDate": "2026-09-10T23:00:00.000+0000",   # 7 h en el local
            "regularHours": 6.5, "overtimeHours": 0.0,   # 6.5 h pagables
            "hourlyWage": 18.0, "breaks": [],
            "employeeReference": {"guid": "e"}, "jobReference": {"guid": "j"},
        }
        a = toast_payroll.audit_time_entry(turno)
        self.assertAlmostEqual(a["payable_hours"], 6.5, places=2)
        self.assertIn("Missed Meal Break", a["violations"],
                      "6.5 h pagables pasan el techo de 6 h: debe violacion")

    def test_el_segundo_meal_no_dispara_entre_10_y_12(self):
        """Con el parametro de la app, la banda de 10 a 12 h no pide 2do meal."""
        turno = {
            "guid": "t", "businessDate": 20260910, "deleted": False,
            "inDate": "2026-09-10T14:00:00.000+0000",
            "outDate": "2026-09-10T01:00:00.000+0000",
            "regularHours": 11.0, "overtimeHours": 0.0,
            "hourlyWage": 18.0,
            "breaks": [{
                "guid": "b", "breakType": {"guid": sorted(toast_payroll.MEAL_BREAK_TYPE_GUIDS)[0]},
                "inDate": "2026-09-10T18:00:00.000+0000",
                "outDate": "2026-09-10T18:32:00.000+0000",
                "paid": False, "missed": False, "waived": False, "deleted": False,
            }],
            "employeeReference": {"guid": "e"}, "jobReference": {"guid": "j"},
        }
        a = toast_payroll.audit_time_entry(turno)
        self.assertAlmostEqual(a["payable_hours"], 11.0, places=2)
        self.assertNotIn("Missing 2nd Meal", a["violations"],
                         "11 h no pasan el umbral de 12 h de la app. La ley "
                         "pide el 2do meal arriba de 10 h: si eso se corrige, "
                         "hay que cambiar SECOND_MEAL_TRIGGER_HOURS a proposito")


class TestEfectivoPorPuesto(unittest.TestCase):
    """El efectivo solo va a ciertos puestos, y si cae en otro se dice.

    En Plaza Bonita lo reciben Server, Bartender y Host; en Gaslamp ninguno.
    Medido en dos periodos: en Bonita solo Server trae efectivo ($79.55 y
    $73.00) y en Gaslamp nadie. La herramienta NO mueve el dinero — el reporte
    manda — pero avisa cuando el efectivo aparece donde no se espera.
    """

    INICIO, FIN = date(2026, 9, 5), date(2026, 9, 18)
    def setUp(self):
        self.esc = toast_falso.Escenario()
        self.addCleanup(self.esc.limpiar)

    @staticmethod
    def _reporte(nombre, job, efectivo, cheque=0.0):
        """Como lo deja load_tip_report, que normaliza el CSV del reporte."""
        return [{"employee": nombre, "job": job, "hours": 24.0,
                 "cash": efectivo, "non_cash": cheque, "gratuity": 0.0}]

    def test_la_regla_esta_escrita_y_es_la_del_negocio(self):
        regla = gusto_export.PUESTOS_CON_EFECTIVO
        self.assertEqual(regla["tacos-franc-bonita-llc"],
                         {"server", "bartender", "host"})
        self.assertEqual(regla["tacos-franc-gaslamp-llc"], set())

    def test_avisa_si_el_efectivo_cae_en_un_taquero_de_bonita(self):
        quien = self.esc.empleado("National City", "Ejemplo", "Ignacio",
                                  gusto_id="t00001")
        for dia in ("2026-09-07", "2026-09-08", "2026-09-09"):
            self.esc.turno(quien, dia, horas=8.0, job="Taquero", con_meal=True)

        r = self.esc.correr(self.INICIO, self.FIN, tip_reports={
            "tacos-franc-bonita-llc": self._reporte("Ignacio Ejemplo", "Taquero", 45.00)})

        avisos = [a for a in r["avisos"]
                  if a["tipo"] == "efectivo_en_puesto_inesperado"]
        self.assertEqual(len(avisos), 1,
                         "un Taquero de Bonita con efectivo tiene que avisar")
        self.assertIn("45.00", avisos[0]["texto"])
        self.assertIn("Taquero", avisos[0]["texto"])

        # y el monto NO se mueve: va al CSV tal como lo trae el reporte
        filas = r["by_company"]["tacos-franc-bonita-llc"]
        efectivo = sum(float(f["cash_tips"] or 0) for f in filas)
        self.assertAlmostEqual(efectivo, 45.00, places=2,
                               msg="la herramienta no debe reasignar el efectivo: "
                                   "el reporte es la fuente")

    def test_no_avisa_si_el_efectivo_cae_en_un_server_de_bonita(self):
        quien = self.esc.empleado("National City", "Ejemplo", "Julia",
                                  gusto_id="t00002", job="Server")
        for dia in ("2026-09-07", "2026-09-08"):
            self.esc.turno(quien, dia, horas=8.0, job="Server", con_meal=True)

        r = self.esc.correr(self.INICIO, self.FIN, tip_reports={
            "tacos-franc-bonita-llc": self._reporte("Julia Ejemplo", "Server", 73.00)})

        self.assertEqual(
            [a for a in r["avisos"] if a["tipo"] == "efectivo_en_puesto_inesperado"],
            [], "Server SI recibe efectivo en Bonita: no debe avisar")


class TestElDiagnosticoMideElPeriodo(unittest.TestCase):
    """Los numeros por sucursal tienen que ser del PERIODO, no de 21 dias.

    `audited` arranca 7 dias antes del periodo para poder ver rachas de 7 dias
    consecutivos que empiezan afuera. El diagnostico reportaba todo eso como si
    fuera del periodo: en una corrida real del 2026-10-03/16 las horas por
    sucursal sumaban 5,805 mientras el cierre decia 3,789, y la tabla mostraba
    47 "Missed Meal Break" mientras el aviso de la MISMA corrida decia 32. Dos
    numeros distintos para lo mismo, en la misma pantalla, es como se pierde la
    confianza en una herramienta que nadie puede verificar a mano.
    """

    INICIO, FIN = date(2026, 9, 5), date(2026, 9, 18)

    def setUp(self):
        self.esc = toast_falso.Escenario()
        self.addCleanup(self.esc.limpiar)

    def test_las_horas_por_sucursal_suman_lo_que_entro(self):
        quien = self.esc.empleado("Gaslamp Quarter", "Ejemplo", "Karla",
                                  gusto_id="d00001")
        # un turno DENTRO del periodo y otro en los 7 dias de contexto previo
        self.esc.turno(quien, "2026-09-07", horas=8.0, con_meal=True)
        self.esc.turno(quien, "2026-09-02", horas=8.0, con_meal=True)

        r = self.esc.correr(self.INICIO, self.FIN)

        suma = sum(s["horas_pagables"]
                   for s in r["diagnostico"]["por_sucursal"].values())
        self.assertAlmostEqual(
            suma, r["hours_reported"], places=2,
            msg="las horas por sucursal no suman lo que el cierre dice que "
                "entro: el diagnostico esta midiendo otra ventana")
        self.assertAlmostEqual(suma, 8.0, places=2,
                               msg="el turno del 2 de septiembre es contexto "
                                   "previo; no debe contarse en el periodo")

    def test_los_turnos_contados_son_los_del_periodo(self):
        quien = self.esc.empleado("Gaslamp Quarter", "Ejemplo", "Lucia",
                                  gusto_id="d00002")
        for dia in ("2026-09-07", "2026-09-08"):
            self.esc.turno(quien, dia, horas=8.0, con_meal=True)
        for dia in ("2026-09-01", "2026-09-02", "2026-09-03"):
            self.esc.turno(quien, dia, horas=8.0, con_meal=True)

        r = self.esc.correr(self.INICIO, self.FIN)
        gaslamp = r["diagnostico"]["por_sucursal"]["Gaslamp Quarter"]
        self.assertEqual(gaslamp["turnos"], 2,
                         "solo 2 de los 5 turnos caen en el periodo")


class TestLaPantallaDeResultados(unittest.TestCase):
    """El veredicto y las descargas, que es lo que se usa casi siempre.

    La pantalla se reorganizo porque la descarga estaba en el paso 6 de 7 y el
    veredicto hasta el final. Este test corre app.py de verdad con un resultado
    real y fija lo que no se puede romper: que haya veredicto, que diga lo
    correcto en cada caso, y que un resultado bloqueado NO ofrezca descargas —
    bajar un archivo roto por accidente es justo lo que el candado evita.
    """

    INICIO, FIN = date(2026, 9, 5), date(2026, 9, 18)

    @classmethod
    def setUpClass(cls):
        if not (os.environ.get("TOAST_CLIENT_ID") and os.environ.get("TOAST_CLIENT_SECRET")):
            raise unittest.SkipTest("sin credenciales de Toast")
        import importlib.util
        if importlib.util.find_spec("streamlit.testing.v1") is None:
            raise unittest.SkipTest("esta version de streamlit no trae AppTest")
        rep = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reference")
        nc = os.path.join(rep, "tips_NC_2026-09-05_2026-09-18.csv")
        gl = os.path.join(rep, "tips_GL_2026-09-05_2026-09-18.csv")
        if not (os.path.exists(nc) and os.path.exists(gl)):
            raise unittest.SkipTest("faltan los reportes de tips de referencia")
        cls.base = gusto_export.build(
            toast_payroll.ToastClient.from_env(), cls.INICIO, cls.FIN, {
                "tacos-franc-bonita-llc": gusto_export.load_tip_report(nc),
                "tacos-franc-gaslamp-llc": gusto_export.load_tip_report(gl)})

    def _pintar(self, resultado):
        from streamlit.testing.v1 import AppTest
        anterior = os.environ.get("APP_PASSWORD")
        os.environ["APP_PASSWORD"] = "prueba"
        self.addCleanup(lambda: os.environ.__setitem__("APP_PASSWORD", anterior)
                        if anterior is not None else os.environ.pop("APP_PASSWORD", None))
        at = AppTest.from_file("app.py", default_timeout=180)
        at.session_state["acceso_ok"] = True
        at.run()
        sel = at.selectbox[0]
        sel.set_value(next(i for i, o in enumerate(sel.options) if "05 Sep" in str(o)))
        at.run()
        at.session_state["resultado"] = resultado
        at.session_state["huella"] = (self.INICIO, self.FIN, ())
        at.run()
        self.assertFalse(at.exception, f"la pantalla tronó: {at.exception}")
        veredicto = next((e for e in at.main if type(e).__name__ in ("Error", "Warning", "Success")
                          and "###" in (getattr(e, "value", "") or "")), None)
        botones = [b for b in at.get("download_button") if "⬇" in (b.label or "")]
        return veredicto, botones

    def test_bloqueado_no_ofrece_descargas(self):
        import copy
        r = copy.deepcopy(self.base)
        r["problems"] = ["Cierre de HORAS: faltan 12.00 h sin explicar"]
        r["hours_held"] -= 12.0
        veredicto, botones = self._pintar(r)
        self.assertEqual(type(veredicto).__name__, "Error")
        self.assertIn("No subas", veredicto.value)
        self.assertEqual(botones, [], "un resultado bloqueado no debe dejar bajar nada")

    def test_con_pendientes_avisa_y_ofrece_las_dos_descargas(self):
        veredicto, botones = self._pintar(self.base)
        self.assertEqual(type(veredicto).__name__, "Warning")
        self.assertIn("revisa", veredicto.value)
        self.assertEqual(len(botones), 2, "una descarga por empresa")

    def _tablas(self, resultado):
        """Las tablas de la vista previa y de totales, tal como se pintaron."""
        from streamlit.testing.v1 import AppTest
        anterior = os.environ.get("APP_PASSWORD")
        os.environ["APP_PASSWORD"] = "prueba"
        self.addCleanup(lambda: os.environ.__setitem__("APP_PASSWORD", anterior)
                        if anterior is not None else os.environ.pop("APP_PASSWORD", None))
        at = AppTest.from_file("app.py", default_timeout=180)
        at.session_state["acceso_ok"] = True
        at.run()
        sel = at.selectbox[0]
        sel.set_value(next(i for i, o in enumerate(sel.options) if "05 Sep" in str(o)))
        at.run()
        at.session_state["resultado"] = resultado
        at.session_state["huella"] = (self.INICIO, self.FIN, ())
        at.run()
        self.assertFalse(at.exception, f"la pantalla tronó: {at.exception}")
        marcos = [(t.value.data if hasattr(t.value, "data") else t.value) for t in at.dataframe]
        previas = [m for m in marcos if "Empleado" in m.columns]
        totales = [m for m in marcos if "" in m.columns and "Reg" in m.columns]
        return previas, totales

    def test_la_vista_previa_muestra_cada_renglon_una_sola_vez(self):
        """Ningun renglon del CSV se pierde ni se duplica al separar efectivo."""
        previas, _ = self._tablas(self.base)
        recibe, resto = previas[0], previas[1]
        bonita = self.base["by_company"]["tacos-franc-bonita-llc"]
        cols = ("regular_hours", "overtime_hours", "double_overtime_hours",
                "missed_break_hours", "paycheck_tips", "cash_tips",
                "custom_earning_distributed_service_charges")
        con_dato = [f for f in bonita if any((f[c] or "") for c in cols)]
        self.assertEqual(len(recibe) + len(resto), len(con_dato),
                         "la vista previa no muestra exactamente los renglones del CSV")

        efectivo = gusto_export.PUESTOS_CON_EFECTIVO["tacos-franc-bonita-llc"]
        for p in recibe["Puesto"]:
            self.assertIn(p.replace(" (Primary)", "").lower(), efectivo,
                          f"'{p}' no recibe efectivo y quedo en esa tabla")
        for p in resto["Puesto"]:
            self.assertNotIn(p.replace(" (Primary)", "").lower(), efectivo,
                             f"'{p}' recibe efectivo y quedo en 'solo cheque'")

    def test_los_totales_de_pantalla_son_los_del_archivo(self):
        _, totales = self._tablas(self.base)
        bonita = self.base["by_company"]["tacos-franc-bonita-llc"]
        en_pantalla = totales[0].iloc[0]
        pares = (("Reg", "regular_hours"), ("OT", "overtime_hours"),
                 ("Tips cheque", "paycheck_tips"), ("Tips efectivo", "cash_tips"))
        for titulo, col in pares:
            archivo = sum(float(f[col]) if (f[col] or "").strip() else 0.0 for f in bonita)
            self.assertAlmostEqual(float(en_pantalla[titulo]), archivo, places=2,
                                   msg=f"el total de '{titulo}' en pantalla no es el del CSV")

    def test_el_efectivo_en_un_puesto_que_no_lo_recibe_se_ve(self):
        """Si la columna se quitara de 'solo cheque', ese dinero seria invisible."""
        import copy
        r = copy.deepcopy(self.base)
        victima = next(f for f in r["by_company"]["tacos-franc-bonita-llc"]
                       if f["title"].startswith("Taquero") and (f["regular_hours"] or ""))
        victima["cash_tips"] = "45.00"
        previas, _ = self._tablas(r)
        resto = previas[1]
        self.assertIn("Tips efectivo", resto.columns,
                      "la tabla 'solo cheque' perdio la columna de efectivo")
        fila = resto[resto["Empleado"].str.startswith(victima["last_name"])]
        self.assertEqual(len(fila), 1)
        self.assertAlmostEqual(float(fila.iloc[0]["Tips efectivo"]), 45.0, places=2)


class TestMarcaDeEfectivo(unittest.TestCase):
    """El rojo va SOLO en efectivo distinto de cero. Se prueba la funcion real,
    sacada de app.py sin correr la app."""

    def test_solo_marca_lo_que_trae_dinero(self):
        import ast
        import pandas as pd
        src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.py"),
                   encoding="utf-8").read()
        arbol = ast.parse(src)
        codigo = "\n".join(ast.get_source_segment(src, n) for n in arbol.body
                           if isinstance(n, ast.FunctionDef)
                           and n.name in ("_numero", "_marcar_efectivo"))
        ns = {}
        exec(codigo, ns)
        df = pd.DataFrame({"Tips efectivo": [45.0, 0.0, None]})
        estilo = df.style.map(ns["_marcar_efectivo"], subset=["Tips efectivo"])
        estilo._compute()
        marcadas = {fila for (fila, _), css in estilo.ctx.items() if css}
        self.assertEqual(marcadas, {0}, "solo el renglon con $45 debe ir en rojo")



class TestReporteDeLaSucursalCorrecta(unittest.TestCase):
    """Cada reporte de tips tiene que ser de la sucursal donde se usa.

    Toast baja el reporte con el MISMO nombre de archivo para las dos
    sucursales —el navegador solo le agrega "(1)" al segundo—, asi que no se
    distinguen por el nombre. En una corrida real del 2026-10-03/16 se subieron
    cruzados. La herramienta lo detuvo ($26,696.21 sin explicar) pero levanto 157
    avisos sin decir nunca la causa. Cada reporte trae la gente de su sucursal:
    100% de coincidencia con la propia contra 9-31% con la otra.
    """

    PLANTILLAS = {
        "National City": {frozenset({"ana", "ejemplo"}), frozenset({"beto", "ejemplo"}),
                          frozenset({"ciro", "ejemplo"}), frozenset({"dora", "ejemplo"})},
        "Gaslamp Quarter": {frozenset({"elsa", "prueba"}), frozenset({"fito", "prueba"}),
                            frozenset({"gema", "prueba"}), frozenset({"dora", "ejemplo"})},
    }

    @staticmethod
    def _filas(*nombres):
        return [{"employee": n} for n in nombres]

    def test_reconoce_de_que_sucursal_es_cada_reporte(self):
        nc = self._filas("Ana Ejemplo", "Beto Ejemplo", "Ciro Ejemplo")
        gl = self._filas("Elsa Prueba", "Fito Prueba", "Gema Prueba")
        self.assertEqual(gusto_export.sucursal_mas_probable(
            gusto_export.coincidencia_por_sucursal(nc, self.PLANTILLAS)), "National City")
        self.assertEqual(gusto_export.sucursal_mas_probable(
            gusto_export.coincidencia_por_sucursal(gl, self.PLANTILLAS)), "Gaslamp Quarter")

    def test_quien_trabaja_en_las_dos_no_confunde(self):
        """Dora esta en las dos plantillas, como Alexa o Benjamin en la realidad."""
        nc = self._filas("Ana Ejemplo", "Beto Ejemplo", "Dora Ejemplo")
        coin = gusto_export.coincidencia_por_sucursal(nc, self.PLANTILLAS)
        self.assertEqual(gusto_export.sucursal_mas_probable(coin), "National City")

    def test_no_adivina_si_no_hay_ganador_claro(self):
        mezcla = self._filas("Ana Ejemplo", "Elsa Prueba")
        coin = gusto_export.coincidencia_por_sucursal(mezcla, self.PLANTILLAS)
        self.assertEqual(gusto_export.sucursal_mas_probable(coin), "",
                         "con 50/50 no debe elegir: mejor pedir que lo revisen")

    def test_ignora_las_cuentas_de_sistema(self):
        nc = self._filas("Ana Ejemplo", "Beto Ejemplo", "Online Ordering")
        coin = gusto_export.coincidencia_por_sucursal(nc, self.PLANTILLAS)
        self.assertAlmostEqual(coin["National City"], 1.0,
                               msg="Online Ordering no es de ninguna plantilla y no "
                                   "debe bajar la coincidencia")

    def test_detecta_un_reporte_subido_en_la_casilla_equivocada(self):
        nc = self._filas("Ana Ejemplo", "Beto Ejemplo", "Ciro Ejemplo")
        coin = gusto_export.coincidencia_por_sucursal(nc, self.PLANTILLAS)
        self.assertEqual(gusto_export.sucursal_real_del_reporte("Gaslamp Quarter", coin),
                         "National City")
        self.assertEqual(gusto_export.sucursal_real_del_reporte("National City", coin), "")


class TestReportesCruzadosEnElMotor(unittest.TestCase):
    """Con el motor completo: cruzados, una linea; no 157 avisos."""

    INICIO, FIN = date(2026, 9, 5), date(2026, 9, 18)

    def setUp(self):
        self.esc = toast_falso.Escenario()
        self.addCleanup(self.esc.limpiar)
        self.nc = [self.esc.empleado("National City", "Ejemplo", n, gusto_id=f"n{i:05d}")
                   for i, n in enumerate(("Ana", "Beto", "Ciro", "Dora"))]
        self.gl = [self.esc.empleado("Gaslamp Quarter", "Prueba", n, gusto_id=f"g{i:05d}")
                   for i, n in enumerate(("Elsa", "Fito", "Gema", "Hugo"))]
        for g in self.nc + self.gl:
            self.esc.turno(g, "2026-09-07", horas=8.0, con_meal=True)

    @staticmethod
    def _reporte(apellido, nombres):
        return [{"employee": f"{n} {apellido}", "job": "Taquero", "hours": 8.0,
                 "cash": 0.0, "non_cash": 100.0, "gratuity": 0.0} for n in nombres]

    def test_bien_puestos_no_bloquea(self):
        r = self.esc.correr(self.INICIO, self.FIN, tip_reports={
            "tacos-franc-bonita-llc": self._reporte("Ejemplo", ("Ana", "Beto", "Ciro", "Dora")),
            "tacos-franc-gaslamp-llc": self._reporte("Prueba", ("Elsa", "Fito", "Gema", "Hugo"))})
        self.assertEqual([a for a in r["avisos"] if a["tipo"] == "reporte_de_otra_sucursal"], [])

    def test_cruzados_dice_al_reves_en_una_linea(self):
        r = self.esc.correr(self.INICIO, self.FIN, tip_reports={
            "tacos-franc-bonita-llc": self._reporte("Prueba", ("Elsa", "Fito", "Gema", "Hugo")),
            "tacos-franc-gaslamp-llc": self._reporte("Ejemplo", ("Ana", "Beto", "Ciro", "Dora"))})
        cruce = [a for a in r["avisos"] if a["tipo"] == "reporte_de_otra_sucursal"]
        self.assertEqual(len(cruce), 1, "dos reportes cruzados son UN mensaje, no dos")
        self.assertIn("AL REVES", cruce[0]["texto"])
        self.assertEqual(cruce[0]["nivel"], "bloquea")
        # y sin la avalancha de avisos persona por persona
        ruido = [a for a in r["avisos"] if a["tipo"] == "tips_sin_ubicar"]
        self.assertEqual(ruido, [], "los reportes equivocados no deben procesarse")


if __name__ == "__main__":
    unittest.main(verbosity=2)
