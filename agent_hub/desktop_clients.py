"""Explicitly bind and launch installed applications; never claim inference from a PID."""
import json
from pathlib import Path
import subprocess

from .discovery import _app_inventory


def installed_apps():
    entries = _app_inventory([Path('/Applications'), Path.home() / 'Applications'])
    result = []
    seen = set()
    for app in entries:
        path = str(Path(app['path']).resolve())
        if not app.get('bundle_id') or app['bundle_id'] == 'local.agenthub.desktop' or path in seen:
            continue
        # Only apps directly installed in the two user-visible Applications folders.
        if Path(path).parent not in (Path('/Applications'), Path.home() / 'Applications'):
            continue
        seen.add(path)
        result.append({'path':path,'name':app['name'],'bundle_id':app['bundle_id'],'version':app['version']})
    return result


def resolve_app(agent, apps=None):
    apps = installed_apps() if apps is None else apps
    path = agent.get('app_path')
    if path:
        candidates = [a for a in apps if a['path'] == path]
    else:
        name = agent.get('name', '').strip().casefold()
        candidates = [a for a in apps if name in (a['name'].casefold(), Path(a['path']).stem.casefold())]
    if len(candidates) != 1:
        raise ValueError('没有找到唯一匹配的已安装应用，请在“编辑”中选择绑定应用')
    app = candidates[0]
    if agent.get('desktop_bundle_id') and app['bundle_id'] != agent['desktop_bundle_id']:
        raise ValueError('应用身份已变化，请重新绑定后检查')
    return app


def open_and_check(store, aid):
    from .core import identifier, now, dump
    from .management import idle
    identifier(aid)
    with idle(store):
        agent = next((a for a in store.agents() if a['id'] == aid), None)
        if not agent or agent.get('transport') == 'http_api':
            raise ValueError('请选择桌面客户端；API 请使用连接测试')
        app = resolve_app(agent)
        server = Path(__file__).resolve().parents[1]
        helper = server.parent / 'ClientProbe'
        if not helper.is_file():
            helper = server / 'macos-app' / 'ClientProbe'
        if not helper.is_file():
            raise ValueError('桌面检查组件未安装，请重新打开最新版桌面工作台')
        try:
            proc = subprocess.run([str(helper), app['path'], app['bundle_id']], capture_output=True, text=True, timeout=15)
            raw = json.loads(proc.stdout)
            started = proc.returncode == 0 and raw.get('started') is True and raw.get('bundle_id') == app['bundle_id'] and raw.get('app_path') == app['path']
            note = '应用已启动；自动提交任务和读取回复尚未验证' if started else '未确认启动，请检查系统提示后重试'
        except (subprocess.TimeoutExpired, OSError, ValueError, TypeError):
            started = False; note = '启动检查未完成，请确认应用可以正常打开后重试'
        result = {'checked_at':now(), 'started':started, 'task_verified':False,
                  'app_path':app['path'], 'bundle_id':app['bundle_id'], 'version':app['version'], 'note':note}
        agent.update(app_path=app['path'], desktop_bundle_id=app['bundle_id'], desktop_check=result)
        with store.connect() as con:
            con.execute('UPDATE agents SET data=? WHERE id=?', (dump(agent),aid))
        store.event(None,None,'agent.desktop_checked',{'agent':aid,**result})
        return result
