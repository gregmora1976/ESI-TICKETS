"""Standalone ESI Tickets security demonstration. NEVER connects to Supabase.
Use only on the test Render service: gunicorn esi_demo:app --bind 0.0.0.0:10000
Data is temporary, stored in /tmp, and resets on redeploy/restart.
"""
import os
import re
import sqlite3
import secrets
import time
import hmac
import threading
from datetime import timedelta
from functools import wraps
from pathlib import Path
from flask import Flask, request, redirect, url_for, session, render_template_string, abort, flash, g
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
secret = os.environ.get('SECRET_KEY', '')
if len(secret) < 32:
    raise RuntimeError('Set SECRET_KEY to a securely generated value of 32+ characters')
app.secret_key = secret
app.config.update(SESSION_COOKIE_SECURE=True, SESSION_COOKIE_HTTPONLY=True,
                  SESSION_COOKIE_SAMESITE='Lax', PERMANENT_SESSION_LIFETIME=timedelta(hours=4),
                  MAX_CONTENT_LENGTH=100_000)
ROLES = ('DEMANDEUR', 'GESTIONNAIRE', 'ADMIN')
DEMO_EMAILS = {'DEMANDEUR':'demandeur@demo.invalid','GESTIONNAIRE':'gestionnaire@demo.invalid','ADMIN':'admin@demo.invalid'}
PASSWORDS = {role:os.environ.get('ESI_DEMO_PASSWORD_'+role, '') for role in ROLES}
if any(len(p)<14 for p in PASSWORDS.values()) or len(set(PASSWORDS.values()))<3:
    raise RuntimeError('Set three distinct ESI_DEMO_PASSWORD_* environment variables, each at least 14 characters')
DB_PATH = Path('/tmp/esi_tickets_security_demo.sqlite3')
_DB_LOCK = threading.RLock()
TRIES = {}
BASE = '''<!doctype html><html lang="fr"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{{title}} — ESI TICKETS DÉMO</title><style>
:root{--blue:#0284c7;--ink:#15334e;--bg:#eef5fa}*{box-sizing:border-box}body{font-family:Arial,Helvetica,sans-serif;color:var(--ink);background:var(--bg);margin:0}header{background:#0f3153;color:white;padding:16px max(18px,calc((100vw - 1050px)/2));display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}header strong{font-size:21px}a{color:#0369a1}header a{color:white}main{max-width:1050px;margin:24px auto;padding:0 16px}.panel{background:white;padding:23px;border-radius:17px;border:1px solid #d8e5ef;margin:15px 0;box-shadow:0 5px 25px #0f31530c}.narrow{max-width:510px;margin:35px auto}h1{font-size:26px;margin:0 0 12px}h2{font-size:19px}p{line-height:1.5}.muted{color:#64748b;font-size:13px}label{font-weight:700;display:block;margin:10px 0 5px}input,select,textarea{font:inherit;border:1px solid #bdcedb;border-radius:10px;padding:10px;width:100%;background:#f8fcff}button,.button{display:inline-block;border:0;border-radius:9px;background:#0284c7;color:white;padding:11px 14px;font-weight:bold;cursor:pointer;text-decoration:none;margin-top:9px}button.secondary,.button.secondary{background:#e0f2fe;color:#075985}.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(235px,1fr));gap:12px}.box{border:1px solid #dbe5ee;background:#f7fbfd;border-radius:12px;padding:15px}.tag{font-size:12px;background:#e0f2fe;border-radius:30px;padding:5px 9px;color:#075985}.warning{padding:10px 14px;background:#fff4d3;border:1px solid #e9cb72;border-radius:9px;margin-bottom:15px}.error{background:#fee2e2;color:#991b1b;padding:10px;border-radius:9px}.ok{background:#dcfce7;color:#166534;padding:10px;border-radius:9px}table{border-collapse:collapse;width:100%}td,th{padding:10px;text-align:left;border-bottom:1px solid #e2e8f0;font-size:13px}th{background:#f0f9ff}.disabled{color:#8b9eaf}.actions{display:flex;gap:10px;flex-wrap:wrap}.actions form{display:inline}form.inline{display:inline}footer{max-width:1050px;margin:20px auto;padding:15px;color:#64748b;font-size:12px}@media(max-width:650px){header{padding:14px}main{margin:10px auto}.panel{padding:15px}}</style><header><strong>ESI TICKETS <span style="font-size:13px;font-weight:400">· DÉMONSTRATION</span></strong><div>{% if user %}{{user['email']}} · {{user['profil']}} · <a href="/deconnexion">Déconnexion</a>{% else %}Aucune connexion Supabase{% endif %}</div></header><main><div class="warning"><strong>Environnement fictif :</strong> aucun accès à Supabase, aucun ticket ou utilisateur réel. Les modifications sont temporaires.</div>{% for category,message in get_flashed_messages(with_categories=true) %}<div class="{{'error' if category=='error' else 'ok'}}">{{message}}</div>{% endfor %}{{body|safe}}</main><footer>ESI TICKETS — démonstration isolée · Les données temporaires sont effacées lors d'un redéploiement/réinitialisation de l'instance.</footer></html>'''

def connect():
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with _DB_LOCK:
        with connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS users (email TEXT PRIMARY KEY, profil TEXT NOT NULL, active INTEGER NOT NULL, hash TEXT NOT NULL, must_change INTEGER NOT NULL, version INTEGER NOT NULL DEFAULT 1)')
            db.execute('CREATE TABLE IF NOT EXISTS tickets (id INTEGER PRIMARY KEY AUTOINCREMENT, titre TEXT NOT NULL, creator TEXT NOT NULL, status TEXT NOT NULL DEFAULT "Créé")')
            db.execute('CREATE TABLE IF NOT EXISTS user_requests (id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT NOT NULL, kind TEXT NOT NULL, message TEXT NOT NULL DEFAULT "", state TEXT NOT NULL DEFAULT "EN_ATTENTE", created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, handled_by TEXT, handled_at TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS admin_audit (id INTEGER PRIMARY KEY AUTOINCREMENT, actor TEXT NOT NULL, action TEXT NOT NULL, target TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)')
            for role,email in DEMO_EMAILS.items():
                exists = db.execute('SELECT 1 FROM users WHERE email=?',(email,)).fetchone()
                if not exists:
                    db.execute('INSERT INTO users(email,profil,active,hash,must_change) VALUES(?,?,?,?,1)',(email,role,1,generate_password_hash(PASSWORDS[role], method='scrypt')))
            if not db.execute('SELECT 1 FROM tickets LIMIT 1').fetchone():
                db.executemany('INSERT INTO tickets(titre,creator,status) VALUES(?,?,?)',[
                  ('DEV-001 — Devis caisse de transport',DEMO_EMAILS['DEMANDEUR'],'Créé'),
                  ('REC-002 — Réception d’œuvres',DEMO_EMAILS['GESTIONNAIRE'],'En cours'),
                  ('C-003 — Colisage expo fictive',DEMO_EMAILS['DEMANDEUR'],'Créé')])
init_db()

def db_one(sql,args=()):
    with connect() as db: return db.execute(sql,args).fetchone()
def db_all(sql,args=()):
    with connect() as db: return db.execute(sql,args).fetchall()
def db_write(sql,args=()):
    with _DB_LOCK:
        with connect() as db:
            return db.execute(sql,args).rowcount

def view(title,body,**context):
    return render_template_string(BASE,title=title,body=render_template_string(body,**context),user=getattr(g,'user',None))
def csrf():
    if not session.get('csrf'): session['csrf']=secrets.token_urlsafe(32)
    return session['csrf']
def check_csrf():
    if not hmac.compare_digest(request.form.get('csrf',''),session.get('csrf','')): abort(403)
def safe_user():
    email=session.get('uid')
    if not email:return None
    user=db_one('SELECT email,profil,active,must_change,version FROM users WHERE email=?',(email,))
    if not user or not user['active'] or user['version']!=session.get('version'):
        session.clear();return None
    return user

@app.before_request
def gate():
    g.user=safe_user()
    if request.path in ('/connexion','/sante','/demande-acces','/activer-compte') : return
    if not g.user:return redirect('/connexion')
    if g.user['must_change'] and request.path not in ('/changer-mot-de-passe','/deconnexion'):
        return redirect('/changer-mot-de-passe')
    if request.method=='POST':
        check_csrf()
        origin=request.headers.get('Origin')
        from urllib.parse import urlsplit
        if origin and urlsplit(origin).netloc!=request.host:abort(403)

def role_allowed(*allowed):
    def decorate(fn):
        @wraps(fn)
        def wrapper(*a,**kw):
            if not g.user or g.user['profil'] not in allowed:abort(403)
            return fn(*a,**kw)
        return wrapper
    return decorate

@app.route('/sante')
def health():return {'demo':True,'supabase':False,'ok':True}
@app.route('/')
def home():return redirect('/demandeur')

@app.route('/connexion',methods=['GET','POST'])
def login():
    if g.user:return redirect('/demandeur')
    error=''
    if request.method=='POST':
        key=(request.remote_addr or '-') + ':' + str(request.form.get('email','')).lower()
        now=time.monotonic()
        tries=[x for x in TRIES.get(key,[]) if now-x<900]
        if len(tries)>=8:
            error='Trop de tentatives : réessaie plus tard.'
        else:
            tries.append(now);TRIES[key]=tries
            email=request.form.get('email','').strip().lower()
            user=db_one('SELECT * FROM users WHERE email=?',(email,))
            if user and user['active'] and check_password_hash(user['hash'],request.form.get('password','')):
                TRIES.pop(key,None); session.clear();session.permanent=True
                session['uid']=email;session['version']=user['version'];csrf()
                return redirect('/changer-mot-de-passe' if user['must_change'] else '/demandeur')
            error='Identifiant ou mot de passe incorrect.'
    return view('Connexion','''<div class="panel narrow"><h1>Connexion individuelle</h1><p class="muted">Les trois comptes de démonstration sont indépendants de Microsoft 365 et de Supabase.</p>{% if error %}<p class="error">{{error}}</p>{% endif %}<form method="post"><label>Adresse e-mail</label><input name="email" type="email" required autocomplete="username" placeholder="demandeur@demo.invalid"><label>Mot de passe provisoire</label><input name="password" type="password" required autocomplete="current-password"><button>SE CONNECTER</button></form><p><a href="/demande-acces">Demander un compte ou une réinitialisation</a> · <a href="/activer-compte">Première connexion</a></p><p class="muted">Comptes : demandeur@demo.invalid · gestionnaire@demo.invalid · admin@demo.invalid</p></div>''',error=error)

@app.route('/changer-mot-de-passe',methods=['GET','POST'])
def change_password():
    err=''
    if request.method=='POST':
        old=request.form.get('old','');new=request.form.get('new','');confirm=request.form.get('confirm','')
        row=db_one('SELECT * FROM users WHERE email=?',(g.user['email'],))
        if not check_password_hash(row['hash'],old):err='Mot de passe actuel incorrect.'
        elif len(new)<14 or len(new)>128:err='Choisir entre 14 et 128 caractères.'
        elif new!=confirm:err='Les nouveaux mots de passe ne correspondent pas.'
        elif hmac.compare_digest(new,old):err='Le nouveau mot de passe doit être différent.'
        else:
            db_write('UPDATE users SET hash=?,must_change=0,version=version+1 WHERE email=?',(generate_password_hash(new,method='scrypt'),g.user['email']))
            session['version']=db_one('SELECT version FROM users WHERE email=?',(g.user['email'],))['version']
            flash('Mot de passe modifié pour ce compte fictif.','ok')
            return redirect('/demandeur')
    return view('Changer le mot de passe','''<div class="panel narrow"><h1>Changer mon mot de passe</h1><p>Changement obligatoire lors de la première connexion.</p>{% if err %}<p class="error">{{err}}</p>{% endif %}<form method="post"><input type="hidden" name="csrf" value="{{csrf}}"><label>Mot de passe actuel</label><input name="old" type="password" required><label>Nouveau mot de passe (14 caractères minimum)</label><input name="new" type="password" minlength="14" required><label>Confirmer le nouveau mot de passe</label><input name="confirm" type="password" minlength="14" required><button>Enregistrer</button></form></div>''',csrf=csrf(),err=err)

@app.route('/deconnexion')
def logout():session.clear();return redirect('/connexion')

@app.route('/demandeur')
def demandeur():
    tickets=db_all('SELECT * FROM tickets ORDER BY id')
    return view('Demandeur','''<div class="panel"><h1>DEMANDE DE TICKETS</h1><p>Profil connecté : <strong>{{role}}</strong> · Toutes les demandes sont visibles, mais les modifications sont limitées selon le profil.</p><div class="grid"><div class="box"><h2>Créer une demande</h2><form method="post" action="/tickets/ajouter"><input type="hidden" name="csrf" value="{{csrf}}"><label>Objet du ticket fictif</label><input name="titre" maxlength="120" required placeholder="Ex. Nouvelle demande de devis"><button>Créer le ticket</button></form></div><div class="box"><h2>Accès aux espaces</h2><div class="actions"><a class="button secondary" href="/gestionnaire">GESTIONNAIRE</a><a class="button secondary" href="/reception">RÉCEPTION</a><a class="button secondary" href="/reporting">REPORTING</a>{% if role=='ADMIN' %}<a class="button" style="background:#b91c1c" href="/administrateur">ADMINISTRATEUR</a>{% endif %}</div><p class="muted">Les accès interdits retournent HTTP 403, même en saisissant l'adresse directement.</p></div></div></div><div class="panel"><h2>Tickets fictifs</h2><table><tr><th>N°</th><th>Objet</th><th>Statut</th><th>Créateur</th><th>Modification</th></tr>{% for t in tickets %}<tr><td>{{t.id}}</td><td>{{t.titre}}</td><td>{{t.status}}</td><td>{{t.creator}}</td><td>{% if role!='DEMANDEUR' %}<form class="inline" method="post" action="/tickets/{{t.id}}/terminer"><input type="hidden" name="csrf" value="{{csrf}}"><button class="secondary">Terminer</button></form>{% else %}<span class="disabled">Lecture seule</span>{% endif %}</td></tr>{% endfor %}</table></div>''',role=g.user['profil'],email=g.user['email'],csrf=csrf(),tickets=tickets)

@app.route('/tickets/ajouter',methods=['POST'])
def add_ticket():
    title=request.form.get('titre','').strip()
    if not (2<=len(title)<=120):abort(400)
    db_write('INSERT INTO tickets(titre,creator) VALUES(?,?)',(title,g.user['email']))
    flash('Ticket de démonstration créé.','ok')
    return redirect('/demandeur')

@app.route('/tickets/<int:ticket_id>/terminer',methods=['POST'])
def end_ticket(ticket_id):
    ticket=db_one('SELECT * FROM tickets WHERE id=?',(ticket_id,))
    if not ticket:abort(404)
    if g.user['profil']=='DEMANDEUR':abort(403)
    db_write('UPDATE tickets SET status=? WHERE id=?',('Terminé',ticket_id))
    flash('Ticket fictif mis à jour.','ok')
    return redirect('/demandeur')

@app.route('/gestionnaire')
@role_allowed('GESTIONNAIRE','ADMIN')
def manager():
    return view('Gestionnaire','''<div class="panel"><h1>GESTIONNAIRE</h1><p>Accès autorisé : gestion de tous les tickets, réception, colisage et liens entre Articles et Packings.</p><div class="grid"><div class="box">Tickets · Lecture et modification</div><div class="box">Articles · Gestion des liens</div><div class="box">COLISAGE · Validation logistique</div></div><p class="muted">Cette page simule les droits ; aucune opération réelle n'est effectuée.</p><a href="/demandeur">Retour</a></div>''')

@app.route('/reception')
def reception():
    return view('Planning réception','''<div class="panel"><h1>PLANNING RÉCEPTION</h1><p>Consultation autorisée pour les trois profils.</p>{% if can_validate %}<div class="ok">Validation des réceptions et sorties : autorisée pour votre profil.</div>{% else %}<div class="warning">Validation des réceptions et sorties : refusée pour DEMANDEUR.</div>{% endif %}<form method="post" action="/reception/valider"><input type="hidden" name="csrf" value="{{csrf}}"><button>Tester la validation fictive</button></form><p><a href="/demandeur">Retour</a></p></div>''',can_validate=g.user['profil']!='DEMANDEUR',csrf=csrf())

@app.route('/reception/valider',methods=['POST'])
@role_allowed('GESTIONNAIRE','ADMIN')
def validate_reception():flash('Validation fictive autorisée (aucune base modifiée).','ok');return redirect('/reception')

@app.route('/reporting')
@role_allowed('ADMIN')
def reporting():return view('Reporting','''<div class="panel"><h1>REPORTING</h1><p>Accès réservé aux Administrateurs conformément à la matrice de droits.</p><p><a href="/demandeur">Retour</a></p></div>''')

@app.route('/admin/utilisateurs')
@role_allowed('ADMIN')
def admin_users():
    rows=db_all('SELECT email,profil,active,must_change FROM users ORDER BY profil,email')
    return view('Administration utilisateurs','''<div class="panel"><h1>ADMIN → UTILISATEURS</h1><p>Gestion fictive des utilisateurs. Aucune modification Supabase.</p><table><tr><th>Compte</th><th>Profil</th><th>Actif</th><th>Changement obligatoire</th><th>Action</th></tr>{% for u in users %}<tr><td>{{u.email}}</td><td>{{u.profil}}</td><td>{{'Oui' if u.active else 'Non'}}</td><td>{{'Oui' if u.must_change else 'Non'}}</td><td>{% if u.email != admin_email %}<form method="post" action="/admin/utilisateurs/modifier"><input type="hidden" name="csrf" value="{{csrf}}"><input type="hidden" name="email" value="{{u.email}}"><select name="profil"><option {{'selected' if u.profil=='DEMANDEUR' else ''}}>DEMANDEUR</option><option {{'selected' if u.profil=='GESTIONNAIRE' else ''}}>GESTIONNAIRE</option><option {{'selected' if u.profil=='ADMIN' else ''}}>ADMIN</option></select><select name="active"><option value="1" {{'selected' if u.active else ''}}>Actif</option><option value="0" {{'selected' if not u.active else ''}}>Désactivé</option></select><button>Enregistrer</button></form>{% else %}Compte administrateur principal{% endif %}</td></tr>{% endfor %}</table><p class="muted">Les changements de rôle et la désactivation sont réellement contrôlés dans cette démonstration temporaire.</p><a href="/demandeur">Retour</a></div>''',users=rows,csrf=csrf(),admin_email=DEMO_EMAILS['ADMIN'])

@app.route('/admin/utilisateurs/modifier',methods=['POST'])
@role_allowed('ADMIN')
def admin_modify():
    email=request.form.get('email','').strip().lower();role=request.form.get('profil','');active=request.form.get('active')
    if email==DEMO_EMAILS['ADMIN'] or role not in ROLES or active not in ('0','1'):abort(400)
    if email not in DEMO_EMAILS.values():abort(400)
    db_write('UPDATE users SET profil=?,active=?,version=version+1 WHERE email=?',(role,int(active),email))
    flash('Compte fictif modifié. Les sessions précédentes sont invalidées.','ok')
    return redirect('/admin/utilisateurs')

@app.errorhandler(403)
def forbidden(e):return view('Accès interdit','''<div class="panel narrow"><h1>403 — Accès interdit</h1><p>Ton profil ne dispose pas de cette autorisation.</p><p><a href="/demandeur">Revenir à l'accueil</a></p></div>'''),403


# -------- Administration de démonstration (sans Supabase) --------
EMAIL_RE = re.compile(r'^[^@\s]{1,64}@[^@\s]{1,190}\.[^@\s]{2,}$')

def demo_audit(action,target):
    db_write('INSERT INTO admin_audit(actor,action,target) VALUES(?,?,?)',
             (g.user['email'],action,target))

@app.route('/demande-acces',methods=['GET','POST'])
def request_access():
    msg=''
    if request.method=='POST':
        email=request.form.get('email','').strip().lower()
        detail=request.form.get('message','').strip()[:400]
        # Même message de confirmation, pour ne pas divulguer les comptes existants.
        if EMAIL_RE.fullmatch(email) and len(email)<=254:
            now=time.monotonic()
            key='request:'+str(request.remote_addr)
            recent=[t for t in TRIES.get(key,[]) if now-t<3600]
            if len(recent)<5:
                with _DB_LOCK:
                    with connect() as db:
                        user=db.execute('SELECT email FROM users WHERE email=?',(email,)).fetchone()
                        kind='REINITIALISATION' if user else 'CREATION'
                        existing=db.execute("SELECT id FROM user_requests WHERE email=? AND kind=? AND state='EN_ATTENTE'",(email,kind)).fetchone()
                        if not existing:
                            db.execute('INSERT INTO user_requests(email,kind,message) VALUES(?,?,?)',(email,kind,detail))
                recent.append(now);TRIES[key]=recent
            msg='Si votre demande peut être traitée, elle apparaîtra dans la rubrique Administrateur.'
        else:
            msg='Vérifiez le format de votre adresse e-mail.'
    return view('Demande d’accès','''<div class="panel narrow"><h1>Demande d’accès à ESI TICKETS</h1>
      <p>Première connexion ou accès perdu ? Un administrateur examinera votre demande.</p>
      {% if msg %}<p class="ok">{{msg}}</p>{% endif %}
      <form method="post"><label>Adresse e-mail professionnelle</label><input type="email" name="email" required maxlength="254">
      <label>Commentaire (facultatif)</label><textarea name="message" maxlength="400" rows="3" placeholder="Expliquez le problème rencontré"></textarea>
      <button>ENVOYER LA DEMANDE</button></form>
      <p class="muted">Aucun compte n'est créé automatiquement. Ce formulaire n'envoie pas d'e-mail.</p>
      <p><a href="/connexion">Retour à la connexion</a></p></div>''',msg=msg)

@app.route('/activer-compte',methods=['GET','POST'])
def activate_user():
    email=request.values.get('email','').strip().lower()
    message=''
    if request.method=='POST':
        password=request.form.get('password','')
        confirm=request.form.get('confirm','')
        with _DB_LOCK:
            with connect() as db:
                user=db.execute('SELECT email,active,hash FROM users WHERE email=?',(email,)).fetchone()
                if not user or not user['active'] or user['hash']!='!':
                    message='Activation impossible pour ce compte.'
                elif len(password)<14 or len(password)>128 or password!=confirm:
                    message='Saisissez deux mots de passe identiques de 14 à 128 caractères.'
                else:
                    db.execute('UPDATE users SET hash=?,must_change=0,version=version+1 WHERE email=? AND hash=?',
                      (generate_password_hash(password,method='scrypt'),email,'!'))
                    return redirect('/connexion')
    return view('Activer mon compte','''<div class="panel narrow"><h1>Première connexion</h1>
      <p class="warning">La démonstration ne vérifie pas la propriété de l'adresse e-mail. Ne l'utilisez pas avec de vrais comptes.</p>
      {% if message %}<p class="error">{{message}}</p>{% endif %}
      <form method="post"><label>Adresse e-mail</label><input name="email" type="email" value="{{email}}" required>
      <label>Créer mon mot de passe (14 caractères minimum)</label><input name="password" type="password" minlength="14" required>
      <label>Confirmer</label><input name="confirm" type="password" minlength="14" required>
      <button>ACTIVER MON COMPTE</button></form>
      <a href="/connexion">Retour à la connexion</a></div>''',email=email,message=message)

@app.route('/administrateur')
@role_allowed('ADMIN')
def admin_dashboard():
    users=db_all('SELECT email,profil,active,hash FROM users ORDER BY profil,email')
    requests=db_all("SELECT * FROM user_requests ORDER BY CASE WHEN state='EN_ATTENTE' THEN 0 ELSE 1 END,id DESC LIMIT 100")
    audits=db_all('SELECT * FROM admin_audit ORDER BY id DESC LIMIT 30')
    pending=sum(1 for item in requests if item['state']=='EN_ATTENTE')
    return view('Administrateur','''<div class="panel"><h1>ADMINISTRATEUR</h1>
      <p>Gestion des utilisateurs, demandes et accès · environnement de test uniquement.</p>
      <div class="grid">
      <div class="box"><h2>{{users|length}}</h2><span>Utilisateurs fictifs</span></div>
      <div class="box"><h2>{{pending}}</h2><span>Demandes en attente</span></div>
      <div class="box"><h2>3</h2><span>Profils disponibles</span></div></div></div>
      <div class="panel"><h2>Demandes de création et réinitialisation</h2>
      <table><tr><th>Date</th><th>Adresse</th><th>Type</th><th>Statut</th><th>Action</th></tr>
      {% for r in requests %}<tr><td>{{r.created_at}}</td><td>{{r.email}}</td><td>{{r.kind}}</td>
      <td>{{r.state}}</td><td>{% if r.state=='EN_ATTENTE' %}
      <form class="inline" method="post" action="/administrateur/traiter">
      <input type="hidden" name="csrf" value="{{csrf}}"><input type="hidden" name="id" value="{{r.id}}">
      {% if r.kind=='CREATION' %}<select name="profil">{% for role in roles %}<option>{{role}}</option>{% endfor %}</select>{% endif %}
      <button name="decision" value="ACCEPTEE">Accepter</button>
      <button class="secondary" name="decision" value="REFUSEE">Refuser</button></form>
      {% else %}Traité par {{r.handled_by}}{% endif %}</td></tr>
      {% else %}<tr><td colspan="5">Aucune demande.</td></tr>{% endfor %}</table>
      <p class="muted">Un administrateur doit vérifier l'identité d'un collaborateur avant une réinitialisation.</p></div>
      <div class="panel"><h2>Utilisateurs</h2>
      <table><tr><th>Adresse</th><th>Profil</th><th>État</th><th>Activation</th></tr>
      {% for u in users %}<tr><td>{{u.email}}</td><td>{{u.profil}}</td><td>{{'Actif' if u.active else 'Désactivé'}}</td>
      <td>{{'À activer' if u.hash=='!' else 'Activé'}}</td></tr>{% endfor %}</table>
      <p><a class="button secondary" href="/admin/utilisateurs">Modifier les profils / désactiver</a></p></div>
      <div class="panel"><h2>Historique administrateur</h2><table><tr><th>Date</th><th>Administrateur</th><th>Opération</th><th>Compte</th></tr>
      {% for a in audits %}<tr><td>{{a.created_at}}</td><td>{{a.actor}}</td><td>{{a.action}}</td><td>{{a.target}}</td></tr>
      {% else %}<tr><td colspan="4">Aucun mouvement.</td></tr>{% endfor %}</table></div>
      <p><a href="/demandeur">Retour à l'accueil</a></p>''',users=users,requests=requests,audits=audits,pending=pending,csrf=csrf(),roles=ROLES)

@app.route('/administrateur/traiter',methods=['POST'])
@role_allowed('ADMIN')
def admin_handle_request():
    try: rid=int(request.form.get('id','0'))
    except ValueError:abort(400)
    decision=request.form.get('decision','')
    role=request.form.get('profil','DEMANDEUR')
    if decision not in ('ACCEPTEE','REFUSEE') or role not in ROLES:abort(400)
    with _DB_LOCK:
        with connect() as db:
            req=db.execute("SELECT * FROM user_requests WHERE id=? AND state='EN_ATTENTE'",(rid,)).fetchone()
            if not req:abort(404)
            if decision=='ACCEPTEE':
                if req['kind']=='CREATION':
                    existing=db.execute('SELECT 1 FROM users WHERE email=?',(req['email'],)).fetchone()
                    if existing:abort(409)
                    db.execute('INSERT INTO users(email,profil,active,hash,must_change) VALUES(?,?,1,?,0)',
                               (req['email'],role,'!'))
                elif req['kind']=='REINITIALISATION':
                    target=db.execute('SELECT email FROM users WHERE email=?',(req['email'],)).fetchone()
                    if not target:abort(404)
                    db.execute("UPDATE users SET hash='!',must_change=0,version=version+1 WHERE email=?",(req['email'],))
            db.execute("UPDATE user_requests SET state=?,handled_by=?,handled_at=CURRENT_TIMESTAMP WHERE id=?",
                      (decision,g.user['email'],rid))
            db.execute('INSERT INTO admin_audit(actor,action,target) VALUES(?,?,?)',
                      (g.user['email'],req['kind']+' '+decision,req['email']))
    flash('Demande traitée. Après acceptation, le collaborateur peut utiliser « Première connexion ».','ok')
    return redirect('/administrateur')

if __name__=='__main__':app.run(port=10000)
)

def demo_audit(action,target):
    db_write('INSERT INTO admin_audit(actor,action,target) VALUES(?,?,?)',
             (g.user['email'],action,target))

@app.route('/demande-acces',methods=['GET','POST'])
def request_access():
    msg=''
    if request.method=='POST':
        email=request.form.get('email','').strip().lower()
        detail=request.form.get('message','').strip()[:400]
        # Même message de confirmation, pour ne pas divulguer les comptes existants.
        if EMAIL_RE.fullmatch(email) and len(email)<=254:
            now=time.monotonic()
            key='request:'+str(request.remote_addr)
            recent=[t for t in TRIES.get(key,[]) if now-t<3600]
            if len(recent)<5:
                with _DB_LOCK:
                    with connect() as db:
                        user=db.execute('SELECT email FROM users WHERE email=?',(email,)).fetchone()
                        kind='REINITIALISATION' if user else 'CREATION'
                        existing=db.execute("SELECT id FROM user_requests WHERE email=? AND kind=? AND state='EN_ATTENTE'",(email,kind)).fetchone()
                        if not existing:
                            db.execute('INSERT INTO user_requests(email,kind,message) VALUES(?,?,?)',(email,kind,detail))
                recent.append(now);TRIES[key]=recent
            msg='Si votre demande peut être traitée, elle apparaîtra dans la rubrique Administrateur.'
        else:
            msg='Vérifiez le format de votre adresse e-mail.'
    return view('Demande d’accès','''<div class="panel narrow"><h1>Demande d’accès à ESI TICKETS</h1>
      <p>Première connexion ou accès perdu ? Un administrateur examinera votre demande.</p>
      {% if msg %}<p class="ok">{{msg}}</p>{% endif %}
      <form method="post"><label>Adresse e-mail professionnelle</label><input type="email" name="email" required maxlength="254">
      <label>Commentaire (facultatif)</label><textarea name="message" maxlength="400" rows="3" placeholder="Expliquez le problème rencontré"></textarea>
      <button>ENVOYER LA DEMANDE</button></form>
      <p class="muted">Aucun compte n'est créé automatiquement. Ce formulaire n'envoie pas d'e-mail.</p>
      <p><a href="/connexion">Retour à la connexion</a></p></div>''',msg=msg)

@app.route('/activer-compte',methods=['GET','POST'])
def activate_user():
    email=request.values.get('email','').strip().lower()
    message=''
    if request.method=='POST':
        password=request.form.get('password','')
        confirm=request.form.get('confirm','')
        with _DB_LOCK:
            with connect() as db:
                user=db.execute('SELECT email,active,hash FROM users WHERE email=?',(email,)).fetchone()
                if not user or not user['active'] or user['hash']!='!':
                    message='Activation impossible pour ce compte.'
                elif len(password)<14 or len(password)>128 or password!=confirm:
                    message='Saisissez deux mots de passe identiques de 14 à 128 caractères.'
                else:
                    db.execute('UPDATE users SET hash=?,must_change=0,version=version+1 WHERE email=? AND hash=?',
                      (generate_password_hash(password,method='scrypt'),email,'!'))
                    return redirect('/connexion')
    return view('Activer mon compte','''<div class="panel narrow"><h1>Première connexion</h1>
      <p class="warning">La démonstration ne vérifie pas la propriété de l'adresse e-mail. Ne l'utilisez pas avec de vrais comptes.</p>
      {% if message %}<p class="error">{{message}}</p>{% endif %}
      <form method="post"><label>Adresse e-mail</label><input name="email" type="email" value="{{email}}" required>
      <label>Créer mon mot de passe (14 caractères minimum)</label><input name="password" type="password" minlength="14" required>
      <label>Confirmer</label><input name="confirm" type="password" minlength="14" required>
      <button>ACTIVER MON COMPTE</button></form>
      <a href="/connexion">Retour à la connexion</a></div>''',email=email,message=message)

@app.route('/administrateur')
@role_allowed('ADMIN')
def admin_dashboard():
    users=db_all('SELECT email,profil,active,hash FROM users ORDER BY profil,email')
    requests=db_all("SELECT * FROM user_requests ORDER BY CASE WHEN state='EN_ATTENTE' THEN 0 ELSE 1 END,id DESC LIMIT 100")
    audits=db_all('SELECT * FROM admin_audit ORDER BY id DESC LIMIT 30')
    pending=sum(1 for item in requests if item['state']=='EN_ATTENTE')
    return view('Administrateur','''<div class="panel"><h1>ADMINISTRATEUR</h1>
      <p>Gestion des utilisateurs, demandes et accès · environnement de test uniquement.</p>
      <div class="grid">
      <div class="box"><h2>{{users|length}}</h2><span>Utilisateurs fictifs</span></div>
      <div class="box"><h2>{{pending}}</h2><span>Demandes en attente</span></div>
      <div class="box"><h2>3</h2><span>Profils disponibles</span></div></div></div>
      <div class="panel"><h2>Demandes de création et réinitialisation</h2>
      <table><tr><th>Date</th><th>Adresse</th><th>Type</th><th>Statut</th><th>Action</th></tr>
      {% for r in requests %}<tr><td>{{r.created_at}}</td><td>{{r.email}}</td><td>{{r.kind}}</td>
      <td>{{r.state}}</td><td>{% if r.state=='EN_ATTENTE' %}
      <form class="inline" method="post" action="/administrateur/traiter">
      <input type="hidden" name="csrf" value="{{csrf}}"><input type="hidden" name="id" value="{{r.id}}">
      {% if r.kind=='CREATION' %}<select name="profil">{% for role in roles %}<option>{{role}}</option>{% endfor %}</select>{% endif %}
      <button name="decision" value="ACCEPTEE">Accepter</button>
      <button class="secondary" name="decision" value="REFUSEE">Refuser</button></form>
      {% else %}Traité par {{r.handled_by}}{% endif %}</td></tr>
      {% else %}<tr><td colspan="5">Aucune demande.</td></tr>{% endfor %}</table>
      <p class="muted">Un administrateur doit vérifier l'identité d'un collaborateur avant une réinitialisation.</p></div>
      <div class="panel"><h2>Utilisateurs</h2>
      <table><tr><th>Adresse</th><th>Profil</th><th>État</th><th>Activation</th></tr>
      {% for u in users %}<tr><td>{{u.email}}</td><td>{{u.profil}}</td><td>{{'Actif' if u.active else 'Désactivé'}}</td>
      <td>{{'À activer' if u.hash=='!' else 'Activé'}}</td></tr>{% endfor %}</table>
      <p><a class="button secondary" href="/admin/utilisateurs">Modifier les profils / désactiver</a></p></div>
      <div class="panel"><h2>Historique administrateur</h2><table><tr><th>Date</th><th>Administrateur</th><th>Opération</th><th>Compte</th></tr>
      {% for a in audits %}<tr><td>{{a.created_at}}</td><td>{{a.actor}}</td><td>{{a.action}}</td><td>{{a.target}}</td></tr>
      {% else %}<tr><td colspan="4">Aucun mouvement.</td></tr>{% endfor %}</table></div>
      <p><a href="/demandeur">Retour à l'accueil</a></p>''',users=users,requests=requests,audits=audits,pending=pending,csrf=csrf(),roles=ROLES)

@app.route('/administrateur/traiter',methods=['POST'])
@role_allowed('ADMIN')
def admin_handle_request():
    try: rid=int(request.form.get('id','0'))
    except ValueError:abort(400)
    decision=request.form.get('decision','')
    role=request.form.get('profil','DEMANDEUR')
    if decision not in ('ACCEPTEE','REFUSEE') or role not in ROLES:abort(400)
    with _DB_LOCK:
        with connect() as db:
            req=db.execute("SELECT * FROM user_requests WHERE id=? AND state='EN_ATTENTE'",(rid,)).fetchone()
            if not req:abort(404)
            if decision=='ACCEPTEE':
                if req['kind']=='CREATION':
                    existing=db.execute('SELECT 1 FROM users WHERE email=?',(req['email'],)).fetchone()
                    if existing:abort(409)
                    db.execute('INSERT INTO users(email,profil,active,hash,must_change) VALUES(?,?,1,?,0)',
                               (req['email'],role,'!'))
                elif req['kind']=='REINITIALISATION':
                    target=db.execute('SELECT email FROM users WHERE email=?',(req['email'],)).fetchone()
                    if not target:abort(404)
                    db.execute("UPDATE users SET hash='!',must_change=0,version=version+1 WHERE email=?",(req['email'],))
            db.execute("UPDATE user_requests SET state=?,handled_by=?,handled_at=CURRENT_TIMESTAMP WHERE id=?",
                      (decision,g.user['email'],rid))
            db.execute('INSERT INTO admin_audit(actor,action,target) VALUES(?,?,?)',
                      (g.user['email'],req['kind']+' '+decision,req['email']))
    flash('Demande traitée. Après acceptation, le collaborateur peut utiliser « Première connexion ».','ok')
    return redirect('/administrateur')

if __name__=='__main__':app.run(port=10000)
