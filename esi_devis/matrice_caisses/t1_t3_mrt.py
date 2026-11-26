from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Dict
from .excel_engine import ExcelSheetCalculator


class T1T3MRTCalculator(ExcelSheetCalculator):
    """Calculateur Python pour l'onglet Excel exact `T1-T3 MRT`."""
    OUTPUT_CELLS = {
        'dimensions_interieures_longueur': 'C39',
        'dimensions_interieures_epaisseur': 'D39',
        'dimensions_interieures_hauteur': 'E39',
        'dimensions_exterieures_longueur': 'C41',
        'dimensions_exterieures_epaisseur': 'D41',
        'dimensions_exterieures_hauteur': 'E41',
        'poids_caisse': 'C43',
        'bilan_carbone': 'E44',
        'minutes_production': 'C44',
        'total_matiere': 'C126',
        'total_mo': 'C127',
        'minutes_mo': 'D127',
        'total_frais_generaux': 'C128',
        'total_revient': 'C129',
        'marge': 'C130',
        'remise_ou_majoration': 'C131',
        'prix_vente': 'C133',
    }

    def __init__(self, overrides: Dict[str, Any] | None = None):
        super().__init__('T1-T3 MRT', overrides)

    def outputs(self) -> Dict[str, Any]:
        return {label: self.cell('T1-T3 MRT', cell) for label, cell in self.OUTPUT_CELLS.items()}


@dataclass
class MRTItem:
    mode_dimensions: str | None = None
    longueur_cm: float | None = None
    epaisseur_cm: float | None = None
    hauteur_cm: float | None = None
    face_mrt: str | None = None
    arriere_mrt: str | None = None


@dataclass
class T1T3MRTInputs:
    """Entrées principales de l'onglet T1-T3 MRT.

    L'onglet accepte jusqu'à 3 MRT. Les cellules d'origine sont conservées :
    MRT 1 = B27/C27/D27/E27/F27/G27, MRT 2 = B30/C30/D30/E30/F30/G30,
    MRT 3 = B33/C33/D33/E33/F33/G33.
    """
    type_contreplaque: str | None = None
    barres: str | None = None
    garnissage: str | None = None
    type_isolant: str | None = None
    fermeture: str | None = None
    peinture: str | None = None
    poignees: str | None = None
    skis: str | None = None
    option_calage: str | None = None
    type_calage: str | None = None
    responsable_dossier: str | None = None
    client: str | None = None
    delai: str | None = None
    mrts: list[MRTItem] = field(default_factory=list)
    overrides: Dict[str, Any] = field(default_factory=dict)

    def as_overrides(self) -> Dict[str, Any]:
        data = dict(self.overrides)
        # Sécurise une formule Excel qui référence E11 dans une branche non utilisée.
        # Excel n'évalue pas cette branche lorsque le calage est 'Aucun', Python oui.
        data.setdefault('E11', 0)
        mapping = {
            'responsable_dossier': 'F9',
            'client': 'F10',
            'delai': 'F11',
            'type_contreplaque': 'C13',
            'barres': 'C15',
            'garnissage': 'F15',
            'type_isolant': 'C17',
            'fermeture': 'C19',
            'peinture': 'F19',
            'poignees': 'C21',
            'skis': 'C23',
            'option_calage': 'F23',
            'type_calage': 'B37',
        }
        for attr, cell in mapping.items():
            val = getattr(self, attr)
            if val is not None:
                data[cell] = val
        cells = [
            ('B27','C27','D27','E27','F27','G27'),
            ('B30','C30','D30','E30','F30','G30'),
            ('B33','C33','D33','E33','F33','G33'),
        ]
        for mrt, (mode_c, long_c, ep_c, haut_c, face_c, arr_c) in zip(self.mrts, cells):
            if mrt.mode_dimensions is not None: data[mode_c] = mrt.mode_dimensions
            if mrt.longueur_cm is not None: data[long_c] = mrt.longueur_cm
            if mrt.epaisseur_cm is not None: data[ep_c] = mrt.epaisseur_cm
            if mrt.hauteur_cm is not None: data[haut_c] = mrt.hauteur_cm
            if mrt.face_mrt is not None: data[face_c] = mrt.face_mrt
            if mrt.arriere_mrt is not None: data[arr_c] = mrt.arriere_mrt
        return data


OPTIONS_T1_T3_MRT = {
    'mode_dimensions': ["Dimensions de l'oeuvre", 'Dimensions ext MRT existant', 'Aucun'],
    'type_contreplaque': ['Peuplier 10 mm', 'Peuplier 15 mm', 'Peuplier Tour 15 mm Fonds 10 mm'],
    'barres': ['Pin non raboté', 'Pin raboté 4 faces'],
    'garnissage': ['Oui', 'Non'],
    'type_isolant': ['Aucun', 'Double isotherme en 30', 'Double isotherme en 50', 'Isotherme en 30', 'Isotherme en 50', 'Super isotherme en 30', 'Super isotherme en 50'],
    'fermeture': ['Vis bois', 'Boulons et platines'],
    'peinture': ['Non', 'Oui'],
    'poignees': ['Bois', 'Métal x 2 sur bouts', 'Métal x 4 sur GC'],
    'skis': ['Skis', '10 dessous en semelles', '10 dessous en skis'],
    'option_calage': ['Aucun', 'Stabilisateur', 'Option A', 'Option B'],
    'type_calage': ['Aucun', 'Etahfoam 50 mm', 'Etahfoam 80 mm'],
    'face_mrt': ['Mrt Ouvert', 'Mrt Fermé Carton Double', 'Mrt Fermé Carton Triple', 'Mrt Fermé cp 10 mm', 'Mrt Fermé Macrolon 10 mm'],
    'arriere_mrt': ['Mrt Ouvert', 'Mrt Fermé Carton Double', 'Mrt Fermé Carton Triple', 'Mrt Fermé cp 10 mm', 'Mrt Fermé Macrolon 10 mm'],
}


def options_t1_t3_mrt() -> Dict[str, list[str]]:
    return OPTIONS_T1_T3_MRT.copy()


def calculer_t1_t3_mrt(inputs: T1T3MRTInputs | None = None, **overrides) -> Dict[str, Any]:
    params = {}
    if inputs:
        params.update(inputs.as_overrides())
    params.update(overrides)
    calc = T1T3MRTCalculator(params)
    return calc.outputs()
