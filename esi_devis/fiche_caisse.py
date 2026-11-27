"""T1 manufacturing sheet, derived exclusively from the 2026 calculation."""
from io import BytesIO
from pathlib import Path
from datetime import date
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.colors import HexColor, white
from reportlab.lib.utils import ImageReader

ROOT = Path(__file__).parent
BLUE = '#075985'
INK = '#0f172a'
PALE = '#e0f2fe'
LINE = '#cbd5e1'

def generate_pdf(data, result, info):
    f = result['fabrication_t1']
    b = BytesIO(); c = canvas.Canvas(b, pagesize=landscape(A4)); W,H = landscape(A4)
    c.setTitle('ESI - Fiche de caisse TABLEAUX')
    def rect(x,y,w,h,fill,stroke=LINE):
        c.setFillColor(HexColor(fill)); c.setStrokeColor(HexColor(stroke)); c.rect(x,H-y-h,w,h,fill=1,stroke=1)
    def txt(x,y,v,size=10,bold=False,color=INK,maxw=None):
        v=str(v if v is not None else '').replace('\n',' ').replace('\r',' ')
        font='Helvetica-Bold' if bold else 'Helvetica'
        if maxw and c.stringWidth(v,font,size)>maxw:
            size=max(7,size*maxw/c.stringWidth(v,font,size))
        if maxw and c.stringWidth(v,font,size)>maxw:
            raise ValueError('Texte trop long : '+v[:35])
        c.setFont(font,size); c.setFillColor(HexColor(color)); c.drawString(x,H-y,v)
    def header(x,y,w,title):
        rect(x,y,w,20,BLUE,BLUE);txt(x+8,y+14,title,10,True,'#ffffff')
    def field(x,y,label,value,w):
        txt(x,y,label,9,False,'#475569',w*.43);txt(x+w*.45,y,value,10,True,maxw=w*.54)
    def tile(x,y,w,h,label,value):
        rect(x,y,w,h,'#f8fafc')
        txt(x+8,y+12,label,8,False,'#475569',w-16)
        txt(x+8,y+h-7,value,10,True,maxw=w-16)
    def num(v):return f'{float(v):.3f}'.rstrip('0').rstrip('.').replace('.',',')
    def val(k,default=''):
        v=info.get(k,default)
        if not isinstance(v,(str,int,float)) or len(str(v))>160:raise ValueError('Champ invalide : '+k)
        return str(v)
    rect(20,18,W-40,48,PALE)
    c.drawImage(str(ROOT/'logo.png'),28,H-60,width=90,height=37,preserveAspectRatio=True,mask='auto')
    txt(140,49,'FICHE DE CAISSE - TABLEAUX',20,True,BLUE)
    txt(655,38,'Version 06',10,True);txt(655,53,date.today().strftime('%d/%m/%Y'),9)
    x=20; right=490; lw=460; rw=W-right-20
    header(x,76,lw,'DIMENSIONS - cm (sans arrondi au cm)' )
    for i,label in enumerate(['Longueur','Épaisseur','Hauteur']):txt(190+i*90,110,label,10,True)
    rows=[('Œuvre',[f[a] for a in ('C29','D29','E29')])]+[(title,[result['dimensions_'+k+'_'+a] for a in ('longueur','epaisseur','hauteur')]) for title,k in [('Intérieur','interieures'),('Extérieur','exterieures')]]
    for i,(title,ds) in enumerate(rows):
        y=117+i*22;rect(x,y,lw,22,PALE if i==2 else '#ffffff');txt(x+8,y+15,title,11,True)
        for j,v in enumerate(ds):txt(190+j*90,y+15,num(v),11,True)
    header(right,76,rw,'CARACTÉRISTIQUES DE FABRICATION')
    for i,(label,a) in enumerate([('Contreplaqué','C13'),('Barres','C15'),('Valsem','F15'),('Isolant','C17'),('Fermetures','C19'),('Peinture','F19'),('Poignées','C21'),('Tyvek','F21'),('Skis','C23'),('Option','F23')]):
        y=96+i*17;rect(right,y,rw,17,'#f8fafc' if i%2==0 else '#ffffff');field(right+7,y+12,label,f[a],rw-14)
    rect(x,183,lw,24,'#f8fafc')
    field(x+8,199,'Inventaire de l’œuvre',val('inventaire'),lw-16)
    header(x,216,lw,'CALAGE INTÉRIEUR')
    # Two matching white cards; retain the historical conditional fields.
    def positive(v):return isinstance(v,(int,float)) and v>0
    r=[f.get(k) for k in ('C41','D41','E41')]
    details=[]
    if f['B34'] in ('Sous verre 2 faces','Sous verre 2 faces ESI') and all(positive(v) for v in r):
        details.append('R : '+' x '.join(num(v) for v in r)+' cm')
    if positive(f.get('E39')) and positive(f.get('C39')):
        details.append('BF : '+num(f['C39'])+' x '+num(f['E39'])+' cm')
    show_dd=f['B34'] in ('Bandes de mousse','Sous verre / bandes de mousse','Mousse à ras')
    y=236
    h=46+(22 if show_dd else 0)+(15 if f['B34']=='Mousse à ras' else 0)+15*len(details)
    rect(x,y,lw,h,'#ffffff')
    txt(x+10,y+15,'Type de calage',9,False,'#475569')
    txt(x+10,y+33,f['B34'],12,True,BLUE,maxw=lw-20)
    yy=y+49
    if show_dd:
        field(x+10,yy,'Dessous',num(f['C37'])+' cm',210)
        field(x+240,yy,'Dessus',num(f['C36'])+' cm',210)
        yy+=22
    if f['B34']=='Mousse à ras':
        txt(x+10,yy,'À ras de',9);yy+=15
    for detail in details:
        txt(x+10,yy,detail,10,maxw=lw-20);yy+=15
    y+=h+6
    rect(x,y,lw,46,'#ffffff')
    for offset,label in [(0,'Mousse de base'),(280,'Qté'),(360,'Dim. (cm)')]:
        txt(x+10+offset,y+15,label,9,False,'#475569')
    txt(x+10,y+33,'PU' if f['F23']=='A plat + Chapeau' else f['B38'],12,True,BLUE,maxw=260)
    if f['F23']!='A plat + Chapeau':
        txt(x+290,y+33,num(f['F38']),12,True,BLUE)
        txt(x+370,y+33,num(f['C38']),12,True,BLUE)
    header(right,274,rw,'DONNÉES LOGISTIQUES')
    l,e,h=rows[-1][1]
    for i,(label,v) in enumerate([('Poids estimatif',num(result['poids_caisse'])+' kg'),('Volume',num(l*e*h/1e6)+' m³'),('Surface au sol',num(l*e/1e4)+' m²'),('Marquage','Français')]):
        y=294+i*23;rect(right,y,rw,23,'#f8fafc' if i%2==0 else '#ffffff');field(right+7,y+15,label,v,rw-14)
    # Historical identification block at bottom of the technical sheet.
    header(x,396,W-40,'IDENTIFICATION DU DOSSIER')
    width=(W-40-12)/3
    for i,(label,v) in enumerate([('Dossier',val('dossier',data.get('dossier',''))),('N° caisse',val('numero')),('Exposition',val('exposition')),('Prêteur',val('preteur')),('Chargé de projet',val('charge'))]):
        col=i%3; row=i//3
        xx=x+col*(width+6); yy=416+row*24
        rect(xx,yy,width,24,'#f8fafc');field(xx+7,yy+15,label,v,width-14)
    rect(x,469,W-40,24,'#ffffff')
    txt(x+7,484,'Observations fabrication :',9,False,'#475569')
    txt(x+130,484,val('observations'),9,True,maxw=W-177)
    header(x,500,W-40,'RÉSERVÉ À LA SOCIÉTÉ ESI - CONTRÔLE QUALITÉ')
    qw=(W-40-12)/4
    for i,(label,v) in enumerate([('Poids à vide','........................ kg'),('Poids total','........................ kg'),('Contrôleur','........................'),('Durée','........................')]):
        xx=x+i*(qw+4); rect(xx,520,qw,23,'#ffffff');field(xx+6,535,label,v,qw-12)
    widths=[190,190,W-40-388]
    xx=x
    for w,label,v in zip(widths,['Débit fournitures','Caisse conforme','Observations'],['OUI / NON','OUI / NON','........................................']):
        rect(xx,547,w,25,'#ffffff');field(xx+6,563,label,v,w-12);xx+=w+4
    c.showPage();c.save();return b.getvalue()
