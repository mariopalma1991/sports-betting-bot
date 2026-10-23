import json
import threading
import time

from websockets.sync.server import serve

import nba_monitor as nm


def test_mercados_de_evento_lee_listas_json():
    evento = {
        "title": "Lakers vs. Celtics",
        "slug": "nba-lal-bos-2026-10-24",
        "markets": [{
            "question": "Lakers vs. Celtics", "sportsMarketType": "moneyline",
            "outcomes": '["Lakers", "Celtics"]', "clobTokenIds": '["111", "222"]',
            "gameStartTime": "2026-10-24 23:30:00+00", "volume24hr": 1234.5,
        }],
    }
    assert nm.es_partido(evento)
    [m] = nm.mercados_de_evento(evento)
    assert (m.resultado, m.token_id, m.volumen_24h) == ("Lakers", "111", 1234.5)
    assert m.inicio.hour == 23


def test_futuros_no_son_partidos():
    assert not nm.es_partido({"slug": "nba-champion-2027", "markets": [{"question": "x"}]})


def test_mensajes_book_y_price_change():
    libros = {}
    nm.aplicar_mensaje(libros, {"event_type": "book", "asset_id": "1",
                                "bids": [{"price": "0.47", "size": "100"}],
                                "asks": [{"price": "0.50", "size": "50"}]})
    nm.aplicar_mensaje(libros, {"event_type": "price_change", "price_changes": [
        {"asset_id": "1", "price": "0.48", "size": "20", "side": "BUY"},
        {"asset_id": "1", "price": "0.50", "size": "0", "side": "SELL"},
        {"asset_id": "1", "price": "0.51", "size": "30", "side": "SELL"},
    ]})
    assert libros["1"].bids == {0.47: 100.0, 0.48: 20.0}
    assert libros["1"].asks == {0.51: 30.0}
    # formato anterior
    nm.aplicar_mensaje(libros, {"event_type": "price_change", "asset_id": "1",
                                "changes": [{"price": "0.49", "size": "5", "side": "BUY"}]})
    assert libros["1"].bids[0.49] == 5.0


def test_analizar_marca_oportunidad():
    mercado = nm.Mercado("A vs B", "Ganador", "A", "1", None, 50_000)
    libro = nm.Libro(bids={0.47: 1000}, asks={0.50: 1000})
    f = nm.analizar(mercado, libro)
    assert (f.compra, f.venta, f.spread, f.ticks_libres) == (0.47, 0.50, 0.03, 2)
    assert f.profundidad == 0.47 * 1000 + 0.50 * 1000
    assert (f.compra_sugerida, f.venta_sugerida, f.margen) == (0.48, 0.49, 0.01)
    assert f.oportunidad
    # spread de 2 ticks: mejorar los dos lados deja margen cero
    f2 = nm.analizar(mercado, nm.Libro(bids={0.48: 1000}, asks={0.50: 1000}))
    assert f2.compra_sugerida is None and not f2.oportunidad
    # ticks de 0.001
    f3 = nm.analizar(mercado, nm.Libro(bids={0.970: 1000}, asks={0.975: 1000}, tick=0.001))
    assert (f3.compra_sugerida, f3.venta_sugerida, f3.margen) == (0.971, 0.974, 0.003)
    # spread de un tick: sin espacio
    assert not nm.analizar(mercado, nm.Libro(bids={0.49: 1000}, asks={0.50: 1000})).oportunidad
    # sin libro
    assert nm.analizar(mercado, None).spread is None


def test_fuente_en_vivo_con_servidor_local(monkeypatch):
    recibido = []

    def manejar(ws):
        recibido.append(json.loads(ws.recv()))
        ws.send(json.dumps([{"event_type": "book", "asset_id": "1",
                             "bids": [{"price": "0.40", "size": "10"}],
                             "asks": [{"price": "0.43", "size": "10"}]}]))
        ws.send(json.dumps({"event_type": "price_change", "price_changes": [
            {"asset_id": "1", "price": "0.41", "size": "7", "side": "BUY"}]}))
        time.sleep(1)

    with serve(manejar, "127.0.0.1", 0) as servidor:
        threading.Thread(target=servidor.serve_forever, daemon=True).start()
        monkeypatch.setattr(nm, "WS_URL", f"ws://127.0.0.1:{servidor.socket.getsockname()[1]}")
        fuente = nm.FuenteEnVivo()
        fuente.suscribir(["1"])
        fuente.iniciar()
        for _ in range(50):
            if fuente.copia().get("1") and 0.41 in fuente.copia()["1"].bids:
                break
            time.sleep(0.1)
        servidor.shutdown()

    assert recibido == [{"type": "market", "assets_ids": ["1"]}]
    assert fuente.copia()["1"].bids == {0.40: 10.0, 0.41: 7.0}
