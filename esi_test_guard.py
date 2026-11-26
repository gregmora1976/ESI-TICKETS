"""Temporary isolated preview. Remove this patch before merging to production."""
import os
import sys


def activate():
    # This branch must never start with any live credentials, even by mistake.
    forbidden = [k for k, v in os.environ.items() if v and (
        k.startswith(('SUPABASE_', 'ESI_DEVIS_SUPABASE_', 'ONEDRIVE_', 'SMTP_'))
        or k in ('GITHUB_TOKEN', 'GITHUB_REPO'))]
    if forbidden:
        raise RuntimeError('TEST ISOLE: supprimer les variables: ' + ', '.join(sorted(forbidden)))
    if os.getenv('ESI_AUTH_ENABLED') != '1':
        raise RuntimeError('TEST ISOLE: ESI_AUTH_ENABLED=1 obligatoire')
    if len(os.getenv('ESI_TEST_PASSWORD', '')) < 24:
        raise RuntimeError('TEST ISOLE: ESI_TEST_PASSWORD (24 caracteres minimum) obligatoire')
    if not os.getenv('ESI_BOOTSTRAP_ADMIN_EMAIL', '').strip():
        raise RuntimeError('TEST ISOLE: ESI_BOOTSTRAP_ADMIN_EMAIL obligatoire')
    os.environ['ESI_DEVIS_LOCAL_TEST'] = '1'
    # Deny outbound network at Python socket level, including urllib and SMTP.
    # Incoming server sockets can still bind/listen/accept.
    def deny_outbound(event, args):
        if event in ('socket.connect', 'socket.getaddrinfo', 'socket.gethostbyname', 'socket.sendto'):
            raise PermissionError('TEST ISOLE: reseau sortant interdit')
    sys.addaudithook(deny_outbound)


def local_auth_db():
    from werkzeug.security import generate_password_hash
    user = dict(id='test-admin', nom='TEST', prenom='Admin',
                email=os.environ['ESI_BOOTSTRAP_ADMIN_EMAIL'].strip().lower(),
                profil='ADMIN', actif=True, changement_obligatoire=False,
                updated_at='test-local-v1',
                mot_de_passe_hash=generate_password_hash(os.environ['ESI_TEST_PASSWORD'], method='scrypt'))
    from urllib.parse import unquote
    def db(method, table, query='', payload=None, **kwargs):
        if method == 'GET' and table == 'utilisateurs_esi':
            q = unquote(query)
            if ('email=eq.' + user['email'] + '&') in q or ('id=eq.' + user['id'] + '&') in q:
                return [dict(user)]
            return []
        if method == 'PATCH' and table == 'utilisateurs_esi' and isinstance(payload, dict) and set(payload) == {'dernier_acces_at'}:
            return None
        if method == 'POST' and table == 'utilisateurs_esi_audit':
            return None
        raise PermissionError('TEST ISOLE: operation non autorisee')
    return db


def register_preview(app):
    from flask import request, jsonify, redirect, g
    @app.before_request
    def preview_guard():
        p, m = request.path, request.method
        if p == '/healthz' and m in ('GET', 'HEAD'):
            return 'OK - TEST ISOLE', 200
        if p in ('/login', '/logout') and m in ('GET', 'HEAD', 'POST'):
            return None
        if not getattr(g, 'esi_user', None):
            if p.startswith('/static/') and m in ('GET', 'HEAD'):
                return None
            return redirect('/login')
        if m in ('GET', 'HEAD'):
            if p in ('/', '/demandeur', '/gestionnaire', '/gestion-reception', '/reception', '/catalogue-caisses', '/catalogue-methodologies', '/api/esi/me') or p.startswith('/static/'):
                return None
            if p in ('/api/tickets', '/api/catalogue-caisses', '/api/catalogue-methodologies'):
                return jsonify([])
            if p.startswith('/devis-caisses/'):
                return None
        if m == 'POST' and (p.startswith('/devis-caisses/api/calculate/') or p in ('/devis-caisses/api/notice/find', '/devis-caisses/api/fiche-pdf')):
            return None
        return jsonify(ok=False, error='MODE TEST ISOLE : operation indisponible, aucune donnee de production'), 403

    @app.after_request
    def mark_preview(response):
        response.headers['Cache-Control'] = 'no-store'
        if response.mimetype == 'text/html' and not response.direct_passthrough:
            body = response.get_data(as_text=True)
            banner = '<div style="background:#fff0bf;color:#704600;padding:10px;text-align:center;font: bold 14px Arial">MODE TEST ISOLE - AUCUNE BASE DE PRODUCTION - TARIFS LOCAUX NON VALIDES</div>'
            import re
            body, count = re.subn(r'(<body\b[^>]*>)', lambda m: m.group(1)+banner, body, count=1, flags=re.I)
            if count: response.set_data(body)
        return response
