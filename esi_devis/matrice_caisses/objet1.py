from __future__ import annotations
from dataclasses import dataclass
from typing import Dict, Any
from .excel_engine import ExcelSheetCalculator


def _f(v, default=0.0):
    try:
        if v in (None, ''):
            return default
        return float(str(v).replace(',', '.').replace(' ', ''))
    except Exception:
        return default


@dataclass
class Objet1Inputs:
    longueur_cm: float | None = None
    largeur_cm: float | None = None
    hauteur_cm: float | None = None
    type_contreplaque: str | None = 'Peuplier 15 mm'
    barres: str | None = 'Pin raboté 4 faces'
    garnissage: str | None = 'Oui'
    type_isolant: str | None = 'Aucun'
    fermeture: str | None = 'Vis bois'
    peinture: str | None = 'Non'
    poignees: str | None = 'Non'
    skis: str | None = '10 dessous en skis'
    cuvette_au_dessus: str | None = 'Non'
    option: str | None = 'Aucun'
    nombre_cote_ouvrant: str | None = 'GC + Couvercle'
    type_calage: str | None = 'Aucun'
    nombre_calages: float | None = None
    plateau_interieur: str | None = 'Aucun'
    base: str | None = 'Stratocell 50 mm'
    responsable_dossier: str | None = None
    client: str | None = 'ESI'


def options_objet1() -> Dict[str, list[str]]:
    return {
        'type_contreplaque': ['Peuplier 10 mm', 'Peuplier 15 mm'],
        'barres': ['Pin raboté 4 faces', 'Pin non raboté'],
        'garnissage': ['Oui','Non'],
        'type_isolant': ['Aucun','Isotherme en 30','Isotherme en 50','Double isotherme en 30','Double isotherme en 50','Super isotherme en 30','Super isotherme en 50'],
        'fermeture': ['Vis bois','Boulons et platines'],
        'peinture': ['Non','Oui'],
        'poignees': ['Non','Bois','Métal x 2 sur bouts','Métal x 4 sur GC'],
        'skis': ['10 dessous en skis','10 dessous en semelles','Skis'],
        'cuvette_au_dessus': ['Non','Oui'],
        'option': ['Aucun','Chapeau','Melinex','Tyvek'],
        'nombre_cote_ouvrant': ['GC + Couvercle','GC','Couvercle'],
        'type_calage': ['Aucun','Bancs fermés','Bancs ouverts','Bandes de mousse 30 mm','Bandes de mousse 50 mm','Découpe Mousse','Ecrin horizontal','Ecrin vertical','Guillotines horizontales','Guillotines verticales','Mousse à ras + Guillotines verticales','Mousse au Carré 20 mm','Mousse au Carré 30 mm ','Mousse au Carré 50 mm'],
        'plateau_interieur': ['Aucun','Carton garnie tyvec','Cp 10 mm garnie tyvec','Cp 15 mm + Plasta 20 mm','Cp 15 mm + Plastazote 20 mm coulissant','Cp 15 mm garni valsem','Cp 15 mm garni valsem coulissant'],
        'base': ['Aucun','Agglocelle 50 mm','Ethafoam 50 mm','PU 30 mm','PU 50 mm','Stratocell 20 mm','Stratocell 30 mm','Stratocell 30 mm 65 Kg','Stratocell 50 mm','Stratocell 50 mm 65 Kg'],
    }


def calculer_objet1(inputs: Objet1Inputs) -> Dict[str, Any]:
    overrides = {
        'C31': _f(inputs.longueur_cm),
        'D31': _f(inputs.largeur_cm),
        'E31': _f(inputs.hauteur_cm),
        'C13': inputs.type_contreplaque or 'Peuplier 15 mm',
        'C15': inputs.barres or 'Pin raboté 4 faces',
        'F15': inputs.garnissage or 'Oui',
        'C17': inputs.type_isolant or 'Aucun',
        'C19': inputs.fermeture or 'Vis bois',
        'F19': inputs.peinture or 'Non',
        'C21': inputs.poignees or 'Non',
        'F21': inputs.skis or '10 dessous en skis',
        'C23': inputs.cuvette_au_dessus or 'Non',
        'F23': inputs.option or 'Aucun',
        'C33': inputs.type_calage or 'Aucun',
        'C35': inputs.plateau_interieur or 'Aucun',
        'C36': inputs.base or 'Stratocell 50 mm',
        'F9': inputs.responsable_dossier or 'CALCUL',
        'F10': inputs.client or 'ESI',
    }
    if inputs.nombre_calages not in (None, ''):
        overrides['E33'] = _f(inputs.nombre_calages)
    calc = ExcelSheetCalculator('Objet 1', overrides)
    ext_l = calc.cell('Objet 1', 'C44')
    ext_w = calc.cell('Objet 1', 'D44')
    ext_h = calc.cell('Objet 1', 'E44')
    prix = calc.cell('Objet 1', 'C131')
    minutes = calc.cell('Objet 1', 'D125')
    poids = calc.cell('Objet 1', 'C46')
    try:
        carbone = float(poids) * 2.08 if poids else 0.0
    except Exception:
        carbone = 0.0
    return {
        'dimensions_exterieures_longueur': round(float(ext_l or 0), 2),
        'dimensions_exterieures_epaisseur': round(float(ext_w or 0), 2),
        'dimensions_exterieures_hauteur': round(float(ext_h or 0), 2),
        'prix_achat': round(float(prix or 0), 2),
        'prix_vente': round(float(prix or 0), 2),
        'poids_caisse': round(float(poids or 0), 2),
        'minutes_production': round(float(minutes or 0), 2),
        'bilan_carbone': round(float(carbone or 0), 2),
        'debug_total_matiere': round(float(calc.cell('Objet 1','C124') or 0), 2),
        'debug_total_mo': round(float(calc.cell('Objet 1','C125') or 0), 2),
        'debug_frais_generaux': round(float(calc.cell('Objet 1','C126') or 0), 2),
        'debug_revient': round(float(calc.cell('Objet 1','C127') or 0), 2),
        'debug_marge': round(float(calc.cell('Objet 1','C128') or 0), 2),
    }
