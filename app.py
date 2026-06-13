import os
import shutil
import argparse
import subprocess
import re
import time
import threading
import json
import zipfile
import io
from datetime import datetime
from flask import Flask, request, render_template_string, jsonify, send_from_directory, redirect, g, session, send_file
from PIL import Image, ImageFilter, ImageEnhance, ImageCms, ImageOps

# Support HEIC dynamique
HEIC_SUPPORT = False
try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
    HEIC_SUPPORT = True
except ImportError:
    pass

app = Flask(__name__)
app.secret_key = "BOITE_NOIRE_SECRET_KEY_12345"

TEMP_FOLDER = './images_temporaires'
ORIG_FOLDER = './images_originales'
CONFIG_FILE = './config_boite.json'
BDD_FILE = './bdd_boite.json'

os.makedirs(TEMP_FOLDER, exist_ok=True)
os.makedirs(ORIG_FOLDER, exist_ok=True)

SERVER_START_TIME = time.time()

# Déclaration des variables globales
GLOBAL_LOGS = []
APP_QUEUE = []
GUEST_COUNTERS = {}
TOTAL_HISTORIC_PRINTS = 0
LAST_AUTO_RESET_TIMESTAMP = time.time()

def add_log(message):
    timestamp = datetime.now().strftime("%H:%M:%S")
    log_entry = f"[{timestamp}] {message}"
    GLOBAL_LOGS.append(log_entry)
    print(log_entry)

# Chargement du profil ICC cible CP1500
ICC_PROFILE_PATH = os.path.join(os.path.abspath(os.path.dirname(__file__)), 'ICC-Profile165-CP1500.icc')
TARGET_PROFILE = None
TARGET_PROFILE_BYTES = None

try:
    if os.path.exists(ICC_PROFILE_PATH):
        with open(ICC_PROFILE_PATH, 'rb') as f:
            TARGET_PROFILE_BYTES = f.read()
        TARGET_PROFILE = ImageCms.ImageCmsProfile(io.BytesIO(TARGET_PROFILE_BYTES))
        add_log(f"[COLOR] Profil ICC CP1500 chargé avec succès : {ICC_PROFILE_PATH}")
    else:
        add_log(f"[COLOR] Profil ICC introuvable : {ICC_PROFILE_PATH}")
except Exception as e:
    add_log(f"[COLOR] Erreur lors du chargement du profil ICC : {e}")


# 💾 COUCHE DE PERSISTANCE DE DONNÉES ET FACTORY DEFAULTS
def load_persisted_data():
    global GUEST_COUNTERS, TOTAL_HISTORIC_PRINTS, LAST_AUTO_RESET_TIMESTAMP
    
    # 1. Chargement de la configuration générale (avec valeurs d'usine par défaut)
    app.config['MAX_QUEUE_SIZE'] = 3
    app.config['MAX_PRINTS_PER_GUEST'] = 3
    app.config['QUOTA_ENABLED'] = True
    app.config['PRINTER_SIMU'] = True         # Modifiable à chaud via l'admin désormais
    app.config['PRINTER_NAME'] = "SELPHY"     # Saisie dynamique via l'admin désormais
    app.config['AUTO_RESET_ENABLED'] = False  # Réinitialisation temporelle cyclique
    app.config['AUTO_RESET_INTERVAL'] = 30    # Intervalle par défaut en minutes

    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r') as f:
                config_data = json.load(f)
                app.config['MAX_QUEUE_SIZE'] = config_data.get('max_queue_size', 3)
                app.config['MAX_PRINTS_PER_GUEST'] = config_data.get('max_prints_per_guest', 3)
                app.config['QUOTA_ENABLED'] = config_data.get('quota_enabled', True)
                app.config['PRINTER_SIMU'] = config_data.get('printer_simu', True)
                app.config['PRINTER_NAME'] = config_data.get('printer_name', 'SELPHY')
                app.config['AUTO_RESET_ENABLED'] = config_data.get('auto_reset_enabled', False)
                app.config['AUTO_RESET_INTERVAL'] = config_data.get('auto_reset_interval', 30)
            print("[STORAGE] Configuration globale chargée depuis le disque.")
        except Exception as e:
            print(f"[STORAGE] Erreur chargement config : {e}")
            
    # 2. Chargement de la base de données des compteurs
    if os.path.exists(BDD_FILE):
        try:
            with open(BDD_FILE, 'r') as f:
                bdd_data = json.load(f)
                GUEST_COUNTERS = bdd_data.get('guest_counters', {})
                TOTAL_HISTORIC_PRINTS = bdd_data.get('total_historic_prints', 0)
                LAST_AUTO_RESET_TIMESTAMP = bdd_data.get('last_auto_reset_timestamp', time.time())
            print("[STORAGE] Base de données des compteurs restaurée.")
        except Exception as e:
            print(f"[STORAGE] Erreur chargement BDD : {e}")

def save_config_to_disk():
    try:
        config_data = {
            "max_queue_size": app.config['MAX_QUEUE_SIZE'],
            "max_prints_per_guest": app.config['MAX_PRINTS_PER_GUEST'],
            "quota_enabled": app.config['QUOTA_ENABLED'],
            "printer_simu": app.config['PRINTER_SIMU'],
            "printer_name": app.config['PRINTER_NAME'],
            "auto_reset_enabled": app.config['AUTO_RESET_ENABLED'],
            "auto_reset_interval": app.config['AUTO_RESET_INTERVAL']
        }
        with open(CONFIG_FILE, 'w') as f:
            json.dump(config_data, f, indent=4)
    except Exception as e:
        add_log(f"[STORAGE] Erreur écriture config : {e}")

def save_bdd_to_disk():
    try:
        bdd_data = {
            "guest_counters": GUEST_COUNTERS,
            "total_historic_prints": TOTAL_HISTORIC_PRINTS,
            "last_auto_reset_timestamp": LAST_AUTO_RESET_TIMESTAMP
        }
        with open(BDD_FILE, 'w') as f:
            json.dump(bdd_data, f, indent=4)
    except Exception as e:
        add_log(f"[STORAGE] Erreur écriture BDD : {e}")


# ⚙️ WORKER THREAD : SPOOLER AUTOMATISÉ CUPS / SIMULATION DYNAMIQUE
def printer_queue_worker():
    global APP_QUEUE, TOTAL_HISTORIC_PRINTS
    add_log("[WORKER] Gestionnaire de file d'impression opérationnel.")
    
    while True:
        current_job = None
        for job in APP_QUEUE:
            if job["status"] == "printing":
                current_job = job
                break
                
        if not current_job:
            for job in APP_QUEUE:
                if job["status"] == "pending":
                    current_job = job
                    current_job["status"] = "printing"
                    add_log(f"[WORKER] Spooling pour : {current_job['job_id']}")
                    
                    # Interrogation de la variable de simulation à chaud
                    if app.config.get('PRINTER_SIMU'):
                        current_job["cups_id"] = "SIMU-ID"
                    else:
                        ready_path = os.path.join(TEMP_FOLDER, current_job["filename"])
                        cmd = f'lp -d {app.config["PRINTER_NAME"]} "{ready_path}"'
                        result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
                        
                        if result.returncode == 0:
                            match = re.search(r"request id is ([\w-]+)", result.stdout)
                            cups_id = match.group(1) if match else "UNKNOWN"
                            current_job["cups_id"] = cups_id
                        else:
                            add_log(f"[WORKER] Échec CUPS : {result.stderr}")
                            if current_job in APP_QUEUE:
                                APP_QUEUE.remove(current_job)
                            current_job = None
                    break

        if current_job:
            if app.config.get('PRINTER_SIMU'):
                time.sleep(8)
                if current_job in APP_QUEUE:
                    # Conservation des photos imprimées dans images_temporaires
                    TOTAL_HISTORIC_PRINTS += 1
                    save_bdd_to_disk()
                    APP_QUEUE.remove(current_job)
            else:
                time.sleep(2)
                if current_job not in APP_QUEUE:
                    continue
                    
                cups_id = current_job["cups_id"]
                if cups_id:
                    active_cups_jobs = subprocess.run('lpstat -o', shell=True, capture_output=True, text=True).stdout
                    printer_status = subprocess.run(f'lpstat -p {app.config["PRINTER_NAME"]}', shell=True, capture_output=True, text=True).stdout
                    is_printer_busy = "processing" in printer_status.lower() or "occupé" in printer_status.lower() or "printing" in printer_status.lower()
                    
                    if cups_id not in active_cups_jobs and not is_printer_busy:
                        add_log(f"[WORKER] Impression matérielle terminée : {current_job['job_id']}")
                        if current_job in APP_QUEUE:
                            # Conservation des photos imprimées dans images_temporaires
                            TOTAL_HISTORIC_PRINTS += 1
                            save_bdd_to_disk()
                            APP_QUEUE.remove(current_job)
        else:
            time.sleep(1)


def process_and_queue(original_file, filename, crop_x, crop_y, crop_w, crop_h, crop_rotate):
    try:
        if len(APP_QUEUE) >= app.config.get('MAX_QUEUE_SIZE', 3):
            return False, None, "File pleine. Réessayez dans un instant."

        timestamp_prefix = datetime.now().strftime("%Y%m%d_%H%M%S_")
        orig_path = os.path.join(ORIG_FOLDER, f"{timestamp_prefix}ORIG_{filename}")
        original_file.save(orig_path)

        ready_filename = f"ready_{timestamp_prefix}{filename}"
        if ready_filename.lower().endswith('.heic') or ready_filename.lower().endswith('.heif'):
            ready_filename = os.path.splitext(ready_filename)[0] + ".jpg"

        ready_path = os.path.join(TEMP_FOLDER, ready_filename)
        
        # Ouvrir l'image d'origine avec Pillow et transposer selon l'EXIF
        img = Image.open(orig_path)
        img = ImageOps.exif_transpose(img)
        
        # S'assurer que le mode de l'image est bien RGB
        if img.mode != 'RGB':
            img = img.convert('RGB')

        # Recadrage et rotation côté serveur
        if crop_w > 0 and crop_h > 0:
            try:
                # 1. Rotation (Cropper rotate est horaire, Pillow transpose est anti-horaire)
                if crop_rotate == 90:
                    img = img.transpose(Image.ROTATE_270)
                elif crop_rotate == 180:
                    img = img.transpose(Image.ROTATE_180)
                elif crop_rotate == 270:
                    img = img.transpose(Image.ROTATE_90)
                
                # 2. Crop
                img = img.crop((crop_x, crop_y, crop_x + crop_w, crop_y + crop_h))
                add_log(f"[CROP] Recadrage serveur appliqué : {crop_w}x{crop_h} (rotation: {crop_rotate}°).")
            except Exception as ce:
                add_log(f"[CROP] Erreur lors du recadrage serveur : {ce}")


        # Conversion colorimétrique vers le profil ICC cible
        if TARGET_PROFILE:
            try:
                # Extraire le profil source s'il existe dans l'image d'origine
                icc_data = img.info.get("icc_profile")
                if icc_data:
                    try:
                        src_profile = ImageCms.ImageCmsProfile(io.BytesIO(icc_data))
                        add_log("[COLOR] Profil ICC d'origine extrait avec succès.")
                    except Exception:
                        src_profile = ImageCms.createProfile("sRGB")
                else:
                    src_profile = ImageCms.createProfile("sRGB")

                img = ImageCms.profileToProfile(img, src_profile, TARGET_PROFILE, renderingIntent=0, outputMode="RGB")
                add_log("[COLOR] Conversion de profil ICC appliquée avec succès.")
            except Exception as e:
                add_log(f"[COLOR] Erreur lors de la conversion du profil ICC : {e}")

        # Unsharp Mask (rayon 2, force 150%, seuil 3) pour compenser la diffusion thermique physique
        img_sharpened = img.filter(ImageFilter.UnsharpMask(radius=2, percent=150, threshold=3))
        
        # Enregistrer en embarquant le profil ICC si disponible
        if TARGET_PROFILE_BYTES:
            try:
                img_sharpened.save(ready_path, format="JPEG", quality=96, icc_profile=TARGET_PROFILE_BYTES)
                add_log("[COLOR] Sauvegarde effectuée avec profil ICC CP1500 embarqué.")
            except Exception as e:
                add_log(f"[COLOR] Erreur lors de la sauvegarde avec profil ICC : {e}")
                img_sharpened.save(ready_path, format="JPEG", quality=96)
        else:
            img_sharpened.save(ready_path, format="JPEG", quality=96)
        
        app_job_id = f"JOB-{int(datetime.now().timestamp())}"
        
        job_data = {
            "job_id": app_job_id,
            "filename": ready_filename,
            "status": "pending",
            "cups_id": None,
            "timestamp": datetime.now()
        }
        APP_QUEUE.append(job_data)
        add_log(f"Job enregistré : {app_job_id}")
        return True, app_job_id, "Ajouté à la file."
    except Exception as e:
        return False, None, str(e)


def get_client_mac(ip):
    try:
        with open("/proc/net/arp", "r") as f:
            lines = f.readlines()
            for line in lines[1:]:
                parts = line.split()
                if len(parts) >= 4 and parts[0] == ip:
                    mac = parts[3]
                    if mac != "00:00:00:00:00:00":
                        return mac.upper()
    except Exception:
        pass
    return None


def simplify_user_agent(ua_string):
    if not ua_string: return "Inconnu"
    ua = ua_string.lower()
    if "iphone" in ua or "ipad" in ua: device_os = "iPhone/iPad"
    elif "android" in ua: device_os = "Android"
    elif "macintosh" in ua: device_os = "Mac"
    elif "windows" in ua: device_os = "Windows"
    else: device_os = "Terminal"
    if "crios" in ua or "chrome" in ua: browser = "Chrome"
    elif "fxios" in ua or "firefox" in ua: browser = "Firefox"
    elif "safari" in ua and "chrome" not in ua: browser = "Safari"
    else: browser = "Navigateur"
    return f"{device_os} ({browser})"


def get_printer_status_info(printer_name):
    if app.config.get('PRINTER_SIMU', True):
        return {
            "status": "Simulation",
            "message": "Imprimante virtuelle active",
            "class": "status-ok"
        }
    
    # Vérification de la présence physique de l'imprimante sur le bus USB
    try:
        res_usb = subprocess.run("lsusb", shell=True, capture_output=True, text=True)
        if res_usb.returncode == 0:
            stdout_lower = res_usb.stdout.lower()
            if "canon" not in stdout_lower and "selphy" not in stdout_lower:
                return {
                    "status": "Débranchée",
                    "message": "L'imprimante est hors tension ou déconnectée en USB.",
                    "class": "status-error"
                }
    except Exception:
        # Fallback si lsusb est introuvable (ex: environnement de développement macOS)
        pass

    try:
        result = subprocess.run(f"lpstat -p {printer_name}", shell=True, capture_output=True, text=True)
        if result.returncode != 0:
            return {
                "status": "Erreur CUPS",
                "message": f"Imprimante '{printer_name}' introuvable ou CUPS arrêté.",
                "class": "status-error"
            }
        
        output = result.stdout.strip()
        status_msg = "Prête"
        css_class = "status-ok"
        detail_msg = "Prête à imprimer."
        
        # Extraction du message détaillé après le tiret
        hardware_status = ""
        if " - " in output:
            hardware_status = output.split(" - ", 1)[-1].strip()
            
        lower_output = output.lower()
        lower_hw = hardware_status.lower()
        
        # Mots-clés indiquant une erreur matérielle
        error_keywords = [
            "paper", "papier", "jam", "bourrage", "encre", "ink", "ruban", "ribbon", 
            "empty", "vide", "offline", "hors ligne", "unplugged", "déconnecté", 
            "error", "erreur", "warning", "alerte"
        ]
        
        has_hw_error = any(kw in lower_hw for kw in error_keywords)
        
        if "disabled" in lower_output or "désactivée" in lower_output or "arretee" in lower_output or has_hw_error:
            status_msg = "Erreur"
            css_class = "status-error"
            if hardware_status:
                detail_msg = hardware_status
            else:
                detail_msg = "Imprimante désactivée ou en erreur. Vérifiez papier/encre."
        elif "printing" in lower_output or "imprime" in lower_output or "processing" in lower_output or "occupée" in lower_output:
            status_msg = "Impression"
            css_class = "status-warn"
            if hardware_status:
                detail_msg = hardware_status
            else:
                detail_msg = "Travail en cours de traitement."
        elif "idle" in lower_output or "inactive" in lower_output:
            status_msg = "Prête"
            css_class = "status-ok"
            if hardware_status:
                detail_msg = hardware_status
            else:
                detail_msg = "Prête à imprimer."
        else:
            detail_msg = output
            
        return {
            "status": status_msg,
            "message": detail_msg,
            "class": css_class
        }
    except Exception as e:
        return {
            "status": "Erreur",
            "message": f"Impossible de lire le statut de l'imprimante : {e}",
            "class": "status-error"
        }


# INTERCEPTEUR ET MOTEUR DE NETTOYAGE CHRONOLOGIQUE AUTOMATIQUE
@app.before_request
def resolve_hardware_identity_and_cron():
    global GUEST_COUNTERS, LAST_AUTO_RESET_TIMESTAMP
    host = request.host.split(':')[0].lower()
    if host == '192.168.4.1':
        return redirect('http://print.box/', code=302)

    if request.path.startswith('/static/') or request.path.startswith('/thumbnail/'):
        return

    # ⏱️ CRON INTERNE : Réinitialisation automatique à intervalle régulier
    if app.config.get('AUTO_RESET_ENABLED', False):
        now = time.time()
        interval_seconds = app.config.get('AUTO_RESET_INTERVAL', 30) * 60
        if now - LAST_AUTO_RESET_TIMESTAMP >= interval_seconds:
            for gid in GUEST_COUNTERS:
                GUEST_COUNTERS[gid]["count"] = 0
            LAST_AUTO_RESET_TIMESTAMP = now
            save_bdd_to_disk()
            add_log(f"[CRON] Exécution automatique de la remise à zéro des compteurs invités ({app.config['AUTO_RESET_INTERVAL']} min).")

    client_ip = request.remote_addr
    if client_ip in ['127.0.0.1', 'localhost']:
        g.guest_id = "MAC-MACBOOK-LOCAL"
    else:
        resolved_mac = get_client_mac(client_ip)
        g.guest_id = resolved_mac if resolved_mac else f"IP-{client_ip}"

    ua_friendly = simplify_user_agent(request.headers.get('User-Agent'))
    timestamp_now = datetime.now()
    
    if g.guest_id not in GUEST_COUNTERS:
        GUEST_COUNTERS[g.guest_id] = {
            "count": 0,
            "user_agent": ua_friendly,
            "last_seen": timestamp_now.strftime("%H:%M:%S"),
            "timestamp": timestamp_now.timestamp()
        }
    else:
        GUEST_COUNTERS[g.guest_id]["user_agent"] = ua_friendly
        GUEST_COUNTERS[g.guest_id]["last_seen"] = timestamp_now.strftime("%H:%M:%S")
        GUEST_COUNTERS[g.guest_id]["timestamp"] = timestamp_now.timestamp()


# --- ROUTE CLIENTS ---
@app.route('/', methods=['GET', 'POST'])
def index():
    guest_prints = GUEST_COUNTERS[g.guest_id]["count"]
    
    if request.method == 'POST':
        if app.config['QUOTA_ENABLED'] and guest_prints >= app.config['MAX_PRINTS_PER_GUEST']:
            return jsonify(success=False, message="Votre quota maximal d'impressions est atteint.")
            
        if 'original' not in request.files:
            return jsonify(success=False, message="Payload corrompu")
            
        original_file = request.files['original']
        
        try:
            crop_x = int(request.form.get('crop_x', 0))
            crop_y = int(request.form.get('crop_y', 0))
            crop_w = int(request.form.get('crop_width', 0))
            crop_h = int(request.form.get('crop_height', 0))
            crop_rotate = int(request.form.get('crop_rotate', 0))
        except Exception:
            crop_x, crop_y, crop_w, crop_h, crop_rotate = 0, 0, 0, 0, 0
            
        success, job_id, message = process_and_queue(
            original_file, 
            original_file.filename, 
            crop_x, 
            crop_y, 
            crop_w, 
            crop_h, 
            crop_rotate
        )
        
        if success:
            GUEST_COUNTERS[g.guest_id]["count"] += 1
            save_bdd_to_disk()
            add_log(f"[QUOTA] Appareil {g.guest_id} : {GUEST_COUNTERS[g.guest_id]['count']} tirages.")
            
        return jsonify(success=success, job_id=job_id, message=message)
        
    return render_template_string(HTML_INTERFACE, 
                                  max_queue_size=app.config['MAX_QUEUE_SIZE'],
                                  guest_prints=guest_prints,
                                  max_prints_per_guest=app.config['MAX_PRINTS_PER_GUEST'],
                                  quota_enabled=app.config['QUOTA_ENABLED'])


# --- ROUTE MAINTENANCE & ADMIN ---
@app.route('/admin', methods=['GET', 'POST'])
def admin_panel():
    if request.method == 'POST':
        submitted_pin = request.form.get('pin')
        if submitted_pin == app.config['SYSTEM_PIN']:
            session['admin_logged_in'] = True
            return redirect('/admin')
        else:
            return render_template_string(HTML_LOGIN, error="Code PIN incorrect.")

    if not session.get('admin_logged_in', False):
        return render_template_string(HTML_LOGIN, error=None)

    return render_template_string(HTML_ADMIN, 
                                  max_queue_size=app.config['MAX_QUEUE_SIZE'],
                                  max_prints_per_guest=app.config['MAX_PRINTS_PER_GUEST'],
                                  quota_enabled=app.config['QUOTA_ENABLED'],
                                  printer_simu=app.config['PRINTER_SIMU'],
                                  printer_name=app.config['PRINTER_NAME'],
                                  auto_reset_enabled=app.config['AUTO_RESET_ENABLED'],
                                  auto_reset_interval=app.config['AUTO_RESET_INTERVAL'])


@app.route('/admin/update_config', methods=['POST'])
def update_config():
    if not session.get('admin_logged_in', False): return jsonify(success=False)
    payload = request.json
    if not payload: return jsonify(success=False)
    try:
        if 'max_queue_size' in payload: app.config['MAX_QUEUE_SIZE'] = int(payload['max_queue_size'])
        if 'max_prints_per_guest' in payload: app.config['MAX_PRINTS_PER_GUEST'] = int(payload['max_prints_per_guest'])
        if 'quota_enabled' in payload: app.config['QUOTA_ENABLED'] = bool(payload['quota_enabled'])
        if 'printer_simu' in payload: app.config['PRINTER_SIMU'] = bool(payload['printer_simu'])
        if 'printer_name' in payload: app.config['PRINTER_NAME'] = str(payload['printer_name']).strip()
        if 'auto_reset_enabled' in payload: app.config['AUTO_RESET_ENABLED'] = bool(payload['auto_reset_enabled'])
        if 'auto_reset_interval' in payload: app.config['AUTO_RESET_INTERVAL'] = int(payload['auto_reset_interval'])
        
        save_config_to_disk()
        add_log("[CONFIG] Paramètres mis à jour à chaud sur le stockage disque.")
        return jsonify(success=True)
    except Exception as e: return jsonify(success=False, message=str(e))


# 🗑️ ACTION DE DESTRUCTION DE SAUVEGARDE (RESET USINE)
@app.route('/admin/wipe_data', methods=['POST'])
def wipe_data():
    if not session.get('admin_logged_in', False): return jsonify(success=False)
    global GUEST_COUNTERS, TOTAL_HISTORIC_PRINTS, LAST_AUTO_RESET_TIMESTAMP
    try:
        # Suppression physique des BDD et fichiers de configurations JSON
        for f in [CONFIG_FILE, BDD_FILE]:
            if os.path.exists(f): os.remove(f)
            
        # Purge complète des répertoires de stockage d'images
        for folder in [TEMP_FOLDER, ORIG_FOLDER]:
            if os.path.exists(folder):
                shutil.rmtree(folder)
            os.makedirs(folder, exist_ok=True)
            
        # Restauration à chaud des variables en mémoire vive
        GUEST_COUNTERS = {}
        TOTAL_HISTORIC_PRINTS = 0
        LAST_AUTO_RESET_TIMESTAMP = time.time()
        load_persisted_data()
        
        add_log("[SYSTEM] Réinitialisation d'usine effectuée : Fichiers de sauvegarde purgés.")
        return jsonify(success=True, message="Fichiers de sauvegarde effacés et répertoires d'images vidés avec succès.")
    except Exception as e:
        return jsonify(success=False, message=str(e))


@app.route('/admin/download_backup', methods=['GET'])
def download_backup():
    if not session.get('admin_logged_in', False):
        return "Accès interdit", 403
    try:
        memory_file = io.BytesIO()
        with zipfile.ZipFile(memory_file, 'w', zipfile.ZIP_DEFLATED) as zip_file:
            # 1. Ajout des images originales (dossier 'originales')
            for root, dirs, files in os.walk(ORIG_FOLDER):
                for file in files:
                    file_path = os.path.join(root, file)
                    arcname = os.path.join('originales', os.path.relpath(file_path, ORIG_FOLDER))
                    zip_file.write(file_path, arcname)
            
            # 2. Ajout des images recadrées/temporaires imprimées (dossier 'recadrees')
            for root, dirs, files in os.walk(TEMP_FOLDER):
                # On ne sauvegarde que les fichiers physiques présents
                for file in files:
                    file_path = os.path.join(root, file)
                    arcname = os.path.join('recadrees', os.path.relpath(file_path, TEMP_FOLDER))
                    zip_file.write(file_path, arcname)
        
        memory_file.seek(0)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"backup_photos_{timestamp}.zip"
        
        add_log(f"[ADMIN] Sauvegarde ZIP (originales et recadrées) générée et téléchargée.")
        
        return send_file(
            memory_file,
            mimetype='application/zip',
            as_attachment=True,
            download_name=filename
        )
    except Exception as e:
        add_log(f"[ADMIN] Erreur lors de la génération du ZIP : {e}")
        return f"Erreur : {e}", 500


@app.route('/admin/get_stats', methods=['GET'])
def get_stats():
    if not session.get('admin_logged_in', False): return jsonify(forbidden=True)
    uptime_seconds = int(time.time() - SERVER_START_TIME)
    hours, remainder = divmod(uptime_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    
    printer_name = app.config.get('PRINTER_NAME', 'SELPHY')
    printer_status = get_printer_status_info(printer_name)
    
    return jsonify(
        total_historic_prints=TOTAL_HISTORIC_PRINTS,
        server_uptime=f"{hours}h {minutes}m {seconds}s",
        printer_status=printer_status
    )


@app.route('/admin/get_counters', methods=['GET'])
def get_counters():
    if not session.get('admin_logged_in', False): return jsonify([])
    raw_list = [{
        "guest_id": gid, "count": info["count"], "user_agent": info["user_agent"],
        "last_seen": info["last_seen"], "timestamp": info["timestamp"], "is_me": (gid == g.guest_id)
    } for gid, info in GUEST_COUNTERS.items()]
    return jsonify(sorted(raw_list, key=lambda x: x['timestamp'], reverse=True))


@app.route('/admin/reset_counter', methods=['POST'])
def reset_counter():
    if not session.get('admin_logged_in', False): return jsonify(success=False)
    target_id = (request.json or {}).get('guest_id')
    if target_id in GUEST_COUNTERS:
        GUEST_COUNTERS[target_id]["count"] = 0
        save_bdd_to_disk()
        return jsonify(success=True)
    return jsonify(success=False)


@app.route('/admin/reset_all_counters', methods=['POST'])
def reset_all_counters():
    if not session.get('admin_logged_in', False): return jsonify(success=False)
    for gid in GUEST_COUNTERS: GUEST_COUNTERS[gid]["count"] = 0
    save_bdd_to_disk()
    return jsonify(success=True)


@app.route('/get_status/<job_id>', methods=['GET'])
def get_status(job_id):
    for index, job in enumerate(APP_QUEUE):
        if job["job_id"] == job_id: return jsonify(status=job["status"], position=index + 1, total=len(APP_QUEUE))
    return jsonify(status="completed", position=0, total=len(APP_QUEUE))


@app.route('/get_queue', methods=['GET'])
def get_queue(): return jsonify([{"job_id": j["job_id"], "status": j["status"]} for j in APP_QUEUE])


@app.route('/cancel_job/<job_id>', methods=['POST'])
def cancel_job(job_id):
    global APP_QUEUE
    target_job = next((j for j in APP_QUEUE if j["job_id"] == job_id), None)
    if not target_job: return jsonify(success=False, message="Tâche introuvable.")
    if target_job["status"] == "printing" and target_job["cups_id"] and not app.config.get('PRINTER_SIMU'):
        subprocess.run(f'cancel {target_job["cups_id"]}', shell=True, capture_output=True)
    try: os.remove(os.path.join(TEMP_FOLDER, target_job["filename"]))
    except: pass
    APP_QUEUE.remove(target_job)
    return jsonify(success=True, message="Impression annulée.")


@app.route('/restart', methods=['POST'])
def restart_server():
    if not session.get('admin_logged_in', False): return jsonify(success=False)
    def kill(): time.sleep(1); os._exit(0)
    threading.Thread(target=kill, daemon=True).start()
    return jsonify(success=True)


@app.route('/get_logs', methods=['GET'])
def get_logs(): return jsonify(GLOBAL_LOGS)


@app.route('/convert_heic', methods=['POST'])
def convert_heic():
    if not HEIC_SUPPORT:
        return jsonify(success=False, message="Le support HEIC n'est pas activé sur le serveur (pillow-heif manquant).")
    
    if 'photo' not in request.files:
        return jsonify(success=False, message="Aucun fichier d'image fourni.")
        
    file = request.files['photo']
    if not file or file.filename == '':
        return jsonify(success=False, message="Nom de fichier invalide.")
        
    try:
        # Ouvrir le fichier HEIC avec Pillow
        img = Image.open(file)
        img = ImageOps.exif_transpose(img)
        
        # S'assurer que le mode de l'image est RGB
        if img.mode != 'RGB':
            img = img.convert('RGB')
            
        # Redimensionner la prévisualisation si trop grande pour la fluidité du navigateur
        max_preview_dim = 2000
        if img.width > max_preview_dim or img.height > max_preview_dim:
            if img.width > img.height:
                new_w = max_preview_dim
                new_h = int(img.height * (max_preview_dim / img.width))
            else:
                new_h = max_preview_dim
                new_w = int(img.width * (max_preview_dim / img.height))
            img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
            add_log(f"[HEIC] Image de prévisualisation redimensionnée à {new_w}x{new_h} pour fluidité.")
            
        output = io.BytesIO()
        img.save(output, format='JPEG', quality=90)
        output.seek(0)
        
        add_log(f"[HEIC] Fichier {file.filename} converti avec succès en JPEG pour le recadrage.")
        return send_file(output, mimetype='image/jpeg')
    except Exception as e:
        add_log(f"[HEIC] Échec de la conversion de {file.filename} : {e}")
        return jsonify(success=False, message=f"Erreur de conversion HEIC : {str(e)}")


# --- INTERFACE FLASK LOGIN DESIGN ---
HTML_LOGIN = '''
<!DOCTYPE html>
<html lang="fr">
<head>
    <meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Accès Sécurisé Admin</title>
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, sans-serif; background: #f5f5f7; display: flex; align-items: center; justify-content: center; height: 100vh; margin:0;}
        .login-card { background: white; padding: 30px; border-radius: 24px; box-shadow: 0 12px 40px rgba(0,0,0,0.05); text-align: center; max-width: 360px; width: 100%; box-sizing: border-box; }
        h2 { font-size: 20px; margin-top: 0; color: #1d1d1f; }
        input[type="password"] { width: 100%; padding: 14px; border-radius: 12px; border: 1px solid #d2d2d7; font-size: 16px; font-weight: bold; text-align: center; box-sizing: border-box; margin-bottom: 15px; letter-spacing: 4px;}
        button { width:100%; background:#0071e3; color:white; padding:14px; border-radius:12px; font-size:15px; font-weight:600; border:none; cursor:pointer;}
        .err { color:#ff3b30; font-size:13px; font-weight:600; margin-bottom:15px;}
    </style>
</head>
<body>
    <div class="login-card">
        <h2>🔐 Administration Borne</h2>
        {% if error %}<div class="err">{{ error }}</div>{% endif %}
        <form method="POST">
            <input type="password" name="pin" placeholder="••••" required autocomplete="off">
            <button type="submit">Se connecter</button>
        </form>
    </div>
</body>
</html>
'''

# --- INTERFACE DESIGN CLIENT (INCLUANT ROTATION EN AMONT) ---
HTML_INTERFACE = '''
<!DOCTYPE html>
<html lang="fr">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>Print Studio</title>
    <link rel="stylesheet" href="/static/cropper.min.css">
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, sans-serif; text-align: center; padding: 15px 10px; background: #f5f5f7; color: #1d1d1f; margin: 0; }
        .card { max-width: 480px; margin: 0 auto; background: white; padding: 25px 20px; border-radius: 24px; box-shadow: 0 12px 40px rgba(0,0,0,0.04); box-sizing: border-box; }
        .tabs { display: flex; background: #e3e3e8; padding: 4px; border-radius: 12px; margin-bottom: 25px; }
        .tab-btn { flex: 1; padding: 10px; border: none; border-radius: 9px; font-size: 13px; font-weight: 600; background: transparent; color: #424245; cursor: pointer; }
        .tab-btn.active { background: white; color: #1d1d1f; box-shadow: 0 2px 8px rgba(0,0,0,0.1); }
        .tab-content { display: none; }
        .tab-content.active { display: block; }
        .btn { background: #0071e3; color: white; padding: 16px 32px; border-radius: 30px; font-size: 16px; font-weight: 600; display: inline-block; cursor: pointer; border: none; margin-top: 15px; }
        .btn-danger { background: #ff3b30; font-size: 13px; padding: 10px 18px; border-radius: 20px; margin-top: 10px; color: white; border: none; cursor: pointer; font-weight: 600; }
        .btn-danger-table { background: #ff3b30; font-size: 12px; padding: 8px 14px; border-radius: 20px; color: white; border: none; }
        .btn-secondary { background: #e8e8ed; color: #1d1d1f; margin-right: 5px; margin-left: 5px; }
        input[type="file"] { display: none !important; }
        .btn.disabled, label.disabled { background: #d2d2d7 !important; color: #86868b !important; cursor: not-allowed !important; opacity: 0.6 !important; pointer-events: none !important; }
        .alert-box { display: none; background: #ffebeb; color: #d9383a; border: 1px solid #f8cbcb; padding: 14px; border-radius: 16px; font-size: 14px; font-weight: 600; margin: 15px 0; }
        .crop-actions-wrapper { display: none; margin-top: 15px; margin-bottom: 15px; justify-content: space-between; gap: 6px; flex-wrap: nowrap; width: 100%; box-sizing: border-box; }
        .crop-actions-wrapper .btn { flex: 1; padding: 12px 4px; font-size: 14px; border-radius: 20px; margin: 0; white-space: nowrap; min-width: 0; text-align: center; }
        .crop-container { display: none; margin: 20px 0; max-height: 52vh; background: #000; border-radius: 14px; overflow: hidden; }
        .crop-container img { max-width: 100%; display: block; }
        .loader-wrapper { display: none; margin: 25px auto; }
        .spinner { border: 4px solid rgba(0,0,0,0.05); width: 44px; height: 44px; border-radius: 50%; border-left-color: #0071e3; animation: spin 0.8s linear infinite; margin: 0 auto; }
        @keyframes spin { 0% { transform: rotate(0deg); } 100% { transform: rotate(360deg); } }
        #status-hud { margin-top: 25px; font-weight: 700; font-size: 17px; }
        #quota-hud { font-size: 13px; color: #86868b; font-weight: 700; margin-top: -15px; margin-bottom: 20px; background: #e8e8ed; padding: 6px 12px; border-radius: 30px; display: inline-block; }
        .queue-item { display: flex; align-items: center; background: #f5f5f7; padding: 12px; border-radius: 16px; margin-bottom: 12px; justify-content: space-between; }
    </style>
</head>
<body>
    <div class="card">
        <div class="tabs">
            <button class="tab-btn active" onclick="switchTab('upload')">📸 Photo</button>
            <button class="tab-btn" onclick="switchTab('queue')">📋 File d'impression</button>
        </div>
        
        <div id="tab-upload" class="tab-content active">
            <div class="alert-box" id="queue-alert">File saturée.</div>
            <p style="color:#86868b; font-size: 14px;" id="instructions-text">Ajouter une photo à imprimer</p>
            <div id="quota-hud">Analyse...</div>
            
            <label class="btn" id="select-label">
                📸 Sélectionner une photo
                <input type="file" id="file-input" accept="image/*,.heic,.heif">
            </label>
            
            <div class="crop-actions-wrapper" id="crop-actions">
                <button class="btn btn-secondary" id="btn-cancel">❌ Annuler</button>
                <button class="btn btn-secondary" id="btn-aspect">📐 Format</button>
                <button class="btn btn-secondary" id="btn-rotate">🔄 Pivoter</button>
                <button class="btn" id="btn-print">🖨️ Imprimer</button>
            </div>

            <div class="crop-container" id="crop-wrapper"><img id="image-to-crop"></div>
            
            <div class="loader-wrapper" id="loader-block">
                <div class="spinner"></div>
                <div style="color: #0071e3; font-size: 15px; font-weight: 600; margin-top: 12px;" id="loader-status-text">Traitement...</div>
            </div>
            <div id="status-hud"></div>
        </div>

        <div id="tab-queue" class="tab-content">
            <div id="queue-container"><div style="color:#86868b; padding:30px 0;">Aucune impression en cours.</div></div>
        </div>
    </div>

    <script src="/static/cropper.min.js"></script>
    <script>
        const fileInput = document.getElementById('file-input');
        const selectLabel = document.getElementById('select-label');
        const cropWrapper = document.getElementById('crop-wrapper');
        const imageToCrop = document.getElementById('image-to-crop');
        const cropActions = document.getElementById('crop-actions');
        const btnPrint = document.getElementById('btn-print');
        const btnCancel = document.getElementById('btn-cancel');
        const btnAspect = document.getElementById('btn-aspect');
        const btnRotate = document.getElementById('btn-rotate');
        const queueAlert = document.getElementById('queue-alert');
        const loaderBlock = document.getElementById('loader-block');
        const loaderStatusText = document.getElementById('loader-status-text');
        const statusHud = document.getElementById('status-hud');
        const quotaHud = document.getElementById('quota-hud');
        const instructionsText = document.getElementById('instructions-text');
        const queueContainer = document.getElementById('queue-container');

        let cropper = null; let isPortraitTarget = false; let currentAspectRatio = 2 / 3;
        let trackingJobId = null; let trackingInterval = null;

        let clientPrints = parseInt("{{ guest_prints }}");
        let maxPrintsPerGuest = parseInt("{{ max_prints_per_guest }}");
        let currentMaxQueueSize = parseInt("{{ max_queue_size }}");
        let isQuotaEnabled = ("{{ quota_enabled }}" === "True");

        document.addEventListener('DOMContentLoaded', () => { renderQuotaUI(); updateQueueView(); });

        function renderQuotaUI() {
            if (isQuotaEnabled) {
                quotaHud.innerText = `Photos imprimées avec cet appareil : ${clientPrints} / ${maxPrintsPerGuest}`;
                if (clientPrints >= maxPrintsPerGuest) {
                    selectLabel.classList.add('disabled'); fileInput.disabled = true; btnPrint.disabled = true;
                    queueAlert.style.display = 'block'; queueAlert.innerText = "⚠️ Limite matérielle de l'événement atteinte.";
                }
            } else {
                quotaHud.innerText = `Photos imprimées avec cet appareil : ${clientPrints}`;
                selectLabel.classList.remove('disabled'); fileInput.disabled = false; queueAlert.style.display = 'none';
            }
        }

        function switchTab(tabName) {
            document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
            document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
            if (tabName === 'upload') {
                document.querySelectorAll('.tab-btn')[0].classList.add('active'); document.getElementById('tab-upload').classList.add('active');
            } else if (tabName === 'queue') {
                document.querySelectorAll('.tab-btn')[1].classList.add('active'); document.getElementById('tab-queue').classList.add('active'); updateQueueView();
            }
        }

        // 🌟 ARCHITECTURE DE RE-CREATION : Factory d'initialisation propre et isolée
        function initCropperInstance(targetRatio) {
            if (cropper) {
                cropper.destroy(); // Suppression totale de l'ancien conteneur dégradé
            }
            
            cropper = new Cropper(imageToCrop, { 
                aspectRatio: targetRatio, 
                viewMode: 1, 
                autoCropArea: 1, 
                background: false
            });
        }

        function isHeicFile(file) {
            if (!file) return false;
            const name = file.name ? file.name.toLowerCase() : '';
            const type = file.type ? file.type.toLowerCase() : '';
            return name.endsWith('.heic') || name.endsWith('.heif') || type === 'image/heic' || type === 'image/heif';
        }

        fileInput.addEventListener('change', function() {
            if (!this.files.length || (isQuotaEnabled && clientPrints >= maxPrintsPerGuest)) return;
            statusHud.innerHTML = ''; const file = this.files[0];
            
            if (isHeicFile(file)) {
                selectLabel.style.display = 'none'; 
                loaderBlock.style.display = 'block'; 
                loaderStatusText.innerText = "Conversion HEIC...";
                instructionsText.innerText = "Traitement du fichier HEIC...";
                
                const formData = new FormData();
                formData.append('photo', file);
                
                fetch('/convert_heic', {
                    method: 'POST',
                    body: formData
                })
                .then(async response => {
                    const contentType = response.headers.get("content-type");
                    if (contentType && contentType.includes("application/json")) {
                        const errData = await response.json();
                        throw new Error(errData.message || "Erreur de conversion.");
                    }
                    if (!response.ok) {
                        throw new Error("Erreur serveur ou réseau.");
                    }
                    return response.blob();
                })
                .then(blob => {
                    loaderBlock.style.display = 'none';
                    const reader = new FileReader();
                    reader.onload = function(e) {
                        cropWrapper.style.display = 'block'; 
                        cropActions.style.display = 'flex';
                        instructionsText.innerText = "Cadrez votre photo.";
                        imageToCrop.src = e.target.result;
                        
                        imageToCrop.onload = function() {
                            isPortraitTarget = imageToCrop.naturalHeight > imageToCrop.naturalWidth;
                            currentAspectRatio = isPortraitTarget ? (2 / 3) : (3 / 2);
                            setTimeout(() => {
                                initCropperInstance(currentAspectRatio);
                            }, 50);
                            imageToCrop.onload = null;
                        };
                    };
                    reader.readAsDataURL(blob);
                })
                .catch(err => {
                    alert("Erreur lors de la conversion du fichier HEIC : " + err.message);
                    resetUI();
                });
            } else {
                const reader = new FileReader();
                reader.onload = function(e) {
                    selectLabel.style.display = 'none'; 
                    cropWrapper.style.display = 'block'; 
                    cropActions.style.display = 'flex';
                    instructionsText.innerText = "Cadrez votre photo.";
                    imageToCrop.src = e.target.result;
                    
                    imageToCrop.onload = function() {
                        isPortraitTarget = imageToCrop.naturalHeight > imageToCrop.naturalWidth;
                        currentAspectRatio = isPortraitTarget ? (2 / 3) : (3 / 2);
                        setTimeout(() => {
                            initCropperInstance(currentAspectRatio);
                        }, 50);
                        imageToCrop.onload = null;
                    };
                };
                reader.readAsDataURL(file);
            }
        });

        // 🌟 BOUTON DE FORMAT : Bascule l'aspect ratio du cadre sans tourner la photo
        btnAspect.addEventListener('click', () => {
            if (!cropper) return;
            currentAspectRatio = (currentAspectRatio === (2 / 3)) ? (3 / 2) : (2 / 3);
            cropper.setAspectRatio(currentAspectRatio);
        });

        // 🌟 BOUTON DE ROTATION : Pivote l'image de 90° dans le cadre sans changer l'aspect ratio à l'écran
        btnRotate.addEventListener('click', () => {
            if (!cropper) return;
            cropper.rotate(90);
        });

        btnCancel.addEventListener('click', () => { resetUI(); });

        btnPrint.addEventListener('click', async function() {
            if (!cropper || (isQuotaEnabled && clientPrints >= maxPrintsPerGuest)) return;
            cropWrapper.style.display = 'none'; cropActions.style.display = 'none'; loaderBlock.style.display = 'block'; loaderStatusText.innerText = "Envoi...";
            
            // Récupérer les coordonnées de recadrage par rapport à l'image d'origine
            const cropData = cropper.getData(true);
            
            const formData = new FormData();
            formData.append('original', fileInput.files[0]);
            formData.append('crop_x', cropData.x);
            formData.append('crop_y', cropData.y);
            formData.append('crop_width', cropData.width);
            formData.append('crop_height', cropData.height);
            formData.append('crop_rotate', cropData.rotate);
            
            try {
                const response = await fetch('/', { method: 'POST', body: formData });
                const payload = await response.json();
                if (payload.success && payload.job_id) startTracking(payload.job_id);
                else { alert(payload.message); resetUI(); }
            } catch (err) { alert("Erreur réseau."); resetUI(); }
        });

        function startTracking(jobId) {
            if (trackingInterval) clearInterval(trackingInterval); trackingJobId = jobId; selectLabel.style.display = 'none';
            trackingInterval = setInterval(async () => {
                try {
                    const res = await fetch('/get_status/' + trackingJobId); const data = await res.json();
                    if (data.status === "pending") {
                        loaderBlock.style.display = 'block'; loaderStatusText.innerText = "En attente...";
                        statusHud.innerHTML = `<div style="color:#ff9500; font-weight:700; margin-bottom:10px;">⏳ Position dans la file : ${data.position} / ${data.total}</div><button class="btn btn-danger" onclick="cancelJob('${trackingJobId}')">❌ Annuler l'impression</button>`;
                    } else if (data.status === "printing") {
                        loaderBlock.style.display = 'block'; loaderStatusText.innerText = "Impression en cours...";
                        statusHud.innerHTML = '<span style="color:#0071e3;">🖨️ Traitement matériel...</span>';
                    } else if (data.status === "completed") {
                        clearInterval(trackingInterval); loaderBlock.style.display = 'none';
                        statusHud.innerHTML = '<div style="color:#34c759; font-size:20px; font-weight:700;">🎉 Impression terminée !</div>';
                        clientPrints++; renderQuotaUI(); setTimeout(resetUI, 4000);
                    }
                } catch (e) {}
            }, 1500);
        }

        async function updateQueueView() {
            try {
                const res = await fetch('/get_queue'); const queue = await res.json();
                if (queue.length >= currentMaxQueueSize && (!isQuotaEnabled || clientPrints < maxPrintsPerGuest)) {
                    selectLabel.classList.add('disabled'); fileInput.disabled = true; btnPrint.disabled = true;
                    if (trackingJobId === null) { queueAlert.style.display = 'block'; queueAlert.innerText = `⚠️ File d'impression saturée (${queue.length}/${currentMaxQueueSize}).`; }
                } else if (!isQuotaEnabled || clientPrints < maxPrintsPerGuest) {
                    selectLabel.classList.remove('disabled'); fileInput.disabled = false; btnPrint.disabled = false; queueAlert.style.display = 'none';
                }
                if (queue.length === 0) { queueContainer.innerHTML = '<div style="color:#86868b; padding:30px 0;">Aucune impression en cours.</div>'; return; }
                let html = '';
                queue.forEach(item => {
                    const isPrinting = item.status === 'printing';
                    html += `<div class="queue-item"><div><span style="font-size:11px; font-family:monospace; color:#86868b;">${item.job_id}</span><br><span style="font-size:14px; font-weight:600; color:${isPrinting?'#0071e3':'#ff9500'};">${isPrinting?'Impression en cours':"En attente"}</span></div><button class="btn-danger-table" onclick="cancelJob('${item.job_id}')">Retirer</button></div>`;
                });
                queueContainer.innerHTML = html;
            } catch (e) {}
        }

        async function cancelJob(jobId) {
            if (!confirm("Annuler cette tâche ?")) return;
            try { const res = await fetch('/cancel_job/' + jobId, { method: 'POST' }); alert((await res.json()).message); if (jobId === trackingJobId) resetUI(); updateQueueView(); } catch (e) {}
        }

        function resetUI() {
            if (trackingInterval) clearInterval(trackingInterval); trackingJobId = null; loaderBlock.style.display = 'none'; cropWrapper.style.display = 'none'; cropActions.style.display = 'none';
            if (!isQuotaEnabled || clientPrints < maxPrintsPerGuest) selectLabel.style.display = 'inline-block';
            instructionsText.innerText = "Ajouter une photo à imprimer"; fileInput.value = ''; statusHud.innerHTML = '';
            absoluteRotation = 0; 
            if (cropper) { cropper.destroy(); cropper = null; } updateQueueView();
        }
    </script>
</body>
</html>
'''

# --- INTERFACE DESIGN EXCLUSIVE ADMIN (/ADMIN) ---
HTML_ADMIN = '''
<!DOCTYPE html>
<html lang="fr">
<head>
    <meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Console d'Administration Système</title>
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, sans-serif; background: #f5f5f7; color: #1d1d1f; padding: 30px 15px; margin: 0; text-align: center; }
        .admin-card { max-width: 760px; margin: 0 auto 25px auto; background: white; padding: 30px; border-radius: 24px; box-shadow: 0 12px 40px rgba(0,0,0,0.04); box-sizing: border-box; text-align: left; }
        .stats-banner { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 15px; margin-bottom: 25px; text-align: center;}
        .stat-box { background: #e8e8ed; padding: 15px; border-radius: 16px; display: flex; flex-direction: column; justify-content: center;}
        .stat-box span { display:block; font-size: 22px; font-weight: bold; color: #0071e3; margin-top: 5px;}
        .status-badge { display: inline-block; padding: 4px 12px; border-radius: 20px; font-size: 13px; font-weight: bold; color: white; margin-top: 5px; align-self: center;}
        .status-ok { background: #34c759; }
        .status-warn { background: #ff9500; }
        .status-error { background: #ff3b30; }
        h2 { margin-top: 0; color: #1d1d1f; border-bottom: 1px solid #e3e3e8; padding-bottom: 12px; font-size: 19px; }
        .control-group { margin-bottom: 20px; }
        .flex-inputs { display: flex; gap: 15px; margin-bottom: 15px; align-items: flex-end; flex-wrap: wrap;}
        .flex-child { flex: 1; min-width: 140px; }
        label { display: block; font-weight: 600; font-size: 13px; margin-bottom: 8px; color: #424245; }
        input[type="number"], input[type="text"], select { width: 100%; padding: 12px; border-radius: 10px; border: 1px solid #d2d2d7; font-size: 15px; box-sizing: border-box; font-weight: 600; background: white;}
        .btn { display: block; width: 100%; background: #0071e3; color: white; padding: 14px; border-radius: 12px; font-size: 14px; font-weight: 600; border: none; cursor: pointer; }
        .btn-danger { background: #ff3b30; }
        .btn-mini { background: #ff9500; padding: 6px 12px; border-radius: 8px; font-size: 11px; font-weight: 700; border: none; color: white; cursor: pointer; }
        .btn-mini-danger { background: #ff3b30; }
        table { width: 100%; border-collapse: collapse; text-align: left; font-size: 13px; margin-top: 15px;}
        th { background: #f5f5f7; padding: 10px; font-weight: 600; border-bottom: 1px solid #d2d2d7; }
        td { padding: 12px 10px; border-bottom: 1px solid #e3e3e8; }
        .row-me { background: #f0f7ff; font-weight: 600; color: #0071e3; }
        .console-output { background: #1d1d1f; color: #30d158; font-family: monospace; padding: 15px; border-radius: 12px; max-height: 180px; overflow-y: auto; font-size: 12px; white-space: pre-wrap; margin-top: 8px; }
    </style>
</head>
<body>

    <div class="admin-card stats-banner">
        <div class="stat-box">Impression(s) totale(s) historique<span><div id="stat-total-prints">...</div></span></div>
        <div class="stat-box">État de l'imprimante<span><span id="printer-status-badge" class="status-badge status-ok">Analyse...</span></span><div id="printer-status-msg" style="font-size: 11px; color: #86868b; margin-top: 5px; font-weight: 500;">...</div></div>
        <div class="stat-box">Disponibilité continue (Uptime)<span><div id="stat-uptime">...</div></span></div>
    </div>

    <div class="admin-card">
        <h2>⚙️ Configurations de la Borne</h2>
        <div class="control-group">
            <div class="flex-inputs">
                <div class="flex-child">
                    <label for="queue-size-input">Capacité file d'impression</label>
                    <input type="number" id="queue-size-input" value="{{ max_queue_size }}" min="1">
                </div>
                <div class="flex-child">
                    <label for="quota-enable-select">Régulation des Quotas</label>
                    <select id="quota-enable-select" onchange="toggleQuotaInputView()">
                        <option value="true" {% if quota_enabled %}selected{% endif %}>Activés (Limite stricte)</option>
                        <option value="false" {% if not quota_enabled %}selected{% endif %}>Désactivés (Illimités)</option>
                    </select>
                </div>
                <div class="flex-child" id="quota-limit-box">
                    <label for="quota-input">Photos max / appareil</label>
                    <input type="number" id="quota-input" value="{{ max_prints_per_guest }}" min="1">
                </div>
            </div>

            <div class="flex-inputs" style="border-top: 1px dashed #e3e3e8; padding-top: 15px;">
                <div class="flex-child">
                    <label for="printer-simu-select">Mode Opérationnel Moteur</label>
                    <select id="printer-simu-select">
                        <option value="true" {% if printer_simu %}selected{% endif %}>Imprimante Simulée (Virtuelle)</option>
                        <option value="false" {% if not printer_simu %}selected{% endif %}>Imprimante Physique (CUPS Hardware)</option>
                    </select>
                </div>
                <div class="flex-child">
                    <label for="printer-name-input">Nom de l'imprimante (Géré par udev)</label>
                    <input type="text" id="printer-name-input" value="{{ printer_name }}" readonly style="background-color: #e8e8ed; color: #86868b; cursor: not-allowed;">
                </div>
            </div>

            <div class="flex-inputs" style="border-top: 1px dashed #e3e3e8; padding-top: 15px;">
                <div class="flex-child">
                    <label for="auto-reset-select">Remise à zéro automatique</label>
                    <select id="auto-reset-select" onchange="toggleAutoResetView()">
                        <option value="false" {% if not auto_reset_enabled %}selected{% endif %}>Désactivée</option>
                        <option value="true" {% if auto_reset_enabled %}selected{% endif %}>Activée à intervalle régulier</option>
                    </select>
                </div>
                <div class="flex-child" id="auto-reset-interval-box">
                    <label for="auto-reset-interval-input">Intervalle de purge (minutes)</label>
                    <input type="number" id="auto-reset-interval-input" value="{{ auto_reset_interval }}" min="1">
                </div>
            </div>

            <button class="btn" style="margin-top:15px;" onclick="saveAdminConfig()">💾 Enregistrer les paramètres à chaud</button>
        </div>
    </div>

    <div class="admin-card">
        <div style="display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #e3e3e8; padding-bottom: 12px; margin-bottom: 15px;">
            <h2>👥 Appareils connectés (Suivi Matériel)</h2>
            <button class="btn btn-mini btn-danger" onclick="resetAllCounters()">🔄 Tout réinitialiser</button>
        </div>
        <div style="width:100%; overflow-x:auto;">
            <table>
                <thead>
                    <tr><th>Adresse MAC / Identifiant</th><th>Type d'Appareil</th><th>Dernière Activité 🕒</th><th>Tirages</th><th>Action</th></tr>
                </thead>
                <tbody id="counters-table-body">
                    <tr><td colspan="5" style="color: #86868b; text-align: center;">Aucun appareil détecté.</td></tr>
                </tbody>
            </table>
        </div>
    </div>

    <div class="admin-card">
        <h2>🔄 Opérations Serveur</h2>
        <a href="/admin/download_backup" class="btn" style="background:#34c759; margin-bottom:12px; text-decoration:none; display:block; text-align:center; box-sizing:border-box;">📥 Télécharger toutes les photos originales (.zip)</a>
        
        <button class="btn" style="background:#8e8e93; margin-bottom:12px;" onclick="restartServer()">🔄 Relancer le processus Python (Flask)</button>
        
        <button class="btn btn-danger" onclick="wipeDataStore()">🗑️ Effacer les fichiers de sauvegarde (Reset d'usine complet)</button>
        
        <h2 style="margin-top: 25px;">🖥️ Logs Applicatifs (Syslog)</h2>
        <div class="console-output" id="console-div">Chargement des flux...</div>
    </div>

    <script>
        const consoleDiv = document.getElementById('console-div');
        const countersTableBody = document.getElementById('counters-table-body');

        document.addEventListener('DOMContentLoaded', () => {
            toggleQuotaInputView(); toggleAutoResetView(); fetchLogs(); fetchCounters(); fetchSystemStats();
            setInterval(fetchLogs, 2500); setInterval(fetchCounters, 2500); setInterval(fetchSystemStats, 1000);
        });

        function toggleQuotaInputView() {
            const isEnabled = document.getElementById('quota-enable-select').value === "true";
            document.getElementById('quota-limit-box').style.visibility = isEnabled ? "visible" : "hidden";
        }

        function toggleAutoResetView() {
            const isEnabled = document.getElementById('auto-reset-select').value === "true";
            document.getElementById('auto-reset-interval-box').style.visibility = isEnabled ? "visible" : "hidden";
        }

        async function saveAdminConfig() {
            try {
                await fetch('/admin/update_config', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        max_queue_size: document.getElementById('queue-size-input').value,
                        max_prints_per_guest: document.getElementById('quota-input').value,
                        quota_enabled: document.getElementById('quota-enable-select').value === "true",
                        printer_simu: document.getElementById('printer-simu-select').value === "true",
                        printer_name: document.getElementById('printer-name-input').value,
                        auto_reset_enabled: document.getElementById('auto-reset-select').value === "true",
                        auto_reset_interval: document.getElementById('auto-reset-interval-input').value
                    })
                });
                location.reload();
            } catch(e) { alert("Erreur réseau de sauvegarde."); }
        }

        // 🌟 COMMANDE AJAX DE PURGE COMPLÈTE DU STORAGE DISQUE
        async function wipeDataStore() {
            if (!confirm("🚨 ATTENTION : Cette opération va effacer définitivement l'ensemble des fichiers JSON de configuration, réinitialiser la BDD des compteurs et vider TOUTES les photos originales et recadrées enregistrées. Confirmer ?")) return;
            try {
                const res = await fetch('/admin/wipe_data', { method: 'POST' });
                const reply = await res.json();
                alert(reply.message);
                location.reload();
            } catch(e) { alert("Erreur lors de la purge."); }
        }

        async function fetchSystemStats() {
            try {
                const res = await fetch('/admin/get_stats'); const data = await res.json();
                document.getElementById('stat-total-prints').innerText = data.total_historic_prints;
                document.getElementById('stat-uptime').innerText = data.server_uptime;
                
                const badge = document.getElementById('printer-status-badge');
                const msg = document.getElementById('printer-status-msg');
                if (badge && msg && data.printer_status) {
                    badge.innerText = data.printer_status.status;
                    badge.className = 'status-badge ' + data.printer_status.class;
                    msg.innerText = data.printer_status.message;
                }
            } catch(e) {}
        }

        async function fetchCounters() {
            try {
                const response = await fetch('/admin/get_counters'); const list = await response.json();
                if (list.length === 0) { countersTableBody.innerHTML = '<tr><td colspan="5" style="color:#86868b; text-align:center;">Aucun appareil connecté.</td></tr>'; return; }
                let html = '';
                list.forEach(item => {
                    const rowClass = item.is_me ? 'class="row-me"' : '';
                    html += `<tr ${rowClass}><td style="font-family:monospace; font-size:12px;">${item.guest_id}</td><td>${item.user_agent}</td><td>${item.last_seen}</td><td style="font-weight:bold; text-align:center;">${item.count}</td><td><button class="btn-mini ${item.is_me?'':'btn-mini-danger'}" onclick="resetIndividualCounter('${item.guest_id}')">Réinitialiser</button></td></tr>`;
                });
                countersTableBody.innerHTML = html;
            } catch (e) {}
        }

        async function resetIndividualCounter(guestId) {
            if (!confirm(`Réinitialiser l'appareil ${guestId} ?`)) return;
            try { await fetch('/admin/reset_counter', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ guest_id: guestId }) }); fetchCounters(); } catch (e) {}
        }

        async function resetAllCounters() {
            if (!confirm("Remettre à zéro tous les compteurs ?")) return;
            try { await fetch('/admin/reset_all_counters', { method: 'POST' }); fetchCounters(); } catch(e) {}
        }

        async function restartServer() {
            if (!confirm("Relancer le serveur Flask ?")) return;
            try { await fetch('/restart', { method: 'POST' }); setTimeout(() => { window.location.href = "http://print.box/"; }, 4000); } catch (e) {}
        }

        async function fetchLogs() { try { consoleDiv.innerText = (await (await fetch('/get_logs')).json()).join('\\n'); consoleDiv.scrollTop = consoleDiv.scrollHeight; } catch (e) {} }
    </script>
</body>
</html>
'''

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Serveur Web-to-Print Quota Control Local.")
    parser.add_argument('--pin', type=str, default="1234", help="Code PIN d'accès exclusif à la console /admin")
    args = parser.parse_known_args()[0]
    
    app.config['SYSTEM_PIN'] = args.pin
    
    # 🌟 Initialisation, création et chargement de la couche de persistance
    load_persisted_data()
    
    add_log(f"[INIT] Démarrage de l'instance d'événement. Code PIN d'administration requis : {args.pin}")
    if HEIC_SUPPORT:
        add_log("[INIT] Support HEIC activé via pillow-heif.")
    else:
        add_log("[WARNING] pillow-heif non installé. Conversion HEIC désactivée.")
    
    # Lancement asynchrone du spooler d'impression
    threading.Thread(target=printer_queue_worker, daemon=True).start()
    
    app.run(host='0.0.0.0', port=8000)