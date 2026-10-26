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
    assert [(a.mercado_id, a.indice, a.precio, a.estrategia) for a in nuevas] == [
        ("m1", 0, 0.52, "mantener"), ("m1", 0, 0.52, "vender_justo"), ("m1", 0, 0.52, "vender_antes"),
        ("m1", 0, 0.51, "limite_justo")]
    assert nuevas[0].venta == "" and float(nuevas[1].venta) == round(ops[0].prob_justa, 2)
    assert nuevas[2].venta == nuevas[1].venta
    # taker paga 0.05 x acciones x p x (1 - p); la orden límite no paga y pone su venta hasta llenarse
    assert float(nuevas[0].comision) == pytest.approx(0.05 * 0.48, abs=1e-4)
    assert float(nuevas[3].comision) == 0 and nuevas[3].venta == "" and cu.pendiente_de_compra(nuevas[3])
    # no se repite ni se apuesta con el partido empezado
    assert cu.registrar(ops, nuevas, 0.03, 5, 1.0, antes) == []
    assert cu.registrar(ops, [], 0.03, 5, 1.0, INICIO + timedelta(minutes=1)) == []

    ruta = tmp_path / "valor.csv"
    cu.escribir_apuestas(ruta, nuevas)
    leidas = cu.leer_apuestas(ruta)
    assert leidas[0].precio == 0.52 and leidas[0].indice == 0
    # todavía abierto: no cambia
    assert cu.liquidar(leidas, {"m1": {"closed": False}}) == 0
    assert cu.liquidar(leidas, {"m1": {"closed": True, "outcomePrices": '["1", "0"]'}}) == 4
    assert leidas[0].resultado == "ganada" and leidas[1].estrategia == "vender_justo"
    assert float(leidas[0].ganancia) == pytest.approx(1 / 0.52 - 1 - 0.05 * 0.48, abs=1e-4)
    # la orden límite nunca se llenó: ni gana ni pierde
    assert leidas[3].resultado == "sin llenar" and float(leidas[3].ganancia) == 0


def test_liquidar_perdida_con_comision():
    a = cu.Apuesta("x", "m1", "p", "Ganador", "Celtics", 1, "", 2.45, 0.4, 0.40, 2.0, comision="0.06")
    cu.liquidar([a], {"m1": {"closed": True, "outcomePrices": ["1", "0"]}})
    assert a.resultado == "perdida" and float(a.ganancia) == pytest.approx(-2.06)


def test_comision_taker_deportes():
    # docs de Polymarket: 100 acciones a 0.30 pagan 1.05
    assert cu.comision_taker(30, 0.30, 0.05) == pytest.approx(1.05)
    # $1 a 0.39 paga unos 3¢
    assert cu.comision_taker(1, 0.39, 0.05) == pytest.approx(0.0305)


def test_precio_objetivo():
    assert cu.precio_objetivo(0.172, 0.16) == 0.17
    assert cu.precio_objetivo(0.197, 0.19) == 0.20
    # ventaja menor a medio centavo: al menos 1¢ arriba de la compra
    assert cu.precio_objetivo(0.163, 0.16) == 0.17


def test_revisar_ventas_vende_al_precio_justo():
    ahora = INICIO + timedelta(minutes=30)
    a = cu.Apuesta("x", "m1", "p", "Ganador", "Bucks", 0, "", 5.65, 0.172, 0.16, 1.0,
                   estrategia="vender_justo", venta="0.17")
    b = cu.Apuesta("x", "m1", "p", "Ganador", "76ers", 1, "", 1.17, 0.828, 0.85, 1.0,
                   estrategia="vender_justo", venta="0.86")
    sigue = cu.Apuesta("x", "m1", "p", "Ganador", "Bucks", 0, "", 5.65, 0.172, 0.16, 1.0)
    # nadie paga 0.17 todavía
    assert cu.revisar_ventas([a, sigue], {"m1": libro(0.16, 0.18)}, ahora) == 0
    # alguien paga 0.17: se vende a; b (lado 76ers = 1 - 0.18 = 0.82) no
    assert cu.revisar_ventas([a, b, sigue], {"m1": libro(0.17, 0.18)}, ahora) == 1
    assert a.resultado == "vendida" and float(a.ganancia) == pytest.approx(1 / 0.16 * 0.17 - 1, abs=1e-4)
    assert a.vendida and not sigue.resultado and not b.resultado
    # ya vendida: liquidar no la toca aunque el equipo pierda
    assert cu.liquidar([a], {"m1": {"closed": True, "outcomePrices": ["0", "1"]}}) == 0
    assert a.resultado == "vendida"


def test_csv_viejo_sin_estrategia(tmp_path):
    ruta = tmp_path / "viejo.csv"
    ruta.write_text("momento,mercado_id,partido,titulo,lado,indice,inicio,momio,prob_justa,precio,monto,resultado,ganancia\n"
                    "2026-10-24 09:48:00,m1,Bucks vs. 76ers,Ganador,Bucks,0,,5.65,0.172,0.16,1.0,,\n", encoding="utf-8")
    a, = cu.leer_apuestas(ruta)
    assert a.estrategia == "mantener" and a.venta == ""


def test_csv_viejo_sin_comision_se_corrige(tmp_path):
    ruta = tmp_path / "viejo.csv"
    ruta.write_text("momento,mercado_id,partido,titulo,lado,indice,inicio,momio,prob_justa,precio,monto,resultado,ganancia,"
                    "estrategia,venta,vendida\n"
                    "x,m1,p,Ganador,Cavaliers,0,,1.61,0.602,0.59,1.0,vendida,0.0169,vender_justo,0.60,y\n"
                    "x,m1,p,Ganador,Cavaliers,0,,1.61,0.602,0.59,1.0,,,mantener,,\n", encoding="utf-8")
    vendida, abierta = cu.leer_apuestas(ruta)
    assert cu.poner_comision([vendida, abierta], 0.05) == 2
    # +1.7¢ vendida a precio justo queda en -0.3¢ con la comisión
    assert float(vendida.ganancia) == pytest.approx(0.0169 - 0.05 * 0.41, abs=1e-4)
    assert float(abierta.comision) == pytest.approx(0.0205) and abierta.ganancia == ""
    assert cu.poner_comision([vendida, abierta], 0.05) == 0  # solo una vez


def test_precio_limite():
    assert cu.precio_limite(0.38, 0.39) == 0.38  # spread de 1¢: se forma en la mejor compra
    assert cu.precio_limite(0.38, 0.42) == 0.39  # hay hueco: 1¢ arriba para ser el primero


def test_limite_justo_compra_vende_o_se_cancela():
    antes, durante = INICIO - timedelta(minutes=30), INICIO + timedelta(minutes=1)
    def orden():
        return cu.Apuesta("x", "m1", "p", "Ganador", "Bucks", 0, INICIO.isoformat(), 5.65, 0.172, 0.15, 1.0,
                          estrategia="limite_justo", comision="0.0000")
    a, b, c = orden(), orden(), orden()
    # nadie vende a 0.15 y la fila de compra sigue en 0.15: no se llena
    assert cu.revisar_compras([a], {"m1": libro(0.15, 0.16)}, antes) == 0 and cu.pendiente_de_compra(a)
    # la venta de nadie se puede llenar antes de comprar
    assert cu.revisar_ventas([a], {"m1": libro(0.18, 0.19)}, antes) == 0
    # alguien vende a 0.15: se llena y pone la venta al precio justo
    assert cu.revisar_compras([a], {"m1": libro(0.14, 0.15)}, antes) == 1
    assert a.comprada and a.venta == "0.17" and not cu.pendiente_de_compra(a)
    # la mejor compra bajó de 0.15: se acabó la fila, también se llena
    assert cu.revisar_compras([b], {"m1": libro(0.14, 0.16)}, antes) == 1 and b.comprada
    # se vende sin comisión de compra
    assert cu.revisar_ventas([a], {"m1": libro(0.17, 0.18)}, durante) == 1
    assert float(a.ganancia) == pytest.approx(1 / 0.15 * 0.17 - 1, abs=1e-4)
    # al empezar el partido sin llenarse se cancela
    assert cu.revisar_compras([c], {"m1": libro(0.15, 0.16)}, durante) == 1
    assert c.resultado == "sin llenar" and float(c.ganancia) == 0


def test_vender_antes_solo_antes_del_inicio():
    antes, durante = INICIO - timedelta(minutes=30), INICIO + timedelta(minutes=30)
    def apuesta(estrategia):
        return cu.Apuesta("x", "m1", "p", "Ganador", "Bucks", 0, INICIO.isoformat(), 5.65, 0.172, 0.16, 1.0,
                          estrategia=estrategia, venta="0.17")
    pre, justo, vivo = apuesta("vender_antes"), apuesta("vender_justo"), apuesta("vender_antes")
    # antes del partido vende igual que vender_justo
    assert cu.revisar_ventas([pre], {"m1": libro(0.17, 0.18)}, antes) == 1 and pre.resultado == "vendida"
    # durante el partido su venta ya se canceló; vender_justo sí vende
    assert cu.revisar_ventas([justo, vivo], {"m1": libro(0.17, 0.18)}, durante) == 1
    assert justo.resultado == "vendida" and not vivo.resultado
    # se queda hasta el final como mantener
    assert cu.liquidar([vivo], {"m1": {"closed": True, "outcomePrices": ["1", "0"]}}) == 1
    assert vivo.resultado == "ganada"
