from functools import wraps
from flask import session, redirect, url_for, request, jsonify
import os


def requires_auth(f):
    """Decorator to require authentication for routes."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not os.getenv('REQUIRE_AUTH', 'false').lower() == 'true':
            return f(*args, **kwargs)

        if os.getenv('AUTH_BYPASS_LOCALHOST', 'true').lower() == 'true':
            if request.remote_addr in ['127.0.0.1', 'localhost', '::1']:
                return f(*args, **kwargs)

        if not session.get('authenticated'):
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return jsonify({'error': 'Unauthorized'}), 401
            return redirect(url_for('login', next=request.url))

        return f(*args, **kwargs)
    return decorated_function
