# sports-betting-bot

## Monitor NBA en Polymarket (tiempo real)

`nba_monitor.py` muestra en la terminal todos los partidos NBA abiertos en Polymarket con su libro de órdenes en vivo: mejor precio de compra y de venta, spread, dinero en el libro cerca del precio y volumen de 24 h. Marca con un punto verde los mercados que se ven buenos para hacer scalping como maker.

Solo **lee** datos públicos. No necesita llaves, no toca tu wallet y no coloca órdenes.

### Cómo funciona

1. Cada 5 minutos pide a la API Gamma de Polymarket la lista de partidos NBA (etiqueta 745). Deja fuera los mercados de temporada, como campeón o MVP.
2. Se suscribe al WebSocket de mercado de Polymarket (`wss://ws-subscriptions-clob.polymarket.com/ws/market`), que manda cada cambio del libro al instante. Si la conexión se cae, se reconecta solo.
3. Redibuja la tabla dos veces por segundo.

### Instalación

Necesitas Python 3.10 o más reciente.

```bash
git clone https://github.com/mariopalma1991/sports-betting-bot.git
cd sports-betting-bot
python -m venv .venv
# Windows:   .venv\Scripts\activate
# Mac/Linux: source .venv/bin/activate
pip install -r requirements.txt
```

### Uso

```bash
python nba_monitor.py                 # datos reales en tiempo real
python nba_monitor.py --demo          # datos simulados, para ver cómo se ve
python nba_monitor.py --csv datos.csv # además guarda una foto de la tabla cada 10 s
python nba_monitor.py --una-vez       # muestra la tabla una vez y sale
```

Para salir, presiona `Ctrl+C`. Si la tabla se ve cortada, haz más ancha la ventana de la terminal.

### Cómo leer la tabla

| Columna | Qué significa |
|---|---|
| Compra / Venta | Mejor precio al que alguien compra y al que alguien vende el lado indicado |
| Spread | Diferencia entre Venta y Compra. Es el espacio donde ganas como maker |
| Compra sug. / Venta sug. | Precios para poner tus órdenes como maker: 1 tick arriba de la mejor compra y 1 tick abajo de la mejor venta, para quedar primero en la fila |
| Margen | Lo que ganas por acción si se llenan las dos órdenes sugeridas |
| Libro ±2¢ | Dólares en órdenes a 2 centavos o menos del precio medio |
| Vol 24h | Volumen negociado en las últimas 24 horas |
| Cambio | Hace cuánto se movió el libro (amarillo = hace menos de 2 s) |

Los precios sugeridos son una regla mecánica, no un consejo: el precio puede moverse antes de que se llenen tus órdenes.

**Punto verde (oportunidad):** hay al menos 2 ticks libres entre compra y venta (así queda margen después de mejorar los dos lados), el spread es de 6¢ o menos, el volumen de 24 h pasa de $5k y el libro tiene al menos $200. Puedes cambiar esos números al inicio de `nba_monitor.py`.

**Rojo** en Libro ±2¢ indica menos de $100 de liquidez. **Gris** en Spread indica un spread de 1 tick, sin espacio para meterse.

### Pruebas

```bash
pip install pytest
python -m pytest tests
```
