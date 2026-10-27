"""Run an isolated password-protected demo behind a temporary HTTPS tunnel."""
import json
import argparse
import os
from pathlib import Path
import re
import secrets
import subprocess
import threading
import time
import uuid

from app import create_server
from power_agent.agent import load_env
from power_agent.sharing import ShareAccess, snapshot_knowledge
from power_agent.storage import create_knowledge_store
from power_agent.postgres import PostgresKnowledgeBase
from power_agent.accounts import AccountStore

ROOT = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--resume', action='store_true', help='Reuse the stopped demo data and password; create a new temporary URL.')
    args = parser.parse_args()
    load_env(ROOT / '.env')
    share_root = ROOT / 'data' / 'sharing'
    share_root.mkdir(parents=True, exist_ok=True)
    # The localhost port also prevents duplicate launchers sharing a state file.
    previous = json.loads((share_root / 'current.json').read_text(encoding='utf-8')) if (share_root / 'current.json').exists() else None
    if previous and (previous.get('status') != 'stopped' or not re.fullmatch(r'[a-f0-9]{32}', previous.get('run_id', ''))
                     or previous.get('schema') != 'demo_' + previous['run_id']):
        raise ValueError('请先停止现有分享，且只能恢复原演示空间。')
    run_id = previous['run_id'] if previous else uuid.uuid4().hex
    folder = share_root / run_id
    folder.mkdir(exist_ok=bool(previous))
    source = create_knowledge_store(ROOT / 'data')
    target = PostgresKnowledgeBase(source.dsn, source.embeddings, schema=previous['schema'], min_similarity=source.min_similarity) if previous else snapshot_knowledge(source, 'demo_' + run_id)
    accounts = AccountStore(target)
    if not accounts.list():
        admin_password = secrets.token_urlsafe(24)
        accounts.create('admin', admin_password, role='admin')
        (share_root / 'admin-access.md').write_text(
            '# 管理员初始登录信息\n\n账号：`admin`\n\n初始密码：`' + admin_password + '`\n\n'
            '登录后可通过「账号管理」创建独立用户。此文件仅供管理员查看，不要分享给其他用户。'
            '\n\n修改管理员密码后此初始密码即失效；最新访问地址见 access.md。\n', encoding='utf-8')
    access = ShareAccess(accounts=accounts)
    server = create_server(8001, folder, knowledge_base=target, access=access)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    state = {'pid': os.getpid(), 'run_id': run_id, 'schema': target.schema,
             'port': server.server_port, 'status': 'starting', 'auth_mode': 'multiuser'}
    state_file = share_root / 'current.json'
    stop_file = share_root / (run_id + '.stop')
    if previous:
        stop_file.unlink(missing_ok=True)
    def save():
        temporary = share_root / 'current.tmp'
        temporary.write_text(json.dumps(state, ensure_ascii=False), encoding='utf-8')
        temporary.replace(state_file)
    tunnel = None
    try:
        save()
        with (folder / 'tunnel.log').open('w', encoding='utf-8') as log:
            tunnel = subprocess.Popen([str(ROOT / 'data/tools/cloudflared.exe'), 'tunnel', '--no-autoupdate',
                '--url', f'http://127.0.0.1:{server.server_port}', '--protocol', 'http2'],
                stdout=log, stderr=subprocess.STDOUT, cwd=folder,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            state['tunnel_pid'] = tunnel.pid
            save()
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                if tunnel.poll() is not None:
                    raise RuntimeError('隧道启动失败，请检查本次 tunnel.log。')
                if stop_file.exists():
                    return
                match = re.search(r'https://[a-z0-9-]+\.trycloudflare\.com', (folder / 'tunnel.log').read_text(encoding='utf-8', errors='replace'))
                if match:
                    access.set_url(match[0])
                    state.update(url=match[0], status='ready')
                    save()
                    (share_root / 'access.md').write_text(
                        '# 电知多用户访问\n\n'
                        f'地址：{match[0]}\n\n每位用户使用管理员分配的独立账号密码。\n\n'
                        '管理员初始信息保存在同目录 admin-access.md，仅供管理员本人查看。\n\n'
                        '个人对话、长期记忆、资料和报告按账号隔离；公共资料按授权共享阅读。\n\n'
                        '请保持本机联网且不休眠。关闭分享：在项目目录运行 `powershell -ExecutionPolicy Bypass -File stop-share.ps1`。\n', encoding='utf-8')
                    print('Temporary sharing ready: ' + match[0], flush=True)
                    break
                time.sleep(.5)
            else:
                raise RuntimeError('90 秒内未取得分享地址，请检查隧道网络。')
            while tunnel.poll() is None and not stop_file.exists():
                time.sleep(.5)
    except Exception as error:
        state['error'] = str(error)
        raise
    finally:
        if tunnel and tunnel.poll() is None:
            tunnel.terminate()
            try:
                tunnel.wait(10)
            except subprocess.TimeoutExpired:
                tunnel.kill()
                tunnel.wait()
        server.shutdown()
        server.server_close()
        state['status'] = 'stopped'
        save()


if __name__ == '__main__':
    main()
