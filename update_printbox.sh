#!/bin/bash

# --- PARAMÈTRES DE CONFLIT DE LA LIAISON SÉCURISÉE ---
REMOTE_USER=${1:-"pi"}
REMOTE_HOST=${2:-"rpi.local"}
TARGET_DIR="/home/pi/webprint"

# Options SSH pour ignorer la vérification de clé d'hôte (très utile en cas de re-flash de la Pi)
SSH_OPTS="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null"

echo "================================================================="
echo " 🚀 ENVOI RAPIDE D'UNE MISE À JOUR SUR LA RASPBERRY PI"
echo " Cible réseau : ${REMOTE_USER}@${REMOTE_HOST}"
echo "================================================================="

# 1. Validation de l'intégrité de l'arborescence locale sur le Mac
if [ ! -f "app.py" ]; then
    echo "❌ Erreur critique : Le fichier 'app.py' doit se trouver dans le même répertoire que ce script."
    exit 1
fi

# 2. Vérification pré-vol de la connexion réseau et de SSH
echo "🔍 [1/3] Vérification de la disponibilité de la Raspberry Pi (${REMOTE_HOST})..."
if ! nc -z -w 3 "$REMOTE_HOST" 22 >/dev/null 2>&1; then
    echo "❌ Erreur : Le service SSH (port 22) est inaccessible sur '${REMOTE_HOST}'."
    echo "   - Vérifiez que la Raspberry Pi est allumée et connectée en Ethernet."
    echo "   - Vérifiez que le partage de connexion est actif sur votre Mac."
    exit 1
fi
echo "✅ Connexion SSH établie avec succès."

# 3. Transfert direct du fichier app.py (et des assets statiques éventuels)
echo "📦 [2/3] Copie de app.py vers la Raspberry Pi..."
scp $SSH_OPTS app.py "${REMOTE_USER}@${REMOTE_HOST}:${TARGET_DIR}/app.py"

if [ $? -ne 0 ]; then
    echo "❌ Erreur : Échec du transfert du fichier app.py."
    exit 1
fi

# Copie du dossier static local s'il existe et contient des modifications
if [ -d "static" ]; then
    echo "📦 Copie du dossier static local..."
    scp $SSH_OPTS -r static/* "${REMOTE_USER}@${REMOTE_HOST}:${TARGET_DIR}/static/" >/dev/null 2>&1
fi

# 4. Installation des dépendances Python sur la RPi
echo "🐍 [3/4] Installation/Mise à jour des dépendances Python (pillow-heif)..."
ssh $SSH_OPTS -t "${REMOTE_USER}@${REMOTE_HOST}" "cd ${TARGET_DIR} && ./venv/bin/pip install --timeout 15 flask pillow pillow-heif"

# 5. Redémarrage du service Flask
echo "⚡ [4/4] Redémarrage de l'application Flask (webprint.service)..."
ssh $SSH_OPTS -t "${REMOTE_USER}@${REMOTE_HOST}" "sudo systemctl restart webprint.service"

if [ $? -eq 0 ]; then
    # Vérification du statut final du service
    echo "🔍 Vérification du statut du service 'webprint.service' sur la Pi..."
    SERVICE_STATUS=$(ssh $SSH_OPTS "${REMOTE_USER}@${REMOTE_HOST}" "systemctl is-active webprint.service" 2>/dev/null)
    if [ "$SERVICE_STATUS" = "active" ]; then
        echo "================================================================="
        echo " 🎉 MISE À JOUR EFFECTUÉE ET SERVICE ACTIF EN QUELQUES SECONDES !"
        echo " - URL de production  : http://print.box"
        echo " - Interface de gestion : http://print.box/admin"
        echo "================================================================="
    else
        echo "⚠️  Attention : Le service a redémarré mais son statut actuel est '$SERVICE_STATUS'."
        echo "   Récupération des logs récents..."
        ssh $SSH_OPTS "${REMOTE_USER}@${REMOTE_HOST}" "sudo journalctl -u webprint.service -n 10 --no-pager"
        exit 1
    fi
else
    echo "❌ Erreur critique lors du redémarrage du service."
    exit 1
fi
