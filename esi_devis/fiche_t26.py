"""T2-T6 manufacturing sheet. Text overrides are separate from calculation."""
from io import BytesIO
from datetime import date
from pathlib import Path
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.colors import HexColor

def extract_fabrication(read):
    common=[('C13','Contreplaqué'),('C15','Barres'),('F15','Valsem'),('C17','Isolant'),('C19','Fermetures'),('F19','Peinture'),('C21','Poignées'),('F21','Tyvek'),('C23','Skis'),('F23','Option')]
    items=[]
    for i,(col,row) in enumerate((('B',29),('H',29),('B',44),('H',44),('B',60),('H',60)),1):
        a,b,c,d,e='ABCDE' if col=='B' else 'GHIJK'
        dims=[read(x+str(row)) for x in (b,c,d)]
        if not all(v>0 for v in dims):continue
        cal=read(b+str(row+1)); fields=[]
        def add(key,label,value):fields.append((f'o{i}_{key}',label,value))
        add('calage','Type de calage',cal)
        if cal in ('Bandes de mousse','Mousse à ras','Sous verre / bandes de mousse'):
            add('dessous','Dessous (cm)',read(b+str(row+4)));add('dessus','Dessus (cm)',read(b+str(row+3)))
        for off,label,key in [(7,'Remplissage','remplissage')]:
            vs=[read(x+str(row+off)) for x in (b,c,d)]
            add(key,label,' × '.join(map(str,vs)) if all(isinstance(v,(float,int)) and v>0 for v in vs) else '')
        add('bf','Bandes de fond','')
        add('base','Mousse de base',read(a+str(row+5)));add('qte','Qté',read(e+str(row+5)));add('dim','Dim. (cm)',read(b+str(row+5)))
        items.append(dict(numero=i,dimensions=dims,champs=fields))
    cuvettes=[]
    for n,r in [(1,55),(2,71)]:
        ds=[read(c+str(r)) for c in 'HIJ']
        if all(v>0 for v in ds):cuvettes.append([n,ds])
    return dict(commun=[(k,l,read(k)) for k,l in common],oeuvres=items,cuvettes=cuvettes)

def generate_pdf(data,result,info,adjustments):
    f=result['fabrication_t26']; allowed={k for k,_,_ in f['commun']}|{k for o in f['oeuvres'] for k,_,_ in o['champs']}
    if not isinstance(adjustments,dict) or any(k not in allowed or not isinstance(v,str) or len(v)>160 for k,v in adjustments.items()):raise ValueError('Textes invalides')
    def fmt(v):return f'{v:.3f}'.rstrip('0').rstrip('.').replace('.',',') if isinstance(v,(int,float)) else str(v or '')
    def val(k,v):return adjustments.get(k,fmt(v))
    b=BytesIO(); W,H=landscape(A4); c=canvas.Canvas(b,pagesize=(W,H)); c.setTitle('ESI - Fiche de caisse TABLEAUX')
    blue='#075985'; pale='#e0f2fe'; line='#cbd5e1'; ink='#0f172a'; x=20; w=W-40
    def rect(x,y,w,h,fill='#ffffff'):
        c.setFillColor(HexColor(fill));c.setStrokeColor(HexColor(line));c.rect(x,H-y-h,w,h,fill=1,stroke=1)
    def txt(x,y,v,size=9,bold=False,color=ink,maxw=780):
        v=fmt(v).replace('\n',' ').replace('\r',' ');font='Helvetica-Bold' if bold else 'Helvetica'; sw=c.stringWidth(v,font,size)
        if sw>maxw:size=max(7,size*maxw/sw)
        if c.stringWidth(v,font,size)>maxw:raise ValueError('Texte trop long : '+v[:30])
        c.setFillColor(HexColor(color));c.setFont(font,size);c.drawString(x,H-y,v)
    def bar(x,y,w,t):
        rect(x,y,w,18,blue);txt(x+7,y+12,t,9,True,'#ffffff',w-14)
    def field(x,y,w,h,l,v):
        rect(x,y,w,h);txt(x+5,y+10,l,7.5,False,'#475569',w-10);txt(x+5,y+h-5,v,9,True,ink,w-10)
    def head():
        rect(x,16,w,42,pale);c.drawImage(str(Path(__file__).with_name('logo.png')),28,H-54,width=80,height=32,preserveAspectRatio=True,mask='auto');txt(125,42,'FICHE DE CAISSE - TABLEAUX',19,True,blue);txt(680,31,'Version 01',9,True);txt(680,45,date.today().strftime('%d/%m/%Y'),8)
    head(); bar(x,65,w,'DIMENSIONS DE LA CAISSE ET FABRICATION')
    dw=230; fw=(w-dw-8)/5
    for i,k in enumerate(('interieures','exterieures')):
        ds=[result['dimensions_'+k+'_'+a] for a in ('longueur','epaisseur','hauteur')];field(x,83+i*28,dw,28,('Intérieur' if i==0 else 'Extérieur')+' — L × Ep × H (cm)',' × '.join(map(fmt,ds)))
    for i,(k,l,v) in enumerate(f['commun']):field(x+dw+8+(i%5)*fw,83+(i//5)*28,fw,28,l,val(k,v))
    y=144
    if f['cuvettes']:
        for n,ds in f['cuvettes']:txt(x,y+10,'Cuvette '+str(n)+' — dimensions intérieures L × Ep × H : '+' × '.join(map(fmt,ds))+' cm',9,True);y+=13
    bar(x,y,w,'ŒUVRES ET CALAGES INDIVIDUELS'); y+=22
    # Six cards, two columns, three rows, matching the historical sheet.
    cw=(w-8)/2; ch=78
    for i,o in enumerate(f['oeuvres']):
        xx=x+(i%2)*(cw+8);yy=y+(i//2)*(ch+5);rect(xx,yy,cw,ch)
        rect(xx,yy,cw,16,pale);txt(xx+6,yy+11,'TABLEAU '+str(o['numero']),9,True,blue)
        ids=info.get('oeuvres',{}).get(str(o['numero']),{})
        txt(xx+80,yy+11,'N° ESI : '+str(ids.get('esi','')),8,maxw=105);txt(xx+190,yy+11,'Inventaire : '+str(ids.get('inventaire','')),8,maxw=cw-196)
        txt(xx+6,yy+25,'L × Ep × H (cm) : '+' × '.join(map(fmt,o['dimensions'])),9,True)
        fields={k.split('_',1)[1]:val(k,v) for k,l,v in o['champs']}
        txt(xx+6,yy+36,'Calage : '+fields['calage'],9,True,blue,cw-12)
        details='   '.join(l+': '+fields[k] for k,l in [('dessous','Dessous'),('dessus','Dessus'),('remplissage','Remplissage'),('bf','BF')] if fields.get(k))
        txt(xx+6,yy+47,details,8,maxw=cw-12)
        rect(xx,yy+51,cw,27);txt(xx+6,yy+61,'Mousse de base',8,False,'#475569');txt(xx+6,yy+73,fields['base'],10,True,blue,cw*.52);txt(xx+cw*.57,yy+61,'Qté',8);txt(xx+cw*.57,yy+73,fields['qte'],10,True,blue,cw*.17);txt(xx+cw*.77,yy+61,'Dim. (cm)',8);txt(xx+cw*.77,yy+73,fields['dim'],10,True,blue,cw*.21)
    y+=((len(f['oeuvres'])+1)//2)*(ch+5)
    # Keep footer fixed on the single page, or move it to a second page if necessary.
    if y+135>H-12:c.showPage();head();y=68
    bar(x,y,w,'IDENTIFICATION DU DOSSIER ET DONNÉES LOGISTIQUES — MARQUAGE FRANÇAIS');y+=18
    l,e,h=[result['dimensions_exterieures_'+a] for a in ('longueur','epaisseur','hauteur')]
    vals=[('Dossier',info.get('dossier','')),('N° caisse',info.get('numero','')),('Exposition',info.get('exposition','')),('Prêteur',info.get('preteur','')),('Chargé de projet',info.get('charge','')),('Poids / Volume / Surface',fmt(result['poids_caisse'])+' kg / '+fmt(l*e*h/1e6)+' m³ / '+fmt(l*e/1e4)+' m²')]
    for i,(lab,v) in enumerate(vals):field(x+(i%3)*w/3,y+(i//3)*25,w/3,25,lab,v)
    y+=50;field(x,y,w,23,'Observations fabrication',info.get('observations',''));y+=25
    bar(x,y,w,'RÉSERVÉ À LA SOCIÉTÉ ESI - CONTRÔLE QUALITÉ');y+=18
    for i,lab in enumerate(['Poids à vide','Poids total','Contrôleur','Durée','Débit fournitures','Caisse conforme','Observations']):field(x+i*w/7,y,w/7,24,lab,'OUI / NON' if i in (4,5) else '....................')
    c.showPage();c.save();return b.getvalue()
