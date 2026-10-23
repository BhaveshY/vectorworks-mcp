"""Apply an explicit section plan to an ALREADY OPEN working copy and verify save/reopen.

The private plan supplies document, change (targets/bands/labels/etc), guards,
expected_dimensions ({uuid: displayed text}), and optional sheet. No project data
is bundled. --output must be new; the original document is never overwritten.
Run only with authorization to edit and save the named working copy.
"""
from __future__ import annotations
import argparse
import base64
import json
import os
import shutil
from pathlib import Path
import sys
import time

from e2e_host import Client, ROOT
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--installed', action='store_true')
    args = parser.parse_args()
    plan = json.loads(args.plan.read_text(encoding='utf-8'))
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    doc = plan['document']
    change = plan['change']
    targets = change.get('targets') or [change['target']]
    guards = plan.get('guards', [])
    checks, calls = [], []
    started = time.perf_counter()
    command=['cmd.exe','/d','/c',str(Path(os.environ['USERPROFILE'])/'.vectorworks-mcp/bin/vectorworks-mcp.cmd')] if args.installed else None
    client = Client(sys.executable, dict(os.environ), 'section-file',command=command)
    completed=False

    def call(tool, arguments=None, allow_error=False):
        a = dict(arguments or {})
        if tool != 'vw_status': a.setdefault('document', doc)
        t = time.perf_counter()
        data, raw = client.call(tool, a, timeout=240)
        if tool=='vw_capture' and data.get('error',{}).get('code')=='still_running':
            job_id=data['error']['details']['job_id']
            deadline=time.monotonic()+180
            while time.monotonic()<deadline:
                time.sleep(2)
                status,_=client.call('vw_status',{'job_id':job_id})
                calls.append({'tool':'vw_status','arguments':{'job_id':job_id},'result':status})
                job=status.get('job',{})
                if job.get('state')=='done':
                    data={'ok':True,**job['result']}
                    shutil.copyfile(data['path'],out/f'{len(calls)+1:03d}-vw_capture-job.png')
                    break
                if job.get('state') not in ('queued','running'): raise AssertionError(status)
        # Keep the source snippets out of the call log; retain inputs and results.
        calls.append({'tool': tool, 'arguments': {k:v for k,v in a.items() if k!='code'},
                      'ms': round((time.perf_counter()-t)*1000), 'result': data})
        for i,b in enumerate(raw.get('content', [])):
            if b.get('type') == 'image':
                (out/f'{len(calls):03d}-{tool}-{i}.png').write_bytes(base64.b64decode(b['data']))
        (out/'calls.json').write_text(json.dumps(calls, indent=2, ensure_ascii=False), encoding='utf-8')
        if not allow_error and not data.get('ok'): raise AssertionError(data)
        return data

    def check(name, condition):
        checks.append({'name':name,'pass':bool(condition)})
        print(('PASS ' if condition else 'FAIL ')+name, flush=True)
        if not condition: raise AssertionError(name)

    def snapshot():
        # A bounded batch read through vw_script avoids one round-trip per loose
        # root. It uses exactly the geometry reader's helpers/revision algorithm.
        lib=ROOT/'vectorworks_mcp/scripts_lib'
        code='\n'.join((lib/(n+'.py')).read_text(encoding='utf-8') for n in ('_prelude','_geometry','_section'))
        code+='\nresult={}\nfor ref in '+repr(targets+guards)+':\n    rows=section_walk([resolve(ref)])\n    result[ref]={"revision":section_revision(rows),"data":[section_state(h) for h in rows]}\n'
        code+='\nresult["__definitions__"]={}\nfor name in '+repr(plan.get('symbol_definitions',[]))+':\n    c=vs.FInSymDef(vs.GetObject(name))\n    members=[]\n    while c:\n        members.extend(section_walk([c]))\n        c=vs.NextObj(c)\n    result["__definitions__"][name]=[section_state(h) for h in members]\n'
        return call('vw_script', {'code':code, 'undo_label':'Read section acceptance snapshot'})['result']

    def flat(s, roots):
        return {r['uuid']:r for root in roots for r in s[root]['data']}

    def ymap(y):
        shift=0
        for b in change['bands']:
            if y >= b['top']: shift+=b['delta']
            elif y > b['bottom']: shift+=b['delta']*(y-b['bottom'])/(b['top']-b['bottom'])
        return y+shift

    def close(a,b):
        if isinstance(a,(float,int)) and isinstance(b,(float,int)): return abs(a-b)<.01
        if isinstance(a,dict) and isinstance(b,dict): return a.keys()==b.keys() and all(close(a[k],b[k]) for k in a)
        if isinstance(a,list) and isinstance(b,list): return len(a)==len(b) and all(close(x,y) for x,y in zip(a,b))
        return a==b

    def verify(before, after):
        old,new=flat(before,targets),flat(after,targets)
        check('all persistent object IDs and hierarchy retained', old.keys()==new.keys() and all(old[u]['parent_uuid']==new[u]['parent_uuid'] for u in old))
        rigid={x['target']:x['elevation'] for x in change.get('rigid',[])}
        labels={x['target']:x for x in change.get('labels',[])}
        fixed={(x['target'],x['vertex']) for x in change.get('fixed_points',[])}
        for uid,r in old.items():
            if not r.get('points'): continue
            expected=[[x,ymap(y)] for x,y in r['points']]
            if uid in rigid:
                e=rigid[uid]; expected=[[x,y+ymap(e)-e] for x,y in r['points']]
            if uid in labels:
                e=labels[uid]['elevation']; expected[0][1]=r['points'][0][1]+ymap(e)-e
            for i,p in enumerate(r['points'],1):
                if (uid,i) in fixed: expected[i-1]=p
            check('mapped geometry '+uid, close(new[uid]['points'],expected))
            for key in ('type_id','class','layer','closed','vertex_types','radii','offset','dimension_class','show_value','primary','symbol','rotation','scale_type','scale'):
                if key in r: check(key+' retained '+uid,close(r[key],new[uid][key]))
            if r['type_id']==15:
                a,b=r['bounds'],new[uid]['bounds']
                check('symbol shape retained '+uid,close([a[1][i]-a[0][i] for i in (0,1)],[b[1][i]-b[0][i] for i in (0,1)]))
        for uid,value in plan['expected_dimensions'].items():
            check('measured dimension '+uid+' = '+value,new[uid]['dimension_text']==value)
        for uid,text in plan.get('expected_labels',{}).items():
            check('level label '+uid,new[uid]['text']==text)
        for uid in plan.get('preserve_heights',[]):
            height=lambda r:max(p[1] for p in r['points'])-min(p[1] for p in r['points'])
            check('slab/roof thickness retained '+uid,close(height(old[uid]),height(new[uid])))
        for left,li,right,ri in plan.get('align_y',[]):
            check('adjoining elevation aligned '+left,close(new[left]['points'][li][1],new[right]['points'][ri][1]))
        check('guard objects unchanged', all(close(before[u]['data'],after[u]['data']) for u in guards))
        check('shared symbol definitions unchanged',close(before['__definitions__'],after['__definitions__']))

    def display_check(s):
        chains=[r for r in flat(s,targets).values() if r.get('plugin')=='NNA_ChainDim']
        code='result={}\nfor uid in '+repr([r['uuid'] for r in chains])+':\n    h=vs.GetObjectByUuid(uid)\n    c=vs.FInGroup(h)\n    values=[]\n    while c:\n        if vs.GetTypeN(c)==63: values.append(vs.GetDimText(c))\n        c=vs.NextObj(c)\n    result[uid]=values'
        display=call('vw_script',{'code':code,'undo_label':'Independently read regenerated dimension display'})['result']
        rows=flat(s,targets)
        for r in chains:
            check('regenerated chain display matches source '+r['uuid'],display[r['uuid']]==[rows[u]['dimension_text'] for u in r['segments']])

    try:
        client.initialize()
        check('bridge ready',call('vw_status').get('ready'))
        before=snapshot()
        (out/'before.json').write_text(json.dumps(before,indent=2,ensure_ascii=False),encoding='utf-8')
        call('vw_capture',{'view':'objects','objects':targets,'max_px':1568})
        change['expected_geometry']={u:before[u]['revision'] for u in targets}
        if plan.get('rollback_probe'):
            from vectorworks_mcp.builders import compile_edit
            from vectorworks_mcp.scripts_lib import build
            ops=compile_edit([change],1,schemas={},descriptors={}).script_ops
            code=build('edit_engine',{'ops':ops})+'\nraise RuntimeError("section acceptance intentional rollback")\n'
            failed=call('vw_script',{'code':code,'undo_label':'Section acceptance rollback probe'},allow_error=True)
            check('injected failure rolls back',not failed.get('ok') and 'intentional rollback' in str(failed))
            rolled_back=snapshot()
            check('rollback restores chain sources, symbols, geometry and labels',all(close(before[u]['data'],rolled_back[u]['data']) for u in targets+guards))
            display_check(rolled_back)
            change['expected_geometry']={u:rolled_back[u]['revision'] for u in targets}
        result=call('vw_edit',{'changes':[change],'preview':False})
        check('section transaction verified',all(r.get('verified') for r in result['receipts']))
        after=snapshot()  # separate call: regeneration has now finished
        (out/'after.json').write_text(json.dumps(after,indent=2,ensure_ascii=False),encoding='utf-8')
        verify(before,after)
        display_check(after)
        call('vw_capture',{'view':'objects','objects':targets,'max_px':1568})
        if plan.get('sheet'): call('vw_capture',{'view':'sheet','sheet':plan['sheet'],'max_px':1568})
        # Changing the idempotency key exercises revision safety rather than cache.
        rejected=call('vw_edit',{'changes':[change],'idempotency_key':'section-file-stale-check','preview':False},allow_error=True)
        check('stale replay rejected',not rejected.get('ok') and 'changed since read' in str(rejected))
        path=out/'Patrick-section-tested.vwx'
        saved=call('vw_document',{'action':'save_as','path':str(path)})
        doc=saved['document']['fingerprint']
        call('vw_document',{'action':'close'})
        opened=call('vw_document',{'action':'open','path':str(path)})
        doc=opened['document']['fingerprint']
        reopened=snapshot()
        (out/'reopened.json').write_text(json.dumps(reopened,indent=2,ensure_ascii=False),encoding='utf-8')
        check('saved geometry and annotations survive reopen',all(close(after[u]['data'],reopened[u]['data']) for u in targets+guards))
        check('symbol definitions survive reopen unchanged',close(before['__definitions__'],reopened['__definitions__']))
        display_check(reopened)
        check('saved working copy exists',path.is_file() and path.stat().st_size>0)
        call('vw_capture',{'view':'objects','objects':targets,'max_px':1568})
        completed=True
    finally:
        report={'status':'PASS' if completed else 'FAIL','checks':checks,'passed':sum(c['pass'] for c in checks),'failed':sum(not c['pass'] for c in checks),
                'calls':len(calls),'elapsed_seconds':round(time.perf_counter()-started,2),'document':doc,
                'route':'installed' if args.installed else 'source'}
        (out/'report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
        client.close()


if __name__=='__main__': main()
