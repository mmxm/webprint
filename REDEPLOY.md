# 🚀 Guide de Redéploiement de la Borne Photo (Web-to-Print)

Ce guide décrit la procédure étape par étape pour réinstaller et configurer complètement la borne d'impression sur une nouvelle Raspberry Pi (ou réinstaller la Pi actuelle) depuis votre Mac.

---

## 📋 Prérequis

1. **La Raspberry Pi** :
   * Raspberry Pi OS (Lite, version 64 bits Debian 12 Trixie recommandée).
   * L'utilisateur SSH par défaut configuré avec le nom **`pi`** et le mot de passe **`tennis`** (ou configurez vos propres variables).
   * SSH activé sur la Pi.
   * Déployé avec raspberry pi imager
2. **Les connexions** :
   * La Raspberry Pi doit être connectée en **Ethernet** à votre Mac.
   * Le **partage de connexion internet** de votre Mac doit être activé sur l'interface Ethernet pour que la Pi puisse télécharger ses dépendances lors du premier déploiement.
3. **L'imprimante** :
   * Canon SELPHY CP1500 (ou CP1300) branchée en USB à la Pi et allumée.

---

## 🛠️ Procédure de déploiement (Étape par Étape)

### Étape 1 : Récupérer le projet sur votre Mac
Ouvrez le terminal de votre Mac et placez-vous dans le dossier du projet :
```bash
cd /Users/francois/Documents/webprint
```

### Étape 2 : Lancer le script d'installation complet
Exécutez le script d'amorçage. Il va valider la connexion avec la Pi, transférer les fichiers nécessaires (`app.py`, `setup_printbox.sh`), installer les paquets système (CUPS, Gutenprint, hostapd, dnsmasq) et configurer le réseau WiFi captif :
```bash
./deploy_printbox.sh
```
*Note : Saisissez le mot de passe SSH de la Pi (`tennis`) lorsqu'il vous sera demandé par la commande.*

### Étape 3 : Appliquer les réglages sans bordure & qualité photo
Une fois le script terminé, appliquez la configuration d'impression Gutenprint optimisée.
* **Option A (Physique)** : Débranchez le câble USB reliant la SELPHY à la Pi, attendez 3 secondes et rebranchez-le. La règle automatique `udev` va configurer l'imprimante instantanément.
* **Option B (Ligne de commande)** : Forcez le déclenchement du script à distance depuis votre Mac :
  ```bash
  ssh pi@rpi.local "sudo /usr/local/bin/auto_add_selphy.sh"
  ```
  *(Entrez le mot de passe `tennis` si demandé).*

### Étape 4 : Valider le déploiement
Pour vous assurer que l'imprimante est correctement configurée et que toutes les options de qualité photo sont actives, lancez cette commande :
```bash
ssh pi@rpi.local "lpoptions -p SELPHY -l"
```
Le retour doit afficher une étoile `*` devant les options suivantes :
* `PageSize/Media Size: *Postcard`
* `StpColorPrecision/Color Precision: *Best`
* `StpiShrinkOutput/Shrink Page: *Expand`
* `StpBorderless/Borderless: *True`
* `StpColorCorrection/Color Correction: *Accurate`
* `StpImageType/Image Type: *Photo`

---

## ⚡ Mises à jour rapides du code (sans réinstallation)

Si vous modifiez uniquement le fichier `app.py` sur votre Mac et que vous voulez pousser les changements sans relancer toute la configuration système (durée : 3 secondes) :
```bash
./update_printbox.sh
```

---

## 🔍 Résolution des problèmes fréquents

### 1. L'imprimante ne veut pas imprimer (Statut "Hors ligne")
Vérifiez que la SELPHY est allumée et branchée en USB. Vous pouvez lister les périphériques USB détectés sur la Pi en lançant :
```bash
ssh pi@rpi.local "lsusb"
```
Vous devez voir une ligne contenant `Canon Inc. SELPHY`. Si ce n'est pas le cas, essayez un autre câble USB ou un autre port.

### 2. Le WiFi captif "Print_Box" n'apparaît pas
Vérifiez que les services réseau tournent correctement sur la Pi :
```bash
ssh pi@rpi.local "sudo systemctl status hostapd dnsmasq webprint.service"
```
Si un service est en échec, redémarrez-le :
```bash
ssh pi@rpi.local "sudo systemctl restart hostapd dnsmasq webprint.service"
```

### 3. Les photos s'impriment encore avec un bord blanc
* Assurez-vous d'avoir utilisé un **onglet de navigation privée** sur votre téléphone pour charger le nouveau code JavaScript du navigateur (sinon votre téléphone envoie une image de mauvaise résolution).
* Assurez-vous d'avoir bien sélectionné le format portrait `2:3` ou paysage `3:2` sur la borne (bouton `📐 Format`) pour correspondre au format physique du papier.
