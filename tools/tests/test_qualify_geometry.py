import asyncio
import ast
import copy
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer
from tools.qualify_geometry import qualify
from tools.geometry_relay import MAX_MESH

SESSION='12345678-1234-4321-9876-123456789abc'
OTHER='22345678-1234-4321-9876-123456789abc'
ROOT=Path(__file__).resolve().parents[2]


def mesh():
    return struct.pack('<II',3,3)+b''.join(struct.pack('<3f4B',*v,120,140,160,0)
        for v in ((0,0,0),(1,0,0),(0,1,0)))+struct.pack('<3I',0,1,2)


def status():
    return dict(type='status',geometry_source='IPHONE ARKIT MESH',session_id=SESSION.upper(),map_epoch=1,
                generation=7,revision=8,mesh_revision=8,points=3,triangles=1,fresh=True,connected=True,
                geometry=dict(source='IPHONE ARKIT MESH',matched=True,retained=False,connected=True,
                              snapshot_complete=True,revision=4,receiver_revision=5))


class QualifyGeometryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.packet=status(); self.body=mesh(); self.requests=[]; self.status_count=0
        self.headers={'X-Geometry-Source':'IPHONE ARKIT MESH','X-Geometry-Retained':'false',
                      'X-Map-Session':SESSION,'X-Map-Epoch':'1','X-Map-Generation':'7','X-Map-Revision':'8'}
        self.hook=None; self.delay=0; self.encoding=None; self.redirect=False
        async def get_status(request):
            self.requests.append((request.method,request.path,request.headers.get('Accept-Encoding')))
            self.status_count+=1
            if self.delay: await asyncio.sleep(self.delay)
            if self.hook: self.hook(self.status_count)
            return web.json_response(self.packet)
        async def get_mesh(request):
            self.requests.append((request.method,request.path,request.headers.get('Accept-Encoding')))
            if self.redirect: raise web.HTTPFound('/ws/control')
            headers=self.headers.copy()
            if self.encoding: headers['Content-Encoding']=self.encoding
            return web.Response(body=self.body,headers=headers)
        app=web.Application(); app.router.add_get('/map/status',get_status); app.router.add_get('/map/mesh.bin',get_mesh)
        self.server=TestServer(app); await self.server.start_server()
        self.origin=str(self.server.make_url('/'))

    async def asyncTearDown(self):
        await self.server.close()

    async def test_complete_source_path_and_receiver_may_advance(self):
        result=await qualify(self.origin)
        self.assertTrue(result['qualified'],result)
        self.assertEqual(result['evidence']['mesh']['vertices'],3)
        self.assertEqual(result['evidence']['mesh']['triangles'],1)
        self.assertEqual(result['evidence']['mesh']['session_id'],SESSION)
        self.assertEqual(result['evidence']['status_before']['receiver_revision'],5)
        self.assertEqual(self.requests,[('GET','/map/status','identity'),('GET','/map/mesh.bin','identity'),('GET','/map/status','identity')])
        self.assertIn('not the 500 ms',result['limitations'][1])

    async def test_receiver_revision_can_advance_without_changing_published_snapshot(self):
        def advance(count):
            if count == 2: self.packet['geometry']['receiver_revision'] = 6
        self.hook=advance
        result=await qualify(self.origin)
        self.assertTrue(result['qualified'],result)
        self.assertEqual(result['attempts'],1)
        self.assertEqual(result['evidence']['status_before']['receiver_revision'],5)
        self.assertEqual(result['evidence']['status_after']['receiver_revision'],6)

    async def test_byte_limits_and_strict_decimal_header_counters(self):
        original=self.body
        self.body=b'x'*(MAX_MESH+1)
        result=await qualify(self.origin)
        self.assertFalse(result['qualified']); self.assertIn('byte limit',result['errors'][0])
        self.body=original
        for value in ('-1','+1','1.0','01','1e0',str(2**64),'9'*4500):
            with self.subTest(header=value[:20]):
                self.headers['X-Map-Epoch']=value
                self.assertFalse((await qualify(self.origin))['qualified'])
        self.headers['X-Map-Epoch']='1'
        self.packet['irrelevant']='x'*65536
        result=await qualify(self.origin)
        self.assertFalse(result['qualified']); self.assertIn('byte limit',result['errors'][0])

    async def test_python310_api_compatibility_and_overall_deadline_cleanup(self):
        # Python3.10 has no asyncio.timeout. Exercise with that API unusable,
        # and parse the source under the supported syntax version too.
        source=(ROOT/'tools/qualify_geometry.py').read_text()
        ast.parse(source,feature_version=(3,10))
        # Limit only the helper's asyncio API surface; aiohttp legitimately
        # chooses its own version-specific timeout implementation on3.11.
        compatible=SimpleNamespace(wait_for=asyncio.wait_for,TimeoutError=asyncio.TimeoutError)
        with patch('tools.qualify_geometry.asyncio',compatible):
            self.assertTrue((await qualify(self.origin))['qualified'])
        self.delay=.03
        sessions=[]
        real_session=aiohttp.ClientSession
        def tracked_session(*args,**kwargs):
            session=real_session(*args,**kwargs); sessions.append(session)
            return session
        with patch('tools.qualify_geometry.aiohttp.ClientSession',side_effect=tracked_session):
            # Each status read can finish within50ms, but two status reads
            # exceed the single overall50ms qualification deadline.
            result=await qualify(self.origin,timeout=.05)
        self.assertFalse(result['qualified'])
        self.assertIn('Overall timeout',result['errors'][0])
        self.assertEqual(result['attempts'],1)
        self.assertTrue(sessions)
        self.assertTrue(all(session.closed for session in sessions))

    async def test_unknown_partial_retained_disconnected_or_stale_fails(self):
        cases=[('snapshot_complete',None),('snapshot_complete',False),('matched',False),('retained',True),('connected',False)]
        for key,value in cases:
            with self.subTest(key=key,value=value):
                self.packet=status(); self.packet['geometry'][key]=value
                result=await qualify(self.origin)
                self.assertFalse(result['qualified']); self.assertTrue(result['errors'])
        for key in ('fresh','connected'):
            self.packet=status(); self.packet[key]=False
            self.assertFalse((await qualify(self.origin))['qualified'])
        self.packet=status(); del self.packet['geometry']['snapshot_complete']
        self.assertFalse((await qualify(self.origin))['qualified'])

    async def test_invalid_identity_boolean_counter_source_and_header_rejected(self):
        for key,value in [('session_id',OTHER),('map_epoch',True),('revision',True),('points',0),('triangles',0),('generation',-1),('mesh_revision',2**64),('geometry_source','SIMULATED')]:
            with self.subTest(key=key):
                self.packet=status(); self.packet[key]=value
                self.assertFalse((await qualify(self.origin))['qualified'])
        self.packet=status()
        for key,value in [('X-Map-Session',OTHER),('X-Map-Epoch','2'),('X-Map-Generation','8'),('X-Map-Revision','true'),('X-Geometry-Retained','true'),('X-Geometry-Source','SIMULATED')]:
            with self.subTest(header=key):
                old=self.headers[key]; self.headers[key]=value
                self.assertFalse((await qualify(self.origin))['qualified'])
                self.headers[key]=old

    async def test_transient_race_retries_but_continuous_revision_race_fails(self):
        def once(count):
            if count==2:
                self.packet['revision']=self.packet['mesh_revision']=9; self.headers['X-Map-Revision']='9'
        self.hook=once
        result=await qualify(self.origin)
        self.assertTrue(result['qualified'],result); self.assertEqual(result['attempts'],2)
        def always(count):
            self.packet['revision']+=1; self.packet['mesh_revision']+=1
            self.headers['X-Map-Revision']=str(self.packet['revision'])
        self.hook=always
        result=await qualify(self.origin)
        self.assertFalse(result['qualified']); self.assertEqual(result['attempts'],3)
        self.assertIn('No stable',result['errors'][0])

    async def test_truncated_invalid_topology_nonfinite_and_mismatched_counts(self):
        for body in (mesh()[:-1],mesh()[:-4]+struct.pack('<I',3),mesh()[:8]+struct.pack('<f',float('nan'))+mesh()[12:],struct.pack('<II',0,0)):
            self.body=body; result=await qualify(self.origin)
            self.assertFalse(result['qualified']); self.assertTrue(result['errors'])

    async def test_encoding_redirect_timeout_and_bad_origin_fail_without_controls(self):
        self.encoding='gzip'; self.assertFalse((await qualify(self.origin))['qualified'])
        self.encoding=None; self.redirect=True; self.assertFalse((await qualify(self.origin))['qualified'])
        self.redirect=False; self.delay=.1
        result=await qualify(self.origin,timeout=.02)
        self.assertFalse(result['qualified']); self.assertIn('Overall timeout',result['errors'][0])
        for timeout in (0,-1,float('inf'),float('nan'),True):
            self.assertFalse((await qualify(self.origin,timeout))['qualified'])
        for origin in ('http://example.com','http://user:secret@127.0.0.1','http://127.0.0.1/path'):
            result=await qualify(origin)
            self.assertFalse(result['qualified']); self.assertNotIn('secret',json.dumps(result))
        self.assertTrue(all(method=='GET' and path in ('/map/status','/map/mesh.bin') for method,path,_ in self.requests))

    async def test_cli_success_and_persisted_failure_exit_status(self):
        with tempfile.TemporaryDirectory() as directory:
            output=Path(directory)/'evidence.json'
            command=[sys.executable,'-m','tools.qualify_geometry','--relay',self.origin,'--output',str(output)]
            run=lambda: subprocess.run(command,cwd=ROOT,capture_output=True,text=True,timeout=10)
            good=await asyncio.to_thread(run)
            self.assertEqual(good.returncode,0,good.stderr+good.stdout)
            self.assertTrue(json.loads(output.read_text())['qualified'])
            self.packet['geometry']['snapshot_complete']=None
            bad=await asyncio.to_thread(run)
            self.assertEqual(bad.returncode,1)
            evidence=json.loads(output.read_text())
            self.assertEqual(evidence,json.loads(bad.stdout)); self.assertFalse(evidence['qualified'])
            self.assertFalse(list(Path(directory).glob('.geometry-check-*')))
