from __future__ import annotations
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
    if isinstance(x, (list, tuple)) or (hasattr(x, '__iter__') and not isinstance(x, (str, bytes, dict))):
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


def xls_max(*args):
    vals = []
    for v in _flatten(list(args)):
        if v in (None, ''):
            continue
        try:
            vals.append(float(v))
        except Exception:
            continue
    return max(vals) if vals else 0


def xls_roundup(value, digits=0):
    value = _to_number(value)
    factor = 10 ** int(digits)
    if value >= 0:
        return math.ceil(value * factor) / factor
    return math.floor(value * factor) / factor


def xls_rounddown(value, digits=0):
    value = _to_number(value)
    factor = 10 ** int(digits)
    if value >= 0:
        return math.floor(value * factor) / factor
    return math.ceil(value * factor) / factor


def xls_or(*args):
    return any(bool(a) for a in args)


def xls_concatenate(*args):
    return ''.join('' if a is None else str(a) for a in args)


class LazyRange:
    def __init__(self, calc, sheet: str, ref: str):
        self.calc = calc
        self.sheet = sheet
        self.ref = ref.replace('$','')
        a, b = self.ref.split(':', 1) if ':' in self.ref else (self.ref, self.ref)
        c1, r1 = _split_cell(a); c2, r2 = _split_cell(b)
        self.c1, self.c2 = _col_to_num(c1), _col_to_num(c2)
        self.r1, self.r2 = r1, r2

    def cell(self, row_idx: int, col_idx: int):
        return self.calc.cell(self.sheet, f'{_num_to_col(self.c1 + col_idx)}{self.r1 + row_idx}')

    def raw_cell(self, row_idx: int, col_idx: int):
        return self.calc.values.get(self.calc._key(self.sheet, f'{_num_to_col(self.c1 + col_idx)}{self.r1 + row_idx}'), 0)

    def rows_count(self):
        return self.r2 - self.r1 + 1

    def cols_count(self):
        return self.c2 - self.c1 + 1

    def __iter__(self):
        for r in range(self.rows_count()):
            yield [self.cell(r, c) for c in range(self.cols_count())]


class ExcelSheetCalculator:
    def __init__(self, sheet_name: str, overrides: Dict[str, Any] | None = None):
        data = json.loads(DATA_PATH.read_text(encoding='utf-8'))
        self.sheet_name = sheet_name
        self.formulas: Dict[str, str] = data['formulas']
        self.values: Dict[str, Any] = data['values']
        self.names = data.get('names', {})
        self.cache: Dict[str, Any] = {}
        if overrides:
            for cell, value in overrides.items():
                if '!' in cell:
                    sh, addr = cell.split('!', 1)
                    self.set(addr, value, sh)
                else:
                    self.set(cell, value, sheet_name)

    def set(self, cell: str, value: Any, sheet: str | None = None) -> None:
        key = self._key(sheet or self.sheet_name, cell)
        self.values[key] = value
        self.formulas.pop(key, None)
        self.cache.clear()

    def _key(self, sheet: str, cell: str) -> str:
        return f"{sheet}!{_normal_cell(cell)}"

    def cell(self, sheet: str, cell: str) -> Any:
        key = self._key(sheet, cell)
        if key in self.cache:
            return self.cache[key]
        # For supporting sheets (Prix Matiere, Infos Clients...), cached values are safer
        # and avoid reimplementing every parameter formula. Target sheet formulas remain live.
        if sheet != self.sheet_name and key in self.values:
            value = self.values.get(key, 0)
        elif key in self.formulas:
            value = self._eval_formula(sheet, self.formulas[key])
        else:
            value = self.values.get(key, 0)
        self.cache[key] = value
        return value

    def range_values(self, sheet: str, ref: str):
        ref = ref.replace('$','')
        if ':' not in ref:
            return LazyRange(self, sheet, f'{ref}:{ref}')
        return LazyRange(self, sheet, ref)

    def name(self, name: str) -> Any:
        if self.sheet_name == 'Objet 1' and name == 'nombre_de_calages':
            # The workbook name points to F25, but the formulas that use the name need the numeric count (column O / 15).
            return self.vlookup(self.cell('Objet 1','C33'), self.range_values('Objet 1','A137:S150'), 15)
        dest = self.names.get(name)
        if not dest:
            return 0
        sheet, ref = dest
        ref = ref.replace('$','')
        if ':' in ref:
            return self.range_values(sheet, ref)
        return self.cell(sheet, ref)

    def vlookup(self, lookup_value, table, col_index, approximate=True):
        col_index = int(col_index) - 1
        if isinstance(table, LazyRange):
            for r in range(table.rows_count()):
                left = table.cell(r, 0)
                if left == lookup_value or (isinstance(left, str) and isinstance(lookup_value, str) and left.strip() == lookup_value.strip()):
                    return table.cell(r, col_index) if col_index < table.cols_count() else 0
            if approximate:
                best_r = None
                for r in range(table.rows_count()):
                    left = table.cell(r, 0)
                    try:
                        if float(left) <= float(lookup_value):
                            best_r = r
                    except Exception:
                        continue
                if best_r is not None and col_index < table.cols_count():
                    return table.cell(best_r, col_index)
            return 0
        rows = table
        for row in rows:
            if row and (row[0] == lookup_value or (isinstance(row[0], str) and isinstance(lookup_value, str) and row[0].strip() == lookup_value.strip())):
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
            'IF': xls_if, 'SUM': xls_sum, 'MAX': xls_max, 'VLOOKUP': self.vlookup,
            'ROUNDUP': xls_roundup, 'ROUNDDOWN': xls_rounddown, 'OR': xls_or, 'CONCATENATE': xls_concatenate,
            'C': self.cell, 'R': self.range_values, 'N': self.name,
            'TRUE': True, 'FALSE': False,
        }
        try:
            return eval(expr, {'__builtins__': {}}, env)
        except ZeroDivisionError:
            return 0
        except Exception as exc:
            raise RuntimeError(f"Erreur formule {sheet}: {formula} -> {expr}: {exc}") from exc

    def _translate(self, expr: str, current_sheet: str) -> str:
        placeholders = {}
        def hold(s):
            key = f'§{len(placeholders)}§'
            placeholders[key] = s
            return key

        expr = re.sub(r'"(?:[^"]|"")*"', lambda m: hold(m.group(0)), expr)

        # Quoted sheet references: 'Prix Matiere'!$A$1:$B$3
        def repl_quoted(m):
            sh = m.group(1)
            sh = sh.replace('[1]', '')
            ref = m.group(2).replace('$','')
            return hold(f"R({sh!r},{ref!r})" if ':' in ref else f"C({sh!r},{ref!r})")
        expr = re.sub(r"'([^']+)'!\$?([A-Z]+\$?\d+(?::\$?[A-Z]+\$?\d+)?)", repl_quoted, expr)

        # Unquoted sheet references. Keep conservative to avoid catching functions.
        def repl_unquoted(m):
            sh = m.group(1).strip().replace('[1]', '')
            ref = m.group(2).replace('$','')
            return hold(f"R({sh!r},{ref!r})" if ':' in ref else f"C({sh!r},{ref!r})")
        expr = re.sub(r"([A-Za-zÀ-ÿ0-9_ éèêàçûùôîïë/\-]+)!\$?([A-Z]+\$?\d+(?::\$?[A-Z]+\$?\d+)?)", repl_unquoted, expr)

        expr = re.sub(r'(?<![A-Za-z0-9_])\$?([A-Z]{1,3})\$?(\d+):\$?([A-Z]{1,3})\$?(\d+)', lambda m: hold(f"R({current_sheet!r},'{m.group(1)}{m.group(2)}:{m.group(3)}{m.group(4)}')"), expr)
        expr = re.sub(r'(?<![A-Za-z0-9_])\$?([A-Z]{1,3})\$?(\d+)(?![A-Za-z0-9_])', lambda m: hold(f"C({current_sheet!r},'{m.group(1)}{m.group(2)}')"), expr)

        expr = expr.replace('<>', '!=').replace('^', '**')
        expr = re.sub(r'(?<![<>=!])=(?!=)', '==', expr)
        for f in ['IF','SUM','MAX','VLOOKUP','ROUNDUP','ROUNDDOWN','OR','CONCATENATE']:
            expr = re.sub(rf'\b{f}\s*\(', f'{f}(', expr, flags=re.I)

        # Sheet-qualified defined names, e.g. 'Objet 1'!nombre_de_calages
        for name in sorted(self.names, key=len, reverse=True):
            expr = re.sub(rf"'[^']+'!{re.escape(name)}(?![A-Za-z0-9_])", hold(f'N({name!r})'), expr)
            expr = re.sub(rf"[A-Za-zÀ-ÿ0-9_ éèêàçûùôîïë/\-]+!{re.escape(name)}(?![A-Za-z0-9_])", hold(f'N({name!r})'), expr)

        function_names = {'IF','SUM','MAX','VLOOKUP','ROUNDUP','ROUNDDOWN','OR','CONCATENATE','TRUE','FALSE'}
        for name in sorted(self.names, key=len, reverse=True):
            if name.upper() in function_names:
                continue
            expr = re.sub(rf'(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])', hold(f'N({name!r})'), expr)

        for k, v in placeholders.items():
            expr = expr.replace(k, v)
        return expr
