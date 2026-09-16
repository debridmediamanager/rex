"""Cycle 32: which characters the HUB search matcher folds, at a word's start and inside it.

GET only, kids. Reads sort_sweep.json (from sortfold.py) for the table hypothesis. A probe whose
control scores in the fuzzy band (a title's non-first word alone scores ~0.33) cannot tell a
fold from a miss: filter on the control score. Needs PLEXLAB_TOKEN.
"""
import json, os, sys, time, urllib.request, urllib.parse, unicodedata
T=os.environ['PLEXLAB_TOKEN']; BASE='http://kids:32400'
SORT=json.load(open('sort_sweep.json'))['s42']
# probe words: (title that must come back, query word, index of the letter to replace)
MID={'a':('Tears of Steel','Tears',1),'b':('The Train Job','Job',2),'c':('Big Buck Bunny','Buck',2),'d':('Bushwhacked','Bushwhacked',10),
 'e':('Sintel','Sintel',4),'f':('Firefly','Firefly',4),'g':('Big Buck Bunny','Big',2),'h':('Elephants Dream','Elephants',4),
 'i':('Sintel','Sintel',1),'k':('Big Buck Bunny','Buck',3),'l':('Sintel','Sintel',5),'m':('Cosmos Laundromat','Cosmos',3),
 'n':('Sintel','Sintel',2),'o':('Cosmos Laundromat','Cosmos',1),'p':('Elephants Dream','Elephants',3),'r':('Firefly','Firefly',2),
 's':('Cosmos Laundromat','Cosmos',2),'t':('Sintel','Sintel',3),'u':('Big Buck Bunny','Bunny',1),'w':('Bushwhacked','Bushwhacked',4),
 'y':('Firefly','Firefly',6)}
START={'b':('Big Buck Bunny','Bunny',0),'c':('Cosmos Laundromat','Cosmos',0),'d':('Elephants Dream','Dream',0),'e':('Elephants Dream','Elephants',0),
 'f':('Firefly','Firefly',0),'j':('The Train Job','Job',0),'l':('Cosmos Laundromat','Laundromat',0),'o':('Our Mrs. Reynolds','Our',0),
 'r':('Our Mrs. Reynolds','Reynolds',0),'s':('Sintel','Sintel',0),'t':('Tears of Steel','Tears',0),'m':('Our Mrs. Reynolds','Mrs.',0)}
def score(q, title):
    u=BASE+'/hubs/search?query=%s&limit=30'%urllib.parse.quote(q)
    for attempt in range(3):
        try:
            r=urllib.request.Request(u, headers={'X-Plex-Token':T,'Accept':'application/json','User-Agent':'curl/8'}, method='GET')
            c=json.load(urllib.request.urlopen(r, timeout=30))['MediaContainer']; break
        except Exception as e:
            time.sleep(1); c=None
    if c is None: return 'ERR'
    for h in c.get('Hub') or []:
        for m in h.get('Metadata') or []:
            if m.get('title')==title: return m.get('score')
    return None
def targets(ch):
    out=set()
    d=unicodedata.normalize('NFD',ch)[0].lower()
    if d.isascii() and d.isalpha(): out.add(d)
    k=unicodedata.normalize('NFKD',ch)
    if k and k[0].isascii() and k[0].isalpha(): out.add(k[0].lower())
    s=SORT.get('%04X'%ord(ch))
    if s and s!=ch and s.lower().isalpha(): out.add(s.lower())
    cf=ch.casefold()
    if cf and cf[0].isascii() and cf[0].isalpha(): out.add(cf[0])
    return out
ranges=[(0x80,0x250),(0x250,0x300),(0x1E00,0x1F00),(0x2100,0x2150),(0xFB00,0xFB07)]
results={}
controls={}
for pos,table in (('mid',MID),('start',START)):
    for L,(title,word,i) in table.items():
        controls[(pos,L)]=score(word,title)
print('controls', {'%s/%s'%k:v for k,v in controls.items()}, flush=True)
n=0
for a,b in ranges:
    for cp in range(a,b):
        ch=chr(cp)
        if unicodedata.category(ch)[0] in 'CZM': continue
        for L in sorted(targets(ch)):
            for pos,table in (('mid',MID),('start',START)):
                if L not in table: continue
                title,word,i=table[L]
                q=word[:i]+ch+word[i+1:]
                s=score(q,title); n+=1
                results['%04X|%s|%s'%(cp,L,pos)]={'q':q,'score':s,'control':controls[(pos,L)]}
json.dump(results, open('hub_sweep.json','w'), ensure_ascii=False, indent=0)
print('requests', n)
