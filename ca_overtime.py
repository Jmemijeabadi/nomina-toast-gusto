"""Overtime y double time de California calculados desde los punches.

Toast devuelve `overtimeHours` pero lo calcula POR RESTAURANTE y no devuelve
`doubleOvertimeHours` en absoluto, asi que repetir sus numeros deja sin pagar el
2x y parte el overtime de quien trabaja en las dos locations. Este modulo lo
calcula de cero, sobre las horas por persona y por jornada.

Labor Code 510(a) y 511, para empleados no exentos:

  Por jornada   mas de 8 h        -> 1.5x
                mas de 12 h       -> 2x
  Por semana    mas de 40 h de tiempo recto -> 1.5x
  Septimo dia   todo el 7mo dia consecutivo de la semana laboral:
                las primeras 8 h  -> 1.5x
                arriba de 8 h     -> 2x

No se piramida: una hora que ya se pago a 1.5x por la regla diaria no se vuelve
a contar para el umbral de las 40 semanales.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta

# Inicio de la semana laboral, en el formato de date.weekday():
# 0=lunes ... 6=domingo. El default de la industria en EE. UU. es domingo.
#
# OJO: el workweek es un periodo fijo y recurrente de 168 h que fija el patron
# (29 CFR 778.105). No se deduce del periodo de nomina ni del calendario, y si
# este valor no coincide con el de Gusto, el Regular Rate of Pay sale mal en las
# dos semanas. Hay que LEERLO en Gusto > Pay > Pay settings y ponerlo aca.
# Sabado. Confirmado leyendo los pay periods de Gusto por MCP el 2026-10-01:
# todos arrancan sabado y cierran viernes (2026-07-25 sab -> 2026-08-07 vie,
# 2026-09-19 sab -> 2026-10-02 vie), asi que el periodo de 14 dias son dos
# semanas laborales sabado-viernes completas, sin semanas partidas.
WORKWEEK_START_WEEKDAY = 5
WORKWEEK_START_CONFIRMED = True

DAILY_OT_THRESHOLD = 8.0
DAILY_DT_THRESHOLD = 12.0
WEEKLY_OT_THRESHOLD = 40.0
SEVENTH_DAY_OT_HOURS = 8.0


def parse_business_date(value: str | date) -> date:
    """Toast entrega businessDate como 'YYYYMMDD'."""
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value), "%Y%m%d").date()


def workweek_start(day: date, start_weekday: int = WORKWEEK_START_WEEKDAY) -> date:
    """Primer dia de la semana laboral que contiene a `day`."""
    delta = (day.weekday() - start_weekday) % 7
    return day - timedelta(days=delta)


def split_daily(hours: float) -> tuple:
    """Reparte las horas de UNA jornada en (recto, 1.5x, 2x) por la regla diaria."""
    straight = min(hours, DAILY_OT_THRESHOLD)
    overtime = max(0.0, min(hours, DAILY_DT_THRESHOLD) - DAILY_OT_THRESHOLD)
    double = max(0.0, hours - DAILY_DT_THRESHOLD)
    return straight, overtime, double


def split_seventh_day(hours: float) -> tuple:
    """El 7mo dia consecutivo no tiene tiempo recto: 8 h a 1.5x y el resto a 2x."""
    overtime = min(hours, SEVENTH_DAY_OT_HOURS)
    double = max(0.0, hours - SEVENTH_DAY_OT_HOURS)
    return 0.0, overtime, double


def is_seventh_consecutive_day(day: date, worked_days: set) -> bool:
    """True si `day` es el 7mo dia trabajado de una racha de 7 en su semana laboral.

    La racha se cuenta DENTRO de la semana laboral: el derecho del 7mo dia nace
    de trabajar los siete dias de una misma semana laboral, no de siete dias
    consecutivos cualesquiera a caballo de dos semanas.
    """
    week = workweek_start(day)
    week_days = {week + timedelta(days=i) for i in range(7)}
    return week_days.issubset(worked_days) and day == week + timedelta(days=6)


def compute_overtime(person_days: dict, start_weekday: int = WORKWEEK_START_WEEKDAY) -> dict:
    """Calcula el reparto de horas por persona y por jornada.

    person_days: {identidad: {business_date (date|str): horas_trabajadas}}

    Devuelve {identidad: {
        "days":  {business_date: {"regular","overtime","double","rule"}},
        "weeks": {inicio_semana: {"regular","overtime","double","worked_days"}},
        "totals": {"regular","overtime","double"},
    }}
    """
    result = {}

    for identity, days_raw in person_days.items():
        days = {parse_business_date(d): float(h or 0.0) for d, h in days_raw.items()}
        worked = {d for d, h in days.items() if h > 0}

        by_week = defaultdict(list)
        for day in sorted(days):
            by_week[workweek_start(day, start_weekday)].append(day)

        per_day = {}
        per_week = {}

        for week, week_days in sorted(by_week.items()):
            # Paso 1: regla diaria y septimo dia.
            daily = {}
            for day in week_days:
                hours = days[day]
                if hours <= 0:
                    daily[day] = (0.0, 0.0, 0.0, "sin horas")
                    continue
                if is_seventh_consecutive_day(day, worked):
                    straight, overtime, double = split_seventh_day(hours)
                    rule = "7mo dia consecutivo"
                else:
                    straight, overtime, double = split_daily(hours)
                    rule = ("diaria >12h" if double else
                            "diaria >8h" if overtime else "recto")
                daily[day] = (straight, overtime, double, rule)

            # Paso 2: las 40 semanales se miden SOLO sobre el tiempo recto, para
            # no piramidar sobre horas que la regla diaria ya pago a 1.5x.
            straight_total = sum(v[0] for v in daily.values())
            weekly_excess = max(0.0, straight_total - WEEKLY_OT_THRESHOLD)

            # El excedente se convierte a 1.5x empezando por los dias mas
            # recientes, que es la convencion que no altera el total de horas.
            remaining = weekly_excess
            for day in sorted(daily, reverse=True):
                if remaining <= 0:
                    break
                straight, overtime, double, rule = daily[day]
                moved = min(straight, remaining)
                if moved > 0:
                    daily[day] = (straight - moved, overtime + moved, double,
                                  rule + " + 40h semanales")
                    remaining -= moved

            for day, (straight, overtime, double, rule) in daily.items():
                per_day[day] = {
                    "regular": round(straight, 4),
                    "overtime": round(overtime, 4),
                    "double": round(double, 4),
                    "rule": rule,
                }

            per_week[week] = {
                "regular": round(sum(daily[d][0] for d in daily), 4),
                "overtime": round(sum(daily[d][1] for d in daily), 4),
                "double": round(sum(daily[d][2] for d in daily), 4),
                "worked_days": sum(1 for d in week_days if days[d] > 0),
            }

        result[identity] = {
            "days": per_day,
            "weeks": per_week,
            "totals": {
                "regular": round(sum(v["regular"] for v in per_day.values()), 2),
                "overtime": round(sum(v["overtime"] for v in per_day.values()), 2),
                "double": round(sum(v["double"] for v in per_day.values()), 2),
            },
        }

    return result
