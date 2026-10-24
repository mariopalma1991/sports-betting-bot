import json
import threading
import time
from datetime import datetime, timezone

from websockets.sync.server import serve

import grabador as gr
import nba_monitor as nm


def _lineas(ruta):
    return [json.loads(l) for l in ruta.read_text(encoding="utf-8").splitlines()]


def test_graba_una_linea_por_mensaje_y_cambia_de_archivo_cada_dia(tmp_path):
    reloj = {"t": datetime(2026, 10, 24, 23, 59, 58, tzinfo=timezone.utc).timestamp()}
    g = gr.Grabador(tmp_path / "datos", reloj=lambda: reloj["t"])
    g.libro({"event_type": "book", "asset_id": "1"})
    g.libro({"event_type": "price_change", "price_changes": []})
    reloj["t"] += 5  # ya es 25 de octubre en UTC
    g.libro({"event_type": "last_trade_price", "price": "0.5"})
    g.marcador({"slug": "nba-chi-mil-2026-10-24", "score": "20-32"})
    g.cerrar()

    dia1 = _lineas(tmp_path / "datos" / "libro_2026-10-24.jsonl")
    dia2 = _lineas(tmp_path / "datos" / "libro_2026-10-25.jsonl")
    assert [l["m"]["event_type"] for l in dia1] == ["book", "price_change"]
    assert [l["m"]["event_type"] for l in dia2] == ["last_trade_price"]
    assert dia1[0]["t"] == reloj["t"] - 5
    assert _lineas(tmp_path / "datos" / "marcador_2026-10-25.jsonl")[0]["m"]["score"] == "20-32"
    assert g.escritos == 4


def test_no_borra_lo_grabado_en_corridas_anteriores(tmp_path):
    for _ in range(2):
        g = gr.Grabador(tmp_path, reloj=lambda: 1_761_350_000.0)
        g.libro({"n": 1})
        g.cerrar()
    [archivo] = tmp_path.glob("libro_*.jsonl")
    assert len(_lineas(archivo)) == 2


def test_partidos_se_reemplaza_completo(tmp_path):
    g = gr.Grabador(tmp_path, reloj=lambda: 100.0)
    g.partidos([{"token_id": "1", "partido": "Bulls vs. Bucks", "inicio": "2026-10-24T00:00:00+00:00"}])
    g.partidos([{"token_id": "2", "partido": "Lakers vs. Celtics", "inicio": None}])
    datos = json.loads((tmp_path / "partidos.json").read_text(encoding="utf-8"))
    assert [m["token_id"] for m in datos["mercados"]] == ["2"]
    assert not (tmp_path / "partidos.json.tmp").exists()


def test_fuente_en_vivo_graba_lo_que_recibe_incluidas_las_operaciones(tmp_path):
    g = gr.Grabador(tmp_path)
    fuente = nm.FuenteEnVivo()
    fuente.grabador = g
    fuente._procesar(json.dumps([{"event_type": "book", "asset_id": "1",
                                  "bids": [{"price": "0.40", "size": "10"}], "asks": []},
                                 {"event_type": "last_trade_price", "asset_id": "1", "price": "0.41"}]))
    fuente._procesar("PONG")
    g.cerrar()
    [archivo] = tmp_path.glob("libro_*.jsonl")
    assert [l["m"]["event_type"] for l in _lineas(archivo)] == ["book", "last_trade_price"]
    assert fuente.copia()["1"].bids == {0.40: 10.0}  # grabar no cambia lo que hace el monitor
    assert len(fuente.sacar_operaciones()) == 1


def test_completar_libros_graba_los_libros_pedidos_por_rest(tmp_path):
    class Resp:
        ok = True

        def json(self):
            return [{"asset_id": "1", "tick_size": "0.01", "bids": [{"price": "0.40", "size": "10"}], "asks": []}]

    class Sesion:
        def post(self, url, json, timeout):
            return Resp()

    g = gr.Grabador(tmp_path)
    fuente = nm.FuenteEnVivo()
    fuente.grabador = g
    fuente.tokens = ["1"]
    fuente.completar_libros(Sesion())
    g.cerrar()
    [archivo] = tmp_path.glob("libro_*.jsonl")
    [linea] = _lineas(archivo)
    assert linea["m"]["event_type"] == "book" and linea["m"]["origen"] == "rest"


def test_marcador_contesta_ping_y_solo_graba_la_nba(tmp_path):
    respuestas = []

    def manejar(ws):
        ws.send("ping")
        respuestas.append(ws.recv(timeout=5))
        ws.send(json.dumps({"slug": "nba-chi-mil-2026-10-24", "live": True, "score": "20-32", "period": "Q2"}))
        ws.send(json.dumps([{"slug": "atp-x-y", "score": "6-4"}, {"slug": "nba-lal-bos-2026-10-24", "live": False}]))
        time.sleep(1)

    with serve(manejar, "127.0.0.1", 0) as servidor:
        threading.Thread(target=servidor.serve_forever, daemon=True).start()
        g = gr.Grabador(tmp_path)
        fuente = gr.FuenteMarcador(g, f"ws://127.0.0.1:{servidor.socket.getsockname()[1]}")
        fuente.iniciar()
        for _ in range(50):
            if g.escritos >= 2:
                break
            time.sleep(0.1)
        servidor.shutdown()
    g.cerrar()

    assert respuestas == ["pong"]
    [archivo] = tmp_path.glob("marcador_*.jsonl")
    assert [l["m"]["slug"] for l in _lineas(archivo)] == ["nba-chi-mil-2026-10-24", "nba-lal-bos-2026-10-24"]
