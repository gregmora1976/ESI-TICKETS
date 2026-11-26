"""Flask bridge for the existing ESI DEVIS request handlers, without a second server."""
from io import BytesIO
import json
from flask import Blueprint, Response, g, request, jsonify
from urllib.parse import quote
import os
from . import app as legacy

PREFIX = '/devis-caisses'
devis_bp = Blueprint('esi_devis', __name__, url_prefix=PREFIX)
ADMIN_PATHS = {'/admin', '/api/admin/config', '/api/admin/save', '/api/caisse-deco/prices', '/api/notice/associate', '/api/notice/delete'}


def prefix_notice_links(value):
    if isinstance(value, dict):
        return {k: prefix_notice_links(v) for k, v in value.items()}
    if isinstance(value, list):
        return [prefix_notice_links(v) for v in value]
    if isinstance(value, str) and value.startswith(('/assets/notices/', 'assets/notices/')):
        return PREFIX + '/' + value.lstrip('/')
    return value


class FlaskHandler(legacy.Handler):
    def __init__(self, path):
        self.path = path
        self.headers = dict(request.headers)
        raw = request.get_data()
        self.headers['Content-Length'] = str(len(raw))
        self.rfile = BytesIO(raw)
        self.wfile = BytesIO()
        self.status = 200
        self.response_headers = {}

    def send_response(self, code, message=None):
        self.status = code
        self.response_headers = {}
        self.wfile = BytesIO()

    def send_header(self, name, value):
        self.response_headers[name] = value

    def end_headers(self):
        pass

    def send_error(self, code, message=None, explain=None):
        self.send_json({'ok': False, 'error': message or 'Ressource indisponible'}, code)

    def send_json(self, payload, status=200):
        super().send_json(prefix_notice_links(payload), status)

    def response(self, admin):
        body = self.wfile.getvalue()
        if self.response_headers.get('Content-Type', '').startswith('text/html'):
            if admin:
                body = body.replace(b'<html lang="fr">', b'<html lang="fr" data-esi-role="ADMIN">', 1)
        if self.response_headers.get('Content-Type', '').startswith('text/html') and not admin:
            css = b'<style>a[href="/devis-caisses/admin"],button[onclick="askNoticeFile()"],button[onclick^="deleteLibraryNotice("],button[onclick="saveDecoPrices()"],.deco-prices-title{display:none!important}input[id^="deco_"]{pointer-events:none;opacity:.65}</style>'
            body = body.replace(b'</head>', css + b'</head>', 1)
        headers = {k: v for k, v in self.response_headers.items() if k.lower() != 'content-length'}
        return Response(body, status=self.status, headers=headers)


@devis_bp.route('/', defaults={'subpath': ''}, methods=['GET', 'POST'])
@devis_bp.route('/<path:subpath>', methods=['GET', 'POST'])
def dispatch(subpath):
    user = getattr(g, 'esi_user', None) or {}
    if user.get('profil') not in {'DEMANDEUR', 'GESTIONNAIRE', 'ADMIN'}:
        return jsonify(ok=False, error='Connexion ESI requise'), 401
    admin = user.get('profil') == 'ADMIN'
    path = '/' + subpath
    if path in ADMIN_PATHS and not admin:
        return jsonify(ok=False, error='Accès administrateur refusé'), 403
    if request.content_length and request.content_length > 30 * 1024 * 1024:
        return jsonify(ok=False, error='Fichier trop volumineux (30 Mo maximum)'), 413
    # Refuse persistent mutations when the separate Devis database is not configured.
    if path.startswith('/api/') and path not in {'/api/health'} and not legacy._admin_supabase_enabled() and os.getenv('ESI_DEVIS_LOCAL_TEST') != '1':
        return jsonify(ok=False, error='Connexion Supabase DEVIS non configurée'), 503
    handler = FlaskHandler(quote(path, safe='/'))
    if not admin and path == '/api/calculate/Caisse déco' and request.method == 'POST':
        try:
            payload = json.loads(handler.rfile.getvalue())
            payload.pop('prices', None)
            raw = json.dumps(payload).encode('utf-8')
            handler.rfile = BytesIO(raw)
            handler.headers['Content-Length'] = str(len(raw))
        except (ValueError, AttributeError):
            return jsonify(ok=False, error='Données invalides'), 400
    if request.method == 'POST':
        handler.do_POST()
    else:
        handler.do_GET()
    return handler.response(admin)
