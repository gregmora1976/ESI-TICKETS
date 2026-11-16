"""ESI Tickets authentication and role checks. Activated via ESI_AUTH_ENABLED=1.
Requires ESI_SESSION_SECRET (random 32+ bytes), server-only Supabase service credentials.
"""
import os, secrets, time, hmac, csv, io, re
from datetime import datetime, timezone
from functools import wraps
from urllib.parse import quote, urlsplit
from flask import request, session, jsonify, redirect, url_for, render_template_string, abort, g
from werkzeug.security import check_password_hash, generate_password_hash

ROLES = {'DEMANDEUR', 'GESTIONNAIRE', 'ADMIN'}
AUTH_ENABLED = os.environ.get('ESI_AUTH_ENABLED', '0').strip() == '1'
_LOGIN_TRIES = {}

STYLE = '''<style>body{font-family:Arial,sans-serif;background:#eef5fb;color:#12324d;margin:0;display:grid;place-items:center;min-height:100vh}.card{background:white;border:1px solid #d9e7f1;border-radius:20px;padding:30px;width:min(92vw,460px);box-shadow:0 18px 55px #173c5c16}h1{margin:0 0 8px}p{color:#576c80}label{display:block;margin:14px 0 5px;font-weight:700}input,select{box-sizing:border-box;width:100%;padding:12px;border:1px solid #bfcfdd;border-radius:10px}button{background:#0284c7;border:0;color:white;border-radius:10px;padding:11px 15px;font-weight:bold;cursor:pointer;margin-top:18px}.error{color:#b91c1c}.top{display:flex;align-items:center;justify-content:space-between}.top a{color:#0369a1}table{width:100%;border-collapse:collapse}td,th{border-bottom:1px solid #e2e8f0;padding:8px;text-align:left;font-size:13px}.mini{padding:5px 8px;margin:0}.wide{width:min(94vw,1100px)}.muted{font-size:12px;color:#64748b}</style>'''

def register_security(app, db_request, load_ticket):
    if AUTH_ENABLED:
        secret = os.environ.get('ESI_SESSION_SECRET', '')
        if len(secret) < 32:
            raise RuntimeError('ESI_SESSION_SECRET: at least 32 random characters required when ESI_AUTH_ENABLED=1')
        app.secret_key = secret
        app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SECURE=True,
                          SESSION_COOKIE_SAMESITE='Lax', PERMANENT_SESSION_LIFETIME=__import__('datetime').timedelta(hours=10))

    def find_user(email):
        email = (email or '').strip().lower()
        if not email or len(email) > 254: return None
        rows = db_request('GET', 'utilisateurs_esi',
              'select=id,nom,prenom,email,profil,actif,mot_de_passe_hash,changement_obligatoire,updated_at'
              '&email=eq.' + quote(email, safe='') + '&limit=1') or []
        return rows[0] if rows else None

    def current_user():
        uid = session.get('esi_uid')
        if not uid: return None
        rows = db_request('GET', 'utilisateurs_esi',
             'select=id,nom,prenom,email,profil,actif,changement_obligatoire,updated_at&id=eq.' + quote(str(uid), safe='') + '&limit=1') or []
        user = rows[0] if rows else None
        if not user or not user.get('actif') or user.get('profil') not in ROLES or user.get('updated_at') != session.get('esi_session_version'):
            session.clear(); return None
        return user

    def audit(action, target='', details=''):
        try:
            user = getattr(g, 'esi_user', None)
            db_request('POST', 'utilisateurs_esi_audit', '', [{
                'utilisateur_id': user.get('id') if user else None,
                'email': (user.get('email') if user else ''), 'evenement': action, 'details': {'cible':str(target)[:250], 'message':str(details)[:1000]}
            }], prefer='return=minimal')
        except Exception:
            app.logger.exception('ESI security audit unavailable')

    def csrf():
        if not session.get('esi_csrf'): session['esi_csrf'] = secrets.token_urlsafe(32)
        return session['esi_csrf']

    def safe_next(value):
        value = str(value or '/demandeur')
        parsed = urlsplit(value)
        return value if value.startswith('/') and not value.startswith('//') and not parsed.netloc and not parsed.scheme else '/demandeur'

    def forbidden():
        if request.path.startswith('/api/'):
            return jsonify({'ok':False,'error':'Accès non autorisé'}), 403
        abort(403)

    @app.before_request
    def auth_before_request():
        if not AUTH_ENABLED: return None
        path = request.path
        if path.startswith('/static/') or path in ('/login','/logout','/healthz','/initialiser-admin','/premiere-connexion','/demander-acces'):
            return None
        # Les QR codes existants portent une signature verifiee par la route.
        # Seuls les liens en lecture seule restent publics ; une URL sans token
        # continue a recevoir un 403 dans article_public_page / colis_public_page.
        if request.method in ('GET','HEAD') and (
            path.startswith('/article/') or path.startswith('/pre-packing/') or path.startswith('/colis/')
        ):
            return None
        try:
            user = current_user()
        except Exception:
            app.logger.exception('Authentication database unavailable')
            if path.startswith('/api/'):
                return jsonify({'error':'Service de connexion indisponible'}), 503
            return 'Service de connexion indisponible', 503
        g.esi_user = user
        if not user:
            if path.startswith('/api/'):
                return jsonify({'ok':False,'error':'Connexion requise'}),401
            return redirect('/login?next=' + quote(request.full_path if request.query_string else path, safe=''))
        if user.get('changement_obligatoire') and path not in ('/changer-mot-de-passe','/logout'):
            if path.startswith('/api/'):
                return jsonify({'ok':False,'error':'Changement de mot de passe obligatoire'}),403
            return redirect('/changer-mot-de-passe')
        if request.method not in ('GET','HEAD','OPTIONS'):
            # Origin check protects existing JS requests without changing every client API call.
            origin = request.headers.get('Origin')
            if origin and urlsplit(origin).netloc != request.host:
                return jsonify({'error':'Origine non autorisée'}),403
            # Browser session requests should have Origin or same-site Referer.
            referer = request.headers.get('Referer')
            if not origin and (not referer or urlsplit(referer).netloc != request.host):
                return jsonify({'error':'Requête non autorisée'}),403
        role = user['profil']
        if path.startswith(('/reporting','/api/reporting','/api/export/excel','/api/restart')) and role != 'ADMIN':
            return forbidden()
        if role == 'DEMANDEUR' and (
            path.startswith(('/gestionnaire', '/gestion-reception', '/cleaner', '/api/cleaner'))
        ):
            return forbidden()
        # La validation du depot et les actions physiques ne sont pas des saisies DEMANDEUR.
        if role == 'DEMANDEUR' and path.startswith('/api/reception') and request.method not in ('GET','HEAD','OPTIONS'):
            return forbidden()
        if role == 'DEMANDEUR' and path == '/api/tickets' and request.method == 'POST':
            # La creation 'Mise en caisse' lance directement l'affectation physique
            # des articles/pre-packings dans app.py : elle n'est pas une simple demande.
            if request.form.get('module','').strip() == 'Mise en caisse':
                return forbidden()
        if path.startswith('/api/reception/') and request.method not in ('GET','HEAD','OPTIONS') and role == 'DEMANDEUR':
            return forbidden()
        if path.startswith('/api/catalogue-') and request.method not in ('GET','HEAD','OPTIONS') and role != 'ADMIN':
            return forbidden()
        if path.startswith('/api/referentiels/') and request.method not in ('GET','HEAD','OPTIONS') and role != 'ADMIN':
            return forbidden()
        if path.startswith('/api/articles/') and request.method not in ('GET','HEAD','OPTIONS'):
            # Business change to relationships and destructive migrations are restricted.
            admin_actions = ('/bulk-delete','/delete-duplicates','/migrate','/sync-packings')
            manager_actions = ('/bulk-update','/colis','/dissocier','/link-search')
            if any(x in path for x in admin_actions) and role != 'ADMIN': return forbidden()
            if any(x in path for x in manager_actions) and role == 'DEMANDEUR': return forbidden()
            if request.method == 'DELETE' and role == 'DEMANDEUR': return forbidden()
        if path.startswith('/api/tickets'):
            is_write = request.method not in ('GET','HEAD','OPTIONS')
            if is_write:
                suffix = path[len('/api/tickets'):]
                # All operational validation, status, reception, file linking managed by gestionnaires.
                operational = ('/status','/localisation','/reception-','/enlevement','/bon-livraison',
                               '/manager-sheet','/fiche','/articles-lies','/valider-sortie','/annuler-enlevement',
                               '/validate-aller-voir')
                if any(x in suffix for x in operational) and role == 'DEMANDEUR':
                    return forbidden()  # Le DEMANDEUR ne peut JAMAIS changer un statut, y compris le sien.
                if request.method == 'DELETE' and role != 'ADMIN': return forbidden()
                if request.method == 'PUT' and role == 'DEMANDEUR':
                    parts = suffix.strip('/').split('/')
                    if len(parts) != 1: return forbidden()
                    ticket = load_ticket(parts[0])
                    # Old tickets are not silently attributed to someone.
                    if not ticket or str(ticket.get('created_by_email') or '').lower() != str(user['email']).lower():
                        return forbidden()
                    # Bloquer les actions opérationnelles même via PUT générique.
                    change = request.get_json(silent=True) or {}
                    if not isinstance(change, dict): return forbidden()
                    allowed = {
                        'dossier','ref','preteur','expo','objet','chargeProjet',
                        'typeCaisse','dimensions','dateEmballage','prixDevis',
                        'dateRdv','heureRdv','typeVisite','lieuRdv',
                        'contactRdv','commentaire'
                    }
                    if not set(change).issubset(allowed):
                        return forbidden()
                if role == 'DEMANDEUR' and suffix and suffix.strip('/') and '/' in suffix.strip('/'):
                    # Seuls les messages sont autorises en POST ; la liste blanche
                    # empeche d'atteindre de nouvelles routes a effet de bord.
                    if not (request.method == 'POST' and suffix.endswith('/messages')):
                        return forbidden()
        if path.startswith('/api/aller-voir/') and request.method not in ('GET','HEAD','OPTIONS') and role == 'DEMANDEUR':
            return forbidden()
        return None

    @app.route('/login', methods=['GET','POST'], endpoint='esi_secure_login')
    def auth_login():
        if not AUTH_ENABLED:
            return redirect('/demandeur')
        error = ''
        if request.method == 'POST':
            key = (request.remote_addr or '-') + ':' + (request.form.get('email','').lower()[:250])
            now = time.monotonic()
            tries = [x for x in _LOGIN_TRIES.get(key,[]) if now-x < 900]
            if len(tries) >= 8:
                error = 'Trop de tentatives. Réessaie plus tard.'
            else:
                tries.append(now); _LOGIN_TRIES[key] = tries
                email = request.form.get('email','').strip().lower()
                password = request.form.get('password','')
                user = find_user(email)
                if user and user.get('actif') and user.get('mot_de_passe_hash') and check_password_hash(user['mot_de_passe_hash'],password):
                    session.clear(); session.permanent=True
                    session['esi_uid'] = str(user['id']); session['esi_csrf'] = secrets.token_urlsafe(32); session['esi_session_version'] = user.get('updated_at'); g.esi_user = user
                    _LOGIN_TRIES.pop(key,None)
                    try:
                        db_request('PATCH','utilisateurs_esi','id=eq.'+quote(str(user['id']),safe=''),
                                   {'dernier_acces_at':datetime.now(timezone.utc).isoformat()},prefer='return=minimal')
                    except Exception: app.logger.exception('Unable to update last access')
                    audit('login', email)
                    if user.get('changement_obligatoire'): return redirect('/changer-mot-de-passe')
                    return redirect(safe_next(request.form.get('next')))
                error = 'Identifiants incorrects ou compte inactif.'
        return render_template_string('''<!doctype html><html lang="fr"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Connexion ESI TICKETS</title>'''+STYLE+'''<form class="card" method="post"><h1>ESI TICKETS</h1><p>Connexion professionnelle</p><label>Adresse e-mail</label><input name="email" type="email" required autocomplete="username" maxlength="254"><label>Mot de passe</label><input name="password" type="password" required autocomplete="current-password"><p><a href="/premiere-connexion">Première connexion : créer mon mot de passe</a></p><p><a href="/demander-acces">Demander un compte ou une réinitialisation</a></p><input type="hidden" name="next" value="{{ next_path }}"><button type="submit">SE CONNECTER</button>{% if error %}<p class="error">{{error}}</p>{% endif %}</form></html>''',error=error,next_path=safe_next(request.args.get('next')))

    @app.route('/initialiser-admin', methods=['GET','POST'])
    def auth_bootstrap_admin():
        if not AUTH_ENABLED: abort(404)
        target = os.environ.get('ESI_BOOTSTRAP_ADMIN_EMAIL','').strip().lower()
        token = os.environ.get('ESI_BOOTSTRAP_TOKEN','')
        if not target or len(token) < 32: abort(404)
        user = find_user(target)
        if not user or user.get('profil') != 'ADMIN' or not user.get('actif') or user.get('mot_de_passe_hash'):
            abort(404)
        error=''; done=False; generated=''
        if request.method == 'POST':
            key='bootstrap:'+str(request.remote_addr)
            now=time.monotonic(); tries=[x for x in _LOGIN_TRIES.get(key,[]) if now-x<3600]
            if len(tries)>=5: abort(429)
            tries.append(now); _LOGIN_TRIES[key]=tries
            if not hmac.compare_digest(request.form.get('code',''),token):
                error='Code de mise en service incorrect.'
            else:
                generated=secrets.token_urlsafe(22)
                db_request('PATCH','utilisateurs_esi','id=eq.'+quote(str(user['id']),safe=''),
                    {'mot_de_passe_hash':generate_password_hash(generated,method='scrypt'),
                     'changement_obligatoire':True,'updated_at':datetime.now(timezone.utc).isoformat()},
                    prefer='return=minimal')
                done=True
        body='''<div class="card"><h1>Initialisation administrateur ESI</h1>{% if done %}<p>Mot de passe provisoire à conserver confidentiellement ; affiché une seule fois.</p><pre style="white-space:pre-wrap;overflow-wrap:anywhere">{{generated}}</pre><p>Supprimer immédiatement ESI_BOOTSTRAP_TOKEN et ESI_BOOTSTRAP_ADMIN_EMAIL des variables Render, puis <a href="/login">se connecter</a>.</p>{% else %}<p>Opération unique réservée au responsable du déploiement.</p><form method="post"><label>Code de mise en service</label><input type="password" name="code" required><button>Initialiser le premier administrateur</button></form>{% if error %}<p class="error">{{error}}</p>{% endif %}{% endif %}</div>'''
        return render_template_string('<!doctype html><html lang="fr"><meta charset="utf-8">'+STYLE+body+'</html>',done=done,generated=generated,error=error)

    @app.route('/logout',methods=['POST'])
    def auth_logout():
        if not AUTH_ENABLED: return redirect('/demandeur')
        if not hmac.compare_digest(request.form.get('csrf',''),session.get('esi_csrf','')): abort(403)
        session.clear(); return redirect('/login')

    @app.route('/changer-mot-de-passe',methods=['GET','POST'])
    def auth_change_password():
        if not AUTH_ENABLED: return redirect('/demandeur')
        user = current_user()
        if not user: return redirect('/login')
        error = ''; success=False
        if request.method == 'POST':
            if not hmac.compare_digest(request.form.get('csrf',''), session.get('esi_csrf','')): abort(403)
            old = request.form.get('ancien',''); new = request.form.get('nouveau','')
            rows = db_request('GET','utilisateurs_esi','select=mot_de_passe_hash&id=eq.'+quote(str(user['id']),safe='')+'&limit=1') or []
            valid = rows and rows[0].get('mot_de_passe_hash') and check_password_hash(rows[0]['mot_de_passe_hash'],old)
            if not valid: error='Mot de passe actuel incorrect.'
            elif len(new) < 14 or len(new) > 128 or new == old: error='Choisis un nouveau mot de passe différent, de 14 caractères minimum.'
            else:
                new_version = datetime.now(timezone.utc).isoformat()
                db_request('PATCH','utilisateurs_esi','id=eq.'+quote(str(user['id']),safe=''),
                           {'mot_de_passe_hash':generate_password_hash(new,method='scrypt'), 'changement_obligatoire':False,
                            'updated_at':new_version},prefer='return=minimal')
                refreshed = db_request('GET','utilisateurs_esi','select=updated_at&id=eq.'+quote(str(user['id']),safe='')+'&limit=1')
                session['esi_session_version'] = refreshed[0]['updated_at']
                audit('password_changed',user['email']);success=True
        return render_template_string('''<!doctype html><html lang="fr"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Mot de passe ESI</title>'''+STYLE+'''<div class="card"><h1>Changer mon mot de passe</h1><p>Choisir au moins 14 caractères.</p>{% if success %}<p>Mot de passe modifié. <a href="/demandeur">Accéder à ESI TICKETS</a></p>{% else %}<form method="post"><input type="hidden" name="csrf" value="{{csrf}}"><label>Mot de passe actuel</label><input name="ancien" type="password" autocomplete="current-password" required><label>Nouveau mot de passe</label><input name="nouveau" type="password" autocomplete="new-password" minlength="14" required><button>ENREGISTRER</button>{% if error %}<p class="error">{{error}}</p>{% endif %}</form>{% endif %}</div></html>''',csrf=csrf(),error=error,success=success)

    @app.route('/api/esi/me',methods=['GET'])
    def auth_me():
        if not AUTH_ENABLED: return jsonify({'enabled':False})
        u=current_user()
        return jsonify({'enabled':True,'email':u['email'],'profil':u['profil'],'nom':u['nom'],'prenom':u['prenom'],'csrf':csrf()})

    @app.route('/admin/utilisateurs',methods=['GET'])
    def auth_admin_users():
        if not AUTH_ENABLED: abort(404)
        user = current_user()
        if not user or user['profil']!='ADMIN':abort(403)
        rows=db_request('GET','utilisateurs_esi','select=id,nom,prenom,email,profil,actif,changement_obligatoire,dernier_acces_at&order=nom.asc&limit=2000') or []
        return render_template_string('''<!doctype html><html lang="fr"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Administration utilisateurs</title>'''+STYLE+'''<div class="card wide"><div class="top"><h1>ADMIN · UTILISATEURS</h1><a href="/demandeur">Retour</a></div><p>{{users|length}} collaborateurs • Les mots de passe ne sont jamais affichés.</p><form method="post" action="/admin/utilisateurs/ajouter"><input type="hidden" name="csrf" value="{{csrf}}"><h3>Ajouter un utilisateur</h3><input name="nom" placeholder="Nom" required><input name="prenom" placeholder="Prénom"><input name="email" type="email" placeholder="Adresse professionnelle" required><select name="profil"><option>DEMANDEUR</option><option>GESTIONNAIRE</option><option>ADMIN</option></select><button>Ajouter (compte sans mot de passe)</button></form><p><a href="/admin/journal">Consulter le journal d'activité</a></p><h3>Importer des nouveaux comptes</h3><form method="post" action="/admin/utilisateurs/importer" enctype="multipart/form-data"><input type="hidden" name="csrf" value="{{csrf}}"><input name="fichier" type="file" accept=".csv,text/csv" required><button>Importer le CSV (sans supprimer les comptes existants)</button></form><h3>Comptes existants</h3><table><tr><th>Nom</th><th>E-mail</th><th>Profil</th><th>Actif</th><th>Action</th></tr>{% for u in users %}<tr><td>{{u.nom}} {{u.prenom}}</td><td>{{u.email}}</td><td>{{u.profil}}</td><td>{{'Oui' if u.actif else 'Non'}}</td><td><form method="post" action="/admin/utilisateurs/modifier"><input type="hidden" name="csrf" value="{{csrf}}"><input type="hidden" name="id" value="{{u.id}}"><select name="profil"><option {% if u.profil=='DEMANDEUR' %}selected{% endif %}>DEMANDEUR</option><option {% if u.profil=='GESTIONNAIRE' %}selected{% endif %}>GESTIONNAIRE</option><option {% if u.profil=='ADMIN' %}selected{% endif %}>ADMIN</option></select><select name="actif"><option value="true" {% if u.actif %}selected{% endif %}>Actif</option><option value="false" {% if not u.actif %}selected{% endif %}>Inactif</option></select><button class="mini">Enregistrer</button></form><form method="post" action="/admin/utilisateurs/reinitialiser"><input type="hidden" name="csrf" value="{{csrf}}"><input type="hidden" name="id" value="{{u.id}}"><button class="mini" style="background:#b45309">Réinitialiser l’accès</button></form></td></tr>{% endfor %}</table></div></html>''', users=rows,csrf=csrf())

    def check_admin_post():
        user=current_user()
        if not user or user['profil']!='ADMIN':abort(403)
        if not hmac.compare_digest(request.form.get('csrf',''),session.get('esi_csrf','')):abort(403)
        return user

    @app.route('/admin/journal')
    def auth_admin_audit_page():
        if not AUTH_ENABLED:abort(404)
        user=current_user()
        if not user or user['profil']!='ADMIN':abort(403)
        rows=db_request('GET','utilisateurs_esi_audit',
            'select=date_evenement,email,evenement,details&order=date_evenement.desc&limit=200') or []
        page='''<!doctype html><html lang="fr"><meta charset="utf-8"><title>Journal ESI</title>'''+STYLE+'''<div class="card wide"><h1>Journal des accès et modifications</h1><p><a href="/admin/utilisateurs">Retour aux utilisateurs</a></p><table><tr><th>Date</th><th>Utilisateur</th><th>Action</th><th>Détails</th></tr>{% for r in rows %}<tr><td>{{r.date_evenement}}</td><td>{{r.email}}</td><td>{{r.evenement}}</td><td>{{r.details}}</td></tr>{% endfor %}</table></div></html>'''
        return render_template_string(page,rows=rows)

    @app.route('/admin/utilisateurs/ajouter',methods=['POST'])
    def auth_admin_add():
        admin=check_admin_post()
        email=request.form.get('email','').strip().lower(); role=request.form.get('profil','DEMANDEUR')
        if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',email) or role not in ROLES:abort(400)
        if find_user(email): abort(409)
        db_request('POST','utilisateurs_esi','',[{'nom':request.form.get('nom','').strip(),
                  'prenom':request.form.get('prenom','').strip(),'email':email,'profil':role,'actif':True,
                  'changement_obligatoire':False}],prefer='return=minimal')
        audit('user_created',email)
        return redirect('/admin/utilisateurs')

    @app.route('/admin/utilisateurs/modifier',methods=['POST'])
    def auth_admin_edit():
        admin=check_admin_post(); uid=request.form.get('id','')
        role=request.form.get('profil','');active=request.form.get('actif')=='true'
        if role not in ROLES:abort(400)
        if uid==str(admin['id']) and (not active or role!='ADMIN'):abort(400,'Impossible de désactiver son propre accès administrateur.')
        db_request('PATCH','utilisateurs_esi','id=eq.'+quote(uid,safe=''),
                 {'profil':role,'actif':active,'updated_at':datetime.now(timezone.utc).isoformat()},prefer='return=minimal')
        audit('user_updated',uid,role+' active='+str(active))
        return redirect('/admin/utilisateurs')


    @app.route('/admin/utilisateurs/reinitialiser', methods=['POST'])
    def auth_admin_reset_password():
        check_admin_post()
        uid = request.form.get('id','')
        rows = db_request('GET','utilisateurs_esi',
            'select=id,email,actif&id=eq.'+quote(uid,safe='')+'&limit=1') or []
        if not rows: abort(404)
        db_request('PATCH','utilisateurs_esi','id=eq.'+quote(uid,safe=''),
            {'mot_de_passe_hash':None,'changement_obligatoire':False,
             'updated_at':datetime.now(timezone.utc).isoformat()},prefer='return=minimal')
        audit('password_reset_by_admin',rows[0]['email'])
        return redirect('/administrateur')

    @app.route('/admin/utilisateurs/importer',methods=['POST'])
    def auth_admin_import():
        check_admin_post()
        file=request.files.get('fichier')
        if not file: abort(400)
        raw=file.read(2_000_001)
        if len(raw)>2_000_000: abort(413)
        try: content=raw.decode('utf-8-sig')
        except UnicodeDecodeError: abort(400,'CSV doit être en UTF-8')
        reader=csv.DictReader(io.StringIO(content),delimiter=';')
        if not {'nom','prenom','email','profil','actif'}.issubset(set(reader.fieldnames or [])):
            abort(400,'Colonnes attendues: nom;prenom;email;profil;actif')
        new=[];skipped=0;seen=set()
        for row in reader:
            email=str(row.get('email') or '').strip().lower()
            role=str(row.get('profil') or 'DEMANDEUR').strip().upper()
            active=str(row.get('actif') or 'true').strip().lower() in ('1','true','oui','yes')
            if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',email) or role not in ROLES:
                abort(400,'Adresse ou profil invalide dans le CSV')
            if not active or email in seen or find_user(email):
                skipped+=1;continue
            seen.add(email)
            new.append({'nom':str(row.get('nom') or '').strip(),'prenom':str(row.get('prenom') or '').strip(),
                'email':email,'profil':role,'actif':True,'changement_obligatoire':False})
        if new:
            db_request('POST','utilisateurs_esi','',new,prefer='return=minimal')
            audit('users_imported',str(len(new)))
        body='<div class="card"><h1>Import terminé</h1><p>{{count}} ajouté(s), {{skipped}} ignoré(s).</p><p>Les utilisateurs pourront créer leur mot de passe à la première connexion.</p><a href="/admin/utilisateurs">Retour</a></div>'
        return render_template_string('<!doctype html><html lang="fr"><meta charset="utf-8">'+STYLE+body+'</html>',count=len(new),skipped=skipped)

    # ---- Activation autonome et demandes d'accès : SANS vérification par e-mail (choix ESI). ----
    # Une adresse autorisée non activée peut être revendiquée par son premier demandeur.
    # Ce risque est assumé et doit être traité avant la mise en production.
    def public_csrf():
        if not session.get('esi_public_csrf'): session['esi_public_csrf'] = secrets.token_urlsafe(32)
        return session['esi_public_csrf']

    def public_post_allowed(bucket):
        if not hmac.compare_digest(request.form.get('csrf',''), session.get('esi_public_csrf','')): abort(403)
        now=time.monotonic()
        key=bucket+':'+(request.remote_addr or '-')
        trials=[x for x in _LOGIN_TRIES.get(key,[]) if now-x < 3600]
        if len(trials)>=8: abort(429)
        trials.append(now); _LOGIN_TRIES[key]=trials

    def valid_email(email):
        return len(email) <= 254 and bool(re.fullmatch(r'[^\s@]{1,64}@[^\s@]{1,190}\.[^\s@]{2,}',email))

    @app.route('/premiere-connexion',methods=['GET','POST'])
    def esi_first_login():
        if not AUTH_ENABLED:return redirect('/demandeur')
        error='';done=False
        if request.method=='POST':
            public_post_allowed('activate')
            email=request.form.get('email','').strip().lower()
            passwd=request.form.get('password','')
            confirm=request.form.get('confirm','')
            if not valid_email(email) or len(passwd)<14 or len(passwd)>128 or passwd!=confirm:
                error='Vérifie ton adresse et choisis deux mots de passe identiques (14 à 128 caractères).'
            else:
                user=find_user(email)
                # Un premier ADMIN ne peut pas s'attribuer un compte par simple
                # connaissance de son e-mail pendant la mise en service.
                # Le jeton de mise en service permet d'activer uniquement le compte
                # ADMIN explicitement désigné dans Render, sans autre fichier.
                bootstrap_token = os.environ.get('ESI_BOOTSTRAP_TOKEN', '')
                bootstrap_target = os.environ.get('ESI_BOOTSTRAP_ADMIN_EMAIL', '').strip().lower()
                needs_bootstrap = bool(user and user.get('profil') == 'ADMIN' and bootstrap_token)
                bootstrap_ok = (
                    len(bootstrap_token) >= 32
                    and email == bootstrap_target
                    and hmac.compare_digest(request.form.get('code_admin', ''), bootstrap_token)
                ) if needs_bootstrap else True
                if not user or not user.get('actif') or user.get('mot_de_passe_hash'):
                    error='Activation impossible. Vérifie ton accès ou contacte un administrateur.'
                elif needs_bootstrap and not bootstrap_ok:
                    error='Activation ADMIN non autorisée : vérifie le compte configuré dans Render et son code de mise en service.'
                else:
                    # Protection contre deux activations concurrentes; la condition sur hash est évaluée en DB.
                    result=db_request('PATCH','utilisateurs_esi',
                        'id=eq.'+quote(str(user['id']),safe='')+'&mot_de_passe_hash=is.null',
                        {'mot_de_passe_hash':generate_password_hash(passwd,method='scrypt'),
                         'changement_obligatoire':False,'updated_at':datetime.now(timezone.utc).isoformat()},
                        prefer='return=representation') or []
                    if result:
                        audit('account_activated',email)
                        done=True
                    else:error='Compte déjà activé. Utilise la connexion classique.'
        body='''<form class="card" method="post"><h1>Première connexion</h1>
        <p>Compte déjà autorisé : définis ton mot de passe personnel.</p>
        <p class="error">Attention : l'identité liée à l'adresse e-mail n'est pas vérifiée automatiquement.</p>
        {% if done %}<p>Compte activé. <a href="/login">Se connecter</a></p>
        {% else %}<input type="hidden" name="csrf" value="{{csrf}}">
        <label>Adresse e-mail</label><input type="email" name="email" maxlength="254" required>
        <label>Choisir mon mot de passe</label><input name="password" type="password" minlength="14" maxlength="128" autocomplete="new-password" required>
        <label>Confirmer le mot de passe</label><input name="confirm" type="password" minlength="14" maxlength="128" autocomplete="new-password" required>
        <label>Code de mise en service (premier ADMIN uniquement)</label>
        <input name="code_admin" type="password" autocomplete="off" placeholder="Laisser vide pour les autres utilisateurs">
        <p class="muted">Pour le compte ADMIN initial indiqué dans Render, utiliser ESI_BOOTSTRAP_TOKEN. Ne communique jamais ce code.</p>
        <button>ACTIVER MON COMPTE</button>{% if error %}<p class="error">{{error}}</p>{% endif %}
        {% endif %}<p><a href="/login">Retour</a></p></form>'''
        return render_template_string('<!doctype html><html lang="fr"><meta charset="utf-8">'+STYLE+body+'</html>',csrf=public_csrf(),error=error,done=done)

    @app.route('/demander-acces',methods=['GET','POST'])
    def esi_request_access():
        if not AUTH_ENABLED:return redirect('/demandeur')
        done=False;error=''
        if request.method=='POST':
            public_post_allowed('request')
            email=request.form.get('email','').strip().lower()
            if not valid_email(email):error='Adresse e-mail invalide.'
            else:
                target=find_user(email)
                kind='REINITIALISATION' if target else 'CREATION'
                # Éviter les doublons (l'unicité est aussi contrôlée en PostgreSQL).
                pending=db_request('GET','utilisateurs_esi_demandes',
                    'select=id&email=eq.'+quote(email,safe='')+'&type=eq.'+kind+'&statut=eq.EN_ATTENTE&limit=1') or []
                if not pending:
                    try:
                        db_request('POST','utilisateurs_esi_demandes','',{
                            'email':email,'type':kind,'commentaire':request.form.get('commentaire','').strip()[:400]},
                            prefer='return=minimal')
                    except Exception:
                        # Concurrence entre requêtes : la contrainte UNIQUE peut rejeter un doublon.
                        app.logger.exception('Demande non enregistrée')
                        error='Impossible de transmettre la demande pour le moment.'
                if not error:done=True
        body='''<form class="card" method="post"><h1>Demande d'accès</h1>
        {% if done %}<p>Demande prise en compte. Un administrateur pourra l'examiner dans ESI TICKETS.</p>
        {% else %}<p>Nouvel utilisateur ou mot de passe perdu : dépose une demande pour les administrateurs.</p>
        <input type="hidden" name="csrf" value="{{csrf}}"><label>Adresse e-mail</label>
        <input name="email" type="email" required maxlength="254">
        <label>Commentaire (facultatif)</label><input name="commentaire" maxlength="400">
        <button>ENVOYER LA DEMANDE</button>{% if error %}<p class="error">{{error}}</p>{% endif %}
        {% endif %}<p><a href="/login">Retour</a></p></form>'''
        return render_template_string('<!doctype html><html lang="fr"><meta charset="utf-8">'+STYLE+body+'</html>',csrf=public_csrf(),error=error,done=done)

    @app.route('/administrateur', methods=['GET'])
    def esi_admin_dashboard():
        if not AUTH_ENABLED:abort(404)
        user=current_user()
        if not user or user['profil']!='ADMIN':abort(403)
        users=db_request('GET','utilisateurs_esi',
            'select=id,nom,prenom,email,profil,actif,mot_de_passe_hash&order=nom.asc&limit=2000') or []
        pending=db_request('GET','utilisateurs_esi_demandes',
            'select=id,email,type,statut,commentaire,created_at&statut=eq.EN_ATTENTE&order=created_at.desc&limit=200') or []
        body='''<div class="card wide"><div class="top"><h1>ADMINISTRATEUR</h1><a href="/demandeur">RETOUR</a></div>
        <p>Gestion des utilisateurs et des demandes d'accès.</p>
        <div style="display:flex;gap:12px;flex-wrap:wrap">
        <div style="padding:18px;background:#eef5fb;border-radius:12px">Utilisateurs : <b>{{users|length}}</b></div>
        <div style="padding:18px;border-radius:12px;background:{{'#fee2e2' if pending else '#eef5fb'}};color:{{'#991b1b' if pending else '#12324d'}}">
        <b>Demandes en attente : {{pending|length}}</b>{% if pending %} · À TRAITER{% endif %}</div></div>
        <h2>Créer directement un utilisateur</h2>
        <form method="post" action="/administrateur/creer"><input type="hidden" name="csrf" value="{{csrf}}">
        <label>Nom</label><input name="nom" maxlength="100" required><label>Prénom</label><input name="prenom" maxlength="100">
        <label>Adresse e-mail</label><input name="email" type="email" required maxlength="254">
        <label>Profil</label><select name="profil"><option>DEMANDEUR</option><option>GESTIONNAIRE</option><option>ADMIN</option></select>
        <button>CRÉER L'UTILISATEUR</button></form>
        <h2>Demandes en attente</h2><table><tr><th>Date</th><th>E-mail</th><th>Nature</th><th>Action</th></tr>
        {% for d in pending %}<tr><td>{{d.created_at}}</td><td>{{d.email}}</td><td>{{d.type}}</td><td>
        <form method="post" action="/administrateur/traiter"><input type="hidden" name="csrf" value="{{csrf}}">
        <input type="hidden" name="id" value="{{d.id}}">
        {% if d.type=='CREATION' %}<select name="profil"><option>DEMANDEUR</option><option>GESTIONNAIRE</option><option>ADMIN</option></select>{% endif %}
        <button name="decision" value="ACCEPTEE" class="mini">Accepter</button>
        <button name="decision" value="REFUSEE" class="mini" style="background:#64748b">Refuser</button></form></td></tr>
        {% else %}<tr><td colspan="4">Aucune demande en attente</td></tr>{% endfor %}</table>
        <h2>Utilisateurs</h2><table><tr><th>Nom</th><th>E-mail</th><th>Profil</th><th>Accès</th></tr>
        {% for u in users %}<tr><td>{{u.nom}} {{u.prenom}}</td><td>{{u.email}}</td><td>{{u.profil}}</td>
        <td>{{'Actif' if u.actif else 'Suspendu'}} · {{'Activé' if u.mot_de_passe_hash else 'À activer'}}</td></tr>{% endfor %}</table>
        <p><a href="/admin/utilisateurs">Modifier les droits / suspendre un compte</a> · <a href="/admin/journal">Historique</a></p></div>'''
        return render_template_string('<!doctype html><html lang="fr"><meta charset="utf-8">'+STYLE+body+'</html>',users=users,pending=pending,csrf=csrf())

    @app.route('/administrateur/creer',methods=['POST'])
    def esi_admin_create_direct():
        check_admin_post()
        email=request.form.get('email','').strip().lower()
        role=request.form.get('profil','DEMANDEUR')
        if not valid_email(email) or role not in ROLES:abort(400)
        if find_user(email):abort(409,'Ce compte existe déjà')
        db_request('POST','utilisateurs_esi','',{
            'nom':request.form.get('nom','').strip()[:100],
            'prenom':request.form.get('prenom','').strip()[:100],
            'email':email,'profil':role,'actif':True,
            'mot_de_passe_hash':None,'changement_obligatoire':False},prefer='return=minimal')
        audit('user_created_direct',email)
        return redirect('/administrateur')

    @app.route('/administrateur/traiter',methods=['POST'])
    def esi_admin_review():
        admin=check_admin_post()
        decision=request.form.get('decision','')
        role=request.form.get('profil','DEMANDEUR')
        if decision not in ('ACCEPTEE','REFUSEE') or role not in ROLES:abort(400)
        rid=request.form.get('id','')
        found=db_request('GET','utilisateurs_esi_demandes',
            'select=id,email,type,statut&id=eq.'+quote(str(rid),safe='')+'&statut=eq.EN_ATTENTE&limit=1') or []
        if not found:abort(404)
        req=found[0]
        if decision=='ACCEPTEE':
            existing=find_user(req['email'])
            if req['type']=='CREATION':
                if existing:abort(409,'Compte déjà présent')
                db_request('POST','utilisateurs_esi','',{
                    'email':req['email'],'profil':role,'actif':True,
                    'mot_de_passe_hash':None,'changement_obligatoire':False},prefer='return=minimal')
            elif req['type']=='REINITIALISATION':
                if not existing:abort(404)
                # L'admin a vérifié l'identité par un canal indépendant.
                db_request('PATCH','utilisateurs_esi','id=eq.'+quote(str(existing['id']),safe=''),{
                    'mot_de_passe_hash':None,'changement_obligatoire':False,
                    'updated_at':datetime.now(timezone.utc).isoformat()},prefer='return=minimal')
        db_request('PATCH','utilisateurs_esi_demandes',
            'id=eq.'+quote(str(rid),safe='')+'&statut=eq.EN_ATTENTE',
            {'statut':decision,'traite_par':str(admin['id']),
             'traite_at':datetime.now(timezone.utc).isoformat()},prefer='return=minimal')
        audit('access_request_'+decision,req['email'],req['type'])
        return redirect('/administrateur')

    # Replace historic login URL only when enabled. Existing handler remains in legacy mode.
    legacy_view = app.view_functions.get('login')
    if legacy_view and AUTH_ENABLED:
        app.view_functions['login'] = auth_login

    return {'find_user':find_user,'current_user':current_user,'audit':audit}
