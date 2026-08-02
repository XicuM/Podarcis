'''Podarcis Server Starlette/FastAPI application for Web Login, Dynamic Reverse Proxy, and Admin Control Panel.'''

import os
import sys
from pathlib import Path
from typing import Any, Dict

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import HTMLResponse, RedirectResponse, Response, JSONResponse
from starlette.routing import Route, Mount, WebSocketRoute
from starlette.templating import Jinja2Templates
from starlette.websockets import WebSocket

import httpx

from podarcis.server.user_manager import UserManager
from podarcis.common import get_config_value, set_config_value

root_dir = Path(__file__).resolve().parent.parent.parent
templates = Jinja2Templates(directory=str(Path(__file__).parent / 'templates'))
user_manager = UserManager(root_dir)


async def route_home(request):
    user = request.session.get('user')
    if user:
        if user == 'admin':
            return RedirectResponse(url='/admin')
        return RedirectResponse(url=f'/user/{user}/')
    return RedirectResponse(url='/login')


async def route_login_get(request):
    error = request.query_params.get('error')
    return templates.TemplateResponse('login.html', {'request': request, 'error': error})


async def route_login_post(request):
    form = await request.form()
    username = str(form.get('username', '')).strip().lower()
    password = str(form.get('password', ''))

    if not username or not password:
        return templates.TemplateResponse('login.html', {'request': request, 'error': 'Username and password are required.'})

    user_info = user_manager.authenticate_user(username, password)
    if not user_info:
        return templates.TemplateResponse('login.html', {'request': request, 'error': 'Invalid username or password.'})

    # Ensure user container is registered & started
    user_manager.start_user_container(username)

    request.session['user'] = username
    if user_info.get('role') == 'admin':
        return RedirectResponse(url='/admin', status_code=303)
    return RedirectResponse(url=f'/user/{username}/', status_code=303)


async def route_logout(request):
    request.session.clear()
    return RedirectResponse(url='/login')


async def route_admin(request):
    user = request.session.get('user')
    if not user:
        return RedirectResponse(url='/login')

    user_info = user_manager.get_users_registry().get(user)
    if not user_info or user_info.get('role') != 'admin':
        return HTMLResponse('<h1>403 Forbidden: Admin Access Required</h1>', status_code=403)

    registry = user_manager.get_users_registry()
    users_list = []
    for uname, udata in registry.items():
        c_info = user_manager.get_container_for_user(uname)
        item = {
            'username': uname,
            'role': udata.get('role', 'user'),
            'status': c_info.get('status') if c_info else 'Stopped',
            'port': c_info.get('port') if c_info else None,
        }
        users_list.append(item)

    return templates.TemplateResponse('admin.html', {'request': request, 'users': users_list})


async def route_admin_config(request):
    user = request.session.get('user')
    user_info = user_manager.get_users_registry().get(user, {}) if user else {}
    if user_info.get('role') != 'admin':
        return RedirectResponse(url='/login', status_code=303)

    form = await request.form()
    if 'backend' in form:
        set_config_value(root_dir, str(form['backend']), 'backend')
    if 'sources_backend' in form:
        set_config_value(root_dir, str(form['sources_backend']), 'sources_backend')
    if 'default_model' in form:
        set_config_value(root_dir, str(form['default_model']), 'default_model')
    return RedirectResponse(url='/admin', status_code=303)


async def route_admin_user_create(request):
    user = request.session.get('user')
    user_info = user_manager.get_users_registry().get(user, {}) if user else {}
    if user_info.get('role') != 'admin':
        return RedirectResponse(url='/login', status_code=303)

    form = await request.form()
    username = str(form.get('username', '')).strip().lower()
    role = str(form.get('role', 'user'))
    password = str(form.get('password', '')).strip()
    if username:
        try:
            user_manager.create_user(username, role, password=password if password else None)
            user_manager.start_user_container(username)
        except Exception:
            pass
    return RedirectResponse(url='/admin', status_code=303)


async def route_admin_user_password(request):
    user = request.session.get('user')
    user_info = user_manager.get_users_registry().get(user, {}) if user else {}
    if user_info.get('role') != 'admin':
        return RedirectResponse(url='/login', status_code=303)

    form = await request.form()
    username = str(form.get('username', '')).strip().lower()
    password = str(form.get('password', '')).strip()
    if username and password:
        try:
            user_manager.set_user_password(username, password)
        except Exception:
            pass
    return RedirectResponse(url='/admin', status_code=303)


async def route_admin_user_start(request):
    user = request.session.get('user')
    user_info = user_manager.get_users_registry().get(user, {}) if user else {}
    if user_info.get('role') != 'admin':
        return RedirectResponse(url='/login', status_code=303)

    form = await request.form()
    username = str(form.get('username', ''))
    if username:
        user_manager.start_user_container(username)
    return RedirectResponse(url='/admin', status_code=303)


async def route_admin_user_stop(request):
    user = request.session.get('user')
    user_info = user_manager.get_users_registry().get(user, {}) if user else {}
    if user_info.get('role') != 'admin':
        return RedirectResponse(url='/login', status_code=303)

    form = await request.form()
    username = str(form.get('username', ''))
    if username:
        user_manager.stop_user_container(username)
    return RedirectResponse(url='/admin', status_code=303)


async def route_admin_user_delete(request):
    user = request.session.get('user')
    user_info = user_manager.get_users_registry().get(user, {}) if user else {}
    if user_info.get('role') != 'admin':
        return RedirectResponse(url='/login', status_code=303)

    form = await request.form()
    username = str(form.get('username', ''))
    if username:
        try:
            user_manager.delete_user(username)
        except Exception:
            pass
    return RedirectResponse(url='/admin', status_code=303)


async def route_user_proxy(request):
    '''Dynamic Reverse Proxy forwarding requests to individual user containers.'''
    user = request.path_params.get('username')
    session_user = request.session.get('user')
    if not session_user:
        return RedirectResponse(url='/login')

    session_user_info = user_manager.get_users_registry().get(session_user, {})
    if session_user != user and session_user_info.get('role') != 'admin':
        return HTMLResponse('<h1>403 Forbidden: Cannot access another user workspace</h1>', status_code=403)

    subpath = request.path_params.get('path', '')

    c_info = user_manager.get_container_for_user(user)
    if not c_info or not c_info.get('port'):
        # Auto-start user container if requested
        c_info = user_manager.start_user_container(user)

    target_port = c_info.get('port', '9001')
    target_url = f'http://127.0.0.1:{target_port}/{subpath}'

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            req_headers = dict(request.headers)
            req_headers.pop('host', None)

            body = await request.body()
            resp = await client.request(
                method=request.method,
                url=target_url,
                headers=req_headers,
                content=body,
                params=dict(request.query_params),
            )
            return Response(
                content=resp.content,
                status_code=resp.status_code,
                headers=dict(resp.headers),
            )
        except Exception as err:
            return HTMLResponse(
                f'''
                <div style="font-family: sans-serif; padding: 40px; background: #0a0d14; color: #fff; min-height: 100vh;">
                  <h2>Podarcis User Workspace Connecting...</h2>
                  <p>User Container <strong>podarcis-user-{user}</strong> is launching on target port {target_port}.</p>
                  <p style="color: #9ca3af;">Status: {c_info.get("status")}</p>
                  <p style="color: #29b8db;">Reload in a few seconds once container initializes.</p>
                </div>
                ''',
                status_code=503,
            )


routes = [
    Route('/', route_home),
    Route('/login', route_login_get, methods=['GET']),
    Route('/login', route_login_post, methods=['POST']),
    Route('/logout', route_logout),
    Route('/admin', route_admin, methods=['GET']),
    Route('/admin/config', route_admin_config, methods=['POST']),
    Route('/admin/user/create', route_admin_user_create, methods=['POST']),
    Route('/admin/user/password', route_admin_user_password, methods=['POST']),
    Route('/admin/user/start', route_admin_user_start, methods=['POST']),
    Route('/admin/user/stop', route_admin_user_stop, methods=['POST']),
    Route('/admin/user/delete', route_admin_user_delete, methods=['POST']),
    Route('/user/{username}', route_user_proxy, methods=['GET', 'POST', 'PUT', 'DELETE']),
    Route('/user/{username}/{path:path}', route_user_proxy, methods=['GET', 'POST', 'PUT', 'DELETE']),
]

middleware = [
    Middleware(SessionMiddleware, secret_key=os.environ.get('SECRET_KEY', 'podarcis-secret-key-multiuser-2026'))
]

app = Starlette(debug=True, routes=routes, middleware=middleware)


def run_server(host: str = '0.0.0.0', port: int = 8080) -> None:
    import uvicorn
    uvicorn.run(app, host=host, port=port)


if __name__ == '__main__':
    run_server()
