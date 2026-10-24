import json
from datetime import datetime, timedelta, timezone

import pytest

import cuotas as cu

INICIO = datetime(2026, 10, 25, 23, 30, tzinfo=timezone.utc)

EVENTO_ODDS = {
    "home_team": "Boston Celtics", "away_team": "Los Angeles Lakers",
    "commence_time": "2026-10-25T23:30:00Z",
    "bookmakers": [{"key": "pinnacle", "markets": [
        {"key": "h2h", "outcomes": [{"name": "Los Angeles Lakers", "price": 1.62},
                                    {"name": "Boston Celtics", "price": 2.45}]},
        {"key": "spreads", "outcomes": [{"name": "Los Angeles Lakers", "price": 1.95, "point": -4.5},
                                        {"name": "Boston Celtics", "price": 1.95, "point": 4.5}]},
        {"key": "totals", "outcomes": [{"name": "Over", "price": 1.90, "point": 224.5},
                                       {"name": "Under", "price": 2.00, "point": 224.5}]},
    ]}],
}

EVENTO_PM = {
    "title": "Lakers vs. Celtics", "slug": "nba-lal-bos-2026-10-25",
    "markets": [
        {"id": "m1", "sportsMarketType": "moneyline", "outcomes": '["Lakers", "Celtics"]',
         "clobTokenIds": '["t1", "t1b"]', "gameStartTime": "2026-10-25 23:30:00+00"},
        {"id": "m2", "groupItemTitle": "Spread -4.5", "outcomes": '["Lakers", "Celtics"]',
         "clobTokenIds": '["t2", "t2b"]', "gameStartTime": "2026-10-25 23:30:00+00"},
        {"id": "m3", "groupItemTitle": "O/U 224.5", "outcomes": '["Over", "Under"]',
         "clobTokenIds": '["t3", "t3b"]', "gameStartTime": "2026-10-25 23:30:00+00"},
        {"id": "m4", "groupItemTitle": "John Collins: Points O/U 4.5", "outcomes": '["Yes", "No"]',
         "clobTokenIds": '["t4", "t4b"]'},
    ],
}


def libro(bid, ask, cantidad=100):
    return {"bids": [{"price": str(bid), "size": str(cantidad)}], "asks": [{"price": str(ask), "size": str(cantidad)}]}


def test_sin_margen():
    lakers, celtics = cu.sin_margen([1.62, 2.45])
    assert round(lakers + celtics, 9) == 1
    assert round(lakers, 3) == 0.602


def test_mismo_equipo():
    assert cu.mismo_equipo("Lakers", "Los Angeles Lakers")
    assert cu.mismo_equipo("76ers", "Philadelphia 76ers")
    assert cu.mismo_equipo("Trail Blazers", "Portland Trail Blazers")
    assert not cu.mismo_equipo("Kings", "Sacramento Kingsmen")
    assert not cu.mismo_equipo("Heat", "Miami Heater")


def test_mercados_pm_descarta_props():
    mercados = cu.mercados_pm_de_evento(EVENTO_PM)
    assert [(m.id, m.tipo, m.punto) for m in mercados] == [("m1", "h2h", None), ("m2", "spreads", -4.5),
                                                            ("m3", "totals", 224.5)]


def test_comparar_los_tres_mercados():
    mercados = cu.mercados_pm_de_evento(EVENTO_PM)
    libros = {"t1": libro(0.50, 0.52), "t2": libro(0.47, 0.49), "t3": libro(0.50, 0.53)}
    ops = {(o.mercado_id, o.indice): o for o in cu.comparar(mercados, [EVENTO_ODDS], libros, "pinnacle")}
    assert len(ops) == 6
    lakers = ops[("m1", 0)]
    assert lakers.precio == 0.52 and round(lakers.ventaja, 3) == 0.082
    celtics = ops[("m1", 1)]
    assert celtics.precio == 0.50 and round(celtics.ventaja, 3) == round(0.398 - 0.50, 3)
    assert ops[("m2", 0)].lado == "Lakers -4.5" and ops[("m2", 1)].lado == "Celtics +4.5"
    assert round(ops[("m2", 0)].prob_justa, 3) == 0.5
    under = ops[("m3", 1)]
    assert under.lado == "Under 224.5" and under.precio == 0.50
    # la mejor ventaja va primero
    assert cu.comparar(mercados, [EVENTO_ODDS], libros, "pinnacle")[0].mercado_id == "m1"


def test_no_empareja_otra_linea_ni_otro_dia():
    otro = json.loads(json.dumps(EVENTO_PM))
    otro["markets"][1]["groupItemTitle"] = "Spread -6.5"
    mercados = cu.mercados_pm_de_evento(otro)
    ops = cu.comparar(mercados, [EVENTO_ODDS], {"t2": libro(0.4, 0.45)}, "pinnacle")
    assert not [o for o in ops if o.mercado_id == "m2"]
    lejos = dict(EVENTO_ODDS, commence_time="2026-10-28T23:30:00Z")
    assert cu.evento_de_partido("Lakers vs. Celtics", INICIO, [lejos]) is None


def test_registrar_y_liquidar(tmp_path):
    mercados = cu.mercados_pm_de_evento(EVENTO_PM)
    ops = cu.comparar(mercados, [EVENTO_ODDS], {"t1": libro(0.50, 0.52)}, "pinnacle")
    antes = INICIO - timedelta(hours=1)
    nuevas = cu.registrar(ops, [], 0.03, 5, 1.0, antes)
    assert [(a.mercado_id, a.indice, a.precio) for a in nuevas] == [("m1", 0, 0.52)]
    # no se repite ni se apuesta con el partido empezado
    assert cu.registrar(ops, nuevas, 0.03, 5, 1.0, antes) == []
    assert cu.registrar(ops, [], 0.03, 5, 1.0, INICIO + timedelta(minutes=1)) == []

    ruta = tmp_path / "valor.csv"
    cu.escribir_apuestas(ruta, nuevas)
    leidas = cu.leer_apuestas(ruta)
    assert leidas[0].precio == 0.52 and leidas[0].indice == 0
    # todavía abierto: no cambia
    assert cu.liquidar(leidas, {"m1": {"closed": False}}) == 0
    assert cu.liquidar(leidas, {"m1": {"closed": True, "outcomePrices": '["1", "0"]'}}) == 1
    assert leidas[0].resultado == "ganada"
    assert float(leidas[0].ganancia) == pytest.approx(1 / 0.52 - 1, abs=1e-4)


def test_liquidar_perdida_con_comision():
    a = cu.Apuesta("x", "m1", "p", "Ganador", "Celtics", 1, "", 2.45, 0.4, 0.40, 2.0)
    cu.liquidar([a], {"m1": {"closed": True, "outcomePrices": ["1", "0"]}}, comision=0.01)
    assert a.resultado == "perdida" and float(a.ganancia) == pytest.approx(-2.02)
