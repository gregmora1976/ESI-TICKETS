
from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
import json, re, math
from typing import Any, Dict, Tuple

ROOT = Path(__file__).resolve().parents[1]
DATA_PATH = ROOT / 'data' / 'workbook_snapshot.json'


def _col_to_num(col: str) -> int:
    n = 0
    for c in col.upper():
        n = n * 26 + ord(c) - 64
    return n


def _num_to_col(n: int) -> str:
    s = ''
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(r + 65) + s
    return s


def _split_cell(addr: str) -> Tuple[str, int]:
    m = re.match(r'\$?([A-Z]+)\$?(\d+)$', addr.upper())
    if not m:
        raise ValueError(f'Adresse cellule invalide: {addr}')
    return m.group(1), int(m.group(2))


def _normal_cell(addr: str) -> str:
    col, row = _split_cell(addr.replace('$',''))
    return f'{col}{row}'


def _flatten(x):
    if isinstance(x, list):
        for y in x:
            yield from _flatten(y)
    else:
        yield x


def _to_number(x):
    if x is None or x == '':
        return 0
    if isinstance(x, bool):
        return int(x)
    return x


def xls_if(cond, yes, no=0):
    return yes if cond else no


def xls_sum(*args):
    total = 0
    for v in _flatten(list(args)):
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            total += v
        elif v in (None, ''):
            continue
        else:
            try:
                total += float(v)
            except Exception:
                continue
    return total


def xls_roundup(value, digits=0):
    value = _to_number(value)
    factor = 10 ** int(digits)
    if value >= 0:
        return math.ceil(value * factor) / factor
    return math.floor(value * factor) / factor


def xls_or(*args):
    return any(bool(a) for a in args)


def xls_concatenate(*args):
    return ''.join('' if a is None else str(a) for a in args)


class T1Calculator:
    """Calculateur Python pour l'onglet T1 de la matrice de chiffrage caisses.

    Cette V1 conserve le graphe de calcul Excel de T1 et l'exécute dans Python.
    Les entrées peuvent être modifiées par adresse de cellule (ex. C29, F25).
    """
    OUTPUT_CELLS = {
        'dimensions_exterieures_longueur': 'C46',
        'dimensions_exterieures_epaisseur': 'D46',
        'dimensions_exterieures_hauteur': 'E46',
        'poids_caisse': 'C48',
        'minutes_production': 'C49',
        'total_matiere': 'C122',
        'total_mo': 'C123',
        'total_frais_generaux': 'C124',
        'total_revient': 'C125',
        'marge': 'C126',
        'prix_vente': 'C129',
    }

    def __init__(self, overrides: Dict[str, Any] | None = None):
        data = json.loads(DATA_PATH.read_text(encoding='utf-8'))
        self.formulas: Dict[str, str] = data['formulas']
        self.values: Dict[str, Any] = data['values']
        self.names = data.get('names', {})
        self.cache: Dict[str, Any] = {}
        if overrides:
            for cell, value in overrides.items():
                self.set(cell, value)

    def set(self, cell: str, value: Any, sheet: str = 'T1') -> None:
        key = self._key(sheet, cell)
        self.values[key] = value
        self.formulas.pop(key, None)
        self.cache.clear()

    def _key(self, sheet: str, cell: str) -> str:
        return f"{sheet}!{_normal_cell(cell)}"

    def cell(self, sheet: str, cell: str) -> Any:
        key = self._key(sheet, cell)
        if key in self.cache:
            return self.cache[key]
        if key in self.formulas:
            value = self._eval_formula(sheet, self.formulas[key])
        else:
            value = self.values.get(key, 0)
        self.cache[key] = value
        return value

    def range_values(self, sheet: str, ref: str):
        ref = ref.replace('$','')
        if ':' not in ref:
            return [[self.cell(sheet, ref)]]
        a, b = ref.split(':', 1)
        c1, r1 = _split_cell(a); c2, r2 = _split_cell(b)
        n1, n2 = _col_to_num(c1), _col_to_num(c2)
        return [[self.cell(sheet, f'{_num_to_col(c)}{r}') for c in range(n1, n2+1)] for r in range(r1, r2+1)]

    def name(self, name: str) -> Any:
        dest = self.names.get(name)
        if not dest:
            # Some names contain accents; if absent return zero instead of blocking the calculation.
            return 0
        sheet, ref = dest
        ref = ref.replace('$','')
        if ':' in ref:
            return self.range_values(sheet, ref)
        return self.cell(sheet, ref)

    def vlookup(self, lookup_value, table, col_index, approximate=True):
        rows = table
        col_index = int(col_index) - 1
        # exact match first
        for row in rows:
            if row and row[0] == lookup_value:
                return row[col_index] if col_index < len(row) else 0
        if approximate:
            best = None
            for row in rows:
                if not row:
                    continue
                left = row[0]
                try:
                    if float(left) <= float(lookup_value):
                        best = row
                except Exception:
                    continue
            if best is not None and col_index < len(best):
                return best[col_index]
        return 0

    def _eval_formula(self, sheet: str, formula: str) -> Any:
        expr = formula[1:] if formula.startswith('=') else formula
        if expr.startswith('+'):
            expr = expr[1:]
        expr = self._translate(expr, sheet)
        env = {
            'IF': xls_if, 'SUM': xls_sum, 'VLOOKUP': self.vlookup,
            'ROUNDUP': xls_roundup, 'OR': xls_or, 'CONCATENATE': xls_concatenate,
            'C': self.cell, 'R': self.range_values, 'N': self.name,
            'TRUE': True, 'FALSE': False,
        }
        try:
            return eval(expr, {'__builtins__': {}}, env)
        except ZeroDivisionError:
            return 0
        except Exception as exc:
            raise RuntimeError(f"Erreur formule T1: {formula} -> {expr}: {exc}") from exc

    def _translate(self, expr: str, current_sheet: str) -> str:
        placeholders = {}
        def hold(s):
            key = f'§{len(placeholders)}§'
            placeholders[key] = s
            return key

        # Strings
        expr = re.sub(r'"(?:[^"]|"")*"', lambda m: hold(m.group(0)), expr)
        # Sheet-qualified ranges/cells, quoted sheet names
        def repl_sheet(m):
            sh = m.group(1) or m.group(2)
            ref = m.group(3).replace('$','')
            if ':' in ref:
                return hold(f'R({sh!r},{ref!r})')
            return hold(f'C({sh!r},{ref!r})')
        expr = re.sub(r"'([^']+)'!\$?([A-Z]+\$?\d+(?::\$?[A-Z]+\$?\d+)?)", lambda m: repl_sheet((lambda mm: type('M',(),{'group':lambda self,i: {1:mm.group(1),2:None,3:mm.group(2)}[i]})())(m)), expr)
        expr = re.sub(r"([A-Za-z0-9_ éèêàçûùôîïë'/-]+)!\$?([A-Z]+\$?\d+(?::\$?[A-Z]+\$?\d+)?)", lambda m: hold(f"R({m.group(1)!r},{m.group(2).replace('$','')!r})" if ':' in m.group(2) else f"C({m.group(1)!r},{m.group(2).replace('$','')!r})"), expr)
        # Local ranges then cells
        expr = re.sub(r'(?<![A-Za-z0-9_])\$?([A-Z]{1,3})\$?(\d+):\$?([A-Z]{1,3})\$?(\d+)', lambda m: hold(f"R({current_sheet!r},'{m.group(1)}{m.group(2)}:{m.group(3)}{m.group(4)}')"), expr)
        expr = re.sub(r'(?<![A-Za-z0-9_])\$?([A-Z]{1,3})\$?(\d+)(?![A-Za-z0-9_])', lambda m: hold(f"C({current_sheet!r},'{m.group(1)}{m.group(2)}')"), expr)
        # Operators and Excel booleans
        expr = expr.replace('<>', '!=').replace('^', '**')
        expr = re.sub(r'(?<![<>=!])=(?!=)', '==', expr)
        for f in ['IF','SUM','VLOOKUP','ROUNDUP','OR','CONCATENATE']:
            expr = re.sub(rf'\b{f}\s*\(', f'{f}(', expr, flags=re.I)
        # Defined names: longer first, not function names/placeholders
        function_names = {'IF','SUM','VLOOKUP','ROUNDUP','OR','CONCATENATE','TRUE','FALSE'}
        for name in sorted(self.names, key=len, reverse=True):
            if name.upper() in function_names:
                continue
            expr = re.sub(rf'(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])', hold(f'N({name!r})'), expr)
        for k, v in placeholders.items():
            expr = expr.replace(k, v)
        return expr

    def outputs(self) -> Dict[str, Any]:
        return {label: self.cell('T1', cell) for label, cell in self.OUTPUT_CELLS.items()}


@dataclass
class T1Inputs:
    """Entrées principales détectées dans l'onglet T1.

    Les autres paramètres restent accessibles via overrides={"C13": "...", ...}.
    """
    # Dimensions oeuvre en cm. Correspondance Excel T1 : C29 / D29 / E29.
    longueur_cm: float | None = None   # C29 : Longueur
    epaisseur_cm: float | None = None  # D29 : Epaisseur / profondeur
    hauteur_cm: float | None = None    # E29 : Hauteur

    # Choix principaux demandés pour T1.
    type_isolant: str | None = None    # C17
    fermeture: str | None = None       # C19
    peinture: str | None = None        # F19
    tyvek: str | None = None           # F21
    type_calage: str | None = None     # B34

    # Autres options conservées.
    type_contreplaque: str | None = None # C13
    garnissage: str | None = None        # F15
    option_calage: str | None = None     # F23
    epaisseur_mousse: str | None = None  # F25
    overrides: Dict[str, Any] = field(default_factory=dict)

    def as_overrides(self) -> Dict[str, Any]:
        data = dict(self.overrides)
        mapping = {
            'longueur_cm': 'C29',
            'epaisseur_cm': 'D29',
            'hauteur_cm': 'E29',
            'type_isolant': 'C17',
            'fermeture': 'C19',
            'peinture': 'F19',
            'tyvek': 'F21',
            'type_calage': 'B34',
            'type_contreplaque': 'C13',
            'garnissage': 'F15',
            'option_calage': 'F23',
            'epaisseur_mousse': 'F25',
        }
        for attr, cell in mapping.items():
            val = getattr(self, attr)
            if val is not None:
                data[cell] = val
        return data


OPTIONS_T1 = {
    'type_isolant': ['Aucun', 'Double isotherme en 30', 'Double isotherme en 50', 'Isotherme en 30', 'Isotherme en 50', 'Super isotherme en 30', 'Super isotherme en 50'],
    'fermeture': ['Boulons et platines', 'Vis bois'],
    'peinture': ['Non', 'Oui'],
    'tyvek': ['Non', 'Oui'],
    'type_calage': ['Aucun', 'Bandes de mousse', 'Bandes de mousse fond plein', 'Entourage Stratocell 20 mm', 'Entourage Stratocell 30 mm', 'Mousse à ras', 'Mousse à ras inversée', 'Sous verre / bandes de mousse', 'Sous verre 2 faces'],
    'option_calage': ['Aucun', 'Agglocelle 50 mm', 'Ethafoam 50 mm'],
    'epaisseur_mousse': ['PU 30 mm', 'PU 50 mm'],
}


def options_t1() -> Dict[str, list[str]]:
    return OPTIONS_T1.copy()


def calculer_t1(inputs: T1Inputs | None = None, **overrides) -> Dict[str, Any]:
    params = {}
    if inputs:
        params.update(inputs.as_overrides())
    params.update(overrides)
    calc = T1Calculator(params)
    return calc.outputs()
