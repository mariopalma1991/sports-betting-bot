import json
import threading
from datetime import datetime, timedelta, timezone
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer

import pytest

import recompensas_ui as ui

AHORA = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)


def test_precio_sugerido():
    assert ui.precio_sugerido(0.21, 0.23, 0.01) == pytest.approx(0.21)    # medio-1¢ = 0.21, igual al mejor comprador
    assert ui.precio_sugerido(0.10, 0.20, 0.01) == pytest.approx(0.14)    # medio 0.15 → 0.14
    assert ui.precio_sugerido(0.500, 0.501, 0.001) == pytest.approx(0.5)  # nunca cruza el libro
    assert ui.precio_sugerido(None, 0.5, 0.01) is None


def test_alerta_de_inicio():
    assert ui.alerta_de_inicio(None, AHORA) == ""
    assert ui.alerta_de_inicio(AHORA + timedelta(hours=3), AHORA) == ""
    assert "30 min" in ui.alerta_de_inicio(AHORA + timedelta(minutes=30), AHORA)
    assert "ya empezó" in ui.alerta_de_inicio(AHORA - timedelta(minutes=1), AHORA)


@pytest.fixture
def tablero():
    t = ui.Tablero(demo=True, capital=25, env={})
    t.escanear()
    return t


def test_demo_poner_ver_y_cancelar(tablero):
    candidatos = [f for f in tablero.foto_candidatos()["filas"] if not f["motivo"]]
    assert [f["condition_id"] for f in candidatos] == ["f1"]
    ficha = tablero.ficha("f1")
    no = next(l for l in ficha["lados"] if l["resultado"] == "No")
    assert no["medio"] == pytest.approx(0.23) and no["sugerido"] == pytest.approx(0.22)
    # Bloqueada: pasa el tope de 15 USD.
    r = tablero.poner({"condition_id": "f1", "lado": "No", "precio": 0.22, "acciones": 100})
    assert not r["ok"] and r["revision"]["bloqueos"]
    assert tablero.foto_cuenta()["ordenes"] == []
    r = tablero.poner({"condition_id": "f1", "lado": "No", "precio": 0.22, "acciones": 50})
    assert r["ok"]
    ordenes = tablero.foto_cuenta()["ordenes"]
    assert len(ordenes) == 1 and ordenes[0]["cuenta"] and ordenes[0]["costo"] == pytest.approx(11)
    assert tablero.cancelar([ordenes[0]["id"]])["canceladas"] == [ordenes[0]["id"]]
    assert tablero.foto_cuenta()["ordenes"] == []


def test_servidor_pide_ficha_y_host_local(tablero):
    servidor = ThreadingHTTPServer(("127.0.0.1", 0), ui.manejador(tablero, "secreta"))
    threading.Thread(target=servidor.serve_forever, daemon=True).start()
    puerto = servidor.server_address[1]

    def pedir(metodo, ruta, cabeceras=None, cuerpo=None):
        c = HTTPConnection("127.0.0.1", puerto, timeout=5)
        c.request(metodo, ruta, body=cuerpo, headers=cabeceras or {})
        r = c.getresponse()
        return r.status, r.read()

    try:
        estado, html = pedir("GET", "/")
        assert estado == 200 and b"secreta" in html
        assert pedir("GET", "/", {"Host": "malo.com"})[0] == 403
        assert pedir("POST", "/api/cancelar-todo", cuerpo=b"{}")[0] == 403
        assert pedir("POST", "/api/cancelar-todo", {"X-Ficha": "otra"}, b"{}")[0] == 403
        estado, cuerpo = pedir("POST", "/api/ficha", {"X-Ficha": "secreta", "Content-Type": "application/json"},
                               json.dumps({"mercado": "f1"}).encode())
        assert estado == 200 and json.loads(cuerpo)["condition_id"] == "f1"
    finally:
        servidor.shutdown()


def test_repartir_estimados_por_mercado():
    ordenes = [
        {"condition_id": "a", "cuenta": True, "acciones": 50, "llenadas": 0},
        {"condition_id": "a", "cuenta": True, "acciones": 150, "llenadas": 50},
        {"condition_id": "b", "cuenta": False, "acciones": 10, "llenadas": 0},
        {"condition_id": "c", "cuenta": True, "acciones": 10, "llenadas": 0},   # sin dato de bolsa
    ]
    total = ui.repartir_estimados(ordenes, {"a": 12.5, "b": 50.0, "c": 10.0}, {"a": 2.0, "b": 10.0, "c": None})
    assert total == pytest.approx(0.25)                         # 12.5% de $2
    assert ordenes[0]["estimado_dia"] == pytest.approx(0.25 * 50 / 150)
    assert ordenes[1]["estimado_dia"] == pytest.approx(0.25 * 100 / 150)
    assert ordenes[2]["estimado_dia"] is None and ordenes[3]["estimado_dia"] is None


def test_bolsa_activa_de_configs():
    from types import SimpleNamespace as N
    configs = [N(rate_per_day=2, start_date=AHORA - timedelta(days=1), end_date=None),
               N(rate_per_day=5, start_date=AHORA + timedelta(days=1), end_date=None),     # aún no empieza
               N(rate_per_day=3, start_date=None, end_date=AHORA - timedelta(hours=1))]    # ya terminó
    assert ui.bolsa_activa_de_configs(configs, AHORA) == pytest.approx(2)


def test_vencimiento_desde_texto():
    assert ui.vencimiento_desde_texto(None, AHORA) is None
    assert ui.vencimiento_desde_texto("2026-10-26T13:00:00Z", AHORA) == int((AHORA + timedelta(hours=1)).timestamp())
    with pytest.raises(ValueError):
        ui.vencimiento_desde_texto("2026-10-26T12:02:00Z", AHORA)   # menos de 4 minutos
    with pytest.raises(ValueError):
        ui.vencimiento_desde_texto("2026-10-26T13:00:00", AHORA)    # sin zona horaria


def test_proximo_partido_del_equipo():
    assert ui.equipo_de_pregunta("Will West Ham United be promoted to Premier League?") == "West Ham United"
    assert ui.equipo_de_pregunta("Will the Lakers win the NBA Finals?") == "Lakers"
    assert ui.equipo_de_pregunta("¿Quién gana?") is None
    eventos = [
        {"title": "West Ham vs. Leeds", "startTime": "2026-10-28T19:00:00Z"},
        {"title": "Leeds vs. West Ham United", "startTime": "2026-10-27T15:00:00Z"},
        {"title": "West Ham vs. Hull", "startTime": "2026-10-25T15:00:00Z"},              # ya pasó
        {"title": "Burnley vs. Hull", "startTime": "2026-10-26T20:00:00Z"},               # otro equipo
        {"title": "West Ham to finish top 2", "markets": [{"gameStartTime": "2026-10-26 20:00:00+00"}]},  # no es partido
    ]
    inicio, titulo = ui.proximo_partido(eventos, "West Ham United", AHORA)
    assert titulo == "Leeds vs. West Ham United"
    rec = ui.recomendacion_cancelar(inicio, titulo, AHORA)
    assert rec["fecha"] == "2026-10-27T14:00:00+00:00" and not rec["ya"]
    assert ui.recomendacion_cancelar(None, None, AHORA)["fecha"] is None


def test_demo_estimado_cartera_y_gtd(tablero):
    vence = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    assert tablero.poner({"condition_id": "f1", "lado": "No", "precio": 0.22, "acciones": 50, "vence": vence})["ok"]
    cuenta = tablero.foto_cuenta()
    orden = cuenta["ordenes"][0]
    assert orden["vence"] is not None and orden["bolsa"] == pytest.approx(2)
    assert orden["estimado_dia"] == pytest.approx(0.42 * 2)       # el demo da 42% de parte
    assert orden["cierre"] is not None                             # fecha en que cierra el mercado
    assert cuenta["estimado"]["dia"] == pytest.approx(0.84)
    assert cuenta["cartera"]["efectivo"] == pytest.approx(27.76 - 11)
    assert len(cuenta["cartera"]["posiciones"]) == 1
    assert len(cuenta["historial"]) == ui.DIAS_HISTORIAL


def test_demo_dice_por_que_no_cuenta(tablero):
    assert tablero.poner({"condition_id": "f1", "lado": "No", "precio": 0.22, "acciones": 20})["ok"]
    orden = tablero.foto_cuenta()["ordenes"][0]
    assert not orden["cuenta"]
    assert "al menos 50 acciones" in orden["motivo"] and "pon 50 acciones ($11.00)" in orden["motivo"]
