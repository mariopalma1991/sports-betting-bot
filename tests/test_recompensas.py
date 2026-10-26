from datetime import datetime, timedelta, timezone

from types import SimpleNamespace

import pytest

import recompensas as rc

AHORA = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)
PREMIO = rc.Premio("c1", bolsa_diaria=50, min_acciones=100, max_distancia=3)


def libro(compras, ventas, tick="0.01"):
    return {"bids": [{"price": str(p), "size": str(c)} for p, c in compras],
            "asks": [{"price": str(p), "size": str(c)} for p, c in ventas], "tick_size": tick}


def test_puntos_por_cercania():
    assert rc.puntos_por_cercania(0, 3) == 1
    assert rc.puntos_por_cercania(1, 3) == pytest.approx(4 / 9)
    assert rc.puntos_por_cercania(2, 3) == pytest.approx(1 / 9)
    assert rc.puntos_por_cercania(3, 3) == 0
    assert rc.puntos_por_cercania(1, 0) == 0


def test_puntos_combinados_un_lado_divide_entre_3():
    assert rc.puntos_combinados(90, 0, 0.5) == pytest.approx(30)
    assert rc.puntos_combinados(90, 90, 0.5) == pytest.approx(90)
    # Fuera de 0.10-0.90 un solo lado no cuenta.
    assert rc.puntos_combinados(90, 0, 0.95) == 0
    assert rc.puntos_combinados(90, 60, 0.95) == 60


def test_bolsa_activa_respeta_fechas():
    item = {"rewards_config": [
        {"start_date": "2026-10-01", "end_date": "2026-10-26", "rate_per_day": 30},  # vence hoy: cuenta
        {"start_date": "2026-10-27", "end_date": "2026-11-30", "rate_per_day": 99},  # aún no empieza
        {"start_date": "2026-09-01", "end_date": "2026-09-30", "rate_per_day": 77},  # ya terminó
        {"start_date": "2026-10-01", "end_date": None, "rate_per_day": 20},
    ]}
    assert rc.bolsa_activa(item, AHORA) == 50
    assert rc.premio_de_item({"condition_id": "x", "rewards_config": []}, AHORA) is None


def test_partido_de_mercado_solo_deportes_abiertos():
    base = {"conditionId": "c1", "clobTokenIds": '["t1", "t2"]', "outcomes": '["Lakers", "Suns"]',
            "events": [{"title": "Lakers vs. Suns"}], "sportsMarketType": "moneyline"}
    p = rc.partido_de_mercado({**base, "gameStartTime": "2026-10-26 23:00:00+00"}, AHORA, set())
    assert p.partido == "Lakers vs. Suns" and p.token_id == "t1" and p.mercado == "moneyline"
    assert rc.partido_de_mercado({**base, "gameStartTime": "2026-10-26 11:00:00+00"}, AHORA, set()) is None  # ya empezó
    assert rc.partido_de_mercado(base, AHORA, set()).inicio is None  # sin hora: mercado de temporada


def test_es_deporte_por_etiqueta_no_por_hora():
    posts = {"conditionId": "c2", "clobTokenIds": '["t1"]', "gameStartTime": "2026-10-26 23:00:00+00",
             "events": [{"title": "Elon Musk # tweets", "tags": [{"id": "999", "slug": "elon"}]}]}
    p = rc.partido_de_mercado(posts, AHORA, {"82"})                      # no es deporte, pero tampoco financiero
    assert p is not None and p.categoria == "elon"
    ascenso = {"conditionId": "c3", "clobTokenIds": '["t1"]', "endDate": "2027-05-30T00:00:00Z",
               "events": [{"title": "EFL Championship", "tags": [{"id": "82", "slug": "efl"}]}]}
    p = rc.partido_de_mercado(ascenso, AHORA, {"82"})
    assert p is not None and p.cierre == datetime(2027, 5, 30, tzinfo=timezone.utc)


def test_motivo_descarte():
    def fila(**cambios):
        f = rc.analizar([rc.Partido("c1", "West Ham", "Temporada", ["Yes", "No"], "t1", None)],
                        {"c1": rc.Premio("c1", 2, 50, 4)},
                        {"t1": libro([(0.76, 10)], [(0.78, 10)])}, [25], 1, AHORA)[0]
        for k, v in cambios.items():
            setattr(f, k, v)
        return f
    assert rc.motivo_descarte(fila()) == ""
    assert fila().costo_minimo == pytest.approx(50 * 0.23)
    assert rc.motivo_descarte(fila(spread=5.0)).startswith("spread")
    assert rc.motivo_descarte(fila(medio=0.9)) == "precio cerca de 0 o 1"
    assert rc.motivo_descarte(fila(medio=None)) == "libro de un solo lado"
    assert rc.motivo_descarte(fila(inicio=AHORA, horas=0.5)) == "empieza pronto"
    assert rc.motivo_descarte(fila(simulaciones=[None])) == "mínimo muy caro"
    assert rc.motivo_descarte(fila(), min_rendimiento=1.0).startswith("rinde poco")
    assert rc.motivo_descarte(fila(), min_rendimiento=0.001, min_ganancia_dia=5) == "bolsa muy chica"
    assert rc.motivo_descarte(fila(historia=[0.76, 0.78, 0.77])) == ""                     # se movió 2¢
    assert rc.motivo_descarte(fila(historia=[0.10, 0.35, 0.14])).startswith("precio inestable (se movió 25¢")


def test_competencia_cuenta_solo_dentro_de_la_distancia():
    comp = rc.competencia(libro([(0.49, 200), (0.45, 1000)], [(0.51, 100)]), PREMIO)
    assert comp.medio == pytest.approx(0.50)
    assert comp.puntos_compra == pytest.approx(200 * 4 / 9)   # 0.45 está a 5¢: no cuenta
    assert comp.puntos_venta == pytest.approx(100 * 4 / 9)
    assert comp.total == pytest.approx(150 * 4 / 9)
    assert rc.competencia(libro([], [(0.51, 100)]), PREMIO) is None


def test_simular_dos_lados_cuando_alcanza():
    comp = rc.competencia(libro([(0.49, 200)], [(0.51, 200)]), PREMIO)  # rivales: 200·4/9 = 88.9 pts
    sim = rc.simular(comp, PREMIO, capital=100, distancia=1, horas=6)
    # Compra a 0.49 y venta a 0.51: cada par cuesta 0.49 + 0.49 = 0.98 → 102 acciones por lado.
    assert sim.lados == 2
    assert sim.acciones == pytest.approx(100 / 0.98)
    assert sim.parte == pytest.approx(sim.puntos / (200 * 4 / 9 + sim.puntos))
    assert sim.ganancia == pytest.approx(sim.parte * 50 * 6 / 24)


def test_simular_un_lado_y_no_alcanza():
    comp = rc.competencia(libro([(0.49, 200)], [(0.51, 200)]), PREMIO)
    sim = rc.simular(comp, PREMIO, capital=50, distancia=1, horas=30)
    assert sim.lados == 1                                  # 2 lados darían 51 acciones < 100
    assert sim.puntos == pytest.approx(50 / 0.49 * 4 / 9 / 3)
    assert sim.ganancia == pytest.approx(sim.parte * 50)  # más de 24 h: cuenta un día
    assert rc.simular(comp, PREMIO, capital=25, distancia=1, horas=6) is None


def test_simular_no_cruza_el_libro():
    comp = rc.competencia(libro([(0.50, 100)], [(0.51, 100)]), PREMIO)  # medio 0.505
    sim = rc.simular(comp, PREMIO, capital=200, distancia=0, horas=1)
    assert sim is not None  # con distancia 0 la compra queda en 0.50 y la venta en 0.51


def test_analizar_ordena_por_rendimiento():
    partidos = [rc.Partido("c1", "A vs B", "Ganador", ["A", "B"], "t1", AHORA + timedelta(hours=6)),
                rc.Partido("c2", "C vs D", "Ganador", ["C", "D"], "t2", AHORA + timedelta(hours=6)),
                rc.Partido("c3", "Sin premio", "Ganador", ["E", "F"], "t3", AHORA + timedelta(hours=6))]
    premios = {"c1": PREMIO, "c2": rc.Premio("c2", 500, 100, 3)}
    libros = {"t1": libro([(0.49, 200)], [(0.51, 200)]), "t2": libro([(0.49, 200)], [(0.51, 200)])}
    filas = rc.analizar(partidos, premios, libros, [50], 1, AHORA)
    assert [f.partido for f in filas] == ["C vs D", "A vs B"]


def test_guardar_csv_agrega(tmp_path):
    partidos, premios, libros = rc.datos_demo(AHORA)
    filas = rc.analizar(partidos, premios, libros, [25, 50], 1, AHORA)
    ruta = tmp_path / "r.csv"
    rc.guardar_csv(ruta, filas, AHORA)
    rc.guardar_csv(ruta, filas, AHORA)
    lineas = ruta.read_text(encoding="utf-8").splitlines()
    assert len(lineas) == 1 + 2 * 2 * len(filas)


def test_obtener_historias_por_bloques_de_20():
    pedidos = []

    class Sesion:
        def post(self, url, json=None, timeout=None):
            pedidos.append((url, json))
            datos = {"history": {t: [{"t": 2, "p": 0.2}, {"t": 1, "p": 0.1}] for t in json["markets"]}}
            return SimpleNamespace(json=lambda: datos, raise_for_status=lambda: None)

    tokens = [f"t{i}" for i in range(25)]
    historias = rc.obtener_historias(Sesion(), tokens, AHORA)
    assert [len(j["markets"]) for _, j in pedidos] == [20, 5]
    assert pedidos[0][0].endswith("/batch-prices-history") and pedidos[0][1]["fidelity"] == 60
    assert pedidos[0][1]["start_ts"] == int((AHORA - timedelta(days=7)).timestamp())
    assert historias["t3"] == [0.1, 0.2]                                        # ordenado por tiempo


def test_bloquea_mercados_financieros():
    base = {"conditionId": "c9", "clobTokenIds": '["t1"]', "question": "Will the Lakers win the 2027 NBA Finals?"}
    assert rc.motivo_financiero(base) == ""
    assert "Crypto" in rc.motivo_financiero({**base, "events": [{"tags": [{"slug": "crypto", "label": "Crypto"}]}]})
    assert "Bitcoin" in rc.motivo_financiero({**base, "question": "Will Bitcoin reach $150k in 2026?"})
    assert "Fed" in rc.motivo_financiero({**base, "question": "Will the Fed cut rates in December?"})
    assert rc.motivo_financiero({**base, "question": "Will Oppenheimer win Best Picture?"}) == ""
    assert rc.partido_de_mercado({**base, "question": "S&P 500 above 7000 on Friday?"}, AHORA, set()) is None
    cultura = rc.partido_de_mercado({**base, "question": "Will Wicked win Best Picture?",
                                     "events": [{"title": "Oscars 2027", "tags": [{"slug": "culture", "label": "Culture"}]}]},
                                    AHORA, set())
    assert cultura.categoria == "Culture" and cultura.inicio is None
