"""Cliente de Toast Labor API y motor de calculo de nomina para Gusto.

Extrae horas, meal violations y tips de las dos locations de Tacos El Franc y
los agrega por empleado para un periodo de nomina. La union con Gusto se hace
por email, que en esta cuenta esta poblado al 100% en los empleados activos.

Solo usa la libreria estandar: no necesita requests ni pandas.
"""

from __future__ import annotations

import csv
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import date, datetime, time, timedelta
# Se importa solo sleep: 'import time' quedaria pisado por el time de
# datetime que se usa abajo para time.min, y time.sleep lanzaria AttributeError.
from time import sleep
from zoneinfo import ZoneInfo

TOAST_API_HOST = "https://ws-api.toasttab.com"
TOAST_TIMEZONE = "America/Los_Angeles"

# Las locations NO se hardcodean: se descubren con /partners/v1/restaurants, que
# lista los restaurantes donde esta habilitada esta integracion. Si falta una
# location, es porque nadie prendio la integracion para ese restaurante en Toast,
# y hay que prenderla alla: aca aparece sola.
#
# Este diccionario solo mapea cada restaurante a su empresa de Gusto. La llave es
# el GUID porque es estable; el nombre en Toast puede cambiar en cualquier momento.
GUSTO_COMPANIES = {
    "2bef5cef-4e30-4d2a-860f-d43406a5ff29": {
        "short": "National City",
        "company": "tacos-franc-bonita-llc",
        "template": "gusto_template_bonita.csv",
    },
    "ac4c6cbd-8723-45cb-bf16-6d31369c5b50": {
        "short": "Gaslamp Quarter",
        "company": "tacos-franc-gaslamp-llc",
        "template": "gusto_template_gaslamp.csv",
    },
}

# Break types descubiertos inspeccionando la cuenta el 2026-09-30.
# Los dos unpaid con mediana ~31 min son meal breaks; el paid de ~11 min es rest break.
MEAL_BREAK_TYPE_GUIDS = {
    "8d8bb4dc-33f2-45fa-868b-87ef685959f5",
    "c6f12e86-cd48-4aa2-8f1a-ad315ee5757c",
}
PAID_REST_BREAK_TYPE_GUIDS = {"6cc1e49a-1fe3-49de-8714-559d8cd46b8b"}

# Toast autogenera emails tipo <guid>@example.com cuando el empleado se dio de
# alta sin correo. No sirven para unir con Gusto: hay que mapearlos a mano.
PLACEHOLDER_EMAIL_MARKERS = ("@example.com",)

# Mapeo toast_guid -> gusto_employee_id, confirmado a mano. Lo genera
# build_maps.py y es la unica llave valida hacia Gusto.
# Directorio de los archivos de configuracion (mapeos y templates). Vacio = el
# directorio actual, que es como corre en local. Al desplegar, la app lo apunta a
# un temporal de la sesion y los archivos se suben por el navegador, de modo que
# el repo no lleve ni un nombre ni un sueldo.
DATA_DIR = os.environ.get("PAYROLL_DATA_DIR", "")


def data_path(nombre: str) -> str:
    return os.path.join(DATA_DIR, nombre) if DATA_DIR else nombre


EMPLOYEE_MAP_PATH = os.environ.get("PAYROLL_EMPLOYEE_MAP", "")

# Cuentas de dispositivo o de sistema que fichan turnos reales en Toast pero no
# son personas y no se pagan. Si entran a la nomina se le emite un renglon a
# algo que no tiene SSN.
NON_PERSON_MARKERS = ("kds", "login", "for all devices", "training",
                      "test account", "online ordering")

# Reglas de California, Labor Code 512(a).
#
# El primer meal se debe a partir de mas de 5 h y se puede renunciar por
# consentimiento mutuo solo si la jornada no pasa de 6 h.
FIRST_MEAL_TRIGGER_HOURS = 5.0
FIRST_MEAL_WAIVER_CEILING_HOURS = 6.0
MEAL_DEADLINE_HOURS = 5.0

# El segundo meal se debe a partir de mas de DIEZ horas. Las 12 h son el techo
# del waiver, no el disparador: confundirlos deja sin auditar toda la banda de
# 10 a 12 h. Medido en esta cuenta: 36 turnos en esa banda en 28 dias, 33 sin
# segundo meal valido, ~$609 de primas que nunca se detectaron.
SECOND_MEAL_TRIGGER_HOURS = 10.0
SECOND_MEAL_WAIVER_CEILING_HOURS = 12.0
SECOND_MEAL_DEADLINE_HOURS = 10.0

# El waiver del segundo meal exige consentimiento mutuo comprobable Y que no se
# haya renunciado al primero. En esta cuenta los 378 breaks vienen con waived=0 y
# auditResponse=null: no hay attestation configurada en Toast, asi que no hay
# waiver que invocar y la banda 10-12 h NO se exenta. Poner en True solo cuando
# Toast este capturando la attestation y exista el consentimiento por escrito.
MEAL_WAIVERS_DOCUMENTED = False

MIN_BREAK_MINUTES = 30

# Tope legal: las primas por meal period se topan en 1 hora por dia de trabajo,
# sin importar cuantos meal periods se hayan perdido ese dia.
MEAL_PREMIUM_HOURS_CAP_PER_DAY = 1.0

# Calendario de nomina, leido de Gusto por MCP (pay_schedule
# ff311926-90c9-4b10-98e9-71b7a8980d4f, "every other Friday", frecuencia
# "Every other week", anchor_end_of_pay_period 2025-10-17). Los periodos corren
# SABADO a VIERNES y el cheque sale el viernes siguiente al cierre.
PAY_PERIOD_DAYS = 14
PAY_PERIOD_ANCHOR_END = date(2026, 10, 2)   # viernes de cierre conocido


def pay_period_containing(day: date) -> tuple:
    """Devuelve (inicio, fin, fecha_de_cheque) del periodo que contiene a `day`."""
    offset = (PAY_PERIOD_ANCHOR_END - day).days % PAY_PERIOD_DAYS
    end = day + timedelta(days=offset)
    start = end - timedelta(days=PAY_PERIOD_DAYS - 1)
    return start, end, end + timedelta(days=7)


def previous_closed_pay_period(today: date | None = None) -> tuple:
    """El ultimo periodo que ya cerro, que es el que se puede correr."""
    today = today or date.today()
    start, end, check = pay_period_containing(today)
    while end >= today:
        start -= timedelta(days=PAY_PERIOD_DAYS)
        end -= timedelta(days=PAY_PERIOD_DAYS)
        check -= timedelta(days=PAY_PERIOD_DAYS)
    return start, end, check


def _limpiar_credencial(valor: str) -> str:
    """Quita lo que se pega de mas al copiar una credencial.

    Un espacio al final o unas comillas que quedaron dentro del valor dan un 401
    identico al de una credencial equivocada, y en pantalla no se ven. Es la
    causa mas comun de que falle recien desplegado.
    """
    limpio = (valor or "").strip().strip(" ").strip()
    for comilla in ('"', "'", "“", "”"):
        if limpio.startswith(comilla) and limpio.endswith(comilla) and len(limpio) > 1:
            limpio = limpio[1:-1].strip()
    return limpio


def huella(valor: str) -> str:
    """Identifica una credencial sin revelarla, para poder compararla a ojo."""
    limpio = _limpiar_credencial(valor)
    if not limpio:
        return "(vacio)"
    if len(limpio) <= 10:
        return f"{len(limpio)} caracteres"
    return f"{len(limpio)} caracteres, {limpio[:4]}...{limpio[-4:]}"


class ToastClient:
    """Cliente minimo de la Labor API de Toast."""

    def __init__(self, client_id: str, client_secret: str, api_host: str = TOAST_API_HOST):
        self.api_host = api_host.rstrip("/")
        self._client_id = _limpiar_credencial(client_id)
        self._client_secret = _limpiar_credencial(client_secret)
        self._token: str | None = None

    @classmethod
    def from_env(cls) -> "ToastClient":
        client_id = os.environ.get("TOAST_CLIENT_ID", "")
        client_secret = os.environ.get("TOAST_CLIENT_SECRET", "")
        if not client_id or not client_secret:
            raise RuntimeError(
                "Faltan TOAST_CLIENT_ID y TOAST_CLIENT_SECRET en el entorno. "
                "Ver secrets.toml.example"
            )
        return cls(client_id, client_secret)

    @property
    def token(self) -> str:
        if self._token is None:
            self._token = self._login()
        return self._token

    def _login(self) -> str:
        url = f"{self.api_host}/authentication/v1/authentication/login"
        payload = {
            "clientId": self._client_id,
            "clientSecret": self._client_secret,
            "userAccessType": "TOAST_MACHINE_CLIENT",
        }
        request = urllib.request.Request(
            url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)["token"]["accessToken"]
        except urllib.error.HTTPError as error:
            if error.code in (401, 403):
                # Toast explica el rechazo en el cuerpo y manda un requestId que
                # su soporte puede rastrear. Tirarlo deja al operador adivinando.
                motivo, request_id = "", ""
                try:
                    cuerpo = json.loads(error.read().decode())
                    motivo = (cuerpo.get("error_description")
                              or cuerpo.get("message") or "")
                    request_id = cuerpo.get("requestId") or ""
                except Exception:
                    pass

                detalle = [
                    f"Toast rechazo las credenciales (HTTP {error.code}).",
                    f"  TOAST_CLIENT_ID     lei {huella(self._client_id)}  "
                    f"(deberia ser 32 caracteres)",
                    f"  TOAST_CLIENT_SECRET lei {huella(self._client_secret)}  "
                    f"(deberia ser 64 caracteres)",
                ]
                if motivo:
                    detalle.append(f"  Toast dice: {motivo}")
                detalle += [
                    "  Si las longitudes son 32 y 64, no se corto al pegarlo.",
                    "",
                    "  Un 401 en el endpoint de autenticacion significa que Toast "
                    "no reconoce el par ID+secret. Ocurre ANTES de evaluar scopes "
                    "o locations, que darian 403, no 401. Asi que el problema es "
                    "el par:",
                    "    1. El secret no es el del MISMO credential que el ID. "
                    "Toast lo muestra una sola vez al crearlo o rotarlo; si se "
                    "perdio, hay que rotarlo de nuevo y copiar los dos juntos.",
                    "    2. El credential quedo en estado Locked, o se borro.",
                    "    3. Se acaba de crear y aun no propaga (unos minutos).",
                    "  Senal util: al crear o rotar, Toast manda un correo "
                    "confirmando que el acceso quedo activado. Si no llego, la "
                    "creacion no se completo.",
                ]
                if request_id:
                    detalle.append(f"  requestId para el soporte de Toast: {request_id}")
                raise RuntimeError("\n".join(detalle)) from None
            raise

    def _get(self, path: str, restaurant_guid: str, params: dict | None = None):
        url = f"{self.api_host}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Toast-Restaurant-External-ID": restaurant_guid,
            "Content-Type": "application/json",
        }
        request = urllib.request.Request(url, headers=headers)
        return self._abrir_con_reintento(request)

    # Toast limita la tasa de llamadas y responde 429 cuando se pasa. Sin
    # reintento, una nomina a medio correr se cae y hay que empezar de cero:
    # para un periodo de 14 dias son cientos de llamadas y basta una. El
    # reintento respeta Retry-After cuando Toast lo manda, y si no, espera el
    # doble cada vez. Tambien reintenta los 5xx, que son fallas del lado de
    # Toast y suelen pasar solas.
    RATE_LIMIT_INTENTOS = 6
    RATE_LIMIT_ESPERA_BASE = 2.0
    # Tope por espera. Sin el, un Retry-After que manda el servidor decide
    # cuanto se congela la app: alguien esta esperando frente a la pantalla el
    # viernes de pago. Con 6 intentos el peor caso son 5 esperas.
    RATE_LIMIT_ESPERA_MAXIMA = 30.0

    def _abrir_con_reintento(self, request, timeout: int = 180):
        for intento in range(self.RATE_LIMIT_INTENTOS):
            try:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    return json.load(response)
            except urllib.error.HTTPError as error:
                ultimo = intento == self.RATE_LIMIT_INTENTOS - 1
                if error.code != 429 and error.code < 500:
                    raise
                if ultimo:
                    raise RuntimeError(
                        f"Toast sigue respondiendo {error.code} despues de "
                        f"{self.RATE_LIMIT_INTENTOS} intentos en {request.full_url}. "
                        f"Si es 429, hay demasiadas llamadas al mismo tiempo; "
                        f"vuelve a correr en unos minutos."
                    ) from None
                espera = self.RATE_LIMIT_ESPERA_BASE * (2 ** intento)
                # `if error.headers` seria truthiness sobre el objeto de encabezados:
                # uno vacio es falsy y se saltaria la indicacion de Toast.
                encabezados = getattr(error, "headers", None)
                indicado = (encabezados.get("Retry-After")
                            if encabezados is not None else None)
                if indicado:
                    try:
                        # Retry-After tambien puede venir como fecha HTTP; en ese
                        # caso float() falla y se queda el backoff exponencial.
                        espera = max(espera, float(indicado))
                    except ValueError:
                        pass
                sleep(min(espera, self.RATE_LIMIT_ESPERA_MAXIMA))
        raise AssertionError("inalcanzable")

    def get_restaurants(self) -> list:
        """Restaurantes donde esta habilitada esta integracion.

        Es la fuente de verdad del alcance del pipeline. No requiere el header
        Toast-Restaurant-External-ID porque es un endpoint de partner.
        """
        url = f"{self.api_host}/partners/v1/restaurants"
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
        request = urllib.request.Request(url, headers=headers)
        return self._abrir_con_reintento(request, timeout=60)

    def get_employees(self, restaurant_guid: str) -> list:
        return self._get("/labor/v1/employees", restaurant_guid)

    def get_jobs(self, restaurant_guid: str) -> list:
        return self._get("/labor/v1/jobs", restaurant_guid)

    def get_restaurant_config(self, restaurant_guid: str) -> dict:
        """Config del restaurante. De aqui sale el closeoutHour, que define
        donde corta el business date (en esta cuenta: 4 a.m.)."""
        return self._get(f"/restaurants/v1/restaurants/{restaurant_guid}", restaurant_guid)

    def get_time_entries_by_business_date(self, restaurant_guid: str,
                                          start: date, end_inclusive: date) -> list:
        """Trae los turnos por businessDate, un dia por llamada.

        Es lo correcto en vez de filtrar por una ventana de inDate: el business
        date cambia al closeoutHour (4 a.m. aca), asi que un turno de cierre que
        ficha a las 00:30 pertenece al business date del dia anterior. Filtrando
        por inDate ese turno cae del lado equivocado del periodo y se pierde
        completo, con sus horas y sus tips.
        """
        entries = []
        day = start
        while day <= end_inclusive:
            entries.extend(self._get(
                "/labor/v1/timeEntries", restaurant_guid,
                {"businessDate": day.strftime("%Y%m%d"), "includeMissedBreaks": "true"},
            ))
            day += timedelta(days=1)
        return entries

    def get_time_entries(
        self,
        restaurant_guid: str,
        start_dt: datetime,
        end_dt: datetime,
        include_archived: bool = False,
    ) -> list:
        return self._get(
            "/labor/v1/timeEntries",
            restaurant_guid,
            {
                "startDate": _format_toast_utc(start_dt),
                "endDate": _format_toast_utc(end_dt),
                "includeMissedBreaks": "true",
                "includeArchived": str(include_archived).lower(),
            },
        )


def _format_toast_utc(dt: datetime) -> str:
    return dt.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%S.000%z")


def elapsed_hours(start: datetime, end: datetime) -> float:
    """Horas reales entre dos instantes, no horas de reloj de pared.

    Python documenta que si dos datetimes tienen el MISMO tzinfo, la resta
    ignora el offset y se hace como si fueran naive. Con ZoneInfo en los dos
    lados, la noche en que termina el horario de verano un turno de 22:00 a
    03:00 mide 5.00 h cuando en realidad duro 6.00 h. Restar en absoluto lo
    arregla: el turno de cierre del 2026-11-01 recupera su hora.
    """
    return (end.timestamp() - start.timestamp()) / 3600.0


def add_work_hours(start: datetime, hours: float) -> datetime:
    """start mas `hours` de trabajo REAL, no de reloj.

    El deadline del meal se cuenta sobre horas trabajadas, asi que sumar reloj
    de pared lo corre una hora la noche del cambio de horario.
    """
    return datetime.fromtimestamp(start.timestamp() + hours * 3600,
                                  tz=start.tzinfo)


def to_local_dt(dt_str: str | None, tz_name: str = TOAST_TIMEZONE) -> datetime | None:
    """Toast devuelve el offset como +0000; fromisoformat necesita +00:00."""
    if not dt_str:
        return None
    normalized = dt_str.replace("+0000", "+00:00")
    return datetime.fromisoformat(normalized).astimezone(ZoneInfo(tz_name))


def safe_float(value, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def pay_period_bounds(start: date, end_inclusive: date, tz_name: str = TOAST_TIMEZONE):
    """Toast acepta maximo 30 dias por llamada; el rango que arma es medio abierto."""
    tz = ZoneInfo(tz_name)
    if (end_inclusive - start).days + 1 > 30:
        raise ValueError("Toast permite maximo 30 dias por llamada; parte el rango.")
    start_dt = datetime.combine(start, time.min, tzinfo=tz)
    end_dt = datetime.combine(end_inclusive + timedelta(days=1), time.min, tzinfo=tz)
    return start_dt, end_dt


def is_usable_email(email: str) -> bool:
    """Un email sirve como llave hacia Gusto solo si es real."""
    if not email or "@" not in email:
        return False
    return not any(marker in email for marker in PLACEHOLDER_EMAIL_MARKERS)


def load_employee_map(path: str = "") -> dict:
    """Carga el mapeo toast_guid -> identidad en Gusto que un humano confirmo.

    Lee el archivo que produce build_maps.py, cuyas columnas son:
      toast_guid, toast_location, toast_name, toast_email,
      gusto_employee_id, gusto_name, confianza, accion, nota

    Devuelve {toast_guid: {"gusto_employee_id", "excluir", "pendiente", ...}}.

    La llave hacia Gusto es gusto_employee_id, NO el email: el template de Gusto
    no tiene columna de email. Una fila con accion=CONFIRMAR queda marcada como
    pendiente y bloquea el export; una con accion=EXCLUIR se saca de la nomina.
    """
    path = path or EMPLOYEE_MAP_PATH or data_path("employee_map.csv")
    if not os.path.exists(path):
        return {}

    overrides = {}
    with open(path, newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            guid = (row.get("toast_guid") or "").strip()
            if not guid:
                continue
            action = (row.get("accion") or "").strip().upper()
            employee_id = (row.get("gusto_employee_id") or "").strip()
            overrides[guid] = {
                "gusto_employee_id": employee_id,
                "person_key": (row.get("person_key") or "").strip(),
                "persona_confirmada": (row.get("persona_confirmada") or "").strip(),
                "gusto_name": (row.get("gusto_name") or "").strip(),
                "confianza": (row.get("confianza") or "").strip(),
                "excluir": action == "EXCLUIR",
                "pendiente": action == "CONFIRMAR" or (not employee_id and action != "EXCLUIR"),
                "nota": (row.get("nota") or "").strip(),
            }
    return overrides


def resolve_locations(client: ToastClient, include_unconfigured: bool = True) -> list:
    """Descubre las locations en Toast y las cruza con su empresa de Gusto.

    Devuelve una lista de dicts con guid, toast_name, short, company, template,
    timezone y configured. Una location que Toast reporta pero que no esta en
    GUSTO_COMPANIES sale con configured=False: son horas reales que hoy no
    tendrian a donde ir, asi que el exportador se detiene si encuentra una.
    """
    locations = []
    for restaurant in client.get_restaurants():
        if restaurant.get("deleted"):
            continue
        guid = restaurant.get("restaurantGuid")
        if not guid:
            continue
        config = GUSTO_COMPANIES.get(guid, {})
        toast_name = (restaurant.get("restaurantName") or "").strip()
        locations.append({
            "guid": guid,
            "toast_name": toast_name,
            "short": config.get("short") or toast_name or guid,
            "company": config.get("company", ""),
            "template": data_path(config["template"]) if config.get("template") else "",
            "management_group": restaurant.get("managementGroupGuid"),
            "configured": bool(config),
        })

    if not include_unconfigured:
        locations = [loc for loc in locations if loc["configured"]]
    return sorted(locations, key=lambda loc: loc["short"])


def assert_all_locations_configured(locations: list) -> None:
    """Se niega a seguir si Toast reporta una location sin empresa de Gusto."""
    unconfigured = [loc for loc in locations if not loc["configured"]]
    if not unconfigured:
        return
    detail = "\n".join(
        f"    {loc['toast_name']}  guid={loc['guid']}" for loc in unconfigured
    )
    raise RuntimeError(
        "Toast reporta locations que no estan mapeadas a una empresa de Gusto:\n"
        f"{detail}\n"
        "  Agregalas a GUSTO_COMPANIES en toast_payroll.py con su template, o el\n"
        "  pipeline dejaria esas horas fuera de la nomina sin avisar."
    )


def build_employee_index(client: ToastClient, employee_map: dict | None = None,
                         locations: list | None = None) -> dict:
    """Indexa los empleados de todas las locations por GUID de Toast.

    El email que queda en "email" es el que se usa para unir con Gusto: real de
    Toast, o el del mapeo manual. Si no hay ninguno queda vacio y el empleado
    sale reportado en las excepciones.
    """
    overrides = employee_map or {}
    if locations is None:
        locations = resolve_locations(client)
    index = {}
    for location in locations:
        location_name, restaurant_guid = location["short"], location["guid"]
        for employee in client.get_employees(restaurant_guid):
            guid = employee.get("guid")
            if not guid:
                continue
            first = (employee.get("chosenName") or employee.get("firstName") or "").strip()
            last = (employee.get("lastName") or "").strip()
            raw_email = (employee.get("email") or "").strip().lower()
            override = overrides.get(guid) or {}

            display_name = f"{last} {first}".strip() or guid
            is_non_person = any(
                marker in f"{last} {first}".strip().lower()
                for marker in NON_PERSON_MARKERS
            )

            index[guid] = {
                # La identidad en Gusto. Vacia = no se puede pagar a esta persona.
                "gusto_employee_id": override.get("gusto_employee_id", ""),
                "gusto_name": override.get("gusto_name", ""),
                "confianza": override.get("confianza", "sin mapear"),
                "excluir": bool(override.get("excluir")) or is_non_person,
                "pendiente": bool(override.get("pendiente")) if override else True,
                "is_non_person": is_non_person,
                # El email es informativo: sirve para desempatar candidatos a
                # mano, no como llave, porque el template de Gusto no lo trae.
                "email": raw_email if is_usable_email(raw_email) else "",
                "raw_email": raw_email,
                "first_name": first,
                "last_name": last,
                "display_name": display_name,
                "external_employee_id": employee.get("externalEmployeeId"),
                "location": location_name,
                "location_guid": restaurant_guid,
            }
    return index


def normalize_meal_breaks(time_entry: dict, tz_name: str = TOAST_TIMEZONE) -> list:
    """Marca cuales breaks del shift cuentan como meal valido.

    Un meal valido es del tipo meal, no missed, no waived, y de al menos
    MIN_BREAK_MINUTES ininterrumpidos. Un meal corto o interrumpido no cuenta,
    y por lo tanto genera violacion.
    """
    normalized = []
    minimum = timedelta(minutes=MIN_BREAK_MINUTES)

    for break_item in time_entry.get("breaks") or []:
        break_type_guid = (break_item.get("breakType") or {}).get("guid")
        start = to_local_dt(break_item.get("inDate"), tz_name)
        end = to_local_dt(break_item.get("outDate"), tz_name)
        duration = (timedelta(hours=elapsed_hours(start, end))
                    if (start and end) else None)
        is_meal_type = break_type_guid in MEAL_BREAK_TYPE_GUIDS
        missed = bool(break_item.get("missed", False))
        waived = bool(break_item.get("waived", False))

        normalized.append(
            {
                "break_type_guid": break_type_guid,
                "start": start,
                "end": end,
                "duration_minutes": round(duration.total_seconds() / 60.0, 1) if duration else None,
                "missed": missed,
                "waived": waived,
                "is_meal_type": is_meal_type,
                "is_paid_rest": break_type_guid in PAID_REST_BREAK_TYPE_GUIDS,
                "counts_as_meal": (
                    is_meal_type
                    and not missed
                    and not waived
                    and duration is not None
                    and duration >= minimum
                ),
            }
        )

    far_future = datetime(9999, 12, 31, tzinfo=ZoneInfo(tz_name))
    normalized.sort(key=lambda item: item["start"] or far_future)
    return normalized


def audit_time_entry(time_entry: dict, tz_name: str = TOAST_TIMEZONE) -> dict:
    """Audita un solo shift y devuelve horas, violaciones y tips.

    Distingue dos cantidades que NO son lo mismo y que antes estaban colapsadas:

      payable_hours  regularHours + overtimeHours. Es lo que Toast ya calculo
                     NETO del break no pagado, y es lo que se paga.
      work_period    el tiempo del empleado en el local menos los breaks no
                     pagados. Es lo que dispara los umbrales de meal.

    Usar payable_hours como disparador deja sin auditar los turnos donde el
    propio break del empleado encoge la jornada por debajo del umbral. Medido en
    esta cuenta: 47 turnos en 28 dias con pagables <= 6.00 h y transcurrido
    > 6.00 h, que hoy no se auditaban.
    """
    start = to_local_dt(time_entry.get("inDate"), tz_name)
    end = to_local_dt(time_entry.get("outDate"), tz_name)

    regular_hours = safe_float(time_entry.get("regularHours"))
    overtime_hours = safe_float(time_entry.get("overtimeHours"))
    payable_hours = regular_hours + overtime_hours

    breaks = normalize_meal_breaks(time_entry, tz_name)
    meals = [item["start"] for item in breaks if item["counts_as_meal"]]

    unpaid_break_hours = sum(
        (item["duration_minutes"] or 0) / 60.0
        for item in breaks
        if not item["is_paid_rest"] and item["duration_minutes"]
    )
    if start and end:
        work_period = max(0.0, elapsed_hours(start, end) - unpaid_break_hours)
    else:
        # Turno abierto: no se puede medir la jornada, asi que no se audita.
        # Sale como excepcion bloqueante en _collect_exceptions.
        work_period = 0.0

    # Un break de comida que Toast reporta con missed=True llega con inDate y
    # SIN outDate: no hay salida que marcar porque el descanso no ocurrio. Hay
    # que pedir includeMissedBreaks=true para que vengan. Medido el 2026-10-02
    # en el periodo 09-05/18: Gaslamp trae 51 de 136 asi y National City 0 de
    # 267. Eso NO es un marcaje incompleto, es Toast diciendo que no se descanso.
    violations = []
    if start and end:
        if work_period > FIRST_MEAL_WAIVER_CEILING_HOURS:
            if not meals:
                violations.append("Missed Meal Break")
            elif meals[0] > add_work_hours(start, MEAL_DEADLINE_HOURS):
                violations.append("Late Meal Break")

        second_meal_owed = work_period > SECOND_MEAL_TRIGGER_HOURS
        if second_meal_owed and MEAL_WAIVERS_DOCUMENTED:
            # El waiver solo alcanza la banda hasta el techo y solo si el primer
            # meal si se tomo.
            if work_period <= SECOND_MEAL_WAIVER_CEILING_HOURS and len(meals) >= 1:
                second_meal_owed = False

        if second_meal_owed:
            if len(meals) < 2:
                violations.append("Missing 2nd Meal")
            elif meals[1] > add_work_hours(start, SECOND_MEAL_DEADLINE_HOURS):
                violations.append("Late 2nd Meal")

    # hourlyWage null significa puesto asalariado, no tarifa cero. Aplanarlo a
    # 0.0 diluye el regular rate del dia o paga la prima a $0.00. Medido: 135 de
    # 1,280 turnos en 28 dias vienen con hourlyWage null.
    raw_wage = time_entry.get("hourlyWage")
    hourly_wage = None if raw_wage is None else safe_float(raw_wage)

    return {
        "employee_guid": (time_entry.get("employeeReference") or {}).get("guid"),
        "business_date": time_entry.get("businessDate"),
        "start": start,
        "end": end,
        "is_open_shift": end is None,
        "auto_clocked_out": bool(time_entry.get("autoClockedOut")),
        "regular_hours": regular_hours,
        "overtime_hours": overtime_hours,
        "payable_hours": payable_hours,
        "work_period_hours": round(work_period, 4),
        "unpaid_break_hours": round(unpaid_break_hours, 4),
        "hourly_wage": hourly_wage,
        "is_salaried_shift": hourly_wage is None,
        "violations": violations,
        "breaks": breaks,
        # Tips reales: propiedad del empleado, excluidos del regular rate.
        "non_cash_tips": safe_float(time_entry.get("nonCashTips")),
        "declared_cash_tips": safe_float(time_entry.get("declaredCashTips")),
        # Service charges: legalmente son salario, no tips. Entran al regular rate.
        "non_cash_service_charges": safe_float(time_entry.get("nonCashGratuityServiceCharges")),
        "cash_service_charges": safe_float(time_entry.get("cashGratuityServiceCharges")),
        "tips_withheld": safe_float(time_entry.get("tipsWithheld")),
    }


def aggregate_for_pay_period(client: ToastClient, start: date, end_inclusive: date) -> dict:
    """Agrega un periodo de nomina por empleado, sumando ambas locations.

    Devuelve {"rows": [...], "exceptions": [...], "totals": {...}}. Las filas se
    agregan por email porque en Gusto un empleado que trabaja en las dos
    locations es un solo registro.
    """
    start_dt, end_dt = pay_period_bounds(start, end_inclusive)
    locations = resolve_locations(client)
    assert_all_locations_configured(locations)
    employees = build_employee_index(client, load_employee_map(), locations)

    audited = []
    for location in locations:
        for time_entry in client.get_time_entries(location["guid"], start_dt, end_dt):
            if time_entry.get("deleted"):
                continue
            row = audit_time_entry(time_entry)
            row["location"] = location["short"]
            row["location_guid"] = location["guid"]
            row["gusto_company"] = location["company"]
            audited.append(row)

    # La prima de meal se topa en 1 hora por empleado por dia de trabajo. El
    # regular rate del dia es el promedio ponderado por horas pagadas e incluye
    # los service charges distribuidos, que legalmente son salario: Ferra v.
    # Loews exige el "regular rate of compensation", no el base wage. Los tips
    # quedan fuera, porque no entran al regular rate.
    day_buckets = defaultdict(lambda: {
        "violations": 0, "wage_hours": 0.0, "hours": 0.0,
        "service_charges": 0.0, "has_salaried_shift": False,
    })
    def identity_of(guid: str) -> str:
        """Identidad estable de la persona: el id de Gusto, no el GUID de Toast.

        Una persona que trabaja en las dos locations tiene DOS GUID de Toast. Si
        el tope de 1 h por dia se llavea por GUID, esa persona cobra 2 h de prima
        por un solo dia de trabajo.
        """
        employee = employees.get(guid) or {}
        return employee.get("gusto_employee_id") or f"SIN-MAPEO::{guid}"

    for row in audited:
        bucket = day_buckets[(identity_of(row["employee_guid"]), row["business_date"])]
        bucket["violations"] += len(row["violations"])
        bucket["service_charges"] += (
            row["non_cash_service_charges"] + row["cash_service_charges"]
        )
        if row["hourly_wage"] is None:
            # Turno asalariado: no aporta tarifa horaria. Meterlo al ponderado
            # con valor cero y peso completo diluiria el rate del dia.
            bucket["has_salaried_shift"] = True
            continue
        bucket["wage_hours"] += row["hourly_wage"] * row["payable_hours"]
        bucket["hours"] += row["payable_hours"]

    premium_by_employee = defaultdict(
        lambda: {"hours": 0.0, "amount": 0.0, "days": 0, "days_sin_tarifa": 0}
    )
    for (identity, _business_date), bucket in day_buckets.items():
        if bucket["violations"] <= 0:
            continue
        premium = premium_by_employee[identity]
        hours = min(float(bucket["violations"]), MEAL_PREMIUM_HOURS_CAP_PER_DAY)
        premium["hours"] += hours
        premium["days"] += 1

        if bucket["hours"] <= 0:
            # Ningun turno del dia trae tarifa horaria, asi que la tarifa de la
            # prima no se puede derivar. No se inventa un $0.00: se cuenta como
            # dia sin tarifa y eso bloquea el export.
            premium["days_sin_tarifa"] += 1
            continue
        rate = (bucket["wage_hours"] + bucket["service_charges"]) / bucket["hours"]
        premium["amount"] += hours * rate

    by_identity = {}
    identity_locations = defaultdict(set)
    excluded = []
    for row in audited:
        employee = employees.get(row["employee_guid"], {})
        if employee.get("excluir"):
            excluded.append({
                "nombre": employee.get("display_name", row["employee_guid"]),
                "location": row["location"],
                "motivo": "cuenta de dispositivo o de sistema"
                          if employee.get("is_non_person") else "marcado EXCLUIR en el mapeo",
            })
            continue

        identity = identity_of(row["employee_guid"])
        identity_locations[identity].add(row["location"])
        # La llave incluye la empresa: ninguna fila puede cruzar LLCs.
        record = by_identity.setdefault(
            (row["gusto_company"], identity),
            {
                "gusto_company": row["gusto_company"],
                "gusto_employee_id": employee.get("gusto_employee_id", ""),
                "identity": identity,
                "confianza": employee.get("confianza", "sin mapear"),
                "pendiente": bool(employee.get("pendiente", True)),
                "email": employee.get("email", ""),
                "first_name": employee.get("first_name", ""),
                "last_name": employee.get("last_name", ""),
                "toast_guids": set(),
                "locations": set(),
                "regular_hours": 0.0,
                "overtime_hours": 0.0,
                "meal_premium_hours": 0.0,
                "meal_premium_amount": 0.0,
                "meal_violation_days": 0,
                "meal_premium_days_sin_tarifa": 0,
                "non_cash_tips": 0.0,
                "declared_cash_tips": 0.0,
                "service_charges": 0.0,
                "shifts": 0,
            },
        )
        record["toast_guids"].add(row["employee_guid"])
        record["locations"].add(row["location"])
        record["regular_hours"] += row["regular_hours"]
        record["overtime_hours"] += row["overtime_hours"]
        record["non_cash_tips"] += row["non_cash_tips"]
        record["declared_cash_tips"] += row["declared_cash_tips"]
        record["service_charges"] += row["non_cash_service_charges"] + row["cash_service_charges"]
        record["shifts"] += 1

    # La prima del dia ya viene topada por persona. Si la persona trabaja en dos
    # LLCs se le atribuye a una sola vez, no una por empresa.
    premium_claimed = set()
    for record in sorted(by_identity.values(), key=lambda r: r["gusto_company"]):
        premium = premium_by_employee.get(record["identity"])
        if premium and record["identity"] not in premium_claimed:
            premium_claimed.add(record["identity"])
            record["meal_premium_hours"] += premium["hours"]
            record["meal_premium_amount"] += premium["amount"]
            record["meal_violation_days"] += premium["days"]
            record["meal_premium_days_sin_tarifa"] += premium["days_sin_tarifa"]

    exceptions = _collect_exceptions(audited, employees, by_identity, identity_locations)

    rows = sorted(by_identity.values(),
                  key=lambda r: (r["gusto_company"], r["last_name"], r["first_name"]))
    money_and_hours = (
        "regular_hours", "overtime_hours", "meal_premium_hours", "meal_premium_amount",
        "non_cash_tips", "declared_cash_tips", "service_charges",
    )
    for record in rows:
        record["locations"] = " + ".join(sorted(record["locations"]))
        record["toast_guids"] = " ".join(sorted(record["toast_guids"]))
        for key in money_and_hours:
            record[key] = round(record[key], 2)

    totals = {"empleados": len(rows), "shifts": len(audited)}
    for key in money_and_hours:
        totals[key] = round(sum(r[key] for r in rows), 2)

    by_company = {}
    for record in rows:
        company = record["gusto_company"] or "(sin empresa)"
        block = by_company.setdefault(company, {"empleados": 0, "pendientes": 0,
                                                **{k: 0.0 for k in money_and_hours}})
        block["empleados"] += 1
        block["pendientes"] += 1 if record["pendiente"] else 0
        for key in money_and_hours:
            block[key] = round(block[key] + record[key], 2)

    pending = [r for r in rows if r["pendiente"]]
    return {"rows": rows, "exceptions": exceptions, "totals": totals,
            "by_company": by_company, "locations": locations,
            "excluded": excluded, "pending": pending}


def _collect_exceptions(audited, employees, by_identity, identity_locations) -> list:
    """Casos que necesitan ojo humano antes de subir el CSV a Gusto."""
    exceptions = []
    # Un shift abierto en el ultimo dia del rango casi siempre es alguien que
    # sigue trabajando; en un dia anterior es un clock-out que nadie cerro.
    last_business_date = max((row["business_date"] or "" for row in audited), default="")

    for row in audited:
        name = employees.get(row["employee_guid"], {}).get("display_name", row["employee_guid"])

        if row["is_open_shift"]:
            is_last_day = row["business_date"] == last_business_date
            exceptions.append({
                "tipo": "Shift abierto" if is_last_day else "Clock-out olvidado",
                "empleado": name,
                "detalle": f"{row['business_date']} en {row['location']}: sin clock-out. "
                           + ("Probablemente sigue trabajando: corre el export cuando cierre "
                              "el periodo." if is_last_day else
                              "Es de un dia ya cerrado: hay que arreglarlo en Toast, las horas "
                              "de ese turno estan incompletas."),
            })

        if row["payable_hours"] > 12:
            exceptions.append({
                "tipo": "Double time sin calcular",
                "empleado": name,
                "detalle": f"{row['business_date']}: {row['payable_hours']:.2f} h pagables. "
                           "Toast no devuelve doubleOvertimeHours y California exige 2x "
                           "despues de 12 h en la jornada. Capturar a mano.",
            })

        if row["auto_clocked_out"]:
            exceptions.append({
                "tipo": "Cerrado por el sistema",
                "empleado": name,
                "detalle": f"{row['business_date']} en {row['location']}: autoClockedOut. "
                           "La hora de salida la puso Toast, no la persona, asi que las "
                           "horas pueden no reflejar lo que trabajo.",
            })

        if row["is_salaried_shift"]:
            exceptions.append({
                "tipo": "Turno asalariado",
                "empleado": name,
                "detalle": f"{row['business_date']} en {row['location']}: hourlyWage viene "
                           "null, es un puesto asalariado. No aporta tarifa al regular rate "
                           "del dia; si ese dia hubo violacion, la prima se captura a mano.",
            })

    unmapped = {}
    for row in audited:
        employee = employees.get(row["employee_guid"], {})
        if employee.get("pendiente") and not employee.get("excluir"):
            unmapped[row["employee_guid"]] = employee
    for employee_guid, employee in unmapped.items():
        exceptions.append({
            "tipo": "Sin identidad en Gusto",
            "empleado": employee.get("display_name", employee_guid),
            "detalle": "No tiene gusto_employee_id confirmado, asi que no se puede "
                       f"pagar. Confirmalo en employee_map.csv para el guid {employee_guid}"
                       + (f" (candidato: {employee['gusto_name']})"
                          if employee.get("gusto_name") else "")
                       + f" [confianza: {employee.get('confianza', 'sin mapear')}]",
        })

    for identity, locations in identity_locations.items():
        if len(locations) > 1:
            exceptions.append({
                "tipo": "Trabaja en 2 LLCs",
                "empleado": identity,
                "detalle": f"{', '.join(sorted(locations))}. Son dos empresas distintas en "
                           "Gusto, asi que sale en dos CSV. Toast calcula el overtime por "
                           "restaurante por separado: las horas semanales combinadas pueden "
                           "cruzar las 40 sin que ninguna location marque OT, y la prima de "
                           "meal se atribuye a una sola de las dos.",
            })

    for record in by_identity.values():
        if record.get("meal_premium_days_sin_tarifa"):
            exceptions.append({
                "tipo": "Prima sin tarifa",
                "empleado": f"{record['last_name']} {record['first_name']}".strip(),
                "detalle": f"{record['meal_premium_days_sin_tarifa']} dia(s) con violacion "
                           "donde ningun turno traia tarifa horaria. La prima se debe pero su "
                           "monto NO se calculo: capturarlo a mano.",
            })

    for record in by_identity.values():
        if record["service_charges"] > 0:
            exceptions.append({
                "tipo": "Service charges",
                "empleado": f"{record['last_name']} {record['first_name']}".strip(),
                "detalle": f"${record['service_charges']:.2f} de auto-gratuity. Legalmente es "
                           "salario, no tip: va como earning aparte y cuenta para el regular "
                           "rate del overtime.",
            })

    return exceptions


def write_csv(path: str, rows: list, fieldnames: list) -> None:
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
