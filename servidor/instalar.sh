#!/usr/bin/env bash
# Instala el bot en un servidor Ubuntu 24.04 (por ejemplo, Vultr en Ciudad de México).
#
# Uso, ya conectado al servidor como root:
#   git clone https://github.com/mariopalma1991/sports-betting-bot.git
#   bash sports-betting-bot/servidor/instalar.sh
#
# Se puede volver a correr para actualizar o cambiar la llave.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE=/etc/nba-bot.env

if [ "$(id -u)" -ne 0 ]; then
  echo "Corre este script como root." >&2
  exit 1
fi

echo "==> Instalando paquetes"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q git tmux python3-venv fail2ban

echo "==> Zona horaria: Ciudad de México"
timedatectl set-timezone America/Mexico_City

echo "==> Preparando Python"
python3 -m venv "$REPO/.venv"
"$REPO/.venv/bin/pip" install -q --upgrade pip
"$REPO/.venv/bin/pip" install -q -r "$REPO/requirements.txt"

if [ ! -s "$ENV_FILE" ] || [ "${1:-}" = "--cambiar-llave" ]; then
  echo
  echo "Pega tu llave de The Odds API y presiona Enter (no se verá en pantalla):"
  read -rs LLAVE < /dev/tty
  echo
  if [ -z "$LLAVE" ]; then
    echo "No se recibió ninguna llave." >&2
    exit 1
  fi
  umask 077
  printf 'ODDS_API_KEY=%s\n' "$LLAVE" > "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  unset LLAVE
fi

echo "==> Instalando servicios"
for f in "$REPO"/servidor/systemd/*; do
  sed "s|@REPO@|$REPO|g" "$f" > "/etc/systemd/system/$(basename "$f")"
done
for f in ver-cuotas ver-monitor actualizar-bot estado-bot; do
  sed "s|@REPO@|$REPO|g" "$REPO/servidor/comandos/$f" > "/usr/local/bin/$f"
  chmod 755 "/usr/local/bin/$f"
done

systemctl daemon-reload
systemctl enable --now nba-monitor.service
systemctl enable --now nba-cuotas-inicio.timer nba-cuotas-fin.timer nba-liquidar.timer
systemctl restart nba-monitor.service

# Si se instala dentro del horario del comparador, arrancarlo ya.
HORA=$((10#$(date +%H)))
if [ "$HORA" -ge 12 ] && [ "$HORA" -lt 20 ]; then
  systemctl restart nba-cuotas.service
fi

echo
echo "Listo. Comandos útiles:"
echo "  ver-cuotas      ver la tabla del comparador (salir sin cerrarlo: Ctrl+B y luego D)"
echo "  ver-monitor     ver el monitor en vivo con las estrategias"
echo "  estado-bot      qué está corriendo y a qué hora arranca lo siguiente"
echo "  actualizar-bot  bajar la versión nueva del código y reiniciar"
