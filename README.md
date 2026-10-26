# sports-betting-bot

## Monitor NBA en Polymarket (tiempo real)

`nba_monitor.py` muestra en la terminal todos los partidos NBA abiertos en Polymarket con su libro de órdenes en vivo: mejor precio de compra y de venta, spread, dinero en el libro cerca del precio y volumen de 24 h. Marca con un punto verde los mercados que se ven buenos para hacer scalping como maker.

Solo **lee** datos públicos. No necesita llaves, no toca tu wallet y no coloca órdenes.

### Cómo funciona

1. Cada 5 minutos pide a la API Gamma de Polymarket la lista de partidos NBA (etiqueta 745). Deja fuera los mercados de temporada, como campeón o MVP.
2. Pide por REST la foto inicial de cada libro que el WebSocket no haya mandado, y reintenta cada 30 s mientras falte alguno.
3. Se suscribe al WebSocket de mercado de Polymarket (`wss://ws-subscriptions-clob.polymarket.com/ws/market`), que manda cada cambio del libro al instante. Si la conexión se cae, se reconecta solo.
4. Redibuja la tabla dos veces por segundo.

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
python nba_monitor.py --mostrar-vacios # incluye los mercados sin órdenes (ocultos por defecto)
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

### Grabar datos para analizarlos después

```bash
python nba_monitor.py --grabar grabaciones/
```

Además de mostrar la tabla, guarda en esa carpeta los datos tal como llegan, para descubrir y probar estrategias con calma (backtest):

| Archivo | Qué contiene |
|---|---|
| `libro_AAAA-MM-DD.jsonl` | Cada mensaje del WebSocket de mercado: el libro completo, cada cambio de precio y cada operación ejecutada. También la foto inicial que se pide por REST (marcada con `"origen": "rest"`) |
| `marcador_AAAA-MM-DD.jsonl` | Cada mensaje del canal de deportes de Polymarket (marcador, periodo, minuto, si el partido está en vivo) de partidos NBA |
| `partidos.json` | Los mercados vigentes: partido, resultado, `token_id` y hora de inicio. Sirve para saber a qué partido pertenece cada `asset_id` y si un mensaje es de antes o después del inicio |

Cada línea de los `.jsonl` es `{"t": hora, "m": mensaje}`, donde `t` son segundos desde 1970 (UTC) y el día del nombre del archivo también es UTC. Si vuelves a correrlo, agrega al final sin borrar lo anterior. Abajo de la tabla se ve cuántas líneas lleva grabadas.

Los archivos crecen rápido: un partido en vivo puede generar muchos mensajes por segundo. Revisa el espacio en disco si lo dejas correr toda una noche. La carpeta `grabaciones/` está en `.gitignore`.

Se puede combinar con `--papel`. No funciona con `--demo`.

### Simulación en papel (probar la estrategia sin dinero)

```bash
python nba_monitor.py --papel
```

Corre varias estrategias a la vez (A, B y C) con los mismos datos reales del WebSocket, pero con órdenes **ficticias**. Cada una tiene su propio presupuesto. No usa dinero ni toca tu wallet. Debajo de la tabla aparece un panel con:

- **Resumen:** ganancia de las operaciones cerradas, ganancia o pérdida de las abiertas, vueltas completas, stops, % de éxito, órdenes canceladas y cuánto del presupuesto está comprometido.
- **Órdenes ficticias:** compras pendientes y posiciones esperando su venta, con el precio actual.
- **Últimas cerradas:** cada operación con su motivo (objetivo o stop), duración y resultado.

Al salir con `Ctrl+C` muestra el resumen final. Cada operación cerrada se guarda en `papel_estrategias.csv`, con una columna `estrategia` (A, B o C). El archivo se va acumulando entre corridas, así que sirve para comparar varios días.

Para ver el resumen por día y por estrategia:

```bash
python resumen_papel.py                          # lee papel_estrategias.csv
python resumen_papel.py papel_operaciones.csv    # también lee el CSV de la versión anterior (lo cuenta como A)
```

**Las estrategias:**

| | Cuándo entra |
|---|---|
| **A** | En cualquier mercado con punto verde |
| **B** | Igual que A, pero solo **antes** de que empiece el partido (cancela sus compras pendientes al empezar), con precio de compra entre 0.20 y 0.80, y no entra si la mejor compra bajó 1¢ o más en los últimos 30 s |
| **C** | Igual que A (cualquier precio, también con el partido en juego), pero solo si la mejor compra **subió** 1¢ o más en los últimos 30 s |

Para correr solo algunas: `--estrategias B,C`. Los filtros se ajustan en `ESTRATEGIAS`, al inicio de `papel.py`.

**Reglas comunes a todas:**

1. **Compra.** En cada mercado con punto verde pone una compra al precio "Compra sug.". Se cancela si alguien mejora su precio o si pasan 60 s sin llenarse, y entonces vuelve a ponerla al precio nuevo.
2. **Venta.** Cuando se llena la compra, deja una venta en "Venta sug." (take profit).
3. **Stop.** Si la mejor compra baja 3¢ desde tu precio de compra, vende de inmediato a ese precio.
4. **Presupuesto.** Cada estrategia nunca compromete más de $7 a la vez, con 5 acciones por orden.

Todo se ajusta con opciones: `--presupuesto 7 --acciones 5 --stop 0.03 --espera 60 --comision-taker 0.01`. La comisión solo se cobra en los stops, porque como maker no pagas comisión. Revisa en Polymarket la comisión actual para mercados deportivos.

**Cómo decide si una orden se llena.** Una compra ficticia se da por llena cuando alguien de verdad vende a ese precio o más abajo, o cuando aparece una venta en el libro a ese precio. La venta funciona al revés. Es una aproximación:

- Supone que tu orden es la primera de la fila y que se llena completa.
- No ve las operaciones que se cruzan contra el otro resultado del mercado.

Toma los resultados como una guía, no como una garantía. Déjalo correr varias noches de partidos antes de sacar conclusiones.

## Comparador Polymarket vs. casas de apuestas

`cuotas.py` compara el precio de cada mercado NBA de Polymarket (ganador, spread y totales) con los momios de una casa de referencia, **Pinnacle** por defecto. Cuando en Polymarket puedes comprar un resultado más barato que la probabilidad que le da la casa, hay ventaja.

**Cómo calcula la probabilidad justa:** pasa el momio decimal a probabilidad (1 / momio) y quita el margen de la casa, repartiéndolo en proporción para que los dos lados sumen 100%. Por ejemplo, Lakers 1.62 y Celtics 2.45 da Lakers 60.2%. Si en Polymarket la venta de Lakers está en 0.52, la ventaja es de **+8.2¢** por acción.

### Llave de The Odds API

Los momios vienen de [The Odds API](https://the-odds-api.com). Crea una cuenta gratis y copia tu llave. **No la pegues en ningún chat ni la subas a GitHub.** Pásala así:

```bash
export ODDS_API_KEY=tu_llave      # Git Bash; en PowerShell: $env:ODDS_API_KEY="tu_llave"
python cuotas.py
```

El plan gratis tiene un límite mensual de créditos. Cada consulta gasta 1 crédito por mercado pedido. Por eso, por defecto pide solo el ganador (`h2h`) cada 10 minutos. El programa muestra cuántos créditos te quedan.

### Uso

```bash
python cuotas.py                                   # tabla en vivo + apuestas en papel
python cuotas.py --mercados h2h,spreads,totals     # los tres mercados (3 créditos por consulta)
python cuotas.py --min-ventaja 0.04 --monto 1      # apostar en papel solo con 4¢ o más de ventaja
python cuotas.py --liquidar                        # resolver las apuestas de partidos terminados
```

**Apuestas en papel:** cada vez que un lado tiene al menos `--min-ventaja` (3¢ por defecto) y $5 disponibles a ese precio, antes de que empiece el partido, se registra una apuesta ficticia de $1 en `valor_apuestas.csv`. Solo una por lado de cada mercado. Al día siguiente, `--liquidar` revisa en Polymarket qué mercados ya se resolvieron y llena el resultado y la ganancia.

**Dos estrategias en paralelo.** Cada oportunidad se apuesta dos veces en papel, $1 cada una, para comparar:

| Estrategia | Qué hace |
|---|---|
| `mantener` | Compra y espera al final del partido. |
| `vender_justo` | Compra y deja una venta al precio justo de la casa, redondeado al centavo (por ejemplo, Bucks 17.2% → vende a 0.17). Si alguien paga ese precio, se cuenta como **vendida** con esa ganancia. Si nunca llega, se queda hasta el final igual que `mantener`. |

Las ventas se revisan cada 30 segundos con el libro de Polymarket, también durante el partido, que es cuando casi siempre se llenan. **Deja el programa corriendo hasta que terminen los partidos.** Después de que empezó el último, ya no consulta momios, así que no gasta créditos. Una venta se cuenta solo cuando alguien ofrece comprar a ese precio o más.

El panel muestra la **ganancia real** de las apuestas resueltas y la **ganancia esperada** según la casa. Si la ventaja es real, con muchas apuestas las dos deberían parecerse. Con pocas apuestas hay mucha suerte de por medio: hacen falta 50 o más para sacar conclusiones.

**Cosas a tener en cuenta:**

- Los spreads y totales solo se comparan si la línea es la misma en los dos lados (−4.5 contra −4.5). The Odds API trae solo la línea principal de la casa.
- En los spreads se supone que la línea del título de Polymarket ("Spread −4.5") es del primer resultado del mercado.
- El precio de compra del segundo resultado se calcula como 1 − mejor compra del primero.
- Usa `--comision` para descontar la comisión de taker de Polymarket en deportes.

## Recompensas de liquidez (cuánto pagaría el premio)

Polymarket reparte cada día una bolsa por mercado entre quienes dejan órdenes límite esperando cerca del precio medio, aunque nadie se las llene. `recompensas.py` te dice qué partidos que aún no empiezan tienen premio hoy y cuánto te tocaría más o menos con tu capital.

Solo **lee** datos públicos. No necesita llaves, no toca tu wallet y no coloca órdenes. Corre una vez, muestra la tabla y sale. Basta con correrlo una o dos veces al día.

```bash
python recompensas.py                    # partidos de hoy con premio, simulando 25 y 50 USD
python recompensas.py --capital 30,60    # otros capitales
python recompensas.py --distancia 2      # tu orden a 2¢ del precio medio en vez de 1¢
python recompensas.py --demo             # datos simulados, para ver cómo se ve
```

### Cómo calcula

1. Pide a Polymarket los premios activos de hoy: bolsa diaria, tamaño mínimo (acciones) y distancia máxima al precio medio (centavos) de cada mercado.
2. Se queda con los partidos de cualquier deporte que todavía no empiezan. Deja fuera futuros de temporada y lo que no es deporte.
3. Lee el libro de cada uno y calcula los puntos de las órdenes que ya compiten con la fórmula oficial: `((máx − distancia) / máx)² × acciones`.
4. Simula tu orden a `--distancia` centavos del medio, de un lado o de los dos, y se queda con la que da más puntos. Con un solo lado los puntos se dividen entre 3, y fuera de 0.10–0.90 un solo lado no cuenta.
5. Tu parte = tus puntos ÷ (tus puntos + los de los demás). La ganancia es esa parte de la bolsa por las horas que faltan para el partido (máximo un día), porque cancelarías al inicio.

### Cómo leer la tabla

| Columna | Qué significa |
|---|---|
| Bolsa/día | Dólares que reparte el mercado por día |
| Mín. acc. / Máx. dist. | Tu orden necesita al menos esas acciones y estar a menos de esa distancia del precio medio para contar |
| Competencia | Puntos de las órdenes que ya están en el libro |
| Con N USD | Dólares estimados hasta el inicio · tu parte de la bolsa · uno o dos lados. "No alcanza" si ese capital no llega al mínimo de acciones |

La tabla está ordenada por ganancia por dólar. Cada corrida se agrega a `recompensas.csv` para ver cómo cambia día con día.

**Cosas a tener en cuenta:**

- Es una estimación. Polymarket toma la foto del libro cada minuto en un momento al azar, usa un precio medio ajustado por tamaño y los demás mueven sus órdenes.
- El libro junta las órdenes por precio, así que no se sabe quién cotiza un lado o los dos. Se supone que los rivales cotizan los dos lados y se cuentan todas sus órdenes, aunque alguna sea menor al mínimo. Por eso tu parte estimada tiende a salir de menos, no de más.
- Polymarket solo paga si en el día juntas al menos 1 USD.
- No mide el riesgo de que te llenen un solo lado justo antes de una noticia. Eso puede costar más que el premio.

## Dashboard en el navegador

`dashboard.py` muestra los partidos NBA del día en una página, uno por tarjeta: arriba el widget
oficial de Polymarket con las probabilidades en vivo (con tu etiqueta `creator=Marhum`) y abajo
compra, venta, spread, liquidez y volumen de cada mercado, igual que en el monitor. Los precios se
actualizan cada 5 segundos sin recargar los widgets.

```bash
python dashboard.py            # abre http://localhost:8050 en tu navegador
python dashboard.py --demo     # datos simulados (sin widgets), para ver cómo se ve
python dashboard.py --puerto 8060 --no-abrir
```

Debajo de cada widget va el marcador y el reloj del partido (`marcador.py`), del canal de deportes
de Polymarket: llega al instante por WebSocket, igual que el que usa `--grabar`.

Arriba eliges **Hoy**, **Mañana** o **Todos**, y con **Solo oportunidades** (activada al abrir) se ven todos los
partidos, pero debajo de cada uno solo los mercados con punto verde (spread con espacio para comprar y
vender como maker); si no hay ninguno, la tabla queda en blanco. Los
mercados de margen exacto ("Exact Margin") no se muestran. El servidor solo escucha en tu computadora
(`127.0.0.1`) y se cierra con Ctrl+C.

### Bot en papel (pestaña "Bot (papel)")

Mientras el dashboard está abierto, un bot opera con dinero ficticio y la pestaña muestra en vivo
su saldo, sus órdenes abiertas y el historial de compras y ventas. Corre tres estrategias, cada una
con $7 ficticios (`--presupuesto`):

- **Spread (estrategia B):** compra 1 tick arriba de la mejor compra y vende 1 tick abajo de la
  mejor venta, solo antes del partido y con precio entre 0.20 y 0.80. Stop a 3¢.
- **Spread en pausas:** la misma jugada, pero solo durante el partido y con el reloj detenido
  (tiempos fuera, tiros libres, entre cuartos), según el marcador de Polymarket: se considera pausa
  cuando el reloj no cambia en 5 s y nadie anota en 3 s. Si el juego se reanuda o alguien anota,
  cancela las compras sin llenar; lo ya comprado sigue esperando su venta o su stop. En la tarjeta
  del partido se ve "pausa" junto al reloj.
- **Comparador:** compra $1 antes del partido cuando Polymarket está al menos 1¢ más barato que la
  probabilidad justa de Pinnacle (`--min-ventaja`), y deja una venta a ese precio justo. Si no se
  vende, espera al final del partido. Necesita la llave de The Odds API:

```bash
export ODDS_API_KEY=tu_llave
python dashboard.py
```

  Pide momios cada 30 minutos (`--momios-cada`, 1 crédito cada vez) y solo mientras haya partidos
  por empezar.

El saldo arrastra lo ganado y lo perdido y se guarda en `bot_spread.csv`, `bot_pausas.csv` y
`bot_comparador.csv`, así que sigue donde se quedó la próxima vez que abras el dashboard. Para empezar
de cero, borra esos archivos. En `--demo` el bot de spread corre con datos simulados y no guarda nada.

## Pruebas

```bash
pip install pytest
python -m pytest tests
```
