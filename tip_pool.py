"""Reconstruye el reparto del tip pool de Toast desde las ordenes.

Para que la nomina sea 100% automatica hay que dejar de bajar el reporte
EmployeeTipTotals a mano. Toast no expone la distribucion del pool por API, pero
si expone todo lo que hace falta para calcularla:

  tips por mesero por dia      /orders/v2/ordersBulk -> order.server +
                               checks[].payments[].tipAmount
  ventas por mesero por dia    selections[].salesCategory + selections[].price
                               (la base del aporte; los items SIN sales category
                               quedan fuera)
  quien estuvo fichado         /labor/v1/timeEntries, por job y por horas
  los porcentajes              la politica de Tips Manager, abajo en POLITICAS

El mesero viene en `order.server`, NO en `check.server`, que llega vacio.

ESTADO MEDIDO (2026-10-02), Gaslamp, dos periodos de 14 dias:

  bruto de las ordenes contra "antes del pool"   $0.00 y $1.00 de diferencia
  reparto contra "despues del pool"              $11.40 y $18.33 por periodo,
                                                 hasta $1.80 en una persona

El bruto cuadra. El reparto no: queda un residuo chico pero real. $1.80 en los
tips de alguien es un cheque equivocado, asi que este modulo NO se usa para
pagar. El reporte EmployeeTipTotals sigue siendo la fuente de verdad y hay que
seguir subiendolo; esto sirve para contrastarlo y sacar avisos.

National City esta SIN VALIDAR. Su pool se calcula sobre ventas (5.5%), no
sobre tips, que es un mecanismo distinto, y su bruto todavia sale corto unos
$75 por periodo. No se le puede atribuir la precision que se midio en Gaslamp.

Lo que falta para cerrarlo: el residuo por persona en Gaslamp, el hueco de $75
del bruto en NC, y validar NC contra su reporte igual que se hizo aca.
"""

from __future__ import annotations

import collections
from datetime import date, timedelta

# Las 7 sales categories de la cuenta. El aporte del mesero se calcula sobre las
# ventas que caen en alguna de ellas; lo que no tiene categoria queda fuera.
# Medido el 2026-09-10 en National City: $1,574.74 de ventas sin categoria, que
# es lo que explica el ~81% entre lo aportado y el 5.5% de las ventas totales.
SALES_CATEGORIES = {
    "4b7c3e80-35f2-48dc-9a97-d7db2b8b16d3": "Bottled Beer",
    "7db95fb4-5879-4a77-9ad0-1fa990b79764": "Liquor",
    "ae2aee68-ccfa-43ce-bf41-06d19462fa24": "NA Beverage",
    "bf42a581-4c7c-4148-89e7-84d57bad22c3": "Draft Beer",
    "cb89e05f-3baa-4da6-8b79-daea94eda149": "Food",
    "df84cd1f-64ea-4b42-b27c-112a86f1a404": "Retail",
    "f75d9ef4-0766-42e2-bdb5-5fbe5e5e5d1d": "Wine",
}

# Que hace Toast con la parte de un job receptor que no tuvo a nadie fichado.
# No se puede leer de la politica ni de la API: se midio contra el reporte
# EmployeeTipTotals, que es lo que de verdad se les pago. Se probaron las 4
# combinaciones en dos periodos, eligiendo la regla en uno y midiendola en el
# otro para no sobreajustar:
#
#   Online Ordering Pool 1 = retener, Pool 1 = repartir   $11.40 y $18.33
#   los dos repartir                                      $44.93 y $71.47
#   Pool 1 = retener                                      $1,697 y $2,113
#
# La combinacion ganadora esta separada de las otras por dos ordenes de
# magnitud, y da el mismo error en el periodo que no se uso para elegirla.
# Aun asi NO cuadra al centavo: quedan $11-18 por periodo, hasta $1.80 en una
# persona. No es redondeo (probado a 2 y 4 decimales, identico). Mientras ese
# residuo exista, esto no sirve para pagar: el reporte sigue siendo la fuente.
REGLA_NO_ASIGNABLE = ("repartir", "retener")


# Politicas leidas de Toast Web > Tips Manager. Los jobs son los de TOAST.
POLITICAS = {
    "National City": {
        "intervalo": "dia",
        "pools": [
            {
                "nombre": "Pool 1",
                "no_asignable": "repartir",    # SIN VALIDAR: NC nunca se midio
                "aportan": {"Server": 0.055},      # 5.5% de las ventas en las 7 categorias
                "base": "ventas",
                "reciben": {
                    "Taquero": 0.40, "Tortillera": 0.08, "Prep": 0.07, "Expo": 0.09,
                    "Dishwasher": 0.10, "Busser": 0.10, "Runner": 0.10, "Host": 0.06,
                },
            },
        ],
    },
    "Gaslamp Quarter": {
        "intervalo": "dia",
        "pools": [
            {
                "nombre": "Online Ordering Pool 1",
                "no_asignable": "retener",     # medido, ver REGLA_NO_ASIGNABLE
                "aportan": {"Online Ordering": 1.00},
                "base": "tips",
                "reciben": {
                    "Cashier": 0.27, "Busser": 0.07, "Tortilla station": 0.10,
                    "Dishwasher": 0.07, "Prep": 0.09, "Taquero": 0.30, "Lead": 0.10,
                },
            },
            {
                "nombre": "Pool 1",
                "no_asignable": "repartir",    # medido, ver REGLA_NO_ASIGNABLE
                "aportan": {j: 1.00 for j in (
                    "Busser", "Prep", "Taquero", "Salaried", "Tortilla station",
                    "Cashier", "Dishwasher", "Lead", "Encargado")},
                "base": "tips",
                "reciben": {
                    "Cashier": 0.26, "Busser": 0.07, "Dishwasher": 0.07, "Prep": 0.10,
                    "Taquero": 0.30, "Tortilla station": 0.10, "Lead": 0.10,
                },
            },
        ],
    },
}


def fetch_orders(client, restaurant_guid: str, business_date: date,
                 max_pages: int = 60) -> list:
    """Trae todas las ordenes de un business date, paginando."""
    stamp = business_date.strftime("%Y%m%d")
    orders = []
    page = 1
    while page <= max_pages:
        lote = client._get("/orders/v2/ordersBulk", restaurant_guid,
                           {"businessDate": stamp, "pageSize": 100, "page": page})
        if not lote:
            break
        orders.extend(lote)
        if len(lote) < 100:
            break
        page += 1
    return orders


def extract_day(orders: list) -> dict:
    """Saca, de un dia de ordenes, los tips y las ventas por mesero.

    Devuelve {"tips": {guid: $}, "ventas": {guid: $}, "sin_categoria": $,
              "tips_sin_mesero": $}.
    """
    tips = collections.defaultdict(float)
    ventas = collections.defaultdict(float)
    sin_categoria = 0.0
    tips_sin_mesero = 0.0

    for order in orders:
        if order.get("deleted"):
            continue
        server = (order.get("server") or {}).get("guid")
        for check in (order.get("checks") or []):
            if check.get("deleted"):
                continue
            # Solo los pagos que quedaron CAPTURED: un VOIDED o DENIED trae su
            # tipAmount en el JSON pero nadie lo cobro, y sumarlo infla el pool.
            propina = sum(float(p.get("tipAmount") or 0)
                          for p in (check.get("payments") or [])
                          if not p.get("deleted")
                          and p.get("paymentStatus") == "CAPTURED")
            # La propina automatica no es un tipAmount: viene como service charge
            # marcado gratuity=true. Sin esto el bruto queda corto.
            propina += sum(float(sc.get("chargeAmount") or 0)
                           for sc in (check.get("appliedServiceCharges") or [])
                           if sc.get("gratuity") and not sc.get("deleted"))
            if server:
                tips[server] += propina
            else:
                tips_sin_mesero += propina
            for sel in (check.get("selections") or []):
                if sel.get("deleted"):
                    continue
                precio = float(sel.get("price") or 0)
                if (sel.get("salesCategory") or {}).get("guid") in SALES_CATEGORIES:
                    if server:
                        ventas[server] += precio
                else:
                    sin_categoria += precio

    return {"tips": dict(tips), "ventas": dict(ventas),
            "sin_categoria": round(sin_categoria, 2),
            "tips_sin_mesero": round(tips_sin_mesero, 2)}


def jobs_del_dia(time_entries: list, jobs: dict) -> dict:
    """{job de Toast: {employee_guid: horas}} para un business date."""
    salida = collections.defaultdict(lambda: collections.defaultdict(float))
    for te in time_entries:
        if te.get("deleted"):
            continue
        guid = (te.get("employeeReference") or {}).get("guid")
        titulo = jobs.get((te.get("jobReference") or {}).get("guid"))
        if not (guid and titulo):
            continue
        horas = (float(te.get("regularHours") or 0)
                 + float(te.get("overtimeHours") or 0))
        if horas > 0:
            salida[titulo][guid] += horas
    return {k: dict(v) for k, v in salida.items()}


# El nombre de la cuenta de sistema por la que entran los pedidos en linea. En
# Toast no es un empleado con job: no tiene jobReferences y nunca ficha, pero en
# Tips Manager si aparece como contribuyente de su propio pool. Hay que
# reconocerla por nombre porque no hay otro dato que la distinga.
ONLINE_ORDERING_JOB = "Online Ordering"


def job_de_cada_quien(jobs_hoy: dict, empleados: dict, jobs: dict,
                      politica: dict) -> tuple:
    """Decide el job de cada persona para efectos del pool.

    Sacarlo solo de los turnos pierde a quien cobro cheques sin fichar, que es
    normal en un encargado: aporta al pool, no recibe nada, y si no aporta todos
    los demas quedan cortos. Por eso el orden es:

      1. el job donde hizo mas horas ese dia, si ficho
      2. si no ficho, el job de su expediente cuando no hay ambiguedad
      3. la cuenta de pedidos en linea, que se reconoce por nombre

    Devuelve (job_de, sin_resolver). Lo que cae en sin_resolver NO aporta, asi
    que se reporta en vez de quedarse callado.
    """
    job_de = {}
    horas_por = collections.defaultdict(lambda: collections.defaultdict(float))
    for titulo, personas in jobs_hoy.items():
        for persona, horas in personas.items():
            horas_por[persona][titulo] += horas
    for persona, titulos in horas_por.items():
        job_de[persona] = max(titulos.items(), key=lambda kv: kv[1])[0]

    aportan = set()
    for pool in politica["pools"]:
        aportan.update(pool["aportan"])

    sin_resolver = {}
    for guid, empleado in empleados.items():
        if guid in job_de:
            continue
        nombre = (f"{empleado.get('firstName') or ''} "
                  f"{empleado.get('lastName') or ''}").strip().lower()
        if "online ordering" in nombre:
            job_de[guid] = ONLINE_ORDERING_JOB
            continue
        titulos = [jobs.get((ref or {}).get("guid"))
                   for ref in (empleado.get("jobReferences") or [])]
        titulos = [t for t in titulos if t]
        candidatos = [t for t in titulos if t in aportan]
        if len(candidatos) == 1:
            job_de[guid] = candidatos[0]
        elif len(titulos) == 1:
            job_de[guid] = titulos[0]
        elif titulos:
            # Varios jobs y ninguno desempata: no se puede adivinar de cual
            # aportaria. Se deja fuera y se dice.
            sin_resolver[guid] = titulos

    return job_de, sin_resolver


def repartir_dia(politica: dict, tips: dict, ventas: dict,
                 jobs_hoy: dict, job_de: dict) -> dict:
    """Aplica los pools de un dia y devuelve {employee_guid: tips despues del pool}.

    Cada contribuyente aporta su parte, el pool se reparte entre los jobs
    receptores segun la politica, y dentro de cada job se divide entre quienes
    estuvieron fichados ese dia a prorrata de sus horas. La parte de un job sin
    nadie presente no se puede asignar y vuelve a los contribuyentes.
    """
    resultado = collections.defaultdict(float, {g: t for g, t in tips.items()})

    for pool in politica["pools"]:
        aportes = {}
        for guid, propina in tips.items():
            job = job_de.get(guid)
            tasa = pool["aportan"].get(job)
            if not tasa:
                continue
            if pool["base"] == "ventas":
                aporte = ventas.get(guid, 0.0) * tasa
                aporte = min(aporte, propina)      # no se aporta mas de lo que se gano
            else:
                aporte = propina * tasa
            if aporte > 0:
                aportes[guid] = aporte

        total = sum(aportes.values())
        if total <= 0:
            continue

        # Que jobs receptores tienen gente fichada hoy
        presentes = {job: horas for job, horas in
                     ((j, jobs_hoy.get(j) or {}) for j in pool["reciben"]) if horas}
        pct_asignable = sum(pool["reciben"][j] for j in presentes)
        if pct_asignable <= 0:
            continue

        # Que pasa con la parte de un job receptor que no tuvo a nadie fichado.
        # No es la misma regla en los dos pools de Gaslamp, y no se puede
        # deducir de la politica: se midio contra el reporte, que es la
        # respuesta conocida. Ver REGLA_NO_ASIGNABLE arriba.
        #   "repartir"  se reasigna entre los jobs presentes, renormalizando
        #   "retener"   se queda con quien aporto
        retiene = pool.get("no_asignable") == "retener"
        factor = pct_asignable if retiene else 1.0

        for guid, aporte in aportes.items():
            resultado[guid] -= aporte * factor

        for job, horas_por_persona in presentes.items():
            parte = total * pool["reciben"][job]
            if not retiene:
                parte /= pct_asignable
            horas_totales = sum(horas_por_persona.values())
            if horas_totales <= 0:
                continue
            for persona, horas in horas_por_persona.items():
                resultado[persona] += parte * (horas / horas_totales)

    return {g: round(v, 4) for g, v in resultado.items()}


def repartir_periodo(client, location: dict, inicio: date, fin: date,
                     jobs: dict, progreso=None) -> dict:
    """Corre el reparto dia por dia y acumula el periodo.

    Devuelve {"por_persona": {guid: $}, "diagnostico": {...}}.
    """
    politica = POLITICAS.get(location["short"])
    if not politica:
        raise RuntimeError(f"sin politica de tip pool para {location['short']}")

    acumulado = collections.defaultdict(float)
    diag = {"tips_brutos": 0.0, "sin_categoria": 0.0, "tips_sin_mesero": 0.0,
            "dias": 0}

    dia = inicio
    while dia <= fin:
        if progreso:
            progreso(dia)
        orders = fetch_orders(client, location["guid"], dia)
        datos = extract_day(orders)
        entries = client._get("/labor/v1/timeEntries", location["guid"],
                              {"businessDate": dia.strftime("%Y%m%d"),
                               "includeMissedBreaks": "true"})
        jobs_hoy = jobs_del_dia(entries, jobs)

        # El job con el que cada persona ficho ese dia. Si ficho en varios, el
        # de mas horas, que es con el que aporta al pool.
        mejor = {}
        for job, personas in jobs_hoy.items():
            for persona, horas in personas.items():
                if persona not in mejor or horas > mejor[persona][1]:
                    mejor[persona] = (job, horas)
        job_de = {persona: job for persona, (job, _) in mejor.items()}

        reparto = repartir_dia(politica, datos["tips"], datos["ventas"],
                               jobs_hoy, job_de)
        for guid, monto in reparto.items():
            acumulado[guid] += monto

        diag["tips_brutos"] += sum(datos["tips"].values()) + datos["tips_sin_mesero"]
        diag["sin_categoria"] += datos["sin_categoria"]
        diag["tips_sin_mesero"] += datos["tips_sin_mesero"]
        diag["dias"] += 1
        dia += timedelta(days=1)

    for clave in ("tips_brutos", "sin_categoria", "tips_sin_mesero"):
        diag[clave] = round(diag[clave], 2)
    return {"por_persona": {g: round(v, 2) for g, v in acumulado.items()},
            "diagnostico": diag}
