from flask import Flask, render_template, jsonify, request, send_file, abort, redirect, url_for
from pathlib import Path
import json, webbrowser, os, urllib.request, urllib.parse
import csv, re, time
import hashlib, hmac, threading
import uuid
import shutil
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
            "error": "Format de Packing invalide"
        }

    try:
        reference_recherchee, numero_recherche = caisse_ref.rsplit("-", 1)
        reference_recherchee = reference_recherchee.strip()
        numero_recherche = str(int(numero_recherche.strip()))
    except Exception:
        return {
            "success": False,
            "caisse": caisse_ref,
            "error": "Format de Packing invalide"
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
        "error": "Packing introuvable dans le suivi fournisseur"
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


def supabase_delete_object(storage_path):
    """Supprime un objet de Supabase Storage. Un fichier déjà absent n'empêche pas la suppression du ticket."""
    if not storage_path:
        return False
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise RuntimeError("Variables SUPABASE_URL ou SUPABASE_SERVICE_KEY manquantes")

    safe_path = urllib.parse.quote(storage_path, safe="/")
    url = f"{SUPABASE_URL}/storage/v1/object/{SUPABASE_BUCKET}/{safe_path}"
    req = urllib.request.Request(
        url,
        method="DELETE",
        headers={
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "apikey": SUPABASE_KEY,
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()
        return True
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        print(f"[SUPABASE STORAGE DELETE ERROR] HTTP {e.code} - {e.reason} - {body}")
        raise RuntimeError(f"Erreur suppression fichier Supabase HTTP {e.code}: {body or e.reason}")


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


def _packing_reference(dossier, numero):
    """Référence métier affichée d'un Packing : C-N°dossier-N°packing.

    Les tickets historiques conservent leur champ technique ``ref`` tel quel
    afin de ne pas casser les rapprochements existants.
    """
    dossier = _as_text(dossier).strip()
    raw = _as_text(numero).strip()
    if not raw:
        return ''
    if raw.upper().startswith('C-'):
        return raw
    if not dossier:
        return raw
    prefix = dossier + '-'
    if raw.startswith(prefix):
        raw = raw[len(prefix):]
    try:
        normalized = str(int(float(raw.replace(',', '.'))))
    except Exception:
        normalized = raw
    if normalized.isdigit():
        normalized = normalized.zfill(2)
    return f"C-{dossier}-{normalized}"


def _packing_local_number(dossier, numero):
    """Numero local conserve dans ticket.ref pour compatibilite (ex. 01)."""
    dossier = _as_text(dossier).strip()
    raw = _as_text(numero).strip()
    if not raw:
        return ''
    if raw.upper().startswith('C-') and dossier and raw.upper().startswith(('C-' + dossier + '-').upper()):
        raw = raw[len(dossier) + 3:]
    elif dossier and raw.startswith(dossier + '-'):
        raw = raw[len(dossier) + 1:]
    try:
        normalized = str(int(float(raw.replace(',', '.'))))
    except Exception:
        normalized = raw
    if normalized.isdigit():
        normalized = normalized.zfill(2)
    return normalized


def _legacy_packing_reference(dossier, numero):
    """Référence historique dossier-numéro, utilisée seulement par les intégrations existantes."""
    dossier = _as_text(dossier).strip()
    raw = _as_text(numero).strip()
    if raw.upper().startswith('C-') and dossier and raw.upper().startswith(('C-' + dossier + '-').upper()):
        raw = raw[len(dossier) + 3:]
    elif dossier and raw.startswith(dossier + '-'):
        raw = raw[len(dossier) + 1:]
    try:
        normalized = str(int(float(raw.replace(',', '.'))))
    except Exception:
        normalized = raw
    if normalized.isdigit():
        normalized = normalized.zfill(2)
    return f"{dossier}-{normalized}" if dossier and normalized else (normalized or raw)


def _module_display_label(module):
    return 'Fiche de Packing' if _as_text(module).strip() == 'Fiche de caisse' else _as_text(module).strip()


def _display_prepacking_type(value):
    raw = _as_text(value).strip()
    return 'Packing bois' if raw.lower() in ('caisse bois', 'caisse_bois', 'caisse-bois', 'packing bois') else raw


# -----------------------------------------------------------------------------
# Type de Pre-Packing - données métier distinctes du N° de Pre-Packing
# -----------------------------------------------------------------------------
_COLIS_TYPE_LABELS = {
    "softpack": "Softpack",
    "carton": "Carton",
    "caisse bois": "Caisse bois",
    "caisse_bois": "Caisse bois",
    "caisse-bois": "Caisse bois",
}


def _normalise_colis_type(value):
    raw = _as_text(value).strip()
    if not raw:
        return ""
    key = raw.lower().replace("é", "e").replace("_", " ").replace("-", " ")
    key = " ".join(key.split())
    if key in ("softpack", "soft pack"):
        return "Softpack"
    if key == "carton":
        return "Carton"
    if key in ("caisse bois", "caisse en bois", "packing bois", "packing en bois", "bois"):
        return "Caisse bois"
    return ""


def _resolve_colis_types(raw_types, colis_refs):
    """Retourne {numero_colis: type}. Accepte une liste ordonnée ou un mapping."""
    refs = [_as_text(x).strip() for x in (colis_refs or []) if _as_text(x).strip()]
    result = {ref: "" for ref in refs}
    if isinstance(raw_types, dict):
        for ref in refs:
            result[ref] = _normalise_colis_type(raw_types.get(ref))
        return result
    if isinstance(raw_types, list):
        for i, ref in enumerate(refs):
            if i < len(raw_types):
                result[ref] = _normalise_colis_type(raw_types[i])
        return result
    return result


def _colis_display(colis_ref, colis_type=""):
    ref = _as_text(colis_ref).strip()
    typ = _display_prepacking_type(_normalise_colis_type(colis_type))
    if ref and typ:
        return f"{ref} - {typ.upper()}"
    return ref or typ


# -----------------------------------------------------------------------------
# QR code colis - accès mobile en lecture seule
# -----------------------------------------------------------------------------
def _colis_qr_secret():
    """Clé de signature des liens QR. Aucune donnée sensible n'est placée dans le QR."""
    secret = (
        os.getenv('ESI_COLIS_QR_SECRET')
        or SUPABASE_KEY
        or os.getenv('SECRET_KEY')
        or 'esi-tickets-colis-fallback'
    )
    return _as_text(secret).encode('utf-8')


def _colis_qr_token(colis_ref):
    colis_ref = _as_text(colis_ref).strip()
    return hmac.new(
        _colis_qr_secret(),
        colis_ref.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()[:32]


def _colis_qr_url(colis_ref):
    """URL absolue encodée dans l'étiquette colis."""
    colis_ref = _as_text(colis_ref).strip()
    token = _colis_qr_token(colis_ref)
    try:
        # Les QR sont destinés à être scannés depuis un téléphone : HTTPS obligatoire.
        return url_for(
            'prepacking_public_page',
            colis_ref=colis_ref,
            k=token,
            _external=True,
            _scheme='https',
        )
    except Exception:
        base = _as_text(os.getenv('ESI_PUBLIC_URL') or 'https://esi-tickets.onrender.com').rstrip('/')
        return f"{base}/pre-packing/{urllib.parse.quote(colis_ref, safe='-')}?k={urllib.parse.quote(token, safe='')}"


def _article_qr_token(esi_id):
    """Signature stable d'un lien QR d'article, distincte des liens colis."""
    esi_id = _as_text(esi_id).strip()
    return hmac.new(
        _colis_qr_secret(),
        ("article:" + esi_id).encode('utf-8'),
        hashlib.sha256
    ).hexdigest()[:32]


def _article_qr_url(esi_id):
    """URL absolue vers la carte d'identité mobile d'un article ESI."""
    esi_id = _as_text(esi_id).strip()
    token = _article_qr_token(esi_id)
    try:
        return url_for(
            'article_public_page',
            esi_id=esi_id,
            k=token,
            _external=True,
            _scheme='https',
        )
    except Exception:
        base = _as_text(os.getenv('ESI_PUBLIC_URL') or 'https://esi-tickets.onrender.com').rstrip('/')
        return f"{base}/article/{urllib.parse.quote(esi_id, safe='-')}?k={urllib.parse.quote(token, safe='')}"


def _colis_articles(colis_ref):
    """Retourne l'état actuel du colis depuis la base Articles."""
    colis_ref = _as_text(colis_ref).strip()
    if not colis_ref:
        return []
    safe_colis = urllib.parse.quote(colis_ref, safe='-_')
    rows = supabase_rest_request(
        'GET',
        'articles',
        'select=esi_id,dossier,reference,description,client,projet,longueur_cm,largeur_cm,'
        'hauteur_cm,poids_kg,lieu_stockage,statut_logistique,dernier_colis,'
        'derniere_reception_ref,raw_json,article_no'
        f'&dernier_colis=eq.{safe_colis}&order=article_no.asc&limit=5000'
    ) or []
    return [_article_row_to_public(row) for row in rows]


@app.route('/colis/<path:colis_ref>')
@app.route('/pre-packing/<path:colis_ref>', endpoint='prepacking_public_page')
def colis_public_page(colis_ref):
    """Fiche colis mobile ouverte depuis le QR code de l'étiquette."""
    colis_ref = _as_text(colis_ref).strip()
    supplied = _as_text(request.args.get('k')).strip()
    expected = _colis_qr_token(colis_ref)
    if not supplied or not hmac.compare_digest(supplied, expected):
        abort(403)

    try:
        articles = _colis_articles(colis_ref)
    except Exception as e:
        print(f'[QR COLIS] Lecture impossible pour {colis_ref}: {e}')
        return (
            '<!doctype html><html lang="fr"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<body style="font-family:Arial,sans-serif;padding:24px"><h2>Pre-Packing indisponible</h2>'
            '<p>Impossible de charger les informations du Pre-Packing pour le moment.</p></body></html>',
            503,
        )

    first = articles[0] if articles else {}
    esc = lambda v: html.escape(_as_text(v).strip() or '-', quote=True)

    dossier = first.get('dossier') or (colis_ref.rsplit('-', 1)[0] if '-' in colis_ref else '')
    client = first.get('client') or ''
    projet = first.get('projet') or ''
    lieu = first.get('lieu_stockage') or ''
    bon = first.get('derniere_reception_ref') or ''
    type_colis = first.get('type_colis') or ''

    cards = []
    for article in articles:
        dims = ' × '.join(
            _as_text(article.get(k)).strip()
            for k in ('longueur_cm', 'largeur_cm', 'hauteur_cm')
            if _as_text(article.get(k)).strip()
        )
        dims = (dims + ' cm') if dims else '-'
        poids = _as_text(article.get('poids_kg')).strip()
        poids = (poids + ' kg') if poids else '-'
        esi = esc(article.get('esi_id'))
        ref = esc(article.get('reference'))
        desc = esc(article.get('description'))
        part_label = _as_text(article.get('partie_label')).strip()
        parent_esi = _as_text(article.get('parent_esi')).strip()
        part_html = ''
        if part_label:
            part_html = f'<div class="part-info">Partie {esc(part_label)}' + (f' · Article principal {esc(parent_esi)}' if parent_esi else '') + '</div>'
        cards.append(f'''
          <article class="item">
            <div class="item-head"><strong>{esi}</strong><span>{ref}</span></div>
            <div class="designation">{desc}</div>
            {part_html}
            <div class="meta"><span><b>Dimensions</b>{esc(dims)}</span><span><b>Poids</b>{esc(poids)}</span></div>
          </article>
        ''')

    item_html = ''.join(cards) if cards else (
        '<div class="empty">Aucun Article n’est actuellement affecté à ce Pre-Packing.</div>'
    )
    count = len(articles)

    page = f'''<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Pre-Packing {esc(_colis_display(colis_ref, type_colis))} - ESI Tickets</title>
<style>
:root{{--blue:#0f2f4f;--light:#eef8fd;--line:#cfe3ee;--text:#17324a;--muted:#60758a}}
*{{box-sizing:border-box}}
body{{margin:0;background:#f4f8fb;color:var(--text);font-family:Arial,Helvetica,sans-serif}}
.wrap{{max-width:780px;margin:0 auto;padding:18px}}
.hero{{background:linear-gradient(135deg,#0f2f4f,#16476f);color:#fff;border-radius:20px;padding:22px;box-shadow:0 10px 28px rgba(15,47,79,.18)}}
.brand{{font-size:12px;font-weight:800;letter-spacing:.12em;opacity:.85}}
h1{{font-size:31px;line-height:1.05;margin:9px 0 5px}}
.sub{{font-size:13px;opacity:.88}}
.grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px;margin-top:14px}}
.field{{background:#fff;border:1px solid var(--line);border-radius:14px;padding:12px}}
.field b{{display:block;font-size:10px;text-transform:uppercase;color:var(--muted);margin-bottom:5px}}
.field div{{font-size:14px;font-weight:700;overflow-wrap:anywhere}}
.section-title{{display:flex;justify-content:space-between;gap:12px;align-items:center;margin:22px 2px 10px}}
.section-title h2{{font-size:17px;margin:0}}
.badge{{background:#dff3ff;color:#075985;border-radius:999px;padding:6px 10px;font-size:12px;font-weight:800}}
.items{{display:grid;gap:10px}}
.item{{background:#fff;border:1px solid var(--line);border-radius:16px;padding:14px;box-shadow:0 3px 12px rgba(15,47,79,.05)}}
.item-head{{display:flex;justify-content:space-between;gap:12px;align-items:flex-start}}
.item-head strong{{font-size:16px;color:#0369a1}}
.item-head span{{font-size:12px;font-weight:800;text-align:right;overflow-wrap:anywhere}}
.designation{{font-size:14px;margin-top:9px;line-height:1.35}}
.part-info{{margin-top:8px;display:inline-block;background:#e0f2fe;color:#075985;border-radius:999px;padding:5px 8px;font-size:11px;font-weight:900}}
.meta{{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:11px}}
.meta span{{background:var(--light);border-radius:10px;padding:8px;font-size:12px}}
.meta b{{display:block;font-size:9px;text-transform:uppercase;color:var(--muted);margin-bottom:3px}}
.empty{{background:#fff;border:1px solid var(--line);border-radius:16px;padding:18px;color:var(--muted)}}
.foot{{font-size:11px;color:var(--muted);text-align:center;margin:20px 0 8px}}
@media(max-width:520px){{.wrap{{padding:12px}}h1{{font-size:27px}}.grid{{grid-template-columns:1fr}}.meta{{grid-template-columns:1fr 1fr}}}}
</style>
</head>
<body>
<div class="wrap">
  <header class="hero">
    <div class="brand">ESI TICKETS · PRE-PACKING</div>
    <h1>{esc(_colis_display(colis_ref, type_colis))}</h1>
    <div class="sub">Informations en temps réel issues de la base Articles</div>
  </header>

  <section class="grid">
    <div class="field"><b>N° dossier</b><div>{esc(dossier)}</div></div>
    <div class="field"><b>Bon de réception</b><div>{esc(bon)}</div></div>
    <div class="field"><b>Type de Pre-Packing</b><div>{esc(_display_prepacking_type(type_colis))}</div></div>
    <div class="field"><b>Client</b><div>{esc(client)}</div></div>
    <div class="field"><b>Projet / exposition</b><div>{esc(projet)}</div></div>
    <div class="field" style="grid-column:1/-1"><b>Stockage actuel</b><div>{esc(lieu)}</div></div>
  </section>

  <div class="section-title"><h2>Articles contenus dans le Pre-Packing</h2><span class="badge">{count} article{'s' if count != 1 else ''}</span></div>
  <section class="items">{item_html}</section>
  <div class="foot">Page en lecture seule · ESI Tickets</div>
</div>
</body>
</html>'''
    response = app.make_response(page)
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['X-Robots-Tag'] = 'noindex, nofollow'
    return response


# -----------------------------------------------------------------------------
# Référentiel global des articles ESI
# -----------------------------------------------------------------------------
_ARTICLE_LOCK = threading.Lock()

# Colonnes utiles à la liste et aux fiches Articles.
# search_text est volontairement exclu des réponses : il reste utilisé côté Supabase
# pour la recherche, mais peut être très volumineux car il duplique une grande partie
# de raw_json.
_ARTICLE_LIST_SELECT = (
    "esi_id,article_no,source_module,type_objet,reference,description,dossier,client,projet,"
    "ref_caisse,longueur_cm,largeur_cm,hauteur_cm,poids_kg,lieu_stockage,"
    "statut_logistique,dernier_colis,derniere_reception_ref,created_at,updated_at,raw_json"
)
_ARTICLE_DETAIL_SELECT = (
    "esi_id,article_no,ticket_id,source_module,source_index,unit_index,type_objet,reference,description,"
    "dossier,client,projet,ref_caisse,transporteur_ref,longueur_cm,largeur_cm,hauteur_cm,"
    "volume_m3,surface_m2,poids_kg,lieu_stockage,statut_logistique,dernier_colis,"
    "derniere_reception_ref,created_at,updated_at,raw_json"
)


def _article_quantity(value, default=1):
    try:
        n = int(float(str(value or default).replace(",", ".")))
        return max(1, n)
    except Exception:
        return default


def _article_search_text(article):
    """Concatène toutes les données connues afin qu'une seule recherche retrouve l'article."""
    values = []
    for key, value in (article or {}).items():
        if key == "raw_json":
            try:
                values.append(json.dumps(value, ensure_ascii=False, sort_keys=True))
            except Exception:
                values.append(_as_text(value))
        elif isinstance(value, (dict, list)):
            try:
                values.append(json.dumps(value, ensure_ascii=False, sort_keys=True))
            except Exception:
                values.append(_as_text(value))
        else:
            values.append(_as_text(value))
    return " | ".join(x.strip() for x in values if _as_text(x).strip())


def _article_payload_from_item(ticket, item, source_module=None, source_index=None, unit_index=1):
    """Transforme une ligne de marchandise en article physique (quantité = 1)."""
    item = dict(item or {})
    module = source_module or ticket.get("module") or ""
    avis = ticket.get("avisArrivee") or ticket.get("avis_arrivee") or {}
    enl = ticket.get("enlevement") or {}

    if module == "Avis d'arrivée":
        dossier = avis.get("dossier_ref") or ticket.get("dossier") or ""
        client = avis.get("client") or ticket.get("dossier") or ""
        projet = avis.get("projet") or ticket.get("expo") or ticket.get("objet") or ""
        reference = item.get("reference") or ""
        description = item.get("description") or ""
        poids = item.get("poids_kg") or ""
        longueur = item.get("longueur_cm") or ""
        largeur = item.get("largeur_cm") or ""
        hauteur = item.get("hauteur_cm") or ""
        volume = item.get("volume_m3") or ""
        surface = item.get("surface_m2") or ""
        ref_caisse = item.get("ref_caisse") or ""
        transporteur_ref = ((avis.get("transporteur") or {}).get("reference") or "")
        charge_projet = avis.get("coordinateur") or ticket.get("chargeProjet") or ""
    else:
        dossier = (
            enl.get("numero_dossier")
            or enl.get("dossier_numero")
            or ticket.get("numeroDossier")
            or ticket.get("numero_dossier")
            or ""
        )
        client = enl.get("client") or ticket.get("client") or ticket.get("dossier") or ""
        projet = enl.get("exhibition") or ticket.get("expo") or ticket.get("objet") or ""
        reference = item.get("reference") or item.get("ref") or ""
        description = item.get("designation") or item.get("description") or ""
        poids = item.get("poids_kg") or item.get("poids") or ""
        dims = _as_text(item.get("dimensions")).strip()
        longueur = item.get("longueur_cm") or ""
        largeur = item.get("largeur_cm") or ""
        hauteur = item.get("hauteur_cm") or ""
        if dims and not any((longueur, largeur, hauteur)):
            parts = [x.strip() for x in re.split(r"[xX×]", dims)]
            if len(parts) >= 1: longueur = parts[0]
            if len(parts) >= 2: largeur = parts[1]
            if len(parts) >= 3: hauteur = parts[2]
        volume = item.get("volume_m3") or ""
        surface = item.get("surface_m2") or ""
        ref_caisse = item.get("ref_caisse") or ""
        transporteur_ref = enl.get("numero_bon") or ticket.get("ref") or ""
        charge_projet = enl.get("coordinateur") or ticket.get("chargeProjet") or ""

    created_at = ticket.get("createdAt") or datetime.now().isoformat()
    payload = {
        "ticket_id": _as_text(ticket.get("id")).strip(),
        "source_module": _as_text(module).strip(),
        "source_index": int(source_index) if source_index is not None else None,
        "unit_index": int(unit_index),
        "reference": _as_text(reference).strip(),
        "description": _as_text(description).strip(),
        "dossier": _as_text(dossier).strip(),
        "client": _as_text(client).strip(),
        "projet": _as_text(projet).strip(),
        "ref_caisse": _as_text(ref_caisse).strip(),
        "transporteur_ref": _as_text(transporteur_ref).strip(),
        "longueur_cm": _as_text(longueur).strip(),
        "largeur_cm": _as_text(largeur).strip(),
        "hauteur_cm": _as_text(hauteur).strip(),
        "volume_m3": _as_text(volume).strip(),
        "surface_m2": _as_text(surface).strip(),
        "poids_kg": _as_text(poids).strip(),
        "lieu_stockage": _as_text(item.get("lieu_stockage")).strip(),
        "statut_logistique": "Créé",
        "created_at": _as_text(created_at).strip(),
        "updated_at": datetime.now().isoformat(),
        "raw_json": {
            "ticket_id": ticket.get("id"),
            "module": module,
            "source_index": source_index,
            "unit_index": unit_index,
            "item": item,
            "article_fields": {
                "charge_projet": _as_text(charge_projet).strip(),
            },
        },
    }
    payload["search_text"] = _article_search_text(payload)
    return payload


_ARTICLE_EXTRA_FIELDS = {
    "charge_projet", "type_colis", "numero_colis", "colis_esi", "articles_lies",
    "categorie_metier", "packing_reference", "packing_ticket_id", "packing_type",
    "prepackings_lies", "mise_en_caisse_ticket_id", "mise_en_caisse_date",
    "parent_esi", "partie_label", "partie_index", "partie_total", "article_en_plusieurs_parties",
    "oeuvre_reference", "artiste", "oeuvre_titre", "oeuvre_technique",
    "oeuvre_longueur_cm", "oeuvre_largeur_cm", "oeuvre_hauteur_cm",
    "oeuvre_volume_m3", "oeuvre_surface_m2", "oeuvre_poids_kg",
    "statut_douanier", "ima_numero", "ima_date",
}


def _article_row_to_public(row):
    """Expose aussi les champs métier stockés dans raw_json sans modifier le schéma Supabase."""
    row = dict(row or {})
    article_no = row.get("article_no")
    if not row.get("esi_id") and article_no is not None:
        row["esi_id"] = f"ESI-{article_no}"

    raw = row.get("raw_json") if isinstance(row.get("raw_json"), dict) else {}
    extra = raw.get("article_fields") if isinstance(raw.get("article_fields"), dict) else {}
    for field in _ARTICLE_EXTRA_FIELDS:
        value = extra.get(field)
        if value is None:
            value = raw.get(field)
        if value is None and field == "charge_projet":
            value = raw.get("chargeProjet")
        row[field] = _as_text(value).strip()

    # Catégorie métier visible dans l'interface. La colonne technique type_objet
    # reste PRODUIT / CONTENANT pour conserver le schéma Supabase existant.
    category = _as_text(row.get("categorie_metier")).strip().upper()
    if category not in ("ARTICLE", "PRE-PACKING", "PACKING"):
        technical_type = _as_text(row.get("type_objet") or "PRODUIT").strip().upper()
        source_module = _as_text(row.get("source_module")).strip().lower()
        reference = _as_text(row.get("reference")).strip().upper()
        raw_source = _as_text(raw.get("source")).strip().lower()
        if technical_type == "CONTENANT":
            if raw_source == "packing_ticket" or source_module in ("fiche de packing", "fiche de caisse") or reference.startswith("C-"):
                category = "PACKING"
            else:
                category = "PRE-PACKING"
        else:
            category = "ARTICLE"
    row["categorie_metier"] = category
    return row


def _article_row_to_list_public(row):
    """Version légère pour la grille : conserve les champs métier mais pas raw_json."""
    public = _article_row_to_public(row)
    public.pop("raw_json", None)
    public.pop("search_text", None)
    return public


def _article_number(value):
    try:
        text = _as_text(value).strip().replace(" ", "").replace(",", ".")
        return float(text) if text else None
    except Exception:
        return None


def _article_metric_text(value):
    if value is None:
        return ""
    return (f"{value:.6f}").rstrip("0").rstrip(".")


def _article_calculated_metrics(longueur, largeur, hauteur):
    """Calcule volume (m³) et surface au sol (m²) à partir de dimensions en cm."""
    l = _article_number(longueur)
    w = _article_number(largeur)
    h = _article_number(hauteur)
    surface = _article_metric_text((l * w / 10000.0) if l is not None and w is not None else None)
    volume = _article_metric_text((l * w * h / 1000000.0) if l is not None and w is not None and h is not None else None)
    return volume, surface


def _create_article_record(payload):
    """Insère un article et retourne sa ligne avec son identifiant ESI."""
    rows = supabase_rest_request(
        "POST",
        "articles",
        "",
        [payload],
        prefer="return=representation"
    ) or []
    if not rows:
        raise RuntimeError("Supabase n'a pas retourné l'article créé.")
    return _article_row_to_public(rows[0])


def _colis_member_summaries(selected_items, colis_by_esi):
    """Construit les liens Colis -> articles a partir de la repartition validee."""
    summaries = {}
    for item in selected_items or []:
        part_map = item.get('partie_par_esi') if isinstance(item.get('partie_par_esi'), dict) else {}
        parent_map = item.get('parent_esi_par_esi') if isinstance(item.get('parent_esi_par_esi'), dict) else {}
        for esi_id in item.get('esi_ids') or []:
            esi_id = _as_text(esi_id).strip()
            colis_ref = _as_text((colis_by_esi or {}).get(esi_id)).strip()
            if not esi_id or not colis_ref:
                continue
            summaries.setdefault(colis_ref, []).append({
                'esi_id': esi_id,
                'reference': _as_text(item.get('reference')).strip(),
                'description': _as_text(item.get('designation') or item.get('description')).strip(),
                'partie_label': _as_text(part_map.get(esi_id)).strip(),
                'parent_esi': _as_text(parent_map.get(esi_id)).strip(),
            })
    return summaries


def _find_colis_record(colis_ref, dossier=''):
    """Retrouve la ligne COLIS (stockee historiquement comme CONTENANT) dans la base Articles."""
    colis_ref = _as_text(colis_ref).strip()
    dossier = _as_text(dossier).strip()
    if not colis_ref:
        return None
    query = (
        'select=*'
        '&type_objet=eq.CONTENANT'
        '&reference=eq.' + urllib.parse.quote(colis_ref, safe='-_')
    )
    if dossier:
        query += '&dossier=eq.' + urllib.parse.quote(dossier, safe='')
    query += '&order=article_no.desc&limit=1'
    rows = supabase_rest_request('GET', 'articles', query) or []
    return dict(rows[0]) if rows else None


def _ensure_colis_article_records(ticket_id, numero_dossier, colis_refs, colis_types, colis_by_esi,
                                  selected_items, client='', projet='', charge_projet='',
                                  lieu_stockage='', reception_ref=''):
    """
    Cree/met a jour une ligne COLIS dans la table Articles pour chaque colis physique.

    Compatibilite schema : la valeur technique type_objet reste CONTENANT pour ne pas
    imposer de migration SQL, mais l'interface l'affiche partout comme COLIS.
    Chaque ligne colis conserve la liste des N ESI contenus et chaque article recoit
    en retour le N ESI de son colis via article_fields.colis_esi.
    """
    numero_dossier = _as_text(numero_dossier).strip()
    now = datetime.now().isoformat()
    members_by_colis = _colis_member_summaries(selected_items, colis_by_esi)
    colis_esi_by_ref = {}
    records = []

    for idx, colis_ref in enumerate(colis_refs or [], start=1):
        colis_ref = _as_text(colis_ref).strip()
        if not colis_ref:
            continue
        colis_type = _normalise_colis_type((colis_types or {}).get(colis_ref))
        members = members_by_colis.get(colis_ref, [])
        member_ids = [_as_text(x.get('esi_id')).strip() for x in members if _as_text(x.get('esi_id')).strip()]
        description = f"{_display_prepacking_type(colis_type) or 'Pre-Packing'} - {len(member_ids)} Article{'s' if len(member_ids) != 1 else ''}"

        existing = _find_colis_record(colis_ref, numero_dossier)
        if existing:
            raw = existing.get('raw_json') if isinstance(existing.get('raw_json'), dict) else {}
            raw = dict(raw or {})
            extra = raw.get('article_fields') if isinstance(raw.get('article_fields'), dict) else {}
            extra = dict(extra or {})
            extra.update({
                'charge_projet': _as_text(charge_projet).strip(),
                'type_colis': colis_type,
                'numero_colis': colis_ref,
                'articles_lies': ', '.join(member_ids),
            })
            raw.update({
                'source': 'colis_reception',
                'ticket_id': _as_text(ticket_id).strip(),
                'reception_ref': _as_text(reception_ref).strip(),
                'colis_ref': colis_ref,
                'colis_type': colis_type,
                'article_esi_ids': member_ids,
                'articles_lies': members,
                'updated_at': now,
                'article_fields': extra,
            })
            patch = {
                'ticket_id': _as_text(ticket_id).strip() or existing.get('ticket_id'),
                'source_module': 'Pre-Packing réception',
                'unit_index': idx,
                'type_objet': 'CONTENANT',
                'reference': colis_ref,
                'description': description,
                'dossier': numero_dossier,
                'client': _as_text(client).strip(),
                'projet': _as_text(projet).strip(),
                'transporteur_ref': _as_text(reception_ref).strip(),
                'lieu_stockage': _as_text(lieu_stockage).strip(),
                'statut_logistique': 'Réceptionné',
                'derniere_reception_ref': _as_text(reception_ref).strip(),
                # Un colis ne doit pas se contenir lui-meme : dernier_colis reste vide.
                'dernier_colis': '',
                'updated_at': now,
                'raw_json': raw,
            }
            merged = dict(existing)
            merged.update(patch)
            patch['search_text'] = _article_search_text(merged)
            safe_esi = urllib.parse.quote(_as_text(existing.get('esi_id')).strip(), safe='-')
            rows = supabase_rest_request(
                'PATCH', 'articles', f'esi_id=eq.{safe_esi}', patch, prefer='return=representation'
            ) or []
            record = _article_row_to_public(rows[0] if rows else merged)
        else:
            article_fields = {
                'charge_projet': _as_text(charge_projet).strip(),
                'type_colis': colis_type,
                'numero_colis': colis_ref,
                'articles_lies': ', '.join(member_ids),
            }
            payload = {
                'ticket_id': _as_text(ticket_id).strip(),
                'source_module': 'Pre-Packing réception',
                'source_index': None,
                'unit_index': idx,
                'type_objet': 'CONTENANT',
                'reference': colis_ref,
                'description': description,
                'dossier': numero_dossier,
                'client': _as_text(client).strip(),
                'projet': _as_text(projet).strip(),
                'ref_caisse': '',
                'transporteur_ref': _as_text(reception_ref).strip(),
                'longueur_cm': '',
                'largeur_cm': '',
                'hauteur_cm': '',
                'volume_m3': '',
                'surface_m2': '',
                'poids_kg': '',
                'lieu_stockage': _as_text(lieu_stockage).strip(),
                'statut_logistique': 'Réceptionné',
                'dernier_colis': '',
                'derniere_reception_ref': _as_text(reception_ref).strip(),
                'created_at': now,
                'updated_at': now,
                'raw_json': {
                    'source': 'colis_reception',
                    'ticket_id': _as_text(ticket_id).strip(),
                    'reception_ref': _as_text(reception_ref).strip(),
                    'colis_ref': colis_ref,
                    'colis_type': colis_type,
                    'article_esi_ids': member_ids,
                    'articles_lies': members,
                    'created_at': now,
                    'article_fields': article_fields,
                },
            }
            payload['search_text'] = _article_search_text(payload)
            record = _create_article_record(payload)

        esi_colis = _as_text(record.get('esi_id')).strip()
        if esi_colis:
            colis_esi_by_ref[colis_ref] = esi_colis
        records.append(record)

    return records, colis_esi_by_ref


def _packing_article_link_summaries(ticket):
    """Retourne les Articles actuellement liés à une fiche Packing."""
    result = []
    for item in ticket.get('articles_lies') or []:
        if isinstance(item, dict):
            esi_id = _as_text(item.get('esi_id')).strip()
            if not esi_id:
                continue
            result.append({
                'esi_id': esi_id,
                'dossier': _as_text(item.get('dossier')).strip(),
                'reference': _as_text(item.get('reference')).strip(),
            })
        else:
            esi_id = _as_text(item).strip()
            if esi_id:
                result.append({'esi_id': esi_id, 'dossier': '', 'reference': ''})
    return result


def _find_packing_article_record(ticket):
    """Retrouve la ligne Base Articles correspondant à une fiche Packing."""
    ticket_id = _as_text(ticket.get('id')).strip()
    dossier = _as_text(ticket.get('dossier')).strip()
    reference = _packing_reference(dossier, ticket.get('ref'))

    candidates = []
    if ticket_id:
        safe_tid = urllib.parse.quote(ticket_id, safe='')
        candidates.extend(supabase_rest_request(
            'GET', 'articles', f'select=*&ticket_id=eq.{safe_tid}&type_objet=eq.CONTENANT&limit=20'
        ) or [])
    if reference:
        safe_ref = urllib.parse.quote(reference, safe='-_')
        candidates.extend(supabase_rest_request(
            'GET', 'articles', f'select=*&reference=eq.{safe_ref}&type_objet=eq.CONTENANT&limit=20'
        ) or [])

    seen = set()
    for row in candidates:
        esi_id = _as_text(row.get('esi_id')).strip()
        if esi_id in seen:
            continue
        seen.add(esi_id)
        public = _article_row_to_public(row)
        if public.get('categorie_metier') == 'PACKING':
            return dict(row)
    return None


def _ensure_packing_article_record(ticket):
    """
    Crée ou met à jour le Packing dans la Base Articles.

    Le Packing conserve un N° ESI comme les autres éléments. Techniquement, il reste
    stocké avec type_objet=CONTENANT afin d'éviter toute migration SQL ; la catégorie
    métier PACKING est enregistrée dans raw_json.article_fields.categorie_metier.
    """
    if _as_text(ticket.get('module')).strip() != 'Fiche de caisse':
        return None, False

    dossier = _as_text(ticket.get('dossier')).strip()
    numero = _as_text(ticket.get('ref')).strip()
    if not dossier or not numero:
        return None, False

    reference = _packing_reference(dossier, numero)
    fiche = ticket.get('fiche') if isinstance(ticket.get('fiche'), dict) else {}
    fiche = dict(fiche or {})
    reception = ticket.get('reception') if isinstance(ticket.get('reception'), dict) else {}
    reception = dict(reception or {})
    linked = _packing_article_link_summaries(ticket)
    linked_ids = [x['esi_id'] for x in linked if x.get('esi_id')]
    prepacking_linked = []
    for item in ticket.get('prepackings_lies') or []:
        if isinstance(item, dict):
            esi_id = _as_text(item.get('esi_id')).strip()
            if not esi_id:
                continue
            prepacking_linked.append({
                'esi_id': esi_id,
                'reference': _as_text(item.get('reference')).strip(),
                'dossier': _as_text(item.get('dossier')).strip(),
            })
        else:
            esi_id = _as_text(item).strip()
            if esi_id:
                prepacking_linked.append({'esi_id': esi_id, 'reference': '', 'dossier': dossier})
    prepacking_ids = list(dict.fromkeys(x['esi_id'] for x in prepacking_linked if x.get('esi_id')))

    existing = _find_packing_article_record(ticket)
    existing_raw = existing.get('raw_json') if existing and isinstance(existing.get('raw_json'), dict) else {}
    raw = dict(existing_raw or {})
    extra = raw.get('article_fields') if isinstance(raw.get('article_fields'), dict) else {}
    extra = dict(extra or {})

    packing_type = _as_text(fiche.get('typeCaisseFiche') or ticket.get('typeCaisse')).strip()
    localisation = _as_text(fiche.get('localisation')).strip()
    poids = _as_text(fiche.get('poids')).strip()
    statut_ticket = _as_text(ticket.get('status')).strip() or 'Demande créée'
    receptionnee = bool(reception.get('receptionnee') is True or reception.get('receptionnee_le') or localisation)
    statut_logistique = 'Réceptionné' if receptionnee else statut_ticket
    bl_numero = _as_text(reception.get('bl_numero')).strip()
    now = datetime.now().isoformat()

    extra.update({
        'categorie_metier': 'PACKING',
        'charge_projet': _as_text(ticket.get('chargeProjet')).strip(),
        'packing_reference': reference,
        'packing_ticket_id': _as_text(ticket.get('id')).strip(),
        'packing_type': packing_type,
        'articles_lies': ', '.join(linked_ids),
        'prepackings_lies': ', '.join(prepacking_ids),
    })
    raw.update({
        'source': 'packing_ticket',
        'ticket_id': _as_text(ticket.get('id')).strip(),
        'packing_reference': reference,
        'packing_local_number': _packing_local_number(dossier, numero),
        'packing_type': packing_type,
        'articles_lies': linked,
        'packing_article_esi_ids': linked_ids,
        'prepackings_lies': prepacking_linked,
        'packing_prepacking_esi_ids': prepacking_ids,
        'status_ticket': statut_ticket,
        'reception': reception,
        'article_fields': extra,
        'updated_at': now,
    })

    description_parts = ['Packing']
    if packing_type and packing_type != '-':
        description_parts.append(packing_type)
    if linked_ids:
        description_parts.append(f"{len(linked_ids)} Article{'s' if len(linked_ids) != 1 else ''}")
    description = ' - '.join(description_parts)

    payload = {
        'ticket_id': _as_text(ticket.get('id')).strip(),
        'source_module': 'Fiche de Packing',
        'source_index': None,
        'unit_index': 1,
        'type_objet': 'CONTENANT',
        'reference': reference,
        'description': description,
        'dossier': dossier,
        'client': '' if _as_text(ticket.get('preteur')).strip() in ('', '-') else _as_text(ticket.get('preteur')).strip(),
        'projet': '' if _as_text(ticket.get('expo') or ticket.get('objet')).strip() in ('', '-') else _as_text(ticket.get('expo') or ticket.get('objet')).strip(),
        'ref_caisse': '',
        'transporteur_ref': bl_numero,
        'longueur_cm': _as_text(fiche.get('longueur')).strip(),
        'largeur_cm': _as_text(fiche.get('largeur')).strip(),
        'hauteur_cm': _as_text(fiche.get('hauteur')).strip(),
        'poids_kg': poids,
        'lieu_stockage': localisation,
        'statut_logistique': statut_logistique,
        'dernier_colis': '',
        'derniere_reception_ref': bl_numero,
        'updated_at': now,
        'raw_json': raw,
    }
    volume, surface = _article_calculated_metrics(
        payload['longueur_cm'], payload['largeur_cm'], payload['hauteur_cm']
    )
    payload['volume_m3'] = volume
    payload['surface_m2'] = surface

    if existing:
        payload['created_at'] = existing.get('created_at') or ticket.get('createdAt') or now
        merged = dict(existing)
        merged.update(payload)
        payload['search_text'] = _article_search_text(merged)
        safe_esi = urllib.parse.quote(_as_text(existing.get('esi_id')).strip(), safe='-')
        rows = supabase_rest_request(
            'PATCH', 'articles', f'esi_id=eq.{safe_esi}', payload, prefer='return=representation'
        ) or []
        return _article_row_to_public(rows[0] if rows else merged), False

    payload['created_at'] = _as_text(ticket.get('createdAt')).strip() or now
    payload['search_text'] = _article_search_text(payload)
    return _create_article_record(payload), True


def _sync_existing_packings_to_articles(tickets=None):
    """Synchronise toutes les fiches Packing existantes vers la Base Articles."""
    if tickets is None:
        tickets = list_tickets()
    stats = {'packings_traites': 0, 'packings_crees': 0, 'packings_mis_a_jour': 0, 'errors': []}
    for ticket in tickets:
        if _as_text(ticket.get('module')).strip() != 'Fiche de caisse':
            continue
        try:
            record, created = _ensure_packing_article_record(ticket)
            if record:
                stats['packings_traites'] += 1
                if created:
                    stats['packings_crees'] += 1
                else:
                    stats['packings_mis_a_jour'] += 1
        except Exception as e:
            stats['errors'].append({'ticket_id': ticket.get('id'), 'error': str(e)})
    return stats


def _delete_packing_article_record(ticket):
    """Supprime uniquement la ligne synthétique PACKING lors de la suppression du ticket Packing."""
    existing = _find_packing_article_record(ticket)
    if not existing:
        return
    esi_id = _as_text(existing.get('esi_id')).strip()
    if esi_id:
        supabase_rest_request(
            'DELETE', 'articles', 'esi_id=eq.' + urllib.parse.quote(esi_id, safe='-'), prefer='return=minimal'
        )


def _article_part_meta(row):
    # Métadonnées de composition stockées dans raw_json pour éviter une migration SQL.
    row = dict(row or {})
    raw = row.get("raw_json") if isinstance(row.get("raw_json"), dict) else {}
    extra = raw.get("article_fields") if isinstance(raw.get("article_fields"), dict) else {}
    parent_esi = _as_text(extra.get("parent_esi") or raw.get("parent_esi")).strip()
    try:
        partie_index = int(extra.get("partie_index") or raw.get("partie_index") or 0)
    except Exception:
        partie_index = 0
    try:
        partie_total = int(extra.get("partie_total") or raw.get("partie_total") or 0)
    except Exception:
        partie_total = 0
    partie_label = _as_text(extra.get("partie_label") or raw.get("partie_label")).strip()
    if not partie_label and partie_index > 0 and partie_total > 1:
        partie_label = f"{partie_index}/{partie_total}"
    return {
        "parent_esi": parent_esi,
        "partie_index": partie_index,
        "partie_total": partie_total,
        "partie_label": partie_label,
    }


def _article_parts_for_parent(parent_esi, dossier=""):
    parent_esi = _as_text(parent_esi).strip()
    dossier = _as_text(dossier).strip()
    if not parent_esi:
        return []
    query = "select=" + _ARTICLE_DETAIL_SELECT + "&order=article_no.asc&limit=5000"
    if dossier:
        query += "&dossier=eq." + urllib.parse.quote(dossier, safe='')
    rows = supabase_rest_request("GET", "articles", query) or []
    parts = []
    for row in rows:
        meta = _article_part_meta(row)
        if meta.get("parent_esi") != parent_esi:
            continue
        public = _article_row_to_public(row)
        public.update(meta)
        parts.append(public)
    parts.sort(key=lambda x: (int(x.get("partie_index") or 0), int(x.get("article_no") or 0)))
    return parts


def _sync_parent_parts_logistics(parent_esi):
    parent_esi = _as_text(parent_esi).strip()
    if not parent_esi:
        return
    safe_parent = urllib.parse.quote(parent_esi, safe='-')
    parent_rows = supabase_rest_request("GET", "articles", f"select=*&esi_id=eq.{safe_parent}&limit=1") or []
    if not parent_rows:
        return
    parent = dict(parent_rows[0])
    parts = _article_parts_for_parent(parent_esi, parent.get("dossier"))
    if not parts:
        return

    raw = parent.get("raw_json") if isinstance(parent.get("raw_json"), dict) else {}
    raw = dict(raw or {})
    extra = raw.get("article_fields") if isinstance(raw.get("article_fields"), dict) else {}
    extra = dict(extra or {})
    extra["article_en_plusieurs_parties"] = "Oui"
    extra["parties_total"] = str(len(parts))
    raw["article_fields"] = extra
    raw["parties"] = [
        {
            "esi_id": _as_text(x.get("esi_id")).strip(),
            "partie_label": _as_text(x.get("partie_label")).strip(),
            "dernier_colis": _as_text(x.get("dernier_colis")).strip(),
            "type_colis": _as_text(x.get("type_colis")).strip(),
            "lieu_stockage": _as_text(x.get("lieu_stockage")).strip(),
            "statut_logistique": _as_text(x.get("statut_logistique")).strip(),
        }
        for x in parts
    ]

    received = [x for x in parts if _as_text(x.get("statut_logistique")).strip().lower().startswith("réceptionné")]
    colis = list(dict.fromkeys(_as_text(x.get("dernier_colis")).strip() for x in parts if _as_text(x.get("dernier_colis")).strip()))
    types = list(dict.fromkeys(_as_text(x.get("type_colis")).strip() for x in parts if _as_text(x.get("type_colis")).strip()))
    lieux = list(dict.fromkeys(_as_text(x.get("lieu_stockage")).strip() for x in parts if _as_text(x.get("lieu_stockage")).strip()))
    refs = list(dict.fromkeys(_as_text(x.get("derniere_reception_ref")).strip() for x in parts if _as_text(x.get("derniere_reception_ref")).strip()))

    if len(received) == len(parts):
        statut = f"Réceptionné ({len(parts)} parties)"
    elif received:
        statut = f"Réception partielle ({len(received)}/{len(parts)} parties)"
    else:
        statut = f"Article en {len(parts)} parties"

    if len(types) == 1:
        extra["type_colis"] = types[0]
    elif len(types) > 1:
        extra["type_colis"] = "Plusieurs types"
    else:
        extra["type_colis"] = ""
    raw["article_fields"] = extra

    patch = {
        "statut_logistique": statut,
        "dernier_colis": ", ".join(colis),
        "lieu_stockage": lieux[0] if len(lieux) == 1 else ("Plusieurs emplacements" if lieux else ""),
        "derniere_reception_ref": refs[-1] if refs else "",
        "updated_at": datetime.now().isoformat(),
        "raw_json": raw,
    }
    merged = dict(parent)
    merged.update(patch)
    patch["search_text"] = _article_search_text(merged)
    supabase_rest_request("PATCH", "articles", f"esi_id=eq.{safe_parent}", patch, prefer="return=minimal")


def _ensure_article_parts(parent_esi, total_parts):
    parent_esi = _as_text(parent_esi).strip()
    try:
        total_parts = int(total_parts)
    except Exception:
        total_parts = 0
    if not parent_esi or total_parts < 2 or total_parts > 20:
        raise ValueError("Le nombre de parties doit être compris entre 2 et 20.")

    safe_parent = urllib.parse.quote(parent_esi, safe='-')
    parent_rows = supabase_rest_request("GET", "articles", f"select=*&esi_id=eq.{safe_parent}&limit=1") or []
    if not parent_rows:
        raise ValueError(f"Article principal {parent_esi} introuvable.")
    parent = dict(parent_rows[0])
    dossier = _as_text(parent.get("dossier")).strip()
    existing = _article_parts_for_parent(parent_esi, dossier)

    if existing:
        valid = len(existing) == total_parts and [int(x.get("partie_index") or 0) for x in existing] == list(range(1, total_parts + 1))
        if valid:
            return existing
        can_replace = True
        for child in existing:
            raw = child.get("raw_json") if isinstance(child.get("raw_json"), dict) else {}
            if (raw.get("receptions") or []) or _as_text(child.get("derniere_reception_ref")).strip():
                can_replace = False
                break
        if not can_replace:
            raise ValueError(f"{parent_esi} est déjà défini avec {len(existing)} partie(s) et possède un historique de réception.")
        for child in existing:
            child_esi = _as_text(child.get("esi_id")).strip()
            if child_esi:
                supabase_rest_request("DELETE", "articles", "esi_id=eq." + urllib.parse.quote(child_esi, safe='-'), prefer="return=minimal")

    parent_raw = parent.get("raw_json") if isinstance(parent.get("raw_json"), dict) else {}
    parent_raw = dict(parent_raw or {})
    inherited_extra = parent_raw.get("article_fields") if isinstance(parent_raw.get("article_fields"), dict) else {}
    inherited_extra = dict(inherited_extra or {})
    for key in ("parent_esi", "partie_index", "partie_total", "partie_label", "article_en_plusieurs_parties", "parties_total"):
        inherited_extra.pop(key, None)

    created = []
    now = datetime.now().isoformat()
    for idx in range(1, total_parts + 1):
        part_extra = dict(inherited_extra)
        part_extra.update({
            "parent_esi": parent_esi,
            "partie_index": str(idx),
            "partie_total": str(total_parts),
            "partie_label": f"{idx}/{total_parts}",
        })
        payload = {
            "ticket_id": parent.get("ticket_id"),
            "source_module": "Partie article",
            "source_index": None,
            "unit_index": idx,
            "type_objet": parent.get("type_objet") or "PRODUIT",
            "reference": parent.get("reference") or "",
            "description": parent.get("description") or "",
            "dossier": parent.get("dossier") or "",
            "client": parent.get("client") or "",
            "projet": parent.get("projet") or "",
            "ref_caisse": parent.get("ref_caisse") or "",
            "transporteur_ref": parent.get("transporteur_ref") or "",
            "longueur_cm": parent.get("longueur_cm") or "",
            "largeur_cm": parent.get("largeur_cm") or "",
            "hauteur_cm": parent.get("hauteur_cm") or "",
            "volume_m3": parent.get("volume_m3") or "",
            "surface_m2": parent.get("surface_m2") or "",
            "poids_kg": parent.get("poids_kg") or "",
            "lieu_stockage": "",
            "statut_logistique": "Partie créée",
            "created_at": now,
            "updated_at": now,
            "raw_json": {
                "source": "partie_article",
                "parent_esi": parent_esi,
                "partie_index": idx,
                "partie_total": total_parts,
                "partie_label": f"{idx}/{total_parts}",
                "article_fields": part_extra,
            },
        }
        payload["search_text"] = _article_search_text(payload)
        created.append(_create_article_record(payload))

    parent_raw["parts_created_at"] = now
    parent_raw["parts_total"] = total_parts
    parent_raw["parts"] = [{"esi_id": _as_text(x.get("esi_id")).strip(), "partie_label": _as_text(x.get("partie_label")).strip()} for x in created]
    parent_extra = parent_raw.get("article_fields") if isinstance(parent_raw.get("article_fields"), dict) else {}
    parent_extra = dict(parent_extra or {})
    parent_extra["article_en_plusieurs_parties"] = "Oui"
    parent_extra["parties_total"] = str(total_parts)
    parent_raw["article_fields"] = parent_extra
    patch = {"raw_json": parent_raw, "updated_at": now, "statut_logistique": f"Article en {total_parts} parties"}
    merged = dict(parent)
    merged.update(patch)
    patch["search_text"] = _article_search_text(merged)
    supabase_rest_request("PATCH", "articles", f"esi_id=eq.{safe_parent}", patch, prefer="return=minimal")
    return _article_parts_for_parent(parent_esi, dossier)


def _normalise_article_parts_specs(raw_specs):
    result = {}
    for entry in raw_specs or []:
        if not isinstance(entry, dict):
            continue
        try:
            idx = int(entry.get("index"))
            unit_offset = int(entry.get("unit_offset") or 0)
            total_parts = int(entry.get("total_parts") or 0)
        except Exception:
            continue
        if idx < 0 or unit_offset < 0:
            continue
        if total_parts < 2 or total_parts > 20:
            raise ValueError("Le nombre de parties doit être compris entre 2 et 20.")
        result[(idx, unit_offset)] = total_parts
    return result


def _expected_reception_unit_keys(selected_items, raw_part_specs):
    specs = _normalise_article_parts_specs(raw_part_specs)
    expected = []
    for item in selected_items:
        idx = int(item.get("index"))
        for unit_offset, _esi_id in enumerate(item.get("esi_ids") or []):
            total = specs.get((idx, unit_offset), 1)
            if total > 1:
                for part_index in range(1, total + 1):
                    expected.append((idx, unit_offset, part_index))
            else:
                expected.append((idx, unit_offset, 0))
    return expected


def _validate_colis_repartition_shape(selected_items, raw_assignments, colis_count, raw_part_specs):
    expected = set(_expected_reception_unit_keys(selected_items, raw_part_specs))
    assignments = {}
    for entry in raw_assignments or []:
        if not isinstance(entry, dict):
            continue
        try:
            key = (int(entry.get("index")), int(entry.get("unit_offset") or 0), int(entry.get("part_index") or 0))
            ci = int(entry.get("colis_index"))
        except Exception:
            continue
        if key not in expected or ci < 0 or ci >= int(colis_count):
            continue
        assignments[key] = ci
    if set(assignments) != expected:
        raise ValueError("Chaque Article ou partie physique doit être associé à un Pre-Packing.")
    if int(colis_count) > len(expected):
        raise ValueError("Le nombre de Pre-Packings ne peut pas dépasser le nombre d'Articles ou parties physiques réceptionnés.")
    if set(assignments.values()) != set(range(int(colis_count))):
        raise ValueError("Chaque Pre-Packing créé doit contenir au moins un Article ou une partie.")
    return assignments


def _expand_selected_items_with_parts(selected_items, raw_part_specs):
    specs = _normalise_article_parts_specs(raw_part_specs)
    expanded_ids = []
    parent_ids = set()
    for item in selected_items:
        idx = int(item.get("index"))
        original_ids = list(item.get("esi_ids") or [])
        new_ids = []
        part_map = {}
        parent_map = {}
        reception_units = []
        composition = []
        for unit_offset, parent_esi in enumerate(original_ids):
            parent_esi = _as_text(parent_esi).strip()
            total = specs.get((idx, unit_offset), 1)
            if total > 1:
                children = _ensure_article_parts(parent_esi, total)
                if len(children) != total:
                    raise ValueError(f"Impossible de préparer les {total} parties de {parent_esi}.")
                parent_ids.add(parent_esi)
                for part_index, child in enumerate(children, start=1):
                    child_esi = _as_text(child.get("esi_id")).strip()
                    label = _as_text(child.get("partie_label")).strip() or f"{part_index}/{total}"
                    new_ids.append(child_esi)
                    part_map[child_esi] = label
                    parent_map[child_esi] = parent_esi
                    reception_units.append({"index": idx, "unit_offset": unit_offset, "part_index": part_index, "esi_id": child_esi, "parent_esi": parent_esi, "partie_label": label})
                    composition.append({"parent_esi": parent_esi, "esi_id": child_esi, "partie_label": label})
            else:
                new_ids.append(parent_esi)
                reception_units.append({"index": idx, "unit_offset": unit_offset, "part_index": 0, "esi_id": parent_esi, "parent_esi": "", "partie_label": ""})
        item["esi_ids_origine"] = original_ids
        item["esi_ids"] = new_ids
        item["partie_par_esi"] = part_map
        item["parent_esi_par_esi"] = parent_map
        item["reception_units"] = reception_units
        if composition:
            item["composition_parties"] = composition
        expanded_ids.extend(new_ids)
    return expanded_ids, parent_ids


def _build_article_labels_from_selected(selected_items, dossier, client, lieu, charge_projet=""):
    labels = []
    for item in selected_items:
        part_map = item.get("partie_par_esi") if isinstance(item.get("partie_par_esi"), dict) else {}
        parent_map = item.get("parent_esi_par_esi") if isinstance(item.get("parent_esi_par_esi"), dict) else {}
        for esi_id in item.get("esi_ids") or []:
            labels.append({
                "titre": "ARTICLE",
                "principal": esi_id,
                "esi_id": esi_id,
                "dossier": dossier,
                "client": client,
                "charge_projet": _as_text(charge_projet).strip(),
                "reference": _as_text(item.get("reference")).strip(),
                "designation": _as_text(item.get("designation") or item.get("description")).strip(),
                "partie": _as_text(part_map.get(esi_id)).strip(),
                "article_principal": _as_text(parent_map.get(esi_id)).strip(),
                "quantite": "1",
                "lieu": lieu,
                "qr_url": _article_qr_url(esi_id),
            })
    return labels

def _normalise_article_reference(value):
    """Normalise une référence uniquement pour le rapprochement bon d'enlèvement / base articles."""
    return re.sub(r"[^A-Za-z0-9]", "", _as_text(value)).upper().strip()


def _find_existing_articles_for_reference(reference, limit=25):
    """Retourne les articles existants dont la référence correspond exactement après normalisation."""
    reference = _as_text(reference).strip()
    if not reference:
        return []

    # Le filtre ilike limite la quantité de données lues, puis la comparaison normalisée
    # évite de rater ABC-01 / ABC 01 ou une différence de casse issue de l'OCR.
    wanted = _normalise_article_reference(reference)
    token = wanted[:4] or reference.replace('*', '').strip()
    pattern = '*' + token + '*'
    rows = supabase_rest_request(
        'GET', 'articles',
        'select=*&reference=ilike.' + urllib.parse.quote(pattern, safe='*') +
        '&order=article_no.desc&limit=' + str(int(limit))
    ) or []

    matches = []
    for row in rows:
        if _normalise_article_reference(row.get('reference')) != wanted:
            continue
        article = _article_row_to_public(row)
        matches.append({
            'esi_id': _as_text(article.get('esi_id')).strip(),
            'reference': _as_text(article.get('reference')).strip(),
            'description': _as_text(article.get('description')).strip(),
            'dossier': _as_text(article.get('dossier')).strip(),
            'client': _as_text(article.get('client')).strip(),
            'projet': _as_text(article.get('projet')).strip(),
            'lieu_stockage': _as_text(article.get('lieu_stockage')).strip(),
            'statut_logistique': _as_text(article.get('statut_logistique')).strip(),
        })
    return matches


def _enlevement_with_article_candidates(parsed):
    """Ajoute les propositions de la base articles aux lignes reconnues par l'OCR."""
    result = dict(parsed or {})
    enriched = []
    for index, source in enumerate(result.get('items') or []):
        item = dict(source or {})
        item['source_index'] = index
        item['article_candidates'] = _find_existing_articles_for_reference(item.get('reference'))
        enriched.append(item)
    result['items'] = enriched
    return result


def _apply_enlevement_article_selections(ticket, selections):
    """
    Applique les ESI existants choisis par le demandeur.
    Les unités marquées 'create' restent sans ESI et seront créées ensuite par
    _ensure_articles_for_ticket().
    """
    enl = dict(ticket.get('enlevement') or {})
    items = list(enl.get('items') or [])
    by_index = {}
    for entry in selections or []:
        if not isinstance(entry, dict):
            continue
        try:
            idx = int(entry.get('index'))
        except Exception:
            continue
        by_index[idx] = entry

    used_esi = set()
    for idx, original in enumerate(items):
        item = dict(original or {})
        qty = _article_quantity(item.get('quantite'), 1)
        selection = by_index.get(idx) or {}
        units = selection.get('units') or []
        chosen = []

        for unit in units[:qty]:
            if not isinstance(unit, dict) or unit.get('mode') != 'existing':
                continue
            esi_id = _as_text(unit.get('esi_id')).strip()
            if not esi_id or esi_id in used_esi:
                continue

            safe_esi = urllib.parse.quote(esi_id, safe='-')
            rows = supabase_rest_request(
                'GET', 'articles', f'select=esi_id,reference&esi_id=eq.{safe_esi}&limit=1'
            ) or []
            if not rows:
                continue
            if _normalise_article_reference(rows[0].get('reference')) != _normalise_article_reference(item.get('reference')):
                continue

            chosen.append(esi_id)
            used_esi.add(esi_id)

        item['esi_ids'] = chosen
        item['esi_id'] = chosen[0] if chosen else ''
        # Les candidats n'ont pas vocation à être stockés dans le ticket final.
        item.pop('article_candidates', None)
        item.pop('source_index', None)
        items[idx] = item

    enl['items'] = items
    enl['references'] = [
        _as_text(x.get('reference')).strip() for x in items
        if _as_text(x.get('reference')).strip()
    ]
    ticket['enlevement'] = enl


def _extract_enlevement_pdf_preview_low_memory(pdf_bytes):
    """
    Analyse légère utilisée uniquement avant la création du ticket.

    Le flux historique faisait deux OCR complets (300 dpi puis OCR spatial 200 dpi),
    ce qui peut dépasser la mémoire/timeout d'un worker Render. Ici on :
      - tente d'abord le texte natif ;
      - OCRise page par page à 180 dpi si nécessaire ;
      - ne lance PAS le second OCR spatial ;
      - extrait tout de même les références et les principaux champs texte.
    """
    try:
        from pypdf import PdfReader
    except Exception as e:
        raise RuntimeError("Le module pypdf n'est pas installé.") from e

    try:
        reader = PdfReader(BytesIO(pdf_bytes))
    except Exception as e:
        raise ValueError(f"PDF illisible : {e}")

    pages_native = []
    for page in reader.pages:
        try:
            pages_native.append(page.extract_text() or "")
        except Exception:
            pages_native.append("")

    native_text = "\n".join(pages_native).strip()
    text = native_text
    ocr_used = False

    if len(native_text) < 120:
        print("[ENLEVEMENT PREVIEW] Texte natif insuffisant, OCR basse mémoire")
        try:
            from pdf2image import convert_from_bytes
            import pytesseract
        except Exception as e:
            raise RuntimeError(
                "OCR indisponible. Vérifie pdf2image, pytesseract, Pillow, tesseract-ocr et poppler-utils."
            ) from e

        ocr_pages = []
        page_count = len(reader.pages)
        with _RECEPTION_OCR_LOCK:
            for page_no in range(1, page_count + 1):
                images = []
                try:
                    images = convert_from_bytes(
                        pdf_bytes,
                        dpi=180,
                        grayscale=True,
                        first_page=page_no,
                        last_page=page_no,
                        thread_count=1,
                    )
                    if not images:
                        ocr_pages.append("")
                        continue
                    image = images[0]
                    page_text = pytesseract.image_to_string(
                        image, lang="fra", config="--psm 4"
                    )
                    if len((page_text or "").strip()) < 80:
                        page_text = pytesseract.image_to_string(
                            image, lang="fra", config="--psm 11"
                        )
                    ocr_pages.append(page_text or "")
                    print(f"[ENLEVEMENT PREVIEW] Page {page_no}/{page_count} OCRisée")
                finally:
                    for image in images:
                        try:
                            image.close()
                        except Exception:
                            pass
                    images.clear()
        text = "\n".join(ocr_pages).strip()
        ocr_used = True

    if not text:
        raise ValueError("Aucun texte exploitable trouvé dans le bon d'enlèvement.")

    clean_text = text.replace("\r", "")
    cutoff = re.search(r"\bAssur[eé]\s+par\b", clean_text, re.I)
    if cutoff:
        clean_text = clean_text[:cutoff.start()].rstrip()

    lines = _enlevement_lines(clean_text)
    label_numero = [
        r"Num[eé]ro\s+de\s+r[eé]f[eé]r(?:ence)?",
        r"N[°ºo]\s*de\s*r[eé]f[eé]rence",
        r"R[eé]f[eé]rence\s+du\s+bon",
    ]
    common_stops = [
        r"Client", r"Coordinateur", r"Exhibition", r"Programme\s+du\s+chantier",
        r"Instructions?", r"Adresse", r"Service"
    ]

    numero_bon = _value_after_label(lines, label_numero, common_stops)
    if numero_bon:
        m = re.search(r"\b([A-Za-z0-9][A-Za-z0-9_-]{3,})\b", numero_bon)
        numero_bon = m.group(1) if m else ""

    client = _value_after_label(
        lines, [r"\bClient\b"],
        [r"Coordinateur", r"Exhibition", r"Programme\s+du\s+chantier", r"Adresse", r"Service"]
    )
    coordinateur = _value_after_label(
        lines, [r"Coordinateur"],
        [r"Client", r"Exhibition", r"Programme\s+du\s+chantier", r"Adresse", r"Service"]
    )
    exhibition = _value_after_label(
        lines, [r"Exhibition"],
        [r"Client", r"Coordinateur", r"Programme\s+du\s+chantier", r"Adresse", r"Service"]
    )

    if client:
        client = re.split(r"\bExhibition\b", client, maxsplit=1, flags=re.I)[0].strip(" :-|")
    if coordinateur:
        coordinateur = re.split(r"\b(?:Client|Exhibition)\b", coordinateur, maxsplit=1, flags=re.I)[0].strip(" :-|")
    if exhibition:
        exhibition = re.split(r"\b(?:Client|Coordinateur)\b", exhibition, maxsplit=1, flags=re.I)[0].strip(" :-|")

    programme = _extract_programme_chantier(clean_text)
    instructions = _extract_instructions_block(clean_text)
    items = _extract_enlevement_items(instructions) if instructions else []
    contact_data = _extract_contact_blocks(clean_text)

    display_name = " - ".join(
        x for x in [_clean_ocr_line(client), _clean_ocr_line(numero_bon)] if x
    )

    return {
        "numero_bon": numero_bon,
        "client": client,
        "display_name": display_name,
        "coordinateur": coordinateur,
        "exhibition": exhibition,
        "date_enlevement": programme.get("date_enlevement", ""),
        "service": programme.get("service", ""),
        "assigne_a": programme.get("assigne_a", ""),
        "vehicules": programme.get("vehicules", ""),
        "notes": programme.get("notes", ""),
        "instructions": instructions,
        "items": items,
        "references": [x.get("reference") for x in items if x.get("reference")],
        "page_count": len(reader.pages),
        "ocr_used": ocr_used,
        "raw_text": clean_text,
        **contact_data,
    }


@app.route('/api/enlevement/analyser-articles', methods=['POST'])
def api_enlevement_analyser_articles():
    """Analyse légère d'un bon d'enlèvement et propose les articles existants."""
    fs = request.files.get('file')
    if not fs or not fs.filename:
        return jsonify({'ok': False, 'error': "Bon d'enlèvement PDF manquant"}), 400
    if not fs.filename.lower().endswith('.pdf'):
        return jsonify({'ok': False, 'error': "Le bon d'enlèvement doit être un PDF"}), 400
    content = fs.read()
    if not content:
        return jsonify({'ok': False, 'error': 'Le fichier PDF est vide'}), 400

    try:
        parsed = _extract_enlevement_pdf_preview_low_memory(content)
        enriched = _enlevement_with_article_candidates(parsed)
        return jsonify({
            'ok': True,
            'analysis': enriched,
            'items': enriched.get('items') or [],
            'existing_count': sum(len(x.get('article_candidates') or []) for x in enriched.get('items') or []),
        })
    except ValueError as e:
        return jsonify({'ok': False, 'error': str(e)}), 400
    except Exception as e:
        print(f'[ENLEVEMENT ARTICLES] Erreur analyse : {e}')
        return jsonify({'ok': False, 'error': str(e)}), 500


def _ensure_articles_for_ticket(ticket, save=True):
    """
    Attribue des ESI-x à toutes les unités des lignes de marchandise d'un ticket.
    Idempotent : une ligne possédant déjà ses esi_ids n'est pas recréée.
    """
    module = _as_text(ticket.get("module")).replace("’", "'").strip()
    if module == "Avis d'arrivée":
        container = dict(ticket.get("avisArrivee") or ticket.get("avis_arrivee") or {})
        items = list(container.get("items") or [])
        container_key = "avisArrivee"
    elif module in ("Demande d'enlèvement", "Demande d'enlevement"):
        container = dict(ticket.get("enlevement") or {})
        items = list(container.get("items") or [])
        container_key = "enlevement"
    else:
        return []

    created = []
    changed = False

    with _ARTICLE_LOCK:
        for index, original in enumerate(items):
            item = dict(original or {})
            qty = _article_quantity(item.get("quantite"), 1)
            existing = [str(x).strip() for x in (item.get("esi_ids") or []) if str(x).strip()]

            # Si l'ancienne structure stocke un seul identifiant.
            if not existing and item.get("esi_id"):
                existing = [_as_text(item.get("esi_id")).strip()]

            while len(existing) < qty:
                unit_index = len(existing) + 1
                payload = _article_payload_from_item(
                    ticket, item, source_module=module, source_index=index, unit_index=unit_index
                )
                article = _create_article_record(payload)
                existing.append(article["esi_id"])
                created.append(article)

            if item.get("esi_ids") != existing:
                item["esi_ids"] = existing
                item["esi_id"] = existing[0] if existing else ""
                items[index] = item
                changed = True

        if changed:
            container["items"] = items
            ticket[container_key] = container
            ticket["updatedAt"] = datetime.now().isoformat()
            if save:
                save_ticket(ticket)

    return created


def _update_article_logistics(esi_ids, lieu_stockage="", statut_logistique="Réceptionné",
                              colis=None, colis_by_esi=None, colis_type_by_esi=None,
                              colis_esi_by_ref=None, reception_ref="", receptionne_par=""):
    """Met à jour la fiche globale des articles après une réception, avec colis et type précis par ESI."""
    colis_by_esi = dict(colis_by_esi or {})
    colis_type_by_esi = dict(colis_type_by_esi or {})
    colis_esi_by_ref = dict(colis_esi_by_ref or {})
    fallback = list(colis or [])
    parent_ids_to_sync = set()
    for esi_id in esi_ids or []:
        esi_id = _as_text(esi_id).strip()
        if not esi_id:
            continue
        article_colis = _as_text(colis_by_esi.get(esi_id)).strip()
        article_type_colis = _normalise_colis_type(colis_type_by_esi.get(esi_id))
        article_colis_list = [article_colis] if article_colis else fallback
        safe_esi = urllib.parse.quote(esi_id, safe='-')
        rows = supabase_rest_request("GET", "articles", f"select=*&esi_id=eq.{safe_esi}&limit=1") or []
        if not rows:
            continue
        current = dict(rows[0])
        raw = current.get("raw_json") if isinstance(current.get("raw_json"), dict) else {}
        raw = dict(raw or {})
        part_meta = _article_part_meta(current)
        if part_meta.get("parent_esi"):
            parent_ids_to_sync.add(part_meta["parent_esi"])
        history = list(raw.get("receptions") or [])
        history.append({"date": datetime.now().isoformat(), "lieu_stockage": lieu_stockage,
                        "colis": article_colis_list, "type_colis": article_type_colis,
                        "reception_ref": reception_ref, "receptionne_par": receptionne_par})
        raw["receptions"] = history
        raw["colis_actuel"] = article_colis or (article_colis_list[0] if len(article_colis_list)==1 else "")
        raw["type_colis_actuel"] = article_type_colis
        extra = raw.get("article_fields") if isinstance(raw.get("article_fields"), dict) else {}
        extra = dict(extra or {})
        extra["type_colis"] = article_type_colis
        extra["numero_colis"] = article_colis or (article_colis_list[0] if len(article_colis_list) == 1 else "")
        extra["colis_esi"] = _as_text(colis_esi_by_ref.get(extra["numero_colis"])).strip()
        raw["article_fields"] = extra
        patch = {"lieu_stockage": _as_text(lieu_stockage).strip(),
                 "statut_logistique": _as_text(statut_logistique).strip(),
                 "dernier_colis": article_colis or ", ".join(article_colis_list),
                 "derniere_reception_ref": _as_text(reception_ref).strip(),
                 "updated_at": datetime.now().isoformat(), "raw_json": raw}
        merged = dict(current); merged.update(patch); patch["search_text"] = _article_search_text(merged)
        supabase_rest_request("PATCH", "articles", f"esi_id=eq.{safe_esi}", patch, prefer="return=minimal")

    for parent_esi in sorted(parent_ids_to_sync):
        try:
            _sync_parent_parts_logistics(parent_esi)
        except Exception as e:
            print(f"[ARTICLES] Synchronisation article principal {parent_esi} impossible: {e}")


def _article_ids_for_received_units(item, previous_qty, qty_received):
    """Retourne les ESI-x correspondant précisément aux unités reçues dans cette opération."""
    ids = [str(x).strip() for x in (item.get("esi_ids") or []) if str(x).strip()]
    start = max(0, int(previous_qty))
    end = start + max(0, int(qty_received))
    return ids[start:end]


ARTICLES_MANUAL_CREATE_JS = r"""(function(){
'use strict';

// Affichage uniquement : les valeurs techniques historiques PRODUIT / CONTENANT
// restent inchangées en base, mais l'interface affiche ARTICLE / PRE-PACKING.
function replaceContenantWording(root){
  const scope=root&&root.nodeType===1?root:document.body;
  if(!scope)return;
  const walker=document.createTreeWalker(scope,NodeFilter.SHOW_TEXT);
  const nodes=[];let n;
  while((n=walker.nextNode())){
    const tag=n.parentElement&&n.parentElement.tagName;
    if(tag&&['SCRIPT','STYLE','TEXTAREA'].includes(tag))continue;
    if(/contenant|produit/i.test(n.nodeValue||''))nodes.push(n);
  }
  nodes.forEach(node=>{
    node.nodeValue=(node.nodeValue||'')
      .replace(/CONTENANTS?/g,'PRE-PACKING')
      .replace(/Contenants?/g,'Pre-Packing')
      .replace(/contenants?/g,'Pre-Packing')
      .replace(/PRODUITS?/g,'ARTICLES')
      .replace(/Produits?/g,'Articles')
      .replace(/produits?/g,'articles');
  });
}
function installColisWording(){
  replaceContenantWording(document.body);
  const obs=new MutationObserver(muts=>{
    muts.forEach(m=>m.addedNodes.forEach(node=>{
      if(node.nodeType===1)replaceContenantWording(node);
      else if(node.nodeType===3&&/contenant|produit/i.test(node.nodeValue||'')){
        const t=node.nodeValue||'';
        node.nodeValue=t.replace(/CONTENANTS?/g,'PRE-PACKING').replace(/Contenants?/g,'Pre-Packing').replace(/contenants?/g,'Pre-Packing').replace(/PRODUITS?/g,'ARTICLES').replace(/Produits?/g,'Articles').replace(/produits?/g,'articles');
      }
    }));
  });
  obs.observe(document.body,{childList:true,subtree:true});
}
if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',installColisWording);else installColisWording();

function ensureManualCreateUI(){
  const actions=document.querySelector('.actions');
  if(!actions || document.getElementById('manualArticleCreateBtn')) return;

  const btn=document.createElement('button');
  btn.className='btn primary';
  btn.id='manualArticleCreateBtn';
  btn.type='button';
  btn.textContent='+ Créer un article';
  const excelBtn=document.getElementById('excelImportBtn');
  if(excelBtn) actions.insertBefore(btn, excelBtn); else actions.appendChild(btn);

  const bg=document.createElement('div');
  bg.className='modal-backdrop';
  bg.id='manualArticleModalBackdrop';
  bg.setAttribute('aria-hidden','true');
  bg.innerHTML=`<div class="modal" role="dialog" aria-modal="true" style="width:min(1040px,96vw)">
    <div class="modal-head">
      <div><div class="modal-title">Créer un article manuellement</div><div class="modal-sub">Le N° ESI sera généré automatiquement. Les volumes et surfaces sont calculés à partir des dimensions en cm.</div></div>
      <button class="modal-close" id="manualArticleModalClose" type="button">×</button>
    </div>
    <div class="modal-body">
      <div class="section-title">Informations existantes</div>
      <div class="edit-grid" id="manualArticleGrid">
        <div class="edit-field"><label>Type</label><select id="manualType"><option value="ARTICLE">ARTICLE</option><option value="PRE-PACKING">PRE-PACKING</option><option value="PACKING">PACKING</option></select></div>
        <div class="edit-field"><label>N° dossier *</label><input id="manualDossier" autocomplete="off" placeholder="Ex. 101129"></div>
        <div class="edit-field"><label>Client</label><input id="manualClient" autocomplete="off"></div>
        <div class="edit-field"><label>Chargé de projet</label><input id="manualChargeProjet" autocomplete="off"></div>
        <div class="edit-field"><label>Projet / exposition</label><input id="manualProjet" autocomplete="off"></div>
        <div class="edit-field"><label>Référence / N° inventaire</label><input id="manualReference" autocomplete="off"></div>
        <div class="edit-field"><label>Réf. Packing</label><input id="manualRefCaisse" autocomplete="off" placeholder="Ex. C-101129-01"></div>
        <div class="edit-field"><label>Réf. transporteur</label><input id="manualTransporteurRef" autocomplete="off"></div>
        <div class="edit-field"><label>Lieu de stockage</label><input id="manualLieuStockage" autocomplete="off"></div>
        <div class="edit-field"><label>Statut logistique</label><input id="manualStatut" value="Créé"></div>
        <div class="edit-field" style="grid-column:1/-1"><label>Description / désignation</label><textarea id="manualDescription"></textarea></div>
        <div class="edit-field"><label>Longueur (cm)</label><input id="manualLongueur" inputmode="decimal"></div>
        <div class="edit-field"><label>Largeur (cm)</label><input id="manualLargeur" inputmode="decimal"></div>
        <div class="edit-field"><label>Hauteur (cm)</label><input id="manualHauteur" inputmode="decimal"></div>
        <div class="edit-field"><label>Poids (kg)</label><input id="manualPoids" inputmode="decimal"></div>
        <div class="edit-field"><label>Volume (m³) — automatique</label><input id="manualVolume" readonly></div>
        <div class="edit-field"><label>Surface (m²) — automatique</label><input id="manualSurface" readonly></div>
      </div>

      <div class="section-title" style="margin-top:20px">Informations œuvre</div>
      <div class="edit-grid">
        <div class="edit-field"><label>N° réf œuvre</label><input id="manualOeuvreReference" autocomplete="off"></div>
        <div class="edit-field"><label>Nom de l’artiste</label><input id="manualArtiste" autocomplete="off"></div>
        <div class="edit-field"><label>Titre de l’œuvre</label><input id="manualOeuvreTitre" autocomplete="off"></div>
        <div class="edit-field"><label>Technique de l’œuvre</label><input id="manualOeuvreTechnique" autocomplete="off"></div>
        <div class="edit-field"><label>Longueur de l’œuvre (cm)</label><input id="manualOeuvreLongueur" inputmode="decimal"></div>
        <div class="edit-field"><label>Largeur de l’œuvre (cm)</label><input id="manualOeuvreLargeur" inputmode="decimal"></div>
        <div class="edit-field"><label>Hauteur de l’œuvre (cm)</label><input id="manualOeuvreHauteur" inputmode="decimal"></div>
        <div class="edit-field"><label>Poids de l’œuvre (kg)</label><input id="manualOeuvrePoids" inputmode="decimal"></div>
        <div class="edit-field"><label>Volume œuvre (m³) — automatique</label><input id="manualOeuvreVolume" readonly></div>
        <div class="edit-field"><label>Surface au sol œuvre (m²) — automatique</label><input id="manualOeuvreSurface" readonly></div>
        <div class="edit-field"><label>Statut douanier</label><select id="manualStatutDouanier"><option value="">Non renseigné</option><option value="Libre">Libre</option><option value="Sous douane">Sous douane</option></select></div>
        <div class="edit-field"><label>N° IMA</label><input id="manualImaNumero" autocomplete="off"></div>
        <div class="edit-field"><label>Date de l’IMA</label><input id="manualImaDate" type="date"></div>
      </div>
      <div id="manualArticleHint" class="muted" style="margin-top:12px;font-size:12px">Si ce N° de dossier existe déjà, Client, Projet et Chargé de projet seront repris automatiquement.</div>
      <div class="modal-actions"><button class="btn" id="manualArticleCancel" type="button">Annuler</button><button class="btn primary" id="manualArticleSave" type="button">Créer l'article</button></div>
    </div>
  </div>`;
  document.body.appendChild(bg);

  const ids=['manualType','manualDossier','manualClient','manualChargeProjet','manualProjet','manualReference','manualRefCaisse','manualTransporteurRef','manualLieuStockage','manualStatut','manualDescription','manualLongueur','manualLargeur','manualHauteur','manualPoids','manualVolume','manualSurface','manualOeuvreReference','manualArtiste','manualOeuvreTitre','manualOeuvreTechnique','manualOeuvreLongueur','manualOeuvreLargeur','manualOeuvreHauteur','manualOeuvrePoids','manualOeuvreVolume','manualOeuvreSurface','manualStatutDouanier','manualImaNumero','manualImaDate'];
  function el(id){return document.getElementById(id)}
  function num(id){const n=Number(String(el(id).value||'').replace(',','.'));return Number.isFinite(n)?n:null}
  function metric(v){if(v===null||!Number.isFinite(v))return '';return v.toFixed(6).replace(/0+$/,'').replace(/\.$/,'')}
  function recalcStandard(){const l=num('manualLongueur'),w=num('manualLargeur'),h=num('manualHauteur');el('manualSurface').value=(l!==null&&w!==null)?metric(l*w/10000):'';el('manualVolume').value=(l!==null&&w!==null&&h!==null)?metric(l*w*h/1000000):''}
  function recalcOeuvre(){const l=num('manualOeuvreLongueur'),w=num('manualOeuvreLargeur'),h=num('manualOeuvreHauteur');el('manualOeuvreSurface').value=(l!==null&&w!==null)?metric(l*w/10000):'';el('manualOeuvreVolume').value=(l!==null&&w!==null&&h!==null)?metric(l*w*h/1000000):''}
  function clearForm(){ids.forEach(id=>{const node=el(id);if(!node)return;if(id==='manualType')node.value='ARTICLE';else if(id==='manualStatut')node.value='Créé';else node.value='';});el('manualArticleHint').textContent='Si ce N° de dossier existe déjà, Client, Projet et Chargé de projet seront repris automatiquement.'}
  function openModal(){clearForm();bg.classList.add('open');bg.setAttribute('aria-hidden','false');setTimeout(()=>el('manualDossier').focus(),50)}
  function closeModal(){bg.classList.remove('open');bg.setAttribute('aria-hidden','true')}

  ['manualLongueur','manualLargeur','manualHauteur'].forEach(id=>el(id).addEventListener('input',recalcStandard));
  ['manualOeuvreLongueur','manualOeuvreLargeur','manualOeuvreHauteur'].forEach(id=>el(id).addEventListener('input',recalcOeuvre));
  btn.onclick=openModal;el('manualArticleModalClose').onclick=closeModal;el('manualArticleCancel').onclick=closeModal;bg.addEventListener('click',e=>{if(e.target===bg)closeModal()});

  let identitySeq=0;
  async function autofillDossier(){
    const dossier=String(el('manualDossier').value||'').trim();const seq=++identitySeq;if(!dossier)return;
    try{
      const r=await fetch('/api/dossiers/lookup?dossier='+encodeURIComponent(dossier),{cache:'no-store'});const d=await r.json();if(seq!==identitySeq||!r.ok)return;
      if(String(d.client||'').trim())el('manualClient').value=d.client;
      if(String(d.projet||'').trim())el('manualProjet').value=d.projet;
      if(String(d.charge_projet||'').trim())el('manualChargeProjet').value=d.charge_projet;
      el('manualArticleHint').textContent=d.found?'Dossier existant : Client, Projet et Chargé de projet ont été repris automatiquement.':'Nouveau dossier : renseigne les informations de dossier nécessaires.';
    }catch(e){}
  }
  el('manualDossier').addEventListener('change',autofillDossier);el('manualDossier').addEventListener('blur',autofillDossier);

  el('manualArticleSave').onclick=async()=>{
    const dossier=String(el('manualDossier').value||'').trim(),reference=String(el('manualReference').value||'').trim(),description=String(el('manualDescription').value||'').trim();
    if(!dossier){alert('Le N° de dossier est obligatoire.');el('manualDossier').focus();return}
    if(!reference&&!description){alert('Renseigne au minimum une référence ou une description.');el('manualReference').focus();return}
    recalcStandard();recalcOeuvre();
    const payload={
      type_objet:el('manualType').value,dossier,reference,description,
      client:String(el('manualClient').value||'').trim(),charge_projet:String(el('manualChargeProjet').value||'').trim(),projet:String(el('manualProjet').value||'').trim(),
      ref_caisse:String(el('manualRefCaisse').value||'').trim(),transporteur_ref:String(el('manualTransporteurRef').value||'').trim(),lieu_stockage:String(el('manualLieuStockage').value||'').trim(),
      longueur_cm:String(el('manualLongueur').value||'').trim(),largeur_cm:String(el('manualLargeur').value||'').trim(),hauteur_cm:String(el('manualHauteur').value||'').trim(),poids_kg:String(el('manualPoids').value||'').trim(),
      statut_logistique:String(el('manualStatut').value||'').trim()||'Créé',
      oeuvre_reference:String(el('manualOeuvreReference').value||'').trim(),artiste:String(el('manualArtiste').value||'').trim(),oeuvre_titre:String(el('manualOeuvreTitre').value||'').trim(),oeuvre_technique:String(el('manualOeuvreTechnique').value||'').trim(),
      oeuvre_longueur_cm:String(el('manualOeuvreLongueur').value||'').trim(),oeuvre_largeur_cm:String(el('manualOeuvreLargeur').value||'').trim(),oeuvre_hauteur_cm:String(el('manualOeuvreHauteur').value||'').trim(),oeuvre_poids_kg:String(el('manualOeuvrePoids').value||'').trim(),
      statut_douanier:String(el('manualStatutDouanier').value||'').trim(),ima_numero:String(el('manualImaNumero').value||'').trim(),ima_date:String(el('manualImaDate').value||'').trim()
    };
    const save=el('manualArticleSave');save.disabled=true;save.textContent='Création…';
    try{const r=await fetch('/api/articles/manual',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});const text=await r.text();let d={};try{d=text?JSON.parse(text):{}}catch(e){}if(!r.ok)throw new Error(d.error||'Impossible de créer l’article');const esi=(d.article&&d.article.esi_id)||d.esi_id||'';closeModal();if(typeof load==='function')await load();if(esi&&typeof openArticleDetail==='function')await openArticleDetail(esi);else alert('Article créé'+(esi?' : '+esi:''));}
    catch(e){alert(e.message||'Impossible de créer l’article')}finally{save.disabled=false;save.textContent="Créer l'article"}
  };
}
if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',ensureManualCreateUI);else ensureManualCreateUI();

// Ajoute l'impression d'une etiquette directement depuis la carte d'identite.
// Version volontairement simple et bornee : aucun MutationObserver et aucune boucle de surveillance.
function _esiIdentityLabelWording(category){
  const c=String(category||'').trim().toUpperCase();
  return c==='PACKING'?'Packing':(c==='PRE-PACKING'?'Pré-Packing':'Article');
}

function _esiMountIdentityLabelButton(d,editing=false){
  try{
    const article=(d&&d.article)||{};
    const fallbackEsi=(typeof articleDetailState!=='undefined'&&articleDetailState)?articleDetailState.esi:'';
    const esi=String(article.esi_id||fallbackEsi||'').trim();
    if(editing||!esi) return;

    const body=document.getElementById('articleModalBody');
    if(!body) return;

    const old=document.getElementById('articlePrintLabelBtn');
    if(old) old.remove();

    const technicalType=String(article.type_objet||'').trim().toUpperCase();
    const category=String(article.categorie_metier||'').trim().toUpperCase() || (technicalType==='CONTENANT'?'PRE-PACKING':'ARTICLE');
    const wording=_esiIdentityLabelWording(category);

    const buttons=[...body.querySelectorAll('button')];
    const modify=buttons.find(b=>String(b.textContent||'').trim().toLowerCase()==='modifier');

    let container=null;
    let before=null;
    if(modify&&modify.parentElement){
      container=modify.parentElement;
      before=modify;
    }else{
      const selectors=['.article-hero-actions','.hero-actions','.article-actions','.identity-actions','.article-detail-actions'];
      for(const selector of selectors){
        const node=body.querySelector(selector);
        if(node){container=node;break;}
      }
    }
    if(!container) return;

    const btn=document.createElement('button');
    btn.id='articlePrintLabelBtn';
    btn.type='button';
    btn.className='btn hero-action secondary';
    btn.textContent='Imprimer l’étiquette '+wording;
    btn.title='Ouvrir l’étiquette '+wording+' prête à imprimer';
    btn.onclick=()=>window.open('/api/articles/'+encodeURIComponent(esi)+'/etiquette','_blank','noopener');

    if(before) container.insertBefore(btn,before);
    else container.appendChild(btn);
  }catch(e){
    console.error('Ajout bouton etiquette impossible',e);
  }
}

function installIdentityLabelPrint(){
  if(typeof renderArticleDetail!=='function') return;
  if(renderArticleDetail.__esiLabelPrintInstalled) return;

  const originalRenderArticleDetail=renderArticleDetail;
  const wrapped=function(d,editing=false){
    const result=originalRenderArticleDetail.apply(this,arguments);
    // Le rendu de la carte est synchrone dans articles.html ; un second essai unique
    // au cycle d'affichage suivant couvre les navigateurs qui finalisent le DOM juste apres.
    _esiMountIdentityLabelButton(d,editing);
    requestAnimationFrame(()=>_esiMountIdentityLabelButton(d,editing));
    return result;
  };
  wrapped.__esiLabelPrintInstalled=true;
  renderArticleDetail=wrapped;
}

if(document.readyState==='loading'){
  document.addEventListener('DOMContentLoaded',installIdentityLabelPrint,{once:true});
}else{
  installIdentityLabelPrint();
}
})();"""

@app.route('/articles')
def articles_page():
    page = render_template('articles.html')
    inline = '<script>' + ARTICLES_MANUAL_CREATE_JS + '</script>'
    if '</body>' in page:
        page = page.replace('</body>', inline + '\n</body>', 1)
    else:
        page += inline
    response = app.make_response(page)
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    return response


@app.route('/api/articles')
def api_articles():
    q = _as_text(request.args.get("q")).strip()
    requested_type = _as_text(request.args.get("type_objet")).strip().upper()
    limit = min(max(int(request.args.get("limit") or 100), 1), 500)

    business_filter = ""
    technical_type = ""
    if requested_type in ("PRE-PACKING", "PREPACKING", "COLIS"):
        technical_type = "CONTENANT"
        business_filter = "PRE-PACKING"
    elif requested_type == "PACKING":
        technical_type = "CONTENANT"
        business_filter = "PACKING"
    elif requested_type in ("ARTICLE", "PRODUIT"):
        technical_type = "PRODUIT"
        business_filter = "ARTICLE"
    elif requested_type == "CONTENANT":
        technical_type = "CONTENANT"

    # Quand un filtre métier doit être appliqué après lecture de raw_json, on lit jusqu'à
    # 500 lignes techniques pour ne pas tronquer artificiellement les résultats.
    query_limit = 500 if business_filter in ("PRE-PACKING", "PACKING") else limit
    query = "select=" + _ARTICLE_LIST_SELECT + "&order=article_no.desc&limit=" + str(query_limit)

    if technical_type in ("PRODUIT", "CONTENANT"):
        query += "&type_objet=eq." + urllib.parse.quote(technical_type, safe='')

    if q:
        pattern = "*" + q.replace("*", "") + "*"
        query += "&search_text=ilike." + urllib.parse.quote(pattern, safe='*')

    rows = [_article_row_to_list_public(row) for row in (supabase_rest_request("GET", "articles", query) or [])]
    if business_filter:
        rows = [row for row in rows if row.get("categorie_metier") == business_filter]
    return jsonify(rows[:limit])


@app.route('/api/articles/manual', methods=['POST'])
def api_article_create_manual():
    """Crée un article ou un colis directement depuis la base Articles. Le stockage DB historique reste CONTENANT."""
    data = request.get_json(silent=True) or {}

    requested_type = _as_text(data.get('type_objet') or 'ARTICLE').strip().upper()
    categorie_metier = 'ARTICLE'
    if requested_type in ('PRE-PACKING', 'PREPACKING', 'COLIS', 'CONTENANT'):
        type_objet = 'CONTENANT'
        categorie_metier = 'PRE-PACKING'
    elif requested_type == 'PACKING':
        type_objet = 'CONTENANT'
        categorie_metier = 'PACKING'
    elif requested_type in ('ARTICLE', 'PRODUIT'):
        type_objet = 'PRODUIT'
        categorie_metier = 'ARTICLE'
    else:
        return jsonify({'ok': False, 'error': 'Type invalide : ARTICLE, PRE-PACKING ou PACKING attendu'}), 400

    dossier = _as_text(data.get('dossier')).strip()
    reference = _as_text(data.get('reference')).strip()
    description = _as_text(data.get('description')).strip()

    if not dossier:
        return jsonify({'ok': False, 'error': 'Le N° de dossier est obligatoire'}), 400
    if not reference and not description:
        return jsonify({'ok': False, 'error': 'Renseigne au minimum une référence ou une description'}), 400

    try:
        identity = _article_dossier_identity(dossier)
    except Exception as e:
        return jsonify({'ok': False, 'error': f'Impossible de lire les informations du dossier : {e}'}), 500

    # Pour un dossier déjà connu, l'identité existante reste prioritaire afin
    # d'éviter des Client / Projet différents pour un même N° de dossier.
    client = _as_text(identity.get('client')).strip() or _as_text(data.get('client')).strip()
    projet = _as_text(identity.get('projet')).strip() or _as_text(data.get('projet')).strip()
    charge_projet = _as_text(identity.get('charge_projet')).strip() or _as_text(data.get('charge_projet')).strip()
    now = datetime.now().isoformat()

    volume_m3, surface_m2 = _article_calculated_metrics(
        data.get('longueur_cm'), data.get('largeur_cm'), data.get('hauteur_cm')
    )
    oeuvre_volume_m3, oeuvre_surface_m2 = _article_calculated_metrics(
        data.get('oeuvre_longueur_cm'), data.get('oeuvre_largeur_cm'), data.get('oeuvre_hauteur_cm')
    )
    article_fields = {
        'charge_projet': charge_projet,
        'categorie_metier': categorie_metier,
        'oeuvre_reference': _as_text(data.get('oeuvre_reference')).strip(),
        'artiste': _as_text(data.get('artiste')).strip(),
        'oeuvre_titre': _as_text(data.get('oeuvre_titre')).strip(),
        'oeuvre_technique': _as_text(data.get('oeuvre_technique')).strip(),
        'oeuvre_longueur_cm': _as_text(data.get('oeuvre_longueur_cm')).strip(),
        'oeuvre_largeur_cm': _as_text(data.get('oeuvre_largeur_cm')).strip(),
        'oeuvre_hauteur_cm': _as_text(data.get('oeuvre_hauteur_cm')).strip(),
        'oeuvre_volume_m3': oeuvre_volume_m3,
        'oeuvre_surface_m2': oeuvre_surface_m2,
        'oeuvre_poids_kg': _as_text(data.get('oeuvre_poids_kg')).strip(),
        'statut_douanier': _as_text(data.get('statut_douanier')).strip(),
        'ima_numero': _as_text(data.get('ima_numero')).strip(),
        'ima_date': _as_text(data.get('ima_date')).strip(),
    }

    payload = {
        'ticket_id': None,
        'source_module': 'Création manuelle',
        'source_index': None,
        'unit_index': 1,
        'type_objet': type_objet,
        'reference': reference,
        'description': description,
        'dossier': dossier,
        'client': client,
        'projet': projet,
        'ref_caisse': _as_text(data.get('ref_caisse')).strip(),
        'transporteur_ref': _as_text(data.get('transporteur_ref')).strip(),
        'longueur_cm': _as_text(data.get('longueur_cm')).strip(),
        'largeur_cm': _as_text(data.get('largeur_cm')).strip(),
        'hauteur_cm': _as_text(data.get('hauteur_cm')).strip(),
        'volume_m3': volume_m3,
        'surface_m2': surface_m2,
        'poids_kg': _as_text(data.get('poids_kg')).strip(),
        'lieu_stockage': _as_text(data.get('lieu_stockage')).strip(),
        'statut_logistique': _as_text(data.get('statut_logistique')).strip() or 'Créé',
        'created_at': now,
        'updated_at': now,
        'raw_json': {
            'source': 'creation_manuelle',
            'dossier': dossier,
            'created_at': now,
            'article_fields': article_fields,
        },
    }
    payload['search_text'] = _article_search_text(payload)

    try:
        with _ARTICLE_LOCK:
            article = _create_article_record(payload)
        return jsonify({
            'ok': True,
            'esi_id': article.get('esi_id'),
            'article': article,
        }), 201
    except Exception as e:
        print(f"[ARTICLE MANUEL] Création impossible : {e}")
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/articles/link-search')
def api_articles_link_search():
    """
    Recherche globale des PRODUITS liables a une fiche de caisse.

    Le champ search_text est construit a partir de l'ensemble des donnees de
    l'article : N ESI, dossier, reference, description, client/preteur, projet,
    dimensions, poids, stockage, caisse/colis, statut, raw_json, etc.

    Les COLIS restent exclus de la liaison a une fiche de caisse.
    """
    q = _as_text(request.args.get('q')).strip()
    if not q:
        return jsonify({'ok': True, 'articles': [], 'count': 0})

    clean_q = q.replace('*', '').strip()
    if not clean_q:
        return jsonify({'ok': True, 'articles': [], 'count': 0})

    rows = []
    seen = set()

    def add_rows(found_rows):
        for row in found_rows or []:
            esi_id = _as_text(row.get('esi_id')).strip()
            if not esi_id or esi_id in seen:
                continue
            type_objet = _as_text(row.get('type_objet') or 'PRODUIT').strip().upper()
            if type_objet == 'CONTENANT':
                continue
            seen.add(esi_id)
            rows.append(row)

    try:
        # Recherche principale : search_text contient toutes les informations
        # connues de la fiche article. Cela permet notamment de rechercher par
        # client/preteur, description, projet, stockage, caisse/colis, etc.
        safe_pattern = urllib.parse.quote('*' + clean_q + '*', safe='*')
        global_rows = supabase_rest_request(
            'GET', 'articles',
            'select=esi_id,dossier,reference,description,client,projet,'
            'ref_caisse,dernier_colis,lieu_stockage,type_objet,article_no'
            '&search_text=ilike.' + safe_pattern +
            '&order=article_no.asc&limit=500'
        ) or []
        add_rows(global_rows)

        # Secours pour les anciennes lignes dont search_text pourrait ne pas
        # encore contenir l'identifiant ESI genere par Supabase.
        esi_rows = supabase_rest_request(
            'GET', 'articles',
            'select=esi_id,dossier,reference,description,client,projet,'
            'ref_caisse,dernier_colis,lieu_stockage,type_objet,article_no'
            '&esi_id=ilike.' + safe_pattern +
            '&order=article_no.asc&limit=100'
        ) or []
        add_rows(esi_rows)

    except Exception as e:
        return jsonify({'ok': False, 'error': str(e), 'articles': []}), 500

    articles = [{
        'esi_id': _as_text(row.get('esi_id')).strip(),
        'dossier': _as_text(row.get('dossier')).strip(),
        'reference': _as_text(row.get('reference')).strip(),
        'description': _as_text(row.get('description')).strip(),
        'client': _as_text(row.get('client')).strip(),
        'projet': _as_text(row.get('projet')).strip(),
        'ref_caisse': _as_text(row.get('ref_caisse')).strip(),
        'dernier_colis': _as_text(row.get('dernier_colis')).strip(),
        'lieu_stockage': _as_text(row.get('lieu_stockage')).strip(),
    } for row in rows]

    articles.sort(key=lambda a: (
        _as_text(a.get('dossier')).casefold(),
        _as_text(a.get('reference')).casefold(),
        _as_text(a.get('esi_id')).casefold(),
    ))

    return jsonify({'ok': True, 'articles': articles, 'count': len(articles)})


@app.route('/api/articles/by-dossier')
def api_articles_by_dossier():
    """Retourne uniquement les articles déjà enregistrés pour un N° de dossier donné."""
    dossier = _as_text(request.args.get("dossier")).strip()
    if not dossier:
        return jsonify({"ok": False, "error": "Le N° de dossier est obligatoire", "articles": []}), 400

    safe_dossier = urllib.parse.quote(dossier, safe='')
    rows = supabase_rest_request(
        "GET",
        "articles",
        f"select={_ARTICLE_LIST_SELECT}&dossier=eq.{safe_dossier}&order=article_no.asc&limit=5000"
    ) or []

    articles = [_article_row_to_list_public(row) for row in rows]
    return jsonify({
        "ok": True,
        "dossier": dossier,
        "count": len(articles),
        "articles": articles,
    })


def _dossier_ticket_values(row):
    """Extrait Client / Projet / Chargé de projet d'un ancien ticket portant ce N° dossier."""
    raw = row.get("raw_json")
    if isinstance(raw, str) and raw.strip():
        try:
            raw = json.loads(raw)
        except Exception:
            raw = {}
    if not isinstance(raw, dict):
        raw = {}

    module = _as_text(row.get("module") or raw.get("module")).replace("’", "'").strip()
    preteur = _as_text(row.get("preteur") or raw.get("preteur")).strip()
    projet = _as_text(row.get("expo") or raw.get("expo") or row.get("objet") or raw.get("objet")).strip()
    charge = _as_text(row.get("charge_projet") or raw.get("chargeProjet")).strip()
    client = ""

    if module == "Avis d'arrivée":
        avis = raw.get("avisArrivee") or raw.get("avis_arrivee") or {}
        if isinstance(avis, dict):
            client = _as_text(avis.get("client")).strip()
            projet = _as_text(avis.get("projet")).strip() or projet
            charge = _as_text(avis.get("coordinateur")).strip() or charge
    elif module in ("Demande d'enlèvement", "Demande d'enlevement"):
        enl = raw.get("enlevement") or {}
        if isinstance(enl, dict):
            client = _as_text(enl.get("client")).strip()
            projet = _as_text(enl.get("exhibition")).strip() or projet
            charge = _as_text(enl.get("coordinateur")).strip() or charge
    elif module == "Fiche de caisse":
        # Dans ESI TICKETS, le prêteur est également l'identité client du dossier.
        client = preteur

    def clean(value):
        value = _as_text(value).strip()
        return "" if value in ("", "-") else value

    return {
        "client": clean(client),
        "projet": clean(projet),
        "charge_projet": clean(charge),
    }


@app.route('/api/dossiers/lookup')
def api_dossier_lookup():
    """
    Retourne les informations déjà connues pour un N° de dossier.

    Priorité :
      1. Client / Projet de la base Articles ;
      2. données des tickets existants portant exactement ce N° dossier ;
      3. Chargé de projet depuis le ticket le plus récent qui le renseigne.
    """
    dossier = _as_text(request.args.get('dossier')).strip()
    if not dossier:
        return jsonify({'ok': False, 'error': 'Le N° de dossier est obligatoire'}), 400

    client = ''
    projet = ''
    charge_projet = ''
    article_count = 0
    ticket_count = 0
    sources = []

    # Base Articles : c'est la source la plus fiable pour Client / Projet d'un dossier.
    try:
        identity = _article_dossier_identity(dossier)
        client = _as_text(identity.get('client')).strip()
        projet = _as_text(identity.get('projet')).strip()
        charge_projet = _as_text(identity.get('charge_projet')).strip()
        safe_dossier = urllib.parse.quote(dossier, safe='')
        count_rows = supabase_rest_request(
            'GET', 'articles', f'select=esi_id&dossier=eq.{safe_dossier}&limit=5000'
        ) or []
        article_count = len(count_rows)
        if client or projet or article_count:
            sources.append('articles')
    except Exception as e:
        print(f'[DOSSIER LOOKUP] Base articles indisponible pour {dossier}: {e}')

    # Tickets : complète les informations manquantes et retrouve le chargé de projet.
    try:
        safe_dossier = urllib.parse.quote(dossier, safe='')
        rows = supabase_rest_request(
            'GET',
            'tickets',
            'select=module,dossier,preteur,expo,objet,charge_projet,raw_json,created_at'
            f'&dossier=eq.{safe_dossier}&order=created_at.desc&limit=200'
        ) or []
        ticket_count = len(rows)
        if rows:
            sources.append('tickets')
        for row in rows:
            values = _dossier_ticket_values(row)
            if not client and values.get('client'):
                client = values['client']
            if not projet and values.get('projet'):
                projet = values['projet']
            if not charge_projet and values.get('charge_projet'):
                charge_projet = values['charge_projet']
            if client and projet and charge_projet:
                break
    except Exception as e:
        print(f'[DOSSIER LOOKUP] Tickets indisponibles pour {dossier}: {e}')

    found = bool(client or projet or charge_projet or article_count or ticket_count)
    return jsonify({
        'ok': True,
        'found': found,
        'dossier': dossier,
        'client': client,
        'projet': projet,
        'charge_projet': charge_projet,
        'article_count': article_count,
        'ticket_count': ticket_count,
        'sources': sources,
    })



def _mise_en_caisse_dossier_data(dossier):
    """Prépare les Articles, Pré-Packings et Packings disponibles pour un dossier."""
    dossier = _as_text(dossier).strip()
    if not dossier:
        raise ValueError("Le N° de dossier est obligatoire")

    safe_dossier = urllib.parse.quote(dossier, safe='')
    rows = supabase_rest_request(
        'GET',
        'articles',
        f"select={_ARTICLE_LIST_SELECT}&dossier=eq.{safe_dossier}&order=article_no.asc&limit=5000"
    ) or []

    articles = []
    prepackings = []
    packings_by_ref = {}

    for source in rows:
        public = _article_row_to_public(source)
        category = _as_text(public.get('categorie_metier')).strip().upper()
        raw = source.get('raw_json') if isinstance(source.get('raw_json'), dict) else {}
        item = {
            'esi_id': _as_text(public.get('esi_id')).strip(),
            'reference': _as_text(public.get('reference')).strip(),
            'description': _as_text(public.get('description')).strip(),
            'dossier': dossier,
            'client': _as_text(public.get('client')).strip(),
            'projet': _as_text(public.get('projet')).strip(),
            'longueur_cm': _as_text(public.get('longueur_cm')).strip(),
            'largeur_cm': _as_text(public.get('largeur_cm')).strip(),
            'hauteur_cm': _as_text(public.get('hauteur_cm')).strip(),
            'poids_kg': _as_text(public.get('poids_kg')).strip(),
            'lieu_stockage': _as_text(public.get('lieu_stockage')).strip(),
            'statut_logistique': _as_text(public.get('statut_logistique')).strip(),
            'prepacking_actuel': _as_text(public.get('dernier_colis')).strip(),
            'packing_actuel': _as_text(public.get('ref_caisse')).strip(),
            'categorie_metier': category,
        }

        if category == 'ARTICLE':
            articles.append(item)
            continue

        if category == 'PRE-PACKING':
            linked_ids = raw.get('article_esi_ids') or []
            if not isinstance(linked_ids, list):
                linked_ids = []
            item.update({
                'type_prepacking': _display_prepacking_type(public.get('type_colis')),
                'member_count': len([x for x in linked_ids if _as_text(x).strip()]),
                'member_esi_ids': [_as_text(x).strip() for x in linked_ids if _as_text(x).strip()],
            })
            prepackings.append(item)
            continue

        if category == 'PACKING':
            packing_ref = _as_text(public.get('packing_reference') or public.get('reference')).strip()
            if not packing_ref:
                continue
            item.update({
                'reference': packing_ref,
                'packing_reference': packing_ref,
                'packing_ticket_id': _as_text(public.get('packing_ticket_id')).strip(),
                'packing_type': _as_text(public.get('packing_type')).strip(),
            })
            packings_by_ref[packing_ref] = item

    # Secours : une ancienne fiche Packing peut exister dans les tickets sans avoir encore
    # été synchronisée vers la Base Articles. On l'ajoute à la liste sans modifier la base.
    try:
        ticket_rows = supabase_rest_request(
            'GET',
            'tickets',
            'select=id,module,dossier,ref,preteur,expo,objet,charge_projet,status,raw_json,created_at'
            f'&dossier=eq.{safe_dossier}&module=eq.' + urllib.parse.quote('Fiche de caisse', safe='') +
            '&order=created_at.desc&limit=500'
        ) or []
        for row in ticket_rows:
            ref = _packing_reference(dossier, row.get('ref'))
            if not ref or ref in packings_by_ref:
                continue
            raw = row.get('raw_json') if isinstance(row.get('raw_json'), dict) else {}
            fiche = raw.get('fiche') if isinstance(raw.get('fiche'), dict) else {}
            packings_by_ref[ref] = {
                'esi_id': '',
                'reference': ref,
                'packing_reference': ref,
                'packing_ticket_id': _as_text(row.get('id')).strip(),
                'packing_type': _as_text(fiche.get('typeCaisseFiche') or raw.get('typeCaisse')).strip(),
                'description': 'Packing',
                'dossier': dossier,
                'client': _as_text(row.get('preteur')).strip(),
                'projet': _as_text(row.get('expo') or row.get('objet')).strip(),
                'lieu_stockage': _as_text(fiche.get('localisation')).strip(),
                'statut_logistique': _as_text(row.get('status')).strip(),
                'categorie_metier': 'PACKING',
            }
    except Exception as e:
        print(f"[MISE EN CAISSE] Lecture des Packings historiques impossible pour {dossier}: {e}")

    identity = {'client': '', 'projet': '', 'charge_projet': ''}
    try:
        identity.update(_article_dossier_identity(dossier) or {})
    except Exception:
        pass

    # Si la base Articles ne suffit pas, reprend les informations depuis les tickets du dossier.
    if not all(_as_text(identity.get(k)).strip() for k in ('client', 'projet', 'charge_projet')):
        try:
            ticket_rows = supabase_rest_request(
                'GET',
                'tickets',
                'select=module,dossier,preteur,expo,objet,charge_projet,raw_json,created_at'
                f'&dossier=eq.{safe_dossier}&order=created_at.desc&limit=200'
            ) or []
            for row in ticket_rows:
                values = _dossier_ticket_values(row)
                for key in ('client', 'projet', 'charge_projet'):
                    if not _as_text(identity.get(key)).strip() and _as_text(values.get(key)).strip():
                        identity[key] = _as_text(values.get(key)).strip()
                if all(_as_text(identity.get(k)).strip() for k in ('client', 'projet', 'charge_projet')):
                    break
        except Exception as e:
            print(f"[MISE EN CAISSE] Identité dossier incomplète pour {dossier}: {e}")

    packings = list(packings_by_ref.values())
    packings.sort(key=lambda x: _as_text(x.get('reference')).casefold())
    return {
        'dossier': dossier,
        'identity': {
            'client': _as_text(identity.get('client')).strip(),
            'projet': _as_text(identity.get('projet')).strip(),
            'charge_projet': _as_text(identity.get('charge_projet')).strip(),
        },
        'articles': articles,
        'prepackings': prepackings,
        'packings': packings,
    }


@app.route('/api/mise-en-caisse/dossier')
def api_mise_en_caisse_dossier():
    dossier = _as_text(request.args.get('dossier')).strip()
    if not dossier:
        return jsonify({'ok': False, 'error': 'Le N° de dossier est obligatoire'}), 400
    try:
        data = _mise_en_caisse_dossier_data(dossier)
        return jsonify({
            'ok': True,
            **data,
            'counts': {
                'articles': len(data['articles']),
                'prepackings': len(data['prepackings']),
                'packings': len(data['packings']),
            }
        })
    except ValueError as e:
        return jsonify({'ok': False, 'error': str(e)}), 400
    except Exception as e:
        print(f"[MISE EN CAISSE] Chargement dossier {dossier} impossible: {e}")
        return jsonify({'ok': False, 'error': str(e)}), 500


def _validate_mise_en_caisse_request(payload):
    """Valide et enrichit une demande sans modifier l'état physique des Articles."""
    if not isinstance(payload, dict):
        raise ValueError('Données de mise en caisse invalides.')

    dossier = _as_text(payload.get('dossier')).strip()
    if not dossier:
        raise ValueError('Le N° de dossier est obligatoire.')

    data = _mise_en_caisse_dossier_data(dossier)
    packing_ref = _as_text(payload.get('packing_reference')).strip()
    packing_ticket_id = _as_text(payload.get('packing_ticket_id')).strip()
    packings = data.get('packings') or []
    packing = next((p for p in packings if _as_text(p.get('reference')).strip() == packing_ref), None)
    if not packing and packing_ticket_id:
        packing = next((p for p in packings if _as_text(p.get('packing_ticket_id')).strip() == packing_ticket_id), None)
    if not packing:
        raise ValueError('Le Packing sélectionné est introuvable pour ce dossier.')

    requested_articles = payload.get('article_esi_ids') or []
    requested_prepackings = payload.get('prepacking_esi_ids') or []
    if not isinstance(requested_articles, list) or not isinstance(requested_prepackings, list):
        raise ValueError('Sélection Articles / Pré-Packings invalide.')

    requested_articles = list(dict.fromkeys(_as_text(x).strip() for x in requested_articles if _as_text(x).strip()))
    requested_prepackings = list(dict.fromkeys(_as_text(x).strip() for x in requested_prepackings if _as_text(x).strip()))
    if not requested_articles and not requested_prepackings:
        raise ValueError('Sélectionne au moins un Article ou un Pré-Packing.')

    articles_by_id = {_as_text(x.get('esi_id')).strip(): x for x in data.get('articles') or [] if _as_text(x.get('esi_id')).strip()}
    prepackings_by_id = {_as_text(x.get('esi_id')).strip(): x for x in data.get('prepackings') or [] if _as_text(x.get('esi_id')).strip()}

    missing_articles = [x for x in requested_articles if x not in articles_by_id]
    missing_prepackings = [x for x in requested_prepackings if x not in prepackings_by_id]
    if missing_articles:
        raise ValueError('Article(s) introuvable(s) dans ce dossier : ' + ', '.join(missing_articles[:10]))
    if missing_prepackings:
        raise ValueError('Pré-Packing(s) introuvable(s) dans ce dossier : ' + ', '.join(missing_prepackings[:10]))

    return {
        'dossier': dossier,
        'client': _as_text(payload.get('client') or data.get('identity', {}).get('client')).strip(),
        'projet': _as_text(payload.get('projet') or data.get('identity', {}).get('projet')).strip(),
        'charge_projet': _as_text(payload.get('charge_projet') or data.get('identity', {}).get('charge_projet')).strip(),
        'date_souhaitee': _as_text(payload.get('date_souhaitee')).strip(),
        'commentaire': _as_text(payload.get('commentaire')).strip(),
        'packing': packing,
        'packing_reference': _as_text(packing.get('reference')).strip(),
        'packing_ticket_id': _as_text(packing.get('packing_ticket_id')).strip(),
        'articles': [articles_by_id[x] for x in requested_articles],
        'prepackings': [prepackings_by_id[x] for x in requested_prepackings],
        'article_esi_ids': requested_articles,
        'prepacking_esi_ids': requested_prepackings,
        'selection_count': len(requested_articles) + len(requested_prepackings),
        'mode_saisie': 'QR' if _as_text(payload.get('mode_saisie')).strip().upper() == 'QR' else 'MANUEL',
        'validation_mode': 'demande_uniquement',
    }


def _mise_en_caisse_rows_by_ids(esi_ids):
    """Charge des lignes Articles par N° ESI, par lots, sans changer leur ordre métier."""
    clean_ids = list(dict.fromkeys(
        _as_text(x).strip() for x in (esi_ids or []) if _as_text(x).strip()
    ))
    rows_by_id = {}
    for offset in range(0, len(clean_ids), 100):
        part = clean_ids[offset:offset + 100]
        if not part:
            continue
        encoded = urllib.parse.quote(','.join(part), safe=',-_')
        rows = supabase_rest_request(
            'GET', 'articles', 'select=*&esi_id=in.(' + encoded + ')&limit=100'
        ) or []
        for row in rows:
            esi_id = _as_text(row.get('esi_id')).strip()
            if esi_id:
                rows_by_id[esi_id] = dict(row)
    return rows_by_id


def _mise_en_caisse_prepacking_member_ids(prepacking_row, snapshot=None):
    """Retourne les Articles actuellement contenus dans un Pré-Packing.

    La base actuelle est prioritaire. Le snapshot du ticket sert uniquement de secours
    pour les anciens Pré-Packings dont la composition n'aurait pas été persistée.
    """
    raw = prepacking_row.get('raw_json') if isinstance(prepacking_row.get('raw_json'), dict) else {}
    member_ids = raw.get('article_esi_ids') or []
    if not isinstance(member_ids, list):
        member_ids = []
    if not member_ids and isinstance(snapshot, dict):
        member_ids = snapshot.get('member_esi_ids') or []
        if not isinstance(member_ids, list):
            member_ids = []
    return list(dict.fromkeys(
        _as_text(x).strip() for x in member_ids if _as_text(x).strip()
    ))


def _apply_mise_en_caisse_to_articles(ticket):
    """Applique une Mise en caisse validée dans la Base Articles.

    Règles métier :
      - Article direct -> ref_caisse = Packing cible ;
      - Article déjà dans un Pré-Packing -> dernier_colis reste inchangé et
        ref_caisse reçoit le Packing ;
      - Pré-Packing -> ref_caisse = Packing cible ;
      - les Articles contenus dans un Pré-Packing reçoivent eux aussi le Packing ;
      - la fiche Packing conserve la liste aplatie des Articles et la liste des
        Pré-Packings liés ;
      - l'opération est idempotente et ne déplace jamais silencieusement un élément
        déjà rattaché à un autre Packing.
    """
    if _as_text(ticket.get('module')).strip() != 'Mise en caisse':
        return {'applied': False, 'reason': 'not_mise_en_caisse'}

    mise = ticket.get('miseEnCaisse') if isinstance(ticket.get('miseEnCaisse'), dict) else {}
    mise = dict(mise or {})
    dossier = _as_text(mise.get('dossier') or ticket.get('dossier')).strip()
    packing_ref = _as_text(mise.get('packing_reference') or ticket.get('ref')).strip()
    packing_ticket_id = _as_text(mise.get('packing_ticket_id')).strip()
    direct_article_ids = list(dict.fromkeys(
        _as_text(x).strip() for x in (mise.get('article_esi_ids') or []) if _as_text(x).strip()
    ))
    prepacking_ids = list(dict.fromkeys(
        _as_text(x).strip() for x in (mise.get('prepacking_esi_ids') or []) if _as_text(x).strip()
    ))

    if not dossier:
        raise ValueError('Mise en caisse : N° dossier manquant.')
    if not packing_ref:
        raise ValueError('Mise en caisse : Packing cible manquant.')
    if not direct_article_ids and not prepacking_ids:
        raise ValueError('Mise en caisse : aucun Article ou Pré-Packing à valider.')

    # Vérifie que le Packing cible existe toujours au moment de la validation.
    current_data = _mise_en_caisse_dossier_data(dossier)
    current_packing = next((
        p for p in (current_data.get('packings') or [])
        if _as_text(p.get('reference')).strip() == packing_ref
    ), None)
    if not current_packing:
        raise ValueError(f'Mise en caisse : le Packing {packing_ref} n’existe plus dans le dossier {dossier}.')
    if not packing_ticket_id:
        packing_ticket_id = _as_text(current_packing.get('packing_ticket_id')).strip()

    # Références équivalentes historiques du même Packing, afin de ne pas créer
    # de faux conflits avec une ancienne valeur dossier-numéro.
    equivalent_refs = {
        packing_ref,
        _legacy_packing_reference(dossier, packing_ref),
    }
    equivalent_refs = {x for x in equivalent_refs if _as_text(x).strip()}

    selected_rows = _mise_en_caisse_rows_by_ids(direct_article_ids + prepacking_ids)
    missing = [x for x in direct_article_ids + prepacking_ids if x not in selected_rows]
    if missing:
        raise ValueError('Mise en caisse : élément(s) introuvable(s) dans la Base Articles : ' + ', '.join(missing[:10]))

    prepacking_snapshots = {
        _as_text(x.get('esi_id')).strip(): x
        for x in (mise.get('prepackings') or [])
        if isinstance(x, dict) and _as_text(x.get('esi_id')).strip()
    }

    member_ids = []
    validation_errors = []
    for esi_id in direct_article_ids:
        public = _article_row_to_public(selected_rows[esi_id])
        if _as_text(public.get('categorie_metier')).strip().upper() != 'ARTICLE':
            validation_errors.append(f'{esi_id} n’est pas un Article')
        if _as_text(public.get('dossier')).strip() != dossier:
            validation_errors.append(f'{esi_id} appartient au dossier {_as_text(public.get("dossier")).strip() or "?"}')

    for esi_id in prepacking_ids:
        row = selected_rows[esi_id]
        public = _article_row_to_public(row)
        if _as_text(public.get('categorie_metier')).strip().upper() != 'PRE-PACKING':
            validation_errors.append(f'{esi_id} n’est pas un Pré-Packing')
        if _as_text(public.get('dossier')).strip() != dossier:
            validation_errors.append(f'{esi_id} appartient au dossier {_as_text(public.get("dossier")).strip() or "?"}')
        member_ids.extend(_mise_en_caisse_prepacking_member_ids(row, prepacking_snapshots.get(esi_id)))

    if validation_errors:
        raise ValueError('Mise en caisse impossible : ' + ' ; '.join(validation_errors[:10]))

    member_ids = list(dict.fromkeys(member_ids))
    member_rows = _mise_en_caisse_rows_by_ids(member_ids)
    missing_members = [x for x in member_ids if x not in member_rows]
    if missing_members:
        raise ValueError(
            'Mise en caisse : Article(s) contenu(s) dans un Pré-Packing introuvable(s) : '
            + ', '.join(missing_members[:10])
        )

    # Tous les membres doivent rester des Articles du même dossier.
    for esi_id in member_ids:
        public = _article_row_to_public(member_rows[esi_id])
        if _as_text(public.get('categorie_metier')).strip().upper() != 'ARTICLE':
            raise ValueError(f'Mise en caisse : {esi_id} contenu dans un Pré-Packing n’est pas un Article.')
        if _as_text(public.get('dossier')).strip() != dossier:
            raise ValueError(f'Mise en caisse : {esi_id} n’appartient pas au dossier {dossier}.')

    all_article_ids = list(dict.fromkeys(direct_article_ids + member_ids))
    all_rows = dict(selected_rows)
    all_rows.update(member_rows)
    affected_ids = list(dict.fromkeys(all_article_ids + prepacking_ids))

    conflicts = []
    for esi_id in affected_ids:
        current_ref = _as_text(all_rows[esi_id].get('ref_caisse')).strip()
        if current_ref and current_ref not in equivalent_refs:
            conflicts.append(f'{esi_id} → {current_ref}')
    if conflicts:
        raise ValueError(
            'Mise en caisse impossible : certains éléments sont déjà liés à un autre Packing : '
            + ', '.join(conflicts[:10])
        )

    now_iso = datetime.now().isoformat()
    changed = []

    def patch_element(esi_id):
        row = all_rows[esi_id]
        raw = row.get('raw_json') if isinstance(row.get('raw_json'), dict) else {}
        raw = dict(raw or {})
        old_raw = row.get('raw_json') if isinstance(row.get('raw_json'), dict) else {}
        old_raw = dict(old_raw or {})
        history = list(raw.get('mise_en_caisse_history') or [])
        ticket_id = _as_text(ticket.get('id')).strip()
        if not any(_as_text(x.get('ticket_id')).strip() == ticket_id for x in history if isinstance(x, dict)):
            history.append({
                'ticket_id': ticket_id,
                'date': now_iso,
                'packing_reference': packing_ref,
                'mode_saisie': _as_text(mise.get('mode_saisie')).strip(),
            })
        raw['mise_en_caisse_history'] = history
        raw['packing_actuel'] = packing_ref
        extra = raw.get('article_fields') if isinstance(raw.get('article_fields'), dict) else {}
        extra = dict(extra or {})
        extra['packing_reference'] = packing_ref
        extra['mise_en_caisse_ticket_id'] = ticket_id
        extra['mise_en_caisse_date'] = now_iso
        raw['article_fields'] = extra

        patch = {
            'ref_caisse': packing_ref,
            # Important : dernier_colis n'est PAS modifié. Le lien Article -> Pré-Packing
            # reste donc intact pendant que l'on ajoute le lien vers le Packing.
            'updated_at': now_iso,
            'raw_json': raw,
        }
        merged = dict(row)
        merged.update(patch)
        patch['search_text'] = _article_search_text(merged)
        safe_esi = urllib.parse.quote(esi_id, safe='-')
        supabase_rest_request('PATCH', 'articles', f'esi_id=eq.{safe_esi}', patch, prefer='return=minimal')
        changed.append({
            'esi_id': esi_id,
            'ref_caisse': row.get('ref_caisse') or '',
            'updated_at': row.get('updated_at'),
            'raw_json': old_raw,
            'search_text': row.get('search_text') or '',
        })

    packing_ticket = None
    packing_ticket_backup = None
    try:
        with _ARTICLE_LOCK:
            for esi_id in affected_ids:
                patch_element(esi_id)

        # Synchronise également la fiche Packing existante : les Articles directs et
        # ceux contenus dans les Pré-Packings deviennent visibles dans sa composition.
        if packing_ticket_id:
            packing_ticket = load_ticket(packing_ticket_id)
        if packing_ticket:
            if _as_text(packing_ticket.get('module')).strip() != 'Fiche de caisse':
                raise ValueError('Le ticket Packing associé à la mise en caisse est invalide.')
            packing_ticket_backup = {
                'articles_lies': list(packing_ticket.get('articles_lies') or []),
                'prepackings_lies': list(packing_ticket.get('prepackings_lies') or []),
                'updatedAt': packing_ticket.get('updatedAt'),
            }

            existing_article_ids = []
            for item in packing_ticket.get('articles_lies') or []:
                val = _as_text(item.get('esi_id') if isinstance(item, dict) else item).strip()
                if val and val not in existing_article_ids:
                    existing_article_ids.append(val)
            merged_article_ids = list(dict.fromkeys(existing_article_ids + all_article_ids))
            article_rows_for_packing = _mise_en_caisse_rows_by_ids(merged_article_ids)
            packing_ticket['articles_lies'] = [
                {
                    'esi_id': esi_id,
                    'dossier': _as_text(article_rows_for_packing.get(esi_id, {}).get('dossier')).strip(),
                    'reference': _as_text(article_rows_for_packing.get(esi_id, {}).get('reference')).strip(),
                }
                for esi_id in merged_article_ids if esi_id in article_rows_for_packing
            ]

            existing_pre_ids = []
            for item in packing_ticket.get('prepackings_lies') or []:
                val = _as_text(item.get('esi_id') if isinstance(item, dict) else item).strip()
                if val and val not in existing_pre_ids:
                    existing_pre_ids.append(val)
            merged_pre_ids = list(dict.fromkeys(existing_pre_ids + prepacking_ids))
            packing_ticket['prepackings_lies'] = [
                {
                    'esi_id': esi_id,
                    'reference': _as_text(selected_rows.get(esi_id, {}).get('reference')).strip(),
                    'dossier': dossier,
                }
                for esi_id in merged_pre_ids if esi_id in selected_rows
            ]
            packing_ticket['updatedAt'] = now_iso
            save_ticket(packing_ticket)

        # Marque le ticket MEC comme effectivement appliqué. Le champ reste dans raw_json.
        mise['validation_mode'] = 'appliquee_base_articles'
        mise['appliquee_le'] = now_iso
        mise['packing_reference'] = packing_ref
        mise['articles_appliques'] = all_article_ids
        mise['prepackings_appliques'] = prepacking_ids
        mise['selection_physique_count'] = len(all_article_ids)
        ticket['miseEnCaisse'] = mise

        return {
            'applied': True,
            'packing_reference': packing_ref,
            'articles_directs': len(direct_article_ids),
            'prepackings': len(prepacking_ids),
            'articles_total': len(all_article_ids),
            'updated_elements': len(affected_ids),
            'applied_at': now_iso,
        }

    except Exception:
        # Rollback des lignes Articles déjà modifiées.
        with _ARTICLE_LOCK:
            for old in reversed(changed):
                try:
                    rollback_patch = {
                        'ref_caisse': old.get('ref_caisse') or '',
                        'updated_at': old.get('updated_at') or now_iso,
                        'raw_json': old.get('raw_json') or {},
                        'search_text': old.get('search_text') or '',
                    }
                    supabase_rest_request(
                        'PATCH', 'articles',
                        'esi_id=eq.' + urllib.parse.quote(old['esi_id'], safe='-'),
                        rollback_patch,
                        prefer='return=minimal'
                    )
                except Exception as rollback_error:
                    print(f"[MISE EN CAISSE] Rollback impossible pour {old.get('esi_id')}: {rollback_error}")

        if packing_ticket and packing_ticket_backup is not None:
            try:
                packing_ticket['articles_lies'] = packing_ticket_backup['articles_lies']
                packing_ticket['prepackings_lies'] = packing_ticket_backup['prepackings_lies']
                packing_ticket['updatedAt'] = packing_ticket_backup['updatedAt'] or now_iso
                save_ticket(packing_ticket)
            except Exception as rollback_error:
                print(f"[MISE EN CAISSE] Rollback fiche Packing impossible: {rollback_error}")
        raise


def _article_file_link(ticket_id, file_info, kind):
    if not isinstance(file_info, dict) or not file_info.get("name"):
        return None
    filename = _as_text(file_info.get("name")).strip()
    if not filename:
        return None
    base = "download-sheet" if kind == "gestionnaire" else "download"
    return {
        "name": filename,
        "kind": kind,
        "url": f"/api/tickets/{urllib.parse.quote(ticket_id, safe='')}/{base}/{urllib.parse.quote(filename, safe='')}",
    }


def _article_reception_history_from_ticket(ticket, article):
    esi_id = _as_text(article.get("esi_id")).strip()
    history = []

    module = _as_text(ticket.get("module")).replace("’", "'").strip()
    if module == "Avis d'arrivée":
        receptions = ticket.get("receptionsAvisArrivee") or []
    else:
        receptions = (ticket.get("enlevement") or {}).get("bons_livraison") or []

    for reception in receptions:
        items = reception.get("items") or []
        linked_item = None
        for item in items:
            if esi_id in [str(x).strip() for x in (item.get("esi_ids") or [])]:
                linked_item = item
                break
        if not linked_item:
            continue

        files = []
        candidates = [
            reception.get("bon_reception_filename"),
            reception.get("filename"),
            reception.get("etiquettes_articles_filename"),
            reception.get("etiquettes_colis_filename"),
        ]
        seen = set()
        for filename in candidates:
            filename = _as_text(filename).strip()
            if not filename or filename in seen:
                continue
            seen.add(filename)
            files.append({
                "name": filename,
                "kind": "gestionnaire",
                "url": f"/api/tickets/{urllib.parse.quote(ticket.get('id') or '', safe='')}/download-sheet/{urllib.parse.quote(filename, safe='')}",
            })

        colis_values = reception.get("colis") or linked_item.get("colis") or []
        if not isinstance(colis_values, list):
            colis_values = [colis_values] if colis_values else []
        colis_map = linked_item.get("colis_par_esi") if isinstance(linked_item.get("colis_par_esi"), dict) else {}
        article_colis = _as_text(colis_map.get(esi_id)).strip()
        type_map = linked_item.get("type_colis_par_esi") if isinstance(linked_item.get("type_colis_par_esi"), dict) else {}
        article_type_colis = _normalise_colis_type(type_map.get(esi_id))
        if not article_type_colis:
            rec_types = reception.get("colis_types") if isinstance(reception.get("colis_types"), dict) else {}
            article_type_colis = _normalise_colis_type(rec_types.get(article_colis))

        history.append({
            "type": "Réception",
            "reference": reception.get("reference") or "",
            "date": reception.get("receptionnee_le") or reception.get("created_at") or reception.get("date_reception") or "",
            "date_affichee": reception.get("date_reception") or "",
            "receptionne_par": reception.get("receptionne_par") or "",
            "lieu_stockage": reception.get("lieu_stockage") or linked_item.get("lieu_stockage") or "",
            "numero_dossier": reception.get("numero_dossier") or article.get("dossier") or "",
            "nombre_colis": reception.get("nombre_colis") or "",
            "colis": colis_values,
            "type_colis": article_type_colis,
            "partie_label": _as_text((linked_item.get("partie_par_esi") or {}).get(esi_id)).strip() if isinstance(linked_item.get("partie_par_esi"), dict) else _as_text(article.get("partie_label")).strip(),
            "parent_esi": _as_text((linked_item.get("parent_esi_par_esi") or {}).get(esi_id)).strip() if isinstance(linked_item.get("parent_esi_par_esi"), dict) else _as_text(article.get("parent_esi")).strip(),
            "quantite": linked_item.get("quantite") or "",
            "files": files,
        })

    return history




_ARTICLE_DB_EDITABLE_FIELDS = {
    "type_objet", "reference", "description", "dossier", "client", "projet",
    "ref_caisse", "transporteur_ref",
    "longueur_cm", "largeur_cm", "hauteur_cm",
    "volume_m3", "surface_m2", "poids_kg",
    "lieu_stockage", "statut_logistique", "dernier_colis",
}
_ARTICLE_EDITABLE_FIELDS = _ARTICLE_DB_EDITABLE_FIELDS | _ARTICLE_EXTRA_FIELDS


def _article_has_history(article):
    """Protège de la suppression les articles déjà liés à l'historique métier."""
    if _as_text(article.get("ticket_id")).strip():
        return True
    raw = article.get("raw_json") if isinstance(article.get("raw_json"), dict) else {}
    if raw.get("receptions"):
        return True
    if _as_text(article.get("derniere_reception_ref")).strip():
        return True
    if _as_text(article.get("dernier_colis")).strip():
        return True
    return False


def _article_dossier_identity(dossier):
    """Retourne Client / Projet / Chargé de projet connus pour un N° de dossier."""
    dossier = _as_text(dossier).strip()
    if not dossier:
        return {"client": "", "projet": "", "charge_projet": ""}

    safe_dossier = urllib.parse.quote(dossier, safe='')
    rows = supabase_rest_request(
        "GET", "articles",
        f"select=client,projet,raw_json&dossier=eq.{safe_dossier}&limit=10000"
    ) or []

    counts = {}
    charge_counts = {}
    for row in rows:
        client = _as_text(row.get("client")).strip()
        projet = _as_text(row.get("projet")).strip()
        raw = row.get("raw_json") if isinstance(row.get("raw_json"), dict) else {}
        extra = raw.get("article_fields") if isinstance(raw.get("article_fields"), dict) else {}
        charge = _as_text(extra.get("charge_projet") or raw.get("charge_projet") or raw.get("chargeProjet")).strip()
        if client or projet:
            key = (client, projet)
            counts[key] = counts.get(key, 0) + 1
        if charge:
            charge_counts[charge] = charge_counts.get(charge, 0) + 1

    if counts:
        client, projet = max(counts.items(), key=lambda kv: (kv[1], bool(kv[0][0]), bool(kv[0][1])))[0]
    else:
        client, projet = "", ""
    charge = max(charge_counts.items(), key=lambda kv: kv[1])[0] if charge_counts else ""

    # Complète les informations manquantes depuis les tickets existants du dossier.
    if not client or not projet or not charge:
        try:
            ticket_rows = supabase_rest_request(
                "GET", "tickets",
                "select=module,dossier,preteur,expo,objet,charge_projet,raw_json,created_at"
                f"&dossier=eq.{safe_dossier}&order=created_at.desc&limit=100"
            ) or []
            for ticket_row in ticket_rows:
                values = _dossier_ticket_values(ticket_row)
                if not client and values.get("client"):
                    client = values["client"]
                if not projet and values.get("projet"):
                    projet = values["projet"]
                if not charge and values.get("charge_projet"):
                    charge = values["charge_projet"]
                if client and projet and charge:
                    break
        except Exception as e:
            print(f"[ARTICLE DOSSIER] Tickets indisponibles pour {dossier}: {e}")

    return {"client": client, "projet": projet, "charge_projet": charge}


def _article_sync_dossier_identity(dossier, client=None, projet=None, charge_projet=None):
    """Uniformise Client / Projet / Chargé de projet sur les articles du même dossier."""
    dossier = _as_text(dossier).strip()
    if not dossier:
        return {"updated_count": 0, "client": "", "projet": "", "charge_projet": ""}

    identity = _article_dossier_identity(dossier)
    final_client = _as_text(client).strip() if client is not None else identity.get("client", "")
    final_projet = _as_text(projet).strip() if projet is not None else identity.get("projet", "")
    final_charge = _as_text(charge_projet).strip() if charge_projet is not None else identity.get("charge_projet", "")

    if not final_client and not final_projet and not final_charge:
        return {"updated_count": 0, "client": "", "projet": "", "charge_projet": ""}

    safe_dossier = urllib.parse.quote(dossier, safe='')
    rows = supabase_rest_request(
        "GET", "articles", f"select=*&dossier=eq.{safe_dossier}&limit=10000"
    ) or []

    updated = 0
    now = datetime.now().isoformat()
    for row in rows:
        patch = {}
        if final_client and _as_text(row.get("client")).strip() != final_client:
            patch["client"] = final_client
        if final_projet and _as_text(row.get("projet")).strip() != final_projet:
            patch["projet"] = final_projet

        raw = row.get("raw_json") if isinstance(row.get("raw_json"), dict) else {}
        raw = dict(raw or {})
        extra = raw.get("article_fields") if isinstance(raw.get("article_fields"), dict) else {}
        extra = dict(extra or {})
        if final_charge and _as_text(extra.get("charge_projet")).strip() != final_charge:
            extra["charge_projet"] = final_charge
            raw["article_fields"] = extra
            patch["raw_json"] = raw

        if not patch:
            continue

        merged = dict(row)
        merged.update(patch)
        patch["updated_at"] = now
        patch["search_text"] = _article_search_text(merged)
        safe_esi = urllib.parse.quote(_as_text(row.get("esi_id")).strip(), safe='-')
        supabase_rest_request(
            "PATCH", "articles", f"esi_id=eq.{safe_esi}", patch, prefer="return=minimal"
        )
        updated += 1

    return {
        "updated_count": updated,
        "client": final_client,
        "projet": final_projet,
        "charge_projet": final_charge,
    }


def _article_duplicate_key(article):
    """Clé conservative : ne confond jamais deux unités physiques légitimes."""
    raw = article.get("raw_json") if isinstance(article.get("raw_json"), dict) else {}
    source_module = _as_text(article.get("source_module")).strip()
    ticket_id = _as_text(article.get("ticket_id")).strip()
    source_index = article.get("source_index")
    unit_index = article.get("unit_index")

    if ticket_id and source_index is not None and unit_index is not None:
        return ("ticket", ticket_id, str(source_index), str(unit_index))

    if source_module == "Import Excel":
        filename = _as_text(raw.get("filename")).strip()
        dossier = _as_text(article.get("dossier") or raw.get("dossier")).strip()
        excel_row = raw.get("excel_row") if raw.get("excel_row") is not None else source_index
        if filename and dossier and excel_row is not None and unit_index is not None:
            return ("excel", filename.casefold(), dossier.casefold(), str(excel_row), str(unit_index))

    return None


def _article_duplicate_groups():
    rows = supabase_rest_request(
        "GET", "articles", "select=*&order=article_no.asc&limit=10000"
    ) or []
    grouped = {}
    for row in rows:
        key = _article_duplicate_key(row)
        if key is None:
            continue
        grouped.setdefault(key, []).append(dict(row))
    return [items for items in grouped.values() if len(items) > 1]


@app.route('/api/articles/bulk-update', methods=['PATCH'])
def api_articles_bulk_update():
    data = request.get_json(silent=True) or {}
    esi_ids = data.get('esi_ids') or []
    changes = data.get('changes') or {}

    if not isinstance(esi_ids, list) or not esi_ids:
        return jsonify({'ok': False, 'error': 'Aucun article sélectionné'}), 400
    if not isinstance(changes, dict) or not changes:
        return jsonify({'ok': False, 'error': 'Aucune modification demandée'}), 400

    clean_changes = {
        field: _as_text(value).strip()
        for field, value in changes.items()
        if field in _ARTICLE_EDITABLE_FIELDS
    }
    # Le formulaire manipule les 3 catégories métier, tandis que Supabase conserve
    # les 2 valeurs techniques historiques PRODUIT / CONTENANT.
    requested_business_type = _as_text(clean_changes.get('type_objet')).strip().upper()
    if requested_business_type:
        if requested_business_type in ('ARTICLE', 'PRODUIT'):
            clean_changes['type_objet'] = 'PRODUIT'
            clean_changes['categorie_metier'] = 'ARTICLE'
        elif requested_business_type in ('PRE-PACKING', 'PREPACKING', 'COLIS', 'CONTENANT'):
            clean_changes['type_objet'] = 'CONTENANT'
            clean_changes['categorie_metier'] = 'PRE-PACKING'
        elif requested_business_type == 'PACKING':
            clean_changes['type_objet'] = 'CONTENANT'
            clean_changes['categorie_metier'] = 'PACKING'
        else:
            return jsonify({'ok': False, 'error': 'Type invalide : ARTICLE, PRE-PACKING ou PACKING attendu'}), 400
    if not clean_changes:
        return jsonify({'ok': False, 'error': 'Aucun champ modifiable fourni'}), 400

    target_dossier = _as_text(clean_changes.get('dossier')).strip() if 'dossier' in clean_changes else ''
    if target_dossier:
        identity = _article_dossier_identity(target_dossier)
        if 'client' not in clean_changes and identity.get('client'):
            clean_changes['client'] = identity['client']
        if 'projet' not in clean_changes and identity.get('projet'):
            clean_changes['projet'] = identity['projet']
        if 'charge_projet' not in clean_changes and identity.get('charge_projet'):
            clean_changes['charge_projet'] = identity['charge_projet']

    updated = []
    errors = []
    now = datetime.now().isoformat()
    affected_dossiers = set()

    with _ARTICLE_LOCK:
        for esi_id in dict.fromkeys(_as_text(x).strip() for x in esi_ids if _as_text(x).strip()):
            safe_esi = urllib.parse.quote(esi_id, safe='-')
            try:
                rows = supabase_rest_request(
                    'GET', 'articles', f'select=*&esi_id=eq.{safe_esi}&limit=1'
                ) or []
                if not rows:
                    errors.append({'esi_id': esi_id, 'error': 'Article introuvable'})
                    continue

                current = dict(rows[0])
                current_public = _article_row_to_public(current)
                patch = {k: v for k, v in clean_changes.items() if k in _ARTICLE_DB_EDITABLE_FIELDS}

                raw = current.get('raw_json') if isinstance(current.get('raw_json'), dict) else {}
                raw = dict(raw or {})
                extra = raw.get('article_fields') if isinstance(raw.get('article_fields'), dict) else {}
                extra = dict(extra or {})
                for field, value in clean_changes.items():
                    if field in _ARTICLE_EXTRA_FIELDS:
                        extra[field] = value

                # Les volumes et surfaces sont toujours dérivés des dimensions en cm.
                dims_changed = any(k in clean_changes for k in ('longueur_cm', 'largeur_cm', 'hauteur_cm'))
                if dims_changed:
                    l = patch.get('longueur_cm', current.get('longueur_cm'))
                    w = patch.get('largeur_cm', current.get('largeur_cm'))
                    h = patch.get('hauteur_cm', current.get('hauteur_cm'))
                    volume, surface = _article_calculated_metrics(l, w, h)
                    patch['volume_m3'] = volume
                    patch['surface_m2'] = surface

                oeuvre_dims_changed = any(k in clean_changes for k in (
                    'oeuvre_longueur_cm', 'oeuvre_largeur_cm', 'oeuvre_hauteur_cm'
                ))
                if oeuvre_dims_changed:
                    l = extra.get('oeuvre_longueur_cm', current_public.get('oeuvre_longueur_cm'))
                    w = extra.get('oeuvre_largeur_cm', current_public.get('oeuvre_largeur_cm'))
                    h = extra.get('oeuvre_hauteur_cm', current_public.get('oeuvre_hauteur_cm'))
                    volume, surface = _article_calculated_metrics(l, w, h)
                    extra['oeuvre_volume_m3'] = volume
                    extra['oeuvre_surface_m2'] = surface

                if any(field in clean_changes for field in _ARTICLE_EXTRA_FIELDS) or oeuvre_dims_changed:
                    raw['article_fields'] = extra
                    patch['raw_json'] = raw

                patch['updated_at'] = now
                merged = dict(current)
                merged.update(patch)
                patch['search_text'] = _article_search_text(merged)

                supabase_rest_request(
                    'PATCH', 'articles', f'esi_id=eq.{safe_esi}',
                    patch, prefer='return=minimal'
                )
                updated.append(esi_id)
                dossier_after = _as_text(merged.get('dossier')).strip()
                if dossier_after:
                    affected_dossiers.add(dossier_after)
            except Exception as e:
                errors.append({'esi_id': esi_id, 'error': str(e)})

        dossier_sync = []
        for dossier in sorted(affected_dossiers):
            try:
                sync_client = clean_changes.get('client') if 'client' in clean_changes else None
                sync_projet = clean_changes.get('projet') if 'projet' in clean_changes else None
                sync_charge = clean_changes.get('charge_projet') if 'charge_projet' in clean_changes else None
                result = _article_sync_dossier_identity(dossier, sync_client, sync_projet, sync_charge)
                dossier_sync.append({'dossier': dossier, **result})
            except Exception as e:
                errors.append({'dossier': dossier, 'error': f'Synchronisation identité dossier : {e}'})

    return jsonify({
        'ok': not errors,
        'updated_count': len(updated),
        'updated': updated,
        'dossier_sync': dossier_sync,
        'errors': errors,
    }), (200 if not errors else 207)


@app.route('/api/articles/bulk-delete', methods=['POST'])
def api_articles_bulk_delete():
    data = request.get_json(silent=True) or {}
    esi_ids = list(dict.fromkeys(
        _as_text(x).strip() for x in (data.get('esi_ids') or []) if _as_text(x).strip()
    ))

    if not esi_ids:
        return jsonify({'ok': False, 'error': 'Aucun article sélectionné'}), 400

    deleted = []
    protected = []
    errors = []

    with _ARTICLE_LOCK:
        for offset in range(0, len(esi_ids), 100):
            part = esi_ids[offset:offset + 100]
            encoded = urllib.parse.quote(','.join(part), safe=',-_')
            try:
                rows = supabase_rest_request(
                    'GET', 'articles', f'select=*&esi_id=in.({encoded})&limit=100'
                ) or []
                by_id = {_as_text(r.get('esi_id')).strip(): dict(r) for r in rows}
                deletable = []
                for esi_id in part:
                    article = by_id.get(esi_id)
                    if not article:
                        errors.append({'esi_id': esi_id, 'error': 'Article introuvable'})
                        continue
                    if _article_has_history(article):
                        protected.append({
                            'esi_id': esi_id,
                            'reason': 'Article lié à un ticket ou à un historique de réception'
                        })
                    else:
                        deletable.append(esi_id)

                if deletable:
                    encoded_delete = urllib.parse.quote(','.join(deletable), safe=',-_')
                    supabase_rest_request(
                        'DELETE', 'articles', f'esi_id=in.({encoded_delete})', prefer='return=minimal'
                    )
                    deleted.extend(deletable)
            except Exception as e:
                errors.append({'esi_ids': part, 'error': str(e)})

    return jsonify({
        'ok': not errors and not protected,
        'deleted_count': len(deleted),
        'deleted': deleted,
        'protected': protected,
        'errors': errors,
    }), (200 if not errors and not protected else 207)


@app.route('/api/articles/duplicates', methods=['GET'])
def api_articles_duplicates():
    try:
        groups = _article_duplicate_groups()
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500

    public_groups = []
    duplicate_count = 0
    deletable_count = 0
    protected_count = 0
    for items in groups:
        ordered = sorted(items, key=lambda a: int(a.get('article_no') or 0))
        keep = ordered[0]
        extras = ordered[1:]
        deletable = [x for x in extras if not _article_has_history(x)]
        protected = [x for x in extras if _article_has_history(x)]
        duplicate_count += len(extras)
        deletable_count += len(deletable)
        protected_count += len(protected)
        public_groups.append({
            'keep': _article_row_to_public(keep),
            'duplicates': [_article_row_to_public(x) for x in extras],
            'deletable_esi_ids': [_as_text(x.get('esi_id')).strip() for x in deletable],
            'protected_esi_ids': [_as_text(x.get('esi_id')).strip() for x in protected],
        })

    return jsonify({
        'ok': True,
        'group_count': len(groups),
        'duplicate_count': duplicate_count,
        'deletable_count': deletable_count,
        'protected_count': protected_count,
        'groups': public_groups,
    })


@app.route('/api/articles/delete-duplicates', methods=['POST'])
def api_articles_delete_duplicates():
    try:
        groups = _article_duplicate_groups()
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500

    to_delete = []
    protected = []
    for items in groups:
        ordered = sorted(items, key=lambda a: int(a.get('article_no') or 0))
        for article in ordered[1:]:
            esi_id = _as_text(article.get('esi_id')).strip()
            if _article_has_history(article):
                protected.append(esi_id)
            elif esi_id:
                to_delete.append(esi_id)

    deleted = []
    errors = []
    with _ARTICLE_LOCK:
        for offset in range(0, len(to_delete), 100):
            part = to_delete[offset:offset + 100]
            encoded = urllib.parse.quote(','.join(part), safe=',-_')
            try:
                supabase_rest_request(
                    'DELETE', 'articles', f'esi_id=in.({encoded})', prefer='return=minimal'
                )
                deleted.extend(part)
            except Exception as e:
                errors.append({'esi_ids': part, 'error': str(e)})

    return jsonify({
        'ok': not errors,
        'deleted_count': len(deleted),
        'deleted': deleted,
        'protected_count': len(protected),
        'protected': protected,
        'errors': errors,
    }), (200 if not errors else 207)


@app.route('/api/articles/colis-by-dossier')
def api_articles_colis_by_dossier():
    dossier=_as_text(request.args.get('dossier')).strip()
    if not dossier: return jsonify({'ok':False,'error':'N° dossier obligatoire','colis':[]}),400
    nums=sorted(_existing_colis_numbers(dossier))
    return jsonify({'ok':True,'dossier':dossier,'colis':[f"{dossier}-{n:03d}" for n in nums]})

@app.route('/api/articles/<esi_id>/photo', methods=['GET', 'POST', 'DELETE'])
def api_article_photo(esi_id):
    """Affiche, ajoute/remplace ou retire la photo principale d'un article."""
    esi_id = _as_text(esi_id).strip()
    safe_esi = urllib.parse.quote(esi_id, safe='-')
    rows = supabase_rest_request(
        'GET', 'articles', f'select=*&esi_id=eq.{safe_esi}&limit=1'
    ) or []
    if not rows:
        return jsonify({'ok': False, 'error': 'Article introuvable'}), 404

    article = dict(rows[0])
    raw = article.get('raw_json') if isinstance(article.get('raw_json'), dict) else {}
    raw = dict(raw or {})

    if request.method == 'GET':
        storage_path = _as_text(raw.get('photo_storage_path')).strip()
        if not storage_path:
            abort(404)
        try:
            return redirect(supabase_signed_download_url(storage_path, expires_in=900))
        except Exception as e:
            print(f'[ARTICLE PHOTO] URL signee impossible pour {esi_id}: {e}')
            try:
                data = supabase_download_bytes(storage_path)
            except Exception:
                abort(404)
            return send_file(
                BytesIO(data),
                mimetype=_as_text(raw.get('photo_content_type')).strip() or 'image/jpeg',
                download_name=_as_text(raw.get('photo_filename')).strip() or f'{esi_id}.jpg',
            )

    if request.method == 'DELETE':
        for key in (
            'photo_storage_path', 'photo_filename', 'photo_content_type',
            'photo_updated_at', 'photo_url', 'photo', 'image_url'
        ):
            raw.pop(key, None)
        raw['photo_deleted_at'] = datetime.now().isoformat()
        patch = {
            'raw_json': raw,
            'updated_at': datetime.now().isoformat(),
        }
        merged = dict(article)
        merged.update(patch)
        patch['search_text'] = _article_search_text(merged)
        supabase_rest_request(
            'PATCH', 'articles', f'esi_id=eq.{safe_esi}', patch, prefer='return=minimal'
        )
        return jsonify({'ok': True, 'esi_id': esi_id, 'photo_url': ''})

    fs = request.files.get('photo') or request.files.get('file')
    if not fs or not fs.filename:
        return jsonify({'ok': False, 'error': 'Photo manquante'}), 400

    filename = _as_text(fs.filename).strip()
    ext = Path(filename).suffix.lower()
    allowed_ext = {'.jpg', '.jpeg', '.png', '.webp'}
    if ext not in allowed_ext:
        return jsonify({
            'ok': False,
            'error': 'Format photo non pris en charge. Utilise JPG, PNG ou WEBP.'
        }), 400

    content = fs.read()
    if not content:
        return jsonify({'ok': False, 'error': 'Le fichier image est vide'}), 400
    if len(content) > 15 * 1024 * 1024:
        return jsonify({'ok': False, 'error': 'La photo dépasse la limite de 15 Mo'}), 400

    # Vérification simple de la signature du fichier pour éviter qu'un autre type
    # de contenu soit envoyé avec une extension d'image.
    valid_signature = False
    if ext in ('.jpg', '.jpeg'):
        valid_signature = content.startswith(b'\xff\xd8\xff')
    elif ext == '.png':
        valid_signature = content.startswith(b'\x89PNG\r\n\x1a\n')
    elif ext == '.webp':
        valid_signature = len(content) >= 12 and content[:4] == b'RIFF' and content[8:12] == b'WEBP'
    if not valid_signature:
        return jsonify({'ok': False, 'error': 'Le fichier ne semble pas être une image valide'}), 400

    safe_name = safe_filename(filename) or ('photo' + ext)
    stamp = datetime.now().strftime('%Y%m%d%H%M%S%f')
    storage_path = f'article_photos/{safe_filename(esi_id)}/{stamp}_{safe_name}'
    content_type = _as_text(fs.content_type).strip().lower()
    if content_type not in {'image/jpeg', 'image/png', 'image/webp'}:
        content_type = {
            '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
            '.png': 'image/png', '.webp': 'image/webp'
        }[ext]

    try:
        supabase_upload_bytes(storage_path, content, content_type)
    except Exception as e:
        return jsonify({'ok': False, 'error': f"Impossible d'enregistrer la photo : {e}"}), 500

    raw['photo_storage_path'] = storage_path
    raw['photo_filename'] = filename
    raw['photo_content_type'] = content_type
    raw['photo_updated_at'] = datetime.now().isoformat()
    raw.pop('photo_deleted_at', None)

    patch = {
        'raw_json': raw,
        'updated_at': datetime.now().isoformat(),
    }
    merged = dict(article)
    merged.update(patch)
    patch['search_text'] = _article_search_text(merged)
    supabase_rest_request(
        'PATCH', 'articles', f'esi_id=eq.{safe_esi}', patch, prefer='return=minimal'
    )

    photo_url = f'/api/articles/{urllib.parse.quote(esi_id, safe="-")}/photo?v={urllib.parse.quote(raw["photo_updated_at"], safe="")}'
    return jsonify({
        'ok': True,
        'esi_id': esi_id,
        'photo_url': photo_url,
        'filename': filename,
    })


@app.route('/api/articles/<esi_id>/colis', methods=['PATCH'])
def api_article_update_colis(esi_id):
    safe_esi=urllib.parse.quote(_as_text(esi_id).strip(),safe='-')
    rows=supabase_rest_request('GET','articles',f'select=*&esi_id=eq.{safe_esi}&limit=1') or []
    if not rows: return jsonify({'ok':False,'error':'Article introuvable'}),404
    article=dict(rows[0]); dossier=_as_text(article.get('dossier')).strip()
    colis=_as_text((request.get_json(silent=True) or {}).get('colis')).strip()
    if colis:
        allowed={f"{dossier}-{n:03d}" for n in _existing_colis_numbers(dossier)}
        if colis not in allowed: return jsonify({'ok':False,'error':"Ce Pre-Packing n'existe pas pour ce dossier"}),400
    raw=article.get('raw_json') if isinstance(article.get('raw_json'),dict) else {}; raw=dict(raw or {})
    raw['colis_actuel']=colis; mods=list(raw.get('modifications_colis') or []); mods.append({'date':datetime.now().isoformat(),'colis':colis}); raw['modifications_colis']=mods
    patch={'dernier_colis':colis,'raw_json':raw,'updated_at':datetime.now().isoformat()}
    merged=dict(article); merged.update(patch); patch['search_text']=_article_search_text(merged)
    supabase_rest_request('PATCH','articles',f'esi_id=eq.{safe_esi}',patch,prefer='return=minimal')
    return jsonify({'ok':True,'esi_id':esi_id,'colis':colis})

@app.route('/api/articles/<esi_id>')
def api_article_detail(esi_id):
    esi_id = _as_text(esi_id).strip()
    safe_esi = urllib.parse.quote(esi_id, safe='-')
    rows = supabase_rest_request(
        "GET", "articles", f"select={_ARTICLE_DETAIL_SELECT}&esi_id=eq.{safe_esi}&limit=1"
    ) or []
    if not rows:
        return jsonify({"error": "Article introuvable"}), 404

    article = _article_row_to_public(rows[0])
    article_raw = article.get("raw_json") if isinstance(article.get("raw_json"), dict) else {}
    article_raw = dict(article_raw or {})
    photo_storage_path = _as_text(article_raw.get("photo_storage_path")).strip()
    if photo_storage_path:
        photo_version = _as_text(article_raw.get("photo_updated_at") or article.get("updated_at")).strip()
        article["photo_url"] = (
            f"/api/articles/{urllib.parse.quote(esi_id, safe='-')}/photo"
            + ("?v=" + urllib.parse.quote(photo_version, safe='') if photo_version else "")
        )
        article["photo_filename"] = _as_text(article_raw.get("photo_filename")).strip()

    ticket_id = _as_text(article.get("ticket_id")).strip()
    ticket = load_ticket(ticket_id) if ticket_id else None

    part_meta = _article_part_meta(article)
    parent_summary = None
    composition_parts = []
    if part_meta.get("parent_esi"):
        safe_parent = urllib.parse.quote(part_meta["parent_esi"], safe='-')
        parent_rows = supabase_rest_request("GET", "articles", f"select={_ARTICLE_DETAIL_SELECT}&esi_id=eq.{safe_parent}&limit=1") or []
        if parent_rows:
            p = _article_row_to_public(parent_rows[0])
            parent_summary = {"esi_id": p.get("esi_id"), "reference": p.get("reference"), "description": p.get("description"), "statut_logistique": p.get("statut_logistique"), "dernier_colis": p.get("dernier_colis")}
        composition_parts = _article_parts_for_parent(part_meta["parent_esi"], article.get("dossier"))
    else:
        # Avant : chaque ouverture de fiche chargeait jusqu'à 5000 lignes du dossier
        # pour vérifier si l'article avait des parties. On ne le fait désormais que
        # pour les articles explicitement déclarés multi-parties.
        has_declared_parts = (
            _as_text(article.get("article_en_plusieurs_parties")).strip().lower() in ("oui", "yes", "true", "1")
            or bool(article_raw.get("parts"))
            or bool(article_raw.get("parts_total"))
        )
        if has_declared_parts:
            composition_parts = _article_parts_for_parent(esi_id, article.get("dossier"))

    detail = {
        "article": article,
        "ticket": None,
        "avis_arrivee": None,
        "demande_enlevement": None,
        "fiche_caisse": None,
        "packing_articles": [],
        "packing_prepackings": [],
        "receptions": [],
        "documents_source": [],
        "composition": {"is_part": bool(part_meta.get("parent_esi")), "parent": parent_summary, "partie_label": part_meta.get("partie_label") or "", "parts": composition_parts},
    }

    if ticket:
        detail["ticket"] = {
            "id": ticket.get("id"),
            "module": ticket.get("module"),
            "status": ticket.get("status"),
            "created_at": ticket.get("createdAt"),
            "updated_at": ticket.get("updatedAt"),
            "dossier": ticket.get("dossier"),
            "ref": (_packing_reference(ticket.get("dossier"), ticket.get("ref")) if ticket.get("module") == "Fiche de caisse" else ticket.get("ref")),
            "client": ticket.get("dossier"),
            "projet": ticket.get("expo") or ticket.get("objet"),
            "charge_projet": ticket.get("chargeProjet"),
        }

        for f in ticket.get("files") or []:
            link = _article_file_link(ticket_id, f, "demandeur")
            if link:
                detail["documents_source"].append(link)

        module = _as_text(ticket.get("module")).replace("’", "'").strip()
        if module == "Avis d'arrivée":
            avis = ticket.get("avisArrivee") or ticket.get("avis_arrivee") or {}
            detail["avis_arrivee"] = {
                "dossier_ref": avis.get("dossier_ref") or "",
                "client": avis.get("client") or "",
                "projet": avis.get("projet") or "",
                "date_reception_prevue": avis.get("date_reception_prevue") or "",
                "coordinateur": avis.get("coordinateur") or "",
                "commentaire": avis.get("commentaire") or ticket.get("commentaire") or "",
                "expediteur": avis.get("expediteur") or {},
                "transporteur": avis.get("transporteur") or {},
            }
        elif module == "Fiche de caisse":
            fiche = ticket.get('fiche') if isinstance(ticket.get('fiche'), dict) else {}
            reception = ticket.get('reception') if isinstance(ticket.get('reception'), dict) else {}
            detail["fiche_caisse"] = {
                "ticket_id": ticket.get('id') or '',
                "reference": _packing_reference(ticket.get('dossier'), ticket.get('ref')),
                "status": ticket.get('status') or '',
                "type_caisse": fiche.get('typeCaisseFiche') or ticket.get('typeCaisse') or '',
                "dimensions_ext": fiche.get('dimensionsExt') or '',
                "poids": fiche.get('poids') or '',
                "date_mise_dispo": ticket.get('dateEmballage') or '',
                "charge_projet": ticket.get('chargeProjet') or '',
                "preteur": ticket.get('preteur') or '',
                "projet": ticket.get('expo') or ticket.get('objet') or '',
                "localisation": fiche.get('localisation') or '',
                "receptionnee": bool(reception.get('receptionnee') is True or reception.get('receptionnee_le') or fiche.get('localisation')),
                "receptionnee_le": reception.get('receptionnee_le') or '',
                "bl_numero": reception.get('bl_numero') or '',
                "bl_date": reception.get('bl_date') or '',
            }
            try:
                detail["packing_articles"] = _linked_articles_for_ticket(ticket)
            except Exception:
                detail["packing_articles"] = _packing_article_link_summaries(ticket)
            detail["packing_prepackings"] = list(ticket.get("prepackings_lies") or [])
        elif module in ("Demande d'enlèvement", "Demande d'enlevement"):
            enl = ticket.get("enlevement") or {}
            detail["demande_enlevement"] = {
                "numero_bon": enl.get("numero_bon") or ticket.get("ref") or "",
                "client": enl.get("client") or ticket.get("dossier") or "",
                "projet": enl.get("exhibition") or ticket.get("expo") or ticket.get("objet") or "",
                "coordinateur": enl.get("coordinateur") or ticket.get("chargeProjet") or "",
                "date_enlevement": enl.get("date_enlevement") or "",
                "adresse_depart": enl.get("adresse_depart") or "",
                "adresse_destination": enl.get("adresse_destination") or "",
                "notes": enl.get("notes") or "",
                "instructions": enl.get("instructions") or "",
            }

        if module != "Fiche de caisse":
            detail["receptions"] = _article_reception_history_from_ticket(ticket, article)

    raw = article_raw
    raw_receptions = raw.get("receptions") or []
    if raw_receptions:
        known_refs = {str(x.get("reference") or "") for x in detail["receptions"]}
        for r in raw_receptions:
            ref = _as_text(r.get("reception_ref")).strip()
            if ref and ref in known_refs:
                continue
            detail["receptions"].append({
                "type": "Réception",
                "reference": ref,
                "date": r.get("date") or "",
                "date_affichee": "",
                "receptionne_par": r.get("receptionne_par") or "",
                "lieu_stockage": r.get("lieu_stockage") or "",
                "numero_dossier": article.get("dossier") or "",
                "nombre_colis": "",
                "colis": r.get("colis") or [],
                "type_colis": _normalise_colis_type(r.get("type_colis")),
                "partie_label": _as_text(article.get("partie_label")).strip(),
                "parent_esi": _as_text(article.get("parent_esi")).strip(),
                "quantite": "1",
                "files": [],
            })

    return jsonify(detail)


@app.route('/api/articles/<path:esi_id>/etiquette')
def api_article_single_label(esi_id):
    """Génère à la demande l'étiquette de l'Article, du Pré-Packing ou du Packing."""
    esi_id = _as_text(esi_id).strip()
    if not esi_id:
        abort(404)
    safe_esi = urllib.parse.quote(esi_id, safe='-')
    rows = supabase_rest_request('GET', 'articles', f'select=*&esi_id=eq.{safe_esi}&limit=1') or []
    if not rows:
        abort(404)

    article = _article_row_to_public(rows[0])
    category = _as_text(article.get('categorie_metier')).strip().upper() or 'ARTICLE'
    dossier = _as_text(article.get('dossier')).strip()
    client = _as_text(article.get('client')).strip()
    charge_projet = _as_text(article.get('charge_projet')).strip()
    lieu = _as_text(article.get('lieu_stockage')).strip()
    bon = _as_text(article.get('derniere_reception_ref') or article.get('transporteur_ref')).strip()

    if category == 'PRE-PACKING':
        prepacking_ref = _as_text(article.get('numero_colis') or article.get('reference') or article.get('dernier_colis')).strip()
        if not prepacking_ref:
            prepacking_ref = esi_id
        prepacking_type = _normalise_colis_type(article.get('type_colis'))
        label = {
            'titre': 'PRE-PACKING',
            'principal': _colis_display(prepacking_ref, prepacking_type),
            'dossier': dossier,
            'client': client,
            'charge_projet': charge_projet,
            'colis': prepacking_ref,
            'type_colis': prepacking_type,
            'lieu': lieu,
            'bon': bon,
            'qr_url': _colis_qr_url(prepacking_ref),
        }
        pdf_bytes = _build_labels_pdf_bytes([label], kind='colis')
        filename = f"{safe_filename(prepacking_ref)}_etiquette_pre_packing.pdf"
    elif category == 'PACKING':
        packing_ref = _as_text(article.get('packing_reference') or article.get('reference') or article.get('ref_caisse')).strip()
        if not packing_ref:
            packing_ref = esi_id
        packing_type = _as_text(article.get('packing_type')).strip()
        label = {
            'titre': 'PACKING',
            'principal': packing_ref,
            'dossier': dossier,
            'client': client,
            'charge_projet': charge_projet,
            'packing_type': packing_type,
            'lieu': lieu,
            'bon': bon,
            # Le QR ouvre la carte d'identité de la ligne PACKING ; le mode Mise en caisse
            # reconnait également cette URL /article/ESI-x comme un Packing grâce à la base.
            'qr_url': _article_qr_url(esi_id),
        }
        pdf_bytes = _build_labels_pdf_bytes([label], kind='colis')
        filename = f"{safe_filename(packing_ref)}_etiquette_packing.pdf"
    else:
        label = {
            'titre': 'ARTICLE',
            'principal': esi_id,
            'esi_id': esi_id,
            'dossier': dossier,
            'client': client,
            'charge_projet': charge_projet,
            'reference': _as_text(article.get('reference')).strip(),
            'designation': _as_text(article.get('description')).strip(),
            'partie': _as_text(article.get('partie_label')).strip(),
            'article_principal': _as_text(article.get('parent_esi')).strip(),
            'quantite': '1',
            'colis': _as_text(article.get('dernier_colis')).strip(),
            'lieu': lieu,
            'bon': bon,
            'qr_url': _article_qr_url(esi_id),
        }
        pdf_bytes = _build_labels_pdf_bytes([label], kind='article')
        filename = f"{safe_filename(esi_id)}_etiquette_article.pdf"

    response = send_file(
        BytesIO(pdf_bytes),
        mimetype='application/pdf',
        as_attachment=False,
        download_name=filename,
    )
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Content-Disposition'] = f'inline; filename="{filename}"'
    return response


@app.route('/article/<path:esi_id>')
def article_public_page(esi_id):
    """Carte d'identité mobile en lecture seule ouverte depuis le QR de l'étiquette article."""
    esi_id = _as_text(esi_id).strip()
    supplied = _as_text(request.args.get('k')).strip()
    expected = _article_qr_token(esi_id)
    if not supplied or not hmac.compare_digest(supplied, expected):
        abort(403)

    safe_esi = urllib.parse.quote(esi_id, safe='-')
    try:
        rows = supabase_rest_request(
            'GET', 'articles', f'select=*&esi_id=eq.{safe_esi}&limit=1'
        ) or []
    except Exception as e:
        print(f'[QR ARTICLE] Lecture impossible pour {esi_id}: {e}')
        return (
            '<!doctype html><html lang="fr"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<body style="font-family:Arial,sans-serif;padding:24px"><h2>Article indisponible</h2>'
            '<p>Impossible de charger la carte d’identité pour le moment.</p></body></html>',
            503,
        )

    if not rows:
        abort(404)

    article = _article_row_to_public(rows[0])
    public_category = _as_text(article.get('categorie_metier')).strip().upper() or 'ARTICLE'
    public_entity_label = 'PACKING' if public_category == 'PACKING' else ('PRE-PACKING' if public_category == 'PRE-PACKING' else 'ARTICLE')
    raw = article.get('raw_json') if isinstance(article.get('raw_json'), dict) else {}
    raw = dict(raw or {})
    part_meta = _article_part_meta(article)
    if part_meta.get('parent_esi'):
        related_parts = _article_parts_for_parent(part_meta.get('parent_esi'), article.get('dossier'))
    else:
        related_parts = _article_parts_for_parent(article.get('esi_id'), article.get('dossier'))
    esc = lambda v: html.escape(_as_text(v).strip() or '-', quote=True)

    dims = ' × '.join(
        _as_text(article.get(k)).strip()
        for k in ('longueur_cm', 'largeur_cm', 'hauteur_cm')
        if _as_text(article.get(k)).strip()
    )
    dims = (dims + ' cm') if dims else '-'
    poids = _as_text(article.get('poids_kg')).strip()
    poids = (poids + ' kg') if poids else '-'

    oeuvre_dims = ' × '.join(
        _as_text(article.get(k)).strip()
        for k in ('oeuvre_longueur_cm', 'oeuvre_largeur_cm', 'oeuvre_hauteur_cm')
        if _as_text(article.get(k)).strip()
    )
    oeuvre_dims = (oeuvre_dims + ' cm') if oeuvre_dims else '-'
    oeuvre_poids = _as_text(article.get('oeuvre_poids_kg')).strip()
    oeuvre_poids = (oeuvre_poids + ' kg') if oeuvre_poids else '-'
    oeuvre_volume = _as_text(article.get('oeuvre_volume_m3')).strip()
    oeuvre_volume = (oeuvre_volume + ' m³') if oeuvre_volume else '-'
    oeuvre_surface = _as_text(article.get('oeuvre_surface_m2')).strip()
    oeuvre_surface = (oeuvre_surface + ' m²') if oeuvre_surface else '-'

    photo_html = '<div class="no-photo">Aucune photo enregistrée</div>'
    if _as_text(raw.get('photo_storage_path')).strip():
        photo_version = _as_text(raw.get('photo_updated_at') or article.get('updated_at')).strip()
        photo_url = f"/api/articles/{urllib.parse.quote(esi_id, safe='-')}/photo"
        if photo_version:
            photo_url += '?v=' + urllib.parse.quote(photo_version, safe='')
        photo_html = f'<img src="{html.escape(photo_url, quote=True)}" alt="Photo de l’article">'

    history = []
    for entry in raw.get('receptions') or []:
        if not isinstance(entry, dict):
            continue
        history.append({
            'reference': _as_text(entry.get('reception_ref')).strip(),
            'date': _as_text(entry.get('date')).strip(),
            'lieu': _as_text(entry.get('lieu_stockage')).strip(),
            'colis': ', '.join(_as_text(x).strip() for x in (entry.get('colis') or []) if _as_text(x).strip()),
            'type_colis': _normalise_colis_type(entry.get('type_colis')),
            'par': _as_text(entry.get('receptionne_par')).strip(),
        })

    history_html = ''
    for entry in reversed(history):
        date_txt = entry['date']
        if date_txt:
            try:
                date_txt = datetime.fromisoformat(date_txt.replace('Z', '+00:00')).strftime('%d/%m/%Y %H:%M')
            except Exception:
                pass
        history_html += f"""
          <div class="history-item">
            <strong>{esc(entry['reference'] or 'Réception')}</strong>
            <div>{esc(date_txt)}</div>
            <div>Stockage : {esc(entry['lieu'])} · Pre-Packing : {esc(_colis_display(entry['colis'], entry['type_colis']))}</div>
            {f'<div>Réceptionné par : {esc(entry["par"])}</div>' if entry['par'] else ''}
          </div>
        """
    if not history_html:
        history_html = '<div class="empty">Aucune réception enregistrée.</div>'

    composition_html = ''
    if part_meta.get('parent_esi'):
        composition_html = (
            '<section class="section"><div class="section-title">Composition de l’article</div><div class="grid">'
            f'<div class="field"><b>Partie</b><div>{esc(part_meta.get("partie_label"))}</div></div>'
            f'<div class="field"><b>Article principal</b><div>{esc(part_meta.get("parent_esi"))}</div></div>'
            '</div></section>'
        )
    elif related_parts:
        part_cards = []
        for part in related_parts:
            child_esi = _as_text(part.get('esi_id')).strip()
            child_url = _article_qr_url(child_esi)
            part_cards.append(
                f'<a class="part-card" href="{html.escape(child_url, quote=True)}">'
                f'<strong>Partie {esc(part.get("partie_label"))}</strong>'
                f'<span>{esc(child_esi)}</span>'
                f'<span>Pre-Packing : {esc(part.get("dernier_colis"))} · {esc(_display_prepacking_type(part.get("type_colis")))}</span>'
                '</a>'
            )
        composition_html = '<section class="section"><div class="section-title">Composition de l’article</div><div class="parts-list">' + ''.join(part_cards) + '</div></section>'

    page = f"""<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>{esc(esi_id)} - Carte d’identité {esc(public_entity_label)}</title>
<style>
:root{{--blue:#0f2f4f;--blue2:#16476f;--accent:#0284c7;--light:#eef8fd;--line:#cfe3ee;--text:#17324a;--muted:#60758a}}
*{{box-sizing:border-box}}
body{{margin:0;background:#f4f8fb;color:var(--text);font-family:Arial,Helvetica,sans-serif}}
.wrap{{max-width:860px;margin:0 auto;padding:18px}}
.hero{{background:linear-gradient(135deg,var(--blue),var(--blue2));color:#fff;border-radius:20px;padding:20px;box-shadow:0 10px 28px rgba(15,47,79,.18)}}
.hero-top{{display:flex;align-items:center;justify-content:space-between;gap:18px}}
.logo{{display:block;max-width:112px;max-height:52px;object-fit:contain;background:#fff;border-radius:8px;padding:4px}}
.brand{{font-size:11px;font-weight:900;letter-spacing:.09em;text-align:right;opacity:.9}}
h1{{font-size:30px;line-height:1.05;margin:16px 0 5px;overflow-wrap:anywhere}}
.desc{{font-size:14px;line-height:1.4;color:#dbeafe}}
.layout{{display:grid;grid-template-columns:minmax(0,1fr) 250px;gap:14px;margin-top:14px}}
.grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}}
.field{{background:#fff;border:1px solid var(--line);border-radius:14px;padding:12px}}
.field.wide{{grid-column:1/-1}}
.field b{{display:block;font-size:9px;text-transform:uppercase;color:var(--muted);letter-spacing:.04em;margin-bottom:5px}}
.field div{{font-size:14px;font-weight:700;overflow-wrap:anywhere;white-space:pre-wrap}}
.photo{{background:#fff;border:1px solid var(--line);border-radius:16px;min-height:250px;display:flex;align-items:center;justify-content:center;overflow:hidden}}
.photo img{{display:block;width:100%;height:100%;min-height:250px;object-fit:contain}}
.no-photo{{font-size:12px;color:var(--muted);text-align:center;padding:20px}}
.section{{margin-top:14px;background:#fff;border:1px solid var(--line);border-radius:16px;padding:14px}}
.section-title{{font-size:11px;font-weight:900;text-transform:uppercase;color:var(--accent);letter-spacing:.04em;margin-bottom:10px}}
.history{{display:grid;gap:8px}}
.history-item{{border-left:4px solid #0ea5e9;background:#f8fafc;border-radius:10px;padding:10px;font-size:12px;line-height:1.45;color:#334155}}
.history-item strong{{color:var(--blue)}}
.parts-list{{display:grid;gap:8px}}.part-card{{display:grid;gap:4px;text-decoration:none;color:var(--text);border:1px solid var(--line);border-radius:12px;background:#f8fafc;padding:11px}}.part-card strong{{color:var(--accent)}}.part-card span{{font-size:12px;color:#475569}}
.empty{{font-size:12px;color:var(--muted)}}
.foot{{font-size:11px;color:var(--muted);text-align:center;margin:20px 0 8px}}
@media(max-width:650px){{.wrap{{padding:12px}}.layout{{grid-template-columns:1fr}}.grid{{grid-template-columns:1fr}}.field.wide{{grid-column:auto}}h1{{font-size:27px}}.hero-top{{align-items:flex-start}}.logo{{max-width:92px}}}}
</style>
</head>
<body>
<div class="wrap">
  <header class="hero">
    <div class="hero-top">
      <img class="logo" src="/static/logo.png" alt="ESI" onerror="this.style.display='none'">
      <div class="brand">ESI TICKETS · CARTE D’IDENTITÉ {esc(public_entity_label)}</div>
    </div>
    <h1>{esc(article.get('reference') or article.get('esi_id') or esi_id)}</h1>
    <div class="desc">{esc(article.get('description'))}</div>
  </header>

  <div class="layout">
    <section class="grid">
      <div class="field"><b>N° ESI</b><div>{esc(article.get('esi_id'))}</div></div>
      <div class="field"><b>N° dossier</b><div>{esc(article.get('dossier'))}</div></div>
      {f'<div class="field"><b>Partie</b><div>{esc(part_meta.get("partie_label"))}</div></div><div class="field"><b>Article principal</b><div>{esc(part_meta.get("parent_esi"))}</div></div>' if part_meta.get('parent_esi') else ''}
      <div class="field"><b>Référence / inventaire</b><div>{esc(article.get('reference'))}</div></div>
      <div class="field"><b>Client</b><div>{esc(article.get('client'))}</div></div>
      <div class="field"><b>Chargé de projet</b><div>{esc(article.get('charge_projet'))}</div></div>
      <div class="field wide"><b>Projet / exposition</b><div>{esc(article.get('projet'))}</div></div>
      <div class="field wide"><b>Description / désignation</b><div>{esc(article.get('description'))}</div></div>
      <div class="field"><b>Dimensions</b><div>{esc(dims)}</div></div>
      <div class="field"><b>Poids</b><div>{esc(poids)}</div></div>
      <div class="field"><b>Stockage actuel</b><div>{esc(article.get('lieu_stockage'))}</div></div>
      <div class="field"><b>Statut logistique</b><div>{esc(article.get('statut_logistique'))}</div></div>
      <div class="field"><b>N° Pre-Packing</b><div>{esc(article.get('dernier_colis'))}</div></div>
      <div class="field"><b>Type de Pre-Packing</b><div>{esc(_display_prepacking_type(article.get('type_colis')))}</div></div>
      <div class="field"><b>N° Packing</b><div>{esc(article.get('ref_caisse'))}</div></div>
      <div class="field"><b>Dernière réception</b><div>{esc(article.get('derniere_reception_ref'))}</div></div>
    </section>
    <aside class="photo">{photo_html}</aside>
  </div>

  {composition_html}

  <section class="section">
    <div class="section-title">Informations de l’œuvre</div>
    <div class="grid">
      <div class="field"><b>N° réf œuvre</b><div>{esc(article.get('oeuvre_reference'))}</div></div>
      <div class="field"><b>Nom de l’artiste</b><div>{esc(article.get('artiste'))}</div></div>
      <div class="field"><b>Titre de l’œuvre</b><div>{esc(article.get('oeuvre_titre'))}</div></div>
      <div class="field"><b>Technique de l’œuvre</b><div>{esc(article.get('oeuvre_technique'))}</div></div>
      <div class="field"><b>Dimensions de l’œuvre</b><div>{esc(oeuvre_dims)}</div></div>
      <div class="field"><b>Poids de l’œuvre</b><div>{esc(oeuvre_poids)}</div></div>
      <div class="field"><b>Volume</b><div>{esc(oeuvre_volume)}</div></div>
      <div class="field"><b>Surface au sol</b><div>{esc(oeuvre_surface)}</div></div>
      <div class="field"><b>Statut douanier</b><div>{esc(article.get('statut_douanier'))}</div></div>
      <div class="field"><b>N° IMA</b><div>{esc(article.get('ima_numero'))}</div></div>
      <div class="field"><b>Date de l’IMA</b><div>{esc(article.get('ima_date'))}</div></div>
    </div>
  </section>

  <section class="section">
    <div class="section-title">Historique des réceptions</div>
    <div class="history">{history_html}</div>
  </section>
  <div class="foot">Page en lecture seule · ESI Tickets</div>
</div>
</body>
</html>"""
    response = app.make_response(page)
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['X-Robots-Tag'] = 'noindex, nofollow'
    return response



def _article_import_text(value):
    """Normalise une valeur Excel sans transformer 12 en '12.0'."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return (f"{value:.10f}").rstrip("0").rstrip(".")
    return re.sub(r"\s+", " ", _as_text(value).replace("\u00a0", " ")).strip()


def _article_import_norm(value):
    """Valeur canonique utilisée uniquement pour la détection de doublons."""
    text = _article_import_text(value).casefold().strip()
    text = re.sub(r"\s+", " ", text)
    return text


def _article_import_date(value):
    """Normalise une date Excel/texte au format YYYY-MM-DD pour la fiche article."""
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.date().isoformat()
    text = _article_import_text(value)
    if not text:
        return ""
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except Exception:
            pass
    # Une cellule Excel peut parfois ressortir avec une heure à minuit.
    try:
        return datetime.fromisoformat(text).date().isoformat()
    except Exception:
        return text


def _article_import_customs_status(value):
    """Uniformise le statut douanier tout en conservant une valeur inhabituelle saisie par l'utilisateur."""
    text = _article_import_text(value)
    norm = re.sub(r"\s+", " ", text.casefold()).strip()
    if norm == "libre":
        return "Libre"
    if norm in {"sous douane", "sous-douane", "sousdouane"}:
        return "Sous douane"
    return text


def _article_import_signature(values):
    """Signature métier d'un article importé pour éviter les créations en double."""
    return "|".join([
        _article_import_norm(values.get("reference")),
        _article_import_norm(values.get("description")),
        _article_import_norm(values.get("longueur_cm")),
        _article_import_norm(values.get("largeur_cm")),
        _article_import_norm(values.get("hauteur_cm")),
        _article_import_norm(values.get("poids_kg")),
    ])


def _article_import_headers(header_values):
    """Repère les colonnes du format Excel normalisé ESI TICKETS à partir de la première ligne.

    Les index retournés sont basés sur 0 afin d'être utilisés directement sur les tuples
    produits par ``iter_rows(values_only=True)``. Cela évite les appels répétés à ``ws.cell``
    qui sont très coûteux avec une feuille OpenPyXL ouverte en mode read_only.
    """
    aliases = {
        "quantite": {"QUANTITE", "QTE", "QTY"},
        "longueur_cm": {"LONGUEUR_CM", "LONGUEUR (CM)", "LONGUEUR"},
        "largeur_cm": {"LARGEUR_CM", "LARGEUR (CM)", "LARGEUR"},
        "hauteur_cm": {"HAUTEUR_CM", "HAUTEUR (CM)", "HAUTEUR"},
        "volume_m3": {"VOLUME_M3", "VOLUME (M3)", "VOLUME (M³)", "VOLUME"},
        "surface_m2": {"SURFACE_M2", "SURFACE (M2)", "SURFACE (M²)", "SURFACE AU SOL"},
        "poids_kg": {"POIDS_BRUT_KG", "POIDS_KG", "POIDS (KG)", "POIDS"},
        "reference": {"REFERENCE_PINTO", "REFERENCE", "REF", "REF ARTICLE"},
        "description": {"DESIGNATION", "DESCRIPTION"},
        "oeuvre_reference": {"OEUVRE_REFERENCE", "REFERENCE_OEUVRE", "REF_OEUVRE", "N° REF OEUVRE", "N° RÉF ŒUVRE"},
        "artiste": {"ARTISTE", "NOM_ARTISTE", "NOM DE L'ARTISTE", "NOM DE L’ARTISTE"},
        "oeuvre_titre": {"OEUVRE_TITRE", "TITRE_OEUVRE", "TITRE DE L'OEUVRE", "TITRE DE L’ŒUVRE"},
        "oeuvre_technique": {"OEUVRE_TECHNIQUE", "TECHNIQUE_OEUVRE", "TECHNIQUE DE L'OEUVRE", "TECHNIQUE DE L’ŒUVRE"},
        "oeuvre_longueur_cm": {"OEUVRE_LONGUEUR_CM", "LONGUEUR_OEUVRE_CM", "LONGUEUR OEUVRE (CM)"},
        "oeuvre_largeur_cm": {"OEUVRE_LARGEUR_CM", "LARGEUR_OEUVRE_CM", "LARGEUR OEUVRE (CM)"},
        "oeuvre_hauteur_cm": {"OEUVRE_HAUTEUR_CM", "HAUTEUR_OEUVRE_CM", "HAUTEUR OEUVRE (CM)"},
        "oeuvre_volume_m3": {"OEUVRE_VOLUME_M3", "VOLUME_OEUVRE_M3", "VOLUME OEUVRE (M3)", "VOLUME OEUVRE (M³)"},
        "oeuvre_surface_m2": {"OEUVRE_SURFACE_M2", "SURFACE_OEUVRE_M2", "SURFACE OEUVRE (M2)", "SURFACE OEUVRE (M²)"},
        "oeuvre_poids_kg": {"OEUVRE_POIDS_KG", "POIDS_OEUVRE_KG", "POIDS OEUVRE (KG)"},
        "statut_douanier": {"STATUT_DOUANIER", "STATUT DOUANIER", "DOUANE"},
        "ima_numero": {"IMA_NUMERO", "NUMERO_IMA", "N° IMA", "NO IMA"},
        "ima_date": {"IMA_DATE", "DATE_IMA", "DATE IMA"},
    }

    found = {}
    for index, value in enumerate(header_values or ()):
        raw = _article_import_text(value)
        header = re.sub(r"\s+", " ", raw.upper()).strip()
        if not header:
            continue
        for field, names in aliases.items():
            if header in names and field not in found:
                found[field] = index
                break
    return found


def _article_import_existing_counts():
    """Compte les articles existants par signature métier."""
    rows = supabase_rest_request(
        "GET",
        "articles",
        "select=reference,description,longueur_cm,largeur_cm,hauteur_cm,poids_kg&limit=10000"
    ) or []
    counts = {}
    for row in rows:
        sig = _article_import_signature(row)
        if sig.strip("|"):
            counts[sig] = counts.get(sig, 0) + 1
    return counts


@app.route('/api/articles/import-excel', methods=['POST'])
def api_articles_import_excel():
    """
    Importe le fichier Excel normalisé ESI TICKETS.

    - 1 unité physique = 1 numéro ESI unique ;
    - la colonne QUANTITE peut donc créer plusieurs ESI pour une même ligne ;
    - les doublons sont contrôlés par référence + désignation + dimensions + poids ;
    - les champs œuvre / douane / IMA de la trame sont enregistrés dans la fiche article ;
    - volumes et surfaces sont recalculés automatiquement à partir des dimensions en cm ;
    - les photos restent ajoutées directement depuis la fiche article dans ESI TICKETS.
    """
    fs = request.files.get('file')
    if not fs or not fs.filename:
        return jsonify({'ok': False, 'error': 'Fichier Excel manquant'}), 400

    filename = _as_text(fs.filename).strip()
    dossier = _as_text(request.form.get('dossier')).strip()
    if not dossier:
        return jsonify({'ok': False, 'error': 'Le N° de dossier est obligatoire pour importer les articles.'}), 400

    if not filename.lower().endswith('.xlsx'):
        return jsonify({'ok': False, 'error': 'Le fichier doit être au format .xlsx'}), 400

    content = fs.read()
    if not content:
        return jsonify({'ok': False, 'error': 'Le fichier Excel est vide'}), 400

    try:
        from openpyxl import load_workbook
        wb = load_workbook(BytesIO(content), read_only=True, data_only=True)
        ws = wb.active
    except Exception as e:
        return jsonify({'ok': False, 'error': f'Fichier Excel illisible : {e}'}), 400

    # Lecture séquentielle de la feuille : indispensable en mode read_only.
    # Un accès répété avec ws.cell(...) force OpenPyXL à reparcourir le flux XML et peut
    # provoquer des délais très importants sur Render, même avec une trame de quelques
    # centaines de lignes préformatées.
    row_iter = ws.iter_rows(values_only=True)
    try:
        header_values = next(row_iter)
    except StopIteration:
        try:
            wb.close()
        except Exception:
            pass
        return jsonify({'ok': False, 'error': 'Le fichier Excel ne contient aucune ligne.'}), 400

    headers = _article_import_headers(header_values)
    if 'reference' not in headers and 'description' not in headers:
        try:
            wb.close()
        except Exception:
            pass
        return jsonify({
            'ok': False,
            'error': "Colonnes non reconnues. Le fichier doit contenir au minimum REFERENCE_PINTO/REFERENCE ou DESIGNATION/DESCRIPTION."
        }), 400

    stats = {
        'lignes_lues': 0,
        'articles_demandes': 0,
        'articles_crees': 0,
        'doublons_ignores': 0,
        'lignes_ignorees': 0,
        'errors': [],
        'esi_ids': [],
    }

    try:
        existing_counts = _article_import_existing_counts()
    except Exception as e:
        return jsonify({'ok': False, 'error': f'Impossible de vérifier les doublons : {e}'}), 500

    now = datetime.now().isoformat()

    # Si ce dossier existe déjà, tous les nouveaux articles reprennent automatiquement
    # le même Client et le même Projet.
    try:
        dossier_identity = _article_dossier_identity(dossier)
    except Exception as e:
        return jsonify({'ok': False, 'error': f'Impossible de lire les informations du dossier : {e}'}), 500

    with _ARTICLE_LOCK:
        for row_num, row_values in enumerate(row_iter, start=2):
            def cell(field):
                index = headers.get(field)
                if index is None or index >= len(row_values):
                    return None
                return row_values[index]

            values = {
                'reference': _article_import_text(cell('reference')),
                'description': _article_import_text(cell('description')),
                'longueur_cm': _article_import_text(cell('longueur_cm')),
                'largeur_cm': _article_import_text(cell('largeur_cm')),
                'hauteur_cm': _article_import_text(cell('hauteur_cm')),
                'volume_m3': _article_import_text(cell('volume_m3')),
                'surface_m2': _article_import_text(cell('surface_m2')),
                'poids_kg': _article_import_text(cell('poids_kg')),
                'oeuvre_reference': _article_import_text(cell('oeuvre_reference')),
                'artiste': _article_import_text(cell('artiste')),
                'oeuvre_titre': _article_import_text(cell('oeuvre_titre')),
                'oeuvre_technique': _article_import_text(cell('oeuvre_technique')),
                'oeuvre_longueur_cm': _article_import_text(cell('oeuvre_longueur_cm')),
                'oeuvre_largeur_cm': _article_import_text(cell('oeuvre_largeur_cm')),
                'oeuvre_hauteur_cm': _article_import_text(cell('oeuvre_hauteur_cm')),
                'oeuvre_volume_m3': _article_import_text(cell('oeuvre_volume_m3')),
                'oeuvre_surface_m2': _article_import_text(cell('oeuvre_surface_m2')),
                'oeuvre_poids_kg': _article_import_text(cell('oeuvre_poids_kg')),
                'statut_douanier': _article_import_customs_status(cell('statut_douanier')),
                'ima_numero': _article_import_text(cell('ima_numero')),
                'ima_date': _article_import_date(cell('ima_date')),
            }

            # Les valeurs calculées sont recalculées côté ESI TICKETS à l'import.
            # Cela rend la trame fiable même si Excel n'a pas encore enregistré le cache des formules.
            volume_calc, surface_calc = _article_calculated_metrics(
                values['longueur_cm'], values['largeur_cm'], values['hauteur_cm']
            )
            if volume_calc:
                values['volume_m3'] = volume_calc
            if surface_calc:
                values['surface_m2'] = surface_calc

            oeuvre_volume_calc, oeuvre_surface_calc = _article_calculated_metrics(
                values['oeuvre_longueur_cm'], values['oeuvre_largeur_cm'], values['oeuvre_hauteur_cm']
            )
            if oeuvre_volume_calc:
                values['oeuvre_volume_m3'] = oeuvre_volume_calc
            if oeuvre_surface_calc:
                values['oeuvre_surface_m2'] = oeuvre_surface_calc

            # Une ligne totalement vide n'est pas une erreur.
            if not any(values.values()) and not _article_import_text(cell('quantite')):
                continue

            stats['lignes_lues'] += 1

            if not values['reference'] and not values['description']:
                stats['lignes_ignorees'] += 1
                stats['errors'].append({
                    'ligne': row_num,
                    'error': 'Référence et désignation toutes les deux vides'
                })
                continue

            qty = _article_quantity(cell('quantite'), 1)
            stats['articles_demandes'] += qty

            signature = _article_import_signature(values)
            already = existing_counts.get(signature, 0)
            to_create = max(0, qty - already)
            skipped = qty - to_create
            stats['doublons_ignores'] += skipped

            created_for_row = 0
            for unit_index in range(already + 1, already + to_create + 1):
                payload = {
                    # Un import Excel dans la base Articles n'est pas rattache a un ticket.
                    # ticket_id reste a NULL : en PostgreSQL, la contrainte UNIQUE
                    # (ticket_id, source_index, unit_index) ne bloque pas plusieurs lignes
                    # lorsque ticket_id est NULL. source_index et unit_index restent renseignes
                    # car la table impose notamment unit_index NOT NULL.
                    'ticket_id': None,
                    'source_module': 'Import Excel',
                    'source_index': row_num,
                    'unit_index': unit_index,
                    'reference': values['reference'],
                    'description': values['description'],
                    'dossier': dossier,
                    'client': dossier_identity.get('client', ''),
                    'projet': dossier_identity.get('projet', ''),
                    'ref_caisse': '',
                    'transporteur_ref': '',
                    'longueur_cm': values['longueur_cm'],
                    'largeur_cm': values['largeur_cm'],
                    'hauteur_cm': values['hauteur_cm'],
                    'volume_m3': values['volume_m3'],
                    'surface_m2': values['surface_m2'],
                    'poids_kg': values['poids_kg'],
                    'lieu_stockage': '',
                    'statut_logistique': 'Créé',
                    'created_at': now,
                    'updated_at': now,
                    'raw_json': {
                        'source': 'import_excel',
                        'filename': filename,
                        'dossier': dossier,
                        'excel_row': row_num,
                        'quantity_source': qty,
                        'unit_index': unit_index,
                        'article_fields': {
                            'charge_projet': _as_text(dossier_identity.get('charge_projet')).strip(),
                            'oeuvre_reference': values['oeuvre_reference'],
                            'artiste': values['artiste'],
                            'oeuvre_titre': values['oeuvre_titre'],
                            'oeuvre_technique': values['oeuvre_technique'],
                            'oeuvre_longueur_cm': values['oeuvre_longueur_cm'],
                            'oeuvre_largeur_cm': values['oeuvre_largeur_cm'],
                            'oeuvre_hauteur_cm': values['oeuvre_hauteur_cm'],
                            'oeuvre_volume_m3': values['oeuvre_volume_m3'],
                            'oeuvre_surface_m2': values['oeuvre_surface_m2'],
                            'oeuvre_poids_kg': values['oeuvre_poids_kg'],
                            'statut_douanier': values['statut_douanier'],
                            'ima_numero': values['ima_numero'],
                            'ima_date': values['ima_date'],
                        },
                    },
                }
                payload['search_text'] = _article_search_text(payload)

                try:
                    article = _create_article_record(payload)
                    stats['articles_crees'] += 1
                    created_for_row += 1
                    if article.get('esi_id'):
                        stats['esi_ids'].append(article['esi_id'])
                except Exception as e:
                    stats['errors'].append({'ligne': row_num, 'error': str(e)})
                    break

            # Seules les créations réellement réussies deviennent des doublons pour les lignes suivantes.
            existing_counts[signature] = already + created_for_row

    try:
        wb.close()
    except Exception:
        pass

    # Uniformise également les anciens articles du dossier si une identité Client/Projet existe.
    try:
        if dossier_identity.get('client') or dossier_identity.get('projet'):
            _article_sync_dossier_identity(
                dossier, dossier_identity.get('client') or None, dossier_identity.get('projet') or None
            )
    except Exception as e:
        stats['errors'].append({'ligne': 0, 'error': f'Synchronisation dossier : {e}'})

    return jsonify({
        'ok': len(stats['errors']) == 0,
        'dossier': dossier,
        **stats,
    }), (200 if not stats['errors'] else 207)


@app.route('/api/articles/migrate', methods=['POST'])
def api_articles_migrate():
    """Synchronise Articles, Pre-Packings déjà créés et Packings historiques vers la Base Articles."""
    stats = {
        "tickets": 0, "articles_crees": 0,
        "packings_traites": 0, "packings_crees": 0, "packings_mis_a_jour": 0,
        "errors": []
    }
    try:
        tickets = list_tickets()
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

    for ticket in tickets:
        module = _as_text(ticket.get("module")).replace("’", "'").strip()
        if module not in ("Avis d'arrivée", "Demande d'enlèvement", "Demande d'enlevement"):
            continue
        try:
            created = _ensure_articles_for_ticket(ticket, save=True)
            stats["tickets"] += 1
            stats["articles_crees"] += len(created)
        except Exception as e:
            stats["errors"].append({"ticket_id": ticket.get("id"), "error": str(e)})

    packing_stats = _sync_existing_packings_to_articles(tickets)
    for key in ("packings_traites", "packings_crees", "packings_mis_a_jour"):
        stats[key] = packing_stats.get(key, 0)
    stats["errors"].extend(packing_stats.get("errors") or [])

    return jsonify({
        "ok": not stats["errors"],
        **stats
    }), (200 if not stats["errors"] else 207)


@app.route('/api/articles/sync-packings', methods=['POST'])
def api_articles_sync_packings():
    """Synchronisation idempotente des fiches Packing existantes vers la Base Articles."""
    try:
        stats = _sync_existing_packings_to_articles()
        return jsonify({'ok': not stats['errors'], **stats}), (200 if not stats['errors'] else 207)
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


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
    #
    # Selon la reponse PostgREST / le type reel de la colonne Supabase, raw_json
    # peut arriver soit deja decode en dict, soit sous forme de chaine JSON.
    raw = row.get("raw_json")
    if isinstance(raw, dict):
        ticket = dict(raw)
    elif isinstance(raw, str) and raw.strip():
        try:
            decoded = json.loads(raw)
            ticket = dict(decoded) if isinstance(decoded, dict) else {}
        except (json.JSONDecodeError, TypeError, ValueError):
            print(f"[SUPABASE DB] raw_json invalide pour ticket {row.get('id')}: {type(raw).__name__}")
            ticket = {}
    else:
        ticket = {}

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



_RECEPTION_MIGRATION_LOCK = threading.Lock()
_RECEPTION_MIGRATION_DONE = False


def _is_caisse_receptionnee(ticket):
    """
    Retourne True si la caisse a été réceptionnée.
    La réception est une information logistique indépendante du statut du ticket.
    """
    reception = ticket.get('reception') or {}
    if reception.get('receptionnee') is True:
        return True

    # Compatibilité avec les réceptions déjà enregistrées avant l'ajout du booléen.
    if reception.get('receptionnee_le'):
        return True

    return False


def _ticket_reception_validee(ticket):
    """
    Indicateur unique pour le planning réception.

    Retourne True dès qu'au moins une réception active a été validée, y compris
    pour les anciens tickets qui ne possèdent pas encore les listes modernes
    receptionsAvisArrivee / bons_livraison mais portent l'état sur les articles.
    """
    module = _as_text(ticket.get('module')).replace("’", "'").strip()
    ticket_id = _as_text(ticket.get('id')).strip()

    is_avis = module == "Avis d'arrivée" or ticket_id.startswith('ARR-')
    is_enlevement = (
        module in ("Demande d'enlèvement", "Demande d'enlevement")
        or ticket_id.startswith('ENL-')
    )

    if is_avis:
        # Structure actuelle : chaque validation crée une réception active.
        for reception in ticket.get('receptionsAvisArrivee') or []:
            if isinstance(reception, dict) and not reception.get('annulee'):
                return True

        # Compatibilité avec l'ancien champ de synthèse.
        last = ticket.get('receptionAvisArrivee')
        if isinstance(last, dict) and not last.get('annulee'):
            if (
                last.get('receptionnee') is True
                or last.get('reference')
                or last.get('receptionnee_le')
                or last.get('date_reception')
            ):
                return True

        container = ticket.get('avisArrivee') or ticket.get('avis_arrivee') or {}
        item_list = container.get('items') or []

    elif is_enlevement:
        container = ticket.get('enlevement') or {}
        for reception in container.get('bons_livraison') or []:
            if isinstance(reception, dict) and not reception.get('annulee'):
                return True
        item_list = container.get('items') or []

    else:
        return False

    # Secours pour l'historique : les anciennes validations peuvent n'avoir
    # enregistré que l'état de réception directement dans les lignes articles.
    for item in item_list:
        if not isinstance(item, dict):
            continue
        if item.get('receptionne') is True:
            return True
        try:
            qty = int(float(str(item.get('quantite_recue_totale') or 0).replace(',', '.')))
        except Exception:
            qty = 0
        if qty > 0 or item.get('receptionne_le'):
            return True
        if any(isinstance(r, dict) for r in (item.get('receptions') or [])):
            return True

    return False


def migrate_caisses_avant_18_aout_2026():
    """
    Corrige l'ancienne logique qui utilisait 'Réceptionnée' comme statut de ticket.

    Règles :
      - les vrais statuts de fiche de caisse restent Demande créée / En cours / Terminé ;
      - toute fiche de caisse anciennement mise au statut 'Réceptionnée' repasse à 'Terminé' ;
      - les fiches terminées dont la date de mise à dispo est strictement antérieure
        au 18/08/2026 sont marquées comme réceptionnées dans ticket['reception'],
        sans modifier leur statut métier.
    """
    global _RECEPTION_MIGRATION_DONE

    with _RECEPTION_MIGRATION_LOCK:
        if _RECEPTION_MIGRATION_DONE:
            return

        cutoff = datetime(2026, 8, 18)
        updated = 0
        repaired_status = 0
        errors = 0

        try:
            all_tickets = list_tickets()
        except Exception as e:
            print(f"[RECEPTION MIGRATION] Lecture des tickets impossible : {e}")
            return

        for ticket in all_tickets:
            try:
                if ticket.get('module') != 'Fiche de caisse':
                    continue

                changed = False
                current_status = _as_text(ticket.get('status')).strip()

                # Répare les tickets affectés par l'ancienne logique.
                if current_status == 'Réceptionnée':
                    ticket['status'] = 'Terminé'
                    current_status = 'Terminé'
                    repaired_status += 1
                    changed = True

                raw_date = _as_text(ticket.get('dateEmballage')).strip()
                date_emballage = None
                if raw_date and raw_date != '-':
                    try:
                        date_emballage = datetime.fromisoformat(raw_date[:10])
                    except Exception:
                        date_emballage = None

                # Historique demandé : uniquement les caisses terminées avant le 18/08/2026.
                if (
                    current_status == 'Terminé'
                    and date_emballage is not None
                    and date_emballage < cutoff
                ):
                    reception = dict(ticket.get('reception') or {})
                    if not reception.get('receptionnee'):
                        reception['receptionnee'] = True
                        reception.setdefault('receptionnee_le', date_emballage.isoformat())
                        reception.setdefault('mode', 'migration_2026_08_18')
                        ticket['reception'] = reception
                        changed = True

                if not changed:
                    continue

                ticket['updatedAt'] = datetime.now().isoformat()

                supabase_rest_request(
                    "PATCH",
                    "tickets",
                    "id=eq." + urllib.parse.quote(ticket.get('id'), safe=''),
                    {
                        "status": ticket['status'],
                        "updated_at": ticket['updatedAt'],
                        "raw_json": ticket
                    },
                    prefer="return=minimal"
                )
                updated += 1

            except Exception as e:
                errors += 1
                print(f"[RECEPTION MIGRATION] Erreur {ticket.get('id')}: {e}")

        _RECEPTION_MIGRATION_DONE = True
        print(
            f"[RECEPTION MIGRATION] {updated} ticket(s) corrigé(s), "
            f"{repaired_status} statut(s) Réceptionnée -> Terminé, "
            f"{errors} erreur(s)"
        )


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

    # Toute fiche Packing possède également sa propre ligne (et son N° ESI)
    # dans la Base Articles. L'opération est idempotente et met à jour la ligne existante.
    if _as_text(ticket.get('module')).strip() == 'Fiche de caisse':
        try:
            _ensure_packing_article_record(ticket)
        except Exception as e:
            print(f"[PACKING BASE ARTICLES] Synchronisation impossible pour {ticket_id}: {e}")

    print("[SUPABASE DB] Ticket sauvegardé", ticket_id)


def load_ticket(ticket_id):
    safe_tid = urllib.parse.quote(ticket_id, safe='')
    rows = supabase_rest_request("GET", "tickets", f"select=*&id=eq.{safe_tid}&limit=1") or []
    if not rows:
        return None
    return _attach_children(_ticket_from_db_row(rows[0]))


def _unlink_articles_from_deleted_caisse(ticket):
    """Retire la référence de caisse des articles liés avant suppression d'une fiche de caisse."""
    if _as_text(ticket.get('module')).strip() != 'Fiche de caisse':
        return

    dossier = _as_text(ticket.get('dossier')).strip()
    numero_brut = _as_text(ticket.get('ref')).strip()
    if not dossier or not numero_brut:
        return

    try:
        numero_norm = _normalise_numero_caisse(numero_brut)
    except Exception:
        numero_norm = numero_brut
    numero_caisse = numero_norm.zfill(2) if _as_text(numero_norm).isdigit() else numero_brut
    equivalent_refs = {
        _packing_reference(dossier, numero_brut),
        _legacy_packing_reference(dossier, numero_brut),
        f"{dossier}-{numero_norm}",
    }

    ids = []
    for item in ticket.get('articles_lies') or []:
        esi_id = _as_text(item.get('esi_id') if isinstance(item, dict) else item).strip()
        if esi_id and esi_id not in ids:
            ids.append(esi_id)

    for esi_id in ids:
        safe_esi = urllib.parse.quote(esi_id, safe='-')
        rows = supabase_rest_request('GET', 'articles', f'select=*&esi_id=eq.{safe_esi}&limit=1') or []
        if not rows:
            continue
        current = dict(rows[0])
        patch = {}
        if _as_text(current.get('ref_caisse')).strip() in equivalent_refs:
            patch['ref_caisse'] = ''
        # Le Pre-Packing est indépendant du Packing : il n'est pas effacé avec la fiche Packing.
        if not patch:
            continue
        patch['updated_at'] = datetime.now().isoformat()
        merged = dict(current)
        merged.update(patch)
        patch['search_text'] = _article_search_text(merged)
        supabase_rest_request('PATCH', 'articles', f'esi_id=eq.{safe_esi}', patch, prefer='return=minimal')


def delete_ticket_permanently(ticket_id):
    """Supprime définitivement un ticket et ses enfants techniques sans casser l'historique Articles."""
    ticket = load_ticket(ticket_id)
    if not ticket:
        return False, []

    safe_tid = urllib.parse.quote(ticket_id, safe='')

    # Les tickets de réception/enlèvement peuvent être à l'origine d'articles physiques.
    # On protège cet historique : ces tickets ne sont pas supprimés tant que des articles
    # portent leur ticket_id. Les tickets classiques restent supprimables normalement.
    linked_articles = supabase_rest_request(
        'GET', 'articles', f'select=*&ticket_id=eq.{safe_tid}&limit=50'
    ) or []
    protected_links = []
    for row in linked_articles:
        public = _article_row_to_public(row)
        if public.get('categorie_metier') != 'PACKING':
            protected_links.append(public)
    if protected_links:
        ids = ', '.join(_as_text(x.get('esi_id')).strip() for x in protected_links if x.get('esi_id'))
        detail = f" ({ids})" if ids else ''
        raise ValueError(
            "Ce ticket est lié à des articles ESI et ne peut pas être supprimé directement" + detail + ". "
            "Utilise l'annulation pour conserver l'historique logistique."
        )

    _unlink_articles_from_deleted_caisse(ticket)
    _delete_packing_article_record(ticket)

    warnings = []
    file_rows = supabase_rest_request(
        'GET', 'ticket_files', f'select=storage_path&ticket_id=eq.{safe_tid}'
    ) or []

    # Supprime d'abord les lignes enfants afin d'éviter une contrainte de clé étrangère.
    supabase_rest_request('DELETE', 'fiches', f'ticket_id=eq.{safe_tid}', prefer='return=minimal')
    supabase_rest_request('DELETE', 'ticket_files', f'ticket_id=eq.{safe_tid}', prefer='return=minimal')
    supabase_rest_request('DELETE', 'tickets', f'id=eq.{safe_tid}', prefer='return=minimal')

    # Nettoyage des fichiers après disparition du ticket. Une erreur de stockage n'annule
    # pas la suppression métier ; elle est simplement signalée dans les warnings.
    for row in file_rows:
        storage_path = _as_text(row.get('storage_path')).strip()
        if not storage_path:
            continue
        try:
            supabase_delete_object(storage_path)
        except Exception as e:
            warnings.append(f"Fichier non supprimé du stockage : {storage_path} ({e})")

    local_folder = files_dir() / ticket_id
    if local_folder.exists():
        try:
            shutil.rmtree(local_folder)
        except Exception as e:
            warnings.append(f"Dossier local non supprimé : {e}")

    remaining = supabase_rest_request('GET', 'tickets', f'select=id&id=eq.{safe_tid}&limit=1') or []
    if remaining:
        raise RuntimeError('Le ticket est encore présent dans Supabase après la suppression')

    return True, warnings


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

    native_text = "\n".join(pages_text).strip()
    text = native_text
    ocr_used = False

    if len(native_text) < 120:
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
                    pdf_bytes,
                    dpi=300,
                    grayscale=True,
                    thread_count=1
                )
            except Exception as e:
                raise RuntimeError(
                    "Impossible de convertir le PDF en image pour l'OCR."
                ) from e

            ocr_pages = []
            total_pages = len(images)

            for index, image in enumerate(images, start=1):
                try:
                    # PSM 4 respecte mieux les blocs/colonnes du bon interne.
                    page_text = pytesseract.image_to_string(
                        image,
                        lang="fra",
                        config="--psm 4"
                    )
                    # Secours pour les scans atypiques.
                    if len((page_text or "").strip()) < 120:
                        page_text = pytesseract.image_to_string(
                            image,
                            lang="fra",
                            config="--psm 11"
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

    print(f"[ENLEVEMENT OCR] {len(text)} caracteres exploitables")
    return text, len(reader.pages), ocr_used


def _clean_ocr_line(value):
    value = _as_text(value)
    value = value.replace("\u00a0", " ")
    value = value.replace("–", "-").replace("—", "-")
    value = value.replace("：", ":")
    return re.sub(r"\s+", " ", value).strip(" \t|")


def _enlevement_lines(text):
    return [
        _clean_ocr_line(line)
        for line in _as_text(text).replace("\r", "").splitlines()
        if _clean_ocr_line(line)
    ]


def _value_after_label(lines, labels, stop_labels=None):
    stop_labels = stop_labels or []
    all_labels = list(labels) + list(stop_labels)

    for i, line in enumerate(lines):
        for label in labels:
            m = re.search(label, line, re.I)
            if not m:
                continue

            rest = line[m.end():].strip(" :-|")
            if rest:
                cuts = []
                for other in all_labels:
                    mo = re.search(other, rest, re.I)
                    if mo and mo.start() > 0:
                        cuts.append(mo.start())
                if cuts:
                    rest = rest[:min(cuts)].strip(" :-|")
                if rest:
                    return rest

            for offset in (1, 2):
                if i + offset < len(lines):
                    candidate = lines[i + offset].strip()
                    if candidate and not any(re.search(x, candidate, re.I) for x in all_labels):
                        return candidate

    return ""


def _normalise_enlevement_date(value):
    value = _clean_ocr_line(value)
    m = re.search(r"\b([0-3]?\d)[/.\-]([01]?\d)[/.\-](\d{2}|\d{4})\b", value)
    if not m:
        return ""
    day = int(m.group(1))
    month = int(m.group(2))
    year = int(m.group(3))
    if year < 100:
        year += 2000
    try:
        return datetime(year, month, day).strftime("%d/%m/%Y")
    except ValueError:
        return ""



def _extract_enlevement_items(instructions_text):
    """
    Extrait les articles de la zone Instructions avec une logique générique.

    Objectif :
      - ne plus dépendre d'un format précis de référence ;
      - accepter LDV_1047, ABC-123, 750012MW02, 960130M 04, etc. ;
      - utiliser le reste de la ligne comme désignation ;
      - éviter les faux positifs (dates, téléphones, dimensions, phrases générales).
    """
    lines = _enlevement_lines(instructions_text)
    items = []
    seen_refs = set()

    noise_patterns = [
        r"^\s*$",
        r"^\s*(?:merci|rappel|vous\s+pouvez|merci\s+de|storage|instruction|assur[eé]|valeur|observation|service)\b",
        r"^\s*(?:date|heure|notes?|assign[eé]|v[eé]hicules?)\s*:?\s*$",
    ]

    def is_noise(line):
        return any(re.search(p, line, re.I) for p in noise_patterns)

    def is_date_like(value):
        return bool(re.fullmatch(r"[0-3]?\d[/.\-][01]?\d[/.\-](?:\d{2}|\d{4})", value.strip()))

    def is_phone_like(value):
        compact = re.sub(r"[\s.\-()]+", "", value)
        return bool(re.fullmatch(r"(?:\+33|0)\d{9,10}", compact))

    def is_dimension_like(value):
        return bool(re.fullmatch(
            r"\d+(?:[.,]\d+)?\s*[xX×]\s*\d+(?:[.,]\d+)?"
            r"(?:\s*[xX×]\s*\d+(?:[.,]\d+)?)?\s*(?:cm|mm|m)?",
            value.strip(),
            re.I
        ))

    def normalise_reference(ref):
        ref = _clean_ocr_line(ref).strip(" :;,.|")
        ref = re.sub(r"\s+", " ", ref)

        # Cas fréquent OCR : "960130M 04" -> "960130M04".
        # On fusionne uniquement si les deux blocs ressemblent à une même référence.
        parts = ref.split()
        if len(parts) == 2:
            a, b = parts
            if (
                re.search(r"[A-Za-z]", a)
                and re.search(r"\d", a)
                and re.fullmatch(r"[A-Za-z0-9]{1,4}", b)
            ):
                ref = a + b

        # Les références métier ne doivent pas garder les espaces internes.
        ref = ref.replace(" ", "")
        ref = re.sub(r"^[^A-Za-z0-9]+|[^A-Za-z0-9_-]+$", "", ref)
        return ref

    def extract_dimensions(line):
        m = re.search(
            r"(?:Dims?\.?\s*[:.-]?\s*)?"
            r"(\d+(?:[.,]\d+)?\s*[xX×]\s*\d+(?:[.,]\d+)?"
            r"(?:\s*[xX×]\s*\d+(?:[.,]\d+)?)?\s*(?:cm|mm|m)?)",
            line,
            re.I
        )
        return _clean_ocr_line(m.group(1)) if m else ""

    def add_item(reference, description="", dimensions="", qty=""):
        ref = normalise_reference(reference)
        if len(ref) < 3:
            return
        if is_date_like(ref) or is_phone_like(ref) or is_dimension_like(ref):
            return

        # Une vraie référence doit contenir au moins une lettre et un chiffre,
        # ou bien comporter un séparateur structurant (_ / -).
        if not (
            (re.search(r"[A-Za-z]", ref) and re.search(r"\d", ref))
            or "_" in ref
            or "-" in ref
        ):
            return

        key = ref.upper()
        if key in seen_refs:
            return
        seen_refs.add(key)

        designation = _clean_ocr_line(description).strip(" -:;,.")
        quantity = _clean_ocr_line(qty)

        # Si aucune quantité n'est donnée explicitement, on considère 1 article.
        if not quantity:
            quantity = "1"

        items.append({
            "reference": ref,
            "quantite": quantity,
            "designation": designation,
            "dimensions": _clean_ocr_line(dimensions),
        })

    # 1) Cas explicite REF / REFERENCE.
    for line in lines:
        m = re.search(
            r"\bREF(?:ERENCE)?\s*[:.=\-]?\s*([A-Za-z0-9][A-Za-z0-9 _/-]{2,30})",
            line,
            re.I
        )
        if not m:
            continue

        raw = _clean_ocr_line(m.group(1))
        # Coupe la référence au premier gros séparateur ou début clair de désignation.
        tokens = raw.split()
        ref_tokens = []
        for token in tokens[:3]:
            if re.fullmatch(r"[A-Za-z0-9_-]+", token):
                ref_tokens.append(token)
            else:
                break
        raw_ref = " ".join(ref_tokens) if ref_tokens else raw

        designation = line[m.end():].strip(" :-|")
        add_item(raw_ref, designation, extract_dimensions(line))

    # 2) Références structurées avec _ ou - n'importe où dans la ligne.
    for line in lines:
        if is_noise(line):
            continue
        for m in re.finditer(
            r"\b([A-Za-z0-9]{1,20}[_-][A-Za-z0-9_-]{2,30})\b",
            line
        ):
            ref = m.group(1)
            designation = (line[:m.start()] + " " + line[m.end():]).strip(" :-|")
            # Si la ligne commence par la référence, la suite est la désignation.
            if not line[:m.start()].strip():
                designation = line[m.end():].strip(" :-|")
            add_item(ref, designation, extract_dimensions(line))

    # 3) Détection générique du début de ligne.
    #    On examine les 1 à 3 premiers blocs et on choisit le meilleur candidat.
    for line in lines:
        if is_noise(line):
            continue

        # Retire une éventuelle quantité en début de ligne : "1 750012MW02 ..."
        qty = ""
        work = line
        mq = re.match(r"^\s*(\d{1,3})\s+[xX]?\s+(.+)$", work)
        if mq:
            qty = mq.group(1)
            work = mq.group(2).strip()

        tokens = work.split()
        if len(tokens) < 2:
            continue

        candidates = []

        # Candidat 1 token : 750012MW02, LDV_1047, ABC-123
        candidates.append((tokens[0], 1))

        # Candidat 2 tokens : 960130M 04
        if len(tokens) >= 3:
            candidates.append((tokens[0] + " " + tokens[1], 2))

        # Candidat 3 tokens, très rare mais toléré.
        if len(tokens) >= 4:
            candidates.append((tokens[0] + " " + tokens[1] + " " + tokens[2], 3))

        best = None
        best_score = -999

        for cand, used in candidates:
            ref = normalise_reference(cand)
            if len(ref) < 4 or len(ref) > 30:
                continue
            if is_date_like(ref) or is_phone_like(ref) or is_dimension_like(ref):
                continue

            score = 0

            has_letter = bool(re.search(r"[A-Za-z]", ref))
            has_digit = bool(re.search(r"\d", ref))

            if has_letter and has_digit:
                score += 5
            if "_" in ref or "-" in ref:
                score += 3
            if 5 <= len(ref) <= 18:
                score += 2
            if re.match(r"^[A-Za-z0-9]", ref):
                score += 1
            if used == 1:
                score += 1

            # Pénalise les mots ordinaires.
            if ref.isalpha():
                score -= 8
            if re.fullmatch(r"\d+", ref):
                score -= 8

            # Il doit rester une désignation crédible après la référence.
            remaining = tokens[used:]
            designation = " ".join(remaining).strip()
            if len(designation) >= 4 and re.search(r"[A-Za-zÀ-ÿ]", designation):
                score += 4
            else:
                score -= 4

            if score > best_score:
                best_score = score
                best = (ref, used, designation)

        if best and best_score >= 7:
            ref, used, designation = best
            dims = extract_dimensions(line)
            add_item(ref, designation, dims, qty)

    print(f"[ENLEVEMENT ITEMS] {len(items)} article(s) detecte(s): {[x['reference'] for x in items]}")
    return items

def _extract_instructions_block(clean_text):
    """Isole la zone Instructions, y compris si le titre est légèrement déformé par l'OCR."""
    m = re.search(r"\bInstr(?:uctions?|uctions|uction)\b", clean_text, re.I)
    if not m:
        return ""

    tail = clean_text[m.end():]
    stop = re.search(
        r"\b(?:Assur[eé]\s+par|OBSERVATIONS?(?:\s+ou\s+R[EÉ]SERVES?)?|"
        r"Valeur\s+assur[eé]e|Signature|Heure\s+d['’]?arriv[eé]e|Heure\s+de\s+d[eé]part)\b",
        tail,
        re.I
    )
    block = tail[:stop.start()] if stop else tail[:3000]
    return block.strip()


def _slice_columns_by_headers(raw_lines, header_index, headers):
    """
    Découpe les lignes sous une ligne d'en-têtes en colonnes selon la position
    horizontale de chaque libellé. Utile pour les tableaux OCR du bon interne.
    """
    header_line = raw_lines[header_index]
    positions = []
    for name, pattern in headers:
        m = re.search(pattern, header_line, re.I)
        if m:
            positions.append((name, m.start()))
    positions.sort(key=lambda x: x[1])

    if len(positions) < 2:
        return {}

    bounds = {}
    for i, (name, pos) in enumerate(positions):
        if i == 0:
            left = 0
        else:
            prev_pos = positions[i - 1][1]
            left = (prev_pos + pos) // 2
        if i + 1 < len(positions):
            right = (pos + positions[i + 1][1]) // 2
        else:
            right = None
        bounds[name] = (left, right)

    columns = {name: [] for name, _ in positions}
    for line in raw_lines[header_index + 1:]:
        for name, (left, right) in bounds.items():
            piece = line[left:right].strip() if right is not None else line[left:].strip()
            if piece:
                columns[name].append(_clean_ocr_line(piece))
    return columns


def _extract_programme_chantier(clean_text):
    """
    Lit la zone 'Programme du chantier' comme un tableau vertical :
    les valeurs sont sous Date:, Heure:, Service:, Notes:, Assigné à:, Véhicules:.
    """
    result = {
        "date_enlevement": "",
        "heure_enlevement": "",
        "service": "",
        "notes": "",
        "assigne_a": "",
        "vehicules": "",
    }

    raw_lines = _as_text(clean_text).replace("\r", "").splitlines()
    start_idx = None
    end_idx = len(raw_lines)

    for i, line in enumerate(raw_lines):
        if re.search(r"Programme\s+du\s+chantier", line, re.I):
            start_idx = i
            break
    if start_idx is None:
        return result

    for i in range(start_idx + 1, len(raw_lines)):
        if re.search(r"\bInstr(?:uctions?|uction)\b", raw_lines[i], re.I):
            end_idx = i
            break

    zone = raw_lines[start_idx + 1:end_idx]

    header_idx_local = None
    for i, line in enumerate(zone):
        # On cherche la vraie ligne d'en-têtes du tableau.
        if re.search(r"\bDate\s*:", line, re.I) and re.search(r"\bNotes?\s*:", line, re.I):
            header_idx_local = i
            break

    if header_idx_local is not None:
        headers = [
            ("date", r"\bDate\s*:"),
            ("heure", r"\bHeure\s*:"),
            ("service", r"\bService\s*:"),
            ("notes", r"\bNotes?\s*:"),
            ("assigne", r"\bAssign[eé]\s+[aà]\s*:"),
            ("vehicules", r"\bV[eé]hicules?\s*:"),
        ]
        cols = _slice_columns_by_headers(zone, header_idx_local, headers)

        def first_value(name):
            vals = [v for v in cols.get(name, []) if v and not re.search(r"^[|:_-]+$", v)]
            return vals[0] if vals else ""

        result["date_enlevement"] = _normalise_enlevement_date(first_value("date"))
        result["heure_enlevement"] = first_value("heure")
        result["service"] = first_value("service")
        result["notes"] = first_value("notes")
        result["assigne_a"] = first_value("assigne")
        result["vehicules"] = first_value("vehicules")

    # Secours ciblé uniquement dans la zone Programme du chantier.
    zone_text = "\n".join(zone)
    if not result["date_enlevement"]:
        result["date_enlevement"] = _normalise_enlevement_date(zone_text)

    # Si les colonnes OCR sont mal alignées, essaie les valeurs situées
    # immédiatement sous les libellés sur des lignes séparées.
    cleaned_zone = _enlevement_lines(zone_text)
    if not result["notes"]:
        result["notes"] = _value_after_label(
            cleaned_zone,
            [r"\bNotes?\b"],
            [r"Assign[eé]\s+[aà]", r"V[eé]hicules?", r"Service", r"Instructions?"]
        )
    if not result["heure_enlevement"]:
        result["heure_enlevement"] = _value_after_label(
            cleaned_zone, [r"\bHeure\b"], [r"Service", r"Notes?", r"Assign[eé]\s+[aà]"]
        )
    if not result["service"]:
        result["service"] = _value_after_label(
            cleaned_zone, [r"\bService\b"], [r"Notes?", r"Assign[eé]\s+[aà]", r"V[eé]hicules?"]
        )

    return result


def _extract_contact_blocks(clean_text):
    """Extrait les blocs Depuis / À en conservant toutes les informations dans l'adresse."""
    result = {
        "adresse_depart": "",
        "adresse_destination": "",
    }

    raw_lines = _as_text(clean_text).replace("\r", "").splitlines()
    header_idx = None
    for i, line in enumerate(raw_lines):
        if re.search(r"\bDepuis\s*:", line, re.I) and re.search(r"(?:\bA\s*:|\bÀ\s*:)", line, re.I):
            header_idx = i
            break
    if header_idx is None:
        return result

    end_idx = len(raw_lines)
    for i in range(header_idx + 1, len(raw_lines)):
        if re.search(r"Programme\s+du\s+chantier", raw_lines[i], re.I):
            end_idx = i
            break

    cols = _slice_columns_by_headers(
        raw_lines[:end_idx], header_idx,
        [("depart", r"\bDepuis\s*:"), ("destination", r"(?:\bA\s*:|\bÀ\s*:)")]
    )

    def full_block(values):
        vals = [_clean_ocr_line(v) for v in values if _clean_ocr_line(v)]
        return "\n".join(vals)

    result["adresse_depart"] = full_block(cols.get("depart", []))
    result["adresse_destination"] = full_block(cols.get("destination", []))
    return result



def _spatial_group_lines(words, y_tolerance=14):
    """Regroupe des mots OCR par lignes en conservant l'ordre horizontal."""
    if not words:
        return []
    words = sorted(words, key=lambda w: (w["cy"], w["left"]))
    lines = []
    for word in words:
        target = None
        for line in lines:
            if abs(word["cy"] - line["cy"]) <= y_tolerance:
                target = line
                break
        if target is None:
            target = {"cy": word["cy"], "words": []}
            lines.append(target)
        target["words"].append(word)
        target["cy"] = sum(w["cy"] for w in target["words"]) / len(target["words"])

    result = []
    for line in sorted(lines, key=lambda x: x["cy"]):
        ordered = sorted(line["words"], key=lambda w: w["left"])
        text = _clean_ocr_line(" ".join(w["text"] for w in ordered))
        if text:
            result.append({"cy": line["cy"], "text": text, "words": ordered})
    return result


def _extract_enlevement_spatial(pdf_bytes):
    """
    Lecture spatiale de la première page du bon d'enlèvement.

    Contrairement à la lecture texte classique, cette méthode utilise les vraies
    coordonnées OCR des mots. Elle sert uniquement aux zones dont la mise en page
    est en colonnes : Adresses (Depuis / À) et Programme du chantier.
    """
    result = {
        "adresse_depart": "",
        "adresse_destination": "",
        "date_enlevement": "",
        "notes": "",
    }

    try:
        from pdf2image import convert_from_bytes
        import pytesseract
    except Exception as e:
        print(f"[ENLEVEMENT SPATIAL] OCR spatial indisponible: {e}")
        return result

    try:
        # 200 dpi suffit pour localiser correctement les colonnes et limite la charge mémoire.
        images = convert_from_bytes(
            pdf_bytes,
            dpi=200,
            grayscale=True,
            first_page=1,
            last_page=1,
            thread_count=1,
        )
        if not images:
            return result
        image = images[0]
        data = pytesseract.image_to_data(
            image,
            lang="fra",
            config="--psm 6",
            output_type=pytesseract.Output.DICT,
        )
    except Exception as e:
        print(f"[ENLEVEMENT SPATIAL] Echec lecture spatiale: {e}")
        return result
    finally:
        try:
            for img in locals().get('images', []) or []:
                img.close()
        except Exception:
            pass

    words = []
    count = len(data.get("text", []))
    for i in range(count):
        text = _clean_ocr_line(data["text"][i])
        if not text:
            continue
        try:
            conf = float(data.get("conf", [0] * count)[i])
        except Exception:
            conf = 0
        if conf < 20:
            continue
        left = int(data["left"][i])
        top = int(data["top"][i])
        width = int(data["width"][i])
        height = int(data["height"][i])
        words.append({
            "text": text,
            "left": left,
            "top": top,
            "right": left + width,
            "bottom": top + height,
            "cx": left + width / 2,
            "cy": top + height / 2,
        })

    if not words:
        return result

    page_width = max(w["right"] for w in words)
    page_mid = page_width * 0.50

    def find_word(pattern, y_min=0, y_max=10**9, x_min=0, x_max=10**9):
        for w in sorted(words, key=lambda z: (z["top"], z["left"])):
            if not (y_min <= w["cy"] <= y_max and x_min <= w["cx"] <= x_max):
                continue
            if re.search(pattern, w["text"], re.I):
                return w
        return None

    # ------------------------------------------------------------------
    # Zone Adresses : sépare physiquement la moitié gauche (Depuis) et droite (À).
    # ------------------------------------------------------------------
    depuis = find_word(r"^Depuis:?$")
    programme = find_word(r"^Programme$", y_min=(depuis["cy"] if depuis else 0))
    if depuis and programme:
        y_start = depuis["bottom"] + 8
        y_end = programme["top"] - 10
        zone_words = [w for w in words if y_start <= w["cy"] <= y_end]

        left_lines = _spatial_group_lines([w for w in zone_words if w["cx"] < page_mid])
        right_lines = _spatial_group_lines([w for w in zone_words if w["cx"] >= page_mid])

        def clean_address_lines(lines):
            cleaned = []
            for line in lines:
                txt = _clean_ocr_line(line["text"])
                if not txt:
                    continue
                if re.fullmatch(r"(?:Depuis|A|À)\s*:?", txt, re.I):
                    continue
                cleaned.append(txt)
            return cleaned

        left_clean = clean_address_lines(left_lines)
        right_clean = clean_address_lines(right_lines)

        if left_clean:
            result["adresse_depart"] = "\n".join(left_clean)
        if right_clean:
            result["adresse_destination"] = "\n".join(right_clean)

    # ------------------------------------------------------------------
    # Programme du chantier : valeur située SOUS le libellé.
    # ------------------------------------------------------------------
    programme = programme or find_word(r"^Programme$")
    instructions = find_word(r"^Instructions?$", y_min=(programme["cy"] if programme else 0))
    if programme and instructions:
        y_top = programme["bottom"] + 8
        y_bottom = instructions["top"] - 8
        prog_words = [w for w in words if y_top <= w["cy"] <= y_bottom]

        # Repère les colonnes à partir des libellés Date / Heure / Service / Notes / Assigné.
        labels = {}
        for name, pattern in [
            ("date", r"^Date:?$"),
            ("heure", r"^Heure:?$"),
            ("service", r"^Service:?$"),
            ("notes", r"^Notes?:?$"),
            ("assigne", r"^Assign[eé]$"),
        ]:
            matches = [w for w in prog_words if re.search(pattern, w["text"], re.I)]
            if matches:
                labels[name] = min(matches, key=lambda w: w["top"])

        ordered = sorted((w["left"], name, w) for name, w in labels.items())
        bounds = {}
        for idx, (left, name, word) in enumerate(ordered):
            x0 = 0 if idx == 0 else (ordered[idx - 1][0] + left) / 2
            x1 = page_width if idx + 1 == len(ordered) else (left + ordered[idx + 1][0]) / 2
            bounds[name] = (x0, x1, word["bottom"] + 4)

        if "date" in bounds:
            x0, x1, y0 = bounds["date"]
            vals = [w for w in prog_words if x0 <= w["cx"] < x1 and w["cy"] >= y0]
            date_text = " ".join(x["text"] for x in sorted(vals, key=lambda z: (z["top"], z["left"])))
            result["date_enlevement"] = _normalise_enlevement_date(date_text)

        if "notes" in bounds:
            x0, x1, y0 = bounds["notes"]
            vals = [w for w in prog_words if x0 <= w["cx"] < x1 and w["cy"] >= y0]
            note_lines = _spatial_group_lines(vals)
            if note_lines:
                note = note_lines[0]["text"]
                # Sur certains scans, le trait oblique sous la note transforme SS# en SSH#.
                note = re.sub(r"\bSSH#(\d+)\b", r"SS#\1", note, flags=re.I)
                result["notes"] = _clean_ocr_line(note)

    print(
        "[ENLEVEMENT SPATIAL] "
        f"depart={result.get('adresse_depart') or '-'} | "
        f"destination={result.get('adresse_destination') or '-'} | "
        f"date={result.get('date_enlevement') or '-'} | notes={result.get('notes') or '-'}"
    )
    return result


def _extract_enlevement_pdf(pdf_bytes):
    """Analyse un bon d'enlevement et retourne les donnees utiles au planning reception."""
    text, page_count, ocr_used = _extract_enlevement_pdf_text(pdf_bytes)
    clean_text = text.replace("\r", "")

    # Toute la partie du bon à partir de "Assuré par" est administrative :
    # elle ne doit jamais alimenter les champs, instructions ou articles.
    cutoff = re.search(r"\bAssur[eé]\s+par\b", clean_text, re.I)
    if cutoff:
        clean_text = clean_text[:cutoff.start()].rstrip()

    lines = _enlevement_lines(clean_text)

    label_numero = [
        r"Num[eé]ro\s+de\s+r[eé]f[eé]r(?:ence)?",
        r"N[°ºo]\s*de\s*r[eé]f[eé]rence",
        r"R[eé]f[eé]rence\s+du\s+bon",
    ]
    common_stops = [
        r"Client", r"Coordinateur", r"Exhibition", r"Programme\s+du\s+chantier",
        r"Instructions?", r"Adresse", r"Service"
    ]

    numero_bon = _value_after_label(lines, label_numero, common_stops)
    if numero_bon:
        m = re.search(r"\b([A-Za-z0-9][A-Za-z0-9_-]{3,})\b", numero_bon)
        numero_bon = m.group(1) if m else ""

    client = _value_after_label(
        lines,
        [r"\bClient\b"],
        [r"Coordinateur", r"Exhibition", r"Programme\s+du\s+chantier", r"Adresse", r"Service"]
    )
    coordinateur = _value_after_label(
        lines,
        [r"Coordinateur"],
        [r"Client", r"Exhibition", r"Programme\s+du\s+chantier", r"Adresse", r"Service"]
    )
    exhibition = _value_after_label(
        lines,
        [r"Exhibition"],
        [r"Client", r"Coordinateur", r"Programme\s+du\s+chantier", r"Adresse", r"Service"]
    )

    if client:
        client = re.split(r"\bExhibition\b", client, maxsplit=1, flags=re.I)[0].strip(" :-|")
    if coordinateur:
        coordinateur = re.split(r"\b(?:Client|Exhibition)\b", coordinateur, maxsplit=1, flags=re.I)[0].strip(" :-|")
    if exhibition:
        exhibition = re.split(r"\b(?:Client|Coordinateur)\b", exhibition, maxsplit=1, flags=re.I)[0].strip(" :-|")

    programme = _extract_programme_chantier(clean_text)
    spatial = _extract_enlevement_spatial(pdf_bytes)
    date_enlevement = spatial.get("date_enlevement") or programme.get("date_enlevement", "")

    instructions = _extract_instructions_block(clean_text)
    # Les articles doivent provenir uniquement de la zone Instructions.
    items = _extract_enlevement_items(instructions) if instructions else []

    contact_data = _extract_contact_blocks(clean_text)
    # Les coordonnées OCR réelles sont prioritaires pour les deux colonnes d'adresses.
    for key in ("adresse_depart", "adresse_destination"):
        if spatial.get(key):
            contact_data[key] = spatial[key]
    # Ces informations ne sont pas utilisées dans la fiche réception.
    contact_data.pop("telephone_depart", None)
    contact_data.pop("telephone_destination", None)
    notes = spatial.get("notes") or programme.get("notes", "")

    display_name = " - ".join(
        x for x in [_clean_ocr_line(client), _clean_ocr_line(numero_bon)] if x
    )

    result = {
        "numero_bon": numero_bon,
        "client": client,
        "display_name": display_name,
        "coordinateur": coordinateur,
        "exhibition": exhibition,
        "date_enlevement": date_enlevement,
        "service": programme.get("service", ""),
        "assigne_a": programme.get("assigne_a", ""),
        "vehicules": programme.get("vehicules", ""),
        "notes": notes,
        "instructions": instructions,
        "items": items,
        "references": [x.get("reference") for x in items if x.get("reference")],
        "page_count": page_count,
        "ocr_used": ocr_used,
        "raw_text": clean_text,
        **contact_data,
    }

    print(
        "[ENLEVEMENT EXTRACTION] "
        f"bon={result.get('numero_bon') or '-'} "
        f"client={result.get('client') or '-'} "
        f"date={result.get('date_enlevement') or '-'} "
        f"refs={result.get('references') or []}"
    )
    return result


def _analyse_enlevement_ticket_background(ticket_id, pdf_bytes):
    """Analyse le bon apres creation du ticket, sans bloquer le demandeur."""
    try:
        print(f"[ENLEVEMENT] Analyse asynchrone demarree pour {ticket_id}")
        parsed = _extract_enlevement_pdf(pdf_bytes)
        ticket = load_ticket(ticket_id)
        if not ticket:
            print(f"[ENLEVEMENT] Ticket introuvable apres creation : {ticket_id}")
            return

        existing_enl = dict(ticket.get("enlevement") or {})
        numero_dossier = _as_text(
            existing_enl.get("numero_dossier")
            or existing_enl.get("dossier_numero")
            or ticket.get("numeroDossier")
            or ticket.get("numero_dossier")
        ).strip()

        # Les informations choisies/saisies à partir du N° dossier restent prioritaires
        # sur les valeurs OCR du bon. L'OCR complète seulement les champs manquants.
        for field in ("client", "coordinateur", "exhibition"):
            if _as_text(existing_enl.get(field)).strip():
                parsed[field] = _as_text(existing_enl.get(field)).strip()
        if numero_dossier:
            parsed["numero_dossier"] = numero_dossier

        ticket["enlevement"] = {
            **parsed,
            # Nom visible côté réception. L'id ENL-xxx reste uniquement technique.
            "display_name": parsed.get("display_name") or " - ".join(
                x for x in [parsed.get("client"), parsed.get("numero_bon")] if x
            ),
            "analysis_status": "ready",
            "analysis_error": "",
            "analysed_at": datetime.now().isoformat(),
        }

        if numero_dossier:
            ticket["numeroDossier"] = numero_dossier
            ticket["dossier"] = numero_dossier
        elif parsed.get("client"):
            # Compatibilité avec les anciens tickets qui utilisaient dossier pour le client.
            ticket["dossier"] = parsed["client"]
        if parsed.get("numero_bon"):
            ticket["ref"] = parsed["numero_bon"]
        if parsed.get("coordinateur"):
            ticket["chargeProjet"] = parsed["coordinateur"]
        if parsed.get("exhibition"):
            ticket["expo"] = parsed["exhibition"]
            ticket["objet"] = parsed["exhibition"]
        if parsed.get("date_enlevement"):
            try:
                dt = datetime.strptime(parsed["date_enlevement"], "%d/%m/%Y")
                ticket["dateRdv"] = dt.strftime("%Y-%m-%d")
            except ValueError:
                pass

        ticket["updatedAt"] = datetime.now().isoformat()
        save_ticket(ticket)
        try:
            _ensure_articles_for_ticket(ticket, save=True)
        except Exception as article_error:
            print(f"[ARTICLES] Attribution ESI impossible pour {ticket_id}: {article_error}")
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
            "Aucune référence de Packing de type 'V/Cde : dossier/numéro' n'a été détectée, même après OCR."
        )

    return {
        "bl_numero": bl_numero,
        "bl_date": bl_date,
        "references": refs,
        "page_count": len(reader.pages),
        "ocr_used": ocr_used,
    }


def _match_reception_refs_to_tickets(references):
    """Rapproche les références du PDF avec les fiches de Packing ESI TICKETS."""
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
                "packing_ref": _packing_reference(t.get("dossier"), t.get("ref")),
                "charge_projet": t.get("chargeProjet") or "",
                "date_emballage": t.get("dateEmballage") or "",
                "localisation": fiche.get("localisation") or "",
                "status": t.get("status") or "",
                "receptionnee": _is_caisse_receptionnee(t),
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


_REFERENTIEL_LOCK = threading.Lock()


def _next_referentiel_id(cfg):
    """
    Genere un identifiant pour les tables de referentiels lorsque la colonne
    Supabase `id` est NOT NULL mais ne possede pas de valeur par defaut.

    La fonction conserve le type deja utilise par la table :
      - id numerique -> prochain entier disponible ;
      - id texte numerique -> prochain nombre sous forme de texte ;
      - id UUID / texte -> nouvel UUID.
    """
    rows = supabase_rest_request(
        "GET", cfg["table"], "select=id&order=id.desc&limit=5000"
    ) or []
    ids = [row.get("id") for row in rows if row.get("id") not in (None, "")]

    # Les tables ESI historiques utilisent des ids numeriques. Si la table est
    # vide, on demarre donc a 1. Un secours UUID est gere lors de l'insertion.
    if not ids:
        return 1

    sample = ids[0]
    if isinstance(sample, (int, float)) and not isinstance(sample, bool):
        numeric = []
        for value in ids:
            try:
                numeric.append(int(value))
            except (TypeError, ValueError):
                pass
        return (max(numeric) + 1) if numeric else 1

    text_ids = [str(value).strip() for value in ids if str(value).strip()]
    if text_ids and all(re.fullmatch(r"\d+", value) for value in text_ids):
        return str(max(int(value) for value in text_ids) + 1)

    return str(uuid.uuid4())


def _insert_referentiel_with_id(cfg, payload):
    """Insere un referentiel en compensant l'absence de DEFAULT sur `id`."""
    try:
        return supabase_rest_request(
            "POST", cfg["table"], "", [payload], prefer="return=representation"
        ) or []
    except Exception as first_error:
        message = str(first_error).lower()
        if not ("null value in column \"id\"" in message or "violates not-null constraint" in message):
            raise

    # La base ne genere pas l'id : on le fournit explicitement.
    generated = _next_referentiel_id(cfg)
    with_id = dict(payload)
    with_id["id"] = generated

    try:
        return supabase_rest_request(
            "POST", cfg["table"], "", [with_id], prefer="return=representation"
        ) or []
    except Exception as second_error:
        # Cas particulier d'une table vide dont l'id serait de type UUID :
        # l'essai numerique ci-dessus permet de detecter le type sans toucher au schema.
        text = str(second_error).lower()
        if generated == 1 and "uuid" in text:
            with_id["id"] = str(uuid.uuid4())
            return supabase_rest_request(
                "POST", cfg["table"], "", [with_id], prefer="return=representation"
            ) or []
        raise


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
        # Certaines tables Supabase du referentiel ont une colonne `id` NOT NULL
        # sans generation automatique. Le helper tente d'abord l'insertion normale,
        # puis fournit un id compatible uniquement si Supabase le demande.
        with _REFERENTIEL_LOCK:
            rows = _insert_referentiel_with_id(cfg, payload)
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
        suffix = _packing_reference(dossier, ref) or dossier or ref
        return f"[ESI Tickets] Fiche de Packing terminée - {suffix}".strip()

    if module == "Demande de devis":
        suffix = " ".join([x for x in [dossier, projet] if x]).strip()
        return f"[ESI Tickets] Demande de devis terminée - {suffix}".strip()

    if module == "Demande Aller voir":
        suffix = " ".join([x for x in [dossier, lieu_rdv or projet] if x]).strip()
        return f"[ESI Tickets] Aller voir finalisé - {suffix}".strip()

    if module == "Avis d'arrivée":
        suffix = " ".join([x for x in [dossier, projet] if x]).strip()
        return f"[ESI Tickets] Avis d'arrivée terminé - {suffix}".strip()

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
Type : {_module_display_label(module)}
Dossier / Client : {dossier}
Référence / N° Packing : {_packing_reference(dossier, ref) if module == "Fiche de caisse" else ref}
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

@app.route('/mise-en-caisse')
def mise_en_caisse_page():
    return render_template('mise_en_caisse.html')

GESTIONNAIRE_ARTICLES_LIES_JS = r"""(function(){
'use strict';

const STYLE_ID='esi-caisse-articles-v2-style';
if(!document.getElementById(STYLE_ID)){
  const style=document.createElement('style');
  style.id=STYLE_ID;
  style.textContent=`
    .caisse-articles-panel{display:none;margin:14px 0 0;border:1px solid #b9dbea;background:linear-gradient(180deg,#f0f9ff,#fff);border-radius:16px;padding:14px}
    .caisse-articles-panel.show{display:block}
    .caisse-articles-title{font-size:11px;font-weight:900;color:#0284c7;text-transform:uppercase;letter-spacing:.055em;margin-bottom:9px}
    .caisse-articles-selected{display:flex;gap:7px;flex-wrap:wrap;align-items:center;min-height:30px}
    .caisse-article-chip{appearance:none;border:1px solid #7dd3fc;background:#e0f2fe;color:#075985;border-radius:999px;padding:7px 10px;font:inherit;font-size:12px;font-weight:800;cursor:pointer;display:inline-flex;gap:7px;align-items:center;max-width:100%}
    .caisse-article-chip:hover{background:#bae6fd}.caisse-article-chip .remove{font-size:15px;line-height:1;color:#0f2f4f}
    .caisse-articles-empty{font-size:12px;color:#64748b}
    .caisse-articles-search{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:8px;margin-top:12px}
    .caisse-articles-search input{width:100%;border:1px solid #9bcbe7;background:#fff;border-radius:12px;padding:10px 12px;font:inherit;box-sizing:border-box}
    .caisse-articles-results{margin-top:10px;border:1px solid #dbe7f0;border-radius:12px;background:#fff;max-height:260px;overflow:auto}
    .caisse-articles-result-head,.caisse-articles-result{display:grid;grid-template-columns:38px minmax(72px,.55fr) minmax(90px,.7fr) minmax(120px,1fr) minmax(190px,1.7fr);gap:8px;align-items:center;padding:9px 10px}
    .caisse-articles-result-head{position:sticky;top:0;background:#f8fafc;border-bottom:1px solid #dbe7f0;font-size:10px;font-weight:900;text-transform:uppercase;color:#64748b;z-index:1}
    .caisse-articles-result{border-bottom:1px solid #eef2f7;font-size:12px}.caisse-articles-result:last-child{border-bottom:0}.caisse-articles-result:hover{background:#f0f9ff}
    .caisse-articles-result input{width:17px;height:17px;cursor:pointer}.caisse-articles-result .ref{font-weight:850;color:#0f2f4f;overflow-wrap:anywhere}.caisse-articles-result .dos{color:#475569;overflow-wrap:anywhere}.caisse-articles-result .esi-search{font-weight:900;color:#0369a1;overflow-wrap:anywhere}.caisse-articles-result .info-search{color:#475569;overflow-wrap:anywhere;line-height:1.3}.caisse-articles-result .info-search strong{color:#0f2f4f}
    .caisse-articles-hint{font-size:10px;color:#64748b;margin-top:7px;line-height:1.35}
    .caisse-article-modal-backdrop{position:fixed;inset:0;background:rgba(15,23,42,.55);z-index:1600;display:none;align-items:center;justify-content:center;padding:18px}.caisse-article-modal-backdrop.show{display:flex}
    .caisse-article-modal{background:#f8fbfd;width:min(1000px,97vw);max-height:93vh;overflow:auto;border-radius:22px;box-shadow:0 26px 80px rgba(15,23,42,.28);border:1px solid #dbe7f0}
    .caisse-article-modal-head{position:sticky;top:0;z-index:3;background:#fff;border-bottom:1px solid #dbe7f0;padding:16px 18px;display:flex;justify-content:space-between;gap:14px;align-items:center}.caisse-article-modal-title{font-size:19px;font-weight:900;color:#0f2f4f}.caisse-article-modal-sub{font-size:11px;color:#64748b;margin-top:3px}.caisse-article-modal-close{border:0;background:#eaf6ff;color:#0f2f4f;border-radius:10px;width:36px;height:36px;font-size:21px;cursor:pointer}
    .caisse-article-modal-body{padding:18px}.caisse-article-hero{background:linear-gradient(135deg,#0f2f4f,#16476f);color:#fff;border-radius:16px;padding:18px;display:flex;justify-content:space-between;gap:16px;align-items:flex-start;flex-wrap:wrap}.caisse-article-hero .ref{font-size:25px;font-weight:950;overflow-wrap:anywhere}.caisse-article-hero .desc{font-size:12px;color:#dbeafe;margin-top:5px;max-width:650px}.caisse-article-hero .esi{font-size:11px;font-weight:900;background:rgba(255,255,255,.13);border:1px solid rgba(255,255,255,.25);padding:6px 9px;border-radius:999px}
    .caisse-article-detail-layout{display:grid;grid-template-columns:minmax(0,1fr) 240px;gap:14px;margin-top:14px}.caisse-article-fields{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:9px}.caisse-article-field{border:1px solid #c9e0ed;background:#fff;border-radius:12px;padding:10px}.caisse-article-field.wide{grid-column:1/-1}.caisse-article-field .k{font-size:9px;font-weight:900;text-transform:uppercase;color:#64748b;letter-spacing:.04em}.caisse-article-field .v{font-size:13px;font-weight:750;color:#16324a;margin-top:5px;white-space:pre-wrap;overflow-wrap:anywhere}.caisse-article-photo{border:1px solid #c9e0ed;background:#fff;border-radius:14px;min-height:220px;display:flex;align-items:center;justify-content:center;overflow:hidden}.caisse-article-photo img{width:100%;height:100%;min-height:220px;object-fit:contain}.caisse-article-no-photo{font-size:11px;color:#64748b;text-align:center;padding:18px}
    .caisse-article-section{margin-top:14px;border:1px solid #dbe7f0;background:#fff;border-radius:14px;padding:13px}.caisse-article-section-title{font-size:11px;font-weight:900;text-transform:uppercase;color:#0369a1;margin-bottom:9px}.caisse-article-history{display:grid;gap:8px}.caisse-article-history-item{border-left:4px solid #0ea5e9;background:#f8fafc;border-radius:10px;padding:9px 10px;font-size:11px;color:#334155}.caisse-article-history-item strong{color:#0f2f4f}
    @media(max-width:760px){.caisse-articles-search{grid-template-columns:1fr}.caisse-article-detail-layout{grid-template-columns:1fr}.caisse-article-fields{grid-template-columns:1fr}.caisse-article-field.wide{grid-column:auto}.caisse-articles-result-head,.caisse-articles-result{grid-template-columns:34px minmax(70px,.55fr) minmax(90px,.7fr) minmax(110px,1fr) minmax(150px,1.4fr)}}
  `;
  document.head.appendChild(style);
}

let linkedTicketId='';
let linkedDraft=new Map();
let linkedSearchResults=[];
let linkedLoadSeq=0;

function currentTicket(){
  try{return state.tickets.find(t=>t.id===state.selectedTicketId)||null}catch(e){return null}
}
function isFicheCaisse(ticket){return !!ticket && ticket.module==='Fiche de caisse'}
function refLabel(article){return String((article&&article.reference)||'').trim()||String((article&&article.esi_id)||'').trim()||'Sans référence'}

function ensurePanel(){
  let panel=document.getElementById('caisseArticlesPanel');
  if(panel)return panel;
  panel=document.createElement('div');
  panel.id='caisseArticlesPanel';
  panel.className='caisse-articles-panel';
  const grid=document.getElementById('detailGrid');
  if(grid)grid.insertAdjacentElement('afterend',panel);
  return panel;
}

function selectedHtml(editing){
  const values=[...linkedDraft.values()];
  if(!values.length)return '<div class="caisse-articles-empty">Aucun Article lié à ce Packing.</div>';
  return values.map(a=>`<button type="button" class="caisse-article-chip" data-open-esi="${escapeHtml(a.esi_id)}" title="Ouvrir la fiche détaillée">${escapeHtml(refLabel(a))}${editing?`<span class="remove" data-remove-esi="${escapeHtml(a.esi_id)}" title="Retirer">×</span>`:''}</button>`).join('');
}

function renderPanel(){
  const panel=ensurePanel(),ticket=currentTicket();
  if(!panel)return;
  if(!isFicheCaisse(ticket)){panel.classList.remove('show');panel.innerHTML='';return}
  panel.classList.add('show');
  const editing=!!state.editMode;
  panel.innerHTML=`
    <div class="caisse-articles-title">Articles liés à ce Packing</div>
    <div class="caisse-articles-selected" id="caisseArticlesSelected">${selectedHtml(editing)}</div>
    ${editing?`<div class="caisse-articles-search"><input id="caisseArticlesSearchInput" autocomplete="off" placeholder="Recherche globale : client / prêteur, description, référence, dossier, projet..."><button class="btn secondary" id="caisseArticlesSearchBtn" type="button">Rechercher</button></div><div class="caisse-articles-hint">Recherche dans toute la fiche article : client / prêteur, description, référence, N° ESI, dossier, projet, stockage, Packing / Pre-Packing, dimensions, poids, etc.</div><div class="caisse-articles-results" id="caisseArticlesResults" style="display:none"></div>`:''}
  `;
  bindPanelEvents();
}

async function loadLinks(force=false){
  const ticket=currentTicket();
  if(!isFicheCaisse(ticket)){linkedTicketId='';linkedDraft.clear();renderPanel();return}
  if(!force && linkedTicketId===ticket.id){renderPanel();return}
  const seq=++linkedLoadSeq;
  linkedTicketId=ticket.id;linkedDraft.clear();linkedSearchResults=[];
  try{
    const r=await fetch('/api/tickets/'+encodeURIComponent(ticket.id)+'/articles-lies',{cache:'no-store'});
    const d=await r.json();
    if(seq!==linkedLoadSeq)return;
    if(!r.ok)throw new Error(d.error||'Impossible de charger les articles liés');
    (d.articles||[]).forEach(a=>linkedDraft.set(String(a.esi_id),a));
  }catch(e){if(seq===linkedLoadSeq)showNotice(e.message||'Impossible de charger les articles liés')}
  if(seq===linkedLoadSeq)renderPanel();
}

function bindSelectedEvents(){
  const selected=document.getElementById('caisseArticlesSelected');if(!selected)return;
  selected.querySelectorAll('[data-open-esi]').forEach(btn=>btn.addEventListener('click',e=>{
    if(e.target&&e.target.matches('[data-remove-esi]'))return;
    openLinkedArticleDetail(btn.dataset.openEsi);
  }));
  selected.querySelectorAll('[data-remove-esi]').forEach(x=>x.addEventListener('click',e=>{
    e.preventDefault();e.stopPropagation();linkedDraft.delete(String(x.dataset.removeEsi));renderPanel();
  }));
}

function bindPanelEvents(){
  bindSelectedEvents();
  const input=document.getElementById('caisseArticlesSearchInput');
  const button=document.getElementById('caisseArticlesSearchBtn');
  if(button)button.addEventListener('click',searchArticles);
  if(input)input.addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();searchArticles()}});
}

async function searchArticles(){
  const input=document.getElementById('caisseArticlesSearchInput'),box=document.getElementById('caisseArticlesResults');
  const q=String(input&&input.value||'').trim();
  if(!q){showNotice('Saisis un mot, un client / prêteur, une description, une référence ou un N° dossier.');return}
  if(box){box.style.display='block';box.innerHTML='<div class="small" style="padding:12px">Recherche…</div>'}
  try{
    const r=await fetch('/api/articles/link-search?q='+encodeURIComponent(q),{cache:'no-store'}),d=await r.json();
    if(!r.ok)throw new Error(d.error||'Recherche impossible');
    linkedSearchResults=d.articles||[];renderSearchResults();
  }catch(e){if(box)box.innerHTML='<div class="small" style="padding:12px">'+escapeHtml(e.message||'Recherche impossible')+'</div>'}
}

function renderSearchResults(){
  const box=document.getElementById('caisseArticlesResults');if(!box)return;
  box.style.display='block';
  if(!linkedSearchResults.length){box.innerHTML='<div class="small" style="padding:12px">Aucun article trouvé.</div>';return}
  box.innerHTML='<div class="caisse-articles-result-head"><div></div><div>N° ESI</div><div>N° dossier</div><div>Référence</div><div>Client / description</div></div>'+linkedSearchResults.map(a=>{const infos=[a.client,a.description].filter(x=>String(x||'').trim()).map(x=>escapeHtml(x)).join('<br>');return `<label class="caisse-articles-result"><div><input type="checkbox" data-link-esi="${escapeHtml(a.esi_id)}" ${linkedDraft.has(String(a.esi_id))?'checked':''}></div><div class="esi-search">${escapeHtml(a.esi_id||'-')}</div><div class="dos">${escapeHtml(a.dossier||'-')}</div><div class="ref">${escapeHtml(a.reference||'-')}</div><div class="info-search">${infos||'-'}</div></label>`}).join('');
  box.querySelectorAll('[data-link-esi]').forEach(cb=>cb.addEventListener('change',()=>{
    const a=linkedSearchResults.find(x=>String(x.esi_id)===String(cb.dataset.linkEsi));
    if(!a)return;
    if(cb.checked)linkedDraft.set(String(a.esi_id),a);else linkedDraft.delete(String(a.esi_id));
    const selected=document.getElementById('caisseArticlesSelected');if(selected)selected.innerHTML=selectedHtml(true);
    bindSelectedEvents();
  }));
}

async function saveLinks(){
  const ticket=currentTicket();if(!isFicheCaisse(ticket))return true;
  const r=await fetch('/api/tickets/'+encodeURIComponent(ticket.id)+'/articles-lies',{
    method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({esi_ids:[...linkedDraft.keys()]})
  });
  const d=await r.json();if(!r.ok)throw new Error(d.error||'Impossible d’enregistrer les articles liés');
  linkedDraft.clear();(d.articles||[]).forEach(a=>linkedDraft.set(String(a.esi_id),a));
  return true;
}

function ensureDetailModal(){
  let bg=document.getElementById('linkedArticleDetailModal');if(bg)return bg;
  bg=document.createElement('div');bg.id='linkedArticleDetailModal';bg.className='caisse-article-modal-backdrop';
  bg.innerHTML=`<div class="caisse-article-modal"><div class="caisse-article-modal-head"><div><div class="caisse-article-modal-title">CARTE D'IDENTITÉ DE L'ARTICLE</div><div class="caisse-article-modal-sub" id="linkedArticleModalSub"></div></div><button class="caisse-article-modal-close" id="linkedArticleModalClose" type="button">×</button></div><div class="caisse-article-modal-body" id="linkedArticleModalBody"></div></div>`;
  document.body.appendChild(bg);
  document.getElementById('linkedArticleModalClose').onclick=()=>bg.classList.remove('show');
  bg.addEventListener('click',e=>{if(e.target===bg)bg.classList.remove('show')});
  return bg;
}
function detailField(k,v,wide=false){const val=String(v??'').trim()||'-';return `<div class="caisse-article-field ${wide?'wide':''}"><div class="k">${escapeHtml(k)}</div><div class="v">${escapeHtml(val)}</div></div>`}
function dateText(v){if(!v)return '-';const d=new Date(v);return isNaN(d.getTime())?String(v):d.toLocaleString('fr-FR')}
async function openLinkedArticleDetail(esi){
  const bg=ensureDetailModal(),body=document.getElementById('linkedArticleModalBody'),sub=document.getElementById('linkedArticleModalSub');
  bg.classList.add('show');sub.textContent=esi;body.innerHTML='<div class="small">Chargement…</div>';
  try{
    const r=await fetch('/api/articles/'+encodeURIComponent(esi),{cache:'no-store'}),d=await r.json();if(!r.ok)throw new Error(d.error||'Article introuvable');
    const a=d.article||{},dims=[a.longueur_cm,a.largeur_cm,a.hauteur_cm].filter(x=>String(x??'').trim()).join(' × '),photo=String(a.photo_url||'').trim();
    sub.textContent=(a.esi_id||esi)+' • Fiche détaillée et historique';
    let html=`<div class="caisse-article-hero"><div><div class="ref">${escapeHtml(a.reference||a.esi_id||esi)}</div>${a.description?`<div class="desc">${escapeHtml(a.description)}</div>`:''}</div><div class="esi">${escapeHtml(a.esi_id||esi)}</div></div>`;
    html+=`<div class="caisse-article-detail-layout"><div class="caisse-article-fields">${detailField('N° ESI',a.esi_id)}${detailField('N° dossier',a.dossier)}${detailField('Référence / inventaire',a.reference)}${detailField('Client',a.client)}${detailField('Projet / exposition',a.projet,true)}${detailField('Description / désignation',a.description,true)}${detailField('Dimensions',dims?dims+' cm':'-')}${detailField('Poids',a.poids_kg?String(a.poids_kg)+' kg':'-')}${detailField('Stockage actuel',a.lieu_stockage)}${detailField('Statut logistique',a.statut_logistique)}${detailField('N° Pre-Packing',a.dernier_colis)}${detailField('Type de Pre-Packing',a.type_colis==='Caisse bois'?'Packing bois':a.type_colis)}${detailField('Dernière réception',a.derniere_reception_ref)}</div><div class="caisse-article-photo">${photo?`<img src="${escapeHtml(photo)}" alt="Photo article">`:'<div class="caisse-article-no-photo">Aucune photo enregistrée</div>'}</div></div>`;
    html+='<div class="caisse-article-section"><div class="caisse-article-section-title">Historique des réceptions</div>';
    if((d.receptions||[]).length){
      html+='<div class="caisse-article-history">'+d.receptions.map(x=>`<div class="caisse-article-history-item"><strong>${escapeHtml(x.reference||'Réception')}</strong> · ${escapeHtml(x.date_affichee||dateText(x.date))}<br>Stockage : ${escapeHtml(x.lieu_stockage||'-')} · Pre-Packing : ${escapeHtml((x.colis||[]).join(', ')||'-')} · Type : ${escapeHtml(x.type_colis==='Caisse bois'?'Packing bois':(x.type_colis||'-'))}</div>`).join('')+'</div>';
    }else html+='<div class="small">Aucune réception enregistrée.</div>';
    html+='</div>';body.innerHTML=html;
  }catch(e){body.innerHTML='<div class="small">'+escapeHtml(e.message||'Impossible de charger la fiche article')+'</div>'}
}

// Rend le bloc a chaque affichage / passage en modification.
if(typeof renderDetail==='function'){
  const originalRenderDetail=renderDetail;
  renderDetail=async function(){const result=await originalRenderDetail.apply(this,arguments);await loadLinks(false);return result};
}

// Sauvegarde d'abord la selection, puis le ticket avec le bouton Enregistrer deja present.
const saveButton=document.getElementById('saveTicketBtn');
if(saveButton){
  let saving=false;
  saveButton.addEventListener('click',async function(e){
    const ticket=currentTicket();if(!state.editMode||!isFicheCaisse(ticket))return;
    e.preventDefault();e.stopImmediatePropagation();if(saving)return;saving=true;
    const oldText=saveButton.textContent;saveButton.disabled=true;saveButton.textContent='Enregistrement…';
    try{await saveLinks();await saveEditedTicket()}catch(err){showNotice(err.message||'Erreur enregistrement des articles liés')}finally{saving=false;saveButton.disabled=false;saveButton.textContent=oldText}
  },true);
}

// Cas ou la page a fini son premier rendu avant l'injection.
setTimeout(()=>{try{loadLinks(true)}catch(e){}},100);
})();"""


@app.route('/gestionnaire')
def gestionnaire():
    from flask import request, redirect, url_for
    if request.args.get('pwd') != '1234':
        return redirect(url_for('login'))

    # Injection inline volontaire : pas de fichier JS externe, donc pas de probleme de cache/404.
    page = render_template('gestionnaire.html')
    inline = '<script>' + GESTIONNAIRE_ARTICLES_LIES_JS + '</script>'
    if '</body>' in page:
        page = page.replace('</body>', inline + '\n</body>', 1)
    else:
        page += inline

    response = app.make_response(page)
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    return response


@app.route('/cleaner')
def cleaner_page():
    """Portail CLEANER - moteur documentaire transversal d'ESI TICKETS."""
    if request.args.get('pwd') != '1234':
        return redirect(url_for('login'))

    response = app.make_response(render_template('cleaner.html'))
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    return response


# -----------------------------------------------------------------------------
# CLEANER V3 - apprentissage par profils et règles, sans IA
# -----------------------------------------------------------------------------
def _cleaner_q(value):
    return urllib.parse.quote(_as_text(value).strip(), safe='-_:')


def _cleaner_db_state():
    """Vérifie si la migration Supabase CLEANER V3 a été appliquée."""
    try:
        supabase_rest_request('GET', 'cleaner_profiles', 'select=id&limit=1')
        supabase_rest_request('GET', 'cleaner_fields', 'select=id&limit=1')
        supabase_rest_request('GET', 'cleaner_rules', 'select=id&limit=1')
        return True, ''
    except Exception as e:
        return False, str(e)


def _cleaner_get_profiles(mode):
    safe_mode = _cleaner_q(mode)
    return supabase_rest_request(
        'GET', 'cleaner_profiles',
        f'select=*&mode=eq.{safe_mode}&active=eq.true&order=updated_at.desc&limit=500'
    ) or []


def _cleaner_get_fields(mode):
    safe_mode = _cleaner_q(mode)
    return supabase_rest_request(
        'GET', 'cleaner_fields',
        f'select=*&mode=eq.{safe_mode}&active=eq.true&order=position.asc,created_at.asc&limit=500'
    ) or []


def _cleaner_get_rules(mode):
    safe_mode = _cleaner_q(mode)
    return supabase_rest_request(
        'GET', 'cleaner_rules',
        f'select=*&mode=eq.{safe_mode}&active=eq.true&order=validated_count.desc,updated_at.desc&limit=2000'
    ) or []


def _cleaner_public_field(row):
    aliases = row.get('aliases')
    if not isinstance(aliases, list):
        aliases = []
    return {
        'id': _as_text(row.get('field_id')).strip(),
        'label': _as_text(row.get('label')).strip(),
        'sourceKey': _as_text(row.get('source_key')).strip(),
        'aliases': [_as_text(x).strip() for x in aliases if _as_text(x).strip()],
        'custom': bool(row.get('is_custom', True)),
        'position': row.get('position') or 100,
        'persisted': True,
    }


def _cleaner_apply_persistent_fields(rows, fields):
    """Copie les données techniques connues vers les champs personnalisés persistants."""
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        for field in fields or []:
            field_id = _as_text(field.get('field_id')).strip()
            source_key = _as_text(field.get('source_key')).strip()
            if not field_id or not source_key:
                continue
            if _as_text(row.get(field_id)).strip():
                continue
            value = row.get(source_key)
            if value is not None and _as_text(value).strip():
                row[field_id] = value
    return rows


def _cleaner_profile_key(mode, name):
    raw = re.sub(r'[^a-z0-9]+', '-', _as_text(name).casefold()).strip('-') or 'profil'
    return f'{_as_text(mode).strip().lower()}:{raw}'


def _cleaner_rule_upsert(mode, profile_key, target_field, source_alias='', source_key='', rule_type='header_alias'):
    target_field = _as_text(target_field).strip()
    source_alias = _as_text(source_alias).strip()
    source_key = _as_text(source_key).strip()
    profile_key = _as_text(profile_key).strip()
    if not target_field or (not source_alias and not source_key):
        return

    safe_mode = _cleaner_q(mode)
    safe_profile = _cleaner_q(profile_key)
    safe_target = _cleaner_q(target_field)
    safe_alias = _cleaner_q(source_alias)
    safe_source_key = _cleaner_q(source_key)
    query = (
        f'select=*&mode=eq.{safe_mode}&profile_key=eq.{safe_profile}'
        f'&target_field=eq.{safe_target}&source_alias=eq.{safe_alias}'
        f'&source_key=eq.{safe_source_key}&limit=1'
    )
    existing = supabase_rest_request('GET', 'cleaner_rules', query) or []
    now = datetime.now().isoformat()
    if existing:
        current = existing[0]
        patch = {
            'validated_count': int(current.get('validated_count') or 0) + 1,
            'active': True,
            'updated_at': now,
        }
        supabase_rest_request(
            'PATCH', 'cleaner_rules',
            'id=eq.' + _cleaner_q(current.get('id')), patch, prefer='return=minimal'
        )
        return

    payload = {
        'mode': _as_text(mode).strip().lower(),
        'profile_key': profile_key,
        'target_field': target_field,
        'source_alias': source_alias,
        'source_key': source_key,
        'rule_type': rule_type,
        'validated_count': 1,
        'active': True,
        'created_at': now,
        'updated_at': now,
    }
    supabase_rest_request('POST', 'cleaner_rules', '', [payload], prefer='return=minimal')


@app.route('/api/cleaner/config')
def api_cleaner_config():
    if request.args.get('pwd') != '1234':
        return jsonify({'ok': False, 'error': 'Accès refusé'}), 403
    mode = _as_text(request.args.get('mode') or 'douanes').strip().lower()
    ready, error = _cleaner_db_state()
    if not ready:
        return jsonify({
            'ok': True,
            'schema_ready': False,
            'schema_error': error,
            'fields': [], 'profiles': [], 'rules': [],
        })
    try:
        fields = [_cleaner_public_field(x) for x in _cleaner_get_fields(mode)]
        profiles = _cleaner_get_profiles(mode)
        rules = _cleaner_get_rules(mode)
        return jsonify({
            'ok': True,
            'schema_ready': True,
            'fields': fields,
            'profiles': [{
                'profile_key': x.get('profile_key'),
                'name': x.get('name'),
                'validated_count': x.get('validated_count') or 0,
                'updated_at': x.get('updated_at'),
            } for x in profiles],
            'rules': [{
                'profile_key': x.get('profile_key'),
                'target_field': x.get('target_field'),
                'source_alias': x.get('source_alias'),
                'source_key': x.get('source_key'),
                'validated_count': x.get('validated_count') or 0,
            } for x in rules],
        })
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/cleaner/fields/<mode>/<field_id>', methods=['DELETE'])
def api_cleaner_delete_field(mode, field_id):
    if request.args.get('pwd') != '1234':
        return jsonify({'ok': False, 'error': 'Accès refusé'}), 403
    ready, error = _cleaner_db_state()
    if not ready:
        return jsonify({'ok': False, 'error': 'Migration Supabase CLEANER V3 non installée', 'detail': error}), 503
    try:
        query = f'mode=eq.{_cleaner_q(mode)}&field_id=eq.{_cleaner_q(field_id)}'
        supabase_rest_request(
            'PATCH', 'cleaner_fields', query,
            {'active': False, 'updated_at': datetime.now().isoformat()}, prefer='return=minimal'
        )
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/cleaner/learn', methods=['POST'])
def api_cleaner_learn():
    """Valide une extraction et mémorise uniquement des règles de structure, jamais des valeurs métier."""
    if request.args.get('pwd') != '1234':
        return jsonify({'ok': False, 'error': 'Accès refusé'}), 403

    ready, error = _cleaner_db_state()
    if not ready:
        return jsonify({
            'ok': False,
            'error': 'La migration Supabase CLEANER V3 doit être appliquée avant le premier apprentissage.',
            'detail': error,
        }), 503

    data = request.get_json(silent=True) or {}
    mode = _as_text(data.get('mode') or 'douanes').strip().lower()
    fields = data.get('fields') or []
    documents = data.get('documents') or []
    rows = data.get('rows') or []
    if not rows:
        return jsonify({'ok': False, 'error': 'Aucune extraction à valider'}), 400

    now = datetime.now().isoformat()
    saved_fields = 0
    saved_profiles = 0
    learned_rules = 0

    try:
        # 1) Champs personnalisés de la trame.
        for position, field in enumerate(fields, 1):
            if not isinstance(field, dict) or not field.get('custom'):
                continue
            field_id = _as_text(field.get('id')).strip()
            label = _as_text(field.get('label')).strip()
            if not field_id or not label:
                continue
            aliases = field.get('aliases') if isinstance(field.get('aliases'), list) else []
            aliases = list(dict.fromkeys(_as_text(x).strip() for x in aliases if _as_text(x).strip()))
            payload = {
                'mode': mode,
                'field_id': field_id,
                'label': label,
                'source_key': _as_text(field.get('sourceKey') or field.get('source_key')).strip(),
                'aliases': aliases,
                'position': position,
                'is_custom': True,
                'active': True,
                'updated_at': now,
            }
            supabase_rest_request(
                'POST', 'cleaner_fields', 'on_conflict=mode,field_id', [payload],
                prefer='resolution=merge-duplicates,return=minimal'
            )
            saved_fields += 1

        # 2) Profils documentaires validés.
        profile_keys = []
        for doc in documents:
            if not isinstance(doc, dict):
                continue
            name = _as_text(doc.get('profile')).strip() or 'Profil CLEANER'
            profile_key = _as_text(doc.get('profile_key')).strip() or _cleaner_profile_key(mode, name)
            signature = doc.get('signature') if isinstance(doc.get('signature'), list) else []
            existing = supabase_rest_request(
                'GET', 'cleaner_profiles', f'select=*&profile_key=eq.{_cleaner_q(profile_key)}&limit=1'
            ) or []
            count = int(existing[0].get('validated_count') or 0) + 1 if existing else 1
            if existing:
                previous_signature = existing[0].get('signature')
                if isinstance(previous_signature, list):
                    signature = list(dict.fromkeys(
                        _as_text(x).strip() for x in (previous_signature + signature) if _as_text(x).strip()
                    ))[:160]
            payload = {
                'profile_key': profile_key,
                'mode': mode,
                'name': name,
                'signature': signature,
                'source_filename': _as_text(doc.get('filename')).strip(),
                'validated_count': count,
                'active': True,
                'updated_at': now,
            }
            supabase_rest_request(
                'POST', 'cleaner_profiles', 'on_conflict=profile_key', [payload],
                prefer='resolution=merge-duplicates,return=minimal'
            )
            profile_keys.append(profile_key)
            saved_profiles += 1

        # 3) Règles observées dans le document : en-tête source -> champ cible.
        #    source_headers vient du moteur d'extraction et ne contient pas les valeurs des œuvres.
        default_profile = profile_keys[0] if len(set(profile_keys)) == 1 and profile_keys else ''
        seen_rules = set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            source_headers = row.get('source_headers') if isinstance(row.get('source_headers'), dict) else {}
            row_profile_key = _as_text(row.get('_profile_key')).strip() or default_profile
            for target_field, source_alias in source_headers.items():
                key = (row_profile_key, _as_text(target_field).strip(), _as_text(source_alias).strip(), '')
                if not key[1] or not key[2] or key in seen_rules:
                    continue
                seen_rules.add(key)
                _cleaner_rule_upsert(mode, *key, rule_type='header_alias')
                learned_rules += 1

        # 4) Règles déclarées par les champs personnalisés.
        for field in fields:
            if not isinstance(field, dict) or not field.get('custom'):
                continue
            target = _as_text(field.get('id')).strip()
            source_key = _as_text(field.get('sourceKey') or field.get('source_key')).strip()
            aliases = field.get('aliases') if isinstance(field.get('aliases'), list) else []
            for profile_key in (profile_keys or ['']):
                if source_key:
                    key = (profile_key, target, '', source_key)
                    if key not in seen_rules:
                        seen_rules.add(key)
                        _cleaner_rule_upsert(mode, profile_key, target, source_key=source_key, rule_type='source_key')
                        learned_rules += 1
                for alias in aliases:
                    alias = _as_text(alias).strip()
                    key = (profile_key, target, alias, '')
                    if alias and key not in seen_rules:
                        seen_rules.add(key)
                        _cleaner_rule_upsert(mode, profile_key, target, source_alias=alias, rule_type='header_alias')
                        learned_rules += 1

        # 5) Historique léger : uniquement noms de fichiers, profils et nombre de lignes.
        #    Les valeurs Owner/Title/Value/etc. ne sont pas archivées par l'apprentissage.
        try:
            supabase_rest_request(
                'POST', 'cleaner_validations', '', [{
                    'mode': mode,
                    'profile_keys': list(dict.fromkeys(profile_keys)),
                    'source_files': [
                        _as_text(d.get('filename')).strip() for d in documents
                        if isinstance(d, dict) and _as_text(d.get('filename')).strip()
                    ],
                    'row_count': len(rows),
                    'created_at': now,
                }], prefer='return=minimal'
            )
        except Exception as history_error:
            print(f'[CLEANER HISTORY] {history_error}')

        return jsonify({
            'ok': True,
            'saved_fields': saved_fields,
            'saved_profiles': saved_profiles,
            'learned_rules': learned_rules,
            'message': 'Format validé. CLEANER a mémorisé la structure et les correspondances de champs.',
        })
    except Exception as e:
        print(f'[CLEANER LEARN] {e}')
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/cleaner/analyse', methods=['POST'])
def api_cleaner_analyse():
    """Analyse des documents depuis CLEANER, sans IA ni écriture métier automatique."""
    if request.args.get('pwd') != '1234':
        return jsonify({'ok': False, 'error': 'Accès refusé'}), 403

    mode = _as_text(request.form.get('mode')).strip().lower()
    files = request.files.getlist('files')
    if not files:
        return jsonify({'ok': False, 'error': 'Aucun document reçu'}), 400

    results = []
    errors = []
    persistent_fields = []
    learned_profiles = []
    learned_rules = []
    ready, schema_error = _cleaner_db_state()
    if ready:
        try:
            persistent_fields = _cleaner_get_fields(mode)
            learned_profiles = _cleaner_get_profiles(mode)
            learned_rules = _cleaner_get_rules(mode)
        except Exception as e:
            print(f'[CLEANER CONFIG] {e}')
    else:
        print(f'[CLEANER CONFIG] tables apprentissage indisponibles: {schema_error}')

    if mode == 'douanes':
        try:
            from cleaner_engine import extract_customs_document
        except Exception as e:
            return jsonify({'ok': False, 'error': f'Moteur CLEANER indisponible : {e}'}), 500

        for fs in files:
            filename = _as_text(fs.filename).strip() or 'document'
            content = fs.read()
            if not content:
                errors.append({'filename': filename, 'error': 'Fichier vide'})
                continue
            try:
                result = extract_customs_document(
                    filename, content,
                    learned_profiles=learned_profiles,
                    learned_rules=learned_rules,
                )
                profile_key = _as_text(result.get('profile_key')).strip()
                for row in result.get('rows') or []:
                    if isinstance(row, dict):
                        row['_profile_key'] = profile_key
                _cleaner_apply_persistent_fields(result.get('rows') or [], persistent_fields)
                results.append(result)
            except Exception as e:
                print(f'[CLEANER DOUANES] {filename}: {e}')
                errors.append({'filename': filename, 'error': str(e)})

        rows = [row for result in results for row in (result.get('rows') or [])]
        return jsonify({
            'ok': bool(rows) and not errors,
            'mode': mode,
            'row_count': len(rows),
            'rows': rows,
            'schema_ready': ready,
            'documents': [{
                'filename': x.get('filename'),
                'profile': x.get('profile'),
                'profile_key': x.get('profile_key'),
                'profile_confidence': x.get('profile_confidence', 0),
                'profile_source': x.get('profile_source'),
                'signature': x.get('signature') or [],
                'owner': x.get('owner'),
                'row_count': x.get('row_count', 0),
            } for x in results],
            'errors': errors,
        }), (200 if rows else 422)

    if mode in ('articles', 'packing'):
        try:
            from cleaner_engine import extract_articles_document
        except Exception as e:
            return jsonify({'ok': False, 'error': f'Moteur CLEANER ARTICLES indisponible : {e}'}), 500

        for fs in files:
            filename = _as_text(fs.filename).strip() or 'document'
            content = fs.read()
            if not content:
                errors.append({'filename': filename, 'error': 'Fichier vide'})
                continue
            try:
                result = extract_articles_document(
                    filename, content,
                    learned_profiles=learned_profiles,
                    learned_rules=learned_rules,
                )
                profile_key = _as_text(result.get('profile_key')).strip()
                if mode == 'packing' and profile_key.startswith('articles:'):
                    profile_key = 'packing:' + profile_key.split(':', 1)[1]
                    result['profile_key'] = profile_key
                prepared_rows = []
                for source_row in result.get('rows') or []:
                    if not isinstance(source_row, dict):
                        continue
                    row = dict(source_row)
                    row['_profile_key'] = profile_key
                    if mode == 'packing':
                        dims = _as_text(row.get('dimensions')).strip()
                        if not dims:
                            parts = [
                                _as_text(row.get('longueur_cm')).strip(),
                                _as_text(row.get('largeur_cm')).strip(),
                                _as_text(row.get('hauteur_cm')).strip(),
                            ]
                            if any(parts):
                                dims = ' × '.join(x or '-' for x in parts) + ' cm'
                        row['dimensions'] = dims
                        row['poids'] = _as_text(row.get('poids_brut_kg')).strip() or _as_text(row.get('poids_kg')).strip()
                        row['observation'] = _as_text(row.get('observation')).strip() or _as_text(row.get('zone')).strip()
                    prepared_rows.append(row)
                result['rows'] = prepared_rows
                result['row_count'] = len(prepared_rows)
                _cleaner_apply_persistent_fields(prepared_rows, persistent_fields)
                results.append(result)
            except Exception as e:
                print(f'[CLEANER {mode.upper()}] {filename}: {e}')
                errors.append({'filename': filename, 'error': str(e)})

        all_rows = [row for result in results for row in (result.get('rows') or [])]
        if not all_rows and results and not errors:
            errors.append({
                'filename': results[0].get('filename') or 'document',
                'error': "Aucune liste d'articles reconnue. CLEANER cherche les en-têtes et tableaux dans les documents."
            })
        return jsonify({
            'ok': bool(all_rows) and not errors,
            'mode': mode,
            'row_count': len(all_rows),
            'rows': all_rows,
            'schema_ready': ready,
            'documents': [{
                'filename': x.get('filename'),
                'profile': x.get('profile'),
                'profile_key': x.get('profile_key'),
                'profile_confidence': x.get('profile_confidence', 0),
                'profile_source': x.get('profile_source'),
                'signature': x.get('signature') or [],
                'row_count': x.get('row_count', 0),
                'sheets': x.get('sheets') or [],
            } for x in results],
            'errors': errors,
        }), (200 if all_rows else 422)

    if mode == 'enlevement':
        for fs in files:
            filename = _as_text(fs.filename).strip() or 'document'
            content = fs.read()
            if not content:
                errors.append({'filename': filename, 'error': 'Fichier vide'})
                continue
            if not filename.lower().endswith('.pdf'):
                errors.append({'filename': filename, 'error': "Le bon d'enlèvement doit être un PDF."})
                continue
            try:
                parsed = _extract_enlevement_pdf_preview_low_memory(content)
                row = {
                    'numero_dossier': parsed.get('numero_dossier') or '',
                    'client': parsed.get('client') or '', 'date': parsed.get('date_enlevement') or '',
                    'adresse_depart': parsed.get('adresse_depart') or '',
                    'adresse_destination': parsed.get('adresse_destination') or '',
                    'references': ', '.join(parsed.get('references') or []),
                    'instructions': parsed.get('instructions') or '',
                    'source_file': filename, 'source_page': '', 'source_headers': {},
                    '_profile_key': 'enlevement:bon-enlevement',
                    '_analysis': {k: v for k, v in parsed.items() if k != 'raw_text'},
                }
                _cleaner_apply_persistent_fields([row], persistent_fields)
                results.append({
                    'filename': filename, 'profile': "Bon d'enlèvement",
                    'profile_key': 'enlevement:bon-enlevement', 'profile_confidence': 1,
                    'profile_source': 'built-in', 'signature': [], 'row_count': 1, 'rows': [row],
                })
            except Exception as e:
                errors.append({'filename': filename, 'error': str(e)})
        all_rows = [row for result in results for row in (result.get('rows') or [])]
        return jsonify({
            'ok': bool(all_rows) and not errors, 'mode': mode, 'row_count': len(all_rows),
            'rows': all_rows, 'schema_ready': ready,
            'documents': [{k: x.get(k) for k in ('filename','profile','profile_key','profile_confidence','profile_source','signature','row_count')} for x in results],
            'errors': errors,
        }), (200 if all_rows else 422)

    if mode == 'reception':
        for fs in files:
            filename = _as_text(fs.filename).strip() or 'document'
            content = fs.read()
            if not content:
                errors.append({'filename': filename, 'error': 'Fichier vide'})
                continue
            if not filename.lower().endswith('.pdf'):
                errors.append({'filename': filename, 'error': 'Le document de réception doit être un PDF.'})
                continue
            try:
                parsed = _extract_reception_pdf(content)
                rows = []
                for ref in parsed.get('references') or []:
                    rows.append({
                        'numero_dossier': ref.get('dossier') or '', 'bl_br': parsed.get('bl_numero') or '',
                        'expediteur': '', 'date': parsed.get('bl_date') or '',
                        'reference': ref.get('numero_pdf') or ref.get('numero') or '',
                        'quantite': '1', 'colis': '', 'source_file': filename, 'source_page': '',
                        'source_headers': {}, '_profile_key': 'reception:bordereau',
                    })
                _cleaner_apply_persistent_fields(rows, persistent_fields)
                results.append({
                    'filename': filename, 'profile': 'Réception / bordereau',
                    'profile_key': 'reception:bordereau', 'profile_confidence': 1,
                    'profile_source': 'built-in', 'signature': [], 'row_count': len(rows), 'rows': rows,
                })
            except Exception as e:
                errors.append({'filename': filename, 'error': str(e)})
        all_rows = [row for result in results for row in (result.get('rows') or [])]
        return jsonify({
            'ok': bool(all_rows) and not errors, 'mode': mode, 'row_count': len(all_rows),
            'rows': all_rows, 'schema_ready': ready,
            'documents': [{k: x.get(k) for k in ('filename','profile','profile_key','profile_confidence','profile_source','signature','row_count')} for x in results],
            'errors': errors,
        }), (200 if all_rows else 422)

    return jsonify({'ok': False, 'error': "Ce type d'extraction est prêt dans l'interface mais son moteur n'est pas encore défini."}), 501



# -----------------------------------------------------------------------------
# CLEANER V3.3 - destinations métier après contrôle
# -----------------------------------------------------------------------------
def _cleaner_check_pwd():
    return request.args.get('pwd') == '1234'


def _cleaner_num(value, default=0.0):
    try:
        return float(_as_text(value).strip().replace(',', '.'))
    except Exception:
        return float(default)


def _cleaner_article_values(row):
    row = dict(row or {})
    longueur = _as_text(row.get('longueur_cm')).strip()
    largeur = _as_text(row.get('largeur_cm')).strip()
    hauteur = _as_text(row.get('hauteur_cm')).strip()
    if not any((longueur, largeur, hauteur)):
        dims = _as_text(row.get('dimensions')).strip()
        nums = re.findall(r"[-+]?\d+(?:[.,]\d+)?", dims)
        if len(nums) >= 3:
            longueur, largeur, hauteur = [x.replace(',', '.') for x in nums[:3]]
    return {
        'reference': _as_text(row.get('reference')).strip(),
        'description': _as_text(row.get('designation') or row.get('description') or row.get('title')).strip(),
        'longueur_cm': longueur,
        'largeur_cm': largeur,
        'hauteur_cm': hauteur,
        'poids_kg': _as_text(row.get('poids_kg') or row.get('poids')).strip(),
    }


def _cleaner_article_plan(rows):
    """Prépare un import en conservant la quantité totale des lignes identiques."""
    requested = {}
    row_data = []
    errors = []
    for index, row in enumerate(rows or [], 1):
        values = _cleaner_article_values(row)
        if not values['reference'] and not values['description']:
            errors.append({'ligne': index, 'error': 'Référence et désignation toutes les deux vides'})
            continue
        qty = _article_quantity((row or {}).get('quantite'), 1)
        sig = _article_import_signature(values)
        requested[sig] = requested.get(sig, 0) + qty
        row_data.append({'index': index, 'row': dict(row or {}), 'values': values, 'qty': qty, 'signature': sig})
    return requested, row_data, errors


def _cleaner_existing_counts():
    return _article_import_existing_counts()


def _cleaner_resolve_dossier_identity(dossier, client='', projet=''):
    identity = _article_dossier_identity(dossier)
    final_client = _as_text(identity.get('client')).strip() or _as_text(client).strip()
    final_projet = _as_text(identity.get('projet')).strip() or _as_text(projet).strip()
    return final_client, final_projet, identity


@app.route('/api/cleaner/articles/import', methods=['POST'])
def api_cleaner_import_articles():
    """Prévisualise puis importe le tableau contrôlé CLEANER dans la base Articles."""
    if not _cleaner_check_pwd():
        return jsonify({'ok': False, 'error': 'Accès refusé'}), 403
    data = request.get_json(silent=True) or {}
    rows = data.get('rows') or []
    dossier = _as_text(data.get('dossier')).strip()
    client = _as_text(data.get('client')).strip()
    projet = _as_text(data.get('projet')).strip()
    confirm_import = bool(data.get('confirm'))
    source_mode = _as_text(data.get('source_mode') or 'articles').strip().upper()

    if not dossier:
        return jsonify({'ok': False, 'error': 'Le N° dossier est obligatoire.'}), 400
    if not isinstance(rows, list) or not rows:
        return jsonify({'ok': False, 'error': 'Aucune ligne CLEANER à importer.'}), 400

    requested, row_data, errors = _cleaner_article_plan(rows)
    try:
        existing = _cleaner_existing_counts()
        final_client, final_projet, identity = _cleaner_resolve_dossier_identity(dossier, client, projet)
    except Exception as e:
        return jsonify({'ok': False, 'error': f'Impossible de préparer l’import : {e}'}), 500

    to_create_by_sig = {
        sig: max(0, qty - int(existing.get(sig, 0)))
        for sig, qty in requested.items()
    }
    requested_total = sum(requested.values())
    create_total = sum(to_create_by_sig.values())
    duplicate_total = requested_total - create_total

    preview = {
        'ok': not errors,
        'preview': True,
        'dossier': dossier,
        'client': final_client,
        'projet': final_projet,
        'identity_from_existing_dossier': bool(identity.get('client') or identity.get('projet')),
        'lignes_lues': len(row_data),
        'articles_demandes': requested_total,
        'articles_a_creer': create_total,
        'doublons_existants': duplicate_total,
        'lignes_invalides': len(errors),
        'errors': errors,
    }
    if not confirm_import:
        return jsonify(preview), (200 if row_data else 400)

    if errors:
        return jsonify({**preview, 'ok': False, 'error': 'Corrige les lignes invalides avant import.'}), 400

    remaining = dict(to_create_by_sig)
    stats = {
        'lignes_lues': len(row_data),
        'articles_demandes': requested_total,
        'articles_crees': 0,
        'doublons_ignores': duplicate_total,
        'esi_ids': [],
        'errors': [],
    }
    now = datetime.now().isoformat()

    with _ARTICLE_LOCK:
        for item in row_data:
            sig = item['signature']
            quota = remaining.get(sig, 0)
            if quota <= 0:
                continue
            create_for_row = min(item['qty'], quota)
            for unit_no in range(1, create_for_row + 1):
                values = item['values']
                source_row = item['row']
                payload = {
                    'ticket_id': None,
                    'source_module': f'CLEANER - {source_mode}',
                    'source_index': item['index'],
                    'unit_index': unit_no,
                    'reference': values['reference'],
                    'description': values['description'],
                    'dossier': dossier,
                    'client': final_client,
                    'projet': final_projet,
                    'ref_caisse': _as_text(source_row.get('numero_caisse') or source_row.get('colis')).strip(),
                    'transporteur_ref': '',
                    'longueur_cm': values['longueur_cm'],
                    'largeur_cm': values['largeur_cm'],
                    'hauteur_cm': values['hauteur_cm'],
                    'volume_m3': '',
                    'surface_m2': '',
                    'poids_kg': values['poids_kg'],
                    'lieu_stockage': '',
                    'statut_logistique': 'Créé',
                    'created_at': now,
                    'updated_at': now,
                    'raw_json': {
                        'source': 'cleaner',
                        'source_mode': source_mode,
                        'source_file': source_row.get('source_file') or '',
                        'source_sheet': source_row.get('source_sheet') or '',
                        'source_page': source_row.get('source_page') or '',
                        'quantity_source': item['qty'],
                        'unit_index': unit_no,
                        'cleaner_row': {k: v for k, v in source_row.items() if not str(k).startswith('_')},
                    },
                }
                payload['search_text'] = _article_search_text(payload)
                try:
                    article = _create_article_record(payload)
                    stats['articles_crees'] += 1
                    if article.get('esi_id'):
                        stats['esi_ids'].append(article['esi_id'])
                    remaining[sig] = max(0, remaining.get(sig, 0) - 1)
                except Exception as e:
                    stats['errors'].append({'ligne': item['index'], 'error': str(e)})
                    break

    try:
        if final_client or final_projet:
            _article_sync_dossier_identity(dossier, final_client or None, final_projet or None)
    except Exception as e:
        stats['errors'].append({'ligne': 0, 'error': f'Synchronisation dossier : {e}'})

    return jsonify({
        'ok': not stats['errors'],
        'preview': False,
        'dossier': dossier,
        'client': final_client,
        'projet': final_projet,
        **stats,
    }), (200 if not stats['errors'] else 207)


@app.route('/api/cleaner/douanes/save', methods=['POST'])
def api_cleaner_save_douanes():
    """Enregistre les lignes DOUANES validées dans une table métier dédiée."""
    if not _cleaner_check_pwd():
        return jsonify({'ok': False, 'error': 'Accès refusé'}), 403
    data = request.get_json(silent=True) or {}
    dossier = _as_text(data.get('dossier')).strip()
    rows = data.get('rows') or []
    if not dossier:
        return jsonify({'ok': False, 'error': 'Le N° dossier est obligatoire.'}), 400
    if not isinstance(rows, list) or not rows:
        return jsonify({'ok': False, 'error': 'Aucune ligne douanière à enregistrer.'}), 400

    try:
        existing_rows = supabase_rest_request(
            'GET', 'cleaner_douanes',
            f'select=record_hash&dossier=eq.{urllib.parse.quote(dossier, safe="")}&limit=10000'
        ) or []
    except Exception as e:
        msg = str(e)
        if 'cleaner_douanes' in msg or '42P01' in msg or 'does not exist' in msg.lower():
            return jsonify({
                'ok': False,
                'migration_required': True,
                'error': "La table Supabase 'cleaner_douanes' n'existe pas encore. Exécute le script SQL CLEANER V3.3 une seule fois dans DBeaver."
            }), 503
        return jsonify({'ok': False, 'error': msg}), 500

    existing_hashes = {_as_text(x.get('record_hash')).strip() for x in existing_rows}
    to_insert = []
    ignored = 0
    now = datetime.now().isoformat()
    technical = {'source_headers', 'source_value_number', '_profile_key', '_analysis'}
    for row in rows:
        if not isinstance(row, dict):
            continue
        clean_row = {k: v for k, v in row.items() if k not in technical and not str(k).startswith('_')}
        identity = {
            'owner': _as_text(row.get('owner')).strip(),
            'title': _as_text(row.get('title')).strip(),
            'medium': _as_text(row.get('medium')).strip(),
            'hs_code': _as_text(row.get('hs_code')).strip(),
            'license': _as_text(row.get('license')).strip(),
            'dimensions': _as_text(row.get('dimensions')).strip(),
            'value_usd': _as_text(row.get('value_usd')).strip(),
            'country_origin': _as_text(row.get('country_origin') or row.get('custom_pays_d_origine')).strip(),
            'source_file': _as_text(row.get('source_file')).strip(),
            'source_page': _as_text(row.get('source_page')).strip(),
        }
        record_hash = hashlib.sha1(
            json.dumps(identity, ensure_ascii=False, sort_keys=True).encode('utf-8')
        ).hexdigest()
        if record_hash in existing_hashes:
            ignored += 1
            continue
        existing_hashes.add(record_hash)
        to_insert.append({
            'dossier': dossier,
            **identity,
            'record_hash': record_hash,
            'raw_json': clean_row,
            'created_at': now,
            'updated_at': now,
        })

    created = 0
    if to_insert:
        try:
            inserted = supabase_rest_request(
                'POST', 'cleaner_douanes', '', to_insert, prefer='return=representation'
            ) or []
            created = len(inserted) if inserted else len(to_insert)
        except Exception as e:
            return jsonify({'ok': False, 'error': str(e)}), 500

    return jsonify({
        'ok': True,
        'dossier': dossier,
        'lignes_recues': len(rows),
        'lignes_enregistrees': created,
        'doublons_ignores': ignored,
    })


def _cleaner_attach_ticket_files(ticket_id, incoming_files):
    out = []
    for fs in incoming_files or []:
        if not fs or not fs.filename:
            continue
        content = fs.read()
        if not content:
            continue
        clean_name = safe_filename(fs.filename)
        storage_path = f"{ticket_id}/{datetime.now().strftime('%Y%m%d%H%M%S%f')}_{clean_name}"
        supabase_upload_bytes(storage_path, content, fs.content_type or 'application/octet-stream')
        out.append({'name': fs.filename, 'size': len(content), 'path': storage_path})
    return out


def _cleaner_reuse_existing_items(dossier, items):
    """Rattache des ESI déjà présents dans le même dossier avant de créer les manquants."""
    safe_dossier = urllib.parse.quote(_as_text(dossier).strip(), safe='')
    rows = supabase_rest_request(
        'GET', 'articles', f'select=*&dossier=eq.{safe_dossier}&limit=10000'
    ) or []
    by_sig = {}
    for article in rows:
        sig = _article_import_signature({
            'reference': article.get('reference'),
            'description': article.get('description'),
            'longueur_cm': article.get('longueur_cm'),
            'largeur_cm': article.get('largeur_cm'),
            'hauteur_cm': article.get('hauteur_cm'),
            'poids_kg': article.get('poids_kg'),
        })
        if sig.strip('|'):
            by_sig.setdefault(sig, []).append(_article_row_to_public(article))

    reused = 0
    used = set()
    prepared = []
    for source in items:
        item = dict(source or {})
        qty = _article_quantity(item.get('quantite'), 1)
        sig = _article_import_signature({
            'reference': item.get('reference'),
            'description': item.get('description'),
            'longueur_cm': item.get('longueur_cm'),
            'largeur_cm': item.get('largeur_cm'),
            'hauteur_cm': item.get('hauteur_cm'),
            'poids_kg': item.get('poids_kg'),
        })
        chosen = []
        for article in by_sig.get(sig, []):
            esi = _as_text(article.get('esi_id')).strip()
            if not esi or esi in used:
                continue
            chosen.append(esi)
            used.add(esi)
            if len(chosen) >= qty:
                break
        if chosen:
            item['esi_ids'] = chosen
            item['esi_id'] = chosen[0]
            reused += len(chosen)
        prepared.append(item)
    return prepared, reused



@app.route('/api/cleaner/reception/validate', methods=['POST'])
def api_cleaner_validate_reception():
    """Prévisualise puis valide une réception de caisses depuis les lignes corrigées dans CLEANER."""
    if not _cleaner_check_pwd():
        return jsonify({'ok': False, 'error': 'Accès refusé'}), 403
    localisation = _as_text(request.form.get('localisation')).strip()
    confirm_validation = _as_text(request.form.get('confirm')).strip().lower() in ('1', 'true', 'yes')
    try:
        rows = json.loads(request.form.get('rows') or '[]')
    except Exception:
        rows = []
    if not localisation:
        return jsonify({'ok': False, 'error': 'La localisation est obligatoire.'}), 400
    if not isinstance(rows, list) or not rows:
        return jsonify({'ok': False, 'error': 'Aucun Packing à réceptionner.'}), 400

    references = []
    seen = set()
    for row in rows:
        dossier = _as_text((row or {}).get('numero_dossier')).strip()
        numero_pdf = _as_text((row or {}).get('reference')).strip()
        numero = _normalise_numero_caisse(numero_pdf)
        key = (dossier, numero)
        if dossier and numero and key not in seen:
            seen.add(key)
            references.append({'dossier': dossier, 'numero': numero, 'numero_pdf': numero_pdf or numero})
    if not references:
        return jsonify({'ok': False, 'error': 'Aucune référence dossier/Packing exploitable.'}), 400

    matches = _match_reception_refs_to_tickets(references)
    found = [x for x in matches if x.get('found') and x.get('ticket_id')]
    preview = {
        'ok': bool(found),
        'preview': True,
        'found_count': len(found),
        'missing_count': len(matches) - len(found),
        'items': matches,
        'localisation': localisation,
    }
    if not confirm_validation:
        return jsonify(preview), (200 if found else 422)
    if not found:
        return jsonify({**preview, 'ok': False, 'error': 'Aucun Packing ESI TICKETS correspondant.'}), 422

    bl_numero = ''
    bl_date = ''
    for row in rows:
        if not bl_numero:
            bl_numero = _as_text((row or {}).get('bl_br')).strip()
        if not bl_date:
            bl_date = _as_text((row or {}).get('date')).strip()

    files = [fs for fs in request.files.getlist('files') if fs and fs.filename]
    if not files:
        return jsonify({'ok': False, 'error': 'Le PDF source est nécessaire pour rattacher le BL à la réception.'}), 400
    fs = files[0]
    content = fs.read()
    if not content:
        return jsonify({'ok': False, 'error': 'Le PDF source est vide.'}), 400
    pdf_hash = hashlib.sha256(content).hexdigest()[:16]
    safe_bl = safe_filename(bl_numero or 'sans_numero')
    bl_filename = fs.filename
    bl_storage_path = f"reception_bls/{safe_bl}_{pdf_hash}_{safe_filename(bl_filename)}"
    try:
        supabase_upload_bytes(bl_storage_path, content, fs.content_type or 'application/pdf')
    except Exception as e:
        return jsonify({'ok': False, 'error': f'Upload du BL impossible : {e}'}), 500

    updated = []
    errors = []
    receptionnee_le = datetime.now().isoformat()
    for match in found:
        ticket_id = _as_text(match.get('ticket_id')).strip()
        try:
            ticket = load_ticket(ticket_id)
            if not ticket or ticket.get('module') != 'Fiche de caisse':
                errors.append({'ticket_id': ticket_id, 'error': 'Fiche de Packing introuvable'})
                continue
            fiche = dict(ticket.get('fiche') or {})
            if not fiche:
                errors.append({'ticket_id': ticket_id, 'error': 'Fiche de Packing introuvable'})
                continue
            fiche['localisation'] = localisation
            ticket['fiche'] = fiche
            ticket['reception'] = {
                'receptionnee': True,
                'receptionnee_le': receptionnee_le,
                'localisation': localisation,
                'mode': 'cleaner_bl',
                'bl_numero': bl_numero,
                'bl_date': bl_date,
                'bl_storage_path': bl_storage_path,
                'bl_filename': bl_filename,
            }
            ticket['updatedAt'] = datetime.now().isoformat()
            supabase_rest_request(
                'PATCH', 'fiches', 'ticket_id=eq.' + urllib.parse.quote(ticket_id, safe=''),
                {'localisation': localisation}, prefer='return=minimal'
            )
            supabase_rest_request(
                'PATCH', 'tickets', 'id=eq.' + urllib.parse.quote(ticket_id, safe=''),
                {'updated_at': ticket['updatedAt'], 'raw_json': ticket}, prefer='return=minimal'
            )
            updated.append(ticket_id)
        except Exception as e:
            errors.append({'ticket_id': ticket_id, 'error': str(e)})

    return jsonify({
        'ok': not errors,
        'preview': False,
        'updated_count': len(updated),
        'updated': updated,
        'missing_count': len(matches) - len(found),
        'errors': errors,
        'bl_numero': bl_numero,
        'localisation': localisation,
    }), (200 if not errors else 207)

@app.route('/api/cleaner/reception/create', methods=['POST'])
def api_cleaner_create_reception():
    """Crée un avis de réception ESI TICKETS depuis une liste ARTICLES/PACKING contrôlée."""
    if not _cleaner_check_pwd():
        return jsonify({'ok': False, 'error': 'Accès refusé'}), 403

    dossier = _as_text(request.form.get('dossier')).strip()
    client = _as_text(request.form.get('client')).strip()
    projet = _as_text(request.form.get('projet')).strip()
    coordinateur = _as_text(request.form.get('coordinateur')).strip()
    date_reception = _as_text(request.form.get('date_reception')).strip()
    expediteur_nom = _as_text(request.form.get('expediteur_nom')).strip()
    transporteur_nom = _as_text(request.form.get('transporteur_nom')).strip()
    transporteur_ref = _as_text(request.form.get('transporteur_ref')).strip()
    commentaire = _as_text(request.form.get('commentaire')).strip()
    try:
        rows = json.loads(request.form.get('rows') or '[]')
    except Exception:
        rows = []

    missing = []
    if not dossier: missing.append('N° dossier')
    if not client: missing.append('Client')
    if not projet: missing.append('Projet')
    if not coordinateur: missing.append('Coordinateur')
    if not date_reception: missing.append('Date de réception prévue')
    if missing:
        return jsonify({'ok': False, 'error': 'Champ(s) obligatoire(s) : ' + ', '.join(missing)}), 400
    if not isinstance(rows, list) or not rows:
        return jsonify({'ok': False, 'error': 'Aucune marchandise à préparer.'}), 400

    items = []
    total_volume = total_surface = total_weight = 0.0
    total_qty = 0
    for row in rows:
        values = _cleaner_article_values(row)
        if not values['reference'] and not values['description']:
            continue
        qty = _article_quantity((row or {}).get('quantite'), 1)
        l = _cleaner_num(values['longueur_cm'])
        w = _cleaner_num(values['largeur_cm'])
        h = _cleaner_num(values['hauteur_cm'])
        weight = _cleaner_num(values['poids_kg'])
        volume = (l * w * h / 1000000.0) if l and w and h else 0.0
        surface = (l * w / 10000.0) if l and w else 0.0
        item = {
            'reference': values['reference'],
            'description': values['description'],
            'longueur_cm': values['longueur_cm'],
            'largeur_cm': values['largeur_cm'],
            'hauteur_cm': values['hauteur_cm'],
            'volume_m3': f'{volume:.3f}' if volume else '',
            'surface_m2': f'{surface:.3f}' if surface else '',
            'poids_kg': values['poids_kg'],
            'quantite': str(qty),
            'ref_caisse': _as_text((row or {}).get('numero_caisse') or (row or {}).get('colis')).strip(),
        }
        items.append(item)
        total_volume += volume * qty
        total_surface += surface * qty
        total_weight += weight * qty
        total_qty += qty

    if not items:
        return jsonify({'ok': False, 'error': 'Aucune ligne exploitable après contrôle.'}), 400

    try:
        items, reused_count = _cleaner_reuse_existing_items(dossier, items)
    except Exception as e:
        return jsonify({'ok': False, 'error': f'Contrôle des articles existants impossible : {e}'}), 500

    ticket_id = next_id('ARR')
    avis = {
        'dossier_ref': dossier,
        'client': client,
        'projet': projet,
        'date_reception_prevue': date_reception,
        'coordinateur': coordinateur,
        'expediteur': {'nom': expediteur_nom, 'adresse': '', 'contact': ''},
        'transporteur': {'nom': transporteur_nom, 'adresse': '', 'contact': '', 'reference': transporteur_ref},
        'items': items,
        'totaux': {
            'volume_m3': f'{total_volume:.3f}',
            'surface_m2': f'{total_surface:.3f}',
            'poids_kg': f'{total_weight:.2f}',
            'quantite': str(total_qty),
        },
        'commentaire': commentaire,
        'cleaner_source': True,
    }
    ticket = {
        'id': ticket_id,
        'module': "Avis d'arrivée",
        'status': 'Demande créée',
        'createdAt': datetime.now().isoformat(),
        'dossier': dossier,
        'ref': transporteur_ref or '-',
        'preteur': expediteur_nom or '-',
        'expo': projet,
        'objet': projet,
        'chargeProjet': coordinateur,
        'typeCaisse': '-',
        'dimensions': '-',
        'dateEmballage': date_reception,
        'prixDevis': '-',
        'dateRdv': '-', 'heureRdv': '-', 'lieuRdv': '-', 'contactRdv': '-',
        'commentaire': commentaire,
        'files': [], 'managerSheets': [],
        'avisArrivee': avis,
    }

    try:
        ticket_folder(ticket_id)
        ticket['files'] = _cleaner_attach_ticket_files(ticket_id, request.files.getlist('files'))
        save_ticket(ticket)
        created = _ensure_articles_for_ticket(ticket, save=True)
        return jsonify({
            'ok': True,
            'id': ticket_id,
            'items': len(items),
            'articles_reutilises': reused_count,
            'articles_crees': len(created),
        }), 201
    except Exception as e:
        print(f'[CLEANER RECEPTION] {e}')
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/cleaner/enlevement/create', methods=['POST'])
def api_cleaner_create_enlevement():
    """Crée un ticket d'enlèvement depuis l'analyse déjà contrôlée dans CLEANER."""
    if not _cleaner_check_pwd():
        return jsonify({'ok': False, 'error': 'Accès refusé'}), 403

    dossier = _as_text(request.form.get('dossier')).strip()
    client = _as_text(request.form.get('client')).strip()
    projet = _as_text(request.form.get('projet')).strip()
    coordinateur = _as_text(request.form.get('coordinateur')).strip()
    try:
        analysis = json.loads(request.form.get('analysis') or '{}')
    except Exception:
        analysis = {}
    if not dossier:
        return jsonify({'ok': False, 'error': 'Le N° dossier est obligatoire.'}), 400
    if not isinstance(analysis, dict) or not analysis:
        return jsonify({'ok': False, 'error': "L'analyse du bon d'enlèvement est manquante."}), 400

    clean_analysis = dict(analysis)
    clean_analysis.pop('raw_text', None)
    clean_analysis['numero_dossier'] = dossier
    if client: clean_analysis['client'] = client
    if projet: clean_analysis['exhibition'] = projet
    if coordinateur: clean_analysis['coordinateur'] = coordinateur
    clean_analysis['display_name'] = ' - '.join(x for x in [clean_analysis.get('client'), clean_analysis.get('numero_bon')] if x)
    clean_analysis['analysis_status'] = 'ready'
    clean_analysis['analysis_error'] = ''
    clean_analysis['analysed_at'] = datetime.now().isoformat()

    # Réutilise automatiquement les ESI dont la référence correspond exactement,
    # en privilégiant ceux du même dossier. Les unités manquantes seront créées.
    used_esi = set()
    reused_count = 0
    items = []
    for source in clean_analysis.get('items') or []:
        item = dict(source or {})
        qty = _article_quantity(item.get('quantite'), 1)
        candidates = _find_existing_articles_for_reference(item.get('reference'), limit=max(25, qty * 10))
        candidates.sort(key=lambda x: 0 if _as_text(x.get('dossier')).strip() == dossier else 1)
        chosen = []
        for candidate in candidates:
            esi = _as_text(candidate.get('esi_id')).strip()
            if not esi or esi in used_esi:
                continue
            chosen.append(esi)
            used_esi.add(esi)
            if len(chosen) >= qty:
                break
        if chosen:
            item['esi_ids'] = chosen
            item['esi_id'] = chosen[0]
            reused_count += len(chosen)
        item.pop('article_candidates', None)
        item.pop('source_index', None)
        items.append(item)
    clean_analysis['items'] = items
    clean_analysis['references'] = [_as_text(x.get('reference')).strip() for x in items if _as_text(x.get('reference')).strip()]

    ticket_id = next_id('ENL')
    date_rdv = '-'
    raw_date = _as_text(clean_analysis.get('date_enlevement')).strip()
    if raw_date:
        for fmt in ('%d/%m/%Y', '%Y-%m-%d'):
            try:
                date_rdv = datetime.strptime(raw_date, fmt).strftime('%Y-%m-%d')
                break
            except Exception:
                pass

    ticket = {
        'id': ticket_id,
        'module': "Demande d'enlèvement",
        'status': 'Demande créée',
        'createdAt': datetime.now().isoformat(),
        'dossier': dossier,
        'numeroDossier': dossier,
        'ref': _as_text(clean_analysis.get('numero_bon')).strip() or '-',
        'preteur': '-',
        'expo': projet or _as_text(clean_analysis.get('exhibition')).strip() or '-',
        'objet': projet or _as_text(clean_analysis.get('exhibition')).strip() or '-',
        'chargeProjet': coordinateur or _as_text(clean_analysis.get('coordinateur')).strip() or '-',
        'typeCaisse': '-', 'dimensions': '-', 'dateEmballage': '-', 'prixDevis': '-',
        'dateRdv': date_rdv, 'heureRdv': '-', 'lieuRdv': '-', 'contactRdv': '-',
        'commentaire': '', 'files': [], 'managerSheets': [],
        'enlevement': clean_analysis,
    }

    try:
        ticket_folder(ticket_id)
        ticket['files'] = _cleaner_attach_ticket_files(ticket_id, request.files.getlist('files'))
        save_ticket(ticket)
        created = _ensure_articles_for_ticket(ticket, save=True)
        return jsonify({
            'ok': True,
            'id': ticket_id,
            'articles_reutilises': reused_count,
            'articles_crees': len(created),
            'references': len(clean_analysis.get('references') or []),
        }), 201
    except Exception as e:
        print(f'[CLEANER ENLEVEMENT] {e}')
        return jsonify({'ok': False, 'error': str(e)}), 500

@app.route('/api/cleaner/export-excel', methods=['POST'])
def api_cleaner_export_excel():
    """Exporte exactement le tableau contrôlé/modifié dans CLEANER."""
    if request.args.get('pwd') != '1234':
        return jsonify({'ok': False, 'error': 'Accès refusé'}), 403

    data = request.get_json(silent=True) or {}
    fields = data.get('fields') or []
    rows = data.get('rows') or []
    mode = _as_text(data.get('mode') or 'cleaner').strip().upper()

    if not isinstance(fields, list) or not fields:
        return jsonify({'ok': False, 'error': 'Aucun champ à exporter'}), 400
    if not isinstance(rows, list) or not rows:
        return jsonify({'ok': False, 'error': 'Aucune ligne à exporter'}), 400

    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill, Alignment
        from openpyxl.utils import get_column_letter

        wb = Workbook()
        ws = wb.active
        ws.title = 'CLEANER'
        ws.freeze_panes = 'A2'
        ws.sheet_view.showGridLines = False

        labels = [_as_text(f.get('label')).strip() or _as_text(f.get('id')).strip() for f in fields]
        for col, label in enumerate(labels, 1):
            cell = ws.cell(1, col, label)
            cell.font = Font(bold=True, color='FFFFFF')
            cell.fill = PatternFill('solid', fgColor='075985')
            cell.alignment = Alignment(horizontal='center', vertical='center')

        for r_idx, row in enumerate(rows, 2):
            for c_idx, field in enumerate(fields, 1):
                key = _as_text(field.get('id')).strip()
                value = row.get(key, '') if isinstance(row, dict) else ''
                ws.cell(r_idx, c_idx, value)

        for c_idx, label in enumerate(labels, 1):
            max_len = len(label)
            for r_idx in range(2, min(ws.max_row, 250) + 1):
                max_len = max(max_len, len(_as_text(ws.cell(r_idx, c_idx).value)))
            ws.column_dimensions[get_column_letter(c_idx)].width = min(max(max_len + 2, 12), 45)

        output = BytesIO()
        wb.save(output)
        output.seek(0)
        stamp = datetime.now().strftime('%Y%m%d_%H%M')
        return send_file(
            output, as_attachment=True, download_name=f'CLEANER_{mode}_{stamp}.xlsx',
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        )
    except Exception as e:
        print(f'[CLEANER EXPORT] {e}')
        return jsonify({'ok': False, 'error': str(e)}), 500


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


@app.route('/gestion-reception')
def gestion_reception():
    return render_template('gestion_reception.html')

@app.route('/api/status')
def api_status():
    root = ensure_shared_root()
    return jsonify({'shared_path': str(root), 'mode': 'automatic_app_folder'})

@app.route('/api/tickets')
def api_tickets():
    status = request.args.get('status')
    limit = request.args.get('limit')

    # Migration historique : toutes les caisses antérieures au 18/08/2026
    # sont considérées comme réceptionnées.
    migrate_caisses_avant_18_aout_2026()

    tickets = list_tickets(status=status, limit=limit)

    # Expose un indicateur logistique normalisé pour le planning.
    # Cela ne modifie jamais le statut métier du ticket.
    for ticket in tickets:
        if ticket.get('module') == 'Fiche de caisse':
            reception = dict(ticket.get('reception') or {})
            reception['receptionnee'] = _is_caisse_receptionnee(ticket)
            ticket['reception'] = reception

        # Indicateur explicite utilisé par le planning Gestion réception.
        # Il fonctionne aussi pour les anciennes réceptions déjà enregistrées.
        ticket['receptionValidee'] = _ticket_reception_validee(ticket)

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
        return jsonify({'ok': False, 'error': 'Fiche de Packing introuvable'}), 404

    data = request.get_json(silent=True) or {}
    localisation = _as_text(data.get('localisation')).strip()

    fiche['localisation'] = localisation
    ticket['fiche'] = fiche
    ticket['updatedAt'] = datetime.now().isoformat()

    # Une localisation renseignée acte une réception manuelle.
    # IMPORTANT : la réception est indépendante du statut métier du ticket.
    reception = dict(ticket.get('reception') or {})
    if localisation:
        reception['receptionnee'] = True
        reception.setdefault('receptionnee_le', datetime.now().isoformat())
        reception.setdefault('mode', 'manuel')
        reception['localisation'] = localisation
        ticket['reception'] = reception
    else:
        # Effacer la localisation annule uniquement l'information de réception.
        # Le statut du ticket (Demande créée / En cours / Terminé) n'est jamais modifié ici.
        ticket.pop('reception', None)
        reception = {}

    try:
        supabase_rest_request(
            "PATCH",
            "fiches",
            "ticket_id=eq." + urllib.parse.quote(ticket_id, safe=''),
            {"localisation": localisation},
            prefer="return=minimal"
        )
        # raw_json conserve les métadonnées de réception sans nouvelle colonne Supabase.
        supabase_rest_request(
            "PATCH",
            "tickets",
            "id=eq." + urllib.parse.quote(ticket_id, safe=''),
            {"updated_at": ticket['updatedAt'], "raw_json": ticket},
            prefer="return=minimal"
        )
        try:
            _ensure_packing_article_record(ticket)
        except Exception as sync_error:
            print(f"[PACKING BASE ARTICLES] Mise à jour localisation impossible pour {ticket_id}: {sync_error}")
        return jsonify({
            'ok': True,
            'localisation': localisation,
            'reception': reception,
            'receptionnee': _is_caisse_receptionnee(ticket),
            'status': ticket.get('status')
        })
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

        # Le PDF analysé est conservé dans Supabase afin de pouvoir le rouvrir
        # depuis la fiche de la caisse après validation de la réception.
        pdf_hash = hashlib.sha256(content).hexdigest()[:16]
        bl_numero = parsed.get('bl_numero', '') or 'sans_numero'
        bl_filename = safe_filename(fs.filename or f"BL_{bl_numero}.pdf")
        bl_storage_path = f"reception_bls/{safe_filename(bl_numero)}_{pdf_hash}_{bl_filename}"
        supabase_upload_bytes(bl_storage_path, content, "application/pdf")

        return jsonify({
            'ok': True,
            'bl_numero': parsed.get('bl_numero', ''),
            'bl_date': parsed.get('bl_date', ''),
            'bl_storage_path': bl_storage_path,
            'bl_filename': fs.filename or bl_filename,
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
    """Valide en lot la réception et rattache le BL fournisseur à chaque caisse."""
    data = request.get_json(silent=True) or {}
    ticket_ids = data.get('ticket_ids') or []
    localisation = _as_text(data.get('localisation')).strip()
    bl_numero = _as_text(data.get('bl_numero')).strip()
    bl_date = _as_text(data.get('bl_date')).strip()
    bl_storage_path = _as_text(data.get('bl_storage_path')).strip()
    bl_filename = _as_text(data.get('bl_filename')).strip()

    if not localisation:
        return jsonify({
            'ok': False,
            'error': 'La localisation est obligatoire pour valider la réception.'
        }), 400

    if not isinstance(ticket_ids, list) or not ticket_ids:
        return jsonify({'ok': False, 'error': 'Aucun Packing sélectionné'}), 400

    if not bl_storage_path:
        return jsonify({'ok': False, 'error': 'Le PDF du BL analysé est manquant.'}), 400

    updated = []
    errors = []
    receptionnee_le = datetime.now().isoformat()

    for ticket_id in dict.fromkeys(_as_text(x).strip() for x in ticket_ids if _as_text(x).strip()):
        try:
            ticket = load_ticket(ticket_id)
            if not ticket:
                errors.append({'ticket_id': ticket_id, 'error': 'Ticket introuvable'})
                continue

            if ticket.get('module') != 'Fiche de caisse':
                errors.append({'ticket_id': ticket_id, 'error': 'Ce ticket n’est pas une fiche de Packing'})
                continue

            fiche = ticket.get('fiche') or {}
            if not fiche:
                errors.append({'ticket_id': ticket_id, 'error': 'Fiche de Packing introuvable'})
                continue

            fiche['localisation'] = localisation
            ticket['fiche'] = fiche
            ticket['reception'] = {
                'receptionnee': True,
                'receptionnee_le': receptionnee_le,
                'localisation': localisation,
                'mode': 'bl_fournisseur',
                'bl_numero': bl_numero,
                'bl_date': bl_date,
                'bl_storage_path': bl_storage_path,
                'bl_filename': bl_filename,
            }
            # La réception logistique ne modifie pas le statut métier du ticket.
            ticket['updatedAt'] = datetime.now().isoformat()

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
                {"updated_at": ticket['updatedAt'], "raw_json": ticket},
                prefer="return=minimal"
            )
            try:
                _ensure_packing_article_record(ticket)
            except Exception as sync_error:
                print(f"[PACKING BASE ARTICLES] Mise à jour réception impossible pour {ticket_id}: {sync_error}")

            updated.append(ticket_id)

        except Exception as e:
            print(f"[RECEPTION PDF] Erreur validation {ticket_id}: {e}")
            errors.append({'ticket_id': ticket_id, 'error': str(e)})

    return jsonify({
        'ok': len(errors) == 0,
        'updated_count': len(updated),
        'updated': updated,
        'errors': errors,
        'localisation': localisation,
        'bl_numero': bl_numero,
        'receptionnee_le': receptionnee_le,
    }), (200 if not errors else 207)


@app.route('/api/tickets/<ticket_id>/reception-bl')
def api_ticket_reception_bl(ticket_id):
    """Ouvre le BL fournisseur associé à la réception d'une caisse."""
    ticket = load_ticket(ticket_id)
    if not ticket:
        abort(404)

    reception = ticket.get('reception') or {}
    storage_path = _as_text(reception.get('bl_storage_path')).strip()
    filename = _as_text(reception.get('bl_filename')).strip() or (
        f"BL_{_as_text(reception.get('bl_numero')).strip() or ticket_id}.pdf"
    )
    if not storage_path:
        abort(404)

    try:
        signed_url = supabase_signed_download_url(storage_path, expires_in=300)
        return redirect(signed_url)
    except Exception as e:
        print(f"[RECEPTION BL] URL signée impossible, fallback Render : {e}")
        try:
            data = supabase_download_bytes(storage_path)
        except Exception:
            abort(404)
        return send_file(BytesIO(data), mimetype='application/pdf', download_name=filename)


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
        "Avis d'arrivée": 'ARR',
        'Mise en caisse': 'MEC',
    }
    prefix = prefixes.get(module, 'AV')

    incoming_files = [fs for fs in request.files.getlist('files') if fs and fs.filename]
    is_enlevement = module in ("Demande d'enlèvement", "Demande d'enlevement")
    is_avis_arrivee = module == "Avis d'arrivée"
    is_mise_en_caisse = module == 'Mise en caisse'
    avis_arrivee = None
    mise_en_caisse = None
    enlevement_analyse = None
    article_selections = []
    enlevement_dossier = _as_text(form.get('numeroDossier') or form.get('dossier')).strip() if is_enlevement else ''
    enlevement_client = _as_text(form.get('enlevementClient')).strip() if is_enlevement else ''
    enlevement_projet = _as_text(form.get('enlevementProjet')).strip() if is_enlevement else ''
    enlevement_coordinateur = _as_text(form.get('enlevementCoordinateur') or form.get('chargeProjet')).strip() if is_enlevement else ''
    if is_enlevement:
        raw_analysis = form.get('enlevementAnalyse', '')
        raw_selections = form.get('articleSelections', '')
        if raw_analysis:
            try:
                enlevement_analyse = json.loads(raw_analysis)
            except (json.JSONDecodeError, TypeError, ValueError):
                return jsonify({'ok': False, 'error': "Analyse du bon d'enlèvement invalide. Relance l'analyse du PDF."}), 400
            if not isinstance(enlevement_analyse, dict):
                return jsonify({'ok': False, 'error': "Analyse du bon d'enlèvement invalide."}), 400
        if raw_selections:
            try:
                article_selections = json.loads(raw_selections)
            except (json.JSONDecodeError, TypeError, ValueError):
                return jsonify({'ok': False, 'error': 'Sélection des articles invalide.'}), 400
            if not isinstance(article_selections, list):
                return jsonify({'ok': False, 'error': 'Sélection des articles invalide.'}), 400
    if is_mise_en_caisse:
        raw_mise = form.get('miseEnCaisse', '')
        try:
            payload_mise = json.loads(raw_mise) if raw_mise else {}
        except (json.JSONDecodeError, TypeError, ValueError):
            return jsonify({'ok': False, 'error': 'Données de mise en caisse invalides.'}), 400
        try:
            mise_en_caisse = _validate_mise_en_caisse_request(payload_mise)
        except ValueError as e:
            return jsonify({'ok': False, 'error': str(e)}), 400
        except Exception as e:
            print(f"[MISE EN CAISSE] Validation impossible: {e}")
            return jsonify({'ok': False, 'error': str(e)}), 500

    if is_avis_arrivee:
        raw_avis = form.get('avisArrivee', '')
        try:
            avis_arrivee = json.loads(raw_avis) if raw_avis else {}
        except (json.JSONDecodeError, TypeError, ValueError):
            return jsonify({'ok': False, 'error': "Données de l'avis d'arrivée invalides."}), 400
        if not isinstance(avis_arrivee, dict):
            return jsonify({'ok': False, 'error': "Données de l'avis d'arrivée invalides."}), 400

        required = {
            'dossier_ref': 'Réf. dossier',
            'client': 'Nom du client',
            'projet': 'Projet ou expo',
            'date_reception_prevue': 'Date de réception prévue',
            'coordinateur': 'Nom du coordinateur',
        }
        missing = [label for key, label in required.items() if not _as_text(avis_arrivee.get(key)).strip()]
        if missing:
            return jsonify({'ok': False, 'error': 'Champ(s) obligatoire(s) manquant(s) : ' + ', '.join(missing)}), 400
        if not isinstance(avis_arrivee.get('items'), list) or not avis_arrivee.get('items'):
            return jsonify({'ok': False, 'error': "Ajoute au moins une ligne de marchandise."}), 400

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
    ticket_dossier = form.get('dossier','')
    ticket_ref = form.get('ref','')
    if module == 'Fiche de caisse':
        ticket_ref = _packing_local_number(ticket_dossier, ticket_ref)

    ticket = {
        'id': ticket_id,
        'module': module,
        'status': 'Demande créée',
        'createdAt': datetime.now().isoformat(),
        'dossier': ticket_dossier,
        'ref': ticket_ref,
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
        'typeVisite': form.get('typeVisite','-') or '-',
        'lieuRdv': form.get('lieuRdv','-') or '-',
        'contactRdv': form.get('contactRdv','-') or '-',
        'commentaire': form.get('commentaire',''),
        'files': [],
        'managerSheets': []
    }

    if is_enlevement and enlevement_dossier:
        # Pour les nouveaux bons d'enlèvement, dossier contient bien le N° dossier.
        # numeroDossier reste aussi dans raw_json pour les écrans métier spécialisés.
        ticket['dossier'] = enlevement_dossier
        ticket['numeroDossier'] = enlevement_dossier

    if is_enlevement:
        if enlevement_analyse:
            clean_analysis = dict(enlevement_analyse)
            for item in clean_analysis.get('items') or []:
                if isinstance(item, dict):
                    item.pop('article_candidates', None)
                    item.pop('source_index', None)
            clean_analysis['analysis_status'] = 'ready'
            clean_analysis['analysis_error'] = ''
            clean_analysis['analysed_at'] = datetime.now().isoformat()

            # Le N° dossier et les champs automatiquement rattachés au dossier
            # sont prioritaires. L'analyse PDF conserve les autres informations du bon.
            if enlevement_dossier:
                clean_analysis['numero_dossier'] = enlevement_dossier
            if enlevement_client:
                clean_analysis['client'] = enlevement_client
            if enlevement_projet:
                clean_analysis['exhibition'] = enlevement_projet
            if enlevement_coordinateur:
                clean_analysis['coordinateur'] = enlevement_coordinateur
            clean_analysis['display_name'] = " - ".join(
                x for x in [
                    _as_text(clean_analysis.get('client')).strip(),
                    _as_text(clean_analysis.get('numero_bon')).strip()
                ] if x
            )
            ticket['enlevement'] = clean_analysis

            if enlevement_dossier:
                ticket['dossier'] = enlevement_dossier
                ticket['numeroDossier'] = enlevement_dossier
            elif clean_analysis.get('client'):
                # Compatibilité avec les anciens formulaires qui ne fournissent pas de N° dossier.
                ticket['dossier'] = clean_analysis['client']
            if clean_analysis.get('numero_bon'):
                ticket['ref'] = clean_analysis['numero_bon']
            if clean_analysis.get('coordinateur'):
                ticket['chargeProjet'] = clean_analysis['coordinateur']
            if clean_analysis.get('exhibition'):
                ticket['expo'] = clean_analysis['exhibition']
                ticket['objet'] = clean_analysis['exhibition']
            if clean_analysis.get('date_enlevement'):
                try:
                    dt = datetime.strptime(clean_analysis['date_enlevement'], '%d/%m/%Y')
                    ticket['dateRdv'] = dt.strftime('%Y-%m-%d')
                except ValueError:
                    pass

            _apply_enlevement_article_selections(ticket, article_selections)
        else:
            ticket['enlevement'] = {
                'analysis_status': 'pending',
                'analysis_error': '',
                'numero_dossier': enlevement_dossier,
                'client': enlevement_client,
                'exhibition': enlevement_projet,
                'coordinateur': enlevement_coordinateur,
                'items': [],
                'references': [],
            }

    if is_avis_arrivee:
        # Les données spécifiques restent dans raw_json : aucune nouvelle colonne Supabase n'est nécessaire.
        # Les champs historiques ci-dessus restent remplis pour que l'avis apparaisse dans les listes existantes.
        ticket['avisArrivee'] = avis_arrivee

    if is_mise_en_caisse and mise_en_caisse:
        ticket['miseEnCaisse'] = mise_en_caisse
        ticket['dossier'] = mise_en_caisse.get('dossier') or ticket['dossier']
        ticket['ref'] = mise_en_caisse.get('packing_reference') or ticket['ref']
        ticket['preteur'] = mise_en_caisse.get('client') or ticket['preteur']
        ticket['expo'] = mise_en_caisse.get('projet') or ticket['expo']
        ticket['objet'] = mise_en_caisse.get('projet') or ticket['objet']
        ticket['chargeProjet'] = mise_en_caisse.get('charge_projet') or ticket['chargeProjet']
        ticket['dateEmballage'] = mise_en_caisse.get('date_souhaitee') or ticket['dateEmballage']
        ticket['commentaire'] = mise_en_caisse.get('commentaire') or ticket['commentaire']

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

    # Attribue immédiatement un identifiant ESI à chaque unité d'un avis d'arrivée.
    # Le ticket est d'abord sauvegardé afin que les références Supabase soient cohérentes.
    save_ticket(ticket)
    if is_avis_arrivee:
        try:
            _ensure_articles_for_ticket(ticket, save=True)
        except Exception as e:
            print(f"[ARTICLES] Attribution ESI impossible pour {ticket_id}: {e}")
            return jsonify({
                'ok': False,
                'error': "Le ticket a été créé, mais l'attribution des numéros ESI a échoué. "
                         "Vérifie que la table Supabase 'articles' a bien été créée."
            }), 500

    if is_enlevement and enlevement_analyse:
        try:
            _ensure_articles_for_ticket(ticket, save=True)
        except Exception as e:
            print(f"[ARTICLES] Rattachement/création ESI impossible pour {ticket_id}: {e}")
            return jsonify({
                'ok': False,
                'error': "Le ticket a été créé, mais le rattachement des articles a échoué. " + str(e)
            }), 500
    elif is_enlevement and enlevement_pdf_bytes:
        # Compatibilité avec les anciennes pages : sans pré-analyse, on conserve l'ancien fonctionnement.
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
        'analysis_status': ('ready' if is_enlevement and enlevement_analyse else ('pending' if is_enlevement else None))
    })


def _reception_qty_int(value, default=0):
    try:
        return max(0, int(float(str(value).replace(',', '.'))))
    except Exception:
        return default


def _rebuild_ticket_after_reception_cancel(ticket, is_avis):
    """Reconstruit les quantités et états articles uniquement depuis les réceptions encore actives."""
    if is_avis:
        container = dict(ticket.get('avisArrivee') or ticket.get('avis_arrivee') or {})
        active = list(ticket.get('receptionsAvisArrivee') or [])
        container_key = 'avisArrivee'
    else:
        container = dict(ticket.get('enlevement') or {})
        active = list(container.get('bons_livraison') or [])
        container_key = 'enlevement'

    items = [dict(x or {}) for x in (container.get('items') or [])]
    for idx, item in enumerate(items):
        total = 0
        item_history = []
        latest = None
        for rec in active:
            for rec_item in rec.get('items') or []:
                try:
                    rec_idx = int(rec_item.get('index'))
                except Exception:
                    continue
                if rec_idx != idx:
                    continue
                qty = _reception_qty_int(rec_item.get('quantite'), 0)
                total += qty
                latest = rec
                item_history.append({
                    'reference': _as_text(rec.get('reference')).strip(),
                    'date': rec.get('receptionnee_le') or rec.get('created_at') or rec.get('date_reception') or '',
                    'quantite': qty,
                    'receptionne_par': rec.get('receptionne_par') or '',
                    'lieu_stockage': rec.get('lieu_stockage') or '',
                })

        planned = max(1, _reception_qty_int(item.get('quantite') or 1, 1))
        item['quantite_recue_totale'] = total
        item['receptionne'] = total >= planned
        item['receptions'] = item_history
        if latest is not None and total > 0:
            item['receptionne_le'] = latest.get('receptionnee_le') or latest.get('created_at') or ''
            item['receptionne_par'] = latest.get('receptionne_par') or ''
            item['lieu_stockage'] = latest.get('lieu_stockage') or ''
        else:
            item['receptionne_le'] = ''
            item['receptionne_par'] = ''
            item['lieu_stockage'] = ''
        items[idx] = item

    container['items'] = items
    ticket[container_key] = container
    if is_avis:
        ticket['receptionAvisArrivee'] = active[-1] if active else None
    return ticket


def _active_reception_for_esi(ticket, esi_id, is_avis):
    if is_avis:
        active = list(ticket.get('receptionsAvisArrivee') or [])
    else:
        active = list((ticket.get('enlevement') or {}).get('bons_livraison') or [])
    found = None
    for rec in active:
        for rec_item in rec.get('items') or []:
            ids = [_as_text(x).strip() for x in (rec_item.get('esi_ids') or [])]
            if esi_id in ids:
                colis_map = rec_item.get('colis_par_esi') if isinstance(rec_item.get('colis_par_esi'), dict) else {}
                colis = _as_text(colis_map.get(esi_id)).strip()
                type_map = rec_item.get('type_colis_par_esi') if isinstance(rec_item.get('type_colis_par_esi'), dict) else {}
                type_colis = _normalise_colis_type(type_map.get(esi_id))
                if not type_colis:
                    rec_types = rec.get('colis_types') if isinstance(rec.get('colis_types'), dict) else {}
                    type_colis = _normalise_colis_type(rec_types.get(colis))
                found = (rec, colis, type_colis)
    return found


def _sync_articles_after_reception_cancel(ticket, cancelled, is_avis):
    """Retire l'historique logistique de la réception annulée sans bloquer l'annulation du ticket."""
    affected = []
    for esi_id in cancelled.get('article_esi_ids') or []:
        esi_id = _as_text(esi_id).strip()
        if esi_id and esi_id not in affected:
            affected.append(esi_id)
    for rec_item in cancelled.get('items') or []:
        for esi_id in rec_item.get('esi_ids') or []:
            esi_id = _as_text(esi_id).strip()
            if esi_id and esi_id not in affected:
                affected.append(esi_id)

    cancelled_ref = _as_text(cancelled.get('reference')).strip()
    parent_ids_to_sync = set()
    for esi_id in affected:
        try:
            safe_esi = urllib.parse.quote(esi_id, safe='-')
            rows = supabase_rest_request('GET', 'articles', f'select=*&esi_id=eq.{safe_esi}&limit=1') or []
            if not rows:
                continue
            article = dict(rows[0])
            raw = article.get('raw_json') if isinstance(article.get('raw_json'), dict) else {}
            raw = dict(raw or {})
            part_meta = _article_part_meta(article)
            if part_meta.get('parent_esi'):
                parent_ids_to_sync.add(part_meta['parent_esi'])

            old_history = list(raw.get('receptions') or [])
            removed = [r for r in old_history if _as_text(r.get('reception_ref')).strip() == cancelled_ref]
            raw['receptions'] = [r for r in old_history if _as_text(r.get('reception_ref')).strip() != cancelled_ref]
            cancelled_history = list(raw.get('receptions_annulees') or [])
            for entry in removed:
                copy = dict(entry)
                copy['annulee_le'] = cancelled.get('annulee_le') or datetime.now().isoformat()
                cancelled_history.append(copy)
            raw['receptions_annulees'] = cancelled_history

            latest = _active_reception_for_esi(ticket, esi_id, is_avis)
            extra = raw.get('article_fields') if isinstance(raw.get('article_fields'), dict) else {}
            extra = dict(extra or {})
            if latest:
                rec, colis, type_colis = latest
                raw['colis_actuel'] = colis
                raw['type_colis_actuel'] = type_colis
                extra['type_colis'] = type_colis
                extra['numero_colis'] = colis
                colis_record = _find_colis_record(colis, article.get('dossier')) if colis else None
                extra['colis_esi'] = _as_text((colis_record or {}).get('esi_id')).strip()
                raw['article_fields'] = extra
                patch = {
                    'lieu_stockage': _as_text(rec.get('lieu_stockage')).strip(),
                    'statut_logistique': 'Réceptionné',
                    'dernier_colis': colis,
                    'derniere_reception_ref': _as_text(rec.get('reference')).strip(),
                    'updated_at': datetime.now().isoformat(),
                    'raw_json': raw,
                }
            else:
                raw['colis_actuel'] = ''
                raw['type_colis_actuel'] = ''
                extra['type_colis'] = ''
                extra['numero_colis'] = ''
                extra['colis_esi'] = ''
                raw['article_fields'] = extra
                patch = {
                    'lieu_stockage': '',
                    'statut_logistique': 'Créé',
                    'dernier_colis': '',
                    'derniere_reception_ref': '',
                    'updated_at': datetime.now().isoformat(),
                    'raw_json': raw,
                }
            merged = dict(article)
            merged.update(patch)
            patch['search_text'] = _article_search_text(merged)
            supabase_rest_request('PATCH', 'articles', f'esi_id=eq.{safe_esi}', patch, prefer='return=minimal')
        except Exception as e:
            print(f'[ANNULATION RECEPTION] Article {esi_id}: {e}')

    for parent_esi in sorted(parent_ids_to_sync):
        try:
            _sync_parent_parts_logistics(parent_esi)
        except Exception as e:
            print(f'[ANNULATION RECEPTION] Synchronisation article principal {parent_esi}: {e}')


def _mark_colis_records_cancelled(cancelled):
    """Conserve la trace du colis mais retire ses liens actifs si la réception est annulée."""
    cancelled_ref = _as_text((cancelled or {}).get('reference')).strip()
    dossier = _as_text((cancelled or {}).get('numero_dossier')).strip()
    now = datetime.now().isoformat()
    for colis_ref in (cancelled or {}).get('colis') or []:
        colis_ref = _as_text(colis_ref).strip()
        if not colis_ref:
            continue
        try:
            row = _find_colis_record(colis_ref, dossier)
            if not row:
                continue
            raw = row.get('raw_json') if isinstance(row.get('raw_json'), dict) else {}
            raw = dict(raw or {})
            # Ne désactive que la fiche issue de la réception annulée.
            if cancelled_ref and _as_text(raw.get('reception_ref')).strip() not in ('', cancelled_ref):
                continue
            extra = raw.get('article_fields') if isinstance(raw.get('article_fields'), dict) else {}
            extra = dict(extra or {})
            extra['articles_lies'] = ''
            raw['article_fields'] = extra
            raw['article_esi_ids'] = []
            raw['articles_lies'] = []
            raw['reception_annulee'] = cancelled_ref
            raw['annulee_le'] = (cancelled or {}).get('annulee_le') or now
            patch = {
                'statut_logistique': 'Réception annulée',
                'lieu_stockage': '',
                'derniere_reception_ref': '',
                'updated_at': now,
                'raw_json': raw,
            }
            merged = dict(row)
            merged.update(patch)
            patch['search_text'] = _article_search_text(merged)
            safe_esi = urllib.parse.quote(_as_text(row.get('esi_id')).strip(), safe='-')
            supabase_rest_request('PATCH', 'articles', f'esi_id=eq.{safe_esi}', patch, prefer='return=minimal')
        except Exception as e:
            print(f'[ANNULATION RECEPTION] Colis {colis_ref}: {e}')


def _cancel_specific_reception(ticket, reference):
    reference = _as_text(reference).strip()
    if not reference:
        raise ValueError('Référence de réception manquante')

    module = _as_text(ticket.get('module')).replace('’', "'").strip()
    is_avis = module == "Avis d'arrivée" or _as_text(ticket.get('id')).startswith('ARR-')
    cancelled = None

    if is_avis:
        active = list(ticket.get('receptionsAvisArrivee') or [])
        kept = []
        for rec in active:
            if cancelled is None and _as_text(rec.get('reference')).strip() == reference:
                cancelled = dict(rec)
            else:
                kept.append(rec)
        if cancelled is None:
            raise ValueError(f'Réception {reference} introuvable parmi les réceptions actives')
        cancelled['annulee'] = True
        cancelled['annulee_le'] = datetime.now().isoformat()
        ticket['receptionsAvisArrivee'] = kept
        history = list(ticket.get('receptionsAvisArriveeAnnulees') or [])
        history.append(cancelled)
        ticket['receptionsAvisArriveeAnnulees'] = history
    else:
        enl = dict(ticket.get('enlevement') or {})
        active = list(enl.get('bons_livraison') or [])
        kept = []
        for rec in active:
            if cancelled is None and _as_text(rec.get('reference')).strip() == reference:
                cancelled = dict(rec)
            else:
                kept.append(rec)
        if cancelled is None:
            raise ValueError(f'Réception {reference} introuvable parmi les réceptions actives')
        cancelled['annulee'] = True
        cancelled['annulee_le'] = datetime.now().isoformat()
        enl['bons_livraison'] = kept
        history = list(enl.get('bons_livraison_annules') or [])
        history.append(cancelled)
        enl['bons_livraison_annules'] = history
        ticket['enlevement'] = enl

    _rebuild_ticket_after_reception_cancel(ticket, is_avis)
    ticket['updatedAt'] = datetime.now().isoformat()
    save_ticket(ticket)

    # Vérification côté base AVANT de dire OK au navigateur.
    checked = load_ticket(ticket.get('id'))
    if not checked:
        raise RuntimeError('Impossible de relire le ticket après annulation')
    if is_avis:
        still_active = any(
            _as_text(r.get('reference')).strip() == reference
            for r in (checked.get('receptionsAvisArrivee') or [])
        )
    else:
        still_active = any(
            _as_text(r.get('reference')).strip() == reference
            for r in ((checked.get('enlevement') or {}).get('bons_livraison') or [])
        )
    if still_active:
        raise RuntimeError(f'La réception {reference} est encore active après sauvegarde Supabase')

    # La base Articles est synchronisée seulement après validation de la sauvegarde du ticket.
    _sync_articles_after_reception_cancel(checked, cancelled, is_avis)
    _mark_colis_records_cancelled(cancelled)
    return checked


@app.route('/api/tickets/<ticket_id>', methods=['PUT', 'DELETE'])
def api_update_ticket(ticket_id):
    ticket = load_ticket(ticket_id)
    if not ticket:
        return jsonify({'error': 'Ticket introuvable'}), 404

    if request.method == 'DELETE':
        try:
            deleted, warnings = delete_ticket_permanently(ticket_id)
            if not deleted:
                return jsonify({'ok': False, 'error': 'Ticket introuvable'}), 404
            return jsonify({'ok': True, 'deleted': ticket_id, 'warnings': warnings})
        except ValueError as e:
            return jsonify({'ok': False, 'error': str(e)}), 409
        except Exception as e:
            print(f'[TICKET DELETE] Suppression impossible {ticket_id}: {e}')
            return jsonify({'ok': False, 'error': str(e)}), 500

    data = request.get_json(silent=True) or {}

    if _as_text(data.get('action')).strip() == 'annuler_reception':
        reference = _as_text(data.get('reference')).strip()
        try:
            refreshed = _cancel_specific_reception(ticket, reference)
            module = _as_text(refreshed.get('module')).replace('’', "'").strip()
            is_avis = module == "Avis d'arrivée" or _as_text(refreshed.get('id')).startswith('ARR-')
            active_count = (
                len(refreshed.get('receptionsAvisArrivee') or [])
                if is_avis else
                len((refreshed.get('enlevement') or {}).get('bons_livraison') or [])
            )
            return jsonify({
                'ok': True,
                'action': 'annuler_reception',
                'reference': reference,
                'active_receptions': active_count,
            })
        except ValueError as e:
            return jsonify({'ok': False, 'error': str(e)}), 404
        except Exception as e:
            print(f'[ANNULATION RECEPTION] {ticket_id} {reference}: {e}')
            return jsonify({'ok': False, 'error': str(e)}), 500

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
        'typeVisite',
        'lieuRdv',
        'contactRdv',
        'commentaire'
    ]

    for field in editable_fields:
        if field in data:
            value = data.get(field, '')
            if field == 'ref' and _as_text(ticket.get('module')).strip() == 'Fiche de caisse':
                value = _packing_local_number(data.get('dossier', ticket.get('dossier')), value)
            ticket[field] = value

    if 'expo' in data and 'objet' not in data:
        ticket['objet'] = data.get('expo', '')

    ticket['updatedAt'] = datetime.now().isoformat()
    save_ticket(ticket)
    return jsonify({'ok': True})


@app.route('/api/tickets/<ticket_id>/reception-avis-arrivee', methods=['POST'])
def api_reception_avis_arrivee(ticket_id):
    """Valide une réception partielle/totale d'un avis d'arrivée et génère les étiquettes."""
    ticket = load_ticket(ticket_id)
    if not ticket:
        return jsonify({'ok': False, 'error': 'Ticket introuvable'}), 404

    module_normalise = _as_text(ticket.get('module')).replace("’", "'").strip()
    if module_normalise != "Avis d'arrivée" and not _as_text(ticket_id).startswith('ARR-'):
        return jsonify({'ok': False, 'error': "Ce ticket n'est pas un avis d'arrivée"}), 400

    data = request.get_json(silent=True) or {}
    receptionne_par = _as_text(data.get('receptionne_par')).strip()
    lieu_stockage = _as_text(data.get('lieu_stockage')).strip()
    numero_dossier = _as_text(data.get('numero_dossier')).strip()
    commentaire = _as_text(data.get('commentaire')).strip()

    try:
        nombre_colis = int(data.get('nombre_colis') or 0)
    except (TypeError, ValueError):
        nombre_colis = 0

    items_reception = data.get('items_reception')
    selected_indexes = data.get('selected_indexes') or []
    colis_repartition = data.get('colis_repartition') or []
    article_parts = data.get('article_parts') or []

    if not receptionne_par:
        return jsonify({'ok': False, 'error': 'Nom et prénom du réceptionnaire manquants'}), 400
    if not lieu_stockage:
        return jsonify({'ok': False, 'error': 'Lieu de stockage manquant'}), 400
    if not numero_dossier:
        return jsonify({'ok': False, 'error': 'N° dossier obligatoire pour numéroter les Pre-Packings'}), 400
    if nombre_colis < 1:
        return jsonify({'ok': False, 'error': 'Le nombre total de Pre-Packings doit être supérieur ou égal à 1'}), 400

    avis = dict(ticket.get('avisArrivee') or ticket.get('avis_arrivee') or {})
    items = list(avis.get('items') or [])
    if not items:
        return jsonify({'ok': False, 'error': "Aucune marchandise dans cet avis d'arrivée"}), 400

    # Compatibilité ancienne interface
    if not isinstance(items_reception, list):
        items_reception = []
        for raw_idx in selected_indexes if isinstance(selected_indexes, list) else []:
            try:
                items_reception.append({'index': int(raw_idx), 'quantite_recue': None})
            except Exception:
                pass

    if not items_reception:
        return jsonify({'ok': False, 'error': 'Aucune marchandise sélectionnée'}), 400

    def _qty_int(value, default=0):
        try:
            return max(0, int(float(str(value).replace(',', '.'))))
        except Exception:
            return default

    def _item_fully_received(item):
        planned = max(1, _qty_int(item.get('quantite') or 1, 1))
        received = _qty_int(item.get('quantite_recue_totale'), -1)
        if received < 0:
            received = planned if item.get('receptionne') else 0
        return received >= planned

    if items and all(_item_fully_received(dict(x or {})) for x in items):
        return jsonify({
            'ok': False,
            'error': 'Réception clôturée : tous les articles ont déjà été réceptionnés.'
        }), 409

    # Sécurise les anciens avis créés avant l'ajout du référentiel articles.
    try:
        _ensure_articles_for_ticket(ticket, save=True)
        avis = dict(ticket.get('avisArrivee') or {})
        items = list(avis.get('items') or [])
    except Exception as e:
        return jsonify({'ok': False, 'error': f"Impossible d'attribuer les numéros ESI : {e}"}), 500

    now = datetime.now()
    selected = []
    article_labels = []
    reception_esi_ids = []

    for entry in items_reception:
        if not isinstance(entry, dict):
            continue

        try:
            idx = int(entry.get('index'))
        except Exception:
            continue

        if idx < 0 or idx >= len(items):
            continue

        item = dict(items[idx] or {})
        planned = max(1, _qty_int(item.get('quantite') or 1, 1))

        previous = _qty_int(item.get('quantite_recue_totale'), -1)
        if previous < 0:
            previous = planned if item.get('receptionne') else 0

        remaining = max(planned - previous, 0)
        if remaining <= 0:
            continue

        requested = entry.get('quantite_recue')
        qty_received = remaining if requested in (None, '', 0, '0') else _qty_int(requested, 0)

        if qty_received < 1:
            continue

        if qty_received > remaining:
            return jsonify({
                'ok': False,
                'error': f"Quantité reçue trop élevée pour {item.get('reference') or idx} : reste {remaining}"
            }), 400

        new_total = previous + qty_received

        history = list(item.get('receptions') or [])
        history.append({
            'date': now.isoformat(),
            'quantite': qty_received,
            'receptionne_par': receptionne_par,
            'lieu_stockage': lieu_stockage,
        })

        item['quantite_recue_totale'] = new_total
        item['receptionne'] = new_total >= planned
        item['receptionne_le'] = now.isoformat()
        item['receptionne_par'] = receptionne_par
        item['lieu_stockage'] = lieu_stockage
        item['receptions'] = history
        items[idx] = item

        received_esi_ids = _article_ids_for_received_units(item, previous, qty_received)
        reception_esi_ids.extend(received_esi_ids)

        selected_item = {
            'index': idx,
            'quantite': str(qty_received),
            'quantite_prevue': str(planned),
            'quantite_deja_recue': str(previous),
            'quantite_recue_totale': str(new_total),
            'reference': _as_text(item.get('reference')).strip(),
            'designation': _as_text(item.get('description')).strip(),
            'esi_ids': received_esi_ids,
        }
        selected.append(selected_item)

        for unit_no, esi_id in enumerate(received_esi_ids, start=1):
            article_labels.append({
                'titre': 'ARTICLE',
                'principal': esi_id,
                'esi_id': esi_id,
                'dossier': numero_dossier,
                'client': avis.get('client') or ticket.get('dossier') or '',
                'reference': _as_text(item.get('reference')).strip(),
                'designation': _as_text(item.get('description')).strip(),
                'quantite': f"{unit_no}/{qty_received}",
                'lieu': lieu_stockage,
                'qr_url': _article_qr_url(esi_id),
            })

    if not selected:
        return jsonify({'ok': False, 'error': 'Aucune marchandise sélectionnée valide'}), 400

    with _BLR_LOCK:
        # Référence propre aux réceptions d'avis
        existing_refs = []
        try:
            for t in list_tickets():
                for r in list(t.get('receptionsAvisArrivee') or []) + list(t.get('receptionsAvisArriveeAnnulees') or []):
                    ref = _as_text(r.get('reference')).strip()
                    m = re.fullmatch(r"RAR-(\d+)", ref, re.I)
                    if m:
                        existing_refs.append(int(m.group(1)))
        except Exception as e:
            print(f"[RAR] Lecture historique impossible: {e}")

        reception_ref = f"RAR-{(max(existing_refs) if existing_refs else 0) + 1:04d}"
        colis_refs = _allocate_colis_numbers(numero_dossier, nombre_colis)
        colis_types = _resolve_colis_types(data.get('colis_types'), colis_refs)
        if any(not colis_types.get(ref) for ref in colis_refs):
            return jsonify({'ok': False, 'error': 'Le type de chaque Pre-Packing est obligatoire : Softpack, Carton ou Packing bois.'}), 400
        try:
            _validate_colis_repartition_shape(selected, colis_repartition, len(colis_refs), article_parts)
            reception_esi_ids, parent_part_ids = _expand_selected_items_with_parts(selected, article_parts)
        except ValueError as e:
            return jsonify({'ok': False, 'error': str(e)}), 400
        except Exception as e:
            return jsonify({'ok': False, 'error': f"Impossible de créer les parties de l'article : {e}"}), 500
        charge_projet = _as_text(avis.get('coordinateur') or ticket.get('chargeProjet') or '').strip()
        article_labels = _build_article_labels_from_selected(
            selected,
            numero_dossier,
            avis.get('client') or ticket.get('dossier') or '',
            lieu_stockage,
            charge_projet,
        )
        try:
            colis_by_esi = _resolve_colis_repartition(selected, colis_repartition, colis_refs)
        except ValueError as e:
            return jsonify({'ok': False, 'error': str(e)}), 400
        colis_type_by_esi = {esi: colis_types.get(ref, '') for esi, ref in colis_by_esi.items()}
        _apply_colis_to_selected_items(selected, colis_by_esi, reception_ref, lieu_stockage, colis_type_by_esi)

        for label in article_labels:
            label['colis'] = colis_by_esi.get(label.get('esi_id'), '')
            label['type_colis'] = colis_type_by_esi.get(label.get('esi_id'), '')

        article_labels_bytes = _build_labels_pdf_bytes(article_labels, kind="article")
        article_labels_filename = f"{reception_ref}_etiquettes_articles.pdf"
        article_labels_path = f"{ticket_id}/receptions_avis/{now.strftime('%Y%m%d%H%M%S')}_{article_labels_filename}"

        colis_labels = [{
            'titre': 'PRE-PACKING',
            'principal': _colis_display(colis_ref, colis_types.get(colis_ref)),
            'dossier': numero_dossier,
            'client': avis.get('client') or ticket.get('dossier') or '',
            'charge_projet': charge_projet,
            'colis': colis_ref,
            'type_colis': colis_types.get(colis_ref, ''),
            'lieu': lieu_stockage,
            'bon': reception_ref,
            'qr_url': _colis_qr_url(colis_ref),
        } for colis_ref in colis_refs]

        colis_labels_bytes = _build_labels_pdf_bytes(colis_labels, kind="colis")
        colis_labels_filename = f"{reception_ref}_etiquettes_pre_packing.pdf"
        colis_labels_path = f"{ticket_id}/receptions_avis/{now.strftime('%Y%m%d%H%M%S')}_{colis_labels_filename}"

        reception_pdf_data = {
            'reference': reception_ref,
            'ticket_id': ticket_id,
            'numero_dossier': numero_dossier,
            'receptionne_par': receptionne_par,
            'lieu_stockage': lieu_stockage,
            'date_reception': now.strftime("%d/%m/%Y %H:%M"),
            'nombre_colis': nombre_colis,
            'colis': colis_refs,
            'colis_types': colis_types,
            'article_esi_ids': list(reception_esi_ids),
            'commentaire': commentaire,
            'items': selected,
        }
        reception_pdf_bytes = _build_reception_form_pdf_bytes(ticket, reception_pdf_data, source_type='avis')
        reception_pdf_filename = f"{reception_ref}_bon_reception.pdf"
        reception_pdf_path = f"{ticket_id}/receptions_avis/{now.strftime('%Y%m%d%H%M%S')}_{reception_pdf_filename}"

        try:
            supabase_upload_bytes(reception_pdf_path, reception_pdf_bytes, "application/pdf")
            supabase_upload_bytes(article_labels_path, article_labels_bytes, "application/pdf")
            supabase_upload_bytes(colis_labels_path, colis_labels_bytes, "application/pdf")
        except Exception as e:
            print(f"[RAR] Erreur upload étiquettes: {e}")
            return jsonify({'ok': False, 'error': f'Impossible d’enregistrer les PDF d’étiquettes : {e}'}), 500

        # Une fois les documents générés, crée les fiches COLIS dans la base Articles.
        try:
            colis_records, colis_esi_by_ref = _ensure_colis_article_records(
                ticket_id=ticket_id,
                numero_dossier=numero_dossier,
                colis_refs=colis_refs,
                colis_types=colis_types,
                colis_by_esi=colis_by_esi,
                selected_items=selected,
                client=avis.get('client') or ticket.get('dossier') or '',
                projet=avis.get('projet') or ticket.get('expo') or ticket.get('objet') or '',
                charge_projet=charge_projet,
                lieu_stockage=lieu_stockage,
                reception_ref=reception_ref,
            )
        except Exception as e:
            print(f"[COLIS ARTICLES] Creation impossible pour {reception_ref}: {e}")
            return jsonify({'ok': False, 'error': f'Impossible de créer les Pre-Packings dans la base Articles : {e}'}), 500

        avis['items'] = items
        ticket['avisArrivee'] = avis

        reception = {
            'reference': reception_ref,
            'ticket_id': ticket_id,
            'receptionnee': True,
            'receptionnee_le': now.isoformat(),
            'date_reception': now.strftime("%d/%m/%Y %H:%M"),
            'receptionne_par': receptionne_par,
            'lieu_stockage': lieu_stockage,
            'numero_dossier': numero_dossier,
            'nombre_colis': nombre_colis,
            'colis': colis_refs,
            'colis_types': colis_types,
            'commentaire': commentaire,
            'items': selected,
            'bon_reception_filename': reception_pdf_filename,
            'bon_reception_path': reception_pdf_path,
            'etiquettes_articles_filename': article_labels_filename,
            'etiquettes_articles_path': article_labels_path,
            'etiquettes_colis_filename': colis_labels_filename,
            'etiquettes_colis_path': colis_labels_path,
        }

        receptions = list(ticket.get('receptionsAvisArrivee') or [])
        receptions.append(reception)
        ticket['receptionsAvisArrivee'] = receptions

        # Compatibilité avec l'ancien champ de synthèse
        ticket['receptionAvisArrivee'] = reception

        manager_sheets = list(ticket.get('managerSheets') or [])
        for name, size, path in [
            (reception_pdf_filename, len(reception_pdf_bytes), reception_pdf_path),
            (article_labels_filename, len(article_labels_bytes), article_labels_path),
            (colis_labels_filename, len(colis_labels_bytes), colis_labels_path),
        ]:
            manager_sheets.append({'name': name, 'size': size, 'path': path})

        ticket['managerSheets'] = manager_sheets
        ticket['updatedAt'] = now.isoformat()

        save_ticket(ticket)
        try:
            _update_article_logistics(
                reception_esi_ids,
                lieu_stockage=lieu_stockage,
                statut_logistique="Réceptionné",
                colis_by_esi=colis_by_esi,
                colis_type_by_esi=colis_type_by_esi,
                colis_esi_by_ref=colis_esi_by_ref,
                reception_ref=reception_ref,
                receptionne_par=receptionne_par,
            )
        except Exception as e:
            print(f"[ARTICLES] Mise à jour logistique avis impossible: {e}")

    return jsonify({
        'ok': True,
        'ticket_id': ticket_id,
        'reference': reception_ref,
        'reception': reception,
        'colis': colis_refs,
        'colis_types': colis_types,
        'colis_esi': colis_esi_by_ref,
        'bon_reception_filename': reception_pdf_filename,
        'etiquettes_articles_filename': article_labels_filename,
        'etiquettes_colis_filename': colis_labels_filename,
        'status': ticket.get('status')
    })


@app.route('/api/tickets/<ticket_id>/enlevement', methods=['PATCH'])
def api_update_enlevement(ticket_id):
    """Enregistre les corrections manuelles des champs d'une demande d'enlèvement."""
    ticket = load_ticket(ticket_id)
    if not ticket:
        return jsonify({'ok': False, 'error': 'Ticket introuvable'}), 404

    module_normalise = _as_text(ticket.get('module')).replace("’", "'").strip()
    if module_normalise not in ("Demande d'enlèvement", "Demande d'enlevement") and not _as_text(ticket_id).startswith('ENL-'):
        return jsonify({'ok': False, 'error': "Ce ticket n'est pas une demande d'enlèvement"}), 400

    data = request.get_json(silent=True) or {}
    current = dict(ticket.get('enlevement') or {})

    editable = [
        'numero_dossier', 'client', 'numero_bon', 'date_enlevement',
        'coordinateur', 'exhibition',
        'adresse_depart',
        'adresse_destination',
        'notes', 'instructions'
    ]
    for field in editable:
        if field in data:
            current[field] = _as_text(data.get(field)).strip()

    if 'items' in data:
        if not isinstance(data.get('items'), list):
            return jsonify({'ok': False, 'error': 'Format des articles invalide'}), 400
        cleaned_items = []
        for item in data.get('items') or []:
            if not isinstance(item, dict):
                continue
            cleaned_items.append({
                'quantite': _as_text(item.get('quantite')).strip(),
                'designation': _as_text(item.get('designation')).strip(),
                'reference': _as_text(item.get('reference')).strip(),
                'dimensions': _as_text(item.get('dimensions')).strip(),
                'esi_ids': list(item.get('esi_ids') or []),
                'esi_id': _as_text(item.get('esi_id')).strip(),
            })
        current['items'] = cleaned_items
        current['references'] = [x['reference'] for x in cleaned_items if x.get('reference')]

    current['display_name'] = " - ".join(
        x for x in [current.get('client', '').strip(), current.get('numero_bon', '').strip()] if x
    )
    current['manually_edited'] = True
    current['manual_updated_at'] = datetime.now().isoformat()
    ticket['enlevement'] = current

    # Synchronisation avec les champs historiques utilisés ailleurs.
    numero_dossier = _as_text(current.get('numero_dossier') or ticket.get('numeroDossier')).strip()
    if numero_dossier:
        ticket['numeroDossier'] = numero_dossier
        ticket['dossier'] = numero_dossier
    else:
        ticket['dossier'] = current.get('client', '')
    ticket['ref'] = current.get('numero_bon', '')
    ticket['chargeProjet'] = current.get('coordinateur', '') or '-'
    ticket['expo'] = current.get('exhibition', '') or '-'
    ticket['objet'] = current.get('exhibition', '') or '-'
    # L'heure n'est pas utilisee dans la gestion reception.
    ticket['heureRdv'] = '-'

    date_fr = current.get('date_enlevement', '')
    if date_fr:
        try:
            ticket['dateRdv'] = datetime.strptime(date_fr, "%d/%m/%Y").strftime("%Y-%m-%d")
        except ValueError:
            return jsonify({'ok': False, 'error': 'La date doit être au format JJ/MM/AAAA'}), 400
    else:
        ticket['dateRdv'] = '-'

    ticket['updatedAt'] = datetime.now().isoformat()
    save_ticket(ticket)
    return jsonify({'ok': True, 'enlevement': current})




_BLR_LOCK = threading.Lock()


def _next_blr_reference():
    """Retourne une référence BLR-0001, BLR-0002... à partir des bons déjà enregistrés."""
    highest = 0
    try:
        for t in list_tickets():
            enl = t.get("enlevement") or {}
            for bon in list(enl.get("bons_livraison") or []) + list(enl.get("bons_livraison_annules") or []):
                ref = _as_text(bon.get("reference")).strip()
                m = re.fullmatch(r"BLR-(\d+)", ref, re.I)
                if m:
                    highest = max(highest, int(m.group(1)))
    except Exception as e:
        print(f"[BLR] Impossible de lire l'historique des BLR: {e}")
    return f"BLR-{highest + 1:04d}"


def _build_reception_form_pdf_bytes(ticket, bon, source_type="enlevement"):
    """Bon de réception A4 : une ligne par article physique / N° ESI, avec pagination automatique."""
    import io
    import textwrap as _tw

    PAGE_W, PAGE_H = 595, 842
    NAVY = (0.035, 0.145, 0.235)
    CYAN = (0.05, 0.55, 0.76)
    PALE = (0.965, 0.975, 0.982)
    LINE = (0.62, 0.70, 0.76)
    WHITE = (1, 1, 1)
    TEXT = (0.06, 0.10, 0.16)

    def clean(v, default='-'):
        s = _as_text(v).replace('\r', ' ').replace('\n', ' ').strip()
        return s or default

    def esc(v):
        return clean(v, '').replace('\\', '\\\\').replace('(', '\\(').replace(')', '\\)')

    def rgb(c, stroke=False):
        return f"{c[0]:.3f} {c[1]:.3f} {c[2]:.3f} {'RG' if stroke else 'rg'}"

    avis = ticket.get('avisArrivee') or ticket.get('avis_arrivee') or {}
    enl = ticket.get('enlevement') or {}
    if source_type == 'avis':
        dossier = clean(bon.get('numero_dossier') or avis.get('dossier_ref') or ticket.get('dossier'))
        client = clean(avis.get('client') or ticket.get('dossier'))
        projet = clean(avis.get('projet') or ticket.get('expo') or ticket.get('objet'))
        coordinateur = clean(avis.get('coordinateur') or ticket.get('chargeProjet'))
        exp = avis.get('expediteur') or {}
        tr = avis.get('transporteur') or {}
        left_block = [clean(exp.get('nom')), clean(exp.get('adresse')), clean(exp.get('contact'))]
        right_block = [clean(tr.get('nom')), clean(tr.get('adresse')), clean(tr.get('contact')), clean(tr.get('reference'))]
    else:
        dossier = clean(bon.get('numero_dossier'))
        client = clean(enl.get('client') or ticket.get('dossier'))
        projet = clean(enl.get('exhibition') or ticket.get('expo') or ticket.get('objet'))
        coordinateur = clean(enl.get('coordinateur') or ticket.get('chargeProjet'))
        left_block = [clean(enl.get('adresse_depart')), '-', '-']
        right_block = [clean(enl.get('adresse_destination')), '-', '-', clean(enl.get('numero_bon') or ticket.get('ref'))]

    # Une ligne du bon = un article physique = un N° ESI.
    # Le bon est ensuite regroupe visuellement par colis pour eviter de repeter
    # le meme N° de colis sur chaque ligne.
    source_rows = bon.get('items') or []
    unit_rows = []
    bon_colis_types = bon.get('colis_types') if isinstance(bon.get('colis_types'), dict) else {}
    for source_row in source_rows:
        row = dict(source_row or {})
        esi_ids = [str(v).strip() for v in (row.get('esi_ids') or []) if str(v).strip()]
        colis_par_esi = row.get('colis_par_esi') if isinstance(row.get('colis_par_esi'), dict) else {}
        type_colis_par_esi = row.get('type_colis_par_esi') if isinstance(row.get('type_colis_par_esi'), dict) else {}
        partie_par_esi = row.get('partie_par_esi') if isinstance(row.get('partie_par_esi'), dict) else {}
        parent_esi_par_esi = row.get('parent_esi_par_esi') if isinstance(row.get('parent_esi_par_esi'), dict) else {}

        if esi_ids:
            for esi_id in esi_ids:
                part_label = _as_text(partie_par_esi.get(esi_id)).strip()
                designation = _as_text(row.get('designation') or row.get('description')).strip()
                if part_label:
                    designation = (designation + f" - Partie {part_label}").strip(" -")
                colis_ref = _as_text(colis_par_esi.get(esi_id)).strip()
                colis_type = _normalise_colis_type(type_colis_par_esi.get(esi_id) or bon_colis_types.get(colis_ref))
                unit_rows.append({
                    'esi_id': esi_id,
                    'reference': row.get('reference'),
                    'designation': designation,
                    'partie': part_label,
                    'parent_esi': _as_text(parent_esi_par_esi.get(esi_id)).strip(),
                    'colis_ref': colis_ref,
                    'colis_type': colis_type,
                    'dimensions': row.get('dimensions'),
                    'quantite': '1',
                    'lieu_stockage': row.get('lieu_stockage') or bon.get('lieu_stockage'),
                })
            continue

        # Compatibilite avec d'anciens bons qui n'auraient pas encore de N° ESI.
        try:
            qty = max(1, int(float(str(row.get('quantite') or 1).replace(',', '.'))))
        except Exception:
            qty = 1
        legacy_colis = row.get('colis') or []
        if not isinstance(legacy_colis, list):
            legacy_colis = [legacy_colis] if legacy_colis else []
        for unit_index in range(qty):
            legacy_ref = _as_text(legacy_colis[unit_index] if unit_index < len(legacy_colis) else '').strip()
            unit_rows.append({
                'esi_id': '',
                'reference': row.get('reference'),
                'designation': row.get('designation') or row.get('description'),
                'colis_ref': legacy_ref,
                'colis_type': _normalise_colis_type(bon_colis_types.get(legacy_ref)),
                'dimensions': row.get('dimensions'),
                'quantite': '1',
                'lieu_stockage': row.get('lieu_stockage') or bon.get('lieu_stockage'),
            })

    # Regroupement des articles par colis dans l'ordre des colis du bon.
    group_map = {}
    for row in unit_rows:
        ref = _as_text(row.get('colis_ref')).strip() or 'SANS-PRE-PACKING'
        if ref not in group_map:
            group_map[ref] = {
                'colis_ref': ref,
                'colis_type': _normalise_colis_type(row.get('colis_type') or bon_colis_types.get(ref)),
                'rows': [],
            }
        group_map[ref]['rows'].append(row)

    order = {_as_text(ref).strip(): i for i, ref in enumerate(bon.get('colis') or [])}
    groups = sorted(
        group_map.values(),
        key=lambda g: (order.get(g['colis_ref'], 10**6), g['colis_ref'])
    )

    # Pagination par hauteur : un titre de colis + ses articles. On evite
    # qu'un titre de colis reste seul en bas de page ; un gros colis peut
    # continuer sur la page suivante avec la mention "suite".
    BODY_CAPACITY = 235
    COLIS_HEADER_H = 21
    ARTICLE_ROW_H = 27
    pages_rows = []
    current_page = []
    used_h = 0

    def flush_page():
        nonlocal current_page, used_h
        if current_page:
            pages_rows.append(current_page)
        current_page = []
        used_h = 0

    for group in groups:
        remaining = list(group.get('rows') or [])
        first_chunk = True
        while remaining:
            min_needed = COLIS_HEADER_H + ARTICLE_ROW_H
            if current_page and used_h + min_needed > BODY_CAPACITY:
                flush_page()

            current_page.append({
                'kind': 'colis',
                'colis_ref': group.get('colis_ref'),
                'colis_type': group.get('colis_type'),
                'count': len(group.get('rows') or []),
                'suite': not first_chunk,
            })
            used_h += COLIS_HEADER_H
            first_chunk = False

            while remaining and used_h + ARTICLE_ROW_H <= BODY_CAPACITY:
                current_page.append({'kind': 'article', 'row': remaining.pop(0)})
                used_h += ARTICLE_ROW_H

            if remaining:
                flush_page()

    flush_page()
    if not pages_rows:
        pages_rows = [[]]

    page_count = len(pages_rows)
    total_received = len(unit_rows)

    # Header avec le vrai logo ESI depuis static/logo.png.
    logo_path = APP_DIR / 'static' / 'logo.png'
    logo_image_bytes = None
    logo_w = logo_h = None
    if logo_path.exists():
        try:
            from PIL import Image
            with Image.open(logo_path) as im:
                if im.mode not in ('RGB', 'L'):
                    im = im.convert('RGB')
                logo_w, logo_h = im.size
                buf = io.BytesIO()
                im.save(buf, format='JPEG', quality=92)
                logo_image_bytes = buf.getvalue()
        except Exception as e:
            print(f'[PDF] Logo ESI non charge: {e}')

    bon_reference = clean(bon.get('reference'))
    page_streams = []

    for page_index, page_rows in enumerate(pages_rows, start=1):
        ops = []

        def rect(x, y, w, h, fill=None, stroke=None, lw=0.6):
            if fill is not None:
                ops.append(rgb(fill))
            if stroke is not None:
                ops.append(rgb(stroke, True))
            ops.append(f"{lw:.2f} w")
            mode = 'B' if fill is not None and stroke is not None else ('f' if fill is not None else 'S')
            ops.append(f"{x:.1f} {y:.1f} {w:.1f} {h:.1f} re {mode}")

        def line(x1, y1, x2, y2, color=LINE, lw=0.6):
            ops.append(rgb(color, True)); ops.append(f"{lw:.2f} w")
            ops.append(f"{x1:.1f} {y1:.1f} m {x2:.1f} {y2:.1f} l S")

        def txt(x, y, value, size=8, bold=False, color=TEXT):
            value = esc(value)
            ops.append(rgb(color))
            ops.append('BT')
            ops.append(f"/{'F2' if bold else 'F1'} {size:.1f} Tf")
            ops.append(f"{x:.1f} {y:.1f} Td")
            ops.append(f"({value}) Tj")
            ops.append('ET')

        def fit_txt(x, y, value, width, size=8, bold=False, max_lines=2, leading=10, color=TEXT):
            approx = max(8, int(width / max(size * 0.62, 1)))
            wrapped = _tw.wrap(clean(value, ''), width=approx) or ['']
            for n, part in enumerate(wrapped[:max_lines]):
                txt(x, y - n * leading, part, size=size, bold=bold, color=color)

        # En-tête répété sur chaque page.
        txt(168, 786, f'BON DE RECEPTION - N° {bon_reference}', 18, True, TEXT)
        subtitle = 'Controle et enregistrement de la marchandise'
        if page_count > 1:
            subtitle += f' - Page {page_index}/{page_count}'
        txt(168, 767, subtitle, 10, False, (0.35,0.40,0.45))
        line(30, 744, 565, 744, CYAN, 1.6)

        txt(30, 726, 'INFORMATIONS DOSSIER', 10, True, CYAN)
        rect(30, 695, 535, 24, fill=NAVY)
        txt(40, 704, 'DOSSIER', 9, True, WHITE)

        cols = [
            (30,100,'Ref. dossier',dossier),
            (130,110,'Nom du client',client),
            (240,110,'Projet ou expo',projet),
            (350,110,'Chargé de projet',coordinateur),
            (460,105,'Date de reception',bon.get('date_reception')),
        ]
        for x,w,label,value in cols:
            rect(x, 652, w, 43, fill=PALE, stroke=LINE)
            txt(x+5, 680, label, 6.5, True, (0.25,0.32,0.38))
            fit_txt(x+5, 663, value, w-10, size=8, bold=True, max_lines=2, leading=9)

        rect(30, 623, 260, 22, fill=NAVY); txt(38, 631, 'EXPEDITEUR / DEPART', 8, True, WHITE)
        rect(305, 623, 260, 22, fill=NAVY); txt(313, 631, 'TRANSPORTEUR / DESTINATION', 8, True, WHITE)
        left_labels = ['Nom / adresse','Adresse','Contact']
        right_labels = ['Nom / adresse','Adresse','Contact','Reference']
        y = 603
        for i, label in enumerate(left_labels):
            rect(30, y-20*i, 62, 20, fill=PALE, stroke=LINE); txt(35, y+7-20*i, label, 6.2, True)
            rect(92, y-20*i, 198, 20, stroke=LINE); fit_txt(97, y+7-20*i, left_block[i], 188, 6.5, False, 1, 8)
        for i, label in enumerate(right_labels):
            rect(305, y-20*i, 62, 20, fill=PALE, stroke=LINE); txt(310, y+7-20*i, label, 6.2, True)
            rect(367, y-20*i, 198, 20, stroke=LINE); fit_txt(372, y+7-20*i, right_block[i], 188, 6.5, False, 1, 8)

        # Tableau marchandises groupe par colis.
        table_top = 517
        widths = [60, 75, 180, 70, 50, 100]
        headers = ['N° ESI','Ref. item','Description de la marchandise','Dimensions','Qte recue','Stockage']
        x = 30
        for w, header in zip(widths, headers):
            rect(x, table_top-34, w, 34, fill=NAVY, stroke=WHITE, lw=0.4)
            fit_txt(x+4, table_top-15, header, w-8, 6.4, True, 2, 8, WHITE)
            x += w

        y_cursor = table_top - 34
        package_fill = (0.90, 0.96, 0.99)
        article_fill = (0.99, 0.985, 0.955)

        for entry in page_rows:
            if entry.get('kind') == 'colis':
                h = 21
                y_cursor -= h
                rect(30, y_cursor, 535, h, fill=package_fill, stroke=CYAN, lw=0.8)
                ref = clean(entry.get('colis_ref'), 'SANS PRE-PACKING')
                typ = _display_prepacking_type(_normalise_colis_type(entry.get('colis_type')))
                label = f"PRE-PACKING {ref}"
                if typ:
                    label += f" - {typ.upper()}"
                count = int(entry.get('count') or 0)
                label += f" - {count} article{'s' if count != 1 else ''}"
                if entry.get('suite'):
                    label += " - SUITE"
                fit_txt(38, y_cursor+7, label, 519, 7.2, True, 1, 8, NAVY)
                continue

            row = entry.get('row') if isinstance(entry.get('row'), dict) else {}
            h = 27
            y_cursor -= h
            vals = [
                row.get('esi_id'),
                row.get('reference'),
                row.get('designation'),
                row.get('dimensions'),
                row.get('quantite') if row else '',
                row.get('lieu_stockage') if row else '',
            ]
            x = 30
            for w, value in zip(widths, vals):
                rect(x, y_cursor, w, h, fill=article_fill, stroke=LINE)
                fit_txt(x+4, y_cursor+17, value, w-8, 6.3, False, 2, 8)
                x += w

        table_bottom = y_cursor
        rect(30, table_bottom-22, 535, 22, fill=PALE, stroke=LINE)
        txt(338, table_bottom-14, 'TOTAL ARTICLES RECEPTIONNES', 7, True)
        txt(515, table_bottom-14, str(total_received), 8, True)

        # Commentaire et signature sont répétés afin que chaque page reste identifiable.
        comment_y = 158
        rect(30, comment_y, 535, 46, stroke=LINE)
        rect(30, comment_y+30, 535, 16, fill=PALE, stroke=LINE)
        txt(38, comment_y+35, 'Commentaire :', 7.2, True)
        fit_txt(38, comment_y+19, bon.get('commentaire') or '-', 515, 7, False, 2, 9)

        sig_y = 72
        txt(305, sig_y+78, "Reception / controle a l'arrivee :", 9, True)
        txt(305, sig_y+57, 'Date :', 8); txt(350, sig_y+57, clean(bon.get('date_reception')), 8)
        txt(305, sig_y+36, 'Nom :', 8); txt(350, sig_y+36, clean(bon.get('receptionne_par')), 8)
        txt(305, sig_y+15, 'Signature : __________________________', 8)

        nombre_colis_footer = len(bon.get('colis') or [])
        txt(30, 35, f"Reference : {clean(bon.get('reference'))}  |  Nombre de Pre-Packings : {nombre_colis_footer}", 7, False, (0.35,0.40,0.45))
        txt(430, 35, f'Groupe ESI - Bon de reception - {page_index}/{page_count}', 7, False, (0.35,0.40,0.45))

        if logo_image_bytes and logo_w and logo_h:
            box_x, box_y, box_w, box_h = 30, 752, 118, 62
            scale = min(box_w / float(logo_w), box_h / float(logo_h))
            draw_w = logo_w * scale
            draw_h = logo_h * scale
            draw_x = box_x + (box_w - draw_w) / 2
            draw_y = box_y + (box_h - draw_h) / 2
            ops.extend([
                'q',
                f'{draw_w:.2f} 0 0 {draw_h:.2f} {draw_x:.2f} {draw_y:.2f} cm',
                '/Im1 Do',
                'Q',
            ])
        else:
            rect(30, 752, 118, 62, fill=NAVY)
            txt(50, 779, 'ESI', 24, True, WHITE)

        page_streams.append('\n'.join(ops).encode('latin-1', errors='replace'))

    # Assemblage PDF multi-pages sans nouvelle dépendance.
    # 1=Catalog, 2=Pages, 3/4=Fonts, puis chaque paire Page/Contents.
    page_object_ids = [5 + 2 * i for i in range(page_count)]
    content_object_ids = [6 + 2 * i for i in range(page_count)]
    image_object_id = 5 + 2 * page_count if logo_image_bytes and logo_w and logo_h else None

    kids = ' '.join(f'{obj_id} 0 R' for obj_id in page_object_ids)
    objects = [
        b'<< /Type /Catalog /Pages 2 0 R >>',
        f'<< /Type /Pages /Kids [{kids}] /Count {page_count} >>'.encode('latin-1'),
        b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
        b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>',
    ]

    for page_obj_id, content_obj_id, stream in zip(page_object_ids, content_object_ids, page_streams):
        resources = '/Resources << /Font << /F1 3 0 R /F2 4 0 R >>'
        if image_object_id is not None:
            resources += f' /XObject << /Im1 {image_object_id} 0 R >>'
        resources += ' >>'
        page_obj = (
            f'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {PAGE_W} {PAGE_H}] '
            f'{resources} /Contents {content_obj_id} 0 R >>'
        ).encode('latin-1')
        content_obj = f'<< /Length {len(stream)} >>\nstream\n'.encode('latin-1') + stream + b'\nendstream'
        objects.extend([page_obj, content_obj])

    if image_object_id is not None:
        image_obj = (
            f'<< /Type /XObject /Subtype /Image /Width {logo_w} /Height {logo_h} '
            f'/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode /Length {len(logo_image_bytes)} >>\nstream\n'
        ).encode('latin-1') + logo_image_bytes + b'\nendstream'
        objects.append(image_obj)

    out = io.BytesIO(); out.write(b'%PDF-1.4\n')
    offsets = []
    for i, obj in enumerate(objects, 1):
        offsets.append(out.tell())
        out.write(f'{i} 0 obj\n'.encode('latin-1'))
        out.write(obj)
        out.write(b'\nendobj\n')
    xref = out.tell()
    out.write(f'xref\n0 {len(objects)+1}\n'.encode('latin-1'))
    out.write(b'0000000000 65535 f \n')
    for off in offsets:
        out.write(f'{off:010d} 00000 n \n'.encode('latin-1'))
    out.write(f'trailer\n<< /Size {len(objects)+1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF'.encode('latin-1'))
    return out.getvalue()

def _build_blr_pdf_bytes(ticket, bon):
    return _build_reception_form_pdf_bytes(ticket, bon, source_type='enlevement')


def _build_labels_pdf_bytes(labels, kind="article"):
    """Génère un PDF d'étiquettes, une étiquette par page.

    - Étiquettes COLIS : format exact 100 x 148 mm avec le vrai logo ESI
      chargé depuis static/logo.png et affiché en haut à gauche sans déformation.
    - Étiquettes ARTICLE : format A6 avec vrai logo ESI et QR vers la carte d'identité.
    """
    import io
    import textwrap as _tw

    # ------------------------------------------------------------------
    # Étiquettes ARTICLE : A6 + vrai logo ESI + QR vers la carte d'identité.
    # ------------------------------------------------------------------
    if kind != "colis":
        page_width, page_height = 298, 420
        margin = 24

        def pdf_escape(value):
            value = _as_text(value)
            return value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

        # Logo ESI : même principe que pour les étiquettes colis, sans recadrage.
        logo_path = APP_DIR / 'static' / 'logo.png'
        logo_image_bytes = None
        logo_w = logo_h = None
        if logo_path.exists():
            try:
                from PIL import Image
                with Image.open(logo_path) as im:
                    if im.mode != 'RGB':
                        bg = Image.new('RGB', im.size, 'white')
                        if 'A' in im.getbands():
                            bg.paste(im, mask=im.getchannel('A'))
                        else:
                            bg.paste(im.convert('RGB'))
                        im = bg
                    logo_w, logo_h = im.size
                    buf = io.BytesIO()
                    im.save(buf, format='JPEG', quality=95)
                    logo_image_bytes = buf.getvalue()
            except Exception as e:
                print(f'[ETIQUETTE ARTICLE] Logo ESI non charge: {e}')

        objects = [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            None,
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>",
        ]

        logo_obj_num = None
        if logo_image_bytes and logo_w and logo_h:
            logo_obj_num = len(objects) + 1
            logo_obj = (
                f'<< /Type /XObject /Subtype /Image /Width {logo_w} /Height {logo_h} '
                f'/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode /Length {len(logo_image_bytes)} >>\nstream\n'
            ).encode('latin-1') + logo_image_bytes + b'\nendstream'
            objects.append(logo_obj)

        def build_article_qr_jpeg(value):
            value = _as_text(value).strip()
            if not value:
                return None, None, None
            try:
                import qrcode
                qr = qrcode.QRCode(
                    version=None,
                    error_correction=qrcode.constants.ERROR_CORRECT_M,
                    box_size=8,
                    border=3,
                )
                qr.add_data(value)
                qr.make(fit=True)
                im = qr.make_image(fill_color='black', back_color='white').convert('RGB')
                w, h = im.size
                buf = io.BytesIO()
                im.save(buf, format='JPEG', quality=100, subsampling=0)
                return buf.getvalue(), w, h
            except Exception as e:
                print(f'[ETIQUETTE ARTICLE] QR code non genere: {e}')
                return None, None, None

        page_refs = []

        for label in labels or [{"titre": "ARTICLE"}]:
            qr_obj_num = None
            qr_bytes, qr_w, qr_h = build_article_qr_jpeg(label.get('qr_url'))
            if qr_bytes and qr_w and qr_h:
                qr_obj_num = len(objects) + 1
                qr_obj = (
                    f'<< /Type /XObject /Subtype /Image /Width {qr_w} /Height {qr_h} '
                    f'/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode /Length {len(qr_bytes)} >>\nstream\n'
                ).encode('latin-1') + qr_bytes + b'\nendstream'
                objects.append(qr_obj)

            stream_lines = []

            logo_box_x = margin
            logo_box_y = page_height - 66
            logo_box_w = 90
            logo_box_h = 40
            if logo_obj_num:
                scale = min(logo_box_w / float(logo_w), logo_box_h / float(logo_h))
                draw_w = logo_w * scale
                draw_h = logo_h * scale
                draw_x = logo_box_x
                draw_y = logo_box_y + (logo_box_h - draw_h) / 2
                stream_lines += [
                    'q',
                    f'{draw_w:.2f} 0 0 {draw_h:.2f} {draw_x:.2f} {draw_y:.2f} cm',
                    '/ImLogo Do',
                    'Q',
                ]
            else:
                stream_lines += [
                    'BT', '/F2 20 Tf', f'{margin} {page_height - 43:.2f} Td', '(ESI) Tj', 'ET'
                ]

            title = _as_text(label.get('titre') or 'ARTICLE').strip()
            stream_lines += [
                'BT', '/F2 16 Tf', f'{page_width - margin - 72:.2f} {page_height - 43:.2f} Td',
                f'({pdf_escape(title)}) Tj', 'ET'
            ]

            principal = _as_text(label.get('principal') or label.get('esi_id')).strip()
            y = page_height - 92
            if principal:
                stream_lines += [
                    'BT', '/F2 9 Tf', f'{margin} {y:.2f} Td', '(N\260 ESI) Tj', 'ET'
                ]
                y -= 20
                size = 22 if len(principal) <= 18 else 18
                for part in _tw.wrap(principal, width=24) or [principal]:
                    stream_lines += [
                        'BT', f'/F2 {size} Tf', f'{margin} {y:.2f} Td',
                        f'({pdf_escape(part)}) Tj', 'ET'
                    ]
                    y -= size + 8

            y -= 2
            stream_lines += [
                '0.25 w', f'{margin} {y:.2f} m {page_width - margin:.2f} {y:.2f} l S'
            ]
            y -= 20

            for key in ("dossier", "client", "charge_projet", "reference", "designation", "partie", "article_principal", "quantite", "colis", "lieu", "bon"):
                value = _display_prepacking_type(label.get(key)) if key == 'type_colis' else _as_text(label.get(key)).strip()
                if not value:
                    continue
                label_name = {
                    "dossier": "Dossier",
                    "client": "Client",
                    "charge_projet": "Charge de projet",
                    "reference": "Article",
                    "designation": "Designation",
                    "partie": "Partie",
                    "article_principal": "Article principal",
                    "quantite": "Quantite",
                    "colis": "Pre-Packing",
                    "lieu": "Stockage",
                    "bon": "Bon",
                }.get(key, key)
                text_line = f"{label_name} : {value}"
                for part in (_tw.wrap(text_line, width=43) or [text_line]):
                    if y < 118:
                        break
                    stream_lines += [
                        'BT', '/F1 10 Tf', f'{margin} {y:.2f} Td',
                        f'({pdf_escape(part)}) Tj', 'ET'
                    ]
                    y -= 15
                if y < 118:
                    break

            if qr_obj_num:
                qr_size = 76
                qr_x = page_width - margin - qr_size
                qr_y = 24
                stream_lines += [
                    'BT', '/F2 7 Tf', f'{qr_x:.2f} {qr_y + qr_size + 6:.2f} Td',
                    '(CARTE IDENTITE) Tj', 'ET',
                    'q',
                    f'{qr_size:.2f} 0 0 {qr_size:.2f} {qr_x:.2f} {qr_y:.2f} cm',
                    '/ImQR Do',
                    'Q',
                ]

            stream = "\n".join(stream_lines).encode("latin-1", errors="replace")
            content_obj_num = len(objects) + 1
            objects.append(
                f"<< /Length {len(stream)} >>\nstream\n".encode("latin-1")
                + stream + b"\nendstream"
            )
            page_obj_num = len(objects) + 1

            resources = '/Resources << /Font << /F1 3 0 R /F2 4 0 R >>'
            xobjects = []
            if logo_obj_num:
                xobjects.append(f'/ImLogo {logo_obj_num} 0 R')
            if qr_obj_num:
                xobjects.append(f'/ImQR {qr_obj_num} 0 R')
            if xobjects:
                resources += ' /XObject << ' + ' '.join(xobjects) + ' >>'
            resources += ' >>'

            page = (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {page_width} {page_height}] "
                f"{resources} /Contents {content_obj_num} 0 R >>"
            )
            objects.append(page.encode("latin-1"))
            page_refs.append(f"{page_obj_num} 0 R")

        objects[1] = (
            f"<< /Type /Pages /Kids [{' '.join(page_refs)}] /Count {len(page_refs)} >>"
        ).encode("latin-1")

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
        pdf.write(
            (
                f"trailer\n<< /Size {len(objects)+1} /Root 1 0 R >>\n"
                f"startxref\n{xref_pos}\n%%EOF"
            ).encode("latin-1")
        )
        return pdf.getvalue()

    # ------------------------------------------------------------------
    # Étiquettes COLIS : 100 x 148 mm + vrai logo ESI en haut à gauche.
    # ------------------------------------------------------------------
    page_width = 100 * 72 / 25.4
    page_height = 148 * 72 / 25.4
    margin = 16

    def pdf_escape(value):
        value = _as_text(value)
        return value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    # Charge le vrai logo du projet. Il n'est jamais recadré : on conserve son ratio.
    logo_path = APP_DIR / 'static' / 'logo.png'
    logo_image_bytes = None
    logo_w = logo_h = None
    if logo_path.exists():
        try:
            from PIL import Image
            with Image.open(logo_path) as im:
                # Le PDF utilise un JPEG RGB afin d'embarquer l'image sans dépendance externe.
                if im.mode != 'RGB':
                    bg = Image.new('RGB', im.size, 'white')
                    if 'A' in im.getbands():
                        bg.paste(im, mask=im.getchannel('A'))
                    else:
                        bg.paste(im.convert('RGB'))
                    im = bg
                logo_w, logo_h = im.size
                buf = io.BytesIO()
                im.save(buf, format='JPEG', quality=95)
                logo_image_bytes = buf.getvalue()
        except Exception as e:
            print(f'[ETIQUETTE COLIS] Logo ESI non charge: {e}')

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        None,
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>",
    ]

    image_obj_num = None
    if logo_image_bytes and logo_w and logo_h:
        image_obj_num = len(objects) + 1
        image_obj = (
            f'<< /Type /XObject /Subtype /Image /Width {logo_w} /Height {logo_h} '
            f'/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode /Length {len(logo_image_bytes)} >>\nstream\n'
        ).encode('latin-1') + logo_image_bytes + b'\nendstream'
        objects.append(image_obj)

    def build_qr_jpeg(value):
        """Construit le QR en mémoire. L'import local évite d'impacter les autres écrans."""
        value = _as_text(value).strip()
        if not value:
            return None, None, None
        try:
            import qrcode
            qr = qrcode.QRCode(
                version=None,
                error_correction=qrcode.constants.ERROR_CORRECT_M,
                box_size=8,
                border=3,
            )
            qr.add_data(value)
            qr.make(fit=True)
            im = qr.make_image(fill_color='black', back_color='white').convert('RGB')
            w, h = im.size
            buf = io.BytesIO()
            im.save(buf, format='JPEG', quality=100, subsampling=0)
            return buf.getvalue(), w, h
        except Exception as e:
            print(f'[ETIQUETTE COLIS] QR code non genere: {e}')
            return None, None, None

    page_refs = []

    for label in labels or [{"titre": "PRE-PACKING"}]:
        stream_lines = []

        # Un QR différent est embarqué sur chaque page car chaque colis a sa propre URL.
        qr_obj_num = None
        qr_bytes, qr_w, qr_h = build_qr_jpeg(label.get('qr_url'))
        if qr_bytes and qr_w and qr_h:
            qr_obj_num = len(objects) + 1
            qr_obj = (
                f'<< /Type /XObject /Subtype /Image /Width {qr_w} /Height {qr_h} '
                f'/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode /Length {len(qr_bytes)} >>\nstream\n'
            ).encode('latin-1') + qr_bytes + b'\nendstream'
            objects.append(qr_obj)

        # Logo en haut à gauche, avec zone réservée 92 x 48 points.
        logo_box_x = margin
        logo_box_y = page_height - 62
        logo_box_w = 92
        logo_box_h = 42
        if image_obj_num:
            scale = min(logo_box_w / float(logo_w), logo_box_h / float(logo_h))
            draw_w = logo_w * scale
            draw_h = logo_h * scale
            draw_x = logo_box_x
            draw_y = logo_box_y + (logo_box_h - draw_h) / 2
            stream_lines += [
                'q',
                f'{draw_w:.2f} 0 0 {draw_h:.2f} {draw_x:.2f} {draw_y:.2f} cm',
                '/Im1 Do',
                'Q',
            ]
        else:
            # Secours uniquement si static/logo.png est absent sur le serveur.
            stream_lines += [
                'BT', '/F2 20 Tf', f'{margin} {page_height - 40:.2f} Td', '(ESI) Tj', 'ET'
            ]

        # Titre de l'emballage en haut à droite du logo.
        title = _as_text(label.get('titre') or 'PRE-PACKING').strip()
        is_packing_label = title.upper() == 'PACKING'
        packaging_name = 'PACKING' if is_packing_label else 'PRE-PACKING'
        stream_lines += [
            'BT', '/F2 17 Tf', f'{page_width - 72:.2f} {page_height - 36:.2f} Td',
            f'({pdf_escape(title)}) Tj', 'ET'
        ]

        # Le N° COLIS reste l'information principale en grand.
        # Le N° BON DE RECEPTION est affiché plus bas à la place de l'ancien champ Bon.
        principal = _as_text(label.get('principal') or label.get('colis')).strip()
        y = page_height - 88
        if principal:
            stream_lines += [
                'BT', '/F2 9 Tf', f'{margin} {y:.2f} Td',
                f'(N\260 {packaging_name}) Tj', 'ET'
            ]
            y -= 18
            principal_size = 23 if len(principal) <= 18 else 19
            for part in _tw.wrap(principal, width=22) or [principal]:
                stream_lines += [
                    'BT', f'/F2 {principal_size} Tf', f'{margin} {y:.2f} Td',
                    f'({pdf_escape(part)}) Tj', 'ET'
                ]
                y -= principal_size + 9

        # Trait de séparation.
        y -= 2
        stream_lines += [
            '0.25 w', f'{margin} {y:.2f} m {page_width - margin:.2f} {y:.2f} l S'
        ]
        y -= 22

        fields = [
            (('packing_type' if is_packing_label else 'type_colis'), ('Type de Packing' if is_packing_label else 'Type de Pre-Packing')),
            ('dossier', 'Dossier'),
            ('client', 'Client'),
            ('charge_projet', 'Charge de projet'),
            ('lieu', 'Stockage'),
            ('bon', 'N° Bon reception'),
        ]
        for key, field_label in fields:
            value = _display_prepacking_type(label.get(key)) if key == 'type_colis' else _as_text(label.get(key)).strip()
            if not value:
                continue
            stream_lines += [
                'BT', '/F2 9 Tf', f'{margin} {y:.2f} Td',
                f'({pdf_escape(field_label.upper())}) Tj', 'ET'
            ]
            y -= 14
            wrapped = _tw.wrap(value, width=34) or [value]
            for part in wrapped[:3]:
                stream_lines += [
                    'BT', '/F1 11 Tf', f'{margin} {y:.2f} Td',
                    f'({pdf_escape(part)}) Tj', 'ET'
                ]
                y -= 14
            y -= 8

        # QR code : 27 mm environ, positionné en bas à droite pour ne pas perturber
        # la mise en page historique de l'étiquette 100 x 148 mm.
        if qr_obj_num:
            qr_size = 76
            qr_x = page_width - margin - qr_size
            qr_y = 28
            stream_lines += [
                'q',
                f'{qr_size:.2f} 0 0 {qr_size:.2f} {qr_x:.2f} {qr_y:.2f} cm',
                '/Qr1 Do',
                'Q',
                'BT', '/F2 6 Tf', f'{qr_x + 1:.2f} 17 Td',
                f'({"SCAN - CARTE IDENTITE PACKING" if is_packing_label else "SCAN - CONTENU PRE-PACKING"}) Tj', 'ET',
            ]

        stream = "\n".join(stream_lines).encode("latin-1", errors="replace")
        content_obj_num = len(objects) + 1
        objects.append(
            f"<< /Length {len(stream)} >>\nstream\n".encode("latin-1")
            + stream + b"\nendstream"
        )

        page_obj_num = len(objects) + 1
        xobjects = []
        if image_obj_num:
            xobjects.append(f'/Im1 {image_obj_num} 0 R')
        if qr_obj_num:
            xobjects.append(f'/Qr1 {qr_obj_num} 0 R')
        xobject = f" /XObject << {' '.join(xobjects)} >>" if xobjects else ""
        page = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {page_width:.4f} {page_height:.4f}] "
            f"/Resources << /Font << /F1 3 0 R /F2 4 0 R >>{xobject} >> "
            f"/Contents {content_obj_num} 0 R >>"
        )
        objects.append(page.encode('latin-1'))
        page_refs.append(f'{page_obj_num} 0 R')

    objects[1] = (
        f"<< /Type /Pages /Kids [{' '.join(page_refs)}] /Count {len(page_refs)} >>"
    ).encode('latin-1')

    pdf = io.BytesIO()
    pdf.write(b'%PDF-1.4\n')
    offsets = []
    for i, obj in enumerate(objects, start=1):
        offsets.append(pdf.tell())
        pdf.write(f'{i} 0 obj\n'.encode('latin-1'))
        pdf.write(obj)
        pdf.write(b'\nendobj\n')

    xref_pos = pdf.tell()
    pdf.write(f'xref\n0 {len(objects)+1}\n'.encode('latin-1'))
    pdf.write(b'0000000000 65535 f \n')
    for offset in offsets:
        pdf.write(f'{offset:010d} 00000 n \n'.encode('latin-1'))
    pdf.write(
        (
            f'trailer\n<< /Size {len(objects)+1} /Root 1 0 R >>\n'
            f'startxref\n{xref_pos}\n%%EOF'
        ).encode('latin-1')
    )
    return pdf.getvalue()

def _existing_colis_numbers(numero_dossier):
    """Retourne tous les numéros de colis déjà utilisés, quel que soit le type de réception."""
    numero_dossier = _as_text(numero_dossier).strip()
    if not numero_dossier: return set()
    used=set(); pattern=re.compile(r"^"+re.escape(numero_dossier)+r"-(\d+)$", re.I)
    def add(ref):
        m=pattern.fullmatch(_as_text(ref).strip())
        if m: used.add(int(m.group(1)))
    try:
        for ticket in list_tickets():
            enl = ticket.get("enlevement") or {}
            for bon in list(enl.get("bons_livraison") or []) + list(enl.get("bons_livraison_annules") or []):
                for ref in bon.get("colis") or []: add(ref)
            for rec in list(ticket.get("receptionsAvisArrivee") or []) + list(ticket.get("receptionsAvisArriveeAnnulees") or []):
                for ref in rec.get("colis") or []: add(ref)
        safe_dossier=urllib.parse.quote(numero_dossier,safe='')
        rows=supabase_rest_request("GET","articles",f"select=dernier_colis&dossier=eq.{safe_dossier}&limit=10000") or []
        for row in rows:
            for ref in _as_text(row.get("dernier_colis")).split(','): add(ref)
    except Exception as e: print(f"[COLIS] Lecture historique impossible pour {numero_dossier}: {e}")
    return used




def _resolve_colis_repartition(selected_items, raw_assignments, colis_refs):
    expected = {}
    for item in selected_items:
        units = item.get('reception_units') if isinstance(item.get('reception_units'), list) else []
        if units:
            for unit in units:
                key = (int(unit.get('index')), int(unit.get('unit_offset') or 0), int(unit.get('part_index') or 0))
                expected[key] = _as_text(unit.get('esi_id')).strip()
        else:
            idx = int(item.get('index'))
            for unit_offset, esi_id in enumerate(item.get('esi_ids') or []):
                expected[(idx, unit_offset, 0)] = esi_id
    assignments = {}
    for entry in raw_assignments or []:
        try:
            key = (int(entry.get('index')), int(entry.get('unit_offset') or 0), int(entry.get('part_index') or 0))
            ci = int(entry.get('colis_index'))
        except Exception:
            continue
        if key not in expected or ci < 0 or ci >= len(colis_refs):
            continue
        assignments[key] = ci
    if set(assignments) != set(expected):
        raise ValueError("Chaque Article ou partie physique doit être associé à un Pre-Packing.")
    used = set(assignments.values())
    if len(colis_refs) > len(expected):
        raise ValueError("Le nombre de Pre-Packings ne peut pas dépasser le nombre d'Articles ou parties physiques réceptionnés.")
    if used != set(range(len(colis_refs))):
        raise ValueError("Chaque Pre-Packing créé doit contenir au moins un Article ou une partie.")
    return {expected[k]: colis_refs[ci] for k, ci in assignments.items()}


def _apply_colis_to_selected_items(selected_items, colis_by_esi, reception_ref, lieu_stockage, colis_type_by_esi=None):
    colis_type_by_esi = dict(colis_type_by_esi or {})
    for item in selected_items:
        mapping={esi:colis_by_esi.get(esi,'') for esi in item.get('esi_ids') or []}
        type_mapping={esi:_normalise_colis_type(colis_type_by_esi.get(esi)) for esi in item.get('esi_ids') or []}
        item['colis_par_esi']=mapping
        item['type_colis_par_esi']=type_mapping
        item['colis']=list(dict.fromkeys(x for x in mapping.values() if x))
        item['types_colis']=list(dict.fromkeys(x for x in type_mapping.values() if x))
        item['reception_ref']=reception_ref; item['lieu_stockage']=lieu_stockage

def _allocate_colis_numbers(numero_dossier, count):
    """Génère des références dossier-001, dossier-002... jamais déjà utilisées."""
    numero_dossier = _as_text(numero_dossier).strip()
    count = int(count)
    if not numero_dossier or count < 1:
        return []

    used = _existing_colis_numbers(numero_dossier)
    refs = []
    n = 1
    while len(refs) < count:
        if n not in used:
            refs.append(f"{numero_dossier}-{n:03d}")
            used.add(n)
        n += 1
    return refs


@app.route('/api/tickets/<ticket_id>/bon-livraison', methods=['POST'])
def api_create_bon_livraison(ticket_id):
    """Valide une réception partielle/totale et génère BL + étiquettes articles + étiquettes colis."""
    ticket = load_ticket(ticket_id)
    if not ticket:
        return jsonify({'ok': False, 'error': 'Ticket introuvable'}), 404

    module_normalise = _as_text(ticket.get('module')).replace("’", "'").strip()
    if module_normalise not in ("Demande d'enlèvement", "Demande d'enlevement") and not _as_text(ticket_id).startswith('ENL-'):
        return jsonify({'ok': False, 'error': "Ce ticket n'est pas une demande d'enlèvement"}), 400

    data = request.get_json(silent=True) or {}
    receptionne_par = _as_text(data.get('receptionne_par')).strip()
    lieu_stockage = _as_text(data.get('lieu_stockage')).strip()
    numero_dossier = _as_text(data.get('numero_dossier')).strip()

    # Secours serveur : si l'interface n'envoie pas le N° dossier,
    # on reprend automatiquement la référence principale du bon/ticket.
    enl_for_dossier = dict(ticket.get('enlevement') or {})
    if not numero_dossier:
        numero_dossier = _as_text(
            enl_for_dossier.get('numero_dossier')
            or enl_for_dossier.get('dossier_numero')
            or enl_for_dossier.get('numero_bon')
            or ticket.get('numeroDossier')
            or ticket.get('numero_dossier')
            or ticket.get('ref')
        ).strip()

    try:
        nombre_colis = int(data.get('nombre_colis') or 0)
    except (TypeError, ValueError):
        nombre_colis = 0

    items_reception = data.get('items_reception')
    selected_indexes = data.get('selected_indexes') or []
    colis_repartition = data.get('colis_repartition') or []
    article_parts = data.get('article_parts') or []

    if not receptionne_par:
        return jsonify({'ok': False, 'error': 'Nom et prénom du réceptionnaire obligatoires'}), 400
    if not lieu_stockage:
        return jsonify({'ok': False, 'error': 'Lieu de stockage obligatoire'}), 400
    if not numero_dossier:
        return jsonify({'ok': False, 'error': 'N° dossier obligatoire pour numéroter les Pre-Packings'}), 400
    if nombre_colis < 1:
        return jsonify({'ok': False, 'error': 'Le nombre total de Pre-Packings doit être supérieur ou égal à 1'}), 400

    # Compatibilité avec l'ancienne interface : si elle n'envoie que selected_indexes,
    # on réceptionne le reliquat complet de chaque article sélectionné.
    if not isinstance(items_reception, list):
        items_reception = []
        for raw_idx in selected_indexes if isinstance(selected_indexes, list) else []:
            try:
                items_reception.append({'index': int(raw_idx), 'quantite_recue': None})
            except Exception:
                pass

    if not items_reception:
        return jsonify({'ok': False, 'error': 'Sélectionne au moins un article à réceptionner'}), 400

    # Sécurise les anciennes demandes créées avant l'ajout du référentiel articles.
    try:
        _ensure_articles_for_ticket(ticket, save=True)
    except Exception as e:
        return jsonify({'ok': False, 'error': f"Impossible d'attribuer les numéros ESI : {e}"}), 500

    enl = dict(ticket.get('enlevement') or {})
    all_items = list(enl.get('items') or [])
    selected = []
    article_labels = []
    reception_esi_ids = []
    now = datetime.now()

    def _qty_int(value, default=0):
        try:
            return max(0, int(float(str(value).replace(',', '.'))))
        except Exception:
            return default

    def _item_fully_received(item):
        planned = max(1, _qty_int(item.get('quantite') or 1, 1))
        received = _qty_int(item.get('quantite_recue_totale'), -1)
        if received < 0:
            received = planned if item.get('receptionne') else 0
        return received >= planned

    if all_items and all(_item_fully_received(dict(x or {})) for x in all_items):
        return jsonify({
            'ok': False,
            'error': 'Réception clôturée : tous les articles ont déjà été réceptionnés.'
        }), 409

    for entry in items_reception:
        if not isinstance(entry, dict):
            continue
        try:
            idx = int(entry.get('index'))
        except Exception:
            continue
        if idx < 0 or idx >= len(all_items):
            continue

        item = dict(all_items[idx] or {})
        planned = max(1, _qty_int(item.get('quantite') or 1, 1))

        # Compatibilité avec les anciennes réceptions qui ne stockaient qu'un booléen.
        previous = _qty_int(item.get('quantite_recue_totale'), -1)
        if previous < 0:
            previous = planned if item.get('receptionne') else 0

        remaining = max(planned - previous, 0)
        if remaining <= 0:
            continue

        requested = entry.get('quantite_recue')
        qty_received = remaining if requested in (None, '', 0, '0') else _qty_int(requested, 0)
        if qty_received < 1:
            continue
        if qty_received > remaining:
            return jsonify({
                'ok': False,
                'error': f"Quantité reçue trop élevée pour {item.get('reference') or idx} : reste {remaining}"
            }), 400

        new_total = previous + qty_received

        history = list(item.get('receptions') or [])
        history.append({
            'date': now.isoformat(),
            'quantite': qty_received,
            'receptionne_par': receptionne_par,
            'lieu_stockage': lieu_stockage,
        })

        item['quantite_recue_totale'] = new_total
        item['receptionne'] = new_total >= planned
        item['receptionne_le'] = now.isoformat()
        item['receptionne_par'] = receptionne_par
        item['lieu_stockage'] = lieu_stockage
        item['receptions'] = history
        all_items[idx] = item

        received_esi_ids = _article_ids_for_received_units(item, previous, qty_received)
        reception_esi_ids.extend(received_esi_ids)

        selected_item = {
            'index': idx,
            'quantite': str(qty_received),
            'quantite_prevue': str(planned),
            'quantite_deja_recue': str(previous),
            'quantite_recue_totale': str(new_total),
            'designation': _as_text(item.get('designation')).strip(),
            'reference': _as_text(item.get('reference')).strip(),
            'dimensions': _as_text(item.get('dimensions')).strip(),
            'esi_ids': received_esi_ids,
        }
        selected.append(selected_item)

        # Une étiquette par unité réellement réceptionnée, avec son ESI-x unique.
        for unit_no, esi_id in enumerate(received_esi_ids, start=1):
            article_labels.append({
                'titre': 'ARTICLE',
                'principal': esi_id,
                'esi_id': esi_id,
                'dossier': numero_dossier,
                'client': enl.get('client') or ticket.get('dossier') or '',
                'reference': _as_text(item.get('reference')).strip(),
                'designation': _as_text(item.get('designation')).strip(),
                'quantite': f"{unit_no}/{qty_received}",
                'lieu': lieu_stockage,
                'qr_url': _article_qr_url(esi_id),
            })

    if not selected:
        return jsonify({'ok': False, 'error': 'Aucun article valide sélectionné'}), 400

    with _BLR_LOCK:
        blr_ref = _next_blr_reference()
        colis_refs = _allocate_colis_numbers(numero_dossier, nombre_colis)
        colis_types = _resolve_colis_types(data.get('colis_types'), colis_refs)
        if any(not colis_types.get(ref) for ref in colis_refs):
            return jsonify({'ok': False, 'error': 'Le type de chaque Pre-Packing est obligatoire : Softpack, Carton ou Packing bois.'}), 400
        try:
            _validate_colis_repartition_shape(selected, colis_repartition, len(colis_refs), article_parts)
            reception_esi_ids, parent_part_ids = _expand_selected_items_with_parts(selected, article_parts)
        except ValueError as e:
            return jsonify({'ok': False, 'error': str(e)}), 400
        except Exception as e:
            return jsonify({'ok': False, 'error': f"Impossible de créer les parties de l'article : {e}"}), 500
        charge_projet = _as_text(enl.get('coordinateur') or ticket.get('chargeProjet') or '').strip()
        article_labels = _build_article_labels_from_selected(
            selected,
            numero_dossier,
            enl.get('client') or ticket.get('dossier') or '',
            lieu_stockage,
            charge_projet,
        )
        try:
            colis_by_esi = _resolve_colis_repartition(selected, colis_repartition, colis_refs)
        except ValueError as e:
            return jsonify({'ok': False, 'error': str(e)}), 400
        colis_type_by_esi = {esi: colis_types.get(ref, '') for esi, ref in colis_by_esi.items()}
        _apply_colis_to_selected_items(selected, colis_by_esi, blr_ref, lieu_stockage, colis_type_by_esi)

        for label in article_labels:
            label['colis'] = colis_by_esi.get(label.get('esi_id'), '')
            label['type_colis'] = colis_type_by_esi.get(label.get('esi_id'), '')

        bon = {
            'reference': blr_ref,
            'ticket_id': ticket_id,
            'client': enl.get('client') or ticket.get('dossier') or '',
            'numero_bon_enlevement': enl.get('numero_bon') or ticket.get('ref') or '',
            'numero_dossier': numero_dossier,
            'receptionne_par': receptionne_par,
            'lieu_stockage': lieu_stockage,
            'date_reception': now.strftime("%d/%m/%Y %H:%M"),
            'created_at': now.isoformat(),
            'nombre_colis': nombre_colis,
            'colis': colis_refs,
            'colis_types': colis_types,
            'article_esi_ids': list(reception_esi_ids),
            'items': selected,
        }

        # BL principal
        pdf_bytes = _build_blr_pdf_bytes(ticket, bon)
        filename = f"{blr_ref}.pdf"
        storage_path = f"{ticket_id}/bons_livraison/{now.strftime('%Y%m%d%H%M%S')}_{filename}"

        # Étiquettes articles
        article_labels_bytes = _build_labels_pdf_bytes(article_labels, kind="article")
        article_labels_filename = f"{blr_ref}_etiquettes_articles.pdf"
        article_labels_path = f"{ticket_id}/bons_livraison/{now.strftime('%Y%m%d%H%M%S')}_{article_labels_filename}"

        # Étiquettes colis
        colis_labels = [{
            'titre': 'PRE-PACKING',
            'principal': _colis_display(colis_ref, colis_types.get(colis_ref)),
            'dossier': numero_dossier,
            'client': enl.get('client') or ticket.get('dossier') or '',
            'charge_projet': charge_projet,
            'colis': colis_ref,
            'type_colis': colis_types.get(colis_ref, ''),
            'lieu': lieu_stockage,
            'bon': blr_ref,
            'qr_url': _colis_qr_url(colis_ref),
        } for colis_ref in colis_refs]
        colis_labels_bytes = _build_labels_pdf_bytes(colis_labels, kind="colis")
        colis_labels_filename = f"{blr_ref}_etiquettes_pre_packing.pdf"
        colis_labels_path = f"{ticket_id}/bons_livraison/{now.strftime('%Y%m%d%H%M%S')}_{colis_labels_filename}"

        try:
            supabase_upload_bytes(storage_path, pdf_bytes, "application/pdf")
            supabase_upload_bytes(article_labels_path, article_labels_bytes, "application/pdf")
            supabase_upload_bytes(colis_labels_path, colis_labels_bytes, "application/pdf")
        except Exception as e:
            print(f"[BLR] Erreur upload PDF: {e}")
            return jsonify({'ok': False, 'error': f'Impossible d’enregistrer les PDF : {e}'}), 500

        # Une fois les documents générés, crée les fiches COLIS dans la base Articles.
        try:
            colis_records, colis_esi_by_ref = _ensure_colis_article_records(
                ticket_id=ticket_id,
                numero_dossier=numero_dossier,
                colis_refs=colis_refs,
                colis_types=colis_types,
                colis_by_esi=colis_by_esi,
                selected_items=selected,
                client=enl.get('client') or ticket.get('dossier') or '',
                projet=enl.get('exhibition') or ticket.get('expo') or ticket.get('objet') or '',
                charge_projet=charge_projet,
                lieu_stockage=lieu_stockage,
                reception_ref=blr_ref,
            )
        except Exception as e:
            print(f"[COLIS ARTICLES] Creation impossible pour {blr_ref}: {e}")
            return jsonify({'ok': False, 'error': f'Impossible de créer les Pre-Packings dans la base Articles : {e}'}), 500

        bon['filename'] = filename
        bon['storage_path'] = storage_path
        bon['etiquettes_articles_filename'] = article_labels_filename
        bon['etiquettes_articles_path'] = article_labels_path
        bon['etiquettes_colis_filename'] = colis_labels_filename
        bon['etiquettes_colis_path'] = colis_labels_path

        enl['items'] = all_items
        enl['references'] = [
            _as_text(x.get('reference')).strip()
            for x in all_items
            if _as_text(x.get('reference')).strip()
        ]
        bons = list(enl.get('bons_livraison') or [])
        bons.append(bon)
        enl['bons_livraison'] = bons
        ticket['enlevement'] = enl

        manager_sheets = list(ticket.get('managerSheets') or [])
        for name, size, path in [
            (filename, len(pdf_bytes), storage_path),
            (article_labels_filename, len(article_labels_bytes), article_labels_path),
            (colis_labels_filename, len(colis_labels_bytes), colis_labels_path),
        ]:
            manager_sheets.append({'name': name, 'size': size, 'path': path})
        ticket['managerSheets'] = manager_sheets
        ticket['updatedAt'] = now.isoformat()
        save_ticket(ticket)
        try:
            _update_article_logistics(
                reception_esi_ids,
                lieu_stockage=lieu_stockage,
                statut_logistique="Réceptionné",
                colis_by_esi=colis_by_esi,
                colis_type_by_esi=colis_type_by_esi,
                colis_esi_by_ref=colis_esi_by_ref,
                reception_ref=blr_ref,
                receptionne_par=receptionne_par,
            )
        except Exception as e:
            print(f"[ARTICLES] Mise à jour logistique BLR impossible: {e}")

    return jsonify({
        'ok': True,
        'reference': blr_ref,
        'filename': filename,
        'bon': bon,
        'colis': colis_refs,
        'colis_types': colis_types,
        'colis_esi': colis_esi_by_ref,
        'etiquettes_articles_filename': article_labels_filename,
        'etiquettes_colis_filename': colis_labels_filename,
    })


@app.route('/api/tickets/<ticket_id>/status', methods=['PATCH'])
def api_update_status(ticket_id):
    ticket = load_ticket(ticket_id)
    if not ticket:
        return jsonify({'error': 'Ticket introuvable'}), 404

    ancien_statut = _as_text(ticket.get('status')).strip()

    data = request.get_json(silent=True) or {}
    nouveau_statut = _as_text(data.get('status', ancien_statut)).strip()
    now_iso = datetime.now().isoformat()
    mise_en_caisse_result = None

    # Une demande "Mise en caisse" ne modifie la Base Articles qu'au moment où
    # elle est réellement validée par le passage au statut Terminé.
    # L'appel reste volontairement idempotent : un ancien ticket déjà Terminé peut
    # être resoumis au même statut afin de reconstruire ses liens si nécessaire.
    if nouveau_statut == 'Terminé' and _as_text(ticket.get('module')).strip() == 'Mise en caisse':
        try:
            mise_en_caisse_result = _apply_mise_en_caisse_to_articles(ticket)
        except ValueError as e:
            return jsonify({
                'ok': False,
                'error': str(e),
                'mise_en_caisse_appliquee': False,
            }), 409
        except Exception as e:
            print(f"[MISE EN CAISSE] Application du ticket {ticket_id} impossible: {e}")
            return jsonify({
                'ok': False,
                'error': 'Impossible de mettre à jour la Base Articles : ' + str(e),
                'mise_en_caisse_appliquee': False,
            }), 500

    # A partir de cette version, on conserve la vraie date du PREMIER passage
    # au statut Terminé. Le champ reste dans raw_json : aucune colonne Supabase
    # supplémentaire n'est nécessaire.
    if nouveau_statut == 'Terminé' and ancien_statut != 'Terminé':
        ticket.setdefault('termineAt', now_iso)

    ticket['status'] = nouveau_statut
    ticket['updatedAt'] = now_iso
    save_ticket(ticket)

    # L'envoi automatique SMTP est volontairement désactivé.
    # La notification se prépare maintenant via Outlook Web avec le bouton "Envoyer Notif".
    return jsonify({
        'ok': True,
        'termineAt': ticket.get('termineAt', ''),
        'mise_en_caisse': mise_en_caisse_result,
    })


@app.route('/api/tickets/<ticket_id>/annuler-enlevement', methods=['PATCH'])
def api_annuler_enlevement(ticket_id):
    """Annule un ticket du planning réception uniquement s'il n'a plus de réception active."""
    ticket = load_ticket(ticket_id)
    if not ticket:
        return jsonify({'ok': False, 'error': 'Ticket introuvable'}), 404

    module_normalise = _as_text(ticket.get('module')).replace("’", "'").strip()
    is_enlevement = (
        module_normalise in ("Demande d'enlèvement", "Demande d'enlevement")
        or _as_text(ticket_id).startswith('ENL-')
    )
    is_avis = (
        module_normalise == "Avis d'arrivée"
        or _as_text(ticket_id).startswith('ARR-')
    )
    if not (is_enlevement or is_avis):
        return jsonify({'ok': False, 'error': "Ce ticket n'appartient pas au planning réception"}), 400

    if is_avis:
        active_receptions = [
            r for r in (ticket.get('receptionsAvisArrivee') or [])
            if not bool((r or {}).get('annulee'))
        ]
    else:
        enl = ticket.get('enlevement') or {}
        active_receptions = [
            r for r in (enl.get('bons_livraison') or [])
            if not bool((r or {}).get('annulee'))
        ]

    if active_receptions:
        refs = [
            _as_text((r or {}).get('reference')).strip()
            for r in active_receptions
            if _as_text((r or {}).get('reference')).strip()
        ]
        detail = ', '.join(refs) if refs else f"{len(active_receptions)} réception(s) active(s)"
        return jsonify({
            'ok': False,
            'error': f"Impossible d'annuler le ticket : annule d'abord la/les réception(s) active(s) ({detail}).",
            'active_receptions': len(active_receptions),
            'references': refs,
        }), 409

    ticket['status'] = 'Annulé'
    ticket['annule_le'] = datetime.now().isoformat()
    ticket['updatedAt'] = ticket['annule_le']
    save_ticket(ticket)

    checked = load_ticket(ticket_id)
    if not checked or _as_text(checked.get('status')).strip().lower() not in ('annulé', 'annule'):
        return jsonify({'ok': False, 'error': "Le ticket n'a pas pu être confirmé comme annulé après sauvegarde."}), 500

    return jsonify({'ok': True, 'status': 'Annulé'})


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
        link_text = f"Consulter la fiche de Packing {_packing_reference(dossier, ref)}"
        intro = "Le Packing suivant a été commandé :"
        details = f"""
        <p>
          <strong>Dossier :</strong> {dossier or '-'}<br>
          <strong>N° Packing / Référence :</strong> {_packing_reference(dossier, ref) or '-'}<br>
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
        link_text = f"Consulter la fiche de Packing {_packing_reference(dossier, ref)}"
        intro = "Le Packing suivant a été commandé :"
        details = f"""
        <p>
          <strong>Dossier :</strong> {esc(dossier)}<br>
          <strong>N° Packing / Référence :</strong> {esc(_packing_reference(dossier, ref))}<br>
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



def _linked_articles_for_ticket(ticket):
    """Retourne les articles lies a une fiche de caisse avec dossier/reference a jour."""
    stored = ticket.get('articles_lies') or []
    esi_ids = []
    for item in stored:
        esi_id = _as_text(item.get('esi_id') if isinstance(item, dict) else item).strip()
        if esi_id and esi_id not in esi_ids:
            esi_ids.append(esi_id)

    if not esi_ids:
        return []

    rows_by_id = {}
    for offset in range(0, len(esi_ids), 100):
        part = esi_ids[offset:offset + 100]
        encoded = urllib.parse.quote(','.join(part), safe=',-_')
        rows = supabase_rest_request(
            'GET', 'articles',
            'select=esi_id,dossier,reference,description,client,projet,type_objet&esi_id=in.(' + encoded + ')&limit=100'
        ) or []
        for row in rows:
            esi_id = _as_text(row.get('esi_id')).strip()
            type_objet = _as_text(row.get('type_objet') or 'PRODUIT').strip().upper()
            if not esi_id or type_objet == 'CONTENANT':
                continue
            rows_by_id[esi_id] = {
                'esi_id': esi_id,
                'dossier': _as_text(row.get('dossier')).strip(),
                'reference': _as_text(row.get('reference')).strip(),
                'description': _as_text(row.get('description')).strip(),
                'client': _as_text(row.get('client')).strip(),
                'projet': _as_text(row.get('projet')).strip(),
            }

    return [rows_by_id[x] for x in esi_ids if x in rows_by_id]


@app.route('/api/tickets/<ticket_id>/articles-lies', methods=['GET', 'PUT'])
def api_ticket_articles_lies(ticket_id):
    ticket = load_ticket(ticket_id)
    if not ticket:
        return jsonify({'ok': False, 'error': 'Ticket introuvable'}), 404
    if _as_text(ticket.get('module')).strip() != 'Fiche de caisse':
        return jsonify({'ok': False, 'error': "Ce ticket n'est pas une fiche de Packing"}), 400

    if request.method == 'GET':
        try:
            articles = _linked_articles_for_ticket(ticket)
            return jsonify({'ok': True, 'articles': articles, 'count': len(articles)})
        except Exception as e:
            return jsonify({'ok': False, 'error': str(e), 'articles': []}), 500

    data = request.get_json(silent=True) or {}
    raw_ids = data.get('esi_ids') or []
    if not isinstance(raw_ids, list):
        return jsonify({'ok': False, 'error': 'Liste des articles invalide'}), 400

    esi_ids = []
    for value in raw_ids:
        esi_id = _as_text(value).strip()
        if esi_id and esi_id not in esi_ids:
            esi_ids.append(esi_id)

    # Référence canonique du Packing : C-N° dossier-N° Packing.
    # Le champ technique ticket.ref reste inchangé pour la compatibilité historique.
    dossier_caisse = _as_text(ticket.get('dossier')).strip()
    numero_brut = _as_text(ticket.get('ref')).strip()
    numero_norm = _normalise_numero_caisse(numero_brut) if numero_brut else ''
    if not dossier_caisse or not numero_norm:
        return jsonify({
            'ok': False,
            'error': "Impossible de déterminer la référence du Packing (N° dossier ou N° Packing manquant)."
        }), 400

    caisse_ref = _packing_reference(dossier_caisse, numero_brut)
    legacy_ref = _legacy_packing_reference(dossier_caisse, numero_brut)
    equivalent_refs = {x for x in (caisse_ref, legacy_ref, f"{dossier_caisse}-{numero_norm}") if x}

    # Mémorise l'ancienne sélection pour savoir quels articles ont été retirés.
    previous_ids = []
    for item in ticket.get('articles_lies') or []:
        previous_id = _as_text(item.get('esi_id') if isinstance(item, dict) else item).strip()
        if previous_id and previous_id not in previous_ids:
            previous_ids.append(previous_id)

    all_ids = list(dict.fromkeys(previous_ids + esi_ids))

    try:
        rows_by_id = {}
        for offset in range(0, len(all_ids), 100):
            part = all_ids[offset:offset + 100]
            if not part:
                continue
            encoded = urllib.parse.quote(','.join(part), safe=',-_')
            rows = supabase_rest_request(
                'GET', 'articles',
                'select=*&esi_id=in.(' + encoded + ')&limit=100'
            ) or []
            for row in rows:
                row_esi = _as_text(row.get('esi_id')).strip()
                if row_esi:
                    rows_by_id[row_esi] = dict(row)

        selected = []
        missing = []
        conflicts = []

        for esi_id in esi_ids:
            row = rows_by_id.get(esi_id)
            if not row:
                missing.append(esi_id)
                continue

            type_objet = _as_text(row.get('type_objet') or 'PRODUIT').strip().upper()
            if type_objet == 'CONTENANT':
                missing.append(esi_id)
                continue

            current_ref = _as_text(row.get('ref_caisse')).strip()
            if current_ref and current_ref not in equivalent_refs:
                conflicts.append({
                    'esi_id': esi_id,
                    'ref_caisse': current_ref,
                })
                continue

            selected.append({
                'esi_id': esi_id,
                'dossier': _as_text(row.get('dossier')).strip(),
                'reference': _as_text(row.get('reference')).strip(),
            })

        if missing:
            return jsonify({
                'ok': False,
                'error': 'Certains Articles sont introuvables ou sont des Pre-Packings : ' + ', '.join(missing[:10])
            }), 400

        if conflicts:
            details = ', '.join(
                f"{x['esi_id']} ({x['ref_caisse']})" for x in conflicts[:10]
            )
            return jsonify({
                'ok': False,
                'error': "Certains Articles sont déjà liés à un autre Packing : " + details
            }), 409

        now = datetime.now().isoformat()
        changed_articles = []

        with _ARTICLE_LOCK:
            try:
                # 1) Met à jour la table ARTICLES.
                for esi_id in all_ids:
                    row = rows_by_id.get(esi_id)
                    if not row:
                        continue

                    current_ref = _as_text(row.get('ref_caisse')).strip()
                    current_colis = _as_text(row.get('dernier_colis')).strip()

                    if esi_id in esi_ids:
                        # Article coché : le Packing et le Pre-Packing sont désormais deux notions distinctes.
                        # On renseigne uniquement la référence Packing C-... et on conserve le Pre-Packing actuel.
                        target_ref = caisse_ref
                        target_colis = current_colis
                    elif esi_id in previous_ids:
                        # Article décoché : retire uniquement le Packing de CETTE fiche.
                        # Le Pre-Packing reste inchangé.
                        target_ref = '' if current_ref in equivalent_refs else current_ref
                        target_colis = current_colis
                    else:
                        continue

                    if current_ref == target_ref and current_colis == target_colis:
                        continue

                    patch = {
                        'ref_caisse': target_ref,
                        'dernier_colis': target_colis,
                        'updated_at': now,
                    }
                    merged = dict(row)
                    merged.update(patch)
                    patch['search_text'] = _article_search_text(merged)

                    supabase_rest_request(
                        'PATCH', 'articles',
                        'esi_id=eq.' + urllib.parse.quote(esi_id, safe='-'),
                        patch,
                        prefer='return=minimal'
                    )
                    changed_articles.append({
                        'esi_id': esi_id,
                        'ref_caisse': current_ref,
                        'dernier_colis': current_colis,
                        'updated_at': row.get('updated_at'),
                        'search_text': row.get('search_text'),
                    })

                # 2) Conserve aussi la sélection dans le ticket comme auparavant.
                ticket['articles_lies'] = selected
                ticket['updatedAt'] = now
                save_ticket(ticket)

            except Exception:
                # Si la sauvegarde échoue, remet au mieux les articles dans leur état précédent.
                for old in reversed(changed_articles):
                    try:
                        rollback_patch = {
                            'ref_caisse': old.get('ref_caisse') or '',
                            'dernier_colis': old.get('dernier_colis') or '',
                            'updated_at': old.get('updated_at') or now,
                            'search_text': old.get('search_text') or '',
                        }
                        supabase_rest_request(
                            'PATCH', 'articles',
                            'esi_id=eq.' + urllib.parse.quote(old['esi_id'], safe='-'),
                            rollback_patch,
                            prefer='return=minimal'
                        )
                    except Exception as rollback_error:
                        print(
                            f"[ARTICLES LIES] Rollback impossible pour {old.get('esi_id')}: "
                            f"{rollback_error}"
                        )
                raise

        return jsonify({
            'ok': True,
            'articles': selected,
            'count': len(selected),
            'ref_caisse': caisse_ref,
            'numero_packing': caisse_ref,
            'numero_colis': caisse_ref,
        })

    except Exception as e:
        print(f"[ARTICLES LIES] Erreur synchro caisse {ticket_id}: {e}")
        return jsonify({'ok': False, 'error': str(e)}), 500


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
        "ID","Module","Statut","Date création","Date rendu","Délai RDV → rendu (jours)",
        "Dossier / Client","Réf / N° Packing","Chargé de projet","Projet / Expo",
        "Type de Packing","Dimensions","Prix devis",
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

    def parse_datetime(value):
        """Accepte les formats ISO historiques utilisés dans ESI TICKETS."""
        txt = _as_text(value).strip()
        if not txt or txt == '-':
            return None
        try:
            return datetime.fromisoformat(txt.replace('Z', '+00:00'))
        except Exception:
            pass
        for fmt in ('%Y-%m-%d', '%d/%m/%Y %H:%M', '%d/%m/%Y'):
            try:
                return datetime.strptime(txt, fmt)
            except Exception:
                pass
        return None

    def parse_date_only(value):
        dt = parse_datetime(value)
        return dt.date() if dt is not None else None

    def storage_path_datetime(path):
        """Récupère le timestamp YYYYMMDDHHMMSS présent dans les chemins d'upload historiques."""
        txt = _as_text(path).strip()
        if not txt:
            return None
        matches = re.findall(r'(?:^|/)(\d{14})_', txt)
        if not matches:
            return None
        try:
            return datetime.strptime(matches[-1], '%Y%m%d%H%M%S')
        except Exception:
            return None

    def historical_termine_datetime(ticket):
        """Retrouve la meilleure date de fin historique disponible sans utiliser updatedAt seul."""
        if _as_text(ticket.get('status')).strip() != 'Terminé':
            return None

        # 1) Date explicite : exacte si elle existe.
        for key in (
            'termineAt', 'termine_at', 'terminatedAt', 'completedAt',
            'finishedAt', 'closedAt', 'termine_le', 'dateTerminee'
        ):
            dt = parse_datetime(ticket.get(key))
            if dt is not None:
                return dt

        created_dt = parse_datetime(ticket.get('createdAt'))
        updated_dt = parse_datetime(ticket.get('updatedAt'))

        # 2) Document gestionnaire : son chemin Supabase contient son vrai timestamp d'upload.
        manager_dates = []
        manager_items = list(ticket.get('managerSheets') or [])
        legacy = ticket.get('managerSheet')
        if isinstance(legacy, dict):
            manager_items.append(legacy)
        for item in manager_items:
            if not isinstance(item, dict):
                continue
            dt = storage_path_datetime(item.get('path'))
            if dt is None:
                continue
            if created_dt is not None and dt.date() < created_dt.date():
                continue
            if updated_dt is not None and dt.date() > updated_dt.date():
                continue
            manager_dates.append(dt)
        if manager_dates:
            return max(manager_dates)

        # 3) Avis d'arrivée / enlèvement : dernière réception active comme indice de clôture.
        # Ce cas n'est jamais utilisé pour une Fiche de caisse, car sa réception est postérieure
        # à la fabrication et ne correspond pas au délai de traitement du ticket.
        module = _as_text(ticket.get('module')).replace('’', "'").strip()
        reception_dates = []
        if module == "Avis d'arrivée":
            receptions = ticket.get('receptionsAvisArrivee') or []
            for reception in receptions:
                if not isinstance(reception, dict) or reception.get('annulee'):
                    continue
                dt = parse_datetime(
                    reception.get('receptionnee_le')
                    or reception.get('created_at')
                    or reception.get('date_reception')
                )
                if dt is not None:
                    reception_dates.append(dt)
        elif module in ("Demande d'enlèvement", "Demande d'enlevement"):
            receptions = (ticket.get('enlevement') or {}).get('bons_livraison') or []
            for reception in receptions:
                if not isinstance(reception, dict) or reception.get('annulee'):
                    continue
                dt = parse_datetime(
                    reception.get('receptionnee_le')
                    or reception.get('created_at')
                    or reception.get('date_reception')
                )
                if dt is not None:
                    reception_dates.append(dt)
        if reception_dates:
            candidate = max(reception_dates)
            if created_dt is None or candidate.date() >= created_dt.date():
                if updated_dt is None or candidate.date() <= updated_dt.date():
                    return candidate

        # 4) Aller voir : updatedAt n'est retenu que lorsqu'il correspond au jour du RDV
        # ou au lendemain. Sinon la date reste vide car elle serait trop incertaine.
        if module == 'Demande Aller voir' and updated_dt is not None:
            rdv_date = parse_date_only(ticket.get('dateRdv'))
            if rdv_date is not None:
                delta = (updated_dt.date() - rdv_date).days
                if 0 <= delta <= 1:
                    return updated_dt

        return None

    for t in tickets:
        fiche = t.get('fiche', {}) or {}
        date_creation = parse_date_only(t.get('createdAt'))
        termine_dt = historical_termine_datetime(t)
        date_terminee = termine_dt.date() if termine_dt is not None else None

        # Le délai métier correspond au temps entre la date de RDV et le rendu du ticket.
        # Si l'une des deux dates manque, ou si la date de rendu est antérieure au RDV,
        # le délai reste vide afin de ne pas afficher une valeur trompeuse.
        date_rdv = parse_date_only(t.get('dateRdv'))
        delai_jours = None
        if date_rdv is not None and date_terminee is not None:
            delta = (date_terminee - date_rdv).days
            if delta >= 0:
                delai_jours = delta

        ws.append([
            t.get('id',''),
            _module_display_label(t.get('module','')),
            t.get('status',''),
            date_creation,
            date_terminee,
            delai_jours,
            t.get('dossier',''),
            (_packing_reference(t.get('dossier'), t.get('ref')) if t.get('module') == 'Fiche de caisse' else t.get('ref','')),
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

    # Dates au format français SANS heure et délai en nombre entier de jours.
    for row in range(2, ws.max_row + 1):
        ws.cell(row=row, column=4).number_format = 'dd/mm/yyyy'
        ws.cell(row=row, column=5).number_format = 'dd/mm/yyyy'
        ws.cell(row=row, column=6).number_format = '0'
        for col in [13, 14, 15]:
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
    add_wrapped(lines, "Module", _module_display_label(ticket.get('module')))
    add_wrapped(lines, "Statut", ticket.get('status'))
    add_wrapped(lines, "Dossier / Client", ticket.get('dossier'))
    add_wrapped(lines, "Reference", _packing_reference(ticket.get('dossier'), ticket.get('ref')) if ticket.get('module') == 'Fiche de caisse' else ticket.get('ref'))
    add_wrapped(lines, "Charge de projet", ticket.get('chargeProjet'))
    add_wrapped(lines, "Projet / Expo", ticket.get('expo') or ticket.get('objet'))
    add_wrapped(lines, "Preteur", ticket.get('preteur'))
    add_wrapped(lines, "Type de Packing", ticket.get('typeCaisse'))
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
        add_wrapped(lines, "Type Packing fiche", fiche.get('typeCaisseFiche'))
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
