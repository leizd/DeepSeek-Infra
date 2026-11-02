"""Actual native online rejection on an independent fixture; no SQL state or provider credential is seeded."""
from __future__ import annotations

from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import threading
from typing import Any

from skills_mutation_routes_e2e import native
from skills_read_routes_e2e import TOKEN


def verify_rejection(gateway: Path, source: Path) -> dict[str, Any]:
    key='fixture-online-rejection-canary'
    requests: list[dict[str, Any]]=[]

    class Provider(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            size=int(self.headers.get('Content-Length','0'))
            if not 0<size<2_000_000:
                self.send_error(400)
                return
            body=json.loads(self.rfile.read(size))
            requests.append({'path':self.path,'credentialMatched':self.headers.get('Authorization')==f'Bearer {key}',
                'nonStreaming':body.get('stream') is False})
            raw=json.dumps({'error':{'message':f'isolated provider rejected {key}'}}).encode()
            self.send_response(401)
            self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, format: str, *args: Any) -> None:
            return

    server=ThreadingHTTPServer(('127.0.0.1',0),Provider)
    thread=threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix='native-skills-rejection-') as directory:
            root=Path(directory)
            # Only packaged static inputs are copied. All runtime journals and
            # databases are created by the actual native process from empty.
            for name in ['skills','ui']:
                shutil.copytree(source/name,root/name)
            url=f'http://127.0.0.1:{server.server_port}/chat/completions'
            with native(gateway,root,'python_disabled',upstream_url=url) as port:
                connection=http.client.HTTPConnection('127.0.0.1',port,timeout=15)
                try:
                    connection.request('POST','/api/skills/plain/run',json.dumps({'apiKey':key,'input':{'topic':'actual provider rejection'}}),
                        {'Content-Type':'application/json','Authorization':f'Bearer {TOKEN}'})
                    response=connection.getresponse()
                    status=response.status
                    body=json.loads(response.read())
                finally:
                    connection.close()
            runs_path=root/'.skills/runs/runs.jsonl'
            runs=[json.loads(line) for line in runs_path.read_text(encoding='utf8').splitlines() if line.strip()] if runs_path.exists() else []
            trace_path=root/'.traces/traces.sqlite3'
            trace_statuses: list[str]=[]
            if trace_path.exists():
                with closing(sqlite3.connect(trace_path.as_uri()+'?mode=ro',uri=True)) as database:
                    trace_statuses=[row[0] for row in database.execute('SELECT status FROM trace_runs')]
            persisted=False
            for name in ['.skills','.traces','.generated']:
                for path in (root/name).rglob('*'):
                    if path.is_file() and key.encode() in path.read_bytes():
                        persisted=True
            qualified=(status==502 and key not in json.dumps(body) and len(requests)==1
                and requests[0]=={'path':'/chat/completions','credentialMatched':True,'nonStreaming':True}
                and len(runs)==1 and runs[0]['status']=='failed' and trace_statuses==['failed']
                and not list((root/'.generated').glob('*.md')) and not persisted)
            return {'qualified':qualified,'scope':'one actual native process, real isolated HTTP401 and real failed journals only',
                'nativeStatus':status,'providerRequests':len(requests),'providerObservations':requests,
                'failedRuns':len(runs),'traceStatuses':trace_statuses,'credentialPersisted':persisted,
                'sqlStateSeeded':False,'fullMigrationQualified':False}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        if thread.is_alive():
            raise RuntimeError('isolated rejection provider did not stop')
