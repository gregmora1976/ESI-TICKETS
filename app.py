from flask import Flask, render_template, jsonify, request, send_file, abort, redirect, url_for
from pathlib import Path
import json, webbrowser, os, urllib.request, urllib.parse
import csv, re, time
import hashlib, threading
from io import StringIO, BytesIO
import smtplib
from email.mime.text import MIMEText
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from datetime import datetime
import html

APP_DIR = Path(__file__).resolve().parent
DATA_DIR = APP_DIR / 'data'
CONFIG_FILE = DATA_DIR / 'config.json'
TICKETS_SUB = 'tickets'
FILES_SUB = 'fichiers'
SUPABASE_URL = (os.getenv("SUPABASE_URL") or "").rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY") or ""
SUPABASE_BUCKET = os.getenv("SUPABASE_BUCKET", "uploads")

app = Flask(__name__, template_folder='templates', static_folder='static')

# Cache court + verrou pour éviter de relancer plusieurs OCR lourds sur le même PDF.
_RECEPTION_OCR_LOCK = threading.Lock()
_RECEPTION_PDF_CACHE = {}
_RECEPTION_PDF_CACHE_TTL = 300  # 5 minutes
_RECEPTION_PDF_CACHE_MAX = 20


def _reception_cache_get(pdf_bytes):
    key = hashlib.sha256(pdf_bytes).hexdigest()
    item = _RECEPTION_PDF_CACHE.get(key)
    if not item:
        return key, None
    if time.time() - item.get('ts', 0) > _RECEPTION_PDF_CACHE_TTL:
        _RECEPTION_PDF_CACHE.pop(key, None)
        return key, None
    return key, dict(item.get('parsed') or {})


def _reception_cache_set(key, parsed):
    if len(_RECEPTION_PDF_CACHE) >= _RECEPTION_PDF_CACHE_MAX:
        oldest = min(_RECEPTION_PDF_CACHE.items(), key=lambda kv: kv[1].get('ts', 0))[0]
        _RECEPTION_PDF_CACHE.pop(oldest, None)
    _RECEPTION_PDF_CACHE[key] = {'ts': time.time(), 'parsed': dict(parsed)}


# -----------------------------------------------------------------------------
# Google Sheet public - suivi fournisseur caisserie
# -----------------------------------------------------------------------------
GOOGLE_SHEET_PUBLIC_ID = os.getenv(
    "GOOGLE_SHEET_PUBLIC_ID",
    "2PACX-1vQSiTSLN-AtXoa4GrscgSM_2VwFzO12Bh-UFyUKNLihyRZSocciqe8OHHIZCKvs5r77ynFqd5NZI29Q"
)
GOOGLE_SHEET_PUBHTML_URL = (
    f"https://docs.google.com/spreadsheets/d/e/{GOOGLE_SHEET_PUBLIC_ID}/pubhtml"
)
GOOGLE_SHEET_CSV_BASE_URL = (
    f"https://docs.google.com/spreadsheets/d/e/{GOOGLE_SHEET_PUBLIC_ID}/pub"
)

# Petit cache mémoire pour éviter de relire la page des onglets à chaque clic.
_GOOGLE_GIDS_CACHE = {"gids": [], "expires_at": 0}


def _http_get_text(url, timeout=10):
    req = urllib.request.Request(
        url,
        method="GET",
        headers={
            "User-Agent": "Mozilla/5.0 ESI-Tickets/1.0",
            "Accept": "text/html,text/csv,*/*"
        }
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        charset = resp.headers.get_content_charset() or "utf-8"
        return raw.decode(charset, errors="replace")


def _google_sheet_gids(force_refresh=False):
    now = time.time()
    if (
        not force_refresh
        and _GOOGLE_GIDS_CACHE["gids"]
        and now < _GOOGLE_GIDS_CACHE["expires_at"]
    ):
        return list(_GOOGLE_GIDS_CACHE["gids"])

    html_page = _http_get_text(GOOGLE_SHEET_PUBHTML_URL, timeout=10)
    gids = re.findall(r"gid=(\d+)", html_page)

    # Supprime les doublons tout en conservant l'ordre du classeur publié.
    gids = list(dict.fromkeys(gids))

    _GOOGLE_GIDS_CACHE["gids"] = gids
    _GOOGLE_GIDS_CACHE["expires_at"] = now + 600  # 10 minutes
    return list(gids)


def get_caisse_fournisseur_status(caisse_ref):
    """
    Recherche une caisse dans tous les onglets publiés du Google Sheet.

    Exemple : 100872-01
      - colonne B = 100872
      - colonne C = 1
      - colonne D = état fournisseur
    """
    caisse_ref = _as_text(caisse_ref).strip()
    if not caisse_ref or "-" not in caisse_ref:
        return {
            "success": False,
            "caisse": caisse_ref,
            "error": "Format de caisse invalide"
        }

    try:
        reference_recherchee, numero_recherche = caisse_ref.rsplit("-", 1)
        reference_recherchee = reference_recherchee.strip()
        numero_recherche = str(int(numero_recherche.strip()))
    except Exception:
        return {
            "success": False,
            "caisse": caisse_ref,
            "error": "Format de caisse invalide"
        }

    try:
        gids = _google_sheet_gids()
    except Exception as e:
        print(f"[GOOGLE SHEET] Impossible de récupérer les onglets : {e}")
        return {
            "success": False,
            "caisse": caisse_ref,
            "error": "Suivi fournisseur indisponible"
        }

    if not gids:
        return {
            "success": False,
            "caisse": caisse_ref,
            "error": "Aucun onglet fournisseur disponible"
        }

    for gid in gids:
        csv_url = (
            f"{GOOGLE_SHEET_CSV_BASE_URL}?gid={urllib.parse.quote(str(gid), safe='')}"
            "&single=true&output=csv"
        )

        try:
            csv_text = _http_get_text(csv_url, timeout=10)
        except Exception as e:
            print(f"[GOOGLE SHEET] Erreur lecture gid={gid} : {e}")
            continue

        reader = csv.reader(StringIO(csv_text))
        for row in reader:
            if len(row) < 4:
                continue

            reference = _as_text(row[1]).strip()
            numero = _as_text(row[2]).strip()
            etat = _as_text(row[3]).strip()

            if not reference or not numero:
                continue

            try:
                numero_normalise = str(int(float(numero.replace(",", "."))))
            except Exception:
                continue

            if (
                reference == reference_recherchee
                and numero_normalise == numero_recherche
            ):
                return {
                    "success": True,
                    "caisse": caisse_ref,
                    "etat": etat or "Non renseigné",
                    "gid": str(gid)
                }

    return {
        "success": False,
        "caisse": caisse_ref,
        "error": "Caisse introuvable dans le suivi fournisseur"
    }

def safe_filename(name):
    """Nettoie le nom du fichier pour Supabase tout en gardant le vrai nom affiché côté appli."""
    name = str(name or "fichier")
    return "".join(
        c if c.isalnum() or c in "._-" else "_"
        for c in name
    )


def supabase_upload_bytes(storage_path, content, content_type="application/octet-stream"):
    """Envoie un fichier dans Supabase Storage sans dépendre du SDK Python."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise RuntimeError("Variables SUPABASE_URL ou SUPABASE_SERVICE_KEY manquantes")

    safe_path = urllib.parse.quote(storage_path, safe="/")
    url = f"{SUPABASE_URL}/storage/v1/object/{SUPABASE_BUCKET}/{safe_path}"

    req = urllib.request.Request(
        url,
        data=content,
        method="POST",
        headers={
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "apikey": SUPABASE_KEY,
            "Content-Type": content_type or "application/octet-stream",
            "x-upsert": "true"
        }
    )

    print("[SUPABASE UPLOAD URL]", url)
    print("[SUPABASE UPLOAD BUCKET]", SUPABASE_BUCKET)
    print("[SUPABASE UPLOAD PATH]", safe_path)

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        print(f"[SUPABASE UPLOAD ERROR] HTTP {e.code} - {e.reason} - {body}")
        raise

def supabase_download_bytes(storage_path):
    """Télécharge un fichier depuis Supabase Storage."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise RuntimeError("Variables SUPABASE_URL ou SUPABASE_SERVICE_KEY manquantes")

    safe_path = urllib.parse.quote(storage_path, safe="/")
    url = f"{SUPABASE_URL}/storage/v1/object/{SUPABASE_BUCKET}/{safe_path}"

    req = urllib.request.Request(
        url,
        method="GET",
        headers={
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "apikey": SUPABASE_KEY
        }
    )

    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


def supabase_signed_download_url(storage_path, expires_in=300):
    """Crée une URL signée Supabase Storage pour éviter de faire transiter le fichier par Render."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise RuntimeError("Variables SUPABASE_URL ou SUPABASE_SERVICE_KEY manquantes")

    safe_path = urllib.parse.quote(storage_path, safe="/")
    url = f"{SUPABASE_URL}/storage/v1/object/sign/{SUPABASE_BUCKET}/{safe_path}"

    payload = json.dumps({"expiresIn": int(expires_in)}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "apikey": SUPABASE_KEY,
            "Content-Type": "application/json"
        }
    )

    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.loads(resp.read().decode("utf-8", errors="replace"))

    signed = body.get("signedURL") or body.get("signedUrl") or body.get("url")
    if not signed:
        raise RuntimeError(f"Réponse URL signée invalide : {body}")
    if signed.startswith("http"):
        return signed
    return SUPABASE_URL + "/storage/v1" + signed

def choose_shared_folder():
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        path = filedialog.askdirectory(title="Choisis le dossier partagé ESI Tickets")
        root.destroy()
        if path:
            return path
    except Exception:
        pass
    return ''

def load_config():
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text(encoding='utf-8'))
        except Exception:
            pass
    return {}

def save_config(cfg):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding='utf-8')

def ensure_shared_root():
    root = APP_DIR
    (root / TICKETS_SUB).mkdir(parents=True, exist_ok=True)
    (root / FILES_SUB).mkdir(parents=True, exist_ok=True)
    return root

def tickets_dir():
    return ensure_shared_root() / TICKETS_SUB

def files_dir():
    return ensure_shared_root() / FILES_SUB

def ticket_file(ticket_id):
    return tickets_dir() / f'{ticket_id}.json'

def ticket_folder(ticket_id):
    path = files_dir() / ticket_id
    path.mkdir(parents=True, exist_ok=True)
    return path



def _as_text(value, default=''):
    if value is None:
        return default
    return str(value)


def supabase_rest_request(method, table, query='', payload=None, prefer=None):
    """Appelle l'API REST Supabase Database sans dépendre du SDK Python."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise RuntimeError("Variables SUPABASE_URL ou SUPABASE_SERVICE_KEY manquantes")

    url = f"{SUPABASE_URL}/rest/v1/{table}"
    if query:
        url += "?" + query.lstrip('?')

    data = None
    headers = {
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "apikey": SUPABASE_KEY,
        "Content-Type": "application/json",
    }
    if prefer:
        headers["Prefer"] = prefer
    elif method.upper() in ("POST", "PATCH", "DELETE"):
        headers["Prefer"] = "return=representation"

    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    req = urllib.request.Request(url, data=data, method=method.upper(), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            if not body:
                return None
            return json.loads(body)
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        print(f"[SUPABASE DB ERROR] {method} {url} -> HTTP {e.code} {e.reason} {body}")
        raise RuntimeError(f"Erreur Supabase DB HTTP {e.code}: {body or e.reason}")


def init_db():
    """Vérifie simplement que les tables Supabase répondent."""
    try:
        supabase_rest_request("GET", "tickets", "select=id&limit=1")
        print("[SUPABASE DB] Connexion OK")
    except Exception as e:
        print(f"[SUPABASE DB] Connexion impossible : {e}")


def _ticket_to_db_row(ticket):
    return {
        "id": _as_text(ticket.get("id")),
        "module": _as_text(ticket.get("module")),
        "status": _as_text(ticket.get("status")),
        "created_at": _as_text(ticket.get("createdAt")),
        "updated_at": _as_text(ticket.get("updatedAt")),
        "dossier": _as_text(ticket.get("dossier")),
        "ref": _as_text(ticket.get("ref")),
        "preteur": _as_text(ticket.get("preteur")),
        "expo": _as_text(ticket.get("expo")),
        "objet": _as_text(ticket.get("objet")),
        "charge_projet": _as_text(ticket.get("chargeProjet")),
        "type_caisse": _as_text(ticket.get("typeCaisse")),
        "dimensions": _as_text(ticket.get("dimensions")),
        "date_emballage": _as_text(ticket.get("dateEmballage")),
        "prix_devis": _as_text(ticket.get("prixDevis")),
        "date_rdv": _as_text(ticket.get("dateRdv")),
        "heure_rdv": _as_text(ticket.get("heureRdv")),
        "lieu_rdv": _as_text(ticket.get("lieuRdv")),
        "contact_rdv": _as_text(ticket.get("contactRdv")),
        "commentaire": _as_text(ticket.get("commentaire")),
        "validated_at": _as_text(ticket.get("validatedAt")),
        "raw_json": ticket,
    }


def _ticket_from_db_row(row):
    # raw_json permet de conserver des donnees metier additionnelles sans ajouter
    # immediatement de nouvelles colonnes Supabase (ex. analyse d'un bon d'enlevement).
    raw = row.get("raw_json")
    ticket = dict(raw) if isinstance(raw, dict) else {}

    # Les colonnes principales restent la source de verite pour les champs historiques.
    ticket.update({
        "id": row.get("id") or "",
        "module": row.get("module") or "",
        "status": row.get("status") or "",
        "createdAt": row.get("created_at") or "",
        "updatedAt": row.get("updated_at") or "",
        "dossier": row.get("dossier") or "",
        "ref": row.get("ref") or "",
        "preteur": row.get("preteur") or "-",
        "expo": row.get("expo") or "-",
        "objet": row.get("objet") or "-",
        "chargeProjet": row.get("charge_projet") or "-",
        "typeCaisse": row.get("type_caisse") or "-",
        "dimensions": row.get("dimensions") or "-",
        "dateEmballage": row.get("date_emballage") or "-",
        "prixDevis": row.get("prix_devis") or "-",
        "dateRdv": row.get("date_rdv") or "-",
        "heureRdv": row.get("heure_rdv") or "-",
        "lieuRdv": row.get("lieu_rdv") or "-",
        "contactRdv": row.get("contact_rdv") or "-",
        "commentaire": row.get("commentaire") or "",
        "validatedAt": row.get("validated_at") or "",
    })
    return ticket


def _fiche_to_db_row(ticket_id, fiche):
    return {
        "ticket_id": ticket_id,
        "longueur": _as_text(fiche.get("longueur")),
        "largeur": _as_text(fiche.get("largeur")),
        "hauteur": _as_text(fiche.get("hauteur")),
        "dimensions_ext": _as_text(fiche.get("dimensionsExt")),
        "prix_achat": _as_text(fiche.get("prixAchat")),
        "prix_cession": _as_text(fiche.get("prixCession")),
        "type_caisse_fiche": _as_text(fiche.get("typeCaisseFiche")),
        "bilan_carbone": _as_text(fiche.get("bilanCarbone")),
        "poids": _as_text(fiche.get("poids")),
        "choix_caissier": _as_text(fiche.get("choixCaissier")),
        "localisation": _as_text(fiche.get("localisation")),
    }


def _fiche_from_db_row(row):
    return {
        "longueur": row.get("longueur") or "",
        "largeur": row.get("largeur") or "",
        "hauteur": row.get("hauteur") or "",
        "dimensionsExt": row.get("dimensions_ext") or "",
        "prixAchat": row.get("prix_achat") or "",
        "prixCession": row.get("prix_cession") or "",
        "typeCaisseFiche": row.get("type_caisse_fiche") or "",
        "bilanCarbone": row.get("bilan_carbone") or "",
        "poids": row.get("poids") or "",
        "choixCaissier": row.get("choix_caissier") or "",
        "localisation": row.get("localisation") or "",
    }


def _add_file_to_ticket(ticket, f):
    item = {
        "name": f.get("filename") or "",
        "size": f.get("size") or 0,
        "path": f.get("storage_path") or ""
    }
    if f.get("kind") == "gestionnaire":
        ticket.setdefault("managerSheets", []).append(item)
    else:
        ticket.setdefault("files", []).append(item)


def _attach_children(ticket):
    """Charge les enfants d'un seul ticket. Utilisé pour les actions ciblées."""
    tid = ticket.get("id")
    if not tid:
        return ticket

    safe_tid = urllib.parse.quote(tid, safe='')

    fiches = supabase_rest_request(
        "GET",
        "fiches",
        f"select=*&ticket_id=eq.{safe_tid}&limit=1"
    ) or []
    if fiches:
        ticket["fiche"] = _fiche_from_db_row(fiches[0])

    rows = supabase_rest_request(
        "GET",
        "ticket_files",
        f"select=*&ticket_id=eq.{safe_tid}&order=uploaded_at.asc"
    ) or []

    ticket["files"] = []
    ticket["managerSheets"] = []
    for f in rows:
        _add_file_to_ticket(ticket, f)

    return ticket


def _chunks(values, size=100):
    for i in range(0, len(values), size):
        yield values[i:i + size]


def _in_filter(values):
    # Format PostgREST : in.(DEM-001,DEM-002). Les ids internes ne contiennent pas de virgule.
    return urllib.parse.quote(",".join(values), safe=",-_")


def list_tickets(status=None, limit=None):
    query = "select=*&order=created_at.desc"
    if status:
        query += "&status=eq." + urllib.parse.quote(status, safe='')
    if limit:
        query += "&limit=" + str(int(limit))

    rows = supabase_rest_request("GET", "tickets", query) or []
    tickets = [_ticket_from_db_row(row) for row in rows]

    by_id = {t.get("id"): t for t in tickets if t.get("id")}
    ids = list(by_id.keys())
    if not ids:
        return tickets

    # Initialisation des listes pour éviter les champs absents côté interface
    for t in tickets:
        t["files"] = []
        t["managerSheets"] = []

    # Chargement groupé des fiches : au lieu de 1 requête par ticket
    for part in _chunks(ids):
        fiches = supabase_rest_request(
            "GET",
            "fiches",
            "select=*&ticket_id=in.(" + _in_filter(part) + ")"
        ) or []
        for f in fiches:
            tid = f.get("ticket_id")
            if tid in by_id:
                by_id[tid]["fiche"] = _fiche_from_db_row(f)

    # Chargement groupé des fichiers : au lieu de 1 requête par ticket
    for part in _chunks(ids):
        rows_files = supabase_rest_request(
            "GET",
            "ticket_files",
            "select=*&ticket_id=in.(" + _in_filter(part) + ")&order=uploaded_at.asc"
        ) or []
        for f in rows_files:
            tid = f.get("ticket_id")
            if tid in by_id:
                _add_file_to_ticket(by_id[tid], f)

    return tickets


def next_id(prefix):
    safe_prefix = urllib.parse.quote(prefix + '-*', safe='*-')
    rows = supabase_rest_request(
        "GET",
        "tickets",
        f"select=id&id=like.{safe_prefix}&order=id.desc&limit=5000"
    ) or []
    nums = []
    for row in rows:
        try:
            nums.append(int(str(row.get("id", "")).split('-')[1]))
        except Exception:
            pass
    mx = max(nums) if nums else 0
    return f"{prefix}-{mx+1:03d}"


def save_ticket(ticket):
    if not ticket.get("id"):
        raise RuntimeError("Ticket sans ID")

    ticket.setdefault("updatedAt", datetime.now().isoformat())

    # Upsert du ticket principal
    supabase_rest_request(
        "POST",
        "tickets",
        "on_conflict=id",
        [_ticket_to_db_row(ticket)],
        prefer="resolution=merge-duplicates,return=minimal"
    )

    ticket_id = ticket.get("id")
    safe_tid = urllib.parse.quote(ticket_id, safe='')

    # Fiche gestionnaire
    fiche = ticket.get("fiche") or {}
    if fiche:
        supabase_rest_request(
            "POST",
            "fiches",
            "on_conflict=ticket_id",
            [_fiche_to_db_row(ticket_id, fiche)],
            prefer="resolution=merge-duplicates,return=minimal"
        )
    else:
        supabase_rest_request("DELETE", "fiches", f"ticket_id=eq.{safe_tid}", prefer="return=minimal")

    # Fichiers : on remplace la liste associée au ticket
    supabase_rest_request("DELETE", "ticket_files", f"ticket_id=eq.{safe_tid}", prefer="return=minimal")

    file_rows = []
    for fs in ticket.get("files") or []:
        if fs and fs.get("name"):
            file_rows.append({
                "ticket_id": ticket_id,
                "kind": "demandeur",
                "filename": _as_text(fs.get("name")),
                "size": fs.get("size") or 0,
                "storage_path": _as_text(fs.get("path")),
            })

    manager_sheets = list(ticket.get("managerSheets") or [])
    legacy = ticket.get("managerSheet")
    if legacy and isinstance(legacy, dict) and legacy.get("name"):
        if not any(x.get("name") == legacy.get("name") for x in manager_sheets):
            manager_sheets.append(legacy)

    for fs in manager_sheets:
        if fs and fs.get("name"):
            file_rows.append({
                "ticket_id": ticket_id,
                "kind": "gestionnaire",
                "filename": _as_text(fs.get("name")),
                "size": fs.get("size") or 0,
                "storage_path": _as_text(fs.get("path")),
            })

    if file_rows:
        supabase_rest_request("POST", "ticket_files", "", file_rows, prefer="return=minimal")

    print("[SUPABASE DB] Ticket sauvegardé", ticket_id)


def load_ticket(ticket_id):
    safe_tid = urllib.parse.quote(ticket_id, safe='')
    rows = supabase_rest_request("GET", "tickets", f"select=*&id=eq.{safe_tid}&limit=1") or []
    if not rows:
        return None
    return _attach_children(_ticket_from_db_row(rows[0]))


# -----------------------------------------------------------------------------
# Demande d'enlevement - analyse automatique des bons PDF
# -----------------------------------------------------------------------------
def _extract_enlevement_pdf_text(pdf_bytes):
    """Extrait le texte d'un bon d'enlevement natif ou scanne."""
    try:
        from pypdf import PdfReader
    except Exception as e:
        raise RuntimeError(
            "Le module pypdf n'est pas installe. Ajoute pypdf a requirements.txt."
        ) from e

    try:
        reader = PdfReader(BytesIO(pdf_bytes))
    except Exception as e:
        raise ValueError(f"PDF illisible : {e}")

    pages_text = []
    for page in reader.pages:
        try:
            pages_text.append(page.extract_text() or "")
        except Exception:
            pages_text.append("")

    text = "\n".join(pages_text).strip()
    ocr_used = False

    if len(text) < 80:
        print("[ENLEVEMENT OCR] Texte natif insuffisant, lancement de l'OCR")
        try:
            from pdf2image import convert_from_bytes
            import pytesseract
        except Exception as e:
            raise RuntimeError(
                "OCR indisponible. Verifie pdf2image, pytesseract, Pillow, "
                "tesseract-ocr et poppler-utils."
            ) from e

        with _RECEPTION_OCR_LOCK:
            try:
                images = convert_from_bytes(
                    pdf_bytes, dpi=200, grayscale=True, thread_count=1
                )
            except Exception as e:
                raise RuntimeError(
                    "Impossible de convertir le PDF en image pour l'OCR."
                ) from e

            ocr_pages = []
            total_pages = len(images)
            for index, image in enumerate(images, start=1):
                try:
                    page_text = pytesseract.image_to_string(
                        image, lang="fra", config="--psm 6"
                    )
                except Exception as e:
                    raise RuntimeError("Echec OCR Tesseract.") from e
                print(f"[ENLEVEMENT OCR] Page {index}/{total_pages} analysee")
                ocr_pages.append(page_text or "")
                try:
                    image.close()
                except Exception:
                    pass
            images.clear()
            text = "\n".join(ocr_pages).strip()
        ocr_used = True

    if not text:
        raise ValueError("Aucun texte exploitable trouve dans le bon d'enlevement.")

    return text, len(reader.pages), ocr_used


def _clean_ocr_line(value):
    return re.sub(r"\s+", " ", _as_text(value)).strip(" \t|")


def _extract_enlevement_items(instructions_text):
    """Extrait les items/references de la zone Instructions avec deduplication."""
    lines = [_clean_ocr_line(x) for x in instructions_text.splitlines()]
    lines = [x for x in lines if x]
    items = []
    seen_refs = set()

    def previous_description(index):
        for j in range(index - 1, max(-1, index - 4), -1):
            candidate = lines[j]
            if re.search(r"merci|rappel|storage|instruction|assur[eé]|valeur", candidate, re.I):
                continue
            if not re.search(r"\bREF\s*[:.-]", candidate, re.I):
                return candidate
        return ""

    def add_item(reference, description="", dimensions=""):
        ref = re.sub(r"^[^A-Za-z0-9]+|[^A-Za-z0-9_-]+$", "", reference or "")
        if len(ref) < 2 or ref.upper() in seen_refs:
            return
        seen_refs.add(ref.upper())
        qty = ""
        designation = description.strip()
        mqty = re.match(r"^\s*(\d+)\s*[xX]?\s+(.+)$", designation)
        if mqty:
            qty = mqty.group(1)
            designation = mqty.group(2).strip()
        items.append({
            "reference": ref,
            "quantite": qty,
            "designation": designation,
            "dimensions": dimensions.strip(),
        })

    # Cas explicite : REF : AN001
    for i, line in enumerate(lines):
        m = re.search(r"\bREF(?:ERENCE)?\s*[:.=-]\s*([A-Za-z0-9][A-Za-z0-9_-]{1,})", line, re.I)
        if not m:
            continue
        dims = ""
        if i + 1 < len(lines):
            md = re.search(r"(\d+(?:[.,]\d+)?\s*[xX×]\s*\d+(?:[.,]\d+)?(?:\s*[xX×]\s*\d+(?:[.,]\d+)?)?\s*(?:cm|mm|m)?)", lines[i + 1], re.I)
            if md:
                dims = md.group(1)
        add_item(m.group(1), previous_description(i), dims)

    # Cas sans libelle REF : LDV_1047 // Dims. 114 x 71 cm
    for i, line in enumerate(lines):
        m = re.match(r"^([A-Za-z][A-Za-z0-9]*[_-][A-Za-z0-9_-]+)\b", line)
        if not m:
            continue
        dims = ""
        md = re.search(r"(?:Dims?\.?\s*)?([0-9]+(?:[.,][0-9]+)?\s*[xX×]\s*[0-9]+(?:[.,][0-9]+)?(?:\s*[xX×]\s*[0-9]+(?:[.,][0-9]+)?)?\s*(?:cm|mm|m)?)", line, re.I)
        if md:
            dims = md.group(1)
        add_item(m.group(1), previous_description(i), dims)

    return items


def _extract_enlevement_pdf(pdf_bytes):
    """Analyse un bon d'enlevement et retourne les donnees utiles au planning reception."""
    text, page_count, ocr_used = _extract_enlevement_pdf_text(pdf_bytes)
    clean_text = text.replace("\r", "")

    def first_match(pattern, flags=re.I | re.M, group=1):
        m = re.search(pattern, clean_text, flags)
        return _clean_ocr_line(m.group(group)) if m else ""

    numero_bon = first_match(
        r"Num[eé]ro\s+de\s+r[eé]f[eé]r(?:ence)?\s*[:.-]?\s*([A-Za-z0-9_-]+)"
    )
    client = first_match(r"^\s*Client\s*[:.-]?\s*([^\n]+)$")
    coordinateur = first_match(r"Coordinateur\s*[:.-]?\s*([^\n]+)")
    exhibition = first_match(r"Exhibition\s*[:.-]?\s*([^\n]+)")

    # Zone Programme du chantier : on prend en priorite la premiere date qui suit ce titre.
    date_enlevement = ""
    programme = re.search(r"Programme\s+du\s+chantier(.*?)(?:Instructions|OBSERVATIONS|Assur[eé]\s+par)", clean_text, re.I | re.S)
    if programme:
        md = re.search(r"\b([0-3]?\d/[01]?\d/(?:\d{2}|\d{4}))\b", programme.group(1))
        if md:
            date_enlevement = md.group(1)

    # Bloc Instructions uniquement, pour ne pas confondre signatures et references.
    instructions = ""
    mi = re.search(
        r"Instructions\s*(.*?)(?:Assur[eé]\s+par|OBSERVATIONS\s+ou\s+RESERVES|OBSERVATIONS|Valeur\s+assur[eé]e)",
        clean_text, re.I | re.S
    )
    if mi:
        instructions = mi.group(1).strip()

    items = _extract_enlevement_items(instructions)

    return {
        "numero_bon": numero_bon,
        "client": client,
        "coordinateur": coordinateur,
        "exhibition": exhibition,
        "date_enlevement": date_enlevement,
        "instructions": instructions,
        "items": items,
        "references": [x.get("reference") for x in items if x.get("reference")],
        "page_count": page_count,
        "ocr_used": ocr_used,
        "raw_text": clean_text,
    }


def _analyse_enlevement_ticket_background(ticket_id, pdf_bytes):
    """Analyse le bon apres creation du ticket, sans bloquer le demandeur."""
    try:
        print(f"[ENLEVEMENT] Analyse asynchrone demarree pour {ticket_id}")
        parsed = _extract_enlevement_pdf(pdf_bytes)
        ticket = load_ticket(ticket_id)
        if not ticket:
            print(f"[ENLEVEMENT] Ticket introuvable apres creation : {ticket_id}")
            return

        ticket["enlevement"] = {
            **parsed,
            "analysis_status": "ready",
            "analysis_error": "",
            "analysed_at": datetime.now().isoformat(),
        }

        # Recopie quelques champs dans le modele historique pour faciliter les listes existantes.
        if parsed.get("client"):
            ticket["dossier"] = parsed["client"]
        if parsed.get("numero_bon"):
            ticket["ref"] = parsed["numero_bon"]
        if parsed.get("exhibition"):
            ticket["expo"] = parsed["exhibition"]
            ticket["objet"] = parsed["exhibition"]
        if parsed.get("date_enlevement"):
            try:
                dt = datetime.strptime(parsed["date_enlevement"], "%d/%m/%y")
                ticket["dateRdv"] = dt.strftime("%Y-%m-%d")
            except ValueError:
                try:
                    dt = datetime.strptime(parsed["date_enlevement"], "%d/%m/%Y")
                    ticket["dateRdv"] = dt.strftime("%Y-%m-%d")
                except ValueError:
                    pass

        ticket["updatedAt"] = datetime.now().isoformat()
        save_ticket(ticket)
        print(
            f"[ENLEVEMENT] Analyse terminee pour {ticket_id}: "
            f"{len(parsed.get('items') or [])} item(s), OCR={parsed.get('ocr_used')}"
        )
    except Exception as e:
        print(f"[ENLEVEMENT] Erreur analyse {ticket_id}: {e}")
        try:
            ticket = load_ticket(ticket_id)
            if ticket:
                current = ticket.get("enlevement") or {}
                current.update({
                    "analysis_status": "error",
                    "analysis_error": str(e),
                    "analysed_at": datetime.now().isoformat(),
                })
                ticket["enlevement"] = current
                ticket["updatedAt"] = datetime.now().isoformat()
                save_ticket(ticket)
        except Exception as save_error:
            print(f"[ENLEVEMENT] Impossible d'enregistrer l'erreur {ticket_id}: {save_error}")


# -----------------------------------------------------------------------------
# Réception caisserie - lecture des bordereaux PDF fournisseur
# -----------------------------------------------------------------------------
def _normalise_numero_caisse(value):
    """Normalise 01, 1, 1.0 -> 1 pour fiabiliser les rapprochements."""
    txt = _as_text(value).strip()
    if not txt:
        return ""
    try:
        return str(int(float(txt.replace(",", "."))))
    except Exception:
        return txt.lstrip("0") or "0"


def _extract_reception_pdf(pdf_bytes):
    """
    Extrait les informations utiles d'un bordereau PDF.

    1) Essaie d'abord l'extraction texte native avec pypdf.
    2) Si le PDF contient trop peu de texte, lance automatiquement un OCR.

    Format SECO actuellement reconnu :
      - BORDEREAU D'EXPEDITION N° 26400467 du 17/08/2026
      - V/Cde : 101138/01
    """
    try:
        from pypdf import PdfReader
    except Exception as e:
        raise RuntimeError(
            "Le module pypdf n'est pas installé. Ajoute pypdf à requirements.txt."
        ) from e

    try:
        reader = PdfReader(BytesIO(pdf_bytes))
    except Exception as e:
        raise ValueError(f"PDF illisible : {e}")

    pages_text = []
    for page in reader.pages:
        try:
            pages_text.append(page.extract_text() or "")
        except Exception:
            pages_text.append("")

    text = "\n".join(pages_text).strip()
    ocr_used = False

    # Un scan image peut renvoyer une chaine vide ou quelques caracteres inutilisables.
    if len(text) < 50:
        print("[RECEPTION OCR] Texte natif insuffisant, lancement de l'OCR")
        try:
            from pdf2image import convert_from_bytes
            import pytesseract
        except Exception as e:
            raise RuntimeError(
                "OCR indisponible. Ajoute pdf2image, pytesseract et Pillow a requirements.txt, "
                "puis installe tesseract-ocr et poppler-utils sur Render."
            ) from e

        # Un seul OCR lourd à la fois par worker. Cela évite les pics mémoire
        # si le navigateur/proxy soumet plusieurs fois le même PDF.
        with _RECEPTION_OCR_LOCK:
            try:
                images = convert_from_bytes(
                    pdf_bytes,
                    dpi=200,
                    grayscale=True,
                    thread_count=1
                )
            except Exception as e:
                raise RuntimeError(
                    "Impossible de convertir le PDF en image pour l'OCR. "
                    "Verifie que poppler-utils est installe sur Render."
                ) from e

            ocr_pages = []
            total_pages = len(images)
            for index, image in enumerate(images, start=1):
                try:
                    page_text = pytesseract.image_to_string(
                        image,
                        lang="fra",
                        config="--psm 6"
                    )
                except Exception as e:
                    raise RuntimeError(
                        "Echec OCR Tesseract. Verifie que tesseract-ocr et la langue francaise sont installes."
                    ) from e
                print(f"[RECEPTION OCR] Page {index}/{total_pages} analysee")
                ocr_pages.append(page_text or "")
                try:
                    image.close()
                except Exception:
                    pass

            # Libère explicitement les images avant le rapprochement Supabase.
            images.clear()
            text = "\n".join(ocr_pages).strip()
        ocr_used = True
        print(f"[RECEPTION OCR] OCR termine, {len(text)} caracteres detectes")

    if not text:
        raise ValueError(
            "Aucun texte exploitable trouve dans le PDF, meme apres OCR."
        )

    # Numéro et date du bordereau.
    bl_numero = ""
    bl_date = ""
    m = re.search(
        r"BORDEREAU\s+D['’]EXPEDITION.*?N\s*[°ºo]?\s*([0-9]+)\s+du\s+([0-9]{2}/[0-9]{2}/[0-9]{4})",
        text,
        flags=re.IGNORECASE | re.DOTALL
    )
    if m:
        bl_numero = m.group(1)
        bl_date = m.group(2)
    else:
        # Secours, plus tolérant.
        m = re.search(r"N\s*[°ºo]?\s*([0-9]{6,})\s+du\s+([0-9]{2}/[0-9]{2}/[0-9]{4})", text)
        if m:
            bl_numero = m.group(1)
            bl_date = m.group(2)

    # Extrait toutes les références V/Cde : dossier/numero.
    refs = []
    seen = set()
    # OCR peut lire "V/Cde" comme "ViCde", "VICde", "V Cde", etc.
    # On tolère donc un séparateur imparfait entre V et Cde.
    for dossier, numero in re.findall(
        r"V\s*[/|Il1i\-]?\s*Cde\s*:\s*([A-Za-z0-9_-]+)\s*/\s*([0-9]+)",
        text,
        flags=re.IGNORECASE
    ):
        dossier = dossier.strip()
        numero_norm = _normalise_numero_caisse(numero)
        key = (dossier, numero_norm)
        if key not in seen:
            seen.add(key)
            refs.append({
                "dossier": dossier,
                "numero": numero_norm,
                "numero_pdf": numero.strip()
            })

    if not refs:
        raise ValueError(
            "Aucune référence de caisse de type 'V/Cde : dossier/numéro' n'a été détectée, même après OCR."
        )

    return {
        "bl_numero": bl_numero,
        "bl_date": bl_date,
        "references": refs,
        "page_count": len(reader.pages),
        "ocr_used": ocr_used,
    }


def _match_reception_refs_to_tickets(references):
    """Rapproche les références du PDF avec les fiches de caisse ESI TICKETS."""
    all_tickets = list_tickets()
    candidates = [
        t for t in all_tickets
        if t.get("module") == "Fiche de caisse"
    ]

    by_key = {}
    for t in candidates:
        dossier = _as_text(t.get("dossier")).strip()
        numero = _normalise_numero_caisse(t.get("ref"))
        if dossier and numero:
            by_key.setdefault((dossier, numero), []).append(t)

    results = []
    for ref in references:
        key = (ref["dossier"], ref["numero"])
        matches = by_key.get(key, [])

        if len(matches) == 1:
            t = matches[0]
            fiche = t.get("fiche") or {}
            results.append({
                "found": True,
                "ambiguous": False,
                "ticket_id": t.get("id"),
                "dossier": t.get("dossier") or "",
                "ref": t.get("ref") or "",
                "charge_projet": t.get("chargeProjet") or "",
                "date_emballage": t.get("dateEmballage") or "",
                "localisation": fiche.get("localisation") or "",
                "status": t.get("status") or "",
            })
        elif len(matches) > 1:
            results.append({
                "found": False,
                "ambiguous": True,
                "dossier": ref["dossier"],
                "ref": ref["numero_pdf"],
                "error": f"{len(matches)} tickets correspondent à cette référence"
            })
        else:
            results.append({
                "found": False,
                "ambiguous": False,
                "dossier": ref["dossier"],
                "ref": ref["numero_pdf"],
                "error": "Ticket introuvable"
            })

    return results


# -----------------------------------------------------------------------------
# Référentiels métier : chargés de projet, clients, contacts
# -----------------------------------------------------------------------------
REFERENTIELS = {
    "project-managers": {
        "table": "project_managers",
        "allowed": ["nom", "email", "telephone", "actif"],
        "search": ["nom", "email", "telephone"],
        "required": ["nom"],
        "defaults": {"actif": True},
        "order": "nom.asc"
    },
    "clients": {
        "table": "clients",
        "allowed": ["nom", "adresse", "contact_nom", "contact_email", "contact_telephone", "actif"],
        "search": ["nom", "adresse", "contact_nom", "contact_email", "contact_telephone"],
        "required": ["nom"],
        "defaults": {"actif": True},
        "order": "nom.asc"
    },
    "contacts": {
        "table": "contacts",
        "allowed": ["nom", "email", "telephone", "client_nom", "fonction", "actif"],
        "search": ["nom", "email", "telephone", "client_nom", "fonction"],
        "required": ["nom"],
        "defaults": {"actif": True},
        "order": "nom.asc"
    }
}


def _referentiel_config(kind):
    cfg = REFERENTIELS.get(kind)
    if not cfg:
        abort(404)
    return cfg


def _clean_referentiel_payload(kind, data, partial=False):
    cfg = _referentiel_config(kind)
    data = data or {}
    payload = {}

    for field in cfg["allowed"]:
        if field in data:
            if field == "actif":
                payload[field] = bool(data.get(field))
            else:
                payload[field] = _as_text(data.get(field)).strip()

    if not partial:
        for field, value in cfg.get("defaults", {}).items():
            payload.setdefault(field, value)

        missing = [field for field in cfg.get("required", []) if not payload.get(field)]
        if missing:
            raise ValueError("Champ obligatoire manquant : " + ", ".join(missing))

    return payload


@app.route('/api/referentiels/<kind>', methods=['GET'])
def api_list_referentiel(kind):
    cfg = _referentiel_config(kind)
    q = (request.args.get('q') or '').strip()
    include_inactive = request.args.get('include_inactive') == '1'
    limit = request.args.get('limit') or '100'

    query = "select=*"
    if not include_inactive:
        query += "&actif=eq.true"
    if q:
        pattern = "*" + q.replace("*", "") + "*"
        parts = []
        for field in cfg["search"]:
            parts.append(f"{field}.ilike.{urllib.parse.quote(pattern, safe='*')}")
        query += "&or=(" + ",".join(parts) + ")"
    query += "&order=" + urllib.parse.quote(cfg.get("order", "nom.asc"), safe='.,')
    query += "&limit=" + urllib.parse.quote(str(limit), safe='')

    rows = supabase_rest_request("GET", cfg["table"], query) or []
    return jsonify(rows)


@app.route('/api/referentiels/<kind>', methods=['POST'])
def api_create_referentiel(kind):
    cfg = _referentiel_config(kind)
    data = request.get_json(silent=True) or {}
    try:
        payload = _clean_referentiel_payload(kind, data, partial=False)
    except ValueError as e:
        return jsonify({'ok': False, 'error': str(e)}), 400

    try:
        rows = supabase_rest_request("POST", cfg["table"], "", [payload], prefer="return=representation") or []
        return jsonify({'ok': True, 'item': rows[0] if rows else payload})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/referentiels/<kind>/<item_id>', methods=['PUT'])
def api_update_referentiel(kind, item_id):
    cfg = _referentiel_config(kind)
    data = request.get_json(silent=True) or {}
    try:
        payload = _clean_referentiel_payload(kind, data, partial=True)
    except ValueError as e:
        return jsonify({'ok': False, 'error': str(e)}), 400

    if not payload:
        return jsonify({'ok': False, 'error': 'Aucune donnée à modifier'}), 400

    safe_id = urllib.parse.quote(str(item_id), safe='')
    try:
        rows = supabase_rest_request("PATCH", cfg["table"], f"id=eq.{safe_id}", payload, prefer="return=representation") or []
        return jsonify({'ok': True, 'item': rows[0] if rows else payload})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/referentiels/<kind>/<item_id>/toggle', methods=['PATCH'])
def api_toggle_referentiel(kind, item_id):
    cfg = _referentiel_config(kind)
    data = request.get_json(silent=True) or {}
    actif = bool(data.get('actif'))
    safe_id = urllib.parse.quote(str(item_id), safe='')
    try:
        rows = supabase_rest_request("PATCH", cfg["table"], f"id=eq.{safe_id}", {"actif": actif}, prefer="return=representation") or []
        return jsonify({'ok': True, 'item': rows[0] if rows else {'id': item_id, 'actif': actif}})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


# -----------------------------------------------------------------------------
# Notifications email Outlook / Microsoft 365
# -----------------------------------------------------------------------------
def _format_ticket_notification_subject(ticket):
    """Prépare l'objet métier du mail de notification, sans numéro interne de ticket."""
    module = ticket.get("module") or "Ticket"
    dossier = (ticket.get("dossier") or "").strip()
    ref = (ticket.get("ref") or "").strip()
    preteur = (ticket.get("preteur") or "").strip()
    projet = (ticket.get("expo") or ticket.get("objet") or "").strip()
    lieu_rdv = (ticket.get("lieuRdv") or "").strip()

    if module == "Fiche de caisse":
        suffix = " ".join([x for x in [dossier, ref] if x]).strip()
        return f"[ESI Tickets] Fiche de caisse terminée - {suffix}".strip()

    if module == "Demande de devis":
        suffix = " ".join([x for x in [dossier, projet] if x]).strip()
        return f"[ESI Tickets] Demande de devis terminée - {suffix}".strip()

    if module == "Demande Aller voir":
        suffix = " ".join([x for x in [dossier, lieu_rdv or projet] if x]).strip()
        return f"[ESI Tickets] Aller voir finalisé - {suffix}".strip()

    suffix = dossier or projet or ref
    return f"[ESI Tickets] Ticket terminé - {suffix}".strip()


def _find_project_manager_email(charge_projet):
    """Retrouve l'email du chargé de projet depuis le référentiel Supabase."""
    charge_projet = (charge_projet or "").strip()
    if not charge_projet or charge_projet == "-":
        return ""

    # Recherche exacte sur le nom enregistré dans le ticket.
    nom_encode = urllib.parse.quote(charge_projet, safe='')
    rows = supabase_rest_request(
        "GET",
        "project_managers",
        f"select=nom,email&nom=eq.{nom_encode}&limit=1"
    ) or []

    if rows and rows[0].get("email"):
        return (rows[0].get("email") or "").strip()

    # Secours : recherche souple si le nom contient une différence d'espace ou de casse.
    pattern = "*" + charge_projet.replace("*", "") + "*"
    pattern_encode = urllib.parse.quote(pattern, safe='*')
    rows = supabase_rest_request(
        "GET",
        "project_managers",
        f"select=nom,email&nom=ilike.{pattern_encode}&limit=1"
    ) or []

    if rows and rows[0].get("email"):
        return (rows[0].get("email") or "").strip()

    return ""


def envoyer_notification_fin_ticket(ticket):
    """Envoie un email au chargé de projet quand un ticket passe au statut Terminé."""
    try:
        print("[MAIL] Début envoi notification ticket terminé")

        charge_projet = (ticket.get("chargeProjet") or "").strip()
        print("[MAIL] Chargé projet =", charge_projet)

        email_dest = _find_project_manager_email(charge_projet)
        print("[MAIL] Destinataire =", email_dest or "NON TROUVÉ")

        if not email_dest:
            print(f"[MAIL] Aucun email trouvé pour le chargé de projet : {charge_projet}")
            return False

        smtp_host = os.getenv("SMTP_HOST")
        smtp_port = int(os.getenv("SMTP_PORT", "587"))
        smtp_user = os.getenv("SMTP_USER")
        smtp_password = os.getenv("SMTP_PASSWORD")

        if not smtp_host or not smtp_user or not smtp_password:
            print("[MAIL] Configuration SMTP manquante : vérifier SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD dans Render")
            return False

        ticket_id = ticket.get("id", "")
        module = ticket.get("module", "")
        dossier = ticket.get("dossier", "")
        ref = ticket.get("ref", "")
        projet = ticket.get("expo") or ticket.get("objet") or ""
        lieu_rdv = ticket.get("lieuRdv", "")
        date_rdv = ticket.get("dateRdv", "")
        heure_rdv = ticket.get("heureRdv", "")
        commentaire = ticket.get("commentaire", "")

        sujet = _format_ticket_notification_subject(ticket)

        corps = f"""Bonjour,

Le ticket suivant vient d'être terminé :

Numéro ticket : {ticket_id}
Type : {module}
Dossier / Client : {dossier}
Référence / N° caisse : {ref}
Projet / Expo : {projet}
Chargé de projet : {charge_projet}
Lieu RDV : {lieu_rdv}
Date RDV : {date_rdv} {heure_rdv}

Commentaire :
{commentaire or '-'}

Le document est disponible dans ESI Tickets.

Cordialement,
ESI Tickets
"""

        msg = MIMEText(corps, "plain", "utf-8")
        msg["Subject"] = sujet
        msg["From"] = smtp_user
        msg["To"] = email_dest

        with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
            server.starttls()
            server.login(smtp_user, smtp_password)
            server.send_message(msg)

        print(f"[MAIL] Notification envoyée à {email_dest}")
        return True

    except Exception as e:
        print(f"[MAIL] Erreur envoi notification : {e}")
        return False


@app.route('/')
def index():
    return redirect(url_for('demandeur'))

@app.route('/demandeur')
def demandeur():
    return render_template('demandeur.html')

@app.route('/gestionnaire')
def gestionnaire():
    from flask import request, redirect, url_for
    if request.args.get('pwd') != '1234':
        return redirect(url_for('login'))
    return render_template('gestionnaire.html')

@app.route('/login', methods=['GET','POST'])
def login():
    from flask import request, redirect, render_template_string
    error = ''
    if request.method == 'POST':
        if request.form.get('password') == '1234':
            return redirect('/gestionnaire?pwd=1234')
        error = 'Mot de passe incorrect'
    return render_template_string("""<!DOCTYPE html>
<html lang='fr'>
<head>
<meta charset='UTF-8'>
<meta name='viewport' content='width=device-width, initial-scale=1.0'>
<title>Connexion gestionnaire</title>
<style>
body{font-family:Arial,Helvetica,sans-serif;background:linear-gradient(180deg,#eef6fb 0%,#f6f8fb 100%);margin:0;display:flex;align-items:center;justify-content:center;height:100vh;color:#1e293b}
.card{background:#fff;border:1px solid #dbe7f0;border-radius:20px;padding:28px;box-shadow:0 12px 30px rgba(15,23,42,.08);width:360px}
h1{margin:0 0 10px;font-size:28px} p{margin:0 0 18px;color:#64748b}
input{width:100%;padding:12px 14px;border:1px solid #dbe7f0;border-radius:14px;font-size:15px;box-sizing:border-box}
button{margin-top:14px;width:100%;padding:12px 14px;border:none;border-radius:14px;background:linear-gradient(135deg,#0ea5e9 0%, #0284c7 100%);color:#fff;font-weight:700;cursor:pointer}
.err{margin-top:12px;color:#b91c1c;font-size:13px}
</style>
</head>
<body>
  <form class='card' method='post'>
    <h1>Gestion Tickets</h1>
    <p>Accès protégé par mot de passe</p>
    <input type='password' name='password' placeholder='Mot de passe' autofocus />
    <button type='submit'>Entrer</button>
    {% if error %}<div class='err'>{{ error }}</div>{% endif %}
  </form>
</body>
</html>""", error=error)


@app.route('/reception')
def reception():
    return render_template('reception.html')

@app.route('/api/status')
def api_status():
    root = ensure_shared_root()
    return jsonify({'shared_path': str(root), 'mode': 'automatic_app_folder'})

@app.route('/api/tickets')
def api_tickets():
    status = request.args.get('status')
    limit = request.args.get('limit')
    tickets = list_tickets(status=status, limit=limit)
    return jsonify(tickets)


@app.route('/api/caisse-status/<path:caisse_ref>')
def api_caisse_status(caisse_ref):
    """Retourne l'état fournisseur d'une caisse depuis le Google Sheet public."""
    result = get_caisse_fournisseur_status(caisse_ref)
    if result.get("success"):
        return jsonify(result)

    # On garde un HTTP 200 pour que la fenêtre puisse afficher proprement
    # "Caisse introuvable" sans traiter cela comme une panne réseau.
    return jsonify(result)


@app.route('/api/tickets/<ticket_id>/localisation', methods=['PATCH'])
def api_update_localisation(ticket_id):
    """Met à jour uniquement la localisation d'une fiche de caisse."""
    ticket = load_ticket(ticket_id)
    if not ticket:
        return jsonify({'ok': False, 'error': 'Ticket introuvable'}), 404

    fiche = ticket.get('fiche') or {}
    if not fiche:
        return jsonify({'ok': False, 'error': 'Fiche de caisse introuvable'}), 404

    data = request.get_json(silent=True) or {}
    localisation = _as_text(data.get('localisation')).strip()

    fiche['localisation'] = localisation
    ticket['fiche'] = fiche
    ticket['updatedAt'] = datetime.now().isoformat()

    try:
        supabase_rest_request(
            "PATCH",
            "fiches",
            "ticket_id=eq." + urllib.parse.quote(ticket_id, safe=''),
            {"localisation": localisation},
            prefer="return=minimal"
        )
        # On met également à jour updated_at du ticket principal sans réécrire les fichiers.
        supabase_rest_request(
            "PATCH",
            "tickets",
            "id=eq." + urllib.parse.quote(ticket_id, safe=''),
            {"updated_at": ticket['updatedAt']},
            prefer="return=minimal"
        )
        return jsonify({'ok': True, 'localisation': localisation})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/reception/analyse-bl', methods=['POST'])
def api_reception_analyse_bl():
    """
    Analyse un bordereau PDF sans modifier la base.
    Retourne les caisses détectées et leur correspondance avec ESI TICKETS.
    """
    fs = request.files.get('file')
    if not fs or not fs.filename:
        return jsonify({'ok': False, 'error': 'Fichier PDF manquant'}), 400

    if not fs.filename.lower().endswith('.pdf'):
        return jsonify({'ok': False, 'error': 'Le fichier doit être un PDF'}), 400

    content = fs.read()
    if not content:
        return jsonify({'ok': False, 'error': 'Le fichier PDF est vide'}), 400

    try:
        cache_key, parsed = _reception_cache_get(content)
        if parsed is not None:
            print("[RECEPTION OCR] Resultat reutilise depuis le cache PDF")
        else:
            parsed = _extract_reception_pdf(content)
            _reception_cache_set(cache_key, parsed)

        print(
            f"[RECEPTION PDF] Analyse terminee: BL={parsed.get('bl_numero', '') or '-'} "
            f"refs={len(parsed.get('references') or [])} ocr={parsed.get('ocr_used', False)}"
        )
        matches = _match_reception_refs_to_tickets(parsed["references"])
        print(f"[RECEPTION PDF] Rapprochement termine: {len(matches)} ligne(s)")

        return jsonify({
            'ok': True,
            'bl_numero': parsed.get('bl_numero', ''),
            'bl_date': parsed.get('bl_date', ''),
            'page_count': parsed.get('page_count', 0),
            'ocr_used': parsed.get('ocr_used', False),
            'items': matches,
            'found_count': sum(1 for x in matches if x.get('found')),
            'missing_count': sum(1 for x in matches if not x.get('found')),
        })
    except ValueError as e:
        return jsonify({'ok': False, 'error': str(e)}), 400
    except Exception as e:
        print(f"[RECEPTION PDF] Erreur analyse : {e}")
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/reception/valider-bl', methods=['POST'])
def api_reception_valider_bl():
    """
    Valide en lot la réception des caisses détectées.
    Le fait de renseigner la localisation acte la réception.
    """
    data = request.get_json(silent=True) or {}
    ticket_ids = data.get('ticket_ids') or []
    localisation = _as_text(data.get('localisation')).strip()

    if not localisation:
        return jsonify({
            'ok': False,
            'error': 'La localisation est obligatoire pour valider la réception.'
        }), 400

    if not isinstance(ticket_ids, list) or not ticket_ids:
        return jsonify({'ok': False, 'error': 'Aucune caisse sélectionnée'}), 400

    updated = []
    errors = []

    for ticket_id in dict.fromkeys(_as_text(x).strip() for x in ticket_ids if _as_text(x).strip()):
        try:
            ticket = load_ticket(ticket_id)
            if not ticket:
                errors.append({'ticket_id': ticket_id, 'error': 'Ticket introuvable'})
                continue

            if ticket.get('module') != 'Fiche de caisse':
                errors.append({'ticket_id': ticket_id, 'error': 'Ce ticket n’est pas une fiche de caisse'})
                continue

            fiche = ticket.get('fiche') or {}
            if not fiche:
                errors.append({'ticket_id': ticket_id, 'error': 'Fiche de caisse introuvable'})
                continue

            supabase_rest_request(
                "PATCH",
                "fiches",
                "ticket_id=eq." + urllib.parse.quote(ticket_id, safe=''),
                {"localisation": localisation},
                prefer="return=minimal"
            )

            supabase_rest_request(
                "PATCH",
                "tickets",
                "id=eq." + urllib.parse.quote(ticket_id, safe=''),
                {"updated_at": datetime.now().isoformat()},
                prefer="return=minimal"
            )

            updated.append(ticket_id)

        except Exception as e:
            print(f"[RECEPTION PDF] Erreur validation {ticket_id}: {e}")
            errors.append({'ticket_id': ticket_id, 'error': str(e)})

    return jsonify({
        'ok': len(errors) == 0,
        'updated_count': len(updated),
        'updated': updated,
        'errors': errors,
        'localisation': localisation
    }), (200 if not errors else 207)


@app.route('/api/tickets', methods=['POST'])
def api_create_ticket():
    form = request.form
    module = form.get('module','')
    # Normalise l'apostrophe typographique envoyee par certains navigateurs/pages HTML.
    # Ainsi "Demande d’enlèvement" et "Demande d'enlèvement" sont traites de la meme facon.
    module = module.replace("’", "'").strip()

    prefixes = {
        'Fiche de caisse': 'DEM',
        'Demande de devis': 'DEV',
        'Demande Aller voir': 'AV',
        'Demande d\'enlèvement': 'ENL',
        'Demande d\'enlevement': 'ENL',
    }
    prefix = prefixes.get(module, 'AV')

    incoming_files = [fs for fs in request.files.getlist('files') if fs and fs.filename]
    is_enlevement = module in ("Demande d'enlèvement", "Demande d'enlevement")

    # Pour une demande d'enlevement, le demandeur ne fait qu'une chose : deposer un PDF.
    if is_enlevement:
        if len(incoming_files) != 1:
            return jsonify({
                'ok': False,
                'error': "La demande d'enlèvement doit contenir un seul bon PDF."
            }), 400
        if not incoming_files[0].filename.lower().endswith('.pdf'):
            return jsonify({
                'ok': False,
                'error': "Le bon d'enlèvement doit être un fichier PDF."
            }), 400

    ticket_id = next_id(prefix)
    ticket = {
        'id': ticket_id,
        'module': module,
        'status': 'Demande créée',
        'createdAt': datetime.now().isoformat(),
        'dossier': form.get('dossier',''),
        'ref': form.get('ref',''),
        'preteur': form.get('preteur','-') or '-',
        'expo': form.get('expo','-') or '-',
        'objet': form.get('objet','-') or '-',
        'chargeProjet': form.get('chargeProjet','-') or '-',
        'typeCaisse': form.get('typeCaisse','-') or '-',
        'dimensions': form.get('dimensions','-') or '-',
        'dateEmballage': form.get('dateEmballage','-') or '-',
        'prixDevis': form.get('prixDevis','-') or '-',
        'dateRdv': form.get('dateRdv','-') or '-',
        'heureRdv': form.get('heureRdv','-') or '-',
        'lieuRdv': form.get('lieuRdv','-') or '-',
        'contactRdv': form.get('contactRdv','-') or '-',
        'commentaire': form.get('commentaire',''),
        'files': [],
        'managerSheets': []
    }

    if is_enlevement:
        ticket['enlevement'] = {
            'analysis_status': 'pending',
            'analysis_error': '',
            'items': [],
            'references': [],
        }

    ticket_folder(ticket_id)  # conserve la création du dossier local historique

    enlevement_pdf_bytes = None
    for fs in incoming_files:
        content = fs.read()
        if is_enlevement:
            enlevement_pdf_bytes = content

        clean_name = safe_filename(fs.filename)
        storage_path = f"{ticket_id}/{datetime.now().strftime('%Y%m%d%H%M%S')}_{clean_name}"

        try:
            supabase_upload_bytes(
                storage_path,
                content,
                fs.content_type
            )
        except Exception as e:
            print(f"[SUPABASE UPLOAD] Erreur : {e}")
            return jsonify({'ok': False, 'error': f'Erreur upload Supabase : {e}'}), 500

        ticket['files'].append({
            'name': fs.filename,
            'size': len(content),
            'path': storage_path
        })

    # Le ticket est sauvegarde avant toute analyse : le demandeur n'attend jamais l'OCR.
    save_ticket(ticket)

    if is_enlevement and enlevement_pdf_bytes:
        worker = threading.Thread(
            target=_analyse_enlevement_ticket_background,
            args=(ticket_id, enlevement_pdf_bytes),
            daemon=True,
            name=f"enlevement-{ticket_id}"
        )
        worker.start()

    return jsonify({
        'ok': True,
        'id': ticket_id,
        'analysis_status': 'pending' if is_enlevement else None
    })


@app.route('/api/tickets/<ticket_id>', methods=['PUT'])
def api_update_ticket(ticket_id):
    ticket = load_ticket(ticket_id)
    if not ticket:
        return jsonify({'error': 'Ticket introuvable'}), 404

    data = request.get_json(silent=True) or {}

    editable_fields = [
        'dossier',
        'ref',
        'preteur',
        'expo',
        'objet',
        'chargeProjet',
        'typeCaisse',
        'dimensions',
        'dateEmballage',
        'prixDevis',
        'dateRdv',
        'heureRdv',
        'lieuRdv',
        'contactRdv',
        'commentaire'
    ]

    for field in editable_fields:
        if field in data:
            ticket[field] = data.get(field, '')

    if 'expo' in data and 'objet' not in data:
        ticket['objet'] = data.get('expo', '')

    ticket['updatedAt'] = datetime.now().isoformat()
    save_ticket(ticket)
    return jsonify({'ok': True})

@app.route('/api/tickets/<ticket_id>/status', methods=['PATCH'])
def api_update_status(ticket_id):
    ticket = load_ticket(ticket_id)
    if not ticket:
        return jsonify({'error': 'Ticket introuvable'}), 404

    ancien_statut = ticket.get('status')

    data = request.get_json(silent=True) or {}
    nouveau_statut = data.get('status', ancien_statut)

    ticket['status'] = nouveau_statut
    ticket['updatedAt'] = datetime.now().isoformat()
    save_ticket(ticket)

    # L'envoi automatique SMTP est volontairement désactivé.
    # La notification se prépare maintenant via Outlook Web avec le bouton "Envoyer Notif".
    return jsonify({'ok': True})


@app.route('/api/tickets/<ticket_id>/notification-url')
def api_ticket_notification_url(ticket_id):
    """Prépare une URL Outlook Web préremplie pour envoyer la notification manuellement."""
    ticket = load_ticket(ticket_id)
    if not ticket:
        return jsonify({'error': 'Ticket introuvable'}), 404

    charge_projet = (ticket.get("chargeProjet") or "").strip()
    email_dest = _find_project_manager_email(charge_projet)

    if not email_dest:
        return jsonify({
            'error': "Aucun email trouvé pour le chargé de projet dans les référentiels."
        }), 404

    module = ticket.get("module", "")
    dossier = (ticket.get("dossier") or "").strip()
    ref = (ticket.get("ref") or "").strip()
    preteur = (ticket.get("preteur") or "").strip()
    projet = (ticket.get("expo") or ticket.get("objet") or "").strip()
    lieu_rdv = (ticket.get("lieuRdv") or "").strip()
    date_rdv = (ticket.get("dateRdv") or "").strip()
    heure_rdv = (ticket.get("heureRdv") or "").strip()
    commentaire = ticket.get("commentaire", "")
    fiche = ticket.get("fiche") or {}

    subject = _format_ticket_notification_subject(ticket)

    # Lien direct vers le ticket dans le portail demandeur, sans mot de passe.
    base_url = request.host_url.rstrip('/')
    ticket_url = f"{base_url}/demandeur?ticket={urllib.parse.quote(ticket_id, safe='')}"

    if module == "Fiche de caisse":
        link_text = f"Consulter la fiche de caisse {dossier}-{ref}"
        intro = "La fiche de caisse suivante a été commandée :"
        details = f"""
        <p>
          <strong>Dossier :</strong> {dossier or '-'}<br>
          <strong>N° caisse / Référence :</strong> {ref or '-'}<br>
          <strong>Prêteur :</strong> {preteur or '-'}<br>
          <strong>Dimensions extérieures :</strong> {fiche.get('dimensionsExt') or '-'}<br>
          <strong>Prix de cession :</strong> {fiche.get('prixCession') or '-'}<br>
          <strong>Date mise à dispo :</strong> {datetime.fromisoformat(ticket.get('dateEmballage')).strftime('%d/%m/%Y') if ticket.get('dateEmballage') and ticket.get('dateEmballage') != '-' else '-'}
        </p>
        """
    elif module == "Demande de devis":
        label = " ".join([x for x in [dossier, projet] if x]).strip() or ticket_id
        link_text = f"Consulter la demande de devis {label}"
        intro = "La demande de devis suivante a été finalisée :"
        details = f"""
        <p>
          <strong>Client / Dossier :</strong> {dossier or '-'}<br>
          <strong>Projet :</strong> {projet or '-'}<br>
          <strong>Chargé de projet :</strong> {charge_projet or '-'}
        </p>
        <p><strong>Commentaire :</strong><br>{(commentaire or '-').replace(chr(10), '<br>')}</p>
        """
    elif module == "Demande Aller voir":
        label = " ".join([x for x in [dossier, projet] if x]).strip() or ticket_id
        link_text = f"Consulter le dossier {label}"
        intro = "La demande Aller Voir suivante a été finalisée :"
        date_rdv_fr = datetime.fromisoformat(date_rdv).strftime('%d/%m/%Y') if date_rdv and date_rdv != '-' else '-'
        details = f"""
        <p>
          <strong>Client / Dossier :</strong> {dossier or '-'}<br>
          <strong>Projet :</strong> {projet or '-'}<br>
          <strong>Lieu de rendez-vous :</strong> {lieu_rdv or '-'}<br>
          <strong>Date :</strong> {date_rdv_fr}<br>
          <strong>Heure :</strong> {heure_rdv or '-'}<br>
          <strong>Prêteur :</strong> {preteur or '-'}
        </p>
        """
    else:
        label = " ".join([x for x in [dossier, projet] if x]).strip() or ticket_id
        link_text = f"Consulter le ticket {label}"
        intro = "La demande suivante a été finalisée :"
        details = f"""
        <p>
          <strong>Client / Dossier :</strong> {dossier or '-'}<br>
          <strong>Projet :</strong> {projet or '-'}<br>
          <strong>Chargé de projet :</strong> {charge_projet or '-'}
        </p>
        """

    body_html = f"""<html>
<body>
<p>Bonjour,</p>
<p>{intro}</p>
{details}
<p>Les documents associés sont disponibles dans ESI Tickets.</p>
<p>
  <a href=\"{ticket_url}\" style=\"background:#0284c7;color:#ffffff;padding:10px 16px;text-decoration:none;border-radius:6px;display:inline-block;font-weight:bold;\">
    {link_text}
  </a>
</p>
</body>
</html>"""

    # Outlook Web accepte le paramètre body dans le deeplink compose.
    # Le contenu HTML permet d'afficher un lien avec un libellé propre au lieu d'une URL brute.
    params = urllib.parse.urlencode({
        "to": email_dest,
        "subject": subject,
        "body": body_html
    })

    outlook_url = "https://outlook.office.com/mail/deeplink/compose?" + params

    return jsonify({
        'ok': True,
        'to': email_dest,
        'subject': subject,
        'body': body_html,
        'outlook_url': outlook_url,
        'ticket_url': ticket_url,
        'link_text': link_text
    })


def _build_notification_content(ticket, ticket_id, notification_mode='final'):
    """Construit les éléments de notification en HTML pour un brouillon .eml Outlook."""
    charge_projet = (ticket.get("chargeProjet") or "").strip()
    email_dest = _find_project_manager_email(charge_projet)

    if not email_dest:
        return None, "Aucun email trouvé pour le chargé de projet dans les référentiels."

    module = ticket.get("module", "")
    dossier = (ticket.get("dossier") or "").strip()
    ref = (ticket.get("ref") or "").strip()
    preteur = (ticket.get("preteur") or "").strip()
    projet = (ticket.get("expo") or ticket.get("objet") or "").strip()
    lieu_rdv = (ticket.get("lieuRdv") or "").strip()
    date_rdv = (ticket.get("dateRdv") or "").strip()
    heure_rdv = (ticket.get("heureRdv") or "").strip()
    commentaire = ticket.get("commentaire", "")
    fiche = ticket.get("fiche") or {}

    subject = _format_ticket_notification_subject(ticket)
    base_url = request.host_url.rstrip('/')
    ticket_url = f"{base_url}/demandeur?ticket={urllib.parse.quote(ticket_id, safe='')}"

    def esc(value):
        return html.escape(str(value or "-"))

    def nl2br(value):
        return html.escape(str(value or "-")).replace("\n", "<br>")

    if module == "Fiche de caisse":
        link_text = f"Consulter la fiche de caisse {dossier}-{ref}"
        intro = "La fiche de caisse suivante a été commandée :"
        details = f"""
        <p>
          <strong>Dossier :</strong> {esc(dossier)}<br>
          <strong>N° caisse / Référence :</strong> {esc(ref)}<br>
          <strong>Prêteur :</strong> {esc(preteur)}<br>
          <strong>Dimensions extérieures :</strong> {esc(fiche.get('dimensionsExt'))}<br>
          <strong>Prix de cession :</strong> {esc(fiche.get('prixCession'))}<br>
          <strong>Date mise à dispo :</strong> {esc(datetime.fromisoformat(ticket.get('dateEmballage')).strftime('%d/%m/%Y') if ticket.get('dateEmballage') and ticket.get('dateEmballage') != '-' else '-')}
        </p>
        """
    elif module == "Demande de devis":
        label = " ".join([x for x in [dossier, projet] if x]).strip() or ticket_id
        link_text = f"Consulter la demande de devis {label}"
        intro = "La demande de devis suivante a été finalisée :"
        details = f"""
        <p>
          <strong>Client / Dossier :</strong> {esc(dossier)}<br>
          <strong>Projet :</strong> {esc(projet)}<br>
          <strong>Chargé de projet :</strong> {esc(charge_projet)}
        </p>
        <p><strong>Commentaire :</strong><br>{nl2br(commentaire)}</p>
        """
    elif module == "Demande Aller voir":
        label = " ".join([x for x in [dossier, projet] if x]).strip() or ticket_id
        link_text = f"Consulter le dossier {label}"
        date_rdv_fr = datetime.fromisoformat(date_rdv).strftime('%d/%m/%Y') if date_rdv and date_rdv != '-' else '-'

        if notification_mode == 'validation':
            subject = f"[ESI Tickets] Créneau Aller voir validé - {label}".strip()
            intro = "Le créneau suivant a été validé :"
            details = f"""
            <p>
              <strong>Client / Dossier :</strong> {esc(dossier)}<br>
              <strong>Projet :</strong> {esc(projet)}<br>
              <strong>Lieu de rendez-vous :</strong> {esc(lieu_rdv)}<br>
              <strong>Date :</strong> {esc(date_rdv_fr)}<br>
              <strong>Heure :</strong> {esc(heure_rdv)}<br>
              <strong>Prêteur :</strong> {esc(preteur)}
            </p>
            <p>Le rendez-vous est désormais confirmé.</p>
            """
        else:
            intro = "La demande Aller Voir suivante a été finalisée :"
            details = f"""
            <p>
              <strong>Client / Dossier :</strong> {esc(dossier)}<br>
              <strong>Projet :</strong> {esc(projet)}<br>
              <strong>Lieu de rendez-vous :</strong> {esc(lieu_rdv)}<br>
              <strong>Date :</strong> {esc(date_rdv_fr)}<br>
              <strong>Heure :</strong> {esc(heure_rdv)}<br>
              <strong>Prêteur :</strong> {esc(preteur)}
            </p>
            """
    else:
        label = " ".join([x for x in [dossier, projet] if x]).strip() or ticket_id
        link_text = f"Consulter le ticket {label}"
        intro = "La demande suivante a été finalisée :"
        details = f"""
        <p>
          <strong>Client / Dossier :</strong> {esc(dossier)}<br>
          <strong>Projet :</strong> {esc(projet)}<br>
          <strong>Chargé de projet :</strong> {esc(charge_projet)}
        </p>
        """

    documents_line = ""
    if not (module == "Demande Aller voir" and notification_mode == "validation"):
        documents_line = "<p>Les documents associés sont disponibles dans ESI Tickets.</p>"

    body_html = f"""<!doctype html>
<html>
<body style="font-family:Arial,Helvetica,sans-serif;font-size:14px;color:#0f172a;line-height:1.45;">
<p>Bonjour,</p>
<p>{html.escape(intro)}</p>
{details}
{documents_line}
<p>
  <a href="{html.escape(ticket_url, quote=True)}" style="background:#0284c7;color:#ffffff;padding:10px 16px;text-decoration:none;border-radius:6px;display:inline-block;font-weight:bold;">
    {html.escape(link_text)}
  </a>
</p>
</body>
</html>"""

    body_text = f"""Bonjour,

{intro}

Dossier : {dossier or '-'}
Référence : {ref or '-'}
Prêteur : {preteur or '-'}

{link_text}
{ticket_url}

"""

    return {
        "to": email_dest,
        "subject": subject,
        "body_html": body_html,
        "body_text": body_text,
        "ticket_url": ticket_url,
        "link_text": link_text,
    }, None


@app.route('/api/tickets/<ticket_id>/notification-eml')
def api_ticket_notification_eml(ticket_id):
    """Génère un brouillon .eml HTML à ouvrir dans Outlook Desktop."""
    ticket = load_ticket(ticket_id)
    if not ticket:
        return jsonify({'error': 'Ticket introuvable'}), 404

    notification_mode = (request.args.get('mode') or 'final').strip()
    content, error = _build_notification_content(ticket, ticket_id, notification_mode)
    if error:
        return jsonify({'error': error}), 404

    msg = EmailMessage()
    msg['To'] = content['to']
    msg['Subject'] = content['subject']
    msg['Date'] = formatdate(localtime=True)
    msg['Message-ID'] = make_msgid(domain='esi-tickets.local')
    # Indique à Outlook que le fichier doit s'ouvrir comme un message non envoyé.
    msg['X-Unsent'] = '1'
    msg.set_content(
        content['body_text'],
        charset='utf-8',
        cte='8bit'
    )
    msg.add_alternative(
        content['body_html'],
        subtype='html',
        charset='utf-8',
        cte='8bit'
    )

    filename_base = "notification_" + safe_filename(ticket_id)
    eml_bytes = msg.as_bytes()

    import io
    return send_file(
        io.BytesIO(eml_bytes),
        as_attachment=True,
        download_name=f"{filename_base}.eml",
        mimetype='message/rfc822'
    )


@app.route('/api/tickets/<ticket_id>/manager-sheet', methods=['POST'])
def api_manager_sheet(ticket_id):
    ticket = load_ticket(ticket_id)
    if not ticket:
        return jsonify({'error': 'Ticket introuvable'}), 404

    files = request.files.getlist('files')
    if not files:
        single = request.files.get('file')
        if single:
            files = [single]

    valid_files = [fs for fs in files if fs and fs.filename]
    if not valid_files:
        return jsonify({'error': 'Fichier manquant'}), 400

    ticket_folder(ticket_id)  # conserve la création du dossier local historique
    manager_sheets = list(ticket.get('managerSheets') or [])
    legacy = ticket.get('managerSheet')
    if legacy and isinstance(legacy, dict) and legacy.get('name'):
        if not any(x.get('name') == legacy.get('name') for x in manager_sheets):
            manager_sheets.append(legacy)

    for fs in valid_files:
        content = fs.read()
        clean_name = safe_filename(fs.filename)
        storage_path = f"{ticket_id}/gestionnaire/{datetime.now().strftime('%Y%m%d%H%M%S')}_{clean_name}"

        try:
            supabase_upload_bytes(
                storage_path,
                content,
                fs.content_type
            )
        except Exception as e:
            print(f"[SUPABASE UPLOAD GESTIONNAIRE] Erreur : {e}")
            return jsonify({'ok': False, 'error': f'Erreur upload Supabase : {e}'}), 500

        manager_sheets = [x for x in manager_sheets if x.get('name') != fs.filename]
        manager_sheets.append({
            'name': fs.filename,
            'size': len(content),
            'path': storage_path
        })
    ticket['managerSheets'] = manager_sheets
    ticket['updatedAt'] = datetime.now().isoformat()
    save_ticket(ticket)
    return jsonify({'ok': True})

def _find_file_info(ticket, filename, kind):
    items = ticket.get('managerSheets') if kind == 'gestionnaire' else ticket.get('files')
    for f in items or []:
        if f.get('name') == filename:
            return f
    return None


def _redirect_to_signed_file(ticket_id, filename, kind):
    ticket = load_ticket(ticket_id)
    if not ticket:
        abort(404)

    file_info = _find_file_info(ticket, filename, kind)
    if not file_info:
        abort(404)

    storage_path = file_info.get('path')
    if not storage_path:
        abort(404)

    try:
        signed_url = supabase_signed_download_url(storage_path, expires_in=300)
    except Exception as e:
        # Secours : si l'URL signée échoue, on garde l'ancien comportement via Render.
        print(f"[SUPABASE SIGNED DOWNLOAD] Erreur, fallback Render : {e}")
        import io
        try:
            data = supabase_download_bytes(storage_path)
        except Exception as e2:
            print(f"[SUPABASE DOWNLOAD] Erreur : {e2}")
            abort(404)
        return send_file(io.BytesIO(data), as_attachment=True, download_name=filename)

    return redirect(signed_url)


@app.route('/api/tickets/<ticket_id>/download/<filename>')
def api_download_file(ticket_id, filename):
    return _redirect_to_signed_file(ticket_id, filename, 'demandeur')


@app.route('/api/tickets/<ticket_id>/download-sheet/<filename>')
def api_download_sheet(ticket_id, filename):
    return _redirect_to_signed_file(ticket_id, filename, 'gestionnaire')


@app.route('/api/tickets/<ticket_id>/fiche', methods=['GET'])
def api_get_fiche(ticket_id):
    ticket = load_ticket(ticket_id)
    if not ticket:
        return jsonify({'error': 'Ticket introuvable'}), 404
    return jsonify(ticket.get('fiche', {}))

@app.route('/api/tickets/<ticket_id>/fiche', methods=['POST'])
def api_save_fiche(ticket_id):
    ticket = load_ticket(ticket_id)
    if not ticket:
        return jsonify({'error': 'Ticket introuvable'}), 404
    data = request.get_json(silent=True) or {}
    longueur = data.get('longueur', '')
    largeur = data.get('largeur', '')
    hauteur = data.get('hauteur', '')
    dimensions_ext = " x ".join([v for v in [longueur, largeur, hauteur] if str(v).strip()])
    ancienne_fiche = ticket.get('fiche') or {}
    ticket['fiche'] = {
        'longueur': longueur,
        'largeur': largeur,
        'hauteur': hauteur,
        'dimensionsExt': dimensions_ext,
        'prixAchat': data.get('prixAchat', ''),
        'prixCession': data.get('prixCession', ''),
        'typeCaisseFiche': data.get('typeCaisseFiche', ''),
        'bilanCarbone': data.get('bilanCarbone', ''),
        'poids': data.get('poids', ''),
        'choixCaissier': data.get('choixCaissier', ''),
        # La localisation est renseignée depuis le planning réception.
        # On la conserve si la fiche est modifiée depuis l'écran gestionnaire.
        'localisation': ancienne_fiche.get('localisation', '')
    }
    save_ticket(ticket)
    return jsonify({'ok': True})



@app.route('/api/export/excel')
def api_export_excel():
    try:
        from openpyxl import Workbook
        import io
        import re
    except Exception:
        return jsonify({'error': "openpyxl non installé"}), 500

    tickets = list_tickets()

    wb = Workbook()
    ws = wb.active
    ws.title = "Tickets"

    ws.append([
        "ID","Module","Statut","Date création","Dossier / Client",
        "Réf / N° caisse","Chargé de projet","Projet / Expo",
        "Type de caisse","Dimensions","Prix devis",
        "Prix d'achat","Prix cession","Commentaire","Choix du caissier",
        "Date RDV","Heure RDV","Lieu RDV"
    ])

    def parse_euro(value):
        if value is None:
            return None
        txt = str(value).strip()
        if not txt or txt == '-':
            return None
        txt = txt.replace('\xa0', ' ').replace('€', '').replace(' ', '')
        txt = txt.replace(',', '.')
        txt = re.sub(r'[^0-9.\-]', '', txt)
        if not txt:
            return None
        try:
            return float(txt)
        except Exception:
            return None

    for t in tickets:
        fiche = t.get('fiche', {}) or {}
        ws.append([
            t.get('id',''),
            t.get('module',''),
            t.get('status',''),
            t.get('createdAt',''),
            t.get('dossier',''),
            t.get('ref',''),
            t.get('chargeProjet',''),
            t.get('expo') or t.get('objet',''),
            t.get('typeCaisse',''),
            t.get('dimensions',''),
            parse_euro(t.get('prixDevis','')),
            parse_euro(fiche.get('prixAchat','')),
            parse_euro(fiche.get('prixCession','')),
            t.get('commentaire',''),
            fiche.get('choixCaissier',''),
            t.get('dateRdv',''),
            t.get('heureRdv',''),
            t.get('lieuRdv','')
        ])

    for row in range(2, ws.max_row + 1):
        for col in [11, 12, 13]:
            ws.cell(row=row, column=col).number_format = '#,##0.00 €'

    output = io.BytesIO()
    wb.save(output)
    output.seek(0)

    return send_file(
        output,
        as_attachment=True,
        download_name="tickets_esi.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )



@app.route('/api/tickets/<ticket_id>/export-pdf')
def api_export_ticket_pdf(ticket_id):
    ticket = load_ticket(ticket_id)
    if not ticket:
        return jsonify({'error': 'Ticket introuvable'}), 404

    import io
    import textwrap

    def clean(value):
        if value is None or value == '':
            return '-'
        return str(value).replace('\r', ' ').replace('\n', ' ')

    def add_wrapped(lines, label, value):
        text = label + " : " + clean(value)
        for part in textwrap.wrap(text, width=82) or [text]:
            lines.append(part)

    lines = []
    lines.append("ESI TICKETS - DETAIL TICKET")
    lines.append("=" * 70)
    lines.append("")
    add_wrapped(lines, "ID", ticket.get('id'))
    add_wrapped(lines, "Module", ticket.get('module'))
    add_wrapped(lines, "Statut", ticket.get('status'))
    add_wrapped(lines, "Dossier / Client", ticket.get('dossier'))
    add_wrapped(lines, "Reference", ticket.get('ref'))
    add_wrapped(lines, "Charge de projet", ticket.get('chargeProjet'))
    add_wrapped(lines, "Projet / Expo", ticket.get('expo') or ticket.get('objet'))
    add_wrapped(lines, "Preteur", ticket.get('preteur'))
    add_wrapped(lines, "Type de caisse", ticket.get('typeCaisse'))
    add_wrapped(lines, "Dimensions", ticket.get('dimensions'))
    add_wrapped(lines, "Prix devis", ticket.get('prixDevis'))
    add_wrapped(lines, "Lieu RDV", ticket.get('lieuRdv'))
    add_wrapped(lines, "Date RDV", ticket.get('dateRdv'))
    add_wrapped(lines, "Heure RDV", ticket.get('heureRdv'))

    lines.append("")
    lines.append("COMMENTAIRE / INFORMATIONS")
    lines.append("-" * 70)
    commentaire = clean(ticket.get('commentaire'))
    for part in textwrap.wrap(commentaire, width=82) or ['-']:
        lines.append(part)

    fiche = ticket.get('fiche') or {}
    if fiche:
        lines.append("")
        lines.append("INFORMATIONS FICHE")
        lines.append("-" * 70)
        add_wrapped(lines, "Dimensions exterieures", fiche.get('dimensionsExt'))
        add_wrapped(lines, "Prix achat", fiche.get('prixAchat'))
        add_wrapped(lines, "Type caisse fiche", fiche.get('typeCaisseFiche'))
        add_wrapped(lines, "Bilan carbone", fiche.get('bilanCarbone'))
        add_wrapped(lines, "Poids", fiche.get('poids'))
        add_wrapped(lines, "Choix caissier", fiche.get('choixCaissier'))

    lines.append("")
    lines.append("DOCUMENTS DU DEMANDEUR")
    lines.append("-" * 70)
    files = ticket.get('files') or []
    if files:
        for f in files:
            lines.append("- " + clean(f.get('name')))
    else:
        lines.append("- Aucun document")

    manager_sheets = ticket.get('managerSheets') or []
    if manager_sheets:
        lines.append("")
        lines.append("DOCUMENTS GESTIONNAIRE")
        lines.append("-" * 70)
        for f in manager_sheets:
            lines.append("- " + clean(f.get('name')))

    lines.append("")
    lines.append("NOTES / ACTIONS A PREVOIR")
    lines.append("-" * 70)
    lines.append("")
    lines.append("_" * 70)
    lines.append("")
    lines.append("_" * 70)
    lines.append("")
    lines.append("_" * 70)

    def pdf_escape(value):
        value = str(value)
        value = value.replace("\\", "\\\\")
        value = value.replace("(", "\\(")
        value = value.replace(")", "\\)")
        return value

    page_width, page_height = 595, 842
    margin_left = 42
    y_start = 800
    line_height = 14
    max_lines = 53
    chunks = [lines[i:i+max_lines] for i in range(0, len(lines), max_lines)] or [["Ticket vide"]]

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        None,
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
    ]

    page_refs = []
    for chunk in chunks:
        content_obj_num = len(objects) + 1
        content_lines = ["BT", "/F1 10 Tf", f"{margin_left} {y_start} Td"]
        first = True
        for line in chunk:
            if not first:
                content_lines.append(f"0 -{line_height} Td")
            first = False
            content_lines.append(f"({pdf_escape(line)}) Tj")
        content_lines.append("ET")

        stream = "\n".join(content_lines).encode("latin-1", errors="replace")
        objects.append(f"<< /Length {len(stream)} >>\nstream\n".encode("latin-1") + stream + b"\nendstream")

        page_obj_num = len(objects) + 1
        page = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {page_width} {page_height}] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_obj_num} 0 R >>"
        )
        objects.append(page.encode("latin-1"))
        page_refs.append(f"{page_obj_num} 0 R")

    objects[1] = f"<< /Type /Pages /Kids [{' '.join(page_refs)}] /Count {len(page_refs)} >>".encode("latin-1")

    pdf = io.BytesIO()
    pdf.write(b"%PDF-1.4\n")
    offsets = []

    for i, obj in enumerate(objects, start=1):
        offsets.append(pdf.tell())
        pdf.write(f"{i} 0 obj\n".encode("latin-1"))
        pdf.write(obj)
        pdf.write(b"\nendobj\n")

    xref_pos = pdf.tell()
    pdf.write(f"xref\n0 {len(objects)+1}\n".encode("latin-1"))
    pdf.write(b"0000000000 65535 f \n")
    for offset in offsets:
        pdf.write(f"{offset:010d} 00000 n \n".encode("latin-1"))

    trailer = f"trailer\n<< /Size {len(objects)+1} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF"
    pdf.write(trailer.encode("latin-1"))
    pdf.seek(0)

    return send_file(
        pdf,
        as_attachment=True,
        download_name=f"{ticket.get('id','ticket')}.pdf",
        mimetype='application/pdf'
    )




@app.route('/api/restart')
def api_restart():
    import os
    os._exit(0)

@app.route('/splash')
def splash():
    return """
    <html>
    <head>
        <title>ESI Tickets</title>
        <style>
            body{margin:0;display:flex;justify-content:center;align-items:center;height:100vh;background:linear-gradient(180deg,#eef6fb,#f6f8fb);font-family:Arial;}
            .box{text-align:center;}
            img{width:120px;margin-bottom:20px;}
            h1{margin:0;color:#0284c7;}
            p{color:#64748b;}
        </style>
        <script>
            setTimeout(()=>{window.location.href="/demandeur";},1500);
        </script>
    </head>
    <body>
        <div class="box">
            <img id="splashLogo" src="/static/logo.jpg" onerror="
                const logos=['/static/logo.png','/static/logo%20esi.jpg'];
                const idx=Number(this.dataset.idx||0);
                if(idx<logos.length){this.dataset.idx=idx+1;this.src=logos[idx];}
                else{this.style.display='none';}
            ">
            <h1>ESI Tickets</h1>
            <p>Chargement en cours...</p>
        </div>
    </body>
    </html>
    """


@app.route('/api/tickets/<ticket_id>/validate-aller-voir', methods=['POST'])
def api_validate_aller_voir(ticket_id):
    ticket = load_ticket(ticket_id)
    if not ticket:
        return jsonify({'error': 'Ticket introuvable'}), 404
    ticket['status'] = 'En cours'
    ticket['validatedAt'] = datetime.now().isoformat()
    save_ticket(ticket)
    return jsonify({'ok': True})

@app.route('/api/tickets/<ticket_id>/calendar.ics')
def api_ticket_calendar_ics(ticket_id):
    ticket = load_ticket(ticket_id)
    if not ticket:
        return jsonify({'error': 'Ticket introuvable'}), 404

    date_rdv = ticket.get('dateRdv')
    heure_rdv = ticket.get('heureRdv')
    if not date_rdv or not heure_rdv or date_rdv == '-' or heure_rdv == '-':
        return jsonify({'error': 'Date/heure manquante'}), 400

    from datetime import timedelta
    start = datetime.fromisoformat(f"{date_rdv}T{heure_rdv}:00")
    end = start + timedelta(hours=2)

    def fmt(dt):
        return dt.strftime('%Y%m%dT%H%M%S')

    lieu = (ticket.get('lieuRdv') or '').strip()
    dossier = (ticket.get('dossier') or '').strip()
    summary = f"Aller voir - {lieu} - {dossier}"

    ics = f"""BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
DTSTART:{fmt(start)}
DTEND:{fmt(end)}
SUMMARY:{summary}
END:VEVENT
END:VCALENDAR"""

    from flask import Response
    return Response(ics, mimetype='text/calendar')



def open_browser():
    webbrowser.open('http://127.0.0.1:5050/splash')

ensure_shared_root()
init_db()

if __name__ == '__main__':
      app.run(host='127.0.0.1', port=5050, debug=False)
