#!/bin/bash

# --- ÉVITEMENT DES INTERACTIONS TTY PENDANT L'APT-GET ---
export DEBIAN_FRONTEND=noninteractive

# --- CONFIGURATION DES VARIABLES INTERNES ---
PRINTER_NAME=${1:-"SELPHY"}
TARGET_DIR="/home/pi/webprint"
WIFI_SSID="Print_Box"
WIFI_CHANNEL=7
IP_GATEWAY="192.168.4.1"
NETMASK="255.255.255.0"
DHCP_RANGE="192.168.4.10,192.168.4.250"

echo "================================================================="
echo "  EXECUTION DISTANTE : CONFIGURATION CAPTIVE PASSIF-LOCAL"
echo "  SSID Wi-Fi    : $WIFI_SSID"
echo "  Domaine Local : http://print.box"
echo "================================================================="

if [ "$EUID" -ne 0 ]; then
  echo "❌ Erreur critique : Ce script doit s'exécuter en root sur la Pi."
  exit 1
fi

echo "🌐 [1/5] Approvisionnement des dépendances logicielles (APT)..."
apt-get update && apt-get install -y \
    hostapd \
    dnsmasq \
    iptables \
    python3-venv \
    python3-pip \
    cups \
    printer-driver-gutenprint \
    curl \
    iptables-persistent

# Désinstallation de ipp-usb pour libérer l'accès aux imprimantes USB (conflits avec Gutenprint)
echo "🗑️  Purge de ipp-usb pour libérer l'accès USB direct..."
apt-get purge -y ipp-usb

# Configuration de CUPS pour autoriser l'administration à distance depuis le Mac
echo "🖨️  Configuration de CUPS (administration à distance et partage)..."
systemctl start cups
cupsctl --remote-admin --remote-any --share-printers WebInterface=yes
systemctl restart cups

systemctl stop hostapd dnsmasq webprint.service 2>/dev/null

# 2. CONFIGURATION DE L'INTERFACE SANS FIL (WLAN0) UNIQUEMENT
echo "🔒 [2/5] Isolation de wlan0 du contrôle NetworkManager..."
NM_CONF="/etc/NetworkManager/NetworkManager.conf"
if [ -f "$NM_CONF" ]; then
    if ! grep -q "unmanaged-devices" "$NM_CONF"; then
        echo -e "\n[keyfile]\nunmanaged-devices=interface-name:wlan0" >> "$NM_CONF"
        systemctl restart NetworkManager
    fi
fi

# Instanciation de l'IP statique wlan0 au boot via drop-in systemd
mkdir -p /etc/systemd/system/hostapd.service.d
cat << EOF > /etc/systemd/system/hostapd.service.d/override.conf
[Service]
ExecStartPre=/bin/sh -c '/bin/ip link set wlan0 up && /bin/ip addr add $IP_GATEWAY/24 dev wlan0 || true'
EOF

# 3. CONFIGURATION DES DAEMONS RÉSEAU COUCHE 2 & 3
echo "📡 [3/5] Écriture des configurations Hostapd et Dnsmasq (Mode Passif)..."

cat << EOF > /etc/hostapd/hostapd.conf
interface=wlan0
driver=nl80211
ssid=$WIFI_SSID
hw_mode=g
channel=$WIFI_CHANNEL
wmm_enabled=0
macaddr_acl=0
auth_algs=1
ignore_broadcast_ssid=0
EOF

sed -i 's|#DAEMON_CONF=""|DAEMON_CONF="/etc/hostapd/hostapd.conf"|g' /etc/default/hostapd

mv /etc/dnsmasq.conf /etc/dnsmasq.conf.bak 2>/dev/null
cat << EOF > /etc/dnsmasq.conf
interface=wlan0
dhcp-range=$DHCP_RANGE,$NETMASK,12h
local=/local/
address=/print.box/$IP_GATEWAY
EOF

# 4. MUTATION DU ROUTAGE (NAT PREROUTING RECONDUCTION PORT 80)
echo "🛡️ [4/5] Mutation de la table nat iptables (Port 80 -> 8000)..."
iptables -t nat -F PREROUTING
iptables -t nat -A PREROUTING -i wlan0 -p tcp --dport 80 -j REDIRECT --to-port 8000
netfilter-persistent save

# 5. DEPLOYMENT DE L'ENVIRONNEMENT PYTHON INTERNE
echo "🐍 [5/5] Instanciation de l'environnement virtuel et des dépendances..."
mkdir -p "$TARGET_DIR"
mv /tmp/app.py "$TARGET_DIR/app.py"
cd "$TARGET_DIR"

# Attente active du rétablissement de la connexion internet
echo "⏳ Attente du rétablissement de la connexion internet après redémarrage réseau (max 30s)..."
has_internet=false
for i in {1..30}; do
    if ping -c 1 -W 2 8.8.8.8 >/dev/null 2>&1 || ping -c 1 -W 2 google.com >/dev/null 2>&1; then
        echo "✅ Connexion internet opérationnelle."
        has_internet=true
        break
    fi
    sleep 1
done

if [ "$has_internet" = false ]; then
    echo "⚠️  Attention : Impossible d'établir une connexion internet. Les étapes d'installation et de téléchargement suivantes risquent de bloquer ou d'échouer."
fi

python3 -m venv venv
# Ajout de timeouts pour éviter tout blocage indéfini
./venv/bin/pip install --timeout 15 --upgrade pip || true
./venv/bin/pip install --timeout 15 flask pillow pillow-heif

# Téléchargement local des assets tiers de Cropper.js (Mode 100% Offline)
mkdir -p "$TARGET_DIR/static"
echo "📥 Téléchargement des assets Cropper.js..."
curl -sL --connect-timeout 5 --max-time 15 "https://cdnjs.cloudflare.com/ajax/libs/cropperjs/1.6.2/cropper.min.js" -o "$TARGET_DIR/static/cropper.min.js" || echo "⚠️ Échec du téléchargement du fichier JS de Cropper."
curl -sL --connect-timeout 5 --max-time 15 "https://cdnjs.cloudflare.com/ajax/libs/cropperjs/1.6.2/cropper.min.css" -o "$TARGET_DIR/static/cropper.min.css" || echo "⚠️ Échec du téléchargement du fichier CSS de Cropper."

# Initialisation de la configuration par défaut si elle n'existe pas encore
if [ ! -f "$TARGET_DIR/config_boite.json" ]; then
    echo "⚙️  Initialisation du fichier de configuration par défaut config_boite.json..."
    cat << EOF > "$TARGET_DIR/config_boite.json"
{
    "max_queue_size": 3,
    "max_prints_per_guest": 3,
    "quota_enabled": true,
    "printer_simu": false,
    "printer_name": "$PRINTER_NAME",
    "auto_reset_enabled": false,
    "auto_reset_interval": 30,
    "enhance_contrast": 1.15,
    "enhance_color": 1.2,
    "enhance_brightness": 1.1
}
EOF
fi

# 6. CONFIGURATION D'AUTO-DÉTECTION DE L'IMPRIMANTE SELPHY (UDEV)
echo "🖨️  Mise en place de la règle udev pour auto-détection de la Canon SELPHY..."

cat << 'EOF' > /usr/local/bin/auto_add_selphy.sh
#!/bin/bash
# Script déclenché par udev pour configurer automatiquement la Canon SELPHY dans CUPS

PRINTER_NAME="SELPHY"

# Log des opérations
exec >> /var/log/auto_add_selphy.log 2>&1
echo "=== [$(date)] Événement USB détecté ==="

# Attente pour s'assurer que le périphérique USB est bien initialisé
sleep 3

# Détection de l'URI de l'imprimante Canon branchée en USB
URI=$(lpinfo -v | grep -i "usb://Canon/" | head -n 1 | cut -d' ' -f2)

if [ -z "$URI" ]; then
    echo "[ERREUR] Aucune URI d'imprimante Canon USB détectée."
    exit 1
fi
echo "[INFO] URI trouvée : $URI"

# Recherche du meilleur driver Gutenprint disponible
PPD=$(lpinfo -m | grep -i "SELPHY" | grep -i "CP1500" | head -n 1 | cut -d' ' -f1)
if [ -z "$PPD" ]; then
    # Essayer de trouver n'importe quel autre driver SELPHY
    PPD=$(lpinfo -m | grep -i "SELPHY" | head -n 1 | cut -d' ' -f1)
fi

if [ -z "$PPD" ]; then
    echo "[ATTENTION] Aucun pilote spécifique Gutenprint SELPHY trouvé. Utilisation de la file brute (Raw)."
    lpadmin -p "$PRINTER_NAME" -E -v "$URI"
else
    echo "[INFO] Pilote sélectionné : $PPD"
    lpadmin -p "$PRINTER_NAME" -E -v "$URI" -m "$PPD"
    # Configuration par défaut en mode Sans Bordure (Borderless Postcard) et Couleurs Fidèles (Raw)
    lpadmin -p "$PRINTER_NAME" -o PageSize=Postcard.Borderless -o StpBorderless=True -o StpiShrinkOutput=Expand -o StpColorCorrection=Accurate -o StpImageType=Photo -o StpColorPrecision=Best
fi

# Activer et accepter les jobs sur l'imprimante
cupsenable "$PRINTER_NAME"
cupsaccept "$PRINTER_NAME"
echo "[SUCCÈS] L'imprimante '$PRINTER_NAME' est configurée et activée."
EOF

chmod +x /usr/local/bin/auto_add_selphy.sh

# Création de la règle udev pour intercepter le Vendor ID de Canon (04a9)
cat << EOF > /etc/udev/rules.d/99-selphy.rules
ACTION=="add", SUBSYSTEM=="usb", ENV{DEVTYPE}=="usb_device", ATTR{idVendor}=="04a9", RUN+="/usr/local/bin/auto_add_selphy.sh"
EOF

# Rechargement des règles udev
udevadm control --reload-rules && udevadm trigger

chown -R pi:pi "$TARGET_DIR" 2>/dev/null || true

# Création du descripteur d'unité de service Systemd
cat << EOF > /etc/systemd/system/webprint.service
[Unit]
Description=Serveur Web-to-Print Headless Passif
After=network.target CUPS.service hostapd.service dnsmasq.service NetworkManager.service

[Service]
Type=simple
User=root
WorkingDirectory=$TARGET_DIR
ExecStart=$TARGET_DIR/venv/bin/python app.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

echo "⚙️  Forçage et démasquage des services sous systemd..."
systemctl daemon-reload
systemctl unmask hostapd
systemctl enable hostapd dnsmasq webprint.service
systemctl restart hostapd dnsmasq webprint.service

echo "🎯 Configuration distante terminée avec succès."