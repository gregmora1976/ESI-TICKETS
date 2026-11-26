from __future__ import annotations

import json
import mimetypes
import os
import math
import tempfile
import shutil
import unicodedata
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlparse, parse_qs, quote

from .matrice_caisses.t1 import T1Inputs, calculer_t1, options_t1
from .matrice_caisses.t1_t6 import T1T6Inputs, OeuvreT1T6, calculer_t1_t6, options_t1_t6
from .matrice_caisses.mrt import MRTInputs, calculer_mrt, options_mrt
from .matrice_caisses.t1_t3_mrt import T1T3MRTInputs, MRTItem, calculer_t1_t3_mrt, options_t1_t3_mrt
from .matrice_caisses.t_glissieres import TGlissieresInputs, TableauGlissiere, calculer_t_glissieres, options_t_glissieres
from .matrice_caisses.t_separations_mousse import TSeparationsMousseInputs, OeuvreSeparationMousse, calculer_t_separations_mousse, options_t_separations_mousse
from .matrice_caisses.objet1 import Objet1Inputs, calculer_objet1, options_objet1
from .matrice_caisses.objet2a6 import Objet2A6Inputs, Objet2A6Item, calculer_objet2a6, options_objet2a6
from .matrice_caisses.caisse_deco import CaisseDecoInputs, calculer_caisse_deco, options_caisse_deco, DEFAULT_PRICES

from .matrice_caisses import integration_2026 as engine2026

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "5000"))


def load_caisse_deco_prices() -> dict:
    """Charge les tarifs Caisse déco depuis data/caisse_deco_prices.json."""
    path = DATA_DIR / "caisse_deco_prices.json"
    prices = dict(DEFAULT_PRICES)
    try:
        if path.exists():
            saved = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                for key in prices:
                    if key in saved:
                        prices[key] = float(str(saved[key]).replace(",", "."))
    except Exception:
        pass
    try:
        prices.update(load_admin_settings().get("deco_prices") or {})
    except Exception: pass
    return prices


def save_caisse_deco_prices(values: dict) -> dict:
    """Enregistre les tarifs Caisse déco."""
    prices = load_caisse_deco_prices()
    for key in prices:
        if key in (values or {}):
            try:
                prices[key] = float(str(values[key]).replace(",", "."))
            except Exception:
                pass
    settings = load_admin_settings()
    settings["deco_prices"] = prices
    save_admin_settings(settings)
    return prices


def debug_log(message: str) -> None:
    """Affiche un diagnostic visible dans les logs Render."""
    print(f"[ESI-DEVIS] {message}", flush=True)

CLASSIQUES = {
    "Tableaux": ["T1", "T1-T6", "MRT", "T1-T3 MRT", "T à Glissières", "T Séparations mousse"],
    "Objets": ["Objet 1", "Objet 2 à 6", "Tapisserie", "Caisse Cuvette", "Claire-Voie", "Palette"],
    "Caissons / Wrapp": ["Wrapp", "Caisson Tableaux", "1 Caisson Objet", "6 Caissons Objets"],
    "Caisse déco": ["Caisse déco"],
}
MIGRES = set(engine2026.OUTPUTS) | {"Caisse déco"}


def load_onglets():
    path = DATA_DIR / "onglets_excel.json"
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def as_float(value, default=None):
    if value in (None, ""):
        return default
    try:
        return float(str(value).replace(",", "."))
    except Exception:
        return default


def fmt(value):
    if isinstance(value, (int, float)):
        return round(value, 2)
    return value


def calculate_sheet(sheet: str, data: dict):
    if sheet == "Caisse déco":
        inputs = CaisseDecoInputs(
            longueur_cm=as_float(data.get("longueur")),
            largeur_cm=as_float(data.get("largeur")),
            hauteur_cm=as_float(data.get("hauteur")),
            type_caisse=data.get("type_caisse") or "PLEINE CP TYPE 16",
            numero_dossier=data.get("numero_dossier") or None,
            numero_colis=data.get("numero_colis") or None,
            client=data.get("client") or None,
            charge_projet=data.get("charge_projet") or None,
            observations=data.get("observations") or None,
        )
        return calculer_caisse_deco(
            inputs,
            prices=data.get("prices") or load_caisse_deco_prices(),
        )
    if sheet in engine2026.NEW:
        return engine2026.generic(sheet, data, effective_admin_cells(strict=True))
    if sheet == "T1":
        inputs = T1Inputs(
            longueur_cm=as_float(data.get("longueur_cm")),
            epaisseur_cm=as_float(data.get("epaisseur_cm")),
            hauteur_cm=as_float(data.get("hauteur_cm")),
            type_isolant=data.get("type_isolant") or None,
            fermeture=data.get("fermeture") or None,
            peinture=data.get("peinture") or None,
            tyvek=data.get("tyvek") or None,
            type_calage=data.get("type_calage") or None,
            type_contreplaque=data.get("type_contreplaque") or None,
            garnissage=data.get("garnissage") or None,
            option_calage=data.get("option_calage") or None,
            epaisseur_mousse=data.get("epaisseur_mousse") or None,
        )
        return engine2026.legacy(sheet, inputs, data, effective_admin_cells(strict=True))
    if sheet == "T1-T6":
        oeuvres = []
        for item in data.get("oeuvres", []):
            oeuvres.append(OeuvreT1T6(
                longueur_cm=as_float(item.get("longueur_cm")),
                epaisseur_cm=as_float(item.get("epaisseur_cm")),
                hauteur_cm=as_float(item.get("hauteur_cm")),
                type_calage=item.get("type_calage") or None,
            ))
        inputs = T1T6Inputs(
            type_isolant=data.get("type_isolant") or None,
            fermeture=data.get("fermeture") or None,
            peinture=data.get("peinture") or None,
            tyvek=data.get("tyvek") or None,
            type_contreplaque=data.get("type_contreplaque") or None,
            garnissage=data.get("garnissage") or None,
            option_calage=data.get("option_calage") or None,
            oeuvres=oeuvres,
        )
        return engine2026.legacy(sheet, inputs, data, effective_admin_cells(strict=True))

    if sheet == "T1-T3 MRT":
        mrts = []
        for item in data.get("mrts", []):
            mrts.append(MRTItem(
                mode_dimensions=item.get("mode_dimensions") or None,
                longueur_cm=as_float(item.get("longueur_cm")),
                epaisseur_cm=as_float(item.get("epaisseur_cm")),
                hauteur_cm=as_float(item.get("hauteur_cm")),
                face_mrt=item.get("face_mrt") or None,
                arriere_mrt=item.get("arriere_mrt") or None,
            ))
        inputs = T1T3MRTInputs(
            type_contreplaque=data.get("type_contreplaque") or None,
            barres=data.get("barres") or None,
            garnissage=data.get("garnissage") or None,
            type_isolant=data.get("type_isolant") or None,
            fermeture=data.get("fermeture") or None,
            peinture=data.get("peinture") or None,
            poignees=data.get("poignees") or None,
            skis=data.get("skis") or None,
            option_calage=data.get("option_calage") or None,
            type_calage=data.get("type_calage") or None,
            responsable_dossier=data.get("responsable_dossier") or None,
            client=data.get("client") or None,
            delai=data.get("delai") or None,
            mrts=mrts,
        )
        return engine2026.legacy(sheet, inputs, data, effective_admin_cells(strict=True))

    if sheet == "T à Glissières":
        tableaux = []
        for item in data.get("tableaux", []):
            tableaux.append(TableauGlissiere(
                inventaire=item.get("inventaire") or None,
                longueur_cm=as_float(item.get("longueur_cm")),
                largeur_cm=as_float(item.get("largeur_cm")),
                hauteur_cm=as_float(item.get("hauteur_cm")),
                quantite=as_float(item.get("quantite"), 1),
                intervalle=as_float(item.get("intervalle"), 2.2),
            ))
        inputs = TGlissieresInputs(
            type_contreplaque=data.get("type_contreplaque") or None,
            barres=data.get("barres") or None,
            garnissage=data.get("garnissage") or None,
            type_isolant=data.get("type_isolant") or None,
            fermeture=data.get("fermeture") or None,
            peinture=data.get("peinture") or None,
            poignees=data.get("poignees") or None,
            skis=data.get("skis") or None,
            epaisseur_mousse=data.get("epaisseur_mousse") or None,
            cuvette_au_dessus=data.get("cuvette_au_dessus") or None,
            # La feuille Excel annule tous les totaux lorsque F10 est vide.
            # On reprend donc le champ "Demandeur" de l'interface et, s'il est vide,
            # on utilise un marqueur interne afin que le prix soit tout de même calculé.
            responsable_dossier=(
                data.get("responsable_dossier")
                or data.get("demandeur")
                or "Calcul"
            ),
            client=data.get("client") or None,
            delai=data.get("delai") or data.get("dossier") or None,
            tableaux=tableaux,
        )
        return engine2026.legacy(sheet, inputs, data, effective_admin_cells(strict=True))

    if sheet == "T Séparations mousse":
        rang1 = []
        for item in data.get("rang1", []):
            rang1.append(OeuvreSeparationMousse(
                longueur_cm=as_float(item.get("longueur_cm")),
                largeur_cm=as_float(item.get("largeur_cm")),
                hauteur_cm=as_float(item.get("hauteur_cm")),
            ))
        rang2 = []
        for item in data.get("rang2", []):
            rang2.append(OeuvreSeparationMousse(
                longueur_cm=as_float(item.get("longueur_cm")),
                largeur_cm=as_float(item.get("largeur_cm")),
                hauteur_cm=as_float(item.get("hauteur_cm")),
            ))
        inputs = TSeparationsMousseInputs(
            type_contreplaque=data.get("type_contreplaque") or None,
            barres=data.get("barres") or None,
            garnissage=data.get("garnissage") or None,
            type_isolant=data.get("type_isolant") or None,
            fermeture=data.get("fermeture") or None,
            peinture=data.get("peinture") or None,
            skis=data.get("skis") or None,
            poignees=data.get("poignees") or None,
            separations=data.get("separations") or None,
            responsable_dossier=data.get("responsable_dossier") or None,
            client=data.get("client") or None,
            delai=data.get("delai") or None,
            rang1=rang1,
            rang2=rang2,
        )
        return engine2026.legacy(sheet, inputs, data, effective_admin_cells(strict=True))

    if sheet == "Objet 1":
        inputs = Objet1Inputs(
            longueur_cm=as_float(data.get("longueur_cm")),
            largeur_cm=as_float(data.get("largeur_cm") or data.get("epaisseur_cm")),
            hauteur_cm=as_float(data.get("hauteur_cm")),
            type_contreplaque=data.get("type_contreplaque") or None,
            barres=data.get("barres") or None,
            garnissage=data.get("garnissage") or None,
            type_isolant=data.get("type_isolant") or None,
            fermeture=data.get("fermeture") or None,
            peinture=data.get("peinture") or None,
            poignees=data.get("poignees") or None,
            skis=data.get("skis") or None,
            cuvette_au_dessus=data.get("cuvette_au_dessus") or None,
            option=data.get("option") or None,
            nombre_cote_ouvrant=data.get("nombre_cote_ouvrant") or None,
            type_calage=data.get("type_calage") or None,
            nombre_calages=as_float(data.get("nombre_calages")),
            plateau_interieur=data.get("plateau_interieur") or None,
            base=data.get("base") or None,
            responsable_dossier=data.get("responsable_dossier") or None,
            client=data.get("client") or None,
        )
        return engine2026.legacy(sheet, inputs, data, effective_admin_cells(strict=True))
    if sheet == "Objet 2 à 6":
        objets = []
        for item in data.get("objets", []):
            objets.append(Objet2A6Item(
                longueur_cm=as_float(item.get("longueur_cm")),
                largeur_cm=as_float(item.get("largeur_cm") or item.get("epaisseur_cm")),
                hauteur_cm=as_float(item.get("hauteur_cm")),
                type_calage=item.get("type_calage") or None,
                plateau_interieur=item.get("plateau_interieur") or None,
                base=item.get("base") or None,
                nombre_calages=as_float(item.get("nombre_calages")),
                type_mousse_calage=item.get("type_mousse_calage") or None,
            ))
        inputs = Objet2A6Inputs(
            type_contreplaque=data.get("type_contreplaque") or None,
            barres=data.get("barres") or None,
            garnissage=data.get("garnissage") or None,
            type_isolant=data.get("type_isolant") or None,
            fermeture=data.get("fermeture") or None,
            peinture=data.get("peinture") or None,
            poignees=data.get("poignees") or None,
            skis=data.get("skis") or None,
            cuvette_au_dessus=data.get("cuvette_au_dessus") or None,
            responsable_dossier=data.get("responsable_dossier") or data.get("demandeur") or None,
            client=data.get("client") or None,
            delai=data.get("delai") or data.get("dossier") or None,
            objets=objets,
        )
        return engine2026.legacy(sheet, inputs, data, effective_admin_cells(strict=True))

    if sheet == "MRT":
        inputs = MRTInputs(
            longueur_cm=as_float(data.get("longueur_cm")),
            epaisseur_cm=as_float(data.get("epaisseur_cm")),
            hauteur_cm=as_float(data.get("hauteur_cm")),
            mode_dimensions=data.get("mode_dimensions") or None,
            type_contreplaque=data.get("type_contreplaque") or None,
            barres=data.get("barres") or None,
            fermeture=data.get("fermeture") or None,
            poignees=data.get("poignees") or None,
            face_mrt=data.get("face_mrt") or None,
            arriere_mrt=data.get("arriere_mrt") or None,
            responsable_dossier=data.get("responsable_dossier") or None,
            client=data.get("client") or None,
            categorie=data.get("categorie") or None,
        )
        return engine2026.legacy(sheet, inputs, data, effective_admin_cells(strict=True))
    raise ValueError("Ce type de caisse n'est pas encore migré.")


def ceil_text(value):
    if value in (None, ""):
        return "-"
    try:
        return f"{math.ceil(float(value)):,.0f}".replace(",", " ")
    except Exception:
        return str(value or "-")


def euro_text(value):
    if value in (None, ""): return "-"
    return f"{float(value):,.2f}".replace(",", " ").replace(".", ",") + " €"


def num_text(value, suffix=""):
    txt = ceil_text(value)
    return "-" if txt == "-" else f"{txt}{suffix}"


def load_parametres_matiere():
    """Paramètres modifiables issus de la feuille Prix Matiere."""
    path = DATA_DIR / "parametres_matiere.json"
    default = {"marge_cession_percent": 30}
    if not path.exists():
        return default
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {**default, **data}
    except Exception:
        return default


def taux_marge_cession():
    params = load_parametres_matiere()
    try:
        params.update({"marge_cession_percent":load_admin_settings().get("marge_cession_percent",params.get("marge_cession_percent",30))})
    except Exception: pass
    value = params.get("marge_cession_percent", 30)
    try:
        return float(str(value).replace(",", ".")) / 100.0
    except Exception:
        return 0.30


def enrich_result_with_cession(result: dict) -> dict:
    """Ajoute le prix de cession = prix d'achat + marge paramétrable."""
    enriched = dict(result or {})
    prix_achat = as_float(enriched.get("prix_vente"), None)
    marge = taux_marge_cession()
    if prix_achat is not None:
        enriched["prix_achat"] = prix_achat
        enriched["marge_cession_percent"] = round(marge * 100, 4)
        enriched["prix_cession"] = prix_achat * (1 + marge)
    return enriched



def slugify(value: str) -> str:
    value = str(value or "").strip().lower()
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    out = []
    for ch in value:
        if ch.isalnum():
            out.append(ch)
        elif ch in (" ", "-", "_", "/", "\\", "."):
            out.append("_")
    slug = "".join(out).strip("_")
    while "__" in slug:
        slug = slug.replace("__", "_")
    return slug or "notice"


def notice_key(sheet: str, data: dict) -> str:
    """Clé stable : type de caisse + isolant + calage.
    Pour les caisses sans champ type_calage, on utilise le champ le plus proche.
    """
    isolant = data.get("type_isolant") or "Aucun"
    calage = (
        data.get("type_calage")
        or data.get("separations")
        or data.get("option_calage")
        or data.get("base")
        or "Aucun"
    )
    return "|".join([slugify(sheet), slugify(isolant), slugify(calage)])



def notices_index_path() -> Path:
    return DATA_DIR / "notices_index.json"


ESI_DEVIS_SUPABASE_BUCKET = os.getenv("ESI_DEVIS_SUPABASE_NOTICES_BUCKET", "notices-techniques")
ESI_DEVIS_SUPABASE_TABLE = os.getenv("ESI_DEVIS_SUPABASE_NOTICES_TABLE", "notices_techniques")


def supabase_configured() -> bool:
    return bool(os.getenv("ESI_DEVIS_SUPABASE_URL") and (os.getenv("ESI_DEVIS_SUPABASE_SERVICE_ROLE_KEY") or os.getenv("ESI_DEVIS_SUPABASE_ANON_KEY")))


def supabase_url() -> str:
    return (os.getenv("ESI_DEVIS_SUPABASE_URL") or "").rstrip("/")


def supabase_key() -> str:
    return os.getenv("ESI_DEVIS_SUPABASE_SERVICE_ROLE_KEY") or os.getenv("ESI_DEVIS_SUPABASE_ANON_KEY") or ""


def supabase_headers(extra: dict | None = None) -> dict:
    key = supabase_key()
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
    }
    if extra:
        headers.update(extra)
    return headers


def supabase_public_url(path: str) -> str:
    safe_path = "/".join(quote(part) for part in str(path).strip("/").split("/"))
    return f"{supabase_url()}/storage/v1/object/public/{ESI_DEVIS_SUPABASE_BUCKET}/{safe_path}"


def supabase_object_url(path: str) -> str:
    safe_path = "/".join(quote(part) for part in str(path).strip("/").split("/"))
    return f"{supabase_url()}/storage/v1/object/{ESI_DEVIS_SUPABASE_BUCKET}/{safe_path}"


def http_json(url: str, method: str = "GET", payload=None, headers: dict | None = None):
    data = None
    all_headers = headers or {}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        all_headers = {**all_headers, "Content-Type": "application/json"}
    req = Request(url, data=data, headers=all_headers, method=method)
    try:
        with urlopen(req, timeout=30) as resp:
            raw = resp.read()
            if not raw:
                return None
            return json.loads(raw.decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Supabase HTTP {exc.code}: {detail}") from exc


def download_url_to_file(url: str, suffix: str = "") -> Path | None:
    if not url:
        return None
    try:
        req = Request(url, headers=supabase_headers() if "/storage/v1/object/" in url and "/public/" not in url else {})
        with urlopen(req, timeout=30) as resp:
            data = resp.read()
        tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
        tmp.write(data)
        tmp.close()
        return Path(tmp.name)
    except Exception:
        return None


def upload_bytes_to_supabase(path: str, content: bytes, content_type: str) -> None:
    if not supabase_configured():
        debug_log("Upload Supabase annulé : configuration manquante")
        raise RuntimeError("Supabase n'est pas configuré.")
    debug_log(f"Upload Supabase -> {path} ({content_type}, {len(content)} octets)")
    req = Request(
        supabase_object_url(path),
        data=content,
        headers=supabase_headers({"Content-Type": content_type, "x-upsert": "true"}),
        method="POST",
    )
    try:
        with urlopen(req, timeout=60) as resp:
            resp.read()
        debug_log(f"Upload Supabase OK -> {path}")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        debug_log(f"Upload Supabase ERREUR {exc.code} -> {detail}")
        raise RuntimeError(f"Upload Supabase impossible ({exc.code}) : {detail}") from exc
    except Exception as exc:
        debug_log(f"Upload Supabase ERREUR inattendue -> {type(exc).__name__}: {exc}")
        raise


def normalize_notice_record(record: dict) -> dict:
    if not record:
        return {}
    pdf_path = record.get("pdf_path") or record.get("pdf") or ""
    preview_path = record.get("preview_path") or record.get("preview") or ""
    return {
        "id": record.get("id"),
        "key": record.get("notice_key") or record.get("key") or "",
        "sheet": record.get("sheet") or "",
        "type_isolant": record.get("type_isolant") or record.get("isolant") or "Aucun",
        "type_calage": record.get("type_calage") or record.get("calage") or record.get("separations") or "Aucun",
        "title": record.get("title") or record.get("titre") or "Notice technique",
        "pdf": pdf_path,
        "preview": preview_path,
        "pdf_url": record.get("pdf_url") or (supabase_public_url(pdf_path) if pdf_path and not str(pdf_path).startswith("assets/") else ""),
        "preview_url": record.get("preview_url") or (supabase_public_url(preview_path) if preview_path and not str(preview_path).startswith("assets/") else ""),
        "auto": bool(record.get("auto", True)),
        "updated_at": record.get("updated_at"),
    }


def list_supabase_notices() -> list[dict]:
    if not supabase_configured():
        return []
    url = f"{supabase_url()}/rest/v1/{ESI_DEVIS_SUPABASE_TABLE}?select=*&order=id.asc"
    data = http_json(url, headers=supabase_headers({"Accept": "application/json"}))
    return [normalize_notice_record(r) for r in (data or [])]


def get_supabase_notice_by_key(key: str):
    if not supabase_configured():
        return None
    url = f"{supabase_url()}/rest/v1/{ESI_DEVIS_SUPABASE_TABLE}?select=*&notice_key=eq.{quote(key, safe='')}&limit=1"
    data = http_json(url, headers=supabase_headers({"Accept": "application/json"}))
    if data:
        return normalize_notice_record(data[0])
    return None


def get_supabase_notice_by_values(sheet: str, isolant: str, calage: str):
    if not supabase_configured():
        return None
    url = (
        f"{supabase_url()}/rest/v1/{ESI_DEVIS_SUPABASE_TABLE}?select=*"
        f"&sheet_slug=eq.{quote(slugify(sheet), safe='')}"
        f"&isolant_slug=eq.{quote(slugify(isolant), safe='')}"
        f"&calage_slug=eq.{quote(slugify(calage), safe='')}"
        f"&limit=1"
    )
    data = http_json(url, headers=supabase_headers({"Accept": "application/json"}))
    if data:
        return normalize_notice_record(data[0])
    return None


def upsert_supabase_notice(record: dict) -> dict:
    debug_log(f"Upsert notice Supabase -> key={record.get('key')} table={ESI_DEVIS_SUPABASE_TABLE}")
    url = f"{supabase_url()}/rest/v1/{ESI_DEVIS_SUPABASE_TABLE}?on_conflict=notice_key"
    payload = {
        "notice_key": record["key"],
        "sheet": record["sheet"],
        "sheet_slug": slugify(record["sheet"]),
        "type_isolant": record["type_isolant"],
        "isolant_slug": slugify(record["type_isolant"]),
        "type_calage": record["type_calage"],
        "calage_slug": slugify(record["type_calage"]),
        "title": record["title"],
        "pdf_path": record["pdf"],
        "preview_path": record.get("preview") or "",
        "pdf_url": record.get("pdf_url") or supabase_public_url(record["pdf"]),
        "preview_url": record.get("preview_url") or (supabase_public_url(record["preview"]) if record.get("preview") else ""),
        "auto": True,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    data = http_json(
        url,
        method="POST",
        payload=payload,
        headers=supabase_headers({"Prefer": "resolution=merge-duplicates,return=representation", "Accept": "application/json"}),
    )
    if data:
        return normalize_notice_record(data[0])
    return normalize_notice_record(payload)


def delete_notice_record(identifier: dict) -> bool:
    """Supprime uniquement l'association de notice.
    Les fichiers PDF/PNG restent dans Supabase Storage pour éviter toute suppression accidentelle.
    """
    key = str(identifier.get("key") or identifier.get("notice_key") or "").strip()
    notice_id = identifier.get("id")

    if supabase_configured():
        if key:
            url = f"{supabase_url()}/rest/v1/{ESI_DEVIS_SUPABASE_TABLE}?notice_key=eq.{quote(key, safe='')}"
        elif notice_id not in (None, ""):
            url = f"{supabase_url()}/rest/v1/{ESI_DEVIS_SUPABASE_TABLE}?id=eq.{quote(str(notice_id), safe='')}"
        else:
            raise ValueError("Identifiant de notice manquant.")
        debug_log(f"Suppression association notice Supabase -> key={key or '-'} id={notice_id or '-'}")
        http_json(url, method="DELETE", headers=supabase_headers({"Prefer": "return=minimal", "Accept": "application/json"}))
        return True

    index = load_notices_index()
    before = len(index.get("notices", []))
    notices = []
    for record in index.get("notices", []):
        same_key = key and (record.get("key") == key or record.get("notice_key") == key)
        same_id = notice_id not in (None, "") and str(record.get("id")) == str(notice_id)
        if not (same_key or same_id):
            notices.append(record)
    index["notices"] = notices
    save_notices_index(index)
    return len(notices) < before


def load_notices_index() -> dict:
    if supabase_configured():
        try:
            return {"notices": list_supabase_notices()}
        except Exception:
            pass
    path = notices_index_path()
    default = {"notices": []}
    if not path.exists():
        return default
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("notices"), list):
            return data
    except Exception:
        pass
    return default


def save_notices_index(index: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    notices_index_path().write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")



def render_pdf_first_page_to_png(pdf_path: Path, png_path: Path) -> bool:
    """Génère un aperçu PNG de la première page du PDF.
    Compatible Render/Python 3.14 grâce à pypdfium2.
    """
    try:
        import pypdfium2 as pdfium
        png_path.parent.mkdir(parents=True, exist_ok=True)
        pdf = pdfium.PdfDocument(str(pdf_path))
        if len(pdf) == 0:
            return False
        page = pdf[0]
        bitmap = page.render(scale=1.8)
        image = bitmap.to_pil()
        if image.mode not in ("RGB", "RGBA"):
            image = image.convert("RGB")
        image.save(str(png_path), "PNG")
        try:
            page.close()
        except Exception:
            pass
        try:
            pdf.close()
        except Exception:
            pass
        return png_path.exists()
    except Exception:
        return False


def notice_record_to_doc(record: dict):
    if not record:
        return None
    record = normalize_notice_record(record)
    rel_pdf = record.get("pdf") or ""
    rel_preview = record.get("preview") or ""
    pdf = None
    preview = None
    if rel_pdf:
        local_pdf = ROOT / rel_pdf
        if local_pdf.exists():
            pdf = local_pdf
        elif record.get("pdf_url"):
            pdf = download_url_to_file(record["pdf_url"], ".pdf")
    if rel_preview:
        local_preview = ROOT / rel_preview
        if local_preview.exists():
            preview = local_preview
        elif record.get("preview_url"):
            preview = download_url_to_file(record["preview_url"], ".png")
    elif pdf:
        preview = pdf.with_suffix(".png")
    return {"title": record.get("title") or "Notice technique", "pdf": pdf, "preview": preview}


def notice_record_to_api(record: dict):
    if not record:
        return None
    record = normalize_notice_record(record)
    rel_pdf = record.get("pdf") or ""
    rel_preview = record.get("preview") or ""
    pdf_url = record.get("pdf_url") or ("/" + rel_pdf.lstrip("/") if rel_pdf else "")
    preview_url = record.get("preview_url") or ("/" + rel_preview.lstrip("/") if rel_preview else "")
    return {
        "title": record.get("title") or "Notice technique",
        "pdf": pdf_url,
        "preview": preview_url,
        "key": record.get("key") or "",
        "auto": bool(record.get("auto")),
    }


def notice_match_values(sheet: str, data: dict) -> tuple[str, str, str]:
    """Valeurs normalisées utilisées pour retrouver une notice.
    On garde la clé stable, mais on accepte aussi les anciens enregistrements
    dont la clé aurait été construite différemment.
    """
    isolant = data.get("type_isolant") or "Aucun"
    calage = (
        data.get("type_calage")
        or data.get("separations")
        or data.get("option_calage")
        or data.get("base")
        or "Aucun"
    )
    return slugify(sheet), slugify(isolant), slugify(calage)


def find_notice_record(sheet: str, data: dict):
    key = notice_key(sheet, data)
    sheet_s, isolant_s, calage_s = notice_match_values(sheet, data)

    if supabase_configured():
        try:
            record = get_supabase_notice_by_key(key)
            if record:
                return record
            record = get_supabase_notice_by_values(sheet_s, isolant_s, calage_s)
            if record:
                return record
        except Exception:
            pass

    notices = load_notices_index().get("notices", [])

    # 1) Recherche par clé exacte
    for record in notices:
        if record.get("key") == key or record.get("notice_key") == key:
            return record

    # 2) Recherche tolérante par champs enregistrés
    for record in notices:
        r_sheet = slugify(record.get("sheet") or "")
        r_isolant = slugify(record.get("type_isolant") or record.get("isolant") or "Aucun")
        r_calage = slugify(record.get("type_calage") or record.get("calage") or record.get("separations") or "Aucun")
        if (r_sheet, r_isolant, r_calage) == (sheet_s, isolant_s, calage_s):
            return record

    return None


def next_notice_id(index: dict) -> int:
    """Retourne le prochain identifiant numérique pour les notices auto."""
    max_id = 0
    for record in index.get("notices", []):
        try:
            max_id = max(max_id, int(record.get("id") or 0))
        except Exception:
            pass
    return max_id + 1


def notice_category_dir(sheet: str) -> str:
    """Dossier lisible pour ranger les notices ajoutées depuis l'application."""
    mapping = {
        "T1": "T1",
        "T1-T6": "T1-T6",
        "MRT": "MRT",
        "T1-T3 MRT": "T1-T3-MRT",
        "T à Glissières": "T-Glissieres",
        "T Séparations mousse": "T-Separations-Mousse",
        "Objet 1": "Objet1",
        "Objet 2 à 6": "Objet2a6",
    }
    return mapping.get(sheet, slugify(sheet))


def register_notice(sheet: str, data: dict, title: str, source_pdf: Path) -> dict:
    key = notice_key(sheet, data)
    index = load_notices_index()
    isolant = data.get("type_isolant") or "Aucun"
    calage = data.get("type_calage") or data.get("separations") or data.get("option_calage") or data.get("base") or "Aucun"

    # Si une notice existait déjà pour cette combinaison, on la remplace proprement
    old_record = None
    for record in index.get("notices", []):
        if record.get("key") == key or record.get("notice_key") == key:
            old_record = record
            break

    notice_id = int(old_record.get("id")) if old_record and old_record.get("id") else next_notice_id(index)
    filename = f"{notice_id:04d}"
    folder_name = notice_category_dir(sheet)

    with open(source_pdf, "rb") as f:
        pdf_bytes = f.read()

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        preview_path = Path(tmp.name)
    preview_ok = render_pdf_first_page_to_png(source_pdf, preview_path)
    preview_bytes = preview_path.read_bytes() if preview_ok else b""
    try:
        preview_path.unlink()
    except Exception:
        pass

    if supabase_configured():
        debug_log(f"Association notice via Supabase : sheet={sheet}, key={key}, bucket={ESI_DEVIS_SUPABASE_BUCKET}")
        pdf_path = f"{folder_name}/{filename}.pdf"
        preview_store_path = f"{folder_name}/{filename}.png" if preview_bytes else ""
        upload_bytes_to_supabase(pdf_path, pdf_bytes, "application/pdf")
        if preview_bytes:
            upload_bytes_to_supabase(preview_store_path, preview_bytes, "image/png")
        record = {
            "id": notice_id,
            "key": key,
            "sheet": sheet,
            "type_isolant": isolant,
            "type_calage": calage,
            "title": title or f"Notice technique - {sheet}",
            "pdf": pdf_path,
            "preview": preview_store_path,
            "pdf_url": supabase_public_url(pdf_path),
            "preview_url": supabase_public_url(preview_store_path) if preview_store_path else "",
            "auto": True,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }
        return upsert_supabase_notice(record)

    debug_log("Association notice via stockage local Render (Supabase non configuré)")
    folder = ROOT / "assets" / "notices" / "Auto" / folder_name
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / f"{filename}.pdf"
    shutil.copyfile(source_pdf, dest)
    local_preview = dest.with_suffix(".png")
    if preview_bytes:
        local_preview.write_bytes(preview_bytes)

    record = {
        "id": notice_id,
        "key": key,
        "sheet": sheet,
        "type_isolant": isolant,
        "type_calage": calage,
        "title": title or f"Notice technique - {sheet}",
        "pdf": str(dest.relative_to(ROOT)).replace("\\", "/"),
        "preview": str(local_preview.relative_to(ROOT)).replace("\\", "/") if preview_bytes else "",
        "auto": True,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }

    notices = [r for r in index.get("notices", []) if r.get("key") != key]
    notices.append(record)
    notices.sort(key=lambda r: int(r.get("id") or 0))
    index["notices"] = notices
    save_notices_index(index)
    return record

def legacy_notice_for(sheet: str, data: dict):
    """Anciennes notices codées en dur, conservées comme secours."""
    calage = str(data.get("type_calage") or "").lower()
    isolant = str(data.get("type_isolant") or "").lower()

    def doc(title, rel_pdf):
        pdf = ROOT / rel_pdf
        return {"title": title, "pdf": pdf, "preview": pdf.with_suffix(".png")}

    if sheet == "T1" and "bandes" in calage:
        if "isotherme" in isolant or "super" in isolant or "double" in isolant:
            return doc("Caisse tableau - bandes de mousse double isotherme", "assets/notices/Tableaux/T1/bandes_mousse_double_isotherme.pdf")
        return doc("Caisse tableau - bandes de mousse", "assets/notices/T1/caisse_tableau_bandes_mousse.pdf")

    if sheet == "MRT" or sheet == "T1-T3 MRT":
        return doc("Caisse tableau MRT", "assets/notices/Tableaux/MRT/mrt.pdf")

    if sheet == "T à Glissières":
        return doc("Caisse glissières", "assets/notices/Tableaux/Glissieres/glissieres.pdf")

    if sheet == "Objet 1":
        if "banc" in calage:
            return doc("Caisse objet - bancs ouverts", "assets/notices/Objets/Objet1/caisse_objet_bancs_ouverts.pdf")
        if "guillotine" in calage:
            return doc("Caisse objet - guillotines", "assets/notices/Objets/Objet1/caisse_objet_guillotines.pdf")
        if "entourage" in calage or "mousse" in calage or "ecrin" in calage:
            if "double" in isolant:
                return doc("Caisse objet - entourage mousse double isotherme", "assets/notices/Objets/Objet1/caisse_objet_entourage_mousse_double_isotherme.pdf")
            if "super" in isolant or "30" in isolant:
                return doc("Objet superiso 30 - entourage mousse", "assets/notices/Objets/Objet1/objet_superiso30_entourage_mousse.pdf")
            if "isotherme" in isolant:
                return doc("Caisse objet - entourage mousse isotherme", "assets/notices/Objets/Objet1/caisse_objet_entourage_mousse_isotherme.pdf")
            return doc("Caisse objet - entourage mousse", "assets/notices/Objets/Objet1/caisse_objet_entourage_mousse.pdf")

    return None


def notice_for(sheet: str, data: dict):
    """Retourne d'abord une notice associée depuis le registre, puis les anciennes notices de secours."""
    registered = find_notice_record(sheet, data)
    if registered:
        return notice_record_to_doc(registered)
    return legacy_notice_for(sheet, data)


def notice_for_api(sheet: str, data: dict):
    registered = find_notice_record(sheet, data)
    if registered:
        return notice_record_to_api(registered)
    legacy = legacy_notice_for(sheet, data)
    if legacy:
        try:
            pdf_rel = str(legacy["pdf"].relative_to(ROOT)).replace("\\", "/")
            preview_rel = str(legacy["preview"].relative_to(ROOT)).replace("\\", "/")
        except Exception:
            return None
        return {"title": legacy["title"], "pdf": "/" + pdf_rel, "preview": "/" + preview_rel, "key": notice_key(sheet, data), "auto": False}
    return None


def parse_multipart_form(headers, body: bytes) -> tuple[dict, dict]:
    """Parse un formulaire multipart/form-data sans module cgi (compatible Python 3.13/3.14).
    Retourne (fields, files) avec files[name] = {filename, content, content_type}.
    """
    from email.parser import BytesParser
    from email.policy import default

    content_type = headers.get("Content-Type", "")
    if "multipart/form-data" not in content_type.lower():
        raise ValueError("Requête multipart/form-data attendue.")

    raw = (
        f"Content-Type: {content_type}\r\n"
        "MIME-Version: 1.0\r\n\r\n"
    ).encode("utf-8") + body

    message = BytesParser(policy=default).parsebytes(raw)
    fields: dict[str, str] = {}
    files: dict[str, dict] = {}

    for part in message.iter_parts():
        disposition = part.get("Content-Disposition", "")
        if "form-data" not in disposition:
            continue
        name = part.get_param("name", header="Content-Disposition")
        filename = part.get_filename()
        payload = part.get_payload(decode=True) or b""
        if not name:
            continue
        if filename:
            files[name] = {
                "filename": filename,
                "content": payload,
                "content_type": part.get_content_type(),
            }
        else:
            charset = part.get_content_charset() or "utf-8"
            fields[name] = payload.decode(charset, errors="replace")
    return fields, files

def generate_internal_fiche_pdf(sheet: str, data: dict, result: dict) -> bytes:
    """Génère une fiche chiffrage interne ESI sur une seule page PDF."""
    result = enrich_result_with_cession(result)
    notice = notice_for(sheet, data)
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        pdf_path = Path(tmp.name)

    c = canvas.Canvas(str(pdf_path), pagesize=A4)
    w, h = A4
    margin = 14 * mm
    blue = colors.HexColor("#0284c7")
    sky = colors.HexColor("#0ea5e9")
    text = colors.HexColor("#0f172a")
    muted = colors.HexColor("#64748b")
    line = colors.HexColor("#dbeafe")
    light = colors.HexColor("#f8fafc")

    # Header
    c.setFillColor(light)
    c.roundRect(margin, h - 32*mm, w - 2*margin, 20*mm, 6*mm, fill=1, stroke=0)
    c.setFillColor(blue)
    c.setFont("Helvetica-Bold", 11)
    c.drawString(margin + 8*mm, h - 18*mm, "ESI - FICHE CHIFFRAGE INTERNE")
    c.setFillColor(text)
    c.setFont("Helvetica-Bold", 20)
    c.drawString(margin + 8*mm, h - 27*mm, str(sheet))
    c.setFillColor(muted)
    c.setFont("Helvetica", 8)
    c.drawRightString(w - margin - 8*mm, h - 18*mm, datetime.now().strftime("%d/%m/%Y"))

    # Meta / input dimensions
    y = h - 42*mm
    c.setFillColor(text)
    c.setFont("Helvetica-Bold", 10)
    c.drawString(margin, y, "Informations")
    y -= 8*mm
    fields = [
        ("Demandeur", data.get("demandeur") or "-"),
        ("Dossier", data.get("dossier") or "-"),
        ("Dimensions oeuvre", f"{data.get('longueur_cm') or '-'} x {data.get('largeur_cm') or data.get('epaisseur_cm') or '-'} x {data.get('hauteur_cm') or '-'} cm"),
    ]
    for lab, val in fields:
        c.setFillColor(muted); c.setFont("Helvetica-Bold", 7); c.drawString(margin, y, lab.upper())
        c.setFillColor(text); c.setFont("Helvetica-Bold", 10); c.drawString(margin + 45*mm, y, str(val))
        y -= 7*mm

    # Result cards
    dim = "-"
    dl = result.get("dimensions_exterieures_longueur")
    de = result.get("dimensions_exterieures_epaisseur")
    dh = result.get("dimensions_exterieures_hauteur")
    if dl or de or dh:
        dim = f"{num_text(dl)} x {num_text(de)} x {num_text(dh)} cm"
    cards = [
        ("Dimensions extérieures", dim),
        ("Poids caisse", num_text(result.get("poids_caisse"), " kg")),
        ("Bilan carbone", num_text(result.get("bilan_carbone"), " kg CO2e")),
        ("Prix de cession", euro_text(result.get("prix_cession"))),
    ]
    y -= 4*mm
    card_w = (w - 2*margin - 8*mm) / 2
    card_h = 20*mm
    for i, (lab, val) in enumerate(cards):
        x = margin + (i % 2) * (card_w + 8*mm)
        yy = y - (i // 2) * (card_h + 6*mm)
        c.setStrokeColor(line); c.setFillColor(light)
        c.roundRect(x, yy - card_h, card_w, card_h, 4*mm, fill=1, stroke=1)
        c.setFillColor(muted); c.setFont("Helvetica-Bold", 7); c.drawString(x + 5*mm, yy - 7*mm, lab.upper())
        c.setFillColor(text); c.setFont("Helvetica-Bold", 14); c.drawString(x + 5*mm, yy - 15*mm, str(val))
    y = y - 2*(card_h + 6*mm) - 4*mm

    # Notice preview
    c.setFillColor(blue); c.setFont("Helvetica-Bold", 10)
    c.drawString(margin, y, "Notice technique")
    y -= 5*mm
    box_x, box_y = margin, margin + 8*mm
    box_w, box_h = w - 2*margin, y - box_y
    c.setStrokeColor(line); c.setFillColor(colors.white)
    c.roundRect(box_x, box_y, box_w, box_h, 5*mm, fill=1, stroke=1)
    preview_path = None
    if notice:
        preview_path = notice.get("preview")
        if (not preview_path or not preview_path.exists()) and notice.get("pdf") and notice["pdf"].exists():
            tmp_preview = Path(tempfile.gettempdir()) / f"notice_preview_{abs(hash(str(notice['pdf'])))}.png"
            if render_pdf_first_page_to_png(notice["pdf"], tmp_preview):
                preview_path = tmp_preview
    if notice and preview_path and preview_path.exists():
        img = ImageReader(str(preview_path))
        iw, ih = img.getSize()
        scale = min((box_w - 10*mm)/iw, (box_h - 13*mm)/ih)
        draw_w, draw_h = iw*scale, ih*scale
        c.drawImage(img, box_x + (box_w-draw_w)/2, box_y + 7*mm, width=draw_w, height=draw_h, preserveAspectRatio=True)
        c.setFillColor(text); c.setFont("Helvetica-Bold", 9)
        c.drawString(box_x + 5*mm, box_y + box_h - 7*mm, notice["title"])
    else:
        c.setFillColor(muted); c.setFont("Helvetica", 11)
        c.drawCentredString(box_x + box_w/2, box_y + box_h/2, "Aucune notice technique associée")

    c.setFillColor(muted); c.setFont("Helvetica", 7)
    c.drawRightString(w - margin, 7*mm, "Document interne généré par la matrice de chiffrage ESI")
    c.showPage()
    c.save()
    pdf_bytes = pdf_path.read_bytes()
    try:
        pdf_path.unlink()
    except Exception:
        pass
    return pdf_bytes



# --- ADMIN ESI-DEVIS : parameters only, Excel formulas remain untouched ---
ADMIN_TABLE = "esi_devis_admin_settings"
ADMIN_ROW = "global"
ADMIN_PATH = DATA_DIR / "admin_settings.json"


def admin_authorized(headers) -> bool:
    from flask import g
    return (getattr(g, "esi_user", None) or {}).get("profil") == "ADMIN"

def _admin_supabase_enabled() -> bool:
    # A service-role key is necessary for server-side administration.
    return bool(supabase_url() and os.getenv("ESI_DEVIS_SUPABASE_SERVICE_ROLE_KEY"))


def admin_storage_headers(extra=None):
    key = os.getenv("ESI_DEVIS_SUPABASE_SERVICE_ROLE_KEY", "")
    return {"apikey": key, "Authorization": "Bearer " + key, **(extra or {})}


def load_admin_settings():
    default = {"cells": {}, "marge_cession_percent": 30.0, "deco_prices": {}}
    if _admin_supabase_enabled():
        url = f"{supabase_url()}/rest/v1/{ADMIN_TABLE}?id=eq.{ADMIN_ROW}&select=payload"
        rows = http_json(url, headers=admin_storage_headers())
        return {**default, **((rows or [{}])[0].get("payload") or {})}
    if ADMIN_PATH.exists():
        try:
            return {**default, **json.loads(ADMIN_PATH.read_text(encoding="utf-8"))}
        except Exception:
            pass
    return default


def save_admin_settings(payload):
    if _admin_supabase_enabled():
        url = f"{supabase_url()}/rest/v1/{ADMIN_TABLE}?on_conflict=id"
        http_json(url, method="POST", payload={"id": ADMIN_ROW, "payload": payload},
                  headers=admin_storage_headers({"Prefer": "resolution=merge-duplicates,return=minimal"}))
    else:
        # Local storage is suitable for development, not persistent Render disks.
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        ADMIN_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _number(v):
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (float, int)) and math.isfinite(v):
        return float(v)
    return None


def admin_workbook():
    sheet = engine2026.workbook()["Prix Matiere"]
    values, formulas = {}, {}
    for row in sheet.iter_rows(min_col=1,max_col=2):
        for cell in row:
            key = f"Prix Matiere!{cell.coordinate}"
            if cell.data_type == "f": formulas[key] = cell.value
            elif cell.value is not None: values[key] = cell.value
    return {"values":values,"formulas":formulas}


def _admin_numeric_price_formula(formula):
    """Evaluate only simple numeric expressions used in column B of Prix Matiere.

    This reads a displayed base price; it never evaluates arbitrary Python or
    changes any spreadsheet formula. Cell references are deliberately rejected.
    """
    import ast
    import operator
    operators = {ast.Add:operator.add, ast.Sub:operator.sub,
                 ast.Mult:operator.mul, ast.Div:operator.truediv,
                 ast.USub:operator.neg, ast.UAdd:operator.pos}
    def evaluate(node):
        if isinstance(node,ast.Expression):
            return evaluate(node.body)
        if isinstance(node,ast.Constant) and type(node.value) in (int,float):
            return float(node.value)
        if isinstance(node,ast.BinOp) and type(node.op) in operators:
            return operators[type(node.op)](evaluate(node.left),evaluate(node.right))
        if isinstance(node,ast.UnaryOp) and type(node.op) in operators:
            return operators[type(node.op)](evaluate(node.operand))
        raise ValueError('Formule de prix non prise en charge')
    try:
        expr=str(formula).strip().lstrip('=')
        result=evaluate(ast.parse(expr,mode='eval'))
        return result if math.isfinite(result) else None
    except (ValueError,SyntaxError,ZeroDivisionError,OverflowError,TypeError):
        return None


def admin_editable_cells():
    """Only original column B prices with a material name in column A.

    Columns C onwards are calculation parameters, not editable material prices.
    Even when a price in column B is a numeric Excel formula, its calculated
    value is shown and may be explicitly overridden at runtime.
    """
    import re
    wb=admin_workbook()
    values=wb.get('values') or {}
    formulas=wb.get('formulas') or {}
    entries=[]
    sheets=set()
    for key in set(values) | set(formulas):
        if '!' not in key: continue
        sheet,cell=key.rsplit('!',1)
        norm=unicodedata.normalize('NFKD',sheet).encode('ascii','ignore').decode('ascii').lower()
        if 'prix' in norm and 'matiere' in norm:
            sheets.add(sheet)
    for sheet in sheets:
        for key in set(values) | set(formulas):
            if not key.startswith(sheet+'!B'): continue
            cell=key.rsplit('!',1)[1]
            match=re.fullmatch(r'B([0-9]+)',cell)
            if not match or int(match.group(1))<4: continue
            row=int(match.group(1))
            label=values.get(f'{sheet}!A{row}')
            if not isinstance(label,str) or not label.strip(): continue
            if label.strip().casefold() in ('frais généraux', 'marge'): continue
            raw=values.get(key)
            value=_number(raw)
            if key in formulas:
                value=_admin_numeric_price_formula(formulas[key])
            if value is None: continue
            entries.append({'key':key,'label':label.strip(),'value':value,
                            'sheet':sheet,'cell':cell})
    return sorted(entries,key=lambda row:int(row['cell'][1:]))


def effective_admin_cells(strict=False):
    try:
        saved = load_admin_settings().get("cells") or {}
        editable = {row["key"]:row["value"] for row in admin_editable_cells()}
        return {k:float(v) for k,v in saved.items() if k in editable and _number(v) is not None}
    except Exception as exc:
        debug_log(f"Admin settings unavailable: {exc}")
        if strict: raise ValueError("Tarifs ADMIN indisponibles : calcul interrompu.") from exc
        return {}


def apply_admin_workbook_overrides(calc):
    for key,value in effective_admin_cells().items():
        if key in calc.values or key in calc.formulas:
            # Override ONLY an explicitly edited price in column B.
            # The original Excel snapshot and all formulas in columns C+ stay intact.
            calc.formulas.pop(key, None)
            calc.values[key] = value
    if hasattr(calc,"cache"): calc.cache.clear()


def install_admin_excel_hooks():
    from .matrice_caisses import excel_engine, t1
    for cls in (excel_engine.ExcelSheetCalculator, t1.T1Calculator):
        if getattr(cls,"_esi_admin_hook",False):
            continue
        orig = cls.__init__
        def wrapped(self,*args,_original=orig,**kwargs):
            _original(self,*args,**kwargs)
            apply_admin_workbook_overrides(self)
        cls.__init__ = wrapped
        cls._esi_admin_hook=True

install_admin_excel_hooks()


def admin_config_payload():
    saved=load_admin_settings()
    overrides=saved.get("cells") or {}
    rows=admin_editable_cells()
    for row in rows:
        row["current"] = overrides.get(row["key"], row["value"])
    return {"cells":rows,"marge_cession_percent":saved.get("marge_cession_percent",30),
            "deco_prices":{**load_caisse_deco_prices(),**(saved.get("deco_prices") or {})},
            "storage":"supabase" if _admin_supabase_enabled() else "local"}


def admin_validate_save(data):
    if not isinstance(data,dict): raise ValueError("Données invalides")
    previous=load_admin_settings()
    allowed={r["key"] for r in admin_editable_cells()}
    cells=data.get("cells",{})
    if not isinstance(cells,dict): raise ValueError("Cellules invalides")
    if set(cells)-allowed: raise ValueError("Modification de cellule non autorisée")
    checked={}
    for k,v in cells.items():
        try: f=float(str(v).replace(",","."))
        except Exception: raise ValueError(f"Valeur non numérique : {k}")
        if not math.isfinite(f) or f<0: raise ValueError(f"Valeur incorrecte : {k}")
        checked[k]=f
    margin=data.get("marge_cession_percent",previous.get("marge_cession_percent",30))
    try: margin=float(str(margin).replace(",","."))
    except Exception: raise ValueError("Marge non numérique")
    if not math.isfinite(margin) or not 0<=margin<=1000: raise ValueError("Marge hors limites")
    allowed_deco=set(DEFAULT_PRICES)
    deco=data.get("deco_prices",previous.get("deco_prices",{}))
    if not isinstance(deco,dict) or set(deco)-allowed_deco: raise ValueError("Tarifs déco invalides")
    clean_deco={}
    for k,v in deco.items():
        try: f=float(str(v).replace(",","."))
        except Exception: raise ValueError(f"Tarif déco non numérique : {k}")
        if not math.isfinite(f) or f<0: raise ValueError(f"Tarif déco incorrect : {k}")
        clean_deco[k]=f
    payload={"cells":checked,"marge_cession_percent":margin,"deco_prices":clean_deco}
    save_admin_settings(payload)
    return payload


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        debug_log(f"HTTP {self.address_string()} - {fmt % args}")

    def send_json(self, payload, status=200):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_file(self, path: Path):
        if not path.exists() or not path.is_file():
            self.send_error(404)
            return
        ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_index_with_esi_theme(self):
        """Sert index.html avec un thème bleu ESI appliqué à tous les champs."""
        path = ROOT / "index.html"
        if not path.exists() or not path.is_file():
            self.send_error(404)
            return

        html = path.read_text(encoding="utf-8")
        esi_fields_css = r"""
<style id="esi-fields-theme">
  /* Champs de saisie : bleu clair ESI Tickets */
  input:not([type="checkbox"]):not([type="radio"]):not([type="range"]):not([type="color"]),
  select,
  textarea {
    background-color: #e8f4fd !important;
    border-color: #cfe3f3 !important;
    color: #0f172a !important;
  }

  input:not([type="checkbox"]):not([type="radio"]):not([type="range"]):not([type="color"]):hover,
  select:hover,
  textarea:hover {
    border-color: #8fc8ef !important;
  }

  input:not([type="checkbox"]):not([type="radio"]):not([type="range"]):not([type="color"]):focus,
  select:focus,
  textarea:focus {
    background-color: #e0f2fe !important;
    border-color: #0284c7 !important;
    outline: none !important;
    box-shadow: 0 0 0 3px rgba(2, 132, 199, 0.14) !important;
  }

  input::placeholder,
  textarea::placeholder {
    color: #64748b !important;
    opacity: 1;
  }

  input:disabled,
  select:disabled,
  textarea:disabled,
  input[readonly],
  textarea[readonly] {
    background-color: #dbeafe !important;
  }
</style>
"""

        if "</head>" in html:
            html = html.replace("</head>", esi_fields_css + "\n</head>", 1)
        else:
            html = esi_fields_css + html

        data = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/admin":
            if not admin_authorized(self.headers):
                return self.send_json({"ok":False,"error":"Accès administrateur refusé"},403)
            return self.send_file(ROOT / "admin.html")
        if parsed.path == "/api/admin/config":
            if not admin_authorized(self.headers):
                return self.send_json({"ok":False,"error":"Accès administrateur refusé"},401)
            try: return self.send_json({"ok":True,**admin_config_payload()})
            except Exception as exc: return self.send_json({"ok":False,"error":str(exc)},500)
        if parsed.path in ("/", "/index.html"):
            return self.send_index_with_esi_theme()
        if parsed.path in ("/health", "/api/health"):
            return self.send_json({"ok": True, "service": "matrice-chiffrage-esi"})
        if parsed.path == "/api/config":
            return self.send_json({
                "onglets": load_onglets(),
                "classiques": CLASSIQUES,
                "migres": sorted(MIGRES),
                "parametres_matiere": load_parametres_matiere(),
                "caisse_deco": {"types": options_caisse_deco().get("type_caisse", []), "prices": load_caisse_deco_prices()},
                "notices_count": len(load_notices_index().get("notices", [])),
                "deployment": {
                    "render": True,
                    "supabase_configured": supabase_configured(),
                    "supabase_url_present": bool(os.getenv("ESI_DEVIS_SUPABASE_URL")),
                    "supabase_service_key_present": bool(os.getenv("ESI_DEVIS_SUPABASE_SERVICE_ROLE_KEY")),
                    "supabase_bucket": ESI_DEVIS_SUPABASE_BUCKET,
                    "supabase_table": ESI_DEVIS_SUPABASE_TABLE,
                },
                "formulaires_2026": engine2026.schemas(),
                "options": {
                    "T1": options_t1(),
                    "T1-T6": {**options_t1_t6(), "type_calage": ["Aucun", "Bandes de mousse", "Bandes de mousse fond plein", "Mousse à ras", "Mousse à ras inversée", "Sous verre / bandes de mousse", "Sous verre 2 faces"]},
                    "MRT": options_mrt(),
                    "T1-T3 MRT": {**options_t1_t3_mrt(), "type_calage": ["Aucun", "Stratocell 30 mm ", "Stratocell 50 mm"]},
                    "T à Glissières": {**options_t_glissieres(), "epaisseur_mousse": ["Mousse 30 mm", "Mousse 50 mm"]},
                    "T Séparations mousse": {**options_t_separations_mousse(), "separations": ["Carton", "Carton + mousse PU 20", "Carton Triple", "PU 30", "PU 50"]},
                    "Objet 1": options_objet1(),
                    "Objet 2 à 6": options_objet2a6(),
                },
            })
        if parsed.path == "/api/notices":
            return self.send_json(load_notices_index())
        # Only the existing public logo and technical notices are served.
        # Calculation workbooks, snapshots, Python and admin settings remain private.
        safe = unquote(parsed.path).lstrip("/")
        target = (ROOT / safe).resolve()
        assets = (ROOT / "assets" / "notices").resolve()
        if target == (ROOT / "logo.png").resolve() or (target.is_relative_to(assets) and target.suffix.lower() in {".pdf", ".png", ".jpg", ".jpeg", ".webp"}):
            return self.send_file(target)
        return self.send_error(404)

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/admin/save":
            if not admin_authorized(self.headers):
                return self.send_json({"ok":False,"error":"Accès administrateur refusé"},401)
            try:
                length=int(self.headers.get("Content-Length","0") or 0)
                if length>500000: return self.send_json({"ok":False,"error":"Requête trop volumineuse"},413)
                data=json.loads(self.rfile.read(length).decode("utf-8") or "{}")
                clean=admin_validate_save(data)
                return self.send_json({"ok":True,"settings":clean})
            except ValueError as exc:
                return self.send_json({"ok":False,"error":str(exc)},400)
            except Exception as exc:
                return self.send_json({"ok":False,"error":str(exc)},500)
        if parsed.path.startswith("/api/calculate/"):
            sheet = unquote(parsed.path.split("/api/calculate/", 1)[1])
            length = int(self.headers.get("Content-Length", "0") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                data = json.loads(raw.decode("utf-8") or "{}")
                result = enrich_result_with_cession(calculate_sheet(sheet, data))
                return self.send_json({"ok": True, "sheet": sheet, "result": {k: fmt(v) for k, v in result.items()}})
            except Exception as exc:
                return self.send_json({"ok": False, "error": str(exc)}, status=500)
        if parsed.path == "/api/caisse-deco/prices":
            if not admin_authorized(self.headers):
                return self.send_json({"ok":False,"error":"Accès administrateur refusé"},401)
            length = int(self.headers.get("Content-Length", "0") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                payload = json.loads(raw.decode("utf-8") or "{}")
                prices = save_caisse_deco_prices(payload)
                return self.send_json({"ok": True, "prices": prices})
            except Exception as exc:
                return self.send_json({"ok": False, "error": str(exc)}, status=500)
        if parsed.path == "/api/notice/find":
            length = int(self.headers.get("Content-Length", "0") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                payload = json.loads(raw.decode("utf-8") or "{}")
                sheet = payload.get("sheet") or ""
                data = payload.get("data") or {}
                found = notice_for_api(sheet, data)
                return self.send_json({"ok": True, "notice": found})
            except Exception as exc:
                return self.send_json({"ok": False, "error": str(exc)}, status=500)
        if parsed.path == "/api/notice/associate":
            debug_log(f"POST /api/notice/associate - supabase_configured={supabase_configured()} url_present={bool(os.getenv('ESI_DEVIS_SUPABASE_URL'))} service_key_present={bool(os.getenv('ESI_DEVIS_SUPABASE_SERVICE_ROLE_KEY'))}")
            try:
                length = int(self.headers.get("Content-Length", "0") or 0)
                body = self.rfile.read(length) if length else b""
                fields, files = parse_multipart_form(self.headers, body)

                sheet = fields.get("sheet", "")
                data_raw = fields.get("data", "{}")
                title = fields.get("title", "")
                data = json.loads(data_raw or "{}")
                file_item = files.get("pdf")
                debug_log(f"Notice reçue : sheet={sheet}, title={title}, file={file_item.get('filename') if file_item else None}, size={len(file_item.get('content') or b'') if file_item else 0}")

                if not sheet:
                    return self.send_json({"ok": False, "error": "Type de caisse manquant."}, status=400)
                if not file_item or not file_item.get("filename"):
                    return self.send_json({"ok": False, "error": "PDF manquant."}, status=400)
                if not str(file_item.get("filename", "")).lower().endswith(".pdf"):
                    return self.send_json({"ok": False, "error": "Le fichier doit être un PDF."}, status=400)
                if not file_item.get("content"):
                    return self.send_json({"ok": False, "error": "Le PDF est vide."}, status=400)

                with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                    tmp.write(file_item["content"])
                    tmp_path = Path(tmp.name)
                try:
                    record = register_notice(sheet, data, title, tmp_path)
                finally:
                    try:
                        tmp_path.unlink()
                    except Exception:
                        pass
                debug_log(f"Notice associée OK : {record}")
                return self.send_json({"ok": True, "notice": notice_record_to_api(record)})
            except Exception as exc:
                debug_log(f"ERREUR association notice : {type(exc).__name__}: {exc}")
                return self.send_json({"ok": False, "error": str(exc)}, status=500)
        if parsed.path == "/api/notice/delete":
            length = int(self.headers.get("Content-Length", "0") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                payload = json.loads(raw.decode("utf-8") or "{}")
                deleted = delete_notice_record(payload)
                return self.send_json({"ok": True, "deleted": deleted})
            except Exception as exc:
                debug_log(f"ERREUR suppression notice : {type(exc).__name__}: {exc}")
                return self.send_json({"ok": False, "error": str(exc)}, status=500)
        if parsed.path == "/api/fiche-pdf":
            length = int(self.headers.get("Content-Length", "0") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                payload = json.loads(raw.decode("utf-8") or "{}")
                sheet = payload.get("sheet") or "Chiffrage"
                data = payload.get("data") or {}
                result = payload.get("result") or calculate_sheet(sheet, data)
                pdf_bytes = generate_internal_fiche_pdf(sheet, data, result)
                filename = f"fiche_chiffrage_{str(sheet).replace(' ', '_').replace('/', '-')}.pdf"
                self.send_response(200)
                self.send_header("Content-Type", "application/pdf")
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
                self.send_header("Content-Length", str(len(pdf_bytes)))
                self.end_headers()
                self.wfile.write(pdf_bytes)
                return
            except Exception as exc:
                return self.send_json({"ok": False, "error": str(exc)}, status=500)
        self.send_error(404)


def main():
    print(f"Matrice de chiffrage ESI : http://{HOST}:{PORT}")
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.serve_forever()


if __name__ == "__main__":
    main()
