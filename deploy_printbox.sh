#!/bin/bash

# --- PARAMÈTRES DE CONFLIT DE LA LIAISON SÉCURISÉE ---
PRINTER_NAME=${1:-"SELPHY"}
REMOTE_USER=${2:-"pi"}
REMOTE_HOST=${3:-"rpi.local"}

# Options SSH pour ignorer la vérification de clé d'hôte (très utile en cas de re-flash de la Pi)
SSH_OPTS="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null"

echo "================================================================="
echo " 🚀 AMORÇAGE DU PIPELINE DE PROVISIONNEMENT DEPUIS MAC"
echo " Cible réseau : ${REMOTE_USER}@${REMOTE_HOST}"
echo " Imprimante   : ${PRINTER_NAME}"
echo "================================================================="

# 1. Validation de l'intégrité de l'arborescence locale sur le Mac
if [ ! -f "app.py" ] || [ ! -f "setup_printbox.sh" ]; then
    echo "❌ Erreur critique : Les fichiers 'app.py' et 'setup_printbox.sh' doivent"
    echo "   impérativement se trouver dans le même répertoire que ce script sur votre Mac."
    exit 1
fi

# 1b. Vérification pré-vol de la connexion réseau et de SSH
echo "🔍 [1/4] Vérification de la disponibilité de la Raspberry Pi (${REMOTE_HOST})...."
if ! nc -z -w 3 "$REMOTE_HOST" 22 >/dev/null 2>&1; then
    echo "❌ Erreur : Le service SSH (port 22) est inaccessible sur '${REMOTE_HOST}'."
    echo "   - Vérifiez que la Raspberry Pi est allumée et connectée en Ethernet."
    echo "   - Vérifiez que le partage de connexion est actif sur votre Mac."
    echo "   - Assurez-vous que l'hôte '${REMOTE_HOST}' ou son adresse IP est correcte."
    exit 1
fi
echo "✅ Connexion SSH établie avec succès."

# 2. Upload des payloads dans l'espace temporaire /tmp de la Pi
echo "📦 [2/4] Transfert des scripts vers la zone d'échange de la Raspberry Pi..."
scp $SSH_OPTS app.py setup_printbox.sh "${REMOTE_USER}@${REMOTE_HOST}:/tmp/"

if [ $? -ne 0 ]; then
    echo "❌ Erreur : Échec de la transmission sécurisée des fichiers."
    exit 1
fi

# 3. Élévation de privilèges et exécution déportée
echo "⚡ [3/4] Connexion SSH et exécution du script de provisionnement..."
# L'allocation de pseudo-TTY (-t) permet de saisir le mot de passe sudo de la Pi de manière interactive
ssh $SSH_OPTS -t "${REMOTE_USER}@${REMOTE_HOST}" "
    chmod +x /tmp/setup_printbox.sh && \
    sudo /tmp/setup_printbox.sh '$PRINTER_NAME' && \
    rm /tmp/setup_printbox.sh
"

if [ $? -eq 0 ]; then
    # 4. Vérification de l'état final du service
    echo "🔍 [4/4] Vérification du statut du service 'webprint.service' sur la Pi..."
    SERVICE_STATUS=$(ssh $SSH_OPTS "${REMOTE_USER}@${REMOTE_HOST}" "systemctl is-active webprint.service" 2>/dev/null)
    if [ "$SERVICE_STATUS" = "active" ]; then
        echo "================================================================="
        echo " 🎉 PIPELINE EXÉCUTÉ AVEC SUCCÈS DEPUIS VOTRE MAC !"
        echo " La borne est opérationnelle et le service est ACTIF."
        echo " - Point d'accès actif : Print_Box"
        echo " - URL de production  : http://print.box"
        echo " - Interface de gestion : http://print.box/admin"
        echo "================================================================="
    else
        echo "⚠️  Attention : Le script s'est terminé mais le service 'webprint.service' est '$SERVICE_STATUS'."
        echo "   Récupération des logs récents..."
        ssh $SSH_OPTS "${REMOTE_USER}@${REMOTE_HOST}" "sudo journalctl -u webprint.service -n 15 --no-pager"
        exit 1
    fi
else
    echo "❌ Erreur critique : Le script distant a renvoyé un code d'erreur."
    exit 1
fi