"""Cycle 32: two play-queue adds released from one barrier, N pairs, against ONE server base.

Counts the pairs whose two answers are the same container (version and last-added id).
A real PMS answers 0; a server that builds the answer after releasing its lock does not.
usage: race.py BASE [N]
"""
import json, sys, threading, urllib.request, urllib.parse, collections
B=sys.argv[1]; N=int(sys.argv[2]) if len(sys.argv)>2 else 60
H={'Accept':'application/json','X-Plex-Client-Identifier':'c32-race','X-Plex-Platform':'Chrome','X-Plex-Product':'Plex Web'}
def req(m,p):
    r=urllib.request.Request(B+p, headers=H, method=m)
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            b=resp.read(); return resp.status, (json.loads(b) if b[:1]==b'{' else b)
    except urllib.error.HTTPError as e:
        return e.code, e.read()
ident=req('GET','/identity')[1]['MediaContainer']['machineIdentifier']
secs=req('GET','/library/sections')[1]['MediaContainer']['Directory']
ms=[d['key'] for d in secs if d['type']=='movie'][0]
rks=[m['ratingKey'] for m in req('GET','/library/sections/%s/all'%ms)[1]['MediaContainer']['Metadata']]
def uri(rk): return urllib.parse.quote('server://%s/com.plexapp.plugins.library/library/metadata/%s'%(ident,rk))
same=0; statuses=collections.Counter()
for i in range(N):
    st,c=req('POST','/playQueues?type=video&uri=%s&own=1'%uri(rks[0]))
    qid=c['MediaContainer']['playQueueID']
    out=[None,None]; bar=threading.Barrier(2)
    def add(k, rk):
        bar.wait(); out[k]=req('PUT','/playQueues/%s?uri=%s'%(qid, uri(rk)))
    ts=[threading.Thread(target=add,args=(0,rks[1])),threading.Thread(target=add,args=(1,rks[2]))]
    [t.start() for t in ts]; [t.join() for t in ts]
    a,b=out
    statuses[(a[0],b[0])]+=1
    if a[0]==200 and b[0]==200:
        ka=(a[1]['MediaContainer'].get('playQueueVersion'), a[1]['MediaContainer'].get('playQueueLastAddedItemID'))
        kb=(b[1]['MediaContainer'].get('playQueueVersion'), b[1]['MediaContainer'].get('playQueueLastAddedItemID'))
        same += (ka==kb)
print(B, 'pairs', N, 'identical containers', same, dict(statuses))
