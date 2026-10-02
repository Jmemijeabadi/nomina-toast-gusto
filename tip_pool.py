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

NADA de esto se usa para pagar hasta que el reparto calculado cuadre al centavo
contra el reporte EmployeeTipTotals en varios periodos. Ese reporte es la
respuesta correcta conocida; este modulo es la hipotesis.
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

# Politicas leidas de Toast Web > Tips Manager. Los jobs son los de TOAST.
POLITICAS = {
    "National City": {
        "intervalo": "dia",
        "pools": [
            {
                "nombre": "Pool 1",
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
                "aportan": {"Online Ordering": 1.00},
                "base": "tips",
                "reciben": {
                    "Cashier": 0.27, "Busser": 0.07, "Tortilla station": 0.10,
                    "Dishwasher": 0.07, "Prep": 0.09, "Taquero": 0.30, "Lead": 0.10,
                },
            },
            {
                "nombre": "Pool 1",
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
            propina = sum(float(p.get("tipAmount") or 0)
                          for p in (check.get("payments") or [])
                          if not p.get("deleted"))
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

        for guid, aporte in aportes.items():
            resultado[guid] -= aporte

        for job, horas_por_persona in presentes.items():
            # La parte no asignable se queda con los contribuyentes: se reparte
            # solo el porcentaje que si tiene destino, renormalizado.
            parte = total * (pool["reciben"][job] / pct_asignable) if pct_asignable else 0.0
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
