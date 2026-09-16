"""Cycle 32: the SORT-title fold, swept by WRITING titles on both writable copies.

Forty codepoints per title between two ASCII letters, read back from `titleSort`, then the
title is restored. Writes to plexscratch42 and plexscratch only. Needs SCRATCH42_TOKEN and
SCRATCH_TOKEN. Writes sort_sweep.json, which hubfold.py reads.
"""
import json, os, sys, urllib.request, urllib.parse, unicodedata
SERVERS={'s42':('http://100.85.222.1:32411', os.environ['SCRATCH42_TOKEN'], '4'), 's43':('http://100.85.222.1:32410', os.environ['SCRATCH_TOKEN'], '3')}
def req(base, tok, method, path):
    r=urllib.request.Request(base+path, headers={'X-Plex-Token':tok,'Accept':'application/json','User-Agent':'curl/8'}, method=method)
    with urllib.request.urlopen(r, timeout=30) as resp:
        body=resp.read(); return resp.status, (json.loads(body) if body[:1]==b'{' else body)
ranges=[(0x80,0x250),(0x250,0x300),(0x370,0x400),(0x400,0x530),(0x1D00,0x1DC0),(0x1E00,0x1F00),(0x2000,0x2070),(0x2100,0x2150),(0x2C60,0x2C80),(0xA720,0xA800),(0xFF00,0xFFF0)]
cps=[]
for a,b in ranges:
    for cp in range(a,b):
        ch=chr(cp)
        cat=unicodedata.category(ch)
        if cat[0] in 'CZ' or cat=='Mn' or ch.isspace(): continue
        cps.append(cp)
print('codepoints', len(cps))
result={n:{} for n in SERVERS}
BATCH=40
for name,(base,tok,rk) in SERVERS.items():
    st,c=req(base,tok,'GET','/library/metadata/%s'%rk); old=c['MediaContainer']['Metadata'][0]['title']
    try:
        for i in range(0,len(cps),BATCH):
            chunk=cps[i:i+BATCH]
            title='Zq ' + ' '.join('a%sb' % chr(cp) for cp in chunk)
            req(base,tok,'PUT','/library/metadata/%s?title.value=%s'%(rk, urllib.parse.quote(title)))
            st,c=req(base,tok,'GET','/library/metadata/%s'%rk); m=c['MediaContainer']['Metadata'][0]
            src=(m.get('titleSort') or m['title'])
            words=src.split(' ')
            if words[0]!='Zq' or len(words)!=len(chunk)+1:
                print(name,'SHAPE', i, repr(src[:120]), len(words), len(chunk)); 
                # fall back: probe one by one
                for cp in chunk:
                    t='Zq a%sb' % chr(cp)
                    req(base,tok,'PUT','/library/metadata/%s?title.value=%s'%(rk, urllib.parse.quote(t)))
                    st,c=req(base,tok,'GET','/library/metadata/%s'%rk); m=c['MediaContainer']['Metadata'][0]
                    s=(m.get('titleSort') or m['title'])
                    result[name]['%04X'%cp]=s[4:-1] if s.startswith('Zq a') and s.endswith('b') else '?'+s
                continue
            for cp,w in zip(chunk, words[1:]):
                result[name]['%04X'%cp]=w[1:-1]
    finally:
        req(base,tok,'PUT','/library/metadata/%s?title.value=%s'%(rk, urllib.parse.quote(old)))
        st,c=req(base,tok,'GET','/library/metadata/%s'%rk); print(name,'restored', repr(c['MediaContainer']['Metadata'][0]['title']), repr(c['MediaContainer']['Metadata'][0].get('titleSort')))
json.dump(result, open('sort_sweep.json','w'), ensure_ascii=False, indent=0)
folded={n:{k:v for k,v in r.items() if v!=chr(int(k,16))} for n,r in result.items()}
print('agree', result['s42']==result['s43'])
for n in folded: print(n, len(folded[n]), json.dumps(folded[n], ensure_ascii=False))
