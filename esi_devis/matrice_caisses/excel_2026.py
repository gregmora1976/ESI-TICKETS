"""2026 workbook calculation engine; unsupported formulas fail explicitly."""
from pathlib import Path
from functools import lru_cache
import math, operator, re
from openpyxl import load_workbook
from openpyxl.formula.tokenizer import Tokenizer
from openpyxl.utils.cell import range_boundaries, get_column_letter

SOURCE = Path(__file__).resolve().parents[1] / 'data' / 'ESI-TABLEAU-OBJET 2026.xlsx'
@lru_cache(maxsize=1)
def workbook():
    return load_workbook(SOURCE, read_only=False, keep_links=True)

class CalculationError(ValueError): pass

def excel_text(v):
    return str(int(v)) if isinstance(v,float) and v.is_integer() else str(v)


class Engine:
    def __init__(self, sheet, overrides=None, materials=None):
        self.wb=workbook(); self.sheet=sheet; self.cache={}; self.active=set(); self.overrides={}
        for address,value in (overrides or {}).items():
            c=self.wb[sheet][address]
            if c.data_type=='f': raise CalculationError(f'Cellule non saisissable: {address}')
            self.overrides[sheet,c.coordinate]=value
        for address,value in (materials or {}).items():
            c=self.wb['Prix Matiere'][address]
            if c.column!=2 or not isinstance(self.wb['Prix Matiere'].cell(c.row,1).value,str): raise CalculationError('Prix matière invalide')
            self.overrides['Prix Matiere',c.coordinate]=value
    def cell(self,sheet,address):
        address=address.replace('$',''); key=(sheet,address)
        if key in self.overrides: return self.overrides[key]
        if key in self.cache: return self.cache[key]
        if key in self.active: raise CalculationError(f'Cycle: {key}')
        self.active.add(key)
        try:
            c=self.wb[sheet][address]; v=c.value
            if c.data_type=='e': raise CalculationError(f'{key}: {v}')
            if c.data_type=='f':
                if sheet=='Objet 2 à 6' and address in {f'C{r}' for r in (122,123,138,139,154,155,170,171,186,187,202,203)}:
                    row=int(address[1:]); start=118+16*((row-118)//16)
                    if str(self.cell(sheet,f'B{start}')).casefold()=='bancs ouverts': v='Stratocell 50 mm'
                    else: v=self.evaluate(sheet,parse(v))
                elif sheet=='T Séparations mousse' and address in {f'N{r}' for r in (*range(114,124),*range(130,140))}:
                    ref=self.cell(sheet,'E'+address[1:]); match=re.fullmatch(r'PU (\d+) mm',str(ref))
                    if match and int(match[1])>50: v=self.cell('Prix Matiere','J46')
                    else: v=self.evaluate(sheet,parse(v))
                else: v=self.evaluate(sheet, parse(v))
            self.cache[key]=0 if v is None else v
            return self.cache[key]
        finally: self.active.remove(key)
    def reference(self,sheet,ref):
        if '!' in ref:
            sheet,ref=ref.rsplit('!',1); sheet=sheet.strip("'").replace("''", "'")
            if sheet in ('[1]Prix Matiere','[3]Prix Matiere','[1]Infos Clients','[1]Objet 1','[2]Objet 1'): sheet=sheet[3:]
            elif '[' in sheet: raise CalculationError(f'Lien externe non validé: {sheet}')
        ref=ref.replace('$','')
        try: bounds=range_boundaries(ref)
        except ValueError:
            name=self.wb[sheet].defined_names.get(ref) or self.wb.defined_names.get(ref)
            if not name: raise CalculationError(f'Nom inconnu: {sheet}!{ref}')
            return self.reference(sheet,name.attr_text)
        if any(x is None for x in bounds): raise CalculationError(f'Plage non bornée: {ref}')
        if ':' not in ref: return self.cell(sheet,ref)
        return (sheet,bounds)
    def range_cell(self,r,row,col):
        sh,(c1,r1,c2,r2)=r
        if not (0<=row<=r2-r1 and 0<=col<=c2-c1): raise CalculationError('Indice de recherche hors plage')
        return self.cell(sh,f'{get_column_letter(c1+col)}{r1+row}')
    def flatten(self,vals):
        for v in vals:
            if isinstance(v,tuple):
                _,(a,b,c,d)=v
                for i in range(d-b+1):
                    for j in range(c-a+1): yield self.range_cell(v,i,j)
            else: yield v
    def evaluate(self,s,n):
        tag=n[0]
        if tag=='literal': return n[1]
        if tag=='ref': return self.reference(s,n[1])
        if tag=='unary':
            v=self.evaluate(s,n[2]); return -v if n[1]=='-' else v/100 if n[1]=='%' else v
        if tag=='op':
            a,b=(self.evaluate(s,x) for x in n[2:]); op=n[1]
            if op in ('=','<>','<','>','<=','>='):
                if isinstance(a,str) and isinstance(b,str): a,b=a.casefold(),b.casefold()
                if type(a)!=type(b) and (isinstance(a,str) or isinstance(b,str)):
                    a,b=(isinstance(a,str),str(a)),(isinstance(b,str),str(b))
                return {'=':operator.eq,'<>':operator.ne,'<':operator.lt,'>':operator.gt,'<=':operator.le,'>=':operator.ge}[op](a,b)
            if op=='&': return excel_text(a)+excel_text(b)
            return {'+':operator.add,'-':operator.sub,'*':operator.mul,'/':operator.truediv,'^':operator.pow}[op](a,b)
        f,args=n[1:]
        if f=='IF': return self.evaluate(s,args[1] if self.evaluate(s,args[0]) else args[2]) if len(args)==3 else (self.evaluate(s,args[1]) if self.evaluate(s,args[0]) else False)
        v=[self.evaluate(s,a) for a in args]
        if f in ('VLOOKUP','HLOOKUP'):
            lookup,r,idx=v[:3]; approximate=len(v)<4 or bool(v[3]); _,(a,b,c,d)=r; vertical=f=='VLOOKUP'; best=None
            for i in range((d-b if vertical else c-a)+1):
                k=self.range_cell(r,i,0) if vertical else self.range_cell(r,0,i)
                if isinstance(k,str) and isinstance(lookup,str): equal=k.casefold()==lookup.casefold() 
                else: equal=k==lookup
                if equal: best=i; break
                if approximate and isinstance(k,(int,float)) and isinstance(lookup,(int,float)) and k<=lookup: best=i
                if approximate and isinstance(k,str) and isinstance(lookup,str) and k.casefold()<=lookup.casefold():
                    if best is None or str(self.range_cell(r,best,0) if vertical else self.range_cell(r,0,best)).casefold()<=k.casefold(): best=i
            if best is None: raise CalculationError(f'{f}: valeur introuvable {lookup!r}')
            return self.range_cell(r,best,int(idx)-1) if vertical else self.range_cell(r,int(idx)-1,best)
        if f in ('SUM','MAX','MIN'):
            nums=[x for x in self.flatten(v) if isinstance(x,(int,float)) and not isinstance(x,bool)]
            return sum(nums) if f=='SUM' else (max(nums,default=0) if f=='MAX' else min(nums,default=0))
        if f in ('ROUNDUP','ROUNDDOWN'):
            x=v[0]; factor=10**int(v[1]); return math.copysign((math.ceil if f=='ROUNDUP' else math.floor)(abs(x)*factor)/factor,x)
        if f=='OR': return any(v)
        if f=='AND': return all(v)
        if f=='CONCATENATE': return ''.join(excel_text(x) for x in v)
        if f=='PI': return math.pi
        if f=='TODAY':
            from datetime import date
            from openpyxl.utils.datetime import to_excel
            return to_excel(date.today())
        raise CalculationError(f'Fonction non gérée: {f}')

@lru_cache(maxsize=50000)
def parse(formula):
    tokens=[t for t in Tokenizer(formula).items if t.type!='WHITE-SPACE']; i=0
    precedence={'=':1,'<>':1,'<':1,'>':1,'<=':1,'>=':1,'&':2,'+':3,'-':3,'*':4,'/':4,'^':5}
    def expr(p=0):
        nonlocal i
        t=tokens[i]; i+=1
        if t.type=='OPERATOR-PREFIX': n=('unary',t.value,expr(6))
        elif t.type=='OPERAND':
            if t.subtype=='RANGE': n=('ref',t.value)
            elif t.subtype=='NUMBER': n=('literal',float(t.value))
            elif t.subtype=='TEXT': n=('literal',t.value[1:-1].replace('""','"'))
            elif t.subtype=='LOGICAL': n=('literal',t.value=='TRUE')
            else: raise CalculationError(f'Opérande non géré: {t.value}')
        elif t.type=='PAREN' and t.subtype=='OPEN': n=expr(); i+=1
        elif t.type=='FUNC' and t.subtype=='OPEN':
            args=[]
            if tokens[i].subtype!='CLOSE':
                while True:
                    args.append(expr())
                    if tokens[i].type!='SEP': break
                    i+=1
            i+=1; n=('func',t.value[:-1].upper(),tuple(args))
        else: raise CalculationError(f'Jeton non géré: {t.value}')
        while i<len(tokens):
            t=tokens[i]
            if t.type=='OPERATOR-POSTFIX': i+=1; n=('unary',t.value,n); continue
            if t.type!='OPERATOR-INFIX' or precedence.get(t.value,-1)<p: break
            i+=1; n=('op',t.value,n,expr(precedence[t.value]+1))
        return n
    result=expr()
    if i!=len(tokens): raise CalculationError(f'Formule non interprétée: {formula}')
    return result
